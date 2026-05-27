"""Build the dashboard data snapshot.

Runs every dashboard query once and stores the raw results in BigQuery so the
live Streamlit app can serve cold loads instantly (see snapshot.py for why).
Invoked by .github/workflows/snapshot.yml on a schedule.

Environment:
  GCP_SERVICE_ACCOUNT_JSON  full service-account JSON (same key the app uses);
                            the account needs BigQuery read + write (Data Editor)
  BQ_DATASET                the dataset that holds the source tables, e.g. dv_supplier

The query/param list below MUST stay in sync with the loaders in
streamlit_app.py — the snapshot key is derived from (filename, param values),
so a mismatch just means the app silently falls back to a live query.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

import snapshot
from constants import ACTIVE_ITEMS

SQL_DIR = Path(__file__).parent / "sql"
CENTRAL_TZ = ZoneInfo("America/Chicago")


def lookback_for_today() -> int:
    """Mirror streamlit_app.py's Standard-view lookback so the snapshot key
    matches what the app requests. Walmart fiscal week = Saturday-Friday."""
    today = datetime.now(CENTRAL_TZ).date()
    days_since_sat = (today.weekday() - 5) % 7
    days_into_current = days_since_sat + 1
    return 27 + days_into_current


def query_jobs(lookback: int):
    """(sql_filename, params) for every dashboard query. Keep in sync with the
    loaders in streamlit_app.py."""
    items = bigquery.ArrayQueryParameter("active_items", "INT64", list(ACTIVE_ITEMS))
    lb = bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback)
    return [
        ("store_query.sql", [items, lb]),
        ("dc_query.sql", [items, lb]),
        ("dc_alignment_query.sql", []),
        ("forecast_query.sql", [items, lb]),
        ("omni_query.sql", [items, lb]),
        ("ecom_inv_query.sql", [items, lb]),
        ("returns_query.sql", [items, lb]),
        ("modular_query.sql", [items]),
        ("backroom_query.sql", [items, lb]),
    ]


def load_sql(filename: str, project: str, dataset: str) -> str:
    text = (SQL_DIR / filename).read_text()
    return text.replace("{project}", project).replace("{dataset}", dataset)


def main() -> int:
    sa_info = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
    dataset = os.environ["BQ_DATASET"]
    project = sa_info["project_id"]
    creds = service_account.Credentials.from_service_account_info(sa_info)
    client = bigquery.Client(credentials=creds, project=project)

    lookback = lookback_for_today()
    built_at = datetime.now(timezone.utc)
    print(f"Building snapshot for {project}.{dataset} (lookback={lookback})")

    rows = []
    for filename, params in query_jobs(lookback):
        try:
            df = client.query(
                load_sql(filename, project, dataset),
                job_config=bigquery.QueryJobConfig(query_parameters=params),
            ).to_dataframe(create_bqstorage_client=False)
            # Serialize inside the try: if one query produces a frame parquet
            # can't handle, skip just that query — the app falls back to a live
            # pull for it while the rest of the snapshot still gets written.
            payload = snapshot.serialize(df)
        except Exception as e:  # noqa: BLE001 - one bad query shouldn't sink the rest
            print(f"  WARN: {filename} failed: {e}", file=sys.stderr)
            continue
        key = snapshot.snapshot_key(filename, params)
        rows.append({
            "snapshot_key": key,
            "built_at": built_at,
            "row_count": len(df),
            "payload": payload,
        })
        print(f"  {filename}: {len(df):,} rows -> {key}")

    if not rows:
        print("ERROR: every query failed; not overwriting the snapshot", file=sys.stderr)
        return 1

    snapshot.write_snapshots(client, project, dataset, rows)
    print(f"Wrote {len(rows)} snapshot rows to {project}.{dataset}.{snapshot.SNAPSHOT_TABLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
