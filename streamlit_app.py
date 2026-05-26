"""
streamlit_app.py — FunPop Sales Dashboard

Tab structure:
  Overview          — KPIs, last 10 days, item performance, stockout risk
  Weather           — Heat/dryness correlation, maps, forward weather demand outlook
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
import logging
import urllib.parse
import urllib.request
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
)

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


def _section_error(label: str, err: object) -> None:
    """Log the real error server-side and show viewers a generic note.
    The dashboard is public, so raw BigQuery errors (which embed the project,
    dataset, and table names) must never be rendered on the page."""
    logger.warning("%s unavailable: %s", label, err)
    st.warning(
        f"{label} is temporarily unavailable. "
        "Try the Refresh button in the sidebar, or check back shortly."
    )


STATE_CENTROIDS = {
    "AL": (32.806671, -86.791130), "AK": (61.370716, -152.404419), "AZ": (33.729759, -111.431221),
    "AR": (34.969704, -92.373123), "CA": (36.116203, -119.681564), "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371), "DE": (39.318523, -75.507141), "FL": (27.766279, -81.686783),
    "GA": (33.040619, -83.643074), "HI": (21.094318, -157.498337), "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137), "IN": (39.849426, -86.258278), "IA": (42.011539, -93.210526),
    "KS": (38.526600, -96.726486), "KY": (37.668140, -84.670067), "LA": (31.169546, -91.867805),
    "ME": (44.693947, -69.381927), "MD": (39.063946, -76.802101), "MA": (42.230171, -71.530106),
    "MI": (43.326618, -84.536095), "MN": (45.694454, -93.900192), "MS": (32.741646, -89.678696),
    "MO": (38.456085, -92.288368), "MT": (46.921925, -110.454353), "NE": (41.125370, -98.268082),
    "NV": (38.313515, -117.055374), "NH": (43.452492, -71.563896), "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482), "NY": (42.165726, -74.948051), "NC": (35.630066, -79.806419),
    "ND": (47.528912, -99.784012), "OH": (40.388783, -82.764915), "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938), "PA": (40.590752, -77.209755), "RI": (41.680893, -71.511780),
    "SC": (33.856892, -80.945007), "SD": (44.299782, -99.438828), "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461), "UT": (40.150032, -111.862434), "VT": (44.045876, -72.710686),
    "VA": (37.769337, -78.169968), "WA": (47.400902, -121.490494), "WV": (38.491226, -80.954453),
    "WI": (44.268543, -89.616508), "WY": (42.755966, -107.302490), "DC": (38.907192, -77.036873),
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
def load_store_data(lookback_days: int, refresh_slot: str = "") -> pd.DataFrame:
    df = _run_query(_load_sql("store_query.sql"), [
        bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
    ])
    # Record the wall-clock time of this BQ fetch. Only executes on cache miss,
    # so this is the actual "last refresh" stamp.
    _freshness_state()["last_refresh_at"] = datetime.now(CENTRAL_TZ).isoformat()
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
def load_dc_data(lookback_days: int, refresh_slot: str = "") -> pd.DataFrame:
    try:
        df = _run_query(_load_sql("dc_query.sql"), [
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
@st.cache_data(ttl=86400, show_spinner=False)
def load_dc_alignment(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("dc_alignment_query.sql"))
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_forecast_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("forecast_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["forecast_date"] = pd.to_datetime(df["forecast_date"])
            df["forecast_quantity"] = pd.to_numeric(df["forecast_quantity"], errors="coerce").fillna(0)
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_omni_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("omni_query.sql"), [
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


@st.cache_data(ttl=86400, show_spinner=False)
def load_ecom_inv_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("ecom_inv_query.sql"), [
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


@st.cache_data(ttl=86400, show_spinner=False)
def load_returns_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("returns_query.sql"), [
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


@st.cache_data(ttl=86400, show_spinner=False)
def load_modular_data(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("modular_query.sql"), [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
        ])
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, show_spinner=False)
def load_backroom_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _run_query(_load_sql("backroom_query.sql"), [
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


def _open_meteo_daily_url(base_url: str, states: tuple[str, ...], **extra_params) -> str:
    coords = [STATE_CENTROIDS[s] for s in states]
    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in coords),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in coords),
        "daily": "temperature_2m_max,precipitation_sum",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "America/Chicago",
    }
    params.update(extra_params)
    return f"{base_url}?{urllib.parse.urlencode(params)}"


def _fetch_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "funpop-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _weather_payload_to_frame(payload, states: tuple[str, ...], period: str) -> pd.DataFrame:
    blocks = payload if isinstance(payload, list) else [payload]
    rows = []
    for state, block in zip(states, blocks):
        daily = block.get("daily", {})
        for date, temp, precip in zip(
            daily.get("time", []),
            daily.get("temperature_2m_max", []),
            daily.get("precipitation_sum", []),
        ):
            lat, lon = STATE_CENTROIDS[state]
            rows.append({
                "state_or_province_code": state,
                "date": pd.to_datetime(date),
                "temperature_max_f": temp,
                "precipitation_in": precip,
                "lat": lat,
                "lon": lon,
                "period": period,
            })
    return pd.DataFrame(rows)


def _add_weather_score(wx: pd.DataFrame) -> pd.DataFrame:
    if wx.empty:
        return wx
    out = wx.copy()
    temp = pd.to_numeric(out["temperature_max_f"], errors="coerce").fillna(0)
    precip = pd.to_numeric(out["precipitation_in"], errors="coerce").fillna(0)
    heat = ((temp - 70) / 25).clip(lower=0, upper=1.4)
    dry = (1 - (precip / 0.50)).clip(lower=0, upper=1)
    out["heat_dry_score"] = ((0.7 * heat + 0.3 * dry) * 100).round(0)
    out["weather_label"] = np.select(
        [out["heat_dry_score"] >= 90, out["heat_dry_score"] >= 65, out["heat_dry_score"] >= 40],
        ["Prime", "Good", "Neutral"],
        default="Soft",
    )
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def load_weather_data(
    states: tuple[str, ...],
    start_date: str,
    end_date: str,
    forecast_days: int = 14,
) -> tuple[pd.DataFrame, pd.DataFrame, str | None]:
    states = tuple(s for s in states if s in STATE_CENTROIDS)
    if not states:
        return pd.DataFrame(), pd.DataFrame(), "No selected states have weather coordinates."
    try:
        hist_url = _open_meteo_daily_url(
            "https://archive-api.open-meteo.com/v1/archive",
            states,
            start_date=start_date,
            end_date=end_date,
        )
        forecast_url = _open_meteo_daily_url(
            "https://api.open-meteo.com/v1/forecast",
            states,
            forecast_days=forecast_days,
        )
        hist = _add_weather_score(_weather_payload_to_frame(_fetch_json(hist_url), states, "Historical"))
        forecast = _add_weather_score(_weather_payload_to_frame(_fetch_json(forecast_url), states, "Forecast"))
        return hist, forecast, None
    except Exception as e:
        return pd.DataFrame(), pd.DataFrame(), str(e)


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


def _safe_corr(frame: pd.DataFrame, x: str, y: str) -> float:
    clean = frame[[x, y]].dropna()
    if len(clean) < 3 or clean[x].nunique() < 2 or clean[y].nunique() < 2:
        return 0.0
    return float(clean[x].corr(clean[y]))


def _fit_weather_lift_model(wx_sales: pd.DataFrame) -> tuple[np.ndarray, pd.Series, pd.Series]:
    model = wx_sales[["units_per_store", "temperature_max_f", "precipitation_in"]].dropna()
    if len(model) < 10 or model["units_per_store"].mean() <= 0:
        return np.array([0.0, 0.0, 0.0]), pd.Series(dtype=float), pd.Series(dtype=float)
    means = model[["temperature_max_f", "precipitation_in"]].mean()
    y = (model["units_per_store"] / model["units_per_store"].mean()) - 1
    x = np.column_stack([
        np.ones(len(model)),
        model["temperature_max_f"] - means["temperature_max_f"],
        model["precipitation_in"] - means["precipitation_in"],
    ])
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    beta[1] = max(0, beta[1])
    beta[2] = min(0, beta[2])
    return beta, means, model["units_per_store"].describe()


# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Controls")
    if st.button("🔄 Refresh data", help="Clear cache, re-pull from BigQuery"):
        st.cache_data.clear()
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
tab_overview, tab_weather, tab_sales, tab_inv, tab_channels, tab_dist = st.tabs([
    "Overview",
    "Weather",
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
        show = daily[["weekday", "date_str", "units_ty", "yoy_pct", "stores_selling"]].iloc[::-1].copy()
        show["stores_selling"] = show["stores_selling"].astype(int)
        show.columns = ["Day", "Date", "Units TY", "YoY %", "Stores Selling"]
        st.dataframe(show, width='stretch', hide_index=True, height=380,
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
    item_perf["sales_yoy_pct"] = _yoy_pct(item_perf["sales_ty"], item_perf["sales_ly"])
    item_perf["on_hand"] = item_perf["walmart_item_number"].map(item_oh).fillna(0).astype(int)
    item_perf["item"] = item_perf["walmart_item_number"].map(ITEM_LABELS).fillna(item_perf["walmart_item_number"].astype(str))
    item_perf["yoy_pct"] = _yoy_pct(item_perf["units_ty"], item_perf["units_ly"])
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
#   TAB 2 — WEATHER
# ═══════════════════════════════════════════════════════════════════════════
with tab_weather:
    st.subheader("Weather impact")
    st.caption(
        "Direct API connection: Open-Meteo historical weather + 14-day forecast. "
        "Weather is mapped at state-center level until exact store coordinates are available."
    )

    state_sales = df.groupby("state_or_province_code", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        stores=("store_number", "nunique"),
    ).sort_values("units_ty", ascending=False)
    weather_states = tuple(state_sales[
        state_sales["state_or_province_code"].isin(STATE_CENTROIDS)
    ]["state_or_province_code"])
    weather_units = float(state_sales[
        state_sales["state_or_province_code"].isin(weather_states)
    ]["units_ty"].sum())
    total_weather_scope_units = float(state_sales["units_ty"].sum())
    weather_coverage_pct = (
        weather_units / total_weather_scope_units * 100
    ) if total_weather_scope_units else 0

    if not weather_states:
        st.info("No state codes in the sales data could be matched to weather coordinates.")
    else:
        weather_hist, weather_forecast, weather_err = load_weather_data(
            weather_states,
            df["business_date"].min().date().isoformat(),
            most_recent.date().isoformat(),
            forecast_days=14,
        )

        if weather_err:
            _section_error("Weather data", weather_err)
        elif weather_hist.empty or weather_forecast.empty:
            st.info("Weather API returned no usable records for the selected states.")
        else:
            daily_state_sales = df.groupby(
                ["business_date", "state_or_province_code"], as_index=False
            ).agg(
                units_ty=("pos_quantity_this_year", "sum"),
                sales_ty=("pos_sales_this_year", "sum"),
                stores_reporting=("store_number", "nunique"),
            )
            daily_state_sales["units_per_store"] = np.where(
                daily_state_sales["stores_reporting"] > 0,
                daily_state_sales["units_ty"] / daily_state_sales["stores_reporting"],
                0,
            )
            wx_sales = daily_state_sales.merge(
                weather_hist,
                left_on=["state_or_province_code", "business_date"],
                right_on=["state_or_province_code", "date"],
                how="inner",
            )

            if wx_sales.empty:
                st.info("Weather loaded, but there were no matching weather dates for the sales window.")
            else:
                temp_corr = _safe_corr(wx_sales, "temperature_max_f", "units_per_store")
                precip_corr = _safe_corr(wx_sales, "precipitation_in", "units_per_store")
                prime_days = int((wx_sales["heat_dry_score"] >= 90).sum())
                dry_days = int((wx_sales["precipitation_in"] <= 0.05).sum())

                w1, w2, w3, w4 = st.columns(4)
                w1.metric("Temp correlation", f"{temp_corr:+.2f}", help="Correlation vs units/store/day")
                w2.metric("Precip correlation", f"{precip_corr:+.2f}", help="Negative means wetter days sold less")
                w3.metric("Prime heat/dry days", f"{prime_days:,}")
                w4.metric("Weather coverage", f"{weather_coverage_pct:.0f}%",
                          help=f"{len(weather_states):,} states matched to weather coordinates")
                st.caption(
                    f"Weather model coverage: {weather_units:,.0f} of {total_weather_scope_units:,.0f} "
                    f"units in the selected period. Dry state-days: {dry_days:,}."
                )

                hist_daily = wx_sales.groupby("business_date", as_index=False).agg(
                    units_ty=("units_ty", "sum"),
                    temperature_max_f=("temperature_max_f", "mean"),
                    precipitation_in=("precipitation_in", "mean"),
                    heat_dry_score=("heat_dry_score", "mean"),
                ).sort_values("business_date")

                h_l, h_r = st.columns([3, 2])
                with h_l:
                    st.markdown("**Historical sales vs heat/dry score**")
                    hist_chart = alt.layer(
                        alt.Chart(hist_daily).mark_bar(opacity=0.45, color="#185FA5").encode(
                            x=alt.X("business_date:T", title="Date"),
                            y=alt.Y("units_ty:Q", title="Units sold"),
                            tooltip=[
                                alt.Tooltip("business_date:T", title="Date"),
                                alt.Tooltip("units_ty:Q", format=",", title="Units"),
                                alt.Tooltip("temperature_max_f:Q", format=".1f", title="Avg high F"),
                                alt.Tooltip("precipitation_in:Q", format=".2f", title="Avg precip in"),
                                alt.Tooltip("heat_dry_score:Q", format=".0f", title="Heat/dry score"),
                            ],
                        ),
                        alt.Chart(hist_daily).mark_line(color="#BA7517", strokeWidth=2).encode(
                            x="business_date:T",
                            y=alt.Y("heat_dry_score:Q", title="Heat/dry score"),
                        ),
                    ).resolve_scale(y="independent").properties(height=320)
                    st.altair_chart(hist_chart, width='stretch')
                with h_r:
                    st.markdown("**Daily relationship**")
                    scatter = alt.Chart(wx_sales).mark_circle(size=70, opacity=0.55).encode(
                        x=alt.X("temperature_max_f:Q", title="Daily high (F)"),
                        y=alt.Y("units_per_store:Q", title="Units / store / day"),
                        color=alt.Color("precipitation_in:Q", scale=alt.Scale(scheme="blues"),
                                        legend=alt.Legend(title="Precip in")),
                        tooltip=[
                            alt.Tooltip("state_or_province_code:N", title="State"),
                            alt.Tooltip("business_date:T", title="Date"),
                            alt.Tooltip("temperature_max_f:Q", format=".1f", title="High F"),
                            alt.Tooltip("precipitation_in:Q", format=".2f", title="Precip in"),
                            alt.Tooltip("units_per_store:Q", format=".2f", title="Units/store"),
                        ],
                    ).properties(height=320)
                    st.altair_chart(scatter, width='stretch')

                st.divider()
                st.subheader("14-day weather-driven outlook")

                beta, weather_means, _ = _fit_weather_lift_model(wx_sales)
                recent_cutoff = most_recent - timedelta(days=13)
                recent_source = df[df["business_date"] >= recent_cutoff].copy()
                recent_days = max(1, recent_source["business_date"].dt.normalize().nunique())
                network_avg_daily_units = float(
                    recent_source["pos_quantity_this_year"].sum() / recent_days
                )
                recent_state = recent_source.groupby(
                    "state_or_province_code", as_index=False
                ).agg(
                    recent_units=("pos_quantity_this_year", "sum"),
                    stores=("store_number", "nunique"),
                )
                # Use one common denominator so state averages add back to the total network average.
                recent_state["avg_daily_units"] = recent_state["recent_units"] / recent_days
                fc = weather_forecast.merge(recent_state, on="state_or_province_code", how="inner")
                if fc.empty:
                    st.info("Forecast weather loaded, but no forecast states matched recent sales states.")
                else:
                    covered_avg_daily_units = float(recent_state[
                        recent_state["state_or_province_code"].isin(fc["state_or_province_code"].unique())
                    ]["avg_daily_units"].sum())
                    unmodeled_avg_daily_units = max(0.0, network_avg_daily_units - covered_avg_daily_units)
                    if len(weather_means):
                        fc["weather_lift"] = (
                            beta[0]
                            + beta[1] * (fc["temperature_max_f"] - weather_means["temperature_max_f"])
                            + beta[2] * (fc["precipitation_in"] - weather_means["precipitation_in"])
                        )
                    else:
                        hist_score = max(1, wx_sales["heat_dry_score"].mean())
                        fc["weather_lift"] = ((fc["heat_dry_score"] - hist_score) / 100) * 0.15
                    fc["weather_lift"] = fc["weather_lift"].clip(lower=-0.35, upper=0.60)
                    fc["predicted_modeled_units"] = (fc["avg_daily_units"] * (1 + fc["weather_lift"])).clip(lower=0)
                    fc["date_label"] = fc["date"].dt.strftime("%a %b %d")

                    fc_daily = fc.groupby("date", as_index=False).agg(
                        modeled_units=("predicted_modeled_units", "sum"),
                        avg_temp=("temperature_max_f", "mean"),
                        avg_precip=("precipitation_in", "mean"),
                        heat_dry_score=("heat_dry_score", "mean"),
                    ).sort_values("date")
                    fc_daily["baseline_unmodeled_units"] = unmodeled_avg_daily_units
                    fc_daily["predicted_units"] = (
                        fc_daily["modeled_units"] + fc_daily["baseline_unmodeled_units"]
                    )
                    fc_daily["recent_baseline_units"] = network_avg_daily_units
                    fc_daily["date_label"] = fc_daily["date"].dt.strftime("%a %b %d")

                    f_l, f_r = st.columns([3, 2])
                    with f_l:
                        outlook_chart = alt.layer(
                            alt.Chart(fc_daily).mark_bar(color="#27500A", opacity=0.65).encode(
                                x=alt.X("date_label:N", sort=list(fc_daily["date_label"]), title="Forecast date",
                                        axis=alt.Axis(labelAngle=-30)),
                                y=alt.Y("predicted_units:Q", title="Weather-adjusted units"),
                                tooltip=[
                                    "date_label:N",
                                    alt.Tooltip("predicted_units:Q", format=",.0f", title="Predicted units"),
                                    alt.Tooltip("recent_baseline_units:Q", format=",.0f", title="Recent baseline"),
                                    alt.Tooltip("avg_temp:Q", format=".1f", title="Avg high F"),
                                    alt.Tooltip("avg_precip:Q", format=".2f", title="Avg precip in"),
                                ],
                            ),
                            alt.Chart(fc_daily).mark_line(color="#BA7517", strokeWidth=2).encode(
                                x=alt.X("date_label:N", sort=list(fc_daily["date_label"])),
                                y=alt.Y("heat_dry_score:Q", title="Heat/dry score"),
                            ),
                        ).resolve_scale(y="independent").properties(height=340)
                        st.altair_chart(outlook_chart, width='stretch')
                    with f_r:
                        total_base = network_avg_daily_units
                        total_pred = float(fc_daily["predicted_units"].mean())
                        avg_lift = ((total_pred - total_base) / total_base * 100) if total_base else 0
                        best_day = fc_daily.sort_values("predicted_units", ascending=False).iloc[0]
                        st.metric("Avg daily outlook", f"{total_pred:,.0f} units", f"{avg_lift:+.1f}% vs recent")
                        st.metric("Best weather day", best_day["date"].strftime("%b %d"),
                                  f"{best_day['heat_dry_score']:.0f} score")
                        st.caption(
                            f"Baseline reconciles to recent all-state average: {network_avg_daily_units:,.0f} "
                            f"units/day over the last {recent_days} data days."
                        )
                        show_fc = fc_daily[[
                            "date_label", "predicted_units", "recent_baseline_units", "avg_temp", "avg_precip"
                        ]].copy()
                        show_fc.columns = ["Date", "Predicted Units", "Recent Baseline", "Avg High F", "Avg Precip"]
                        st.dataframe(show_fc, width='stretch', hide_index=True, height=300,
                                     column_config={
                                         "Predicted Units": st.column_config.NumberColumn(format="%.0f"),
                                         "Recent Baseline": st.column_config.NumberColumn(format="%.0f"),
                                         "Avg High F": st.column_config.NumberColumn(format="%.1f"),
                                         "Avg Precip": st.column_config.NumberColumn(format="%.2f in"),
                                     })

                st.divider()
                st.subheader("Weather maps")
                map_l, map_r = st.columns(2)
                with map_l:
                    st.markdown("**Historical state opportunity**")
                    hist_map = wx_sales.groupby("state_or_province_code", as_index=False).agg(
                        units_ty=("units_ty", "sum"),
                        heat_dry_score=("heat_dry_score", "mean"),
                        lat=("lat", "first"),
                        lon=("lon", "first"),
                    )
                    hist_map["map_size"] = (
                        hist_map["units_ty"] / max(1, hist_map["units_ty"].max()) * 45000 + 7000
                    )
                    st.map(hist_map, latitude="lat", longitude="lon", size="map_size")
                    st.caption("Point size = historical units in the selected window.")
                with map_r:
                    st.markdown("**Next 7 days weather demand**")
                    if "predicted_modeled_units" in fc.columns:
                        fc7 = fc[fc["date"] <= fc["date"].min() + pd.Timedelta(days=6)]
                        fc_map = fc7.groupby("state_or_province_code", as_index=False).agg(
                            predicted_units=("predicted_modeled_units", "sum"),
                            heat_dry_score=("heat_dry_score", "mean"),
                            lat=("lat", "first"),
                            lon=("lon", "first"),
                        )
                        fc_map["map_size"] = (
                            fc_map["predicted_units"] / max(1, fc_map["predicted_units"].max()) * 45000 + 7000
                        )
                        st.map(fc_map, latitude="lat", longitude="lon", size="map_size")
                        st.caption("Point size = weather-adjusted predicted units for the next 7 days.")
                    else:
                        st.info("Forecast map appears after a usable weather/sales prediction is built.")


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 3 — SALES & VELOCITY
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
    st.markdown("**U/S/W by item (period total)**")
    item_uspw = df.groupby("walmart_item_number", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
    )
    ty_active_item = (df[df["pos_quantity_this_year"] > 0]
                      .groupby("walmart_item_number")["store_number"].nunique()
                      .rename("stores_ty").reset_index())
    ly_active_item = (df[df["pos_quantity_last_year"] > 0]
                      .groupby("walmart_item_number")["store_number"].nunique()
                      .rename("stores_ly").reset_index())
    item_uspw = item_uspw.merge(ty_active_item, on="walmart_item_number", how="left")
    item_uspw = item_uspw.merge(ly_active_item, on="walmart_item_number", how="left")
    item_uspw["stores_ty"] = item_uspw["stores_ty"].fillna(0).astype(int)
    item_uspw["stores_ly"] = item_uspw["stores_ly"].fillna(0).astype(int)
    item_uspw["item"] = item_uspw["walmart_item_number"].map(ITEM_LABELS).fillna(
        item_uspw["walmart_item_number"].astype(str))
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
    ).properties(height=300)), width='stretch')

    # ── NEW: Forecast attainment ────────────────────────────────────────────
    st.divider()
    st.subheader("Forecast attainment")
    st.caption(
        "Actual sales as a percent of forecasted demand. Tells you whether the YoY decline "
        "is hitting forecast (demand issue) or missing forecast (supply/distribution issue)."
    )

    fcst_df, fcst_err = load_forecast_data(lookback, slot)
    if fcst_err:
        _section_error("Forecast data", fcst_err)
    elif fcst_df.empty:
        st.info("No forecast records returned for the selected items and lookback window.")
    else:
        fcst_df_filt = fcst_df[fcst_df["walmart_item_number"].isin(item_filter)].copy()
        # Attainment only makes sense where actuals can exist. The forecast table
        # carries future-dated rows; including them would left-join to absent
        # actuals (filled with 0) and understate the current week's attainment.
        fcst_df_filt = fcst_df_filt[fcst_df_filt["forecast_date"] <= most_recent]
        actuals = df.groupby(["business_date", "walmart_item_number"], as_index=False)[
            "pos_quantity_this_year"].sum().rename(columns={
                "business_date": "forecast_date",
                "pos_quantity_this_year": "actual_quantity",
            })
        attn = fcst_df_filt.groupby(["forecast_date", "walmart_item_number"], as_index=False)[
            "forecast_quantity"].sum().merge(actuals, on=["forecast_date", "walmart_item_number"], how="left")
        attn["actual_quantity"] = attn["actual_quantity"].fillna(0)
        weekly = attn.copy()
        weekly["week_start"] = _walmart_week_start(weekly["forecast_date"])
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
            ).properties(height=320)), width='stretch')
        with fa_r:
            show = weekly_attn[["week_label", "actual", "forecast", "attainment_pct"]].iloc[::-1].copy()
            show.columns = ["Week", "Actual", "Forecast", "Attainment %"]
            st.dataframe(show, width='stretch', hide_index=True,
                         column_config={"Attainment %": st.column_config.NumberColumn(format="%.1f%%")})

        avg_attn = weekly_attn["attainment_pct"].mean() if not weekly_attn.empty else 0.0
        st.caption(f"**Average period attainment: {avg_attn:.1f}%** — values below 100% mean actuals lagged forecast.")


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 4 — INVENTORY & DC
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

    if dc_df.empty:
        st.info("DC data unavailable for the current filter.")
    else:
        # Load store→DC alignment
        align_df, align_err = load_dc_alignment(slot)
        latest_dc_date = dc_df["inventory_date"].max()
        dc_latest = dc_df[dc_df["inventory_date"] == latest_dc_date].copy()

        # Compute true per-DC demand if alignment is available
        if align_err or align_df.empty:
            if align_err:
                logger.warning("DC alignment unavailable: %s", align_err)
                st.caption("⚠️  Using approximate DC demand allocation (store→DC alignment table unavailable).")
            total_per_day = df["pos_quantity_this_year"].sum() / period_days
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
            # Deterministic primary pick: lowest alignment_type code, then lowest
            # DC number. Without this, "first" depended on BigQuery row order and
            # could reassign a store's demand to a different DC between refreshes.
            primary_align = (
                align_df.sort_values(
                    ["store_number", "alignment_type", "distribution_center_number"]
                ).drop_duplicates(subset=["store_number"], keep="first")
            )
            store_demand = df.groupby("store_number", as_index=False)["pos_quantity_this_year"].sum()
            store_demand = store_demand.merge(primary_align, on="store_number", how="left")
            dc_demand = store_demand.groupby("distribution_center_number", as_index=False)[
                "pos_quantity_this_year"].sum().rename(columns={"pos_quantity_this_year": "period_demand"})
            dc_demand["daily_demand"] = dc_demand["period_demand"] / period_days

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
            st.dataframe(show_dc, width='stretch', hide_index=True, height=420,
                         column_config={"WOS (OH)": st.column_config.NumberColumn(format="%.1f wks")})


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

    state_perf = df.groupby("state_or_province_code", as_index=False).agg(
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
