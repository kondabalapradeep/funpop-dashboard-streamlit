"""
streamlit_app.py — FunPop Sales Dashboard

Tab structure:
  Overview          — KPIs, last 10 days, item performance, stockout risk
  Sales & Velocity  — Weekly sales, U/S/W, forecast attainment
  Inventory & DC    — Weekly inventory, phantom inventory, DC pipeline (true alignment)
  Channels          — Omni sales, eComm inventory, store returns
  Distribution      — Modular coverage, state performance

Data sources (BigQuery, dv_supplier dataset):
  store_sales + store_invt + store_dim + item_dim ─→ load_store_data
  dc_item + dc_dim                                 ─→ load_dc_data
  dc_alignment                                     ─→ load_dc_alignment
  daily_demand_forecast                            ─→ load_forecast_data
  omni_sales                                       ─→ load_omni_data
  ecom_invt + omni_item_dimensions                 ─→ load_ecom_inv_data
  store_returns                                    ─→ load_returns_data
  modular_plan_upc                                 ─→ load_modular_data
  backroom_adjusted_inventory                      ─→ load_backroom_data

Every secondary loader is fault-tolerant: a schema mismatch or auth issue
on one source produces a section-local warning rather than a page crash.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
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

st.markdown(
    """
    <style>
    @media (max-width: 900px) {
        div[data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
            gap: 0.75rem;
        }
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            flex: 1 1 100% !important;
            min-width: 100% !important;
            width: 100% !important;
        }
        .block-container {
            padding-top: 1rem;
            padding-left: 0.75rem;
            padding-right: 0.75rem;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.5rem;
        }
        div[data-testid="stDataFrame"] {
            overflow-x: auto;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

SQL_DIR = Path(__file__).parent / "sql"

ITEM_LABELS = {
    658442130: "Half Bin",
    658442128: "Full Bin",
    666209064: "Shelf",
}

# DC inventory ships in case packs, not eaches. Multiplier converts to units.
CASE_PACK_UNITS = {
    658442128: 208,  # Full Bin
    658442130: 126,  # Half Bin
    666209064: 6,    # Shelf
}


# ─── Auth + BigQuery client ──────────────────────────────────────────────────
@st.cache_resource
def _get_sa_info():
    return json.loads(st.secrets["gcp_service_account_json"])


@st.cache_resource
def get_bq_client():
    sa_info = _get_sa_info()
    creds = service_account.Credentials.from_service_account_info(sa_info)
    return bigquery.Client(credentials=creds, project=sa_info["project_id"])


def _load_sql(filename: str) -> str:
    text = (SQL_DIR / filename).read_text()
    project = _get_sa_info()["project_id"]
    dataset = st.secrets["bigquery"]["dataset"]
    return text.replace("{project}", project).replace("{dataset}", dataset)


def _run_query(sql, params=None):
    client = get_bq_client()
    job_config = bigquery.QueryJobConfig(query_parameters=params or [])
    return client.query(sql, job_config=job_config).to_dataframe(create_bqstorage_client=False)


# ─── Primary data loaders ────────────────────────────────────────────────────
@st.cache_data(ttl=86400, show_spinner="Loading store data...")
def load_store_data(lookback_days: int) -> pd.DataFrame:
    df = _run_query(_load_sql("store_query.sql"), [
        bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
    ])
    if df.empty:
        return df
    df["business_date"] = pd.to_datetime(df["business_date"])
    for c in [
        "pos_quantity_this_year", "pos_quantity_last_year",
        "store_on_hand_quantity_this_year", "store_on_hand_quantity_last_year",
        "store_in_warehouse_quantity_this_year", "store_in_transit_quantity_this_year",
        "store_specific_retail_amount_this_year",
        "pos_sales_this_year", "pos_sales_last_year",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    # Unit-price correction
    sold = df[df["pos_quantity_this_year"] > 0]
    if len(sold) and sold["store_specific_retail_amount_this_year"].median() > 10:
        unit_price = np.where(
            df["pos_quantity_this_year"] > 0,
            (df["pos_sales_this_year"] / df["pos_quantity_this_year"].replace(0, np.nan)).round(2),
            0,
        )
        df["store_specific_retail_amount_this_year"] = np.where(np.isnan(unit_price), 0, unit_price)
    return df


@st.cache_data(ttl=86400, show_spinner="Loading DC data...")
def load_dc_data(lookback_days: int) -> pd.DataFrame:
    try:
        df = _run_query(_load_sql("dc_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
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
        # Convert case packs → eaches for on_hand and on_order (OOS is already eaches)
        multiplier = df["walmart_item_number"].map(CASE_PACK_UNITS).fillna(1)
        for c in [
            "on_hand_warehouse_inventory_in_units_this_year",
            "on_hand_warehouse_inventory_in_units_last_year",
            "on_order_warehouse_quantity_in_units_this_year",
            "on_order_warehouse_quantity_in_units_last_year",
        ]:
            df[c] = (df[c] * multiplier).astype("int64")
        return df
    except Exception as e:
        st.warning(f"DC query failed: {e}")
        return pd.DataFrame()


# ─── Secondary loaders (fault-tolerant) ──────────────────────────────────────
@st.cache_data(ttl=86400, show_spinner=False)
def load_dc_alignment() -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("dc_alignment_query.sql"))
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_forecast_data(lookback_days: int) -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("forecast_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["forecast_date"] = pd.to_datetime(df["forecast_date"])
            df["forecast_quantity"] = pd.to_numeric(df["forecast_quantity"], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_omni_data(lookback_days: int) -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("omni_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["business_date"] = pd.to_datetime(df["business_date"])
            for c in ["units_ty", "units_ly", "sales_ty", "sales_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_ecom_inv_data(lookback_days: int) -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("ecom_inv_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["report_date"] = pd.to_datetime(df["report_date"])
            for c in ["on_hand_units", "available_to_sell", "on_hand_units_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_returns_data(lookback_days: int) -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("returns_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["return_date"] = pd.to_datetime(df["return_date"])
            for c in ["returns_ty", "returns_ly", "return_sales_ty", "return_sales_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_modular_data() -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("modular_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
        ])
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_backroom_data(lookback_days: int) -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("backroom_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["adjustment_date"] = pd.to_datetime(df["adjustment_date"])
            for c in ["adjustment_qty_ty", "adjustment_qty_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Controls")
    if st.button("🔄 Refresh data", help="Clear cache, re-pull from BigQuery"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Data is cached for 24 hours. Click for a manual refresh.")

    st.divider()
    st.subheader("Filters")

    BIN_ITEMS_SET = [658442128, 658442130]
    SHELF_ITEMS_SET = [666209064]
    item_view = st.radio(
        "View",
        options=["Total (all items)", "Both Bins (full + half)", "Shelf only"],
        index=0,
    )
    if item_view == "Total (all items)":
        item_filter = list(core.ACTIVE_ITEMS)
    elif item_view == "Both Bins (full + half)":
        item_filter = BIN_ITEMS_SET
    else:
        item_filter = SHELF_ITEMS_SET

    lookback = st.slider(
        "Lookback (days)",
        min_value=14, max_value=120, value=30, step=7,
        help="Larger lookback = slower load.",
    )

    st.divider()
    perf_window = st.radio(
        "Performance window",
        options=["Most recent day", "Last 7 days", "Full lookback"],
        index=2,
        help="Filters KPIs and Item Performance only.",
    )


# ─── Primary data load ───────────────────────────────────────────────────────
df_all = load_store_data(lookback_days=lookback)
dc_df_all = load_dc_data(lookback_days=lookback)

if df_all.empty:
    st.error(
        f"No store data for the last {lookback} days. "
        f"Items {list(core.ACTIVE_ITEMS)} may have no sales in this window."
    )
    st.stop()

# Apply item filter
df = df_all[df_all["walmart_item_number"].isin(item_filter)].copy()
dc_df = dc_df_all[dc_df_all["walmart_item_number"].isin(item_filter)].copy() if not dc_df_all.empty else dc_df_all

if df.empty:
    st.warning("No data matches the current item filter.")
    st.stop()

most_recent = df["business_date"].max()
weeks_in_period = max(1, lookback / 7)

# Compute the performance window slice
if perf_window == "Most recent day":
    df_window = df[df["business_date"] == most_recent].copy()
    window_label = f"Day of {most_recent.strftime('%b %d, %Y')}"
    weeks_in_window = 1 / 7
elif perf_window == "Last 7 days":
    cutoff_7d = most_recent - timedelta(days=6)
    df_window = df[df["business_date"] >= cutoff_7d].copy()
    window_label = f"Last 7 days ({cutoff_7d.strftime('%b %d')}–{most_recent.strftime('%b %d')})"
    weeks_in_window = 1.0
else:
    df_window = df
    window_label = f"Full lookback ({df['business_date'].min().strftime('%b %d')}–{most_recent.strftime('%b %d')}, {lookback}d)"
    weeks_in_window = weeks_in_period


# ─── Header ──────────────────────────────────────────────────────────────────
st.title("FunPop Sales Dashboard")
st.caption(
    f"**Data range:** {df['business_date'].min().strftime('%b %d, %Y')} – "
    f"{most_recent.strftime('%b %d, %Y')} ({lookback}d) · "
    f"{df['store_number'].nunique():,} stores · {item_view}"
)


# ─── Tabs ────────────────────────────────────────────────────────────────────
tab_overview, tab_sales, tab_inv, tab_channels, tab_dist = st.tabs([
    "Overview",
    "Sales & Velocity",
    "Inventory & DC",
    "Channels",
    "Distribution",
])


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 1 — OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════
with tab_overview:
    # ── Headline KPIs ────────────────────────────────────────────────────────
    st.subheader(f"Period at a glance — {window_label}")

    units_ty = int(df_window["pos_quantity_this_year"].sum())
    units_ly = int(df_window["pos_quantity_last_year"].sum())
    units_yoy = units_ty - units_ly
    units_yoy_pct = (units_yoy / units_ly * 100) if units_ly else 0
    sales_ty = float(df_window["pos_sales_this_year"].sum())
    sales_ly = float(df_window["pos_sales_last_year"].sum())
    sales_yoy_pct = ((sales_ty - sales_ly) / sales_ly * 100) if sales_ly else 0
    stores_selling = df_window[df_window["pos_quantity_this_year"] > 0]["store_number"].nunique()
    stores_total = df_window["store_number"].nunique()
    weekly_velocity = units_ty / stores_selling / weeks_in_window if stores_selling else 0

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Units sold", f"{units_ty:,}", f"{units_yoy_pct:+.1f}% YoY")
    k2.metric("Sales", f"${sales_ty:,.0f}", f"{sales_yoy_pct:+.1f}% YoY")
    k3.metric("YoY units", f"{units_yoy:+,}")
    k4.metric("Stores w/ sales", f"{stores_selling:,}/{stores_total:,}",
              f"{(stores_selling/stores_total*100 if stores_total else 0):.0f}%")
    k5.metric("Avg units/store/wk", f"{weekly_velocity:.1f}")

    # ── Last 10 days daily detail ────────────────────────────────────────────
    st.subheader("Last 10 days — daily detail")

    cutoff_10d = most_recent - timedelta(days=9)
    last10 = df[df["business_date"] >= cutoff_10d].copy()

    daily = last10.groupby("business_date", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
    ).sort_values("business_date")
    stores_per_day = (
        last10[last10["pos_quantity_this_year"] > 0]
        .groupby("business_date")["store_number"].nunique()
        .reset_index().rename(columns={"store_number": "stores_selling"})
    )
    daily = daily.merge(stores_per_day, on="business_date", how="left").fillna({"stores_selling": 0})
    daily["yoy_units"] = daily["units_ty"] - daily["units_ly"]
    daily["yoy_pct"] = np.where(daily["units_ly"] > 0,
                                 (daily["yoy_units"] / daily["units_ly"] * 100).round(1), 0)
    daily["weekday"] = daily["business_date"].dt.strftime("%a")
    daily["date_str"] = daily["business_date"].dt.strftime("%b %d")

    c_l, c_r = st.columns([3, 2])
    with c_l:
        daily_m = daily.melt(id_vars=["date_str", "weekday", "business_date"],
                             value_vars=["units_ty", "units_ly"],
                             var_name="Period", value_name="Units")
        daily_m["Period"] = daily_m["Period"].map({"units_ty": "This Year", "units_ly": "Last Year"})
        chart = (alt.Chart(daily_m).mark_line(point=True, strokeWidth=2.5).encode(
            x=alt.X("date_str:N", sort=list(daily["date_str"]), title="Date",
                    axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("Units:Q", title="Units"),
            color=alt.Color("Period:N",
                            scale=alt.Scale(domain=["This Year", "Last Year"],
                                            range=["#185FA5", "#A0A09A"])),
            tooltip=["date_str", "weekday", "Period", alt.Tooltip("Units:Q", format=",")],
        ).properties(height=300))
        st.altair_chart(chart, use_container_width=True)
    with c_r:
        show = daily[["weekday", "date_str", "units_ty", "yoy_pct", "stores_selling"]].iloc[::-1].copy()
        show["stores_selling"] = show["stores_selling"].astype(int)
        show.columns = ["Day", "Date", "Units TY", "YoY %", "Stores Selling"]
        st.dataframe(show, use_container_width=True, hide_index=True, height=380,
                     column_config={"YoY %": st.column_config.NumberColumn(format="%.1f%%")})

    # ── Item performance ─────────────────────────────────────────────────────
    st.subheader(f"Item performance — {window_label}")

    latest_per_item = df.groupby("walmart_item_number")["business_date"].max().to_dict()
    item_oh = {}
    for item, item_max in latest_per_item.items():
        snap = df[(df["walmart_item_number"] == item) & (df["business_date"] == item_max)]
        item_oh[item] = int(snap["store_on_hand_quantity_this_year"].sum())

    item_perf = df_window.groupby("walmart_item_number", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        sales_ly=("pos_sales_last_year", "sum"),
    )
    item_perf["sales_yoy_pct"] = np.where(item_perf["sales_ly"] > 0,
        ((item_perf["sales_ty"] - item_perf["sales_ly"]) / item_perf["sales_ly"] * 100).round(1), 0)
    item_perf["on_hand"] = item_perf["walmart_item_number"].map(item_oh).fillna(0).astype(int)
    item_perf["item"] = item_perf["walmart_item_number"].map(ITEM_LABELS).fillna(item_perf["walmart_item_number"].astype(str))
    item_perf["yoy_pct"] = np.where(item_perf["units_ly"] > 0,
        ((item_perf["units_ty"] - item_perf["units_ly"]) / item_perf["units_ly"] * 100).round(1), 0)
    full_units_per_item = df.groupby("walmart_item_number")["pos_quantity_this_year"].sum()
    item_perf["wos_units_ty"] = item_perf["walmart_item_number"].map(full_units_per_item).fillna(0)
    item_perf["wos"] = np.where(item_perf["wos_units_ty"] > 0,
        (item_perf["on_hand"] / (item_perf["wos_units_ty"] / weeks_in_period)).round(1), np.inf)

    if len(item_perf) > 0:
        ip_cols = st.columns(len(item_perf))
        for col, (_, row) in zip(ip_cols, item_perf.iterrows()):
            with col:
                st.markdown(f"### {row['item']}")
                st.caption(f"Item {int(row['walmart_item_number'])}")
                st.metric("Units sold", f"{int(row['units_ty']):,}", f"{row['yoy_pct']:+.1f}% YoY")
                st.metric("Sales", f"${row['sales_ty']:,.0f}", f"{row['sales_yoy_pct']:+.1f}% YoY")
                st.metric("On hand (latest)", f"{int(row['on_hand']):,}")
                wos_label = "∞" if not np.isfinite(row['wos']) else f"{row['wos']:.1f} wks"
                st.metric("Weeks of supply", wos_label)

    # ── Stockout risk ────────────────────────────────────────────────────────
    st.subheader("Stockout risk")

    store_day_oh = df.groupby(["business_date", "store_number"], as_index=False)[
        "store_on_hand_quantity_this_year"
    ].sum()
    store_day_oh["is_oos"] = (store_day_oh["store_on_hand_quantity_this_year"] == 0).astype(int)
    oos_daily = store_day_oh.groupby("business_date", as_index=False).agg(
        oos_stores=("is_oos", "sum"),
        total_stores=("store_number", "nunique"),
    ).sort_values("business_date").reset_index(drop=True)
    oos_daily["oos_pct"] = (oos_daily["oos_stores"] / oos_daily["total_stores"] * 100).round(1)

    sr_l, sr_r = st.columns([2, 3])
    with sr_l:
        if len(oos_daily):
            latest_oos = oos_daily.iloc[-1]
            week_ago = oos_daily.iloc[-8] if len(oos_daily) >= 8 else oos_daily.iloc[0]
            oos_delta = int(latest_oos["oos_stores"] - week_ago["oos_stores"])
            st.metric("Stores OOS today", f"{int(latest_oos['oos_stores']):,}",
                      delta=f"{oos_delta:+,} vs week ago", delta_color="inverse")
            st.metric("OOS rate today", f"{latest_oos['oos_pct']:.1f}%")
        days_oos = (
            df.groupby(["business_date", "store_number"])["store_on_hand_quantity_this_year"]
            .sum().reset_index()
        )
        chronic = int((days_oos[days_oos["store_on_hand_quantity_this_year"] == 0]
                       .groupby("store_number").size() >= 7).sum())
        st.metric("Chronically OOS (≥7 days)", f"{chronic:,} stores")
    with sr_r:
        if not oos_daily.empty:
            chart = (alt.Chart(oos_daily).mark_area(opacity=0.3, color="#791F1F").encode(
                x=alt.X("business_date:T", title="Date"),
                y=alt.Y("oos_stores:Q", title="Stores with on-hand = 0"),
                tooltip=[alt.Tooltip("business_date:T", title="Date"),
                         alt.Tooltip("oos_stores:Q", format=",", title="OOS stores")],
            ).properties(height=280)) + (
                alt.Chart(oos_daily).mark_line(color="#791F1F", strokeWidth=2)
                .encode(x="business_date:T", y="oos_stores:Q")
            )
            st.altair_chart(chart, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 2 — SALES & VELOCITY
# ═══════════════════════════════════════════════════════════════════════════
with tab_sales:
    # ── Weekly sales trend ───────────────────────────────────────────────────
    st.subheader("Weekly sales trend")

    weekly_sales = df.groupby("walmart_calendar_week", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        sales_ly=("pos_sales_last_year", "sum"),
        week_start=("business_date", "min"),
    ).sort_values("walmart_calendar_week").reset_index(drop=True)
    weekly_sales["yoy_units"] = weekly_sales["units_ty"] - weekly_sales["units_ly"]
    weekly_sales["yoy_pct"] = np.where(weekly_sales["units_ly"] > 0,
        (weekly_sales["yoy_units"] / weekly_sales["units_ly"] * 100).round(1), 0)
    weekly_sales["week_label"] = "WM Wk " + weekly_sales["walmart_calendar_week"].astype(str).str[-2:]

    if not weekly_sales.empty:
        m = weekly_sales.melt(id_vars=["week_label", "walmart_calendar_week"],
                              value_vars=["units_ty", "units_ly"],
                              var_name="Period", value_name="Units")
        m["Period"] = m["Period"].map({"units_ty": "This Year", "units_ly": "Last Year"})
        st.altair_chart((alt.Chart(m).mark_bar().encode(
            x=alt.X("week_label:N", sort=list(weekly_sales["week_label"]), title="Week",
                    axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("Units:Q", title="Units"),
            color=alt.Color("Period:N", scale=alt.Scale(domain=["This Year", "Last Year"],
                            range=["#185FA5", "#A0A09A"])),
            xOffset="Period:N",
            tooltip=["week_label", "Period", alt.Tooltip("Units:Q", format=",")],
        ).properties(height=320)), use_container_width=True)

        show = weekly_sales[["week_label", "units_ty", "units_ly", "yoy_units", "yoy_pct", "sales_ty"]].copy()
        show.columns = ["Week", "Units TY", "Units LY", "YoY Units", "YoY %", "Sales TY ($)"]
        show = show.tail(8).iloc[::-1]
        st.dataframe(show, use_container_width=True, hide_index=True,
                     column_config={
                         "Sales TY ($)": st.column_config.NumberColumn(format="$%.0f"),
                         "YoY %": st.column_config.NumberColumn(format="%.1f%%"),
                     })

    # ── Velocity U/S/W ───────────────────────────────────────────────────────
    st.subheader("Velocity — Units per Store per Week (U/S/W)")
    st.caption(
        "Total units a week, normalized by stores that actually moved product. "
        "If total units drop but U/S/W holds, it's a distribution problem; "
        "if U/S/W also drops, customers aren't buying."
    )

    def _active(g, col): return g[g[col] > 0]["store_number"].nunique()

    weekly_uspw = df.groupby("walmart_calendar_week", as_index=False).apply(
        lambda g: pd.Series({
            "week_start": g["business_date"].min(),
            "units_ty": g["pos_quantity_this_year"].sum(),
            "units_ly": g["pos_quantity_last_year"].sum(),
            "stores_ty": _active(g, "pos_quantity_this_year"),
            "stores_ly": _active(g, "pos_quantity_last_year"),
        }),
        include_groups=False,
    ).reset_index(drop=True)
    weekly_uspw["uspw_ty"] = np.where(weekly_uspw["stores_ty"] > 0,
        (weekly_uspw["units_ty"] / weekly_uspw["stores_ty"]).round(2), 0)
    weekly_uspw["uspw_ly"] = np.where(weekly_uspw["stores_ly"] > 0,
        (weekly_uspw["units_ly"] / weekly_uspw["stores_ly"]).round(2), 0)
    weekly_uspw["week_label"] = "WM Wk " + weekly_uspw["walmart_calendar_week"].astype(str).str[-2:]
    weekly_uspw = weekly_uspw.sort_values("walmart_calendar_week").reset_index(drop=True)

    if not weekly_uspw.empty:
        m = weekly_uspw.melt(id_vars=["week_label", "walmart_calendar_week"],
                             value_vars=["uspw_ty", "uspw_ly"],
                             var_name="Period", value_name="U/S/W")
        m["Period"] = m["Period"].map({"uspw_ty": "This Year", "uspw_ly": "Last Year"})
        st.altair_chart((alt.Chart(m).mark_line(point=alt.OverlayMarkDef(size=80), strokeWidth=3).encode(
            x=alt.X("week_label:N", sort=list(weekly_uspw["week_label"]), title="Week",
                    axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("U/S/W:Q", title="Units per Store per Week"),
            color=alt.Color("Period:N", scale=alt.Scale(domain=["This Year", "Last Year"],
                            range=["#185FA5", "#A0A09A"])),
            tooltip=["week_label", "Period", alt.Tooltip("U/S/W:Q", format=".2f")],
        ).properties(height=300)), use_container_width=True)

    # ── U/S/W by item ────────────────────────────────────────────────────────
    st.markdown("**U/S/W by item (period total)**")
    item_uspw = df.groupby("walmart_item_number", as_index=False).apply(
        lambda g: pd.Series({
            "units_ty": g["pos_quantity_this_year"].sum(),
            "units_ly": g["pos_quantity_last_year"].sum(),
            "stores_ty": _active(g, "pos_quantity_this_year"),
            "stores_ly": _active(g, "pos_quantity_last_year"),
        }),
        include_groups=False,
    )
    item_uspw["walmart_item_number"] = df.groupby("walmart_item_number").size().index
    item_uspw["item"] = item_uspw["walmart_item_number"].map(ITEM_LABELS).fillna(
        item_uspw["walmart_item_number"].astype(str))
    item_uspw["uspw_ty"] = np.where(item_uspw["stores_ty"] > 0,
        (item_uspw["units_ty"] / item_uspw["stores_ty"] / weeks_in_period).round(2), 0)
    item_uspw["uspw_ly"] = np.where(item_uspw["stores_ly"] > 0,
        (item_uspw["units_ly"] / item_uspw["stores_ly"] / weeks_in_period).round(2), 0)
    item_uspw["uspw_yoy_pct"] = np.where(item_uspw["uspw_ly"] > 0,
        ((item_uspw["uspw_ty"] - item_uspw["uspw_ly"]) / item_uspw["uspw_ly"] * 100).round(1), 0)

    if len(item_uspw) > 0:
        iu_cols = st.columns(len(item_uspw))
        for col, (_, row) in zip(iu_cols, item_uspw.iterrows()):
            with col:
                yoy_pct = row["uspw_yoy_pct"]
                st.metric(f"{row['item']}", f"{row['uspw_ty']:.2f} U/S/W",
                          delta=f"{yoy_pct:+.1f}% vs LY ({row['uspw_ly']:.2f})")

    # ── Store velocity distribution ──────────────────────────────────────────
    st.markdown("**Store-level velocity distribution**")
    store_uspw = df.groupby("store_number", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
    )
    store_uspw["uspw_ty"] = (store_uspw["units_ty"] / weeks_in_period).round(1)
    store_uspw["uspw_ly"] = (store_uspw["units_ly"] / weeks_in_period).round(1)

    def _bucket(v):
        if v == 0: return "0 (none)"
        if v < 1:  return "0-1"
        if v < 3:  return "1-3"
        if v < 5:  return "3-5"
        if v < 10: return "5-10"
        if v < 20: return "10-20"
        return "20+"
    ORDER = ["0 (none)", "0-1", "1-3", "3-5", "5-10", "10-20", "20+"]
    store_uspw["bucket_ty"] = store_uspw["uspw_ty"].apply(_bucket)
    store_uspw["bucket_ly"] = store_uspw["uspw_ly"].apply(_bucket)
    d_ty = store_uspw["bucket_ty"].value_counts().reindex(ORDER, fill_value=0).reset_index()
    d_ty.columns = ["bucket", "stores"]; d_ty["Period"] = "This Year"
    d_ly = store_uspw["bucket_ly"].value_counts().reindex(ORDER, fill_value=0).reset_index()
    d_ly.columns = ["bucket", "stores"]; d_ly["Period"] = "Last Year"
    dist = pd.concat([d_ty, d_ly], ignore_index=True)
    st.altair_chart((alt.Chart(dist).mark_bar().encode(
        x=alt.X("bucket:N", sort=ORDER, title="U/S/W tier"),
        y=alt.Y("stores:Q", title="Number of stores"),
        color=alt.Color("Period:N", scale=alt.Scale(domain=["This Year", "Last Year"],
                        range=["#185FA5", "#A0A09A"])),
        xOffset="Period:N",
        tooltip=["bucket", "Period", alt.Tooltip("stores:Q", format=",")],
    ).properties(height=300)), use_container_width=True)

    # ── NEW: Forecast attainment ────────────────────────────────────────────
    st.divider()
    st.subheader("Forecast attainment")
    st.caption(
        "Actual sales as a percent of forecasted demand. Tells you whether the YoY decline "
        "is hitting forecast (demand issue) or missing forecast (supply/distribution issue)."
    )

    fcst_df, fcst_err = load_forecast_data(lookback)
    if fcst_err:
        st.warning(f"Forecast data unavailable — schema mismatch or permission issue.\n\n`{fcst_err}`")
    elif fcst_df.empty:
        st.info("No forecast records returned for the selected items and lookback window.")
    else:
        fcst_df_filt = fcst_df[fcst_df["walmart_item_number"].isin(item_filter)].copy()
        actuals = df.groupby(["business_date", "walmart_item_number"], as_index=False)[
            "pos_quantity_this_year"].sum().rename(columns={
                "business_date": "forecast_date",
                "pos_quantity_this_year": "actual_quantity",
            })
        attn = fcst_df_filt.groupby(["forecast_date", "walmart_item_number"], as_index=False)[
            "forecast_quantity"].sum().merge(actuals, on=["forecast_date", "walmart_item_number"], how="left")
        attn["actual_quantity"] = attn["actual_quantity"].fillna(0)
        weekly = attn.copy()
        weekly["week_start"] = weekly["forecast_date"] - pd.to_timedelta(weekly["forecast_date"].dt.weekday, unit="D")
        weekly_attn = weekly.groupby("week_start", as_index=False).agg(
            forecast=("forecast_quantity", "sum"),
            actual=("actual_quantity", "sum"),
        ).sort_values("week_start")
        weekly_attn["attainment_pct"] = np.where(weekly_attn["forecast"] > 0,
            (weekly_attn["actual"] / weekly_attn["forecast"] * 100).round(1), 0)
        weekly_attn["week_label"] = weekly_attn["week_start"].dt.strftime("Wk %b %d")

        fa_l, fa_r = st.columns([3, 2])
        with fa_l:
            chart_m = weekly_attn.melt(id_vars=["week_label"], value_vars=["forecast", "actual"],
                                       var_name="Series", value_name="Units")
            chart_m["Series"] = chart_m["Series"].map({"forecast": "Forecast", "actual": "Actual"})
            st.altair_chart((alt.Chart(chart_m).mark_bar().encode(
                x=alt.X("week_label:N", sort=list(weekly_attn["week_label"]), title="Week",
                        axis=alt.Axis(labelAngle=-30)),
                y=alt.Y("Units:Q", title="Units"),
                color=alt.Color("Series:N", scale=alt.Scale(domain=["Forecast", "Actual"],
                                range=["#A0A09A", "#185FA5"])),
                xOffset="Series:N",
                tooltip=["week_label", "Series", alt.Tooltip("Units:Q", format=",")],
            ).properties(height=320)), use_container_width=True)
        with fa_r:
            show = weekly_attn[["week_label", "actual", "forecast", "attainment_pct"]].iloc[::-1].copy()
            show.columns = ["Week", "Actual", "Forecast", "Attainment %"]
            st.dataframe(show, use_container_width=True, hide_index=True,
                         column_config={"Attainment %": st.column_config.NumberColumn(format="%.1f%%")})

        avg_attn = weekly_attn["attainment_pct"].mean()
        st.caption(f"**Average period attainment: {avg_attn:.1f}%** — values below 100% mean actuals lagged forecast.")


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 3 — INVENTORY & DC
# ═══════════════════════════════════════════════════════════════════════════
with tab_inv:
    # ── Weekly inventory trend ───────────────────────────────────────────────
    st.subheader("Weekly inventory trend (network total)")

    last_day_per_week_item = (
        df.groupby(["walmart_calendar_week", "walmart_item_number"])["business_date"]
        .max().reset_index().rename(columns={"business_date": "snapshot_date"})
    )
    eow = df.merge(last_day_per_week_item,
                   left_on=["walmart_calendar_week", "walmart_item_number", "business_date"],
                   right_on=["walmart_calendar_week", "walmart_item_number", "snapshot_date"],
                   how="inner")
    weekly_inv = eow.groupby("walmart_calendar_week", as_index=False).agg(
        on_hand_ty=("store_on_hand_quantity_this_year", "sum"),
        on_hand_ly=("store_on_hand_quantity_last_year", "sum"),
        in_warehouse=("store_in_warehouse_quantity_this_year", "sum"),
        in_transit=("store_in_transit_quantity_this_year", "sum"),
        snapshot_date=("snapshot_date", "max"),
    ).sort_values("walmart_calendar_week").reset_index(drop=True)
    weekly_inv["week_label"] = "WM Wk " + weekly_inv["walmart_calendar_week"].astype(str).str[-2:]

    if not weekly_inv.empty:
        m = weekly_inv.melt(id_vars=["week_label", "walmart_calendar_week"],
                            value_vars=["on_hand_ty", "in_warehouse", "in_transit"],
                            var_name="Component", value_name="Units")
        m["Component"] = m["Component"].map({
            "on_hand_ty": "On Hand (stores)", "in_warehouse": "In Warehouse", "in_transit": "In Transit",
        })
        st.altair_chart((alt.Chart(m).mark_area().encode(
            x=alt.X("week_label:N", sort=list(weekly_inv["week_label"]),
                    title="Week (end-of-week snapshot)", axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("Units:Q", stack="zero", title="Units"),
            color=alt.Color("Component:N", scale=alt.Scale(
                domain=["On Hand (stores)", "In Warehouse", "In Transit"],
                range=["#185FA5", "#5BA3D8", "#A8D0E6"])),
            tooltip=["week_label", "Component", alt.Tooltip("Units:Q", format=",")],
        ).properties(height=320)), use_container_width=True)

        latest = weekly_inv.iloc[-1]
        yoy = ((latest["on_hand_ty"] - latest["on_hand_ly"]) / latest["on_hand_ly"] * 100) if latest["on_hand_ly"] else 0
        st.caption(f"Latest week network on-hand: **{int(latest['on_hand_ty']):,}** units "
                   f"({yoy:+.1f}% vs LY {int(latest['on_hand_ly']):,})")

    # ── NEW: Phantom Inventory ───────────────────────────────────────────────
    st.divider()
    st.subheader("Phantom inventory (backroom adjustments)")
    st.caption(
        "Inventory adjustments correcting system on-hand to match reality. "
        "Large **negative** adjustments = system was over-counting (stores thought they had stock but didn't). "
        "This is a key driver of unexplained out-of-stocks."
    )

    br_df, br_err = load_backroom_data(lookback)
    if br_err:
        st.warning(f"Backroom adjustment data unavailable.\n\n`{br_err}`")
    elif br_df.empty:
        st.info("No backroom adjustment records in window.")
    else:
        br_filt = br_df[br_df["walmart_item_number"].isin(item_filter)].copy()
        if br_filt.empty:
            st.info("No adjustments for the selected items.")
        else:
            # Daily total adjustment volume and net direction
            br_daily = br_filt.groupby("adjustment_date", as_index=False).agg(
                net_adj=("adjustment_qty_ty", "sum"),
                abs_adj=("adjustment_qty_ty", lambda s: s.abs().sum()),
                stores_affected=("store_number", "nunique"),
            ).sort_values("adjustment_date")

            pi_l, pi_r = st.columns([3, 2])
            with pi_l:
                st.altair_chart((alt.Chart(br_daily).mark_bar().encode(
                    x=alt.X("adjustment_date:T", title="Date"),
                    y=alt.Y("net_adj:Q", title="Net adjustment (units)"),
                    color=alt.condition(
                        alt.datum.net_adj < 0,
                        alt.value("#791F1F"),  # Negative = red (phantom inv discovered)
                        alt.value("#27500A"),  # Positive = green
                    ),
                    tooltip=[alt.Tooltip("adjustment_date:T", title="Date"),
                             alt.Tooltip("net_adj:Q", format=",.0f", title="Net adj")],
                ).properties(height=280)), use_container_width=True)
            with pi_r:
                net_total = int(br_filt["adjustment_qty_ty"].sum())
                abs_total = int(br_filt["adjustment_qty_ty"].abs().sum())
                neg_total = int(br_filt[br_filt["adjustment_qty_ty"] < 0]["adjustment_qty_ty"].sum())
                stores_aff = int(br_filt["store_number"].nunique())
                st.metric("Net adjustment", f"{net_total:+,}",
                          delta="phantom inventory if negative", delta_color="off")
                st.metric("Negative adjustments", f"{neg_total:,}",
                          help="Units removed because they weren't really there")
                st.metric("Total adjustment magnitude", f"{abs_total:,}")
                st.metric("Stores with adjustments", f"{stores_aff:,}")

    # ── DC pipeline health (using true alignment) ────────────────────────────
    st.divider()
    st.subheader("DC pipeline health")

    if dc_df.empty:
        st.info("DC data unavailable for the current filter.")
    else:
        # Load store→DC alignment
        align_df, align_err = load_dc_alignment()
        latest_dc_date = dc_df["inventory_date"].max()
        dc_latest = dc_df[dc_df["inventory_date"] == latest_dc_date].copy()

        # Compute true per-DC demand if alignment is available
        if align_err or align_df.empty:
            if align_err:
                st.caption(f"⚠️  Using approximate DC demand allocation (alignment table unavailable: {align_err})")
            total_per_day = df["pos_quantity_this_year"].sum() / max(1, lookback)
            dc_summary = dc_latest.groupby(
                ["distribution_center_number", "name_of_the_dc"], as_index=False
            ).agg(
                on_hand=("on_hand_warehouse_inventory_in_units_this_year", "sum"),
                on_order=("on_order_warehouse_quantity_in_units_this_year", "sum"),
                oos=("out_of_stock_each_quantity_this_year", "sum"),
            )
            dc_summary["total_supply"] = dc_summary["on_hand"] + dc_summary["on_order"]
            network_oh = max(1, dc_summary["on_hand"].sum())
            dc_summary["daily_demand_est"] = (dc_summary["on_hand"] / network_oh) * total_per_day
            dc_summary["wos_oh"] = np.where(dc_summary["daily_demand_est"] > 0,
                (dc_summary["on_hand"] / (dc_summary["daily_demand_est"] * 7)).round(1), 0)
        else:
            # True allocation: sum store sales by their aligned DC
            st.caption(f"✓ Using true store→DC demand allocation ({len(align_df):,} alignments)")
            # If multiple alignments per store, use first (typically primary)
            primary_align = align_df.drop_duplicates(subset=["store_number"], keep="first")
            store_demand = df.groupby("store_number", as_index=False)["pos_quantity_this_year"].sum()
            store_demand = store_demand.merge(primary_align, on="store_number", how="left")
            dc_demand = store_demand.groupby("distribution_center_number", as_index=False)[
                "pos_quantity_this_year"].sum().rename(columns={"pos_quantity_this_year": "period_demand"})
            dc_demand["daily_demand"] = dc_demand["period_demand"] / max(1, lookback)

            dc_summary = dc_latest.groupby(
                ["distribution_center_number", "name_of_the_dc"], as_index=False
            ).agg(
                on_hand=("on_hand_warehouse_inventory_in_units_this_year", "sum"),
                on_order=("on_order_warehouse_quantity_in_units_this_year", "sum"),
                oos=("out_of_stock_each_quantity_this_year", "sum"),
            )
            dc_summary["total_supply"] = dc_summary["on_hand"] + dc_summary["on_order"]
            dc_summary = dc_summary.merge(dc_demand[["distribution_center_number", "daily_demand"]],
                                          on="distribution_center_number", how="left")
            dc_summary["daily_demand"] = dc_summary["daily_demand"].fillna(0)
            dc_summary["wos_oh"] = np.where(dc_summary["daily_demand"] > 0,
                (dc_summary["on_hand"] / (dc_summary["daily_demand"] * 7)).round(1), np.inf)

        dc_summary = dc_summary.sort_values("on_hand", ascending=False)

        dc_l, dc_r = st.columns([1, 2])
        with dc_l:
            st.metric("Network DC on-hand", f"{int(dc_summary['on_hand'].sum()):,}")
            st.metric("Network DC on-order", f"{int(dc_summary['on_order'].sum()):,}")
            critical = int((dc_summary["wos_oh"].replace(np.inf, 999) <= 1).sum())
            st.metric("DCs ≤ 1 wk supply", f"{critical:,}", delta_color="inverse")
            st.caption(f"Snapshot: {latest_dc_date.strftime('%b %d, %Y')}")
        with dc_r:
            show_dc = dc_summary[["distribution_center_number", "name_of_the_dc",
                                  "on_hand", "on_order", "total_supply", "wos_oh"]].copy()
            show_dc["wos_oh"] = show_dc["wos_oh"].replace(np.inf, np.nan)
            show_dc.columns = ["DC #", "DC Name", "On Hand", "On Order", "Total Supply", "WOS (OH)"]
            st.dataframe(show_dc, use_container_width=True, hide_index=True, height=420,
                         column_config={"WOS (OH)": st.column_config.NumberColumn(format="%.1f wks")})


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 4 — CHANNELS (omni sales, ecom inventory, returns)
# ═══════════════════════════════════════════════════════════════════════════
with tab_channels:
    st.subheader("Omni-channel sales")
    st.caption(
        "Sales across all channels (in-store + pickup + ship-to-home + ship-from-store etc). "
        "Comparing in-store-only against omni-total tells you whether decline is real or channel migration."
    )

    omni_df, omni_err = load_omni_data(lookback)
    if omni_err:
        st.warning(f"Omni sales unavailable.\n\n`{omni_err}`")
    elif omni_df.empty:
        st.info("No omni sales records.")
    else:
        omni_filt = omni_df[omni_df["walmart_item_number"].isin(item_filter)].copy()

        omni_total_ty = float(omni_filt["units_ty"].sum())
        omni_total_ly = float(omni_filt["units_ly"].sum())
        omni_yoy = ((omni_total_ty - omni_total_ly) / omni_total_ly * 100) if omni_total_ly else 0

        in_store_ty = float(df["pos_quantity_this_year"].sum())
        in_store_ly = float(df["pos_quantity_last_year"].sum())
        in_store_yoy = ((in_store_ty - in_store_ly) / in_store_ly * 100) if in_store_ly else 0

        ch_k1, ch_k2, ch_k3 = st.columns(3)
        ch_k1.metric("In-store units", f"{int(in_store_ty):,}", f"{in_store_yoy:+.1f}% YoY")
        ch_k2.metric("Omni units (all channels)", f"{int(omni_total_ty):,}", f"{omni_yoy:+.1f}% YoY")
        non_store = max(0, omni_total_ty - in_store_ty)
        ch_k3.metric("Non-in-store omni", f"{int(non_store):,}",
                     help="Online, pickup, ship-from-store, etc.")

        # Channel mix
        st.markdown("**Sales by service channel**")
        by_chan = omni_filt.groupby("service_channel", as_index=False).agg(
            units_ty=("units_ty", "sum"),
            units_ly=("units_ly", "sum"),
            sales_ty=("sales_ty", "sum"),
        ).sort_values("units_ty", ascending=False)
        by_chan["yoy_pct"] = np.where(by_chan["units_ly"] > 0,
            ((by_chan["units_ty"] - by_chan["units_ly"]) / by_chan["units_ly"] * 100).round(1), 0)
        if not by_chan.empty:
            st.altair_chart((alt.Chart(by_chan).mark_bar().encode(
                x=alt.X("units_ty:Q", title="Units TY"),
                y=alt.Y("service_channel:N", sort="-x", title="Channel"),
                color=alt.Color("yoy_pct:Q",
                    scale=alt.Scale(scheme="redyellowgreen", domainMid=0),
                    legend=alt.Legend(title="YoY %")),
                tooltip=["service_channel:N", alt.Tooltip("units_ty:Q", format=","),
                         alt.Tooltip("units_ly:Q", format=","),
                         alt.Tooltip("yoy_pct:Q", format=".1f")],
            ).properties(height=280)), use_container_width=True)

    # ── eComm Inventory ──────────────────────────────────────────────────────
    st.divider()
    st.subheader("eComm inventory (ship nodes)")
    st.caption("On-hand and available-to-sell at fulfillment-center / ship-from-store nodes.")

    ecom_df, ecom_err = load_ecom_inv_data(lookback)
    if ecom_err:
        st.warning(f"eComm inventory unavailable.\n\n`{ecom_err}`")
    elif ecom_df.empty:
        st.info("No eComm inventory records.")
    else:
        ecom_filt = ecom_df[ecom_df["walmart_item_number"].isin(item_filter)].copy()
        latest_ecom = ecom_filt["report_date"].max() if not ecom_filt.empty else None
        if latest_ecom is not None:
            ecom_snap = ecom_filt[ecom_filt["report_date"] == latest_ecom]

            ec_k1, ec_k2, ec_k3 = st.columns(3)
            ec_k1.metric("On-hand units (latest)", f"{int(ecom_snap['on_hand_units'].sum()):,}")
            ec_k2.metric("Available to sell", f"{int(ecom_snap['available_to_sell'].sum()):,}")
            ec_k3.metric("Ship nodes carrying", f"{ecom_snap['ship_node'].nunique():,}")
            st.caption(f"Snapshot: {latest_ecom.strftime('%b %d, %Y')}")

            # Trend
            ecom_trend = ecom_filt.groupby("report_date", as_index=False).agg(
                on_hand=("on_hand_units", "sum"),
                ats=("available_to_sell", "sum"),
            ).sort_values("report_date")
            if not ecom_trend.empty:
                m = ecom_trend.melt(id_vars=["report_date"], value_vars=["on_hand", "ats"],
                                    var_name="Series", value_name="Units")
                m["Series"] = m["Series"].map({"on_hand": "On Hand", "ats": "Available to Sell"})
                st.altair_chart((alt.Chart(m).mark_line(point=True).encode(
                    x=alt.X("report_date:T", title="Date"),
                    y=alt.Y("Units:Q", title="Units"),
                    color=alt.Color("Series:N", scale=alt.Scale(domain=["On Hand", "Available to Sell"],
                                    range=["#185FA5", "#5BA3D8"])),
                    tooltip=[alt.Tooltip("report_date:T"), "Series", alt.Tooltip("Units:Q", format=",")],
                ).properties(height=280)), use_container_width=True)

    # ── Store Returns ────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Store returns")
    st.caption(
        "Tracks if returns are growing. Climbing returns eat into net sales and may signal "
        "product quality, packaging, or fit issues."
    )

    ret_df, ret_err = load_returns_data(lookback)
    if ret_err:
        st.warning(f"Returns data unavailable.\n\n`{ret_err}`")
    elif ret_df.empty:
        st.info("No return records in window.")
    else:
        ret_filt = ret_df[ret_df["walmart_item_number"].isin(item_filter)].copy()
        if ret_filt.empty:
            st.info("No returns for selected items.")
        else:
            ret_ty_total = int(ret_filt["returns_ty"].sum())
            ret_ly_total = int(ret_filt["returns_ly"].sum())
            ret_yoy = ((ret_ty_total - ret_ly_total) / ret_ly_total * 100) if ret_ly_total else 0
            # Return rate = returns / units sold
            units_total = float(df["pos_quantity_this_year"].sum())
            ret_rate = (ret_ty_total / units_total * 100) if units_total else 0

            r_k1, r_k2, r_k3 = st.columns(3)
            r_k1.metric("Returns (units)", f"{ret_ty_total:,}", f"{ret_yoy:+.1f}% YoY",
                        delta_color="inverse")
            r_k2.metric("Return rate", f"{ret_rate:.2f}%",
                        help="returns / units sold over the period")
            r_k3.metric("Return $ (TY)", f"${ret_filt['return_sales_ty'].sum():,.0f}")

            # Weekly trend
            ret_filt["week_start"] = ret_filt["return_date"] - pd.to_timedelta(
                ret_filt["return_date"].dt.weekday, unit="D")
            weekly_ret = ret_filt.groupby("week_start", as_index=False).agg(
                returns_ty=("returns_ty", "sum"),
                returns_ly=("returns_ly", "sum"),
            ).sort_values("week_start")
            weekly_ret["week_label"] = weekly_ret["week_start"].dt.strftime("Wk %b %d")

            if not weekly_ret.empty:
                m = weekly_ret.melt(id_vars=["week_label"], value_vars=["returns_ty", "returns_ly"],
                                    var_name="Period", value_name="Returns")
                m["Period"] = m["Period"].map({"returns_ty": "This Year", "returns_ly": "Last Year"})
                st.altair_chart((alt.Chart(m).mark_bar().encode(
                    x=alt.X("week_label:N", sort=list(weekly_ret["week_label"]),
                            title="Week", axis=alt.Axis(labelAngle=-30)),
                    y=alt.Y("Returns:Q", title="Returns (units)"),
                    color=alt.Color("Period:N", scale=alt.Scale(domain=["This Year", "Last Year"],
                                    range=["#791F1F", "#A0A09A"])),
                    xOffset="Period:N",
                    tooltip=["week_label", "Period", alt.Tooltip("Returns:Q", format=",")],
                ).properties(height=280)), use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 5 — DISTRIBUTION
# ═══════════════════════════════════════════════════════════════════════════
with tab_dist:
    # ── NEW: Modular coverage (distribution gap) ─────────────────────────────
    st.subheader("Modular coverage gap")
    st.caption(
        "Stores in the active modular plan vs. stores that actually sold this item. "
        "The gap = execution failure (item should be carried but isn't moving)."
    )

    mod_df, mod_err = load_modular_data()
    if mod_err:
        st.warning(f"Modular plan data unavailable.\n\n`{mod_err}`")
    elif mod_df.empty:
        st.info("No modular plan records returned.")
    else:
        mod_filt = mod_df[mod_df["walmart_item_number"].isin(item_filter)].copy()
        if mod_filt.empty:
            st.info("No modular plans for selected items.")
        else:
            # Per item: planogram stores vs selling stores
            rows = []
            for item in item_filter:
                planogram_stores = set(mod_filt[mod_filt["walmart_item_number"] == item]["store_number"])
                selling_stores = set(
                    df[(df["walmart_item_number"] == item) & (df["pos_quantity_this_year"] > 0)]["store_number"]
                )
                in_plan = len(planogram_stores)
                in_plan_selling = len(planogram_stores & selling_stores)
                selling_not_in_plan = len(selling_stores - planogram_stores)
                coverage_pct = (in_plan_selling / in_plan * 100) if in_plan else 0
                rows.append({
                    "Item": ITEM_LABELS.get(item, str(item)),
                    "Item #": item,
                    "In plan": in_plan,
                    "In plan + selling": in_plan_selling,
                    "Coverage %": round(coverage_pct, 1),
                    "Selling but not in plan": selling_not_in_plan,
                })
            cov_df = pd.DataFrame(rows)
            st.dataframe(cov_df, use_container_width=True, hide_index=True,
                         column_config={"Coverage %": st.column_config.NumberColumn(format="%.1f%%")})

            # KPI bar across items
            cv_cols = st.columns(len(cov_df))
            for col, (_, row) in zip(cv_cols, cov_df.iterrows()):
                with col:
                    st.markdown(f"**{row['Item']}**")
                    st.metric("Coverage", f"{row['Coverage %']:.1f}%",
                              help=f"{row['In plan + selling']:,} of {row['In plan']:,} planogram stores have sales")
                    gap = row["In plan"] - row["In plan + selling"]
                    st.caption(f"Gap: **{gap:,}** stores in plan but not selling")

    # ── Performance by state ─────────────────────────────────────────────────
    st.divider()
    st.subheader("Performance by state (top 20)")

    state_perf = df.groupby("state_or_province_code", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        stores=("store_number", "nunique"),
    )
    state_perf["yoy_pct"] = np.where(state_perf["units_ly"] > 0,
        ((state_perf["units_ty"] - state_perf["units_ly"]) / state_perf["units_ly"] * 100).round(1), 0)
    state_perf = state_perf.sort_values("units_ty", ascending=False).head(20)

    st.altair_chart((alt.Chart(state_perf).mark_bar().encode(
        x=alt.X("units_ty:Q", title="Units TY"),
        y=alt.Y("state_or_province_code:N", sort="-x", title="State"),
        color=alt.Color("yoy_pct:Q", scale=alt.Scale(scheme="redyellowgreen", domainMid=0),
                        legend=alt.Legend(title="YoY %")),
        tooltip=[alt.Tooltip("state_or_province_code:N", title="State"),
                 alt.Tooltip("units_ty:Q", format=",", title="Units TY"),
                 alt.Tooltip("units_ly:Q", format=",", title="Units LY"),
                 alt.Tooltip("yoy_pct:Q", format=".1f", title="YoY %"),
                 alt.Tooltip("stores:Q", title="Stores")],
    ).properties(height=420)), use_container_width=True)


# ─── Footer ──────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"BigQuery rows: {len(df):,} store · {len(dc_df):,} DC · "
    f"Cache TTL: 24 hr · Sidebar refresh button for manual reload"
)
