"""Durable snapshot of dashboard query results, published to a data branch.

The dashboard's expensive work is the BigQuery pulls. Streamlit Community Cloud
discards in-memory ``@st.cache_data`` whenever the app process restarts (sleep,
redeploy, recycle), so a visitor who lands after a restart pays the full query
cost again — which is why the dashboard wasn't already loaded at 8am.

The service-account key the dashboard uses is **read-only**, so we cannot write
a snapshot back into BigQuery. Instead a scheduled GitHub Actions job
(``snapshot_build.py``) runs the same read-only queries and writes each result
as a parquet file under ``snapshot_data/``. The workflow then force-pushes that
directory as the *single commit* of the orphan ``snapshot-data`` branch —
NOT to the deploy branch. Committing ~3.5 MB of parquets to main every day
grew the repo's history without bound (~85 MB of pack data in the first month)
and every snapshot commit triggered a full Streamlit redeploy, restarting the
container and dumping its warm cache right before the morning visit.

The deployed app reads a snapshot file from the local ``snapshot_data/`` dir if
present (dev checkouts, transition), otherwise downloads it from the branch's
raw URL. Any miss/staleness/error degrades to a live query, so behaviour is
never worse than a direct pull.

This module is import-safe outside Streamlit (no ``streamlit`` import) so both
the app and the builder can share it.
"""
import hashlib
import io
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
# Registers the dbdate/dbtime extension types with pandas so pd.read_parquet
# can decode the BigQuery DATE/TIME columns the builder writes into the snapshot.
# Without this import, every snapshot read silently raises and falls back to a
# live query — making the snapshot worthless. The module is already a
# requirements.txt dep (used by google-cloud-bigquery's to_dataframe).
import db_dtypes  # noqa: F401

logger = logging.getLogger("funpop_dashboard.snapshot")

# Local snapshot dir: where the builder writes, and where a dev checkout (or a
# repo that still has the files committed) reads without any network.
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshot_data"
# Single timestamp for the whole snapshot; the freshness anchor (see below).
MANIFEST_PATH = SNAPSHOT_DIR / "built_at.txt"

# Raw-URL base of the snapshot-data branch the workflow force-pushes. The
# deployed app fetches snapshot files from here when they aren't on local disk.
# Override with the SNAPSHOT_REMOTE_URL env var if the repo moves.
SNAPSHOT_REMOTE_URL = os.environ.get(
    "SNAPSHOT_REMOTE_URL",
    "https://raw.githubusercontent.com/nathantj123/funpop-dashboard-streamlit/snapshot-data",
).rstrip("/")
# Only needed if the repo is made private (raw URLs 404 without auth then).
# The app can also set this from st.secrets at startup.
SNAPSHOT_REMOTE_TOKEN = os.environ.get("SNAPSHOT_REMOTE_TOKEN", "")
_REMOTE_TIMEOUT_SECONDS = 10

# Ignore a snapshot older than this. If the builder stops running we fall back
# to a live query (which the read-only key can still do) rather than serve stale
# data forever. 30h tolerates one fully-missed daily build.
MAX_AGE_SECONDS = 30 * 3600

# Memo for the remote manifest so the ~9 loaders on a cold start share one
# built_at.txt download instead of fetching it once each.
_MANIFEST_MEMO_TTL_SECONDS = 60.0
_manifest_memo = {"fetched_at": -_MANIFEST_MEMO_TTL_SECONDS, "built": None}


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
    # zstd cuts the committed files ~30-40% vs the default snappy at no
    # meaningful read cost — less to push, store, and download on cold start.
    df.to_parquet(buf, index=False, compression="zstd")
    return buf.getvalue()


# ── Builder side ─────────────────────────────────────────────────────────────
def write_if_changed(key: str, df: pd.DataFrame) -> bool:
    """Write the parquet for ``key`` only if its bytes differ from what's on
    disk. Returns True if the file changed. Skipping unchanged files keeps the
    builder from publishing (and the manifest from advancing) for no reason."""
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


def prune_snapshot(keep_keys) -> list[str]:
    """Delete committed snapshot parquets that are NOT part of the latest build.

    The snapshot key embeds the day's Standard-view lookback (``27 + days into
    the current Walmart week``), which cycles weekly — so a given key is only
    rewritten on the matching weekday. Left alone, prior builds' parquets pile
    up, and (much worse) the next time the app's rolling key lands back on one,
    ``read_snapshot`` happily serves a week-old file because the *global*
    ``built_at`` stamp still looks fresh. That is how the dashboard froze on an
    old business_date.

    Keeping only the current build's files means any key the latest build did
    not produce is simply absent, so ``read_snapshot`` returns ``None`` and the
    app falls back to a live query (fresh) instead of serving stale data.
    ``built_at.txt`` and any non-parquet files are left untouched. Returns the
    filenames removed.

    ``keep_keys`` is the set of snapshot keys (no extension) the build wrote or
    intended to write this run, including the static store-directory key."""
    keep = {f"{k}.parquet" for k in keep_keys}
    removed = []
    for path in SNAPSHOT_DIR.glob("*.parquet"):
        if path.name not in keep:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as e:  # noqa: BLE001 - a prune failure must not sink the build
                logger.warning("could not prune stale snapshot %s: %s", path.name, e)
    return removed


# ── App side ─────────────────────────────────────────────────────────────────
def fetch_remote(filename: str) -> bytes | None:
    """Best-effort download of one snapshot file from the snapshot-data branch.
    Returns None on any failure (404 before the first publish, network, auth)."""
    url = f"{SNAPSHOT_REMOTE_URL}/{filename}"
    req = urllib.request.Request(url)
    if SNAPSHOT_REMOTE_TOKEN:
        req.add_header("Authorization", f"token {SNAPSHOT_REMOTE_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=_REMOTE_TIMEOUT_SECONDS) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001 - remote is best-effort
        logger.info("remote snapshot fetch failed for %s: %s", filename, e)
        return None


def _parse_manifest(text: str) -> datetime:
    built = datetime.fromisoformat(text.strip())
    if built.tzinfo is None:
        built = built.replace(tzinfo=timezone.utc)
    return built


def built_at() -> datetime | None:
    """Build time of the current snapshot: the local manifest if present (dev /
    builder checkouts), else the snapshot-data branch's. None if unreachable."""
    if MANIFEST_PATH.exists():
        try:
            return _parse_manifest(MANIFEST_PATH.read_text())
        except Exception as e:  # noqa: BLE001
            logger.warning("could not parse local snapshot manifest: %s", e)
    now = time.monotonic()
    if now - _manifest_memo["fetched_at"] < _MANIFEST_MEMO_TTL_SECONDS:
        return _manifest_memo["built"]
    raw = fetch_remote(MANIFEST_PATH.name)
    built = None
    if raw is not None:
        try:
            built = _parse_manifest(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("could not parse remote snapshot manifest: %s", e)
    _manifest_memo["fetched_at"] = now
    _manifest_memo["built"] = built
    return built


def read_snapshot(key: str, max_age_seconds: int = MAX_AGE_SECONDS):
    """Return the snapshotted DataFrame for ``key``, or ``None`` if it is
    missing, stale, or unreadable. Never raises — a snapshot problem must
    degrade to a live query, not break the page."""
    try:
        built = built_at()
        if built is None:
            return None
        age = (datetime.now(timezone.utc) - built).total_seconds()
        if age > max_age_seconds:
            logger.info("snapshot stale (built %s, %.0fs ago) — ignoring", built, age)
            return None
        path = _parquet_path(key)
        if path.exists():
            return pd.read_parquet(path)
        data = fetch_remote(f"{key}.parquet")
        if data is None:
            return None
        return pd.read_parquet(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - any failure must fall back to live
        logger.warning("snapshot read failed for %s: %s", key, e)
        return None
