"""Durable BigQuery-backed snapshot of dashboard query results.

The dashboard's expensive work is the BigQuery pulls. Streamlit Community Cloud
discards in-memory ``@st.cache_data`` whenever the app process restarts (sleep,
redeploy, recycle), so a visitor who lands after a restart pays the full query
cost again — which is why the dashboard wasn't already loaded at 8am.

To make cold loads fast, a scheduled GitHub Actions job (``snapshot_build.py``)
runs the same queries and stores each raw result in a single BigQuery table.
The live app reads from that table on a cache miss instead of re-querying the
source tables. Any miss/staleness/error degrades to a live query, so behaviour
is never worse than a direct pull.

This module is import-safe outside Streamlit (no ``streamlit`` import), so both
the app and the builder can share key derivation and (de)serialization.
"""
import base64
import hashlib
import io
import logging

import pandas as pd
from google.cloud import bigquery

logger = logging.getLogger("funpop_dashboard.snapshot")

# Name of the table (in the same dataset as the source data) that holds the
# serialized query results, one row per (query, params) pair.
SNAPSHOT_TABLE = "_dashboard_snapshot"

# Ignore snapshots older than this. If the builder stops running we must fall
# back to a live query rather than serve stale data forever. 30h tolerates one
# fully-missed daily build cycle.
MAX_AGE_SECONDS = 30 * 3600


def snapshot_key(sql_filename: str, params=None) -> str:
    """Stable key for a (query, params) pair.

    Must match between the builder and the app, so it depends only on the SQL
    filename and the parameter *values* — never object identity or the fully
    substituted SQL text (which embeds the project/dataset and so differs by
    environment)."""
    parts = [sql_filename]
    for p in params or []:
        if hasattr(p, "values"):          # ArrayQueryParameter
            parts.append(f"{p.name}={list(p.values)}")
        else:                              # ScalarQueryParameter
            parts.append(f"{p.name}={p.value}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def serialize(df: pd.DataFrame) -> str:
    """DataFrame -> base64 string of parquet bytes.

    base64 text (not raw BYTES) is used so the value round-trips cleanly through
    ``load_table_from_dataframe`` and ordinary query reads with no dependence on
    BYTES-column handling."""
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def deserialize(payload: str) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(base64.b64decode(payload)))


def _table_id(project: str, dataset: str) -> str:
    return f"{project}.{dataset}.{SNAPSHOT_TABLE}"


def read_snapshot(client, project, dataset, key, max_age_seconds=MAX_AGE_SECONDS):
    """Return the snapshotted DataFrame for ``key``, or ``None`` if it is
    missing, stale, or unreadable.

    Never raises: a snapshot problem must degrade to a live query, not break
    the page."""
    try:
        sql = f"""
            SELECT payload, UNIX_SECONDS(built_at) AS built_unix
            FROM `{_table_id(project, dataset)}`
            WHERE snapshot_key = @key
            LIMIT 1
        """
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("key", "STRING", key)]
            ),
        )
        rows = list(job.result())
        if not rows:
            return None
        import time
        age = time.time() - rows[0]["built_unix"]
        if age > max_age_seconds:
            logger.info("snapshot %s is %.0fs old (> %ss) — ignoring", key, age, max_age_seconds)
            return None
        return deserialize(rows[0]["payload"])
    except Exception as e:  # noqa: BLE001 - any failure must fall back to live
        logger.warning("snapshot read failed for %s: %s", key, e)
        return None


def _schema():
    return [
        bigquery.SchemaField("snapshot_key", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("built_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("row_count", "INTEGER"),
        bigquery.SchemaField("payload", "STRING", mode="REQUIRED"),
    ]


def write_snapshots(client, project, dataset, rows) -> None:
    """Overwrite the snapshot table with ``rows``.

    ``rows`` is a list of dicts with keys snapshot_key, built_at, row_count,
    payload. WRITE_TRUNCATE keeps the table to exactly the current set of
    queries and makes each build atomic (one load job)."""
    df = pd.DataFrame(rows, columns=["snapshot_key", "built_at", "row_count", "payload"])
    job = client.load_table_from_dataframe(
        df,
        _table_id(project, dataset),
        job_config=bigquery.LoadJobConfig(
            schema=_schema(),
            write_disposition="WRITE_TRUNCATE",
        ),
    )
    job.result()
