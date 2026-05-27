"""Build the dashboard data snapshot.

Runs every dashboard query once (read-only) and writes the results to parquet
files under snapshot_data/, which the workflow then commits to the repo so the
live Streamlit app can serve cold loads instantly (see snapshot.py for why).
Invoked by .github/workflows/snapshot.yml on a schedule.

Environment:
  GCP_SERVICE_ACCOUNT_JSON  the service-account JSON (the same read-only key the
                            app uses — no write access to BigQuery is needed)
  BQ_DATASET                the dataset that holds the source tables, e.g. dv_supplier

The query/param list below MUST stay in sync with the loaders in
streamlit_app.py — the snapshot key is derived from (filename, param values),
so a mismatch just means the app silently falls back to a live query.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    print(f"Building snapshot for {project}.{dataset} (lookback={lookback})")

    succeeded = 0
    changed = False
    for filename, params in query_jobs(lookback):
        try:
            df = client.query(
                load_sql(filename, project, dataset),
                job_config=bigquery.QueryJobConfig(query_parameters=params),
            ).to_dataframe(create_bqstorage_client=False)
            key = snapshot.snapshot_key(filename, params)
            updated = snapshot.write_if_changed(key, df)
        except Exception as e:  # noqa: BLE001 - one bad query shouldn't sink the rest
            print(f"  WARN: {filename} failed: {e}", file=sys.stderr)
            continue
        succeeded += 1
        changed = changed or updated
        state = "updated" if updated else "unchanged"
        print(f"  {filename}: {len(df):,} rows -> {key} ({state})")

    if succeeded == 0:
        print("ERROR: every query failed; leaving the snapshot untouched", file=sys.stderr)
        return 1

    if changed:
        snapshot.write_manifest()
        print("Snapshot data changed; manifest timestamp updated.")
    else:
        print("No data changes; snapshot left as-is (no commit expected).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
