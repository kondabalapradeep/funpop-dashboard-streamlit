"""Durable snapshot of dashboard query results, stored in the repo.

The dashboard's expensive work is the BigQuery pulls. Streamlit Community Cloud
discards in-memory ``@st.cache_data`` whenever the app process restarts (sleep,
redeploy, recycle), so a visitor who lands after a restart pays the full query
cost again — which is why the dashboard wasn't already loaded at 8am.

The service-account key the dashboard uses is **read-only**, so we cannot write
a snapshot back into BigQuery. Instead a scheduled GitHub Actions job
(``snapshot_build.py``) runs the same read-only queries and commits each result
as a parquet file under ``snapshot_data/``. The deployed app reads those files
on a cache miss instead of re-querying. Any miss/staleness/error degrades to a
live query, so behaviour is never worse than a direct pull.

This module is import-safe outside Streamlit (no ``streamlit`` import) so both
the app and the builder can share it.
"""
import hashlib
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger("funpop_dashboard.snapshot")

# Committed snapshot lives alongside the code so the deployed app has it on disk.
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshot_data"
# Single timestamp for the whole snapshot; the freshness anchor (see below).
MANIFEST_PATH = SNAPSHOT_DIR / "built_at.txt"

# Ignore a snapshot older than this. If the builder stops running we fall back
# to a live query (which the read-only key can still do) rather than serve stale
# data forever. 30h tolerates one fully-missed daily build.
MAX_AGE_SECONDS = 30 * 3600


def snapshot_key(sql_filename: str, params=None) -> str:
    """Stable key for a (query, params) pair. Must match between the builder and
    the app, so it depends only on the filename and the parameter *values*."""
    parts = [sql_filename]
    for p in params or []:
        if hasattr(p, "values"):          # ArrayQueryParameter
            parts.append(f"{p.name}={list(p.values)}")
        else:                              # ScalarQueryParameter
            parts.append(f"{p.name}={p.value}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parquet_path(key: str) -> Path:
    return SNAPSHOT_DIR / f"{key}.parquet"


def parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


# ── Builder side ─────────────────────────────────────────────────────────────
def write_if_changed(key: str, df: pd.DataFrame) -> bool:
    """Write the parquet for ``key`` only if its bytes differ from what's on
    disk. Returns True if the file changed. Skipping unchanged files keeps the
    builder from creating a needless commit (and redeploy) every hour."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = parquet_bytes(df)
    path = _parquet_path(key)
    if path.exists() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def write_manifest(built_at: datetime | None = None) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = (built_at or datetime.now(timezone.utc)).isoformat()
    MANIFEST_PATH.write_text(ts + "\n")


# ── App side ─────────────────────────────────────────────────────────────────
def read_snapshot(key: str, max_age_seconds: int = MAX_AGE_SECONDS):
    """Return the snapshotted DataFrame for ``key``, or ``None`` if it is
    missing, stale, or unreadable. Never raises — a snapshot problem must
    degrade to a live query, not break the page."""
    try:
        path = _parquet_path(key)
        if not path.exists() or not MANIFEST_PATH.exists():
            return None
        built = datetime.fromisoformat(MANIFEST_PATH.read_text().strip())
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - built).total_seconds()
        if age > max_age_seconds:
            logger.info("snapshot stale (built %s, %.0fs ago) — ignoring", built, age)
            return None
        return pd.read_parquet(path)
    except Exception as e:  # noqa: BLE001 - any failure must fall back to live
        logger.warning("snapshot read failed for %s: %s", key, e)
        return None
