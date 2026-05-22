"""Discovery — temporarily replaces streamlit_app.py to enumerate accessible BQ datasets/tables."""

import json
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account

st.set_page_config(page_title="BQ Discovery", layout="wide")
st.title("BigQuery Dataset & Table Discovery")
st.caption("Using your service account to enumerate what's accessible.")

try:
    sa_info = json.loads(st.secrets["gcp_service_account_json"])
    credentials = service_account.Credentials.from_service_account_info(sa_info)
    client = bigquery.Client(credentials=credentials, project=sa_info["project_id"])
    st.success(f"Authenticated as **{sa_info['client_email']}** in project **{sa_info['project_id']}**")
except Exception as e:
    st.error(f"Auth failed: {type(e).__name__}: {e}")
    st.stop()

st.divider()
st.subheader(f"Datasets visible in `{sa_info['project_id']}`")

try:
    datasets = list(client.list_datasets())
except Exception as e:
    st.error(f"Could not list datasets at the project level: {e}")
    st.info(
        "Your service account is authenticated but lacks project-wide list permissions. "
        "Try entering a dataset name below — your Walmart onboarding contact would have given you this."
    )
    manual = st.text_input("Dataset name to inspect:")
    if manual:
        try:
            tables = list(client.list_tables(f"{sa_info['project_id']}.{manual}"))
            st.success(f"Found {len(tables)} tables in `{manual}`:")
            for t in tables:
                st.code(f"{t.dataset_id}.{t.table_id}", language="text")
        except Exception as inner:
            st.error(f"Could not list tables in `{manual}`: {inner}")
    st.stop()

if not datasets:
    st.warning(
        "Service account is authenticated but no datasets are visible. "
        "Contact your Walmart Data Ventures account team for dataset access."
    )
    st.stop()

st.write(f"Found **{len(datasets)}** dataset(s). Expand each to see its tables.")

for ds in datasets:
    with st.expander(f"📁 `{ds.dataset_id}`", expanded=(len(datasets) <= 3)):
        try:
            tables = list(client.list_tables(ds.reference))
            st.write(f"{len(tables)} tables:")
            for t in tables:
                st.code(f"{t.table_id}", language="text")
        except Exception as e:
            st.error(f"Could not list tables: {e}")

st.divider()
st.info(
    "📋 **What to do next:** Find the dataset that contains your sales and inventory tables "
    "(look for names matching: store_sales, store_inventory, dc_item_measures, or similar). "
    "Send me the dataset name + table names."
)
