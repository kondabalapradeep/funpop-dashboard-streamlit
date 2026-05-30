"""
streamlit_app.py — FunPop Sales Dashboard

Tab structure:
  Overview          — KPIs, last 10 days, item performance, stockout risk
  Sales & Velocity  — Weekly sales, U/S/W
  Forecast          — Upcoming demand forecast vs sales, attainment & bias, store
                      replenishment watchlist, and DC demand coverage
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
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from google.cloud import bigquery
from google.oauth2 import service_account

from constants import (
    ACTIVE_ITEMS,
    BIN_ITEMS,
    CASE_PACK_UNITS,
    ITEM_LABELS,
    SHELF_ITEMS,
    item_group_label,
)
import snapshot

# Walmart is in Bentonville (Central Time). Daily BI Link feeds typically
# land around 7am Central, sometimes later.
CENTRAL_TZ = ZoneInfo("America/Chicago")


@st.cache_resource
def _freshness_state() -> dict:
    """Tiny shared state: tracks which day's data has been confirmed-fresh.
    cache_resource persists across user sessions in the same app instance, so
    once any visitor confirms today's data is in, all subsequent visitors that
    day skip the 7:30/8am retry slots."""
    return {"confirmed_date": None}


def _refresh_slot() -> str:
    """Returns the active cache-key slot. Strategy:
    * Before 6am Central — hold yesterday's data
    * 6am–11pm Central — slot advances every hour, triggering a fresh BQ pull
      on first visit until data is confirmed fresh
    * Once confirmed fresh — slot sticks to "today_done" for the rest of the day"""
    now = datetime.now(CENTRAL_TZ)
    today_iso = now.date().isoformat()
    state = _freshness_state()

    # If we already confirmed today's data, hold steady — no more BQ pulls today.
    if state.get("confirmed_date") == today_iso:
        return f"{today_iso}_done"

    # Before 6am — yesterday's "final" data is what's current
    if now.hour < 6:
        return f"{(now - timedelta(days=1)).date().isoformat()}_final"

    # 6am onward — slot advances every hour. The hour itself is part of the key,
    # so each new hour invalidates the cache and triggers another fetch attempt.
    return f"{today_iso}_h{now.hour:02d}"


def _confirm_freshness_if_current(max_date) -> None:
    """Mark today as 'data is fresh' if the BI Link feed has caught up to its
    normal 1-day lag. Walmart's feed never includes same-day data; the most
    recent business_date should equal yesterday (Central). If max_date is
    older than yesterday, the overnight feed hasn't landed yet — keep the
    slot advancing through 7:30am and 8:00am retries."""
    if max_date is None:
        return
    now = datetime.now(CENTRAL_TZ)
    today = now.date()
    yesterday = today - timedelta(days=1)
    max_d = max_date.date() if hasattr(max_date, "date") else max_date
    # "Fresh" = the feed has advanced to include yesterday's data.
    # max_d == today would be unexpected (feed never has same-day), but we
    # accept it just in case to avoid pointless re-pulls.
    if max_d >= yesterday:
        _freshness_state()["confirmed_date"] = today.isoformat()


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

logger = logging.getLogger("funpop_dashboard")

# Set from session_state at top level each run (reading session_state inside a
# cached function is unreliable). When True, _cached_query skips the snapshot
# and pulls live — used by the sidebar "Refresh data" button.
_FORCE_LIVE_REFRESH = False


def _section_error(label: str, err: object) -> None:
    """Log the real error server-side and show viewers a generic note.
    The dashboard is public, so raw BigQuery errors (which embed the project,
    dataset, and table names) must never be rendered on the page."""
    logger.warning("%s unavailable: %s", label, err)
    st.warning(
        f"{label} is temporarily unavailable. "
        "Try the Refresh button in the sidebar, or check back shortly."
    )


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


def _cached_query(sql_filename: str, params=None) -> pd.DataFrame:
    """Fetch a query result, preferring the durable BigQuery snapshot written
    by snapshot_build.py (run on a schedule by GitHub Actions) over a live pull.

    Community Cloud drops in-memory @st.cache_data whenever the app process
    restarts, so without this a visitor arriving after a restart pays the full
    multi-query cost — that's why the page wasn't already loaded at 8am. The
    snapshot makes a cold load fast and survives restarts. Any snapshot
    miss/staleness/error falls through to a live query, so behaviour is never
    worse than a direct pull.

    The sidebar "Refresh data" button sets _force_live_refresh so it bypasses
    the snapshot and still confirms the very latest data."""
    params = params or []
    if not _FORCE_LIVE_REFRESH:
        try:
            df = snapshot.read_snapshot(snapshot.snapshot_key(sql_filename, params))
            if df is not None:
                return df
        except Exception as e:  # noqa: BLE001 - snapshot is best-effort
            logger.warning("snapshot lookup skipped for %s: %s", sql_filename, e)
    return _run_query(_load_sql(sql_filename), params)


# ─── Primary data loaders ────────────────────────────────────────────────────
# max_entries caps how many cached frames each loader keeps. The cache key
# includes refresh_slot (advances hourly until the feed lands) and the custom
# lookback slider, so without a cap @st.cache_data would retain a full copy of
# every slot/lookback combination for the whole 24h TTL — several hundred MB of
# stale frames that Community Cloud's RAM limit can't absorb. A small cap keeps
# the current view (plus a couple of recent ones) and evicts the rest.
@st.cache_data(ttl=86400, max_entries=3, show_spinner="Loading store data...")
def load_store_data(lookback_days: int, refresh_slot: str = "") -> pd.DataFrame:
    df = _cached_query("store_query.sql", [
        bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
    ])
    # Record the wall-clock time of this BQ fetch. Only executes on cache miss,
    # so this is the actual "last refresh" stamp.
    _freshness_state()["last_refresh_at"] = datetime.now(CENTRAL_TZ).isoformat()
    if df.empty:
        return df
    # Memory hygiene — this is the dashboard's heaviest frame (~4,500 stores ×
    # ~35 days × 3 items ≈ 550k rows) and Community Cloud caps app RAM, so an
    # un-trimmed frame (with @st.cache_data holding a copy per cache key) is what
    # pushes the app over the limit. Two cheap wins, both safe with older
    # snapshots thanks to the guards:
    #   • Drop columns the query fetches but the dashboard never reads
    #     (store_name / city_name / item_name). These are repeated Python strings
    #     across every row — together ~120 MB in memory for nothing.
    #   • Store the low-cardinality state code (~50 distinct) as a category
    #     instead of a repeated string — another ~30 MB.
    df = df.drop(columns=["store_name", "city_name", "item_name"], errors="ignore")
    if "state_or_province_code" in df.columns:
        df["state_or_province_code"] = df["state_or_province_code"].astype("category")
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


@st.cache_data(ttl=86400, max_entries=3, show_spinner="Loading DC data...")
def load_dc_data(lookback_days: int, refresh_slot: str = "") -> pd.DataFrame:
    try:
        df = _cached_query("dc_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
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
        # Convert warehouse packs → eaches for on_hand and on_order (OOS already
        # eaches). Prefer the real per-row eaches-per-pack from BigQuery; fall back
        # to the known constant only where the feed is missing or zero.
        if "warehouse_pack_each_quantity" in df.columns:
            pack_each = pd.to_numeric(df["warehouse_pack_each_quantity"], errors="coerce").fillna(0)
            fallback = df["walmart_item_number"].map(CASE_PACK_UNITS).fillna(1)
            multiplier = pack_each.where(pack_each > 0, fallback)
        else:
            multiplier = df["walmart_item_number"].map(CASE_PACK_UNITS).fillna(1)
        for c in [
            "on_hand_warehouse_inventory_in_units_this_year",
            "on_hand_warehouse_inventory_in_units_last_year",
            "on_order_warehouse_quantity_in_units_this_year",
            "on_order_warehouse_quantity_in_units_last_year",
        ]:
            df[c] = (df[c] * multiplier).round().astype("int64")
        return df
    except Exception as e:
        _section_error("DC data", e)
        return pd.DataFrame()


# ─── Secondary loaders (fault-tolerant) ──────────────────────────────────────
@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_dc_alignment(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("dc_alignment_query.sql")
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_forecast_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("forecast_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["forecast_date"] = pd.to_datetime(df["forecast_date"])
            df["forecast_quantity"] = pd.to_numeric(df["forecast_quantity"], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_omni_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("omni_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["business_date"] = pd.to_datetime(df["business_date"])
            for c in ["units_ty", "units_ly", "sales_ty", "sales_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_ecom_inv_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("ecom_inv_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["report_date"] = pd.to_datetime(df["report_date"])
            for c in ["on_hand_units", "available_to_sell", "on_hand_units_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_returns_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("returns_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["return_date"] = pd.to_datetime(df["return_date"])
            for c in ["returns_ty", "returns_ly", "return_sales_ty", "return_sales_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_modular_data(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("modular_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
        ])
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_backroom_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("backroom_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["adjustment_date"] = pd.to_datetime(df["adjustment_date"])
            for c in ["adjustment_qty_ty", "adjustment_qty_ly"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


def _walmart_week_start(dates: pd.Series) -> pd.Series:
    """Start date (the Saturday) of the Walmart fiscal week for each date.
    Walmart weeks run Saturday-Friday; pandas weekday() has Saturday=5. Used so
    forecast and returns bucket into the same weeks as the rest of the dashboard,
    which groups on walmart_calendar_week (also Saturday-Friday)."""
    days_since_sat = (dates.dt.weekday - 5) % 7
    return dates - pd.to_timedelta(days_since_sat, unit="D")


def _yoy_pct(ty: pd.Series, ly: pd.Series) -> np.ndarray:
    """Year-over-year percent change, rounded to 1 dp. Returns 0 where last
    year is 0 (no meaningful base), and avoids divide-by-zero warnings."""
    ly_safe = ly.replace(0, np.nan)
    return np.where(ly > 0, ((ty - ly) / ly_safe * 100).round(1), 0)


def _week_norm_factor(days_in_week: pd.Series) -> pd.Series:
    """Scaling factor that extrapolates a partial Walmart week's totals to a
    full 7-day equivalent, so in-progress weeks compare fairly to complete ones."""
    return 7 / days_in_week.clip(lower=1)


def _wm_week_label(weeks: pd.Series, is_partial=None):
    """'WM Wk NN' label from a walmart_calendar_week column. When is_partial is
    given, in-progress weeks get a ' *' suffix."""
    base = "WM Wk " + weeks.astype(str).str[-2:]
    if is_partial is None:
        return base
    return np.where(np.asarray(is_partial), base + " *", base)


# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Controls")
    if st.button("🔄 Refresh data", help="Clear cache, re-pull from BigQuery"):
        st.cache_data.clear()
        # Bypass the snapshot on the next run so Refresh confirms the very
        # latest data with a live pull (the flag is cleared at end of script).
        st.session_state["_force_live_refresh"] = True
        # Reset the freshness state so the manual refresh actually re-fetches
        try:
            _freshness_state()["confirmed_date"] = None
        except Exception:
            pass
        st.rerun()
    st.caption("Auto-refreshes every hour starting at 6am Central until new data arrives. Click for an immediate refresh.")

    st.divider()
    st.subheader("Filters")

    item_view = st.radio(
        "View",
        options=["Total (all items)", "Both Bins (full + half)", "Shelf only"],
        index=0,
    )
    if item_view == "Total (all items)":
        item_filter = list(ACTIVE_ITEMS)
    elif item_view == "Both Bins (full + half)":
        item_filter = list(BIN_ITEMS)
    else:
        item_filter = list(SHELF_ITEMS)

    view_mode = st.radio(
        "Date range",
        options=["Standard (4 full weeks + current)", "Custom"],
        index=0,
        help="Standard = last 4 completed Walmart weeks (Sat-Fri) plus the in-progress week.",
    )

    if view_mode == "Standard (4 full weeks + current)":
        # Walmart fiscal week = Saturday-Friday.
        # weekday(): Monday=0..Sunday=6, so Saturday=5.
        # Days since the most recent Saturday (start of current WM week):
        _today_central = datetime.now(CENTRAL_TZ).date()
        _days_since_sat = (_today_central.weekday() - 5) % 7  # 0 on a Saturday
        _days_into_current = _days_since_sat + 1               # +1 to include today
        # Window must start on the Saturday 4 weeks before the current WM week.
        # The BQ filter is `bus_dt >= today - lookback`, so:
        #   lookback = 27 + days_into_current
        #            = 28 full-week days + (days_into_current - 1) elapsed days.
        # (Using 28 + days_into_current pulled one extra day, creating a stray
        #  1-day partial week at the left edge of the weekly charts.)
        lookback = 27 + _days_into_current
        st.caption(
            f"📅 Showing **4 full Walmart weeks + {_days_into_current} day(s)** "
            f"into the current week ({lookback + 1} calendar days)"
        )
    else:
        lookback = st.slider(
            "Lookback (days)",
            min_value=14, max_value=120, value=30, step=7,
            help="Custom lookback — affects all trend charts.",
        )

    st.divider()
    perf_window = st.radio(
        "Performance window",
        options=["Most recent day", "Last 7 days", "Full lookback"],
        index=0,
        help="Filters KPIs and Item Performance only.",
    )


# ─── Primary data load ───────────────────────────────────────────────────────
# Read the force-live flag at top level (safe) so the cached loaders' helper
# can consult it without touching session_state from inside a cached function.
_FORCE_LIVE_REFRESH = bool(st.session_state.get("_force_live_refresh"))

# Time-aware cache key. Changes at 7am/7:30am/8am Central → auto-refresh.
slot = _refresh_slot()

df_all = load_store_data(lookback_days=lookback, refresh_slot=slot)
dc_df_all = load_dc_data(lookback_days=lookback, refresh_slot=slot)

# After the primary data lands, mark today as "confirmed fresh" if the data
# actually advanced. This stops the 7:30 and 8am retries from running once
# the 7am pull has succeeded.
if not df_all.empty:
    _confirm_freshness_if_current(df_all["business_date"].max())

if df_all.empty:
    st.error(
        f"No store data for the last {lookback} days. "
        f"Items {list(ACTIVE_ITEMS)} may have no sales in this window."
    )
    st.stop()

# Apply item filter
df = df_all[df_all["walmart_item_number"].isin(item_filter)].copy()
# Coarse display group (Bins = Full + Half) for the per-item breakouts that
# shouldn't split the two bin packs. Carried into df_window via its slices.
df["item_group"] = df["walmart_item_number"].map(item_group_label)
dc_df = dc_df_all[dc_df_all["walmart_item_number"].isin(item_filter)].copy() if not dc_df_all.empty else dc_df_all

if df.empty:
    st.warning("No data matches the current item filter.")
    st.stop()

most_recent = df["business_date"].max()
period_days = max(1, df["business_date"].dt.normalize().nunique())
weeks_in_period = max(1 / 7, period_days / 7)

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
    window_label = f"Full lookback ({df['business_date'].min().strftime('%b %d')}–{most_recent.strftime('%b %d')}, {period_days} data days)"
    weeks_in_window = weeks_in_period


# ─── Header ──────────────────────────────────────────────────────────────────
st.title("FunPop Sales Dashboard")
now_central = datetime.now(CENTRAL_TZ)
expected_max = (now_central - timedelta(days=1)).date()
actual_max = most_recent.date() if hasattr(most_recent, "date") else most_recent
days_behind = (expected_max - actual_max).days
freshness_note = ""
if days_behind == 0:
    freshness_note = " ✓ current"
elif days_behind == 1:
    freshness_note = " ⚠️ 1 day behind"
elif days_behind > 1:
    freshness_note = f" ⚠️ {days_behind} days behind"

last_refresh_iso = _freshness_state().get("last_refresh_at")
if last_refresh_iso:
    try:
        last_refresh_dt = datetime.fromisoformat(last_refresh_iso)
        # Convert to Central if needed; isoformat preserves tz
        if last_refresh_dt.tzinfo is None:
            last_refresh_dt = last_refresh_dt.replace(tzinfo=CENTRAL_TZ)
        last_refresh_str = last_refresh_dt.astimezone(CENTRAL_TZ).strftime("%b %d, %Y at %I:%M %p %Z")
    except Exception:
        last_refresh_str = "unknown"
else:
    last_refresh_str = "not yet (cached)"

st.caption(
    f"**Last refreshed:** {last_refresh_str}  ·  "
    f"**Data range:** {df['business_date'].min().strftime('%b %d, %Y')} – "
    f"{most_recent.strftime('%b %d, %Y')} ({period_days} data days){freshness_note}  ·  "
    f"{df['store_number'].nunique():,} stores  ·  {item_view}"
)
st.caption(
    f"Note: Walmart's BI Link feed runs on a 1-day lag — today's most recent "
    f"data point is **{most_recent.strftime('%b %d')}** (yesterday Central). "
    f"Auto-refresh runs hourly from 6am Central until new data lands."
)


# ─── Tabs ────────────────────────────────────────────────────────────────────
tab_overview, tab_sales, tab_forecast, tab_inv, tab_channels, tab_dist = st.tabs([
    "Overview",
    "Sales & Velocity",
    "Forecast",
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
    k1.metric("Units sold", f"{units_ty:,}", f"{units_yoy:+,} ({units_yoy_pct:+.1f}%) YoY")
    k2.metric("Sales", f"${sales_ty:,.0f}", f"{sales_yoy_pct:+.1f}% YoY")
    k3.metric("Units LY", f"{units_ly:,}", help="Units sold in the same period last year")
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
    daily["yoy_pct"] = _yoy_pct(daily["units_ty"], daily["units_ly"])
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
        st.altair_chart(chart, width='stretch')
    with c_r:
        show = daily[["weekday", "date_str", "units_ty", "units_ly", "yoy_units",
                      "yoy_pct", "stores_selling"]].iloc[::-1].copy()
        show["stores_selling"] = show["stores_selling"].astype(int)
        show[["units_ly", "yoy_units"]] = show[["units_ly", "yoy_units"]].astype(int)
        show.columns = ["Day", "Date", "Units TY", "Units LY", "YoY Units", "YoY %", "Stores Selling"]
        st.dataframe(show, width='stretch', hide_index=True, height=380,
                     column_config={"YoY %": st.column_config.NumberColumn(format="%.1f%%")})

    # ── Item performance ─────────────────────────────────────────────────────
    # Full + Half bins are rolled into one "Bins" line; the half/full split
    # isn't actionable here. Shelf stays on its own.
    st.subheader(f"Item performance — {window_label}")

    # On-hand uses each item's own latest snapshot date, then rolls up to the
    # display group (Bins = Full on-hand + Half on-hand).
    latest_per_item = df.groupby("walmart_item_number")["business_date"].max().to_dict()
    group_oh = {}
    group_items = {}
    for item, item_max in latest_per_item.items():
        snap = df[(df["walmart_item_number"] == item) & (df["business_date"] == item_max)]
        g = item_group_label(item)
        group_oh[g] = group_oh.get(g, 0) + int(snap["store_on_hand_quantity_this_year"].sum())
        group_items.setdefault(g, []).append(int(item))

    item_perf = df_window.groupby("item_group", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        sales_ly=("pos_sales_last_year", "sum"),
    ).rename(columns={"item_group": "item"})
    item_perf["sales_yoy_pct"] = _yoy_pct(item_perf["sales_ty"], item_perf["sales_ly"])
    item_perf["on_hand"] = item_perf["item"].map(group_oh).fillna(0).astype(int)
    item_perf["yoy_pct"] = _yoy_pct(item_perf["units_ty"], item_perf["units_ly"])
    item_perf["yoy_units"] = item_perf["units_ty"] - item_perf["units_ly"]
    full_units_per_group = df.groupby("item_group")["pos_quantity_this_year"].sum()
    item_perf["wos_units_ty"] = item_perf["item"].map(full_units_per_group).fillna(0)
    item_perf["wos"] = np.where(item_perf["wos_units_ty"] > 0,
        (item_perf["on_hand"] / (item_perf["wos_units_ty"] / weeks_in_period)).round(1), np.inf)

    if len(item_perf) > 0:
        ip_cols = st.columns(len(item_perf))
        for col, (_, row) in zip(ip_cols, item_perf.iterrows()):
            with col:
                st.markdown(f"### {row['item']}")
                nums = sorted(group_items.get(row["item"], []))
                st.caption(("Items " if len(nums) > 1 else "Item ") + ", ".join(str(n) for n in nums))
                st.metric("Units sold", f"{int(row['units_ty']):,}",
                          f"{int(row['yoy_units']):+,} ({row['yoy_pct']:+.1f}%) YoY")
                st.metric("Units LY", f"{int(row['units_ly']):,}",
                          help="Units sold in the same period last year")
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
            # Compare against the most recent day on or before 7 calendar days
            # ago — robust to missing feed days (positional iloc[-8] was not).
            week_ago_target = latest_oos["business_date"] - pd.Timedelta(days=7)
            prior = oos_daily[oos_daily["business_date"] <= week_ago_target]
            week_ago = prior.iloc[-1] if not prior.empty else oos_daily.iloc[0]
            oos_delta = int(latest_oos["oos_stores"] - week_ago["oos_stores"])
            st.metric("Stores OOS today", f"{int(latest_oos['oos_stores']):,}",
                      delta=f"{oos_delta:+,} vs week ago", delta_color="inverse")
            st.metric("OOS rate today", f"{latest_oos['oos_pct']:.1f}%")
        # Chronic = OOS on 7+ distinct days in the window (reuses store_day_oh).
        chronic = int((store_day_oh[store_day_oh["is_oos"] == 1]
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
            st.altair_chart(chart, width='stretch')


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 2 — SALES & VELOCITY
# ═══════════════════════════════════════════════════════════════════════════
with tab_sales:
    # ── Weekly sales trend ───────────────────────────────────────────────────
    st.subheader("Weekly sales trend")

    weekly_sales = df.groupby("walmart_calendar_week", as_index=False).agg(
        units_ty_raw=("pos_quantity_this_year", "sum"),
        units_ly_raw=("pos_quantity_last_year", "sum"),
        sales_ty_raw=("pos_sales_this_year", "sum"),
        sales_ly_raw=("pos_sales_last_year", "sum"),
        days_in_week=("business_date", "nunique"),
        week_start=("business_date", "min"),
    ).sort_values("walmart_calendar_week").reset_index(drop=True)
    # Normalize each week to a 7-day equivalent based on days of data present.
    # This keeps the current (in-progress) week comparable to completed weeks.
    norm = _week_norm_factor(weekly_sales["days_in_week"])
    weekly_sales["units_ty"] = (weekly_sales["units_ty_raw"] * norm).round().astype(int)
    weekly_sales["units_ly"] = (weekly_sales["units_ly_raw"] * norm).round().astype(int)
    weekly_sales["sales_ty"] = (weekly_sales["sales_ty_raw"] * norm).round(2)
    weekly_sales["sales_ly"] = (weekly_sales["sales_ly_raw"] * norm).round(2)
    weekly_sales["yoy_units"] = weekly_sales["units_ty"] - weekly_sales["units_ly"]
    weekly_sales["yoy_pct"] = _yoy_pct(weekly_sales["units_ty"], weekly_sales["units_ly"])
    # Flag in-progress weeks
    weekly_sales["is_partial"] = weekly_sales["days_in_week"] < 7
    weekly_sales["week_label"] = _wm_week_label(
        weekly_sales["walmart_calendar_week"], weekly_sales["is_partial"]
    )

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
        ).properties(height=320)), width='stretch')

        if weekly_sales["is_partial"].any():
            partial_wk = weekly_sales[weekly_sales["is_partial"]].iloc[-1]
            st.caption(
                f"Note: week marked with `*` ({partial_wk['week_label']}) is in-progress "
                f"({int(partial_wk['days_in_week'])}/7 days). Values shown are extrapolated "
                f"to a full-week equivalent for fair comparison."
            )
        show = weekly_sales[["week_label", "units_ty", "units_ly", "yoy_units", "yoy_pct", "sales_ty"]].copy()
        show.columns = ["Week", "Units TY (7d eq)", "Units LY (7d eq)", "YoY Units", "YoY %", "Sales TY ($)"]
        show = show.tail(8).iloc[::-1]
        st.dataframe(show, width='stretch', hide_index=True,
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

    # Build weekly U/S/W with plain agg (pandas-3-safe; avoids groupby.apply)
    weekly_uspw = df.groupby("walmart_calendar_week", as_index=False).agg(
        week_start=("business_date", "min"),
        units_ty_raw=("pos_quantity_this_year", "sum"),
        units_ly_raw=("pos_quantity_last_year", "sum"),
        days_in_week=("business_date", "nunique"),
    )
    # Active stores per week (count of distinct stores across the week, not affected by partial-week)
    ty_active = (df[df["pos_quantity_this_year"] > 0]
                 .groupby("walmart_calendar_week")["store_number"].nunique()
                 .rename("stores_ty").reset_index())
    ly_active = (df[df["pos_quantity_last_year"] > 0]
                 .groupby("walmart_calendar_week")["store_number"].nunique()
                 .rename("stores_ly").reset_index())
    weekly_uspw = weekly_uspw.merge(ty_active, on="walmart_calendar_week", how="left")
    weekly_uspw = weekly_uspw.merge(ly_active, on="walmart_calendar_week", how="left")
    weekly_uspw["stores_ty"] = weekly_uspw["stores_ty"].fillna(0).astype(int)
    weekly_uspw["stores_ly"] = weekly_uspw["stores_ly"].fillna(0).astype(int)
    # Normalize partial-week units to 7-day equivalent for fair YoY comparison
    norm = _week_norm_factor(weekly_uspw["days_in_week"])
    weekly_uspw["units_ty"] = weekly_uspw["units_ty_raw"] * norm
    weekly_uspw["units_ly"] = weekly_uspw["units_ly_raw"] * norm
    weekly_uspw["uspw_ty"] = np.where(weekly_uspw["stores_ty"] > 0,
        (weekly_uspw["units_ty"] / weekly_uspw["stores_ty"]).round(2), 0)
    weekly_uspw["uspw_ly"] = np.where(weekly_uspw["stores_ly"] > 0,
        (weekly_uspw["units_ly"] / weekly_uspw["stores_ly"]).round(2), 0)
    weekly_uspw["is_partial"] = weekly_uspw["days_in_week"] < 7
    weekly_uspw["week_label"] = _wm_week_label(
        weekly_uspw["walmart_calendar_week"], weekly_uspw["is_partial"]
    )
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
        ).properties(height=300)), width='stretch')

    # ── U/S/W by item ────────────────────────────────────────────────────────
    # Full + Half bins are combined into a single "Bins" line; Shelf stays
    # separate. Active stores for "Bins" counts distinct stores that moved
    # either pack (not the sum of the per-pack store counts, which would
    # double-count stores selling both).
    st.markdown("**U/S/W by item (period total)**")
    item_uspw = df.groupby("item_group", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
    ).rename(columns={"item_group": "item"})
    ty_active_item = (df[df["pos_quantity_this_year"] > 0]
                      .groupby("item_group")["store_number"].nunique()
                      .rename("stores_ty").reset_index().rename(columns={"item_group": "item"}))
    ly_active_item = (df[df["pos_quantity_last_year"] > 0]
                      .groupby("item_group")["store_number"].nunique()
                      .rename("stores_ly").reset_index().rename(columns={"item_group": "item"}))
    item_uspw = item_uspw.merge(ty_active_item, on="item", how="left")
    item_uspw = item_uspw.merge(ly_active_item, on="item", how="left")
    item_uspw["stores_ty"] = item_uspw["stores_ty"].fillna(0).astype(int)
    item_uspw["stores_ly"] = item_uspw["stores_ly"].fillna(0).astype(int)
    item_uspw["uspw_ty"] = np.where(item_uspw["stores_ty"] > 0,
        (item_uspw["units_ty"] / item_uspw["stores_ty"] / weeks_in_period).round(2), 0)
    item_uspw["uspw_ly"] = np.where(item_uspw["stores_ly"] > 0,
        (item_uspw["units_ly"] / item_uspw["stores_ly"] / weeks_in_period).round(2), 0)
    item_uspw["uspw_yoy_pct"] = _yoy_pct(item_uspw["uspw_ty"], item_uspw["uspw_ly"])

    if len(item_uspw) > 0:
        iu_cols = st.columns(len(item_uspw))
        for col, (_, row) in zip(iu_cols, item_uspw.iterrows()):
            with col:
                yoy_pct = row["uspw_yoy_pct"]
                uspw_yoy = row["uspw_ty"] - row["uspw_ly"]
                st.metric(f"{row['item']}", f"{row['uspw_ty']:.2f} U/S/W",
                          delta=f"{uspw_yoy:+.2f} ({yoy_pct:+.1f}%) vs LY {row['uspw_ly']:.2f}")

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
    ).properties(height=300)), width='stretch')


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 3 — FORECAST
# ═══════════════════════════════════════════════════════════════════════════
with tab_forecast:
    st.caption(
        "Walmart's daily demand forecast for your items — what's coming, how well actual "
        "sales have tracked it, and whether store shelves and DCs are positioned to cover it."
    )

    fcst_df, fcst_err = load_forecast_data(lookback, slot)
    if fcst_err:
        _section_error("Forecast data", fcst_err)
    elif fcst_df.empty:
        st.info("No forecast records returned for the selected items and lookback window.")
    else:
        fc = fcst_df[fcst_df["walmart_item_number"].isin(item_filter)].copy()
        # Actuals lag ~1 day, so "upcoming" = forecast dated after the latest actual.
        future = fc[fc["forecast_date"] > most_recent].copy()
        horizon_days = int((future["forecast_date"].max() - most_recent).days) if not future.empty else 0

        # Recent run-rate from actuals (last 7 data days) — the baseline we compare against.
        recent_cut = most_recent - timedelta(days=6)
        recent_actual = df[df["business_date"] >= recent_cut]
        recent_days = max(1, recent_actual["business_date"].dt.normalize().nunique())
        run_rate_day = float(recent_actual["pos_quantity_this_year"].sum() / recent_days)

        # ── A · Upcoming demand outlook ─────────────────────────────────────
        st.subheader("Upcoming demand outlook")
        if future.empty:
            st.info(
                "The forecast feed has no forward-dated rows right now, so there's no upcoming "
                "outlook to show. Attainment vs past forecast is below."
            )
        else:
            def _fcst_window(days):
                end = most_recent + pd.Timedelta(days=days)
                return float(future[future["forecast_date"] <= end]["forecast_quantity"].sum())

            next7, next14 = _fcst_window(7), _fcst_window(14)
            rr7 = run_rate_day * 7
            d7 = ((next7 - rr7) / rr7 * 100) if rr7 else 0.0
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Next 7 days forecast", f"{next7:,.0f}", f"{d7:+.1f}% vs run-rate")
            k2.metric("Next 14 days forecast", f"{next14:,.0f}")
            k3.metric("Recent run-rate", f"{run_rate_day:,.0f}/day",
                      help=f"Avg actual units/day over the last {recent_days} data days")
            k4.metric("Forecast horizon", f"{horizon_days} days",
                      help="How far the forecast feed currently extends past the latest actual")
            trend = "above" if d7 > 2 else ("below" if d7 < -2 else "in line with")
            tail = ("Demand is expected to **rise** — make sure stores and DCs are positioned for it."
                    if d7 > 2 else
                    ("Demand is expected to **soften** — watch for overstock and ease replenishment."
                     if d7 < -2 else "Demand looks **steady** versus recent weeks."))
            st.markdown(
                f"Walmart forecasts **{next7:,.0f} units over the next 7 days** — **{d7:+.1f}%** "
                f"{trend} your recent run-rate of {rr7:,.0f}/week. {tail}"
            )

        # ── B · Forecast vs actual timeline ─────────────────────────────────
        st.divider()
        st.subheader("Forecast vs actual")
        st.caption(
            "Bars = actual units sold (through the latest feed day). Line = Walmart's forecast, "
            "continuing past the dashed marker into the upcoming window."
        )
        actual_daily = df.groupby("business_date", as_index=False)["pos_quantity_this_year"].sum().rename(
            columns={"business_date": "date", "pos_quantity_this_year": "actual"})
        fc_daily = fc.groupby("forecast_date", as_index=False)["forecast_quantity"].sum().rename(
            columns={"forecast_date": "date", "forecast_quantity": "forecast"})
        timeline = fc_daily.merge(actual_daily, on="date", how="outer").sort_values("date")
        timeline = timeline[timeline["date"] >= most_recent - pd.Timedelta(days=21)]
        tl_base = alt.Chart(timeline)
        tl_bars = tl_base.mark_bar(opacity=0.5, color="#185FA5").encode(
            x=alt.X("date:T", title="Date"),
            y=alt.Y("actual:Q", title="Units"),
            tooltip=[alt.Tooltip("date:T", title="Date"),
                     alt.Tooltip("actual:Q", format=",.0f", title="Actual"),
                     alt.Tooltip("forecast:Q", format=",.0f", title="Forecast")])
        tl_line = tl_base.mark_line(color="#BA7517", strokeWidth=2.5, point=True).encode(
            x="date:T", y=alt.Y("forecast:Q"))
        tl_rule = alt.Chart(pd.DataFrame({"d": [most_recent]})).mark_rule(
            color="#A0A09A", strokeDash=[5, 4]).encode(x="d:T")
        st.altair_chart((tl_bars + tl_line + tl_rule).properties(height=340), width='stretch')

        # ── C · Forecast attainment & bias (past) ───────────────────────────
        st.divider()
        st.subheader("Forecast attainment & bias")
        st.caption(
            "How actual sales tracked the forecast on past dates. Below 100% = selling under "
            "forecast (supply/distribution gap or soft demand); above = beating it (forecast set low)."
        )
        past_fc = fc[fc["forecast_date"] <= most_recent]
        actuals = df.groupby(["business_date", "walmart_item_number"], as_index=False)[
            "pos_quantity_this_year"].sum().rename(columns={
                "business_date": "forecast_date", "pos_quantity_this_year": "actual_quantity"})
        attn = past_fc.groupby(["forecast_date", "walmart_item_number"], as_index=False)[
            "forecast_quantity"].sum().merge(actuals, on=["forecast_date", "walmart_item_number"], how="left")
        attn["actual_quantity"] = attn["actual_quantity"].fillna(0)
        if attn.empty:
            st.info("No overlapping forecast and actual dates to measure attainment.")
        else:
            wk = attn.copy()
            wk["week_start"] = _walmart_week_start(wk["forecast_date"])
            wkly = wk.groupby("week_start", as_index=False).agg(
                forecast=("forecast_quantity", "sum"), actual=("actual_quantity", "sum")).sort_values("week_start")
            wkly["attainment_pct"] = np.where(wkly["forecast"] > 0,
                                              (wkly["actual"] / wkly["forecast"] * 100).round(1), 0)
            wkly["week_label"] = wkly["week_start"].dt.strftime("Wk %b %d")
            tot_f, tot_a = float(wkly["forecast"].sum()), float(wkly["actual"].sum())
            bias = ((tot_a - tot_f) / tot_f * 100) if tot_f else 0.0

            at_l, at_r = st.columns([3, 2])
            with at_l:
                m = wkly.melt(id_vars=["week_label"], value_vars=["forecast", "actual"],
                              var_name="Series", value_name="Units")
                m["Series"] = m["Series"].map({"forecast": "Forecast", "actual": "Actual"})
                st.altair_chart((alt.Chart(m).mark_bar().encode(
                    x=alt.X("week_label:N", sort=list(wkly["week_label"]), title="Week",
                            axis=alt.Axis(labelAngle=-30)),
                    y=alt.Y("Units:Q", title="Units"),
                    color=alt.Color("Series:N", scale=alt.Scale(domain=["Forecast", "Actual"],
                                    range=["#A0A09A", "#185FA5"])),
                    xOffset="Series:N",
                    tooltip=["week_label", "Series", alt.Tooltip("Units:Q", format=",")],
                ).properties(height=300)), width='stretch')
            with at_r:
                st.metric("Period attainment", f"{(tot_a / tot_f * 100) if tot_f else 0:.0f}%",
                          f"{bias:+.0f}% vs forecast")
                item_at = attn.groupby("walmart_item_number", as_index=False).agg(
                    forecast=("forecast_quantity", "sum"), actual=("actual_quantity", "sum"))
                item_at["Item"] = item_at["walmart_item_number"].map(ITEM_LABELS).fillna(
                    item_at["walmart_item_number"].astype(str))
                item_at["Attainment %"] = np.where(item_at["forecast"] > 0,
                                                   (item_at["actual"] / item_at["forecast"] * 100).round(0), 0)
                st.dataframe(item_at[["Item", "Attainment %"]], width='stretch', hide_index=True,
                             column_config={"Attainment %": st.column_config.NumberColumn(format="%.0f%%")})
            verdict = ("running **under** forecast — likely availability/supply or softer demand" if bias < -5
                       else ("**beating** forecast — the forecast may be set conservatively" if bias > 5
                             else "tracking forecast closely"))
            st.caption(f"Net, you're {verdict} ({bias:+.0f}% vs forecast over the window).")

        # ── D · Store replenishment watchlist ───────────────────────────────
        if not future.empty:
            st.divider()
            st.subheader("Store replenishment watchlist")
            st.caption(
                "Upcoming forecast vs each store's latest on-hand and recent sell-through — surfacing "
                "stores most likely to stock out (or sit overstocked) against the forecast."
            )
            end7 = most_recent + pd.Timedelta(days=7)
            store_fc = future[future["forecast_date"] <= end7].groupby(
                "store_number", as_index=False)["forecast_quantity"].sum().rename(
                    columns={"forecast_quantity": "fcst_7d"})
            store_recent = recent_actual.groupby("store_number", as_index=False)["pos_quantity_this_year"].sum()
            store_recent["recent_day"] = store_recent["pos_quantity_this_year"] / recent_days
            latest_mask = df.groupby("store_number")["business_date"].transform("max") == df["business_date"]
            latest_oh = df[latest_mask].groupby("store_number", as_index=False)[
                "store_on_hand_quantity_this_year"].sum().rename(
                    columns={"store_on_hand_quantity_this_year": "on_hand"})
            watch = (store_fc
                     .merge(store_recent[["store_number", "recent_day"]], on="store_number", how="left")
                     .merge(latest_oh, on="store_number", how="left"))
            watch["recent_day"] = watch["recent_day"].fillna(0.0)
            watch["on_hand"] = watch["on_hand"].fillna(0.0)
            watch["fcst_day"] = watch["fcst_7d"] / 7
            watch["days_cover"] = np.where(watch["fcst_day"] > 0, watch["on_hand"] / watch["fcst_day"], np.inf)
            watch["surge_x"] = np.where(watch["recent_day"] > 0, watch["fcst_day"] / watch["recent_day"], np.nan)

            def _flag(r):
                if r["fcst_day"] > 0 and r["days_cover"] < 7:
                    return "Stockout risk"
                if pd.notna(r["surge_x"]) and r["surge_x"] >= 1.5 and r["fcst_7d"] >= 5:
                    return "Demand surge"
                if r["fcst_7d"] > 0 and r["on_hand"] > r["fcst_7d"] * 4:
                    return "Overstocked"
                return "OK"

            watch["flag"] = watch.apply(_flag, axis=1)
            risk_n = int((watch["flag"] == "Stockout risk").sum())
            surge_n = int((watch["flag"] == "Demand surge").sum())
            w1, w2, w3 = st.columns(3)
            w1.metric("Stores at stockout risk", f"{risk_n:,}", delta_color="inverse",
                      help="Less than 1 week of on-hand vs the next-7-day forecast")
            w2.metric("Stores with demand surge", f"{surge_n:,}",
                      help="Next-7-day forecast ≥ 1.5× recent sell-through")
            w3.metric("Upcoming 7-day forecast", f"{watch['fcst_7d'].sum():,.0f} units")
            order_map = {"Stockout risk": 0, "Demand surge": 1, "Overstocked": 2, "OK": 3}
            watch["_o"] = watch["flag"].map(order_map)
            show_w = watch.sort_values(["_o", "fcst_7d"], ascending=[True, False]).head(25)
            show_w = show_w[["store_number", "flag", "fcst_7d", "recent_day", "on_hand", "days_cover"]].copy()
            show_w["days_cover"] = show_w["days_cover"].replace(np.inf, np.nan)
            show_w.columns = ["Store", "Flag", "Forecast 7d", "Recent units/day", "On hand", "Days cover"]
            st.dataframe(show_w, width='stretch', hide_index=True, height=380, column_config={
                "Forecast 7d": st.column_config.NumberColumn(format="%.0f"),
                "Recent units/day": st.column_config.NumberColumn(format="%.1f"),
                "On hand": st.column_config.NumberColumn(format="%.0f"),
                "Days cover": st.column_config.NumberColumn(format="%.1f"),
            })
            st.caption("Showing the 25 highest-priority stores (stockout risk first, then surge, by forecast size).")

        # ── E · DC demand coverage ──────────────────────────────────────────
        st.divider()
        st.subheader("DC demand coverage")
        if dc_df.empty:
            st.info("DC data unavailable for the current filter.")
        elif future.empty:
            st.info("No forward-dated forecast to roll up to DCs.")
        else:
            st.caption(
                "Upcoming store demand forecast rolled up to each DC (via store→DC alignment) versus "
                "that DC's on-hand + on-order supply (in eaches)."
            )
            align_df, align_err = load_dc_alignment(slot)
            latest_dc_date = dc_df["inventory_date"].max()
            dc_latest = dc_df[dc_df["inventory_date"] == latest_dc_date].copy()
            # load_dc_data already converts warehouse packs → eaches, so aggregate directly.
            dc_supply = dc_latest.groupby(
                ["distribution_center_number", "name_of_the_dc"], as_index=False).agg(
                    on_hand=("on_hand_warehouse_inventory_in_units_this_year", "sum"),
                    on_order=("on_order_warehouse_quantity_in_units_this_year", "sum"))
            dc_supply["total_supply"] = dc_supply["on_hand"] + dc_supply["on_order"]

            end14 = most_recent + pd.Timedelta(days=14)
            store_fc14 = future[future["forecast_date"] <= end14].groupby(
                "store_number", as_index=False)["forecast_quantity"].sum()
            if align_err or align_df.empty:
                st.caption("⚠ Store→DC alignment unavailable — forecast allocated to DCs proportionally to on-hand.")
                total_fc14 = float(store_fc14["forecast_quantity"].sum())
                net_oh = max(1.0, float(dc_supply["on_hand"].sum()))
                dc_supply["fcst_14d"] = dc_supply["on_hand"] / net_oh * total_fc14
            else:
                primary = (align_df
                           .sort_values(["store_number", "alignment_type", "distribution_center_number"])
                           .drop_duplicates("store_number", keep="first"))
                sd = store_fc14.merge(primary, on="store_number", how="left")
                dcd = sd.groupby("distribution_center_number", as_index=False)["forecast_quantity"].sum().rename(
                    columns={"forecast_quantity": "fcst_14d"})
                dc_supply = dc_supply.merge(dcd, on="distribution_center_number", how="left")
                dc_supply["fcst_14d"] = dc_supply["fcst_14d"].fillna(0.0)

            dc_supply["fcst_day"] = dc_supply["fcst_14d"] / 14
            dc_supply["wos_oh"] = np.where(dc_supply["fcst_day"] > 0,
                                           dc_supply["on_hand"] / (dc_supply["fcst_day"] * 7), np.inf)
            under = int(((~(dc_supply["total_supply"] >= dc_supply["fcst_14d"]))
                         & (dc_supply["fcst_14d"] > 0)).sum())
            dd1, dd2, dd3 = st.columns(3)
            dd1.metric("Network DC on-hand", f"{dc_supply['on_hand'].sum():,.0f} ea")
            dd2.metric("Upcoming 14-day forecast", f"{dc_supply['fcst_14d'].sum():,.0f}")
            dd3.metric("DCs short of forecast", f"{under:,}", delta_color="inverse",
                       help="On-hand + on-order below the next-14-day forecast demand")
            dc_show = dc_supply.sort_values("fcst_14d", ascending=False).copy()
            dc_show["wos_oh"] = dc_show["wos_oh"].replace(np.inf, np.nan)
            dc_show = dc_show[["distribution_center_number", "name_of_the_dc", "fcst_14d",
                               "on_hand", "on_order", "total_supply", "wos_oh"]]
            dc_show.columns = ["DC #", "DC Name", "Forecast 14d", "On hand (ea)",
                               "On order (ea)", "Total supply (ea)", "WOS (OH)"]
            st.dataframe(dc_show, width='stretch', hide_index=True, height=380, column_config={
                "Forecast 14d": st.column_config.NumberColumn(format="%.0f"),
                "On hand (ea)": st.column_config.NumberColumn(format="%.0f"),
                "On order (ea)": st.column_config.NumberColumn(format="%.0f"),
                "Total supply (ea)": st.column_config.NumberColumn(format="%.0f"),
                "WOS (OH)": st.column_config.NumberColumn(format="%.1f wks"),
            })
            st.caption(f"Snapshot {latest_dc_date.strftime('%b %d, %Y')} · DC supply shown in eaches.")


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 4 — INVENTORY & DC
# ═══════════════════════════════════════════════════════════════════════════
with tab_inv:
    # ── Inventory health at a glance ─────────────────────────────────────────
    st.subheader("Inventory health at a glance")

    # Velocity basis = recent run-rate (last 14 data days), so weeks-of-supply
    # reflects how fast we're selling *now*, not the whole lookback.
    inv_recent = df[df["business_date"] >= most_recent - timedelta(days=13)]
    inv_recent_days = max(1, inv_recent["business_date"].dt.normalize().nunique())
    daily_units = float(inv_recent["pos_quantity_this_year"].sum() / inv_recent_days)
    weekly_units = daily_units * 7

    snap = df[df["business_date"] == most_recent]
    store_oh = float(snap["store_on_hand_quantity_this_year"].sum())
    store_oh_ly = float(snap["store_on_hand_quantity_last_year"].sum())
    in_whse = float(snap["store_in_warehouse_quantity_this_year"].sum())
    in_transit = float(snap["store_in_transit_quantity_this_year"].sum())
    oh_yoy = ((store_oh - store_oh_ly) / store_oh_ly * 100) if store_oh_ly else 0.0

    dc_oh_total = 0.0
    if not dc_df.empty:
        _dc_snap = dc_df[dc_df["inventory_date"] == dc_df["inventory_date"].max()]
        dc_oh_total = float(_dc_snap["on_hand_warehouse_inventory_in_units_this_year"].sum())

    store_wos = (store_oh / weekly_units) if weekly_units else float("inf")
    # In-transit has left the DC; in-warehouse is DC stock earmarked for stores, so it
    # would double-count DC on-hand — exclude it from the combined system total.
    system_units = store_oh + in_transit + dc_oh_total
    system_wos = (system_units / weekly_units) if weekly_units else float("inf")

    store_day_oh = snap.groupby("store_number")["store_on_hand_quantity_this_year"].sum()
    oos_now = int((store_day_oh == 0).sum())
    tot_stores = int(store_day_oh.shape[0])
    oos_pct = (oos_now / tot_stores * 100) if tot_stores else 0.0

    _wks = lambda v: f"{v:.1f} wks" if np.isfinite(v) else "—"
    iv1, iv2, iv3, iv4, iv5 = st.columns(5)
    iv1.metric("Store on-hand", f"{store_oh:,.0f}", f"{oh_yoy:+.1f}% YoY")
    iv2.metric("Store weeks of supply", _wks(store_wos),
               help="Store on-hand ÷ recent weekly sell-through")
    iv3.metric("Store pipeline", f"{in_whse + in_transit:,.0f}",
               help="Units in-warehouse + in-transit heading to stores")
    iv4.metric("DC on-hand", f"{dc_oh_total:,.0f}", help="Distribution-center on-hand (eaches)")
    iv5.metric("Total system cover", _wks(system_wos),
               help="Store on-hand + in-transit + DC on-hand ÷ recent weekly sell-through")
    st.caption(
        f"Snapshot **{most_recent.strftime('%b %d, %Y')}** · velocity basis: last {inv_recent_days} days "
        f"(**{daily_units:,.0f}** units/day) · stores out of stock now: **{oos_now:,}/{tot_stores:,}** "
        f"({oos_pct:.0f}%)."
    )

    # ── Weekly inventory trend ───────────────────────────────────────────────
    st.divider()
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
    weekly_inv["week_label"] = _wm_week_label(weekly_inv["walmart_calendar_week"])

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
        ).properties(height=320)), width='stretch')

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

    br_df, br_err = load_backroom_data(lookback, slot)
    if br_err:
        _section_error("Backroom adjustment data", br_err)
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
                ).properties(height=280)), width='stretch')
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

    st.caption(
        "Can the DCs keep stores replenished? Weeks-of-supply = DC on-hand ÷ the sell-through "
        "of the stores that DC serves (recent run-rate). Sorted worst-first so risk is up top."
    )

    if dc_df.empty:
        st.info("DC data unavailable for the current filter.")
    else:
        align_df, align_err = load_dc_alignment(slot)
        latest_dc_date = dc_df["inventory_date"].max()
        dc_latest = dc_df[dc_df["inventory_date"] == latest_dc_date].copy()
        # load_dc_data already converts packs → eaches; on-hand/on-order/OOS are eaches.
        dc_summary = dc_latest.groupby(
            ["distribution_center_number", "name_of_the_dc"], as_index=False
        ).agg(
            on_hand=("on_hand_warehouse_inventory_in_units_this_year", "sum"),
            on_order=("on_order_warehouse_quantity_in_units_this_year", "sum"),
            oos=("out_of_stock_each_quantity_this_year", "sum"),
        )
        dc_summary["total_supply"] = dc_summary["on_hand"] + dc_summary["on_order"]

        # Per-DC demand from the stores it serves, at the recent run-rate.
        if align_err or align_df.empty:
            if align_err:
                logger.warning("DC alignment unavailable: %s", align_err)
            st.caption("⚠ Store→DC alignment unavailable — demand allocated proportionally to on-hand "
                       "(weeks-of-supply will look uniform across DCs).")
            net_oh = max(1.0, float(dc_summary["on_hand"].sum()))
            dc_summary["daily_demand"] = dc_summary["on_hand"] / net_oh * daily_units
        else:
            # Deterministic primary pick: lowest alignment_type, then lowest DC number,
            # so a store's demand can't hop DCs between refreshes on row-order alone.
            primary_align = align_df.sort_values(
                ["store_number", "alignment_type", "distribution_center_number"]
            ).drop_duplicates(subset=["store_number"], keep="first")
            store_demand = inv_recent.groupby("store_number", as_index=False)["pos_quantity_this_year"].sum()
            store_demand = store_demand.merge(primary_align, on="store_number", how="left")
            dc_demand = store_demand.groupby("distribution_center_number", as_index=False)[
                "pos_quantity_this_year"].sum().rename(columns={"pos_quantity_this_year": "recent_units"})
            dc_demand["daily_demand"] = dc_demand["recent_units"] / inv_recent_days
            dc_summary = dc_summary.merge(dc_demand[["distribution_center_number", "daily_demand"]],
                                          on="distribution_center_number", how="left")
            dc_summary["daily_demand"] = dc_summary["daily_demand"].fillna(0.0)

        wk_demand = dc_summary["daily_demand"] * 7
        dc_summary["wos_oh"] = np.where(wk_demand > 0, dc_summary["on_hand"] / wk_demand, np.inf)
        dc_summary["wos_total"] = np.where(wk_demand > 0, dc_summary["total_supply"] / wk_demand, np.inf)
        dc_summary["status"] = np.select(
            [dc_summary["wos_oh"] <= 1, dc_summary["wos_oh"] <= 2], ["Critical", "Low"], default="Healthy")
        dc_summary = dc_summary.sort_values("wos_oh", ascending=True)

        dc_l, dc_r = st.columns([1, 2])
        with dc_l:
            st.metric("Network DC on-hand", f"{int(dc_summary['on_hand'].sum()):,}")
            st.metric("Network DC on-order", f"{int(dc_summary['on_order'].sum()):,}")
            st.metric("DC out-of-stock (eaches)", f"{int(dc_summary['oos'].sum()):,}",
                      delta_color="inverse", help="DC-level out-of-stock units — demand it couldn't fill")
            critical = int((dc_summary["wos_oh"] <= 1).sum())
            st.metric("DCs ≤ 1 wk supply", f"{critical:,}", delta_color="inverse")
            st.caption(f"Snapshot: {latest_dc_date.strftime('%b %d, %Y')}")
        with dc_r:
            show_dc = dc_summary[["distribution_center_number", "name_of_the_dc", "status",
                                  "on_hand", "on_order", "oos", "wos_oh", "wos_total"]].copy()
            show_dc["wos_oh"] = show_dc["wos_oh"].replace(np.inf, np.nan)
            show_dc["wos_total"] = show_dc["wos_total"].replace(np.inf, np.nan)
            show_dc.columns = ["DC #", "DC Name", "Status", "On Hand", "On Order",
                               "DC OOS", "WOS (OH)", "WOS (OH+OO)"]
            st.dataframe(show_dc, width='stretch', hide_index=True, height=420,
                         column_config={
                             "On Hand": st.column_config.NumberColumn(format="%d"),
                             "On Order": st.column_config.NumberColumn(format="%d"),
                             "DC OOS": st.column_config.NumberColumn(format="%d"),
                             "WOS (OH)": st.column_config.NumberColumn(format="%.1f wks"),
                             "WOS (OH+OO)": st.column_config.NumberColumn(format="%.1f wks"),
                         })


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 5 — CHANNELS (omni sales, ecom inventory, returns)
# ═══════════════════════════════════════════════════════════════════════════
with tab_channels:
    st.subheader("Omni-channel sales")
    st.caption(
        "Sales across all channels (in-store + pickup + ship-to-home + ship-from-store etc). "
        "Comparing in-store-only against omni-total tells you whether decline is real or channel migration."
    )

    omni_df, omni_err = load_omni_data(lookback, slot)
    if omni_err:
        _section_error("Omni sales data", omni_err)
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
        by_chan["yoy_pct"] = _yoy_pct(by_chan["units_ty"], by_chan["units_ly"])
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
            ).properties(height=280)), width='stretch')

    # ── eComm Inventory ──────────────────────────────────────────────────────
    st.divider()
    st.subheader("eComm inventory (ship nodes)")
    st.caption("On-hand and available-to-sell at fulfillment-center / ship-from-store nodes.")

    ecom_df, ecom_err = load_ecom_inv_data(lookback, slot)
    if ecom_err:
        _section_error("eComm inventory data", ecom_err)
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
                ).properties(height=280)), width='stretch')

    # ── Store Returns ────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Store returns")
    st.caption(
        "Tracks if returns are growing. Climbing returns eat into net sales and may signal "
        "product quality, packaging, or fit issues."
    )

    ret_df, ret_err = load_returns_data(lookback, slot)
    if ret_err:
        _section_error("Returns data", ret_err)
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
            ret_filt["week_start"] = _walmart_week_start(ret_filt["return_date"])
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
                ).properties(height=280)), width='stretch')


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 6 — DISTRIBUTION
# ═══════════════════════════════════════════════════════════════════════════
with tab_dist:
    # ── NEW: Modular coverage (distribution gap) ─────────────────────────────
    st.subheader("Modular coverage gap")
    st.caption(
        "Stores in the active modular plan vs. stores that actually sold this item. "
        "The gap = execution failure (item should be carried but isn't moving)."
    )

    mod_df, mod_err = load_modular_data(slot)
    if mod_err:
        _section_error("Modular plan data", mod_err)
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
            st.dataframe(cov_df, width='stretch', hide_index=True,
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

    state_perf = df.groupby("state_or_province_code", as_index=False, observed=True).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        stores=("store_number", "nunique"),
    )
    state_perf["yoy_pct"] = _yoy_pct(state_perf["units_ty"], state_perf["units_ly"])
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
    ).properties(height=420)), width='stretch')


# ─── Footer ──────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"BigQuery rows: {len(df):,} store · {len(dc_df):,} DC · "
    f"Auto-refresh: hourly from 6am Central until data arrives · Manual refresh in sidebar · Current slot: {slot}"
)

# A forced live refresh applies only to the rerun it triggered; clear it now
# (after every loader has run) so the next rerun uses the fast snapshot again.
if st.session_state.get("_force_live_refresh"):
    st.session_state["_force_live_refresh"] = False
