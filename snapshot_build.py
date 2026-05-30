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


def _committed_store_max_date(store_key: str):
    """Read max(business_date) from the already-committed store snapshot, if any.
    Used to skip rebuilds when intraday data has churned but the daily feed
    hasn't actually landed a new business_date — preventing redeploy storms."""
    path = snapshot.SNAPSHOT_DIR / f"{store_key}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        if "business_date" not in df.columns or df.empty:
            return None
        return pd.to_datetime(df["business_date"]).max()
    except Exception as e:  # noqa: BLE001
        print(f"  (could not read prior store snapshot for gating: {e})", file=sys.stderr)
        return None


def main() -> int:
    sa_info = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
    dataset = os.environ["BQ_DATASET"]
    project = sa_info["project_id"]
    creds = service_account.Credentials.from_service_account_info(sa_info)
    client = bigquery.Client(credentials=creds, project=project)

    lookback = lookback_for_today()
    jobs = query_jobs(lookback)
    print(f"Building snapshot for {project}.{dataset} (lookback={lookback})")

    # Gate the whole build on the store feed's max business_date actually
    # advancing. BigQuery's intraday rows churn (on-hand updates, etc.) so the
    # parquet bytes differ every hour even when the daily feed hasn't landed a
    # new day — which previously caused hourly commits and hourly app redeploys.
    store_filename, store_params = jobs[0]
    assert store_filename == "store_query.sql", "store_query.sql must be the freshness anchor"
    store_key = snapshot.snapshot_key(store_filename, store_params)
    prior_max = _committed_store_max_date(store_key)

    try:
        store_df = client.query(
            load_sql(store_filename, project, dataset),
            job_config=bigquery.QueryJobConfig(query_parameters=store_params),
        ).to_dataframe(create_bqstorage_client=False)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: store_query failed; leaving snapshot untouched: {e}", file=sys.stderr)
        return 1

    new_max = pd.to_datetime(store_df["business_date"]).max() if "business_date" in store_df.columns and not store_df.empty else None
    if prior_max is not None and pd.notna(prior_max) and pd.notna(new_max) and new_max <= prior_max:
        print(f"Store feed has not advanced (max={new_max.date()}, prior={prior_max.date()}); skipping build.")
        return 0

    print(f"Store feed advanced (max={new_max}, prior={prior_max}); rebuilding all queries.")

    # Write the store result first (it's already in hand), then run the rest.
    succeeded = 0
    changed = False
    try:
        if snapshot.write_if_changed(store_key, store_df):
            changed = True
        succeeded += 1
        print(f"  {store_filename}: {len(store_df):,} rows -> {store_key}")
    except Exception as e:  # noqa: BLE001
        print(f"  WARN: writing {store_filename} failed: {e}", file=sys.stderr)

    for filename, params in jobs[1:]:
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

    # Static store address directory for the Store Actions export. Addresses are
    # reference data, so we build it here (introspecting store_dim) and commit it
    # under a fixed key; the app reads that parquet straight from disk instead of
    # querying store_dim on every cold start. A failure here must not sink the
    # rest of the snapshot.
    try:
        import store_directory
        run_query = lambda sql: client.query(
            sql, job_config=bigquery.QueryJobConfig()
        ).to_dataframe(create_bqstorage_client=False)
        dir_df = store_directory.build_directory_df(run_query, project, dataset)
        if store_directory.DIRECTORY_FILENAME and not dir_df.empty:
            updated = snapshot.write_if_changed(store_directory.DIRECTORY_FILENAME, dir_df)
            changed = changed or updated
            print(f"  store_directory: {len(dir_df):,} rows -> {store_directory.DIRECTORY_FILENAME} "
                  f"({'updated' if updated else 'unchanged'})")
    except Exception as e:  # noqa: BLE001
        print(f"  WARN: store directory build failed: {e}", file=sys.stderr)

    if changed:
        snapshot.write_manifest()
        print("Snapshot data changed; manifest timestamp updated.")
    else:
        print("Store advanced but no per-file content changed; this should not normally happen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
