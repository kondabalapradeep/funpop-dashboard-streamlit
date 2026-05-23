"""
streamlit_app.py — FunPop Sales Dashboard, redesigned.

Sections, top to bottom:
  1. Headline KPIs
  2. Weekly sales trend (TY vs LY, with YoY%)
  3. Weekly inventory trend (network on-hand + total supply)
  4. Last 10 days — daily detail
  5. Item performance comparison (3 items side by side)
  6. Stockout risk — store-level OOS counts over time
  7. Top + bottom store movers
  8. State-level performance
  9. DC pipeline health
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

# Responsive CSS — stacks multi-column layouts vertically on phones/small tablets
# while keeping the wide desktop layout. Breakpoint: 900px (covers phones in
# any orientation and tablets in portrait; landscape tablets get desktop view).
st.markdown(
    """
    <style>
    @media (max-width: 900px) {
        /* Stack st.columns content vertically */
        div[data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
            gap: 0.75rem;
        }
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            flex: 1 1 100% !important;
            min-width: 100% !important;
            width: 100% !important;
        }
        /* Tighten page padding for narrow screens */
        .block-container {
            padding-top: 1rem;
            padding-left: 0.75rem;
            padding-right: 0.75rem;
        }
        /* Slightly smaller metric values so labels stop wrapping */
        div[data-testid="stMetricValue"] {
            font-size: 1.5rem;
        }
        /* Make tables horizontally scrollable rather than crammed */
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

# DC inventory comes in case packs, not eaches. Multiplier converts to units.
# Source: confirmed with Nathan (full bin = 208 ea, half bin = 126 ea, shelf = 6 ea).
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


# ─── Data loaders ────────────────────────────────────────────────────────────
@st.cache_data(ttl=86400, show_spinner="Loading store data from BigQuery...")
def load_store_data(lookback_days: int) -> pd.DataFrame:
    client = get_bq_client()
    sql = _load_sql("store_query.sql")
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
    ])
    df = client.query(sql, job_config=job_config).to_dataframe(create_bqstorage_client=False)
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
    sold = df[df["pos_quantity_this_year"] > 0]
    if len(sold) and sold["store_specific_retail_amount_this_year"].median() > 10:
        unit_price = np.where(
            df["pos_quantity_this_year"] > 0,
            (df["pos_sales_this_year"] / df["pos_quantity_this_year"].replace(0, np.nan)).round(2),
            0,
        )
        df["store_specific_retail_amount_this_year"] = np.where(np.isnan(unit_price), 0, unit_price)
    return df


@st.cache_data(ttl=86400, show_spinner="Loading DC data from BigQuery...")
def load_dc_data(lookback_days: int) -> pd.DataFrame:
    try:
        client = get_bq_client()
        sql = _load_sql("dc_query.sql")
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("active_items", "INT64", core.ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        df = client.query(sql, job_config=job_config).to_dataframe(create_bqstorage_client=False)
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

        # Convert case packs -> eaches using per-item multiplier.
        # Walmart's DC tables report on_hand/on_order in warehouse packs, not units.
        # The OOS columns are already in eaches, so we skip those.
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
        st.warning(f"DC query failed; dashboard will run without DC analysis. ({e})")
        return pd.DataFrame()


# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Controls")
    if st.button("🔄 Refresh data", help="Clear cache, re-pull from BigQuery"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Data is cached for 24 hours. Click the button above to pull fresh data.")

    st.divider()
    st.subheader("Filters")

    item_options = list(core.ACTIVE_ITEMS)
    item_filter = st.multiselect(
        "Items",
        options=item_options,
        default=item_options,
        format_func=lambda i: f"{ITEM_LABELS.get(i, i)} ({i})",
    )

    lookback = st.slider(
        "Lookback (days)",
        min_value=14,
        max_value=120,
        value=30,
        step=7,
        help="Larger lookback = slower load. 30 days covers a healthy weekly view.",
    )


# ─── Load + filter ───────────────────────────────────────────────────────────
df = load_store_data(lookback_days=lookback)
dc_df = load_dc_data(lookback_days=lookback)

if df.empty:
    st.error(
        f"No store data for the last {lookback} days. "
        f"Check that items {list(core.ACTIVE_ITEMS)} have sales in this window."
    )
    st.stop()

if item_filter:
    df = df[df["walmart_item_number"].isin(item_filter)].copy()
    if not dc_df.empty:
        dc_df = dc_df[dc_df["walmart_item_number"].isin(item_filter)].copy()

if df.empty:
    st.warning("No data matches the current item filter.")
    st.stop()

most_recent = df["business_date"].max()
weeks_in_period = max(1, lookback / 7)


# ─── Header ──────────────────────────────────────────────────────────────────
st.title("FunPop Sales Dashboard")
st.caption(
    f"**{lookback}-day window** ending **{most_recent.strftime('%b %d, %Y')}** · "
    f"{df['store_number'].nunique():,} stores · {len(item_filter)} item(s)"
)


# ─── SECTION 1 — Headline KPIs ───────────────────────────────────────────────
st.subheader("Period at a glance")

units_ty = int(df["pos_quantity_this_year"].sum())
units_ly = int(df["pos_quantity_last_year"].sum())
units_yoy = units_ty - units_ly
units_yoy_pct = (units_yoy / units_ly * 100) if units_ly else 0
sales_ty = float(df["pos_sales_this_year"].sum())
sales_ly = float(df["pos_sales_last_year"].sum())
sales_yoy_pct = ((sales_ty - sales_ly) / sales_ly * 100) if sales_ly else 0
stores_selling = df[df["pos_quantity_this_year"] > 0]["store_number"].nunique()
stores_total = df["store_number"].nunique()
weekly_velocity = units_ty / stores_total / weeks_in_period if stores_total else 0

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Units sold", f"{units_ty:,}", f"{units_yoy_pct:+.1f}% YoY")
k2.metric("Sales", f"${sales_ty:,.0f}", f"{sales_yoy_pct:+.1f}% YoY")
k3.metric("YoY units", f"{units_yoy:+,}")
k4.metric("Stores w/ sales", f"{stores_selling:,}/{stores_total:,}",
          f"{(stores_selling/stores_total*100 if stores_total else 0):.0f}%")
k5.metric("Avg units/store/wk", f"{weekly_velocity:.1f}")


# ─── SECTION 2 — Last 10 days daily detail ───────────────────────────────────
st.subheader("Last 10 days — daily detail")

cutoff = most_recent - timedelta(days=9)
last10 = df[df["business_date"] >= cutoff].copy()

daily = last10.groupby("business_date", as_index=False).agg(
    units_ty=("pos_quantity_this_year", "sum"),
    units_ly=("pos_quantity_last_year", "sum"),
    sales_ty=("pos_sales_this_year", "sum"),
).sort_values("business_date")
# Stores selling on each day
stores_per_day = (
    last10[last10["pos_quantity_this_year"] > 0]
    .groupby("business_date")["store_number"].nunique()
    .reset_index()
    .rename(columns={"store_number": "stores_selling"})
)
daily = daily.merge(stores_per_day, on="business_date", how="left").fillna({"stores_selling": 0})
daily["yoy_units"] = daily["units_ty"] - daily["units_ly"]
daily["yoy_pct"] = np.where(
    daily["units_ly"] > 0,
    (daily["yoy_units"] / daily["units_ly"] * 100).round(1),
    0,
)
daily["weekday"] = daily["business_date"].dt.strftime("%a")
daily["date_str"] = daily["business_date"].dt.strftime("%b %d")

col_left, col_right = st.columns([3, 2])

with col_left:
    daily_melted = daily.melt(
        id_vars=["date_str", "weekday", "business_date"],
        value_vars=["units_ty", "units_ly"],
        var_name="Period", value_name="Units",
    )
    daily_melted["Period"] = daily_melted["Period"].map({"units_ty": "This Year", "units_ly": "Last Year"})
    daily_chart = (
        alt.Chart(daily_melted)
        .mark_line(point=True, strokeWidth=2.5)
        .encode(
            x=alt.X("date_str:N", sort=list(daily["date_str"]), title="Date", axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("Units:Q", title="Units"),
            color=alt.Color("Period:N",
                            scale=alt.Scale(domain=["This Year", "Last Year"],
                                            range=["#185FA5", "#A0A09A"])),
            tooltip=["date_str", "weekday", "Period", alt.Tooltip("Units:Q", format=",")],
        )
        .properties(height=300)
    )
    st.altair_chart(daily_chart, use_container_width=True)

with col_right:
    show_daily = daily[["weekday", "date_str", "units_ty", "yoy_pct", "stores_selling"]].iloc[::-1].copy()
    show_daily["stores_selling"] = show_daily["stores_selling"].astype(int)
    show_daily.columns = ["Day", "Date", "Units TY", "YoY %", "Stores Selling"]
    st.dataframe(
        show_daily, use_container_width=True, hide_index=True, height=380,
        column_config={"YoY %": st.column_config.NumberColumn(format="%.1f%%")},
    )


# ─── SECTION 3 — Weekly sales trend ──────────────────────────────────────────
st.subheader("Weekly sales trend")

weekly_sales = df.groupby("walmart_calendar_week", as_index=False).agg(
    units_ty=("pos_quantity_this_year", "sum"),
    units_ly=("pos_quantity_last_year", "sum"),
    sales_ty=("pos_sales_this_year", "sum"),
    sales_ly=("pos_sales_last_year", "sum"),
    week_start=("business_date", "min"),
).sort_values("walmart_calendar_week").reset_index(drop=True)
weekly_sales["yoy_units"] = weekly_sales["units_ty"] - weekly_sales["units_ly"]
weekly_sales["yoy_pct"] = np.where(
    weekly_sales["units_ly"] > 0,
    (weekly_sales["yoy_units"] / weekly_sales["units_ly"] * 100).round(1),
    0,
)
weekly_sales["week_label"] = "WM Wk " + weekly_sales["walmart_calendar_week"].astype(str).str[-2:]

if not weekly_sales.empty:
    melted = weekly_sales.melt(
        id_vars=["week_label", "walmart_calendar_week"],
        value_vars=["units_ty", "units_ly"],
        var_name="Period", value_name="Units",
    )
    melted["Period"] = melted["Period"].map({"units_ty": "This Year", "units_ly": "Last Year"})
    chart = (
        alt.Chart(melted)
        .mark_bar()
        .encode(
            x=alt.X("week_label:N", sort=list(weekly_sales["week_label"]), title="Week",
                    axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("Units:Q", title="Units"),
            color=alt.Color("Period:N",
                            scale=alt.Scale(domain=["This Year", "Last Year"],
                                            range=["#185FA5", "#A0A09A"])),
            xOffset="Period:N",
            tooltip=["week_label", "Period", alt.Tooltip("Units:Q", format=",")],
        )
        .properties(height=320)
    )
    st.altair_chart(chart, use_container_width=True)

    show = weekly_sales[["week_label", "units_ty", "units_ly", "yoy_units", "yoy_pct", "sales_ty"]].copy()
    show.columns = ["Week", "Units TY", "Units LY", "YoY Units", "YoY %", "Sales TY ($)"]
    # Newest week at top
    show = show.tail(8).iloc[::-1]
    st.dataframe(
        show,
        use_container_width=True, hide_index=True,
        column_config={
            "Sales TY ($)": st.column_config.NumberColumn(format="$%.0f"),
            "YoY %": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )


# ─── SECTION 4 — Velocity (U/S/W) ────────────────────────────────────────────
st.subheader("Velocity — Units per Store per Week (U/S/W)")
st.caption(
    "Total units a week, normalized by the number of stores that actually moved "
    "product. Separates a sales decline from a distribution decline: if total units "
    "drop but U/S/W holds, it's a distribution problem; if U/S/W is also dropping, "
    "customers aren't buying."
)

# Weekly U/S/W — for each week, count active stores (TY and LY separately)
def _active_stores_count(g, year_col):
    return g[g[year_col] > 0]["store_number"].nunique()

weekly_uspw = df.groupby("walmart_calendar_week", as_index=False).apply(
    lambda g: pd.Series({
        "week_start": g["business_date"].min(),
        "units_ty": g["pos_quantity_this_year"].sum(),
        "units_ly": g["pos_quantity_last_year"].sum(),
        "stores_ty": _active_stores_count(g, "pos_quantity_this_year"),
        "stores_ly": _active_stores_count(g, "pos_quantity_last_year"),
    }),
    include_groups=False,
).reset_index(drop=True)
weekly_uspw["uspw_ty"] = np.where(
    weekly_uspw["stores_ty"] > 0,
    (weekly_uspw["units_ty"] / weekly_uspw["stores_ty"]).round(2),
    0,
)
weekly_uspw["uspw_ly"] = np.where(
    weekly_uspw["stores_ly"] > 0,
    (weekly_uspw["units_ly"] / weekly_uspw["stores_ly"]).round(2),
    0,
)
weekly_uspw["uspw_yoy_pct"] = np.where(
    weekly_uspw["uspw_ly"] > 0,
    ((weekly_uspw["uspw_ty"] - weekly_uspw["uspw_ly"]) / weekly_uspw["uspw_ly"] * 100).round(1),
    0,
)
weekly_uspw["week_label"] = "WM Wk " + weekly_uspw["walmart_calendar_week"].astype(str).str[-2:]
weekly_uspw = weekly_uspw.sort_values("walmart_calendar_week").reset_index(drop=True)

if not weekly_uspw.empty:
    uspw_melted = weekly_uspw.melt(
        id_vars=["week_label", "walmart_calendar_week"],
        value_vars=["uspw_ty", "uspw_ly"],
        var_name="Period", value_name="U/S/W",
    )
    uspw_melted["Period"] = uspw_melted["Period"].map({"uspw_ty": "This Year", "uspw_ly": "Last Year"})
    uspw_chart = (
        alt.Chart(uspw_melted)
        .mark_line(point=alt.OverlayMarkDef(size=80), strokeWidth=3)
        .encode(
            x=alt.X("week_label:N", sort=list(weekly_uspw["week_label"]), title="Week",
                    axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("U/S/W:Q", title="Units per Store per Week"),
            color=alt.Color("Period:N",
                            scale=alt.Scale(domain=["This Year", "Last Year"],
                                            range=["#185FA5", "#A0A09A"])),
            tooltip=["week_label", "Period", alt.Tooltip("U/S/W:Q", format=".2f")],
        )
        .properties(height=300)
    )
    st.altair_chart(uspw_chart, use_container_width=True)

# U/S/W by item — current period summary
st.markdown("**U/S/W by item (period total)**")
item_uspw = df.groupby("walmart_item_number", as_index=False).apply(
    lambda g: pd.Series({
        "units_ty": g["pos_quantity_this_year"].sum(),
        "units_ly": g["pos_quantity_last_year"].sum(),
        "stores_ty": _active_stores_count(g, "pos_quantity_this_year"),
        "stores_ly": _active_stores_count(g, "pos_quantity_last_year"),
    }),
    include_groups=False,
)
# Re-attach the walmart_item_number after apply
item_uspw["walmart_item_number"] = df.groupby("walmart_item_number").size().index
item_uspw["item"] = item_uspw["walmart_item_number"].map(ITEM_LABELS).fillna(
    item_uspw["walmart_item_number"].astype(str)
)
item_uspw["uspw_ty"] = np.where(
    item_uspw["stores_ty"] > 0,
    (item_uspw["units_ty"] / item_uspw["stores_ty"] / weeks_in_period).round(2),
    0,
)
item_uspw["uspw_ly"] = np.where(
    item_uspw["stores_ly"] > 0,
    (item_uspw["units_ly"] / item_uspw["stores_ly"] / weeks_in_period).round(2),
    0,
)
item_uspw["uspw_yoy_pct"] = np.where(
    item_uspw["uspw_ly"] > 0,
    ((item_uspw["uspw_ty"] - item_uspw["uspw_ly"]) / item_uspw["uspw_ly"] * 100).round(1),
    0,
)

if len(item_uspw) > 0:
    item_cols = st.columns(len(item_uspw))
    for col, (_, row) in zip(item_cols, item_uspw.iterrows()):
        with col:
            yoy_pct = row["uspw_yoy_pct"]
            st.metric(
                f"{row['item']}",
                f"{row['uspw_ty']:.2f} U/S/W",
                delta=f"{yoy_pct:+.1f}% vs LY ({row['uspw_ly']:.2f})",
                delta_color="normal" if yoy_pct >= 0 else "inverse",
            )

# Store-level U/S/W distribution — see whether decline is concentrated or broad
st.markdown("**Store-level velocity distribution**")
st.caption(
    "Each bar = number of stores at that velocity tier. A flatter distribution means "
    "consistent performance across the network; a tall left bar means most stores are "
    "selling little, only a few are driving the totals."
)

store_uspw = df.groupby("store_number", as_index=False).agg(
    units_ty=("pos_quantity_this_year", "sum"),
    units_ly=("pos_quantity_last_year", "sum"),
)
store_uspw["uspw_ty"] = (store_uspw["units_ty"] / weeks_in_period).round(1)
store_uspw["uspw_ly"] = (store_uspw["units_ly"] / weeks_in_period).round(1)

# Bucket stores into velocity tiers
def _bucket_label(v):
    if v == 0: return "0 (none)"
    if v < 1:  return "0-1"
    if v < 3:  return "1-3"
    if v < 5:  return "3-5"
    if v < 10: return "5-10"
    if v < 20: return "10-20"
    return "20+"

BUCKET_ORDER = ["0 (none)", "0-1", "1-3", "3-5", "5-10", "10-20", "20+"]

store_uspw["bucket_ty"] = store_uspw["uspw_ty"].apply(_bucket_label)
store_uspw["bucket_ly"] = store_uspw["uspw_ly"].apply(_bucket_label)

dist_ty = store_uspw["bucket_ty"].value_counts().reindex(BUCKET_ORDER, fill_value=0).reset_index()
dist_ty.columns = ["bucket", "stores"]
dist_ty["Period"] = "This Year"

dist_ly = store_uspw["bucket_ly"].value_counts().reindex(BUCKET_ORDER, fill_value=0).reset_index()
dist_ly.columns = ["bucket", "stores"]
dist_ly["Period"] = "Last Year"

dist_combined = pd.concat([dist_ty, dist_ly], ignore_index=True)

dist_chart = (
    alt.Chart(dist_combined)
    .mark_bar()
    .encode(
        x=alt.X("bucket:N", sort=BUCKET_ORDER, title="Units per Store per Week (tier)"),
        y=alt.Y("stores:Q", title="Number of stores"),
        color=alt.Color("Period:N",
                        scale=alt.Scale(domain=["This Year", "Last Year"],
                                        range=["#185FA5", "#A0A09A"])),
        xOffset="Period:N",
        tooltip=["bucket", "Period", alt.Tooltip("stores:Q", format=",")],
    )
    .properties(height=300)
)
st.altair_chart(dist_chart, use_container_width=True)


# ─── SECTION 5 — Weekly inventory trend ──────────────────────────────────────
st.subheader("Weekly inventory trend (network total)")

last_day_per_week = df.groupby("walmart_calendar_week")["business_date"].max().reset_index()
last_day_per_week.columns = ["walmart_calendar_week", "snapshot_date"]
end_of_week = df.merge(
    last_day_per_week,
    left_on=["walmart_calendar_week", "business_date"],
    right_on=["walmart_calendar_week", "snapshot_date"],
    how="inner",
)
weekly_inv = end_of_week.groupby("walmart_calendar_week", as_index=False).agg(
    on_hand_ty=("store_on_hand_quantity_this_year", "sum"),
    on_hand_ly=("store_on_hand_quantity_last_year", "sum"),
    in_warehouse=("store_in_warehouse_quantity_this_year", "sum"),
    in_transit=("store_in_transit_quantity_this_year", "sum"),
    snapshot_date=("snapshot_date", "first"),
).sort_values("walmart_calendar_week").reset_index(drop=True)
weekly_inv["week_label"] = "WM Wk " + weekly_inv["walmart_calendar_week"].astype(str).str[-2:]

if not weekly_inv.empty:
    inv_melted = weekly_inv.melt(
        id_vars=["week_label", "walmart_calendar_week"],
        value_vars=["on_hand_ty", "in_warehouse", "in_transit"],
        var_name="Component", value_name="Units",
    )
    inv_melted["Component"] = inv_melted["Component"].map({
        "on_hand_ty": "On Hand (stores)",
        "in_warehouse": "In Warehouse",
        "in_transit": "In Transit",
    })
    inv_chart = (
        alt.Chart(inv_melted)
        .mark_area()
        .encode(
            x=alt.X("week_label:N", sort=list(weekly_inv["week_label"]),
                    title="Week (end-of-week snapshot)", axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("Units:Q", stack="zero", title="Units"),
            color=alt.Color("Component:N",
                            scale=alt.Scale(domain=["On Hand (stores)", "In Warehouse", "In Transit"],
                                            range=["#185FA5", "#5BA3D8", "#A8D0E6"])),
            tooltip=["week_label", "Component", alt.Tooltip("Units:Q", format=",")],
        )
        .properties(height=320)
    )
    st.altair_chart(inv_chart, use_container_width=True)

    if len(weekly_inv) >= 1:
        latest_oh_ty = weekly_inv.iloc[-1]["on_hand_ty"]
        latest_oh_ly = weekly_inv.iloc[-1]["on_hand_ly"]
        yoy_inv_pct = ((latest_oh_ty - latest_oh_ly) / latest_oh_ly * 100) if latest_oh_ly else 0
        st.caption(
            f"Latest week network on-hand: **{int(latest_oh_ty):,}** units "
            f"({yoy_inv_pct:+.1f}% vs LY {int(latest_oh_ly):,})"
        )


# ─── SECTION 6 — Item performance comparison ─────────────────────────────────
st.subheader("Item performance")

latest_snapshot = df[df["business_date"] == most_recent]
item_oh = latest_snapshot.groupby("walmart_item_number")["store_on_hand_quantity_this_year"].sum().to_dict()

item_perf = df.groupby("walmart_item_number", as_index=False).agg(
    units_ty=("pos_quantity_this_year", "sum"),
    units_ly=("pos_quantity_last_year", "sum"),
    sales_ty=("pos_sales_this_year", "sum"),
    stores=("store_number", "nunique"),
)
item_perf["on_hand"] = item_perf["walmart_item_number"].map(item_oh).fillna(0).astype(int)
item_perf["item"] = item_perf["walmart_item_number"].map(ITEM_LABELS).fillna(item_perf["walmart_item_number"].astype(str))
item_perf["yoy_units"] = item_perf["units_ty"] - item_perf["units_ly"]
item_perf["yoy_pct"] = np.where(
    item_perf["units_ly"] > 0,
    (item_perf["yoy_units"] / item_perf["units_ly"] * 100).round(1),
    0,
)
item_perf["wos"] = np.where(
    item_perf["units_ty"] > 0,
    (item_perf["on_hand"] / (item_perf["units_ty"] / weeks_in_period)).round(1),
    np.inf,
)

if len(item_perf) > 0:
    cols = st.columns(len(item_perf))
    for col, (_, row) in zip(cols, item_perf.iterrows()):
        with col:
            st.markdown(f"### {row['item']}")
            st.caption(f"Item {int(row['walmart_item_number'])}")
            st.metric("Units sold", f"{int(row['units_ty']):,}", f"{row['yoy_pct']:+.1f}% YoY")
            st.metric("Sales", f"${row['sales_ty']:,.0f}")
            st.metric("On hand (latest)", f"{int(row['on_hand']):,}")
            wos_label = "∞" if not np.isfinite(row['wos']) else f"{row['wos']:.1f} wks"
            st.metric("Weeks of supply", wos_label)


# ─── SECTION 7 — Stockout risk ──────────────────────────────────────────────
st.subheader("Stockout risk")

oos_daily_records = []
for date, g in df.groupby("business_date"):
    # Per-store latest on_hand on that date (if multi-item: store-item rows)
    store_totals = g.groupby("store_number")["store_on_hand_quantity_this_year"].sum()
    oos_daily_records.append({
        "business_date": date,
        "oos_stores": int((store_totals == 0).sum()),
        "total_stores": int(len(store_totals)),
    })
oos_daily = pd.DataFrame(oos_daily_records).sort_values("business_date").reset_index(drop=True)
oos_daily["oos_pct"] = (oos_daily["oos_stores"] / oos_daily["total_stores"] * 100).round(1)

co1, co2 = st.columns([2, 3])

with co1:
    if len(oos_daily):
        latest_oos = oos_daily.iloc[-1]
        week_ago = oos_daily.iloc[-8] if len(oos_daily) >= 8 else oos_daily.iloc[0]
        oos_delta = int(latest_oos["oos_stores"] - week_ago["oos_stores"])
        st.metric("Stores OOS today", f"{int(latest_oos['oos_stores']):,}",
                  delta=f"{oos_delta:+,} vs week ago", delta_color="inverse")
        st.metric("OOS rate today", f"{latest_oos['oos_pct']:.1f}%")
    # Chronic: store had on_hand=0 on >=7 days in the window
    days_oos_per_store = (
        df.groupby(["business_date", "store_number"])["store_on_hand_quantity_this_year"]
        .sum().reset_index()
    )
    days_oos_per_store = days_oos_per_store[days_oos_per_store["store_on_hand_quantity_this_year"] == 0]
    chronic_counts = days_oos_per_store.groupby("store_number").size()
    chronic = int((chronic_counts >= 7).sum())
    st.metric(f"Chronically OOS (≥7 days)", f"{chronic:,} stores")

with co2:
    if not oos_daily.empty:
        oos_chart = (
            alt.Chart(oos_daily)
            .mark_area(opacity=0.3, color="#791F1F")
            .encode(
                x=alt.X("business_date:T", title="Date"),
                y=alt.Y("oos_stores:Q", title="Stores with on-hand = 0"),
                tooltip=[alt.Tooltip("business_date:T", title="Date"),
                         alt.Tooltip("oos_stores:Q", format=",", title="OOS stores")],
            )
            .properties(height=280)
        ) + (
            alt.Chart(oos_daily)
            .mark_line(color="#791F1F", strokeWidth=2)
            .encode(x="business_date:T", y="oos_stores:Q")
        )
        st.altair_chart(oos_chart, use_container_width=True)


# ─── SECTION 8 — Store rankings ──────────────────────────────────────────────
st.subheader("Top & bottom store movers")

store_perf = df.groupby(
    ["store_number", "store_name", "city_name", "state_or_province_code"], as_index=False
).agg(
    units_ty=("pos_quantity_this_year", "sum"),
    units_ly=("pos_quantity_last_year", "sum"),
    sales_ty=("pos_sales_this_year", "sum"),
)
store_perf["yoy_units"] = store_perf["units_ty"] - store_perf["units_ly"]
store_perf["yoy_pct"] = np.where(
    store_perf["units_ly"] > 0,
    (store_perf["yoy_units"] / store_perf["units_ly"] * 100).round(1),
    np.nan,
)

s_left, s_right = st.columns(2)
with s_left:
    st.markdown("**Top 20 by units sold**")
    top = store_perf.nlargest(20, "units_ty")[
        ["store_number", "store_name", "state_or_province_code", "units_ty", "units_ly", "yoy_pct"]
    ].copy()
    top.columns = ["Store #", "Name", "State", "Units TY", "Units LY", "YoY %"]
    st.dataframe(top, use_container_width=True, hide_index=True,
                 column_config={"YoY %": st.column_config.NumberColumn(format="%.1f%%")})
with s_right:
    st.markdown("**Biggest decliners** (Bottom 20 by YoY)")
    declining = store_perf[store_perf["units_ly"] > 0].nsmallest(20, "yoy_units")[
        ["store_number", "store_name", "state_or_province_code", "units_ty", "units_ly", "yoy_units", "yoy_pct"]
    ].copy()
    declining.columns = ["Store #", "Name", "State", "Units TY", "Units LY", "YoY Δ", "YoY %"]
    st.dataframe(declining, use_container_width=True, hide_index=True,
                 column_config={"YoY %": st.column_config.NumberColumn(format="%.1f%%")})


# ─── SECTION 9 — Performance by state ────────────────────────────────────────
st.subheader("Performance by state (top 20)")

state_perf = df.groupby("state_or_province_code", as_index=False).agg(
    units_ty=("pos_quantity_this_year", "sum"),
    units_ly=("pos_quantity_last_year", "sum"),
    sales_ty=("pos_sales_this_year", "sum"),
    stores=("store_number", "nunique"),
)
state_perf["yoy_pct"] = np.where(
    state_perf["units_ly"] > 0,
    ((state_perf["units_ty"] - state_perf["units_ly"]) / state_perf["units_ly"] * 100).round(1),
    0,
)
state_perf["units_per_store"] = (state_perf["units_ty"] / state_perf["stores"]).round(1)
state_perf = state_perf.sort_values("units_ty", ascending=False).head(20)

st_chart = (
    alt.Chart(state_perf)
    .mark_bar()
    .encode(
        x=alt.X("units_ty:Q", title="Units TY"),
        y=alt.Y("state_or_province_code:N", sort="-x", title="State"),
        color=alt.Color("yoy_pct:Q",
                        scale=alt.Scale(scheme="redyellowgreen", domainMid=0),
                        legend=alt.Legend(title="YoY %")),
        tooltip=[
            alt.Tooltip("state_or_province_code:N", title="State"),
            alt.Tooltip("units_ty:Q", format=",", title="Units TY"),
            alt.Tooltip("units_ly:Q", format=",", title="Units LY"),
            alt.Tooltip("yoy_pct:Q", format=".1f", title="YoY %"),
            alt.Tooltip("stores:Q", title="Stores"),
        ],
    )
    .properties(height=420)
)
st.altair_chart(st_chart, use_container_width=True)


# ─── SECTION 10 — DC pipeline health ─────────────────────────────────────────
if not dc_df.empty:
    st.subheader("DC pipeline health")

    latest_dc_date = dc_df["inventory_date"].max()
    dc_latest = dc_df[dc_df["inventory_date"] == latest_dc_date].copy()

    total_units_per_day = df["pos_quantity_this_year"].sum() / max(1, lookback)
    dc_count = max(1, dc_latest["distribution_center_number"].nunique())
    daily_per_dc = total_units_per_day / dc_count

    dc_summary = dc_latest.groupby(
        ["distribution_center_number", "name_of_the_dc"], as_index=False
    ).agg(
        on_hand=("on_hand_warehouse_inventory_in_units_this_year", "sum"),
        on_order=("on_order_warehouse_quantity_in_units_this_year", "sum"),
        oos=("out_of_stock_each_quantity_this_year", "sum"),
    )
    dc_summary["total_supply"] = dc_summary["on_hand"] + dc_summary["on_order"]
    dc_summary["wos_oh"] = np.where(
        daily_per_dc > 0, (dc_summary["on_hand"] / (daily_per_dc * 7)).round(1), 0
    )
    dc_summary = dc_summary.sort_values("on_hand", ascending=False)

    dc_left, dc_right = st.columns([1, 2])
    with dc_left:
        st.metric("Network DC on-hand", f"{int(dc_summary['on_hand'].sum()):,}")
        st.metric("Network DC on-order", f"{int(dc_summary['on_order'].sum()):,}")
        critical_dcs = int((dc_summary["wos_oh"] <= 1).sum())
        st.metric("DCs ≤ 1 wk supply", f"{critical_dcs:,}", delta_color="inverse")
        st.caption(f"Snapshot: {latest_dc_date.strftime('%b %d, %Y')}")

    with dc_right:
        show_dc = dc_summary[
            ["distribution_center_number", "name_of_the_dc", "on_hand", "on_order", "total_supply", "wos_oh"]
        ].copy()
        show_dc.columns = ["DC #", "DC Name", "On Hand", "On Order", "Total Supply", "WOS (OH)"]
        st.dataframe(
            show_dc, use_container_width=True, hide_index=True, height=420,
            column_config={"WOS (OH)": st.column_config.NumberColumn(format="%.1f wks")},
        )


# ─── Footer ──────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"BigQuery rows: {len(df):,} store · {len(dc_df):,} DC · "
    f"Cache TTL: 24 hr · Sidebar refresh button for manual reload"
)
