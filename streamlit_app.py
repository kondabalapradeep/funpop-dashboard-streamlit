"""
streamlit_app.py — FunPop Sales Dashboard, Streamlit edition.

Architecture
------------
* Authenticates to BigQuery using the service-account key stored in st.secrets.
* Queries the JOINed store data and the DC data — same SQL we wrote before.
* Caches both DataFrames for 10 minutes; the "Refresh data" sidebar button
  clears the cache and forces a fresh pull.
* Reuses prepare_data / build_core / build_dc_analysis from funpop_core.py.
* Renders the result with native Streamlit components — st.metric for KPIs,
  st.dataframe for tables, st.line_chart for trends.

Secrets file required (.streamlit/secrets.toml) — see secrets.toml.example.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account

import funpop_core as core


# ─── Page setup ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FunPop Sales Dashboard",
    page_icon="🍿",
    layout="wide",
    initial_sidebar_state="expanded",
)

SQL_DIR = Path(__file__).parent / "sql"


# ─── Auth + BigQuery client (cached across the session) ──────────────────────
@st.cache_resource
def _get_sa_info():
    """Parse the service account JSON blob from secrets."""
    import json
    return json.loads(st.secrets["gcp_service_account_json"])


@st.cache_resource
def get_bq_client():
    """Build a BigQuery client from the service account key in st.secrets."""
    sa_info = _get_sa_info()
    credentials = service_account.Credentials.from_service_account_info(sa_info)
    return bigquery.Client(credentials=credentials, project=sa_info["project_id"])


def _load_sql(filename: str) -> str:
    """Read a SQL file and substitute {project} / {dataset} from secrets."""
    text = (SQL_DIR / filename).read_text()
    project = _get_sa_info()["project_id"]
    dataset = st.secrets["bigquery"]["dataset"]
    return text.replace("{project}", project).replace("{dataset}", dataset)


# ─── Cached data loaders ─────────────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Loading store data from BigQuery...")
def load_store_data(lookback_days: int = 120) -> pd.DataFrame:
    """Pull joined daily store data. Cached for 10 min."""
    client = get_bq_client()
    sql = _load_sql("store_query.sql")
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
    ])
    df = client.query(sql, job_config=job_config).to_dataframe()

    if df.empty:
        return df

    df["business_date"] = pd.to_datetime(df["business_date"])
    numeric_cols = [
        "pos_quantity_this_year", "pos_quantity_last_year",
        "store_on_hand_quantity_this_year", "store_on_hand_quantity_last_year",
        "store_in_warehouse_quantity_this_year", "store_in_transit_quantity_this_year",
        "store_specific_retail_amount_this_year",
        "pos_sales_this_year", "pos_sales_last_year",
    ]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Mirror the unit-price correction the original load() did, in case the
    # retail column is delivered as an aggregated $ sum rather than unit price.
    sold = df[df["pos_quantity_this_year"] > 0]
    if len(sold) and sold["store_specific_retail_amount_this_year"].median() > 10:
        unit_price = np.where(
            df["pos_quantity_this_year"] > 0,
            (df["pos_sales_this_year"] /
             df["pos_quantity_this_year"].replace(0, np.nan)).round(2),
            0,
        )
        df["store_specific_retail_amount_this_year"] = np.where(
            np.isnan(unit_price), 0, unit_price,
        )

    return df


@st.cache_data(ttl=600, show_spinner="Loading DC data from BigQuery...")
def load_dc_data(lookback_days: int = 120) -> pd.DataFrame:
    """Pull DC data. Returns empty DataFrame if the query fails — DC is optional."""
    try:
        client = get_bq_client()
        sql = _load_sql("dc_query.sql")
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        df = client.query(sql, job_config=job_config).to_dataframe()
        if df.empty:
            return df
        df["inventory_date"] = pd.to_datetime(df["inventory_date"])
        for c in [
            "on_hand_warehouse_inventory_in_units_this_year",
            "on_hand_warehouse_inventory_in_units_last_year",
            "on_order_warehouse_quantity_in_units_this_year",
            "on_order_warehouse_quantity_in_units_last_year",
            "out_of_stock_each_quantity_this_year",
            "out_of_stock_each_quantity_last_year",
        ]:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df
    except Exception as e:
        st.warning(f"DC query failed; dashboard will run without DC analysis. ({e})")
        return pd.DataFrame()


# ─── Compute the dashboard data for the current filter selection ─────────────
def compute_dashboard(df: pd.DataFrame, dc_df: pd.DataFrame, item_filter: list) -> dict:
    """Filter the raw data by selected items and run the original build_core /
    build_dc_analysis. Returns the same dict the HTML version consumed."""
    df_filt = df if not item_filter else df[df["walmart_item_number"].isin(item_filter)]
    if df_filt.empty:
        return {}
    weeks = sorted(df_filt["walmart_calendar_week"].unique())
    data = core.build_core(df_filt, weeks)
    if not data:
        return {}
    if dc_df is not None and not dc_df.empty:
        dc_filt = dc_df if not item_filter else dc_df[dc_df["walmart_item_number"].isin(item_filter)]
        if not dc_filt.empty:
            data["dc"] = core.build_dc_analysis(dc_filt, df_filt)
    return data


# ─── Sidebar — filters and refresh ───────────────────────────────────────────
with st.sidebar:
    st.header("Controls")

    if st.button("🔄 Refresh data", help="Clear cache and re-pull from BigQuery"):
        st.cache_data.clear()
        st.rerun()

    st.caption("Data refreshes from BigQuery every 10 minutes automatically.")

    st.divider()
    st.subheader("Filters")

    # Item filter — multiselect with friendly labels from BIN_ITEMS / non-bin items
    item_options = list(core.ACTIVE_ITEMS)
    item_labels = {
        i: f"{i} ({'Bin' if i in core.BIN_ITEMS else 'Shelf'})"
        for i in item_options
    }
    item_filter = st.multiselect(
        "Items",
        options=item_options,
        default=item_options,
        format_func=lambda i: item_labels.get(i, str(i)),
    )

    lookback = st.slider("Lookback (days)", 30, 365, 120, step=30)


# ─── Load ────────────────────────────────────────────────────────────────────
df = load_store_data(lookback_days=lookback)
dc_df = load_dc_data(lookback_days=lookback)

if df.empty:
    st.error(
        "No store data returned for the selected lookback window. "
        "Check that the BQ tables contain rows for items "
        f"{list(core.ACTIVE_ITEMS)} within the last {lookback} days."
    )
    st.stop()

data = compute_dashboard(df, dc_df, item_filter)
if not data:
    st.warning("No data after applying current filters.")
    st.stop()


# ─── Header ──────────────────────────────────────────────────────────────────
left, right = st.columns([3, 1])
with left:
    st.title("FunPop Sales Dashboard")
    st.caption(f"{data['kpi']['date_range']} · {df['business_date'].max().strftime('%b %d, %Y')} most recent")
with right:
    yoy_pct = data['kpi']['yoy_pct']
    delta_color = "normal" if yoy_pct >= 0 else "inverse"
    st.metric("Period YOY", f"{yoy_pct:+.1f}%", delta=f"{data['kpi']['yoy']:+,} units", delta_color=delta_color)


# ─── KPI bar ─────────────────────────────────────────────────────────────────
st.subheader("Period KPIs")
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("POS Units TY", f"{data['kpi']['ty']:,}")
k2.metric("POS Units LY", f"{data['kpi']['ly']:,}")
k3.metric("YOY Units", f"{data['kpi']['yoy']:+,}")
k4.metric("Last 7d TY", f"{data['totals7d']['ty']:,}", delta=f"{data['totals7d']['pct']:+.1f}% vs LY")
k5.metric("Last 7d YOY", f"{data['totals7d']['yoy']:+,}")


# ─── 7-day tracker ───────────────────────────────────────────────────────────
st.subheader("Last 7 Days")
tracker_df = pd.DataFrame(data["tracker7d"])
if not tracker_df.empty:
    tracker_df = tracker_df[["date", "day", "ty", "ly", "yoy", "yoy_pct"]]
    tracker_df.columns = ["Date", "Day", "Units TY", "Units LY", "YOY", "YOY %"]
    st.dataframe(tracker_df, use_container_width=True, hide_index=True)


# ─── 4-week trend chart ──────────────────────────────────────────────────────
st.subheader("4-Week Daily Trend")
chart_df = pd.DataFrame(data["chart4w"])
if not chart_df.empty:
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    chart_df = chart_df.set_index("date")[["ty", "ly"]]
    chart_df.columns = ["This Year", "Last Year"]
    st.line_chart(chart_df, use_container_width=True)


# ─── Inventory snapshot ──────────────────────────────────────────────────────
st.subheader(f"Store Inventory — {data.get('inv_date', 'latest')}")
inv_df = pd.DataFrame(data["inventory"])
if not inv_df.empty:
    inv_show = inv_df[[
        "item", "stores", "on_hand_ty", "on_hand_ly",
        "warehouse", "in_transit", "total_supply", "wos",
    ]].copy()
    inv_show.columns = [
        "Item", "Stores", "On Hand TY", "On Hand LY",
        "In Warehouse", "In Transit", "Total Supply", "Weeks of Supply",
    ]
    st.dataframe(inv_show, use_container_width=True, hide_index=True)


# ─── Store rankings ──────────────────────────────────────────────────────────
st.subheader("Store Rankings")
rank_df = pd.DataFrame(data["rankings"])
if not rank_df.empty:
    rank_show = rank_df[[
        "rank_ty", "store", "name", "city", "state",
        "ty", "ly", "yoy", "yoy_pct", "sales_ty",
        "on_hand", "cur_price", "status",
    ]].copy()
    rank_show.columns = [
        "Rank", "Store #", "Name", "City", "State",
        "Units TY", "Units LY", "YOY", "YOY %", "Sales TY ($)",
        "On Hand", "Current Price", "Trend",
    ]
    st.dataframe(
        rank_show,
        use_container_width=True,
        hide_index=True,
        height=min(600, 40 + 35 * len(rank_show)),
        column_config={
            "Sales TY ($)": st.column_config.NumberColumn(format="$%.2f"),
            "Current Price": st.column_config.NumberColumn(format="$%.2f"),
            "YOY %": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )


# ─── DC analysis (only if we got DC data) ────────────────────────────────────
if "dc" in data and data["dc"]:
    dc = data["dc"]
    st.divider()
    st.header("Distribution Center Analysis")

    # DC KPI strip
    if "kpis" in dc:
        cols = st.columns(len(dc["kpis"]))
        for col, kpi in zip(cols, dc["kpis"]):
            col.metric(kpi.get("label", ""), kpi.get("value", ""), delta=kpi.get("delta"))

    # Critical / overstock tables
    for section_key, label in [("critical", "DCs Critically Low (≤7 days supply)"),
                                ("overstock", "DCs Overstocked (>30 days supply)")]:
        rows = dc.get(section_key, [])
        if rows:
            st.markdown(f"**{label}**")
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Full DC table
    if dc.get("full_table"):
        with st.expander("Full DC table"):
            st.dataframe(pd.DataFrame(dc["full_table"]), use_container_width=True, hide_index=True)


# ─── Footer ──────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"BigQuery rows: {len(df):,} store · {len(dc_df):,} DC  ·  "
    f"Cache TTL: 10 min  ·  Use sidebar to refresh manually."
)
