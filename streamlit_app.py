"""
streamlit_app.py — FunPop Sales Dashboard

Tab structure:
  Overview          — KPIs, last 10 days, item performance, stockout risk
  Weather & Demand  — Why sales moved (weather attribution), the learned weather
                      sensitivity model, recent actual-vs-expected, a 14-day demand
                      forecast, YoY weather context, and a state tailwind map
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
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import streamlit as st
import altair as alt
from google.cloud import bigquery
from google.oauth2 import service_account

import store_geo as geo
import weather_model as wm
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


# Bundled US ZIP-code → centroid lookup (2013 government data, public domain).
# Used to place each store on the map and snap it to a weather grid cell.
ZIP_CENTROIDS_PATH = Path(__file__).parent / "data" / "zip_centroids.csv"

# Set to a column name (e.g. "postal_cd") to force the store_dim postal column;
# leave None to auto-detect it from the table schema.
STORE_POSTAL_COLUMN = None


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


@st.cache_data(ttl=604800, show_spinner=False)
def load_zip_centroids() -> pd.DataFrame:
    """Bundled US ZIP → centroid table (zip, lat, lng)."""
    return pd.read_csv(ZIP_CENTROIDS_PATH, dtype={"zip": str})


@st.cache_data(ttl=86400, show_spinner=False)
def load_store_dim_columns(refresh_slot: str = "") -> list:
    """Column names present in store_dim, so we can locate the postal column
    without hard-coding a schema that may differ across tenants."""
    proj = _get_sa_info()["project_id"]
    ds = st.secrets["bigquery"]["dataset"]
    sql = (f"SELECT column_name FROM `{proj}.{ds}.INFORMATION_SCHEMA.COLUMNS` "
           f"WHERE table_name = 'store_dim'")
    df = _run_query(sql)
    return list(df["column_name"]) if not df.empty else []


@st.cache_data(ttl=86400, show_spinner="Loading store locations...")
def load_store_geo(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None, str | None]:
    """Return (store_geo, postal_column, error).

    store_geo has one row per store: store_number, postal, zip5, lat, lon, cell_id.
    Coordinates come from the store's ZIP code mapped to the bundled centroid
    table; cell_id snaps the store onto a weather grid. Stores whose postal code
    can't be matched to a US ZIP get NaN coordinates and drop out downstream."""
    empty = pd.DataFrame()
    try:
        cols = load_store_dim_columns(refresh_slot)
    except Exception as e:
        return empty, None, str(e)
    postal_col = STORE_POSTAL_COLUMN or geo.pick_postal_column(cols)
    if not postal_col:
        return empty, None, "no_postal_column"
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", postal_col):
        return empty, None, "invalid_postal_column"
    try:
        proj = _get_sa_info()["project_id"]
        ds = st.secrets["bigquery"]["dataset"]
        sql = (f"SELECT store_nbr AS store_number, "
               f"ANY_VALUE(CAST(`{postal_col}` AS STRING)) AS postal "
               f"FROM `{proj}.{ds}.store_dim` GROUP BY store_nbr")
        stores = _run_query(sql)
        if stores.empty:
            return empty, postal_col, "no_rows"
        g = geo.assign_cells(geo.attach_coords(stores, load_zip_centroids(), "postal"))
        g["store_number"] = pd.to_numeric(g["store_number"], errors="coerce")
        return g, postal_col, None
    except Exception as e:
        return empty, postal_col, str(e)


def _fetch_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "funpop-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _open_meteo_url(base_url: str, coords, **extra_params) -> str:
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


def _fetch_open_meteo_blocks(base_url: str, coords, **params) -> list:
    """Fetch daily weather for many coordinates, batched to stay within URL/
    fair-use limits. Returns one daily block per coordinate, in input order."""
    blocks = []
    for batch in geo.chunk(range(len(coords)), 150):
        sub = [coords[i] for i in batch]
        payload = _fetch_json(_open_meteo_url(base_url, sub, **params))
        blocks.extend(payload if isinstance(payload, list) else [payload])
    return blocks


def _blocks_to_frame(blocks, cell_ids, coords) -> pd.DataFrame:
    rows = []
    for cell_id, (lat, lon), block in zip(cell_ids, coords, blocks):
        daily = (block or {}).get("daily", {})
        for day, temp, precip in zip(
            daily.get("time", []),
            daily.get("temperature_2m_max", []),
            daily.get("precipitation_sum", []),
        ):
            rows.append({
                "cell_id": cell_id,
                "date": pd.to_datetime(day),
                "temperature_max_f": temp,
                "precipitation_in": precip,
                "lat": lat,
                "lon": lon,
            })
    return pd.DataFrame(rows)


@st.cache_data(ttl=21600, show_spinner="Loading weather for store regions...")
def load_weather_for_cells(
    cells_tuple: tuple,
    start_date: str,
    end_date: str,
    forecast_days: int = 16,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str | None]:
    """Return (history, forecast, last_year_history, error), keyed by cell_id.

    cells_tuple is a hashable tuple of (cell_id, lat, lon). Recent history and the
    forward forecast come from one Open-Meteo forecast call (past_days +
    forecast_days) so the most recent days are never blank — the archive endpoint
    lags several days. Today appears in both history and forecast. Last-year
    history is shifted 364 days (52 weeks) to keep weekdays aligned with Walmart's
    LY comparison."""
    empty = pd.DataFrame()
    if not cells_tuple:
        return empty, empty, empty, "No store regions to fetch weather for."
    cell_ids = [c[0] for c in cells_tuple]
    coords = [(float(c[1]), float(c[2])) for c in cells_tuple]
    try:
        today = datetime.now(CENTRAL_TZ).date()
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        past_days = min(92, max(1, (today - start).days + 1))

        combo = _blocks_to_frame(
            _fetch_open_meteo_blocks(
                "https://api.open-meteo.com/v1/forecast", coords,
                past_days=past_days, forecast_days=forecast_days),
            cell_ids, coords)
        if combo.empty:
            return empty, empty, empty, "Weather API returned no records."
        combo["date"] = pd.to_datetime(combo["date"])
        today_ts = pd.Timestamp(today)
        hist = wm.add_weather_features(combo[combo["date"] <= today_ts].copy())
        forecast = wm.add_weather_features(combo[combo["date"] >= today_ts].copy())

        # Last-year weather for the same window (best-effort; never fatal).
        try:
            hist_ly = wm.add_weather_features(_blocks_to_frame(
                _fetch_open_meteo_blocks(
                    "https://archive-api.open-meteo.com/v1/archive", coords,
                    start_date=(start - timedelta(days=364)).isoformat(),
                    end_date=(end - timedelta(days=364)).isoformat()),
                cell_ids, coords))
            if not hist_ly.empty:
                hist_ly["date"] = pd.to_datetime(hist_ly["date"])
        except Exception:
            hist_ly = empty
        return hist, forecast, hist_ly, None
    except Exception as e:
        return empty, empty, empty, str(e)


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
    "Weather & Demand",
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
    st.subheader("Weather & Demand")
    st.caption(
        "Your sales move with the weather — hotter sells more, and **hot *and* dry** sells "
        "most. Weather is read at each store's own location (mapped from its ZIP code) and "
        "clustered into local grid cells, so a hot, dry Phoenix isn't averaged together with "
        "a rainy Seattle. The tab learns that relationship from your daily sell-through, "
        "explains why recent numbers moved, and turns the forecast into a demand outlook."
    )

    store_geo, postal_col, geo_err = load_store_geo(slot)

    if store_geo.empty or int(store_geo["cell_id"].notna().sum()) == 0:
        reason = {
            "no_postal_column": "No postal-code column was found in `store_dim`, so stores can't be located.",
            "invalid_postal_column": "The configured postal column name is invalid.",
            "no_rows": "`store_dim` returned no rows.",
        }.get(geo_err, "Store coordinates could not be loaded.")
        st.error(
            "**Store-level weather is unavailable.** " + reason +
            " This tab requires store locations. If your store table uses a non-standard postal "
            "column, set `STORE_POSTAL_COLUMN` near the top of streamlit_app.py."
        )
        if geo_err and geo_err not in ("no_postal_column", "invalid_postal_column", "no_rows"):
            _section_error("Store locations", geo_err)
    else:
        geo_small = store_geo.dropna(subset=["cell_id", "lat", "lon"]).copy()
        geo_small["store_number"] = pd.to_numeric(geo_small["store_number"], errors="coerce").astype("Int64")
        geo_small["cell_id"] = geo_small["cell_id"].astype(str)
        geo_small = geo_small[["store_number", "lat", "lon", "cell_id"]]

        dfx = df.copy()
        dfx["store_number"] = pd.to_numeric(dfx["store_number"], errors="coerce").astype("Int64")
        dfx = dfx.merge(geo_small, on="store_number", how="left")
        mapped = dfx["cell_id"].notna()
        total_units = float(df["pos_quantity_this_year"].sum())
        mapped_units = float(dfx.loc[mapped, "pos_quantity_this_year"].sum())
        coverage_pct = (mapped_units / total_units * 100) if total_units else 0.0
        mapped_stores = int(dfx.loc[mapped, "store_number"].nunique())
        total_stores = int(df["store_number"].nunique())

        if coverage_pct < 50:
            st.error(
                f"**Store-level weather is unavailable.** Only {mapped_stores:,} of {total_stores:,} "
                f"stores ({coverage_pct:.0f}% of units) could be mapped to a US ZIP location via "
                f"`store_dim.{postal_col}`. Check that the postal column is correct — set "
                f"`STORE_POSTAL_COLUMN` near the top of streamlit_app.py to override."
            )
        else:
            dfx = dfx[mapped].copy()
            dfx["cell_id"] = dfx["cell_id"].astype(str)
            today_ts = pd.Timestamp(datetime.now(CENTRAL_TZ).date())

            footprint_geo = store_geo[store_geo["store_number"].isin(
                dfx["store_number"].dropna().unique())]
            cells = geo.cell_coords(footprint_geo)
            cells["cell_id"] = cells["cell_id"].astype(str)
            cells_tuple = tuple(
                (str(r.cell_id), round(float(r.lat), 4), round(float(r.lon), 4))
                for r in cells.itertuples())

            weather_hist, weather_forecast, weather_hist_ly, weather_err = load_weather_for_cells(
                cells_tuple,
                df["business_date"].min().date().isoformat(),
                most_recent.date().isoformat(),
                forecast_days=16,
            )

            if weather_err:
                _section_error("Weather data", weather_err)
            elif weather_hist.empty or weather_forecast.empty:
                st.info("Weather API returned no usable records for the store regions.")
            else:
                # ── Daily cell-level sales×weather panel ────────────────────────
                # Velocity = units per *selling* store/day within each local cell,
                # so a store being out of stock counts as availability (store
                # effect), not as weak weather-driven demand.
                daily_units = dfx.groupby(["business_date", "cell_id"], as_index=False).agg(
                    units_ty=("pos_quantity_this_year", "sum"))
                selling = (dfx[dfx["pos_quantity_this_year"] > 0]
                           .groupby(["business_date", "cell_id"])["store_number"]
                           .nunique().rename("stores_selling").reset_index())
                cell_day = daily_units.merge(selling, on=["business_date", "cell_id"], how="left")
                cell_day["stores_selling"] = cell_day["stores_selling"].fillna(0)
                cell_day = cell_day[cell_day["stores_selling"] > 0].copy()
                cell_day["units_per_store"] = cell_day["units_ty"] / cell_day["stores_selling"]

                wx_panel = cell_day.merge(
                    weather_hist, left_on=["cell_id", "business_date"],
                    right_on=["cell_id", "date"], how="inner")

                if wx_panel.empty:
                    st.info("Weather loaded, but no weather dates lined up with the sales window.")
                else:
                    # Weight weather by where we actually sell (units per cell).
                    cell_units = dfx.groupby("cell_id")["pos_quantity_this_year"].sum()

                    def _footprint_mean(frame, col):
                        w = frame["cell_id"].map(cell_units).fillna(0.0)
                        total = float(w.sum())
                        return float((frame[col] * w).sum() / total) if total else float(frame[col].mean())

                    model = wm.fit_velocity_model(
                        wx_panel[["units_per_store", "heat", "rain", "hot_dry", "date", "cell_id"]]
                        .rename(columns={"cell_id": "area"}))

                    # Footprint daily series (store-weighted weather, national velocity).
                    wcols = ["heat", "rain", "hot_dry", "temperature_max_f",
                             "precipitation_in", "heat_dry_score"]
                    foot = wx_panel.copy()
                    for c in wcols:
                        foot[c + "_wsum"] = foot[c] * foot["stores_selling"]
                    daily = foot.groupby("business_date", as_index=False).agg(
                        units=("units_ty", "sum"), stores=("stores_selling", "sum"),
                        **{c + "_wsum": (c + "_wsum", "sum") for c in wcols})
                    for c in wcols:
                        daily[c] = np.where(daily["stores"] > 0, daily[c + "_wsum"] / daily["stores"], np.nan)
                    daily["vel"] = np.where(daily["stores"] > 0, daily["units"] / daily["stores"], 0.0)
                    daily = daily.sort_values("business_date").reset_index(drop=True)
                    all_dates = list(daily["business_date"])

                    win = min(7, len(all_dates) // 2)
                    recent_dates = all_dates[-win:] if win >= 1 else all_dates
                    prior_dates = all_dates[-2 * win:-win] if win >= 2 else []

                    # ── Today's conditions banner ───────────────────────────────
                    today_wx = weather_forecast[weather_forecast["date"] == today_ts]
                    if today_wx.empty:
                        today_wx = weather_forecast[weather_forecast["date"] == weather_forecast["date"].min()]
                    today_temp = _footprint_mean(today_wx, "temperature_max_f")
                    today_precip = _footprint_mean(today_wx, "precipitation_in")
                    st.info(
                        f"**Today across your footprint:** about **{today_temp:.0f}°F** and "
                        f"**{today_precip:.2f} in** of rain — {wm.verdict_phrase(today_temp, today_precip)}."
                    )
                    st.caption(
                        f"Mapped **{mapped_stores:,}/{total_stores:,}** stores "
                        f"({coverage_pct:.0f}% of units) via `store_dim.{postal_col}` to "
                        f"**{len(cells):,}** local weather areas · {len(daily):,} sales days analysed."
                    )

                    # ════════════════════════════════════════════════════════════
                    #  1 · WHY YOUR NUMBERS MOVED
                    # ════════════════════════════════════════════════════════════
                    st.divider()
                    st.markdown("### Why your numbers moved")

                    def _period_summary(dates_list):
                        sub = daily[daily["business_date"].isin(dates_list)]
                        if sub.empty:
                            return None
                        nd = len(sub)
                        w = sub["stores"]
                        wtot = float(w.sum())
                        wm_ = lambda c: float((sub[c] * w).sum() / wtot) if wtot else float(sub[c].mean())
                        stores = float(sub["stores"].mean())
                        units_per_day = float(sub["units"].sum() / nd)
                        return {
                            "n": nd, "units_per_day": units_per_day, "stores": stores,
                            "vel": (units_per_day / stores) if stores else 0.0,
                            "heat": wm_("heat"), "rain": wm_("rain"), "hot_dry": wm_("hot_dry"),
                            "temp": wm_("temperature_max_f"), "precip": wm_("precipitation_in"),
                        }

                    if win >= 2:
                        rec = _period_summary(recent_dates)
                        pri = _period_summary(prior_dates)
                        dec = wm.decompose_change(rec, pri, model)

                        delta = dec["total_delta"]
                        pct = (delta / dec["daily_prior"] * 100) if dec["daily_prior"] else 0.0
                        dword = "up" if delta >= 0 else "down"
                        d_temp = rec["temp"] - pri["temp"]
                        d_precip = rec["precip"] - pri["precip"]

                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric(f"Daily units (last {win}d)", f"{rec['units_per_day']:,.0f}",
                                  f"{pct:+.1f}% vs prior {win}d")
                        m2.metric("Weather drove", f"{dec['weather_effect']:+,.0f}/day",
                                  help="Estimated units/day from the weather change, per the model.")
                        m3.metric("Stores selling drove", f"{dec['store_effect']:+,.0f}/day",
                                  help="Change attributable to how many stores had product and sold.")
                        m4.metric("Everything else", f"{dec['other_effect']:+,.0f}/day",
                                  help="Demand, price, promo, execution — what weather and store count don't explain.")

                        def _phrase(val, helped, hurt, flat="was roughly flat"):
                            if abs(val) < max(1.0, 0.01 * abs(dec["daily_prior"])):
                                return flat
                            return helped if val > 0 else hurt

                        wx_tail = (
                            f"a **tailwind** (+{dec['weather_effect']:,.0f}/day)" if dec["weather_effect"] > 0
                            else f"a **headwind** ({dec['weather_effect']:,.0f}/day)"
                        )
                        st.markdown(
                            f"Daily units are **{dword} {abs(delta):,.0f}/day** ({pct:+.1f}%) versus the prior "
                            f"{win} days. It was **{d_temp:+.0f}°F** and **{d_precip:+.2f} in** of rain different, "
                            f"so weather was {wx_tail}. Store availability {_phrase(dec['store_effect'], 'added', 'cost')} "
                            f"~{abs(dec['store_effect']):,.0f}/day, and everything else "
                            f"(demand, pricing, execution) {_phrase(dec['other_effect'], 'added', 'cost')} "
                            f"~{abs(dec['other_effect']):,.0f}/day."
                        )

                        contrib = pd.DataFrame({
                            "driver": ["Stores selling", "Weather", "Everything else"],
                            "effect": [dec["store_effect"], dec["weather_effect"], dec["other_effect"]],
                        })
                        contrib["sign"] = np.where(contrib["effect"] >= 0, "Helped", "Hurt")
                        contrib_chart = alt.Chart(contrib).mark_bar().encode(
                            x=alt.X("effect:Q", title="Units/day impact vs prior period"),
                            y=alt.Y("driver:N", sort=["Stores selling", "Weather", "Everything else"], title=None),
                            color=alt.Color("sign:N", scale=alt.Scale(domain=["Helped", "Hurt"],
                                            range=["#27500A", "#791F1F"]), legend=None),
                            tooltip=[alt.Tooltip("driver:N", title="Driver"),
                                     alt.Tooltip("effect:Q", format="+,.0f", title="Units/day")],
                        ).properties(height=150)
                        st.altair_chart(contrib_chart, width='stretch')
                    else:
                        st.caption("Need at least 4 sales days to break down a recent swing.")

                    # ── Year-over-year weather context ──────────────────────────
                    if not weather_hist_ly.empty:
                        ty_cell = weather_hist.groupby("cell_id", as_index=False).agg(
                            heat=("heat", "mean"), rain=("rain", "mean"), hot_dry=("hot_dry", "mean"),
                            temperature_max_f=("temperature_max_f", "mean"),
                            precipitation_in=("precipitation_in", "mean"))
                        ly_cell = weather_hist_ly.groupby("cell_id", as_index=False).agg(
                            heat=("heat", "mean"), rain=("rain", "mean"), hot_dry=("hot_dry", "mean"),
                            temperature_max_f=("temperature_max_f", "mean"),
                            precipitation_in=("precipitation_in", "mean"))
                        ty_temp = _footprint_mean(ty_cell, "temperature_max_f")
                        ly_temp = _footprint_mean(ly_cell, "temperature_max_f")
                        ty_precip = _footprint_mean(ty_cell, "precipitation_in")
                        ly_precip = _footprint_mean(ly_cell, "precipitation_in")
                        yoy_units_txt = ""
                        if model is not None and model.confidence != "Low":
                            ty_f = pd.DataFrame([{k: _footprint_mean(ty_cell, k) for k in wm.WEATHER_FEATURES}])
                            ly_f = pd.DataFrame([{k: _footprint_mean(ly_cell, k) for k in wm.WEATHER_FEATURES}])
                            wc_diff = float(model.weather_component(ty_f).iloc[0]
                                            - model.weather_component(ly_f).iloc[0])
                            yoy_weather_units = wc_diff * float(daily["stores"].mean())
                            easier = "easier" if yoy_weather_units < 0 else "tougher"
                            yoy_units_txt = (
                                f" That makes this year's weather worth about **{yoy_weather_units:+,.0f} "
                                f"units/day** vs last year — so the YoY comparison is **{easier}** on weather alone."
                            )
                        st.markdown("**Versus last year (same dates):**")
                        y1, y2, y3 = st.columns(3)
                        y1.metric("Temp vs last year", f"{ty_temp - ly_temp:+.1f}°F",
                                  help=f"This year {ty_temp:.0f}°F vs {ly_temp:.0f}°F last year")
                        y2.metric("Rain vs last year", f"{ty_precip - ly_precip:+.2f} in",
                                  help=f"This year {ty_precip:.2f} in/day vs {ly_precip:.2f} last year")
                        warmer = "warmer" if ty_temp >= ly_temp else "cooler"
                        drier = "drier" if ty_precip <= ly_precip else "wetter"
                        y3.metric("Weather backdrop", f"{warmer}, {drier}")
                        if yoy_units_txt:
                            st.caption(
                                f"You're lapping weather that was {('hotter' if ly_temp > ty_temp else 'cooler')} "
                                f"last year.{yoy_units_txt}"
                            )

                    # ════════════════════════════════════════════════════════════
                    #  2 · HOW WEATHER DRIVES YOUR SALES (THE MODEL)
                    # ════════════════════════════════════════════════════════════
                    st.divider()
                    st.markdown("### How weather drives your sales")

                    if model is not None:
                        st.markdown(f"**Model confidence: {model.confidence}** — {model.confidence_note}")
                        for note in model.notes:
                            st.caption(f"Note: {note}")
                        dl, dr = st.columns([2, 3])
                        with dl:
                            for bullet in model.driver_summary():
                                st.markdown(f"- {bullet}")
                        with dr:
                            band = wx_panel.copy()
                            band["temp_band"] = pd.cut(
                                band["temperature_max_f"], bins=[-100, 60, 70, 80, 90, 200],
                                labels=["<60°", "60–70°", "70–80°", "80–90°", "90°+"])
                            band["rain_cat"] = np.where(band["precipitation_in"] <= 0.05, "Dry day", "Wet day")
                            band_g = band.groupby(["temp_band", "rain_cat"], observed=True, as_index=False).agg(
                                vel=("units_per_store", "mean"), days=("units_per_store", "size"))
                            band_g = band_g[band_g["days"] >= 2]
                            band_chart = alt.Chart(band_g).mark_bar().encode(
                                x=alt.X("temp_band:N", title="Daily high temperature",
                                        sort=["<60°", "60–70°", "70–80°", "80–90°", "90°+"]),
                                xOffset="rain_cat:N",
                                y=alt.Y("vel:Q", title="Avg units / store / day"),
                                color=alt.Color("rain_cat:N", scale=alt.Scale(domain=["Dry day", "Wet day"],
                                                range=["#BA7517", "#185FA5"]), legend=alt.Legend(title=None)),
                                tooltip=[alt.Tooltip("temp_band:N", title="High temp"),
                                         alt.Tooltip("rain_cat:N", title="Conditions"),
                                         alt.Tooltip("vel:Q", format=".2f", title="Units/store/day"),
                                         alt.Tooltip("days:Q", title="Cell-days")],
                            ).properties(height=300, title="Velocity by heat — dry vs wet days")
                            st.altair_chart(band_chart, width='stretch')
                    else:
                        st.info(
                            "Not enough matched sales-and-weather days to fit a reliable model yet "
                            "(need ~14+). The forecast below falls back to a simple heat/dry-score rule."
                        )

                    # ════════════════════════════════════════════════════════════
                    #  3 · RECENT PERFORMANCE vs WEATHER EXPECTATION
                    # ════════════════════════════════════════════════════════════
                    st.divider()
                    st.markdown("### Recent sales vs what the weather predicted")
                    st.caption(
                        "The gold line is what the weather + day-of-week say each day *should* have done, "
                        "anchored to your average velocity. Bars above the line = you beat the weather "
                        "(strong execution / demand); below = you left sales on the table."
                    )

                    if model is not None:
                        daily["wc"] = model.weather_component(daily[["heat", "rain", "hot_dry"]])
                        daily["wknd_eff"] = (model.coef["weekend"]
                                             * daily["business_date"].dt.weekday.isin([5, 6]).astype(float))
                    else:
                        daily["wc"] = 0.0
                        daily["wknd_eff"] = 0.0
                    tot_stores = float(daily["stores"].sum())
                    base_vel_nat = (daily["units"].sum() / tot_stores) if tot_stores else 0.0
                    mean_wc = float((daily["wc"] * daily["stores"]).sum() / tot_stores) if tot_stores else 0.0
                    mean_wknd = float((daily["wknd_eff"] * daily["stores"]).sum() / tot_stores) if tot_stores else 0.0
                    daily["exp_vel"] = (base_vel_nat + (daily["wc"] - mean_wc)
                                        + (daily["wknd_eff"] - mean_wknd)).clip(lower=0)
                    daily["expected_units"] = daily["exp_vel"] * daily["stores"]
                    daily["residual"] = daily["units"] - daily["expected_units"]

                    last30 = daily.tail(30).copy()
                    exp_chart = alt.layer(
                        alt.Chart(last30).mark_bar(opacity=0.5, color="#185FA5").encode(
                            x=alt.X("business_date:T", title="Date"),
                            y=alt.Y("units:Q", title="Units sold"),
                            tooltip=[alt.Tooltip("business_date:T", title="Date"),
                                     alt.Tooltip("units:Q", format=",.0f", title="Actual units"),
                                     alt.Tooltip("expected_units:Q", format=",.0f", title="Weather-expected"),
                                     alt.Tooltip("residual:Q", format="+,.0f", title="Beat/missed by"),
                                     alt.Tooltip("temperature_max_f:Q", format=".0f", title="High F"),
                                     alt.Tooltip("precipitation_in:Q", format=".2f", title="Precip in")],
                        ),
                        alt.Chart(last30).mark_line(color="#BA7517", strokeWidth=2.5, point=True).encode(
                            x="business_date:T", y="expected_units:Q"),
                    ).properties(height=320)
                    st.altair_chart(exp_chart, width='stretch')

                    beat = daily.sort_values("residual", ascending=False).iloc[0]
                    miss = daily.sort_values("residual").iloc[0]
                    b1, b2 = st.columns(2)
                    b1.metric("Best vs weather", beat["business_date"].strftime("%b %d"),
                              f"{beat['residual']:+,.0f} units", help="Day you most beat what weather predicted.")
                    b2.metric("Softest vs weather", miss["business_date"].strftime("%b %d"),
                              f"{miss['residual']:+,.0f} units", delta_color="inverse",
                              help="Day you most underperformed the weather — worth a look (stock, promo, execution).")

                    # ════════════════════════════════════════════════════════════
                    #  4 · DEMAND FORECAST
                    # ════════════════════════════════════════════════════════════
                    st.divider()
                    st.markdown("### Demand forecast from the weather")

                    fc = pd.DataFrame()
                    cell_lift = pd.Series(dtype=float)
                    if win >= 2:
                        recent_panel = wx_panel[wx_panel["business_date"].isin(recent_dates)]
                        base_cell = recent_panel.groupby("cell_id", as_index=False).agg(
                            base_vel=("units_per_store", "mean"),
                            base_stores=("stores_selling", "mean"),
                            base_heat=("heat", "mean"), base_rain=("rain", "mean"),
                            base_hot_dry=("hot_dry", "mean"))
                        if model is not None:
                            base_cell["base_wc"] = (model.coef["heat"] * base_cell["base_heat"]
                                                    + model.coef["rain"] * base_cell["base_rain"]
                                                    + model.coef["hot_dry"] * base_cell["base_hot_dry"])
                        else:
                            base_cell["base_wc"] = 0.0
                        recent_wknd_share = float(
                            pd.to_datetime(pd.Series(recent_dates)).dt.weekday.isin([5, 6]).mean())
                        base_wknd = (model.coef["weekend"] * recent_wknd_share) if model is not None else 0.0

                        fc = weather_forecast[weather_forecast["date"] >= today_ts].merge(
                            base_cell, on="cell_id", how="inner")

                    if fc.empty:
                        st.info("Forecast weather loaded, but no forecast cells matched recent sales.")
                    else:
                        if model is not None and model.confidence != "Low":
                            fc["wc"] = model.weather_component(fc)
                            fc["wknd"] = (model.coef["weekend"]
                                          * pd.to_datetime(fc["date"]).dt.weekday.isin([5, 6]).astype(float))
                            fc["vel_pred"] = (fc["base_vel"] + (fc["wc"] - fc["base_wc"])
                                              + (fc["wknd"] - base_wknd)).clip(lower=0)
                        else:
                            hist_score = float((wx_panel["heat_dry_score"] * wx_panel["stores_selling"]).sum()
                                               / max(1.0, wx_panel["stores_selling"].sum()))
                            lift = (((fc["heat_dry_score"] - hist_score) / 100) * 0.15).clip(-0.35, 0.60)
                            fc["vel_pred"] = (fc["base_vel"] * (1 + lift)).clip(lower=0)
                        fc["units_pred"] = fc["vel_pred"] * fc["base_stores"]
                        fc["units_base"] = fc["base_vel"] * fc["base_stores"]

                        fw = fc.copy()
                        for c in ["temperature_max_f", "precipitation_in", "heat_dry_score"]:
                            fw[c + "_wsum"] = fw[c] * fw["base_stores"]
                        fc_daily = fw.groupby("date", as_index=False).agg(
                            units_pred=("units_pred", "sum"),
                            wstore=("base_stores", "sum"),
                            **{c + "_wsum": (c + "_wsum", "sum") for c in
                               ["temperature_max_f", "precipitation_in", "heat_dry_score"]})
                        for c in ["temperature_max_f", "precipitation_in", "heat_dry_score"]:
                            fc_daily[c] = fc_daily[c + "_wsum"] / fc_daily["wstore"]
                        fc_daily = fc_daily.sort_values("date").reset_index(drop=True)

                        recent_network_day = float(daily[daily["business_date"].isin(recent_dates)]["units"].mean())
                        covered_base_day = float((fc.drop_duplicates("cell_id")
                                                  .eval("base_vel * base_stores")).sum())
                        uncovered_day = max(0.0, recent_network_day - covered_base_day)
                        fc_daily["units_pred"] = fc_daily["units_pred"] + uncovered_day
                        fc_daily["baseline"] = recent_network_day
                        fc_daily["date_label"] = fc_daily["date"].dt.strftime("%a %b %d")

                        cov = (min(0.40, model.resid_std / max(0.1, model.units_per_store_mean))
                               if model is not None else 0.25)
                        fc_daily["lo"] = fc_daily["units_pred"] * (1 - cov)
                        fc_daily["hi"] = fc_daily["units_pred"] * (1 + cov)

                        order = list(fc_daily["date_label"])
                        fl, fr = st.columns([3, 2])
                        with fl:
                            outlook = alt.layer(
                                alt.Chart(fc_daily).mark_area(opacity=0.18, color="#27500A").encode(
                                    x=alt.X("date_label:N", sort=order, title="Forecast date",
                                            axis=alt.Axis(labelAngle=-35)),
                                    y=alt.Y("lo:Q", title="Predicted units/day"), y2="hi:Q"),
                                alt.Chart(fc_daily).mark_line(color="#27500A", strokeWidth=2.5, point=True).encode(
                                    x=alt.X("date_label:N", sort=order), y="units_pred:Q",
                                    tooltip=["date_label:N",
                                             alt.Tooltip("units_pred:Q", format=",.0f", title="Predicted"),
                                             alt.Tooltip("baseline:Q", format=",.0f", title="Recent run-rate"),
                                             alt.Tooltip("temperature_max_f:Q", format=".0f", title="High F"),
                                             alt.Tooltip("precipitation_in:Q", format=".2f", title="Precip in"),
                                             alt.Tooltip("heat_dry_score:Q", format=".0f", title="Heat/dry score")]),
                                alt.Chart(fc_daily).mark_rule(color="#A0A09A", strokeDash=[5, 4]).encode(
                                    y="baseline:Q"),
                            ).properties(height=340)
                            st.altair_chart(outlook, width='stretch')
                            st.caption(
                                "Solid line = weather-adjusted prediction · dashed grey = your recent run-rate · "
                                "shaded band = typical day-to-day variation."
                            )
                        with fr:
                            horizon = fc_daily.head(14)
                            pred_total = float(horizon["units_pred"].sum())
                            base_total = recent_network_day * len(horizon)
                            lift_pct = ((pred_total - base_total) / base_total * 100) if base_total else 0.0
                            best = fc_daily.sort_values("units_pred", ascending=False).iloc[0]
                            worst = fc_daily.sort_values("units_pred").iloc[0]
                            st.metric(f"Next {len(horizon)} days predicted", f"{pred_total:,.0f} units",
                                      f"{lift_pct:+.1f}% vs run-rate")
                            st.metric("Best day", best["date"].strftime("%a %b %d"),
                                      f"{best['units_pred'] / recent_network_day - 1:+.0%} vs run-rate"
                                      if recent_network_day else "")
                            st.metric("Softest day", worst["date"].strftime("%a %b %d"),
                                      f"{worst['units_pred'] / recent_network_day - 1:+.0%} vs run-rate"
                                      if recent_network_day else "", delta_color="inverse")
                            if lift_pct >= 8:
                                st.success(
                                    "**Hot/dry stretch ahead.** Expect demand above your recent run-rate — "
                                    "pull inventory forward and keep your top-volume stores stocked.")
                            elif lift_pct <= -8:
                                st.warning(
                                    "**Cooler/wetter stretch ahead.** Expect softer sell-through — ease off "
                                    "reorders and watch for overstock building in stores.")
                            else:
                                st.info(
                                    "**Weather looks neutral** vs your recent run-rate — no big weather swing "
                                    "to plan around in the next two weeks.")

                        show_fc = fc_daily[["date_label", "units_pred", "baseline",
                                            "temperature_max_f", "precipitation_in"]].copy()
                        show_fc.columns = ["Date", "Predicted", "Run-rate", "High F", "Precip"]
                        st.dataframe(show_fc, width='stretch', hide_index=True, height=300,
                                     column_config={
                                         "Predicted": st.column_config.NumberColumn(format="%.0f"),
                                         "Run-rate": st.column_config.NumberColumn(format="%.0f"),
                                         "High F": st.column_config.NumberColumn(format="%.0f"),
                                         "Precip": st.column_config.NumberColumn(format="%.2f in"),
                                     })

                        # Per-cell next-7-day lift factor, for store/state roll-ups below.
                        fc7 = fc[fc["date"] <= today_ts + pd.Timedelta(days=6)]
                        cl = fc7.groupby("cell_id", as_index=False).agg(
                            vp=("vel_pred", "sum"), bv=("base_vel", "sum"))
                        cl["lift"] = np.where(cl["bv"] > 0, (cl["vp"] / cl["bv"]).clip(0.5, 2.0), 1.0)
                        cell_lift = cl.set_index("cell_id")["lift"]

                    # ════════════════════════════════════════════════════════════
                    #  5 · WHERE WEATHER MATTERS (MAP + STATE ROLL-UP)
                    # ════════════════════════════════════════════════════════════
                    st.divider()
                    st.markdown("### Where the weather matters")

                    store_recent = (dfx[dfx["business_date"].isin(recent_dates)]
                                    .groupby("store_number")["pos_quantity_this_year"].sum()
                                    / max(1, len(recent_dates)))
                    store_pts = geo_small[geo_small["store_number"].isin(
                        dfx["store_number"].dropna().unique())].copy()
                    store_pts["recent_units"] = store_pts["store_number"].map(store_recent).fillna(0.0)
                    store_pts["lift"] = store_pts["cell_id"].map(cell_lift).fillna(1.0) if len(cell_lift) else 1.0
                    store_pts["pred_units"] = store_pts["recent_units"] * store_pts["lift"]

                    map_l, map_r = st.columns(2)
                    with map_l:
                        st.markdown("**Recent volume by store**")
                        mx = max(1.0, store_pts["recent_units"].max())
                        rmap = store_pts[store_pts["recent_units"] > 0].copy()
                        rmap["sz"] = rmap["recent_units"] / mx * 9000 + 1200
                        st.map(rmap, latitude="lat", longitude="lon", size="sz")
                        st.caption("Each dot is a store; size = recent units/day.")
                    with map_r:
                        st.markdown("**Next 7 days predicted demand**")
                        if len(cell_lift):
                            mxp = max(1.0, store_pts["pred_units"].max())
                            pmap = store_pts[store_pts["pred_units"] > 0].copy()
                            pmap["sz"] = pmap["pred_units"] / mxp * 9000 + 1200
                            st.map(pmap, latitude="lat", longitude="lon", size="sz")
                            st.caption("Size = weather-adjusted predicted units/day for the next 7 days.")
                        else:
                            st.info("Forecast map appears once a usable prediction is built.")

                    if len(cell_lift):
                        store_state = dfx.groupby("store_number")["state_or_province_code"].first()
                        cell_wx7 = fc7.groupby("cell_id", as_index=False).agg(
                            temp=("temperature_max_f", "mean"), precip=("precipitation_in", "mean"))
                        stbl = pd.DataFrame({"store_number": store_recent.index,
                                             "recent_per_day": store_recent.values})
                        stbl["cell_id"] = stbl["store_number"].map(geo_small.set_index("store_number")["cell_id"])
                        stbl["state"] = stbl["store_number"].map(store_state)
                        stbl["lift"] = stbl["cell_id"].map(cell_lift).fillna(1.0)
                        stbl = stbl.merge(cell_wx7, on="cell_id", how="left")
                        stbl["base7"] = stbl["recent_per_day"] * 7
                        stbl["pred7"] = stbl["base7"] * stbl["lift"]
                        stbl["temp_w"] = stbl["temp"] * stbl["base7"]
                        stbl["precip_w"] = stbl["precip"] * stbl["base7"]
                        state_tw = stbl.dropna(subset=["state"]).groupby("state", as_index=False).agg(
                            base7=("base7", "sum"), pred7=("pred7", "sum"),
                            stores=("store_number", "nunique"),
                            temp_w=("temp_w", "sum"), precip_w=("precip_w", "sum"))
                        state_tw = state_tw[state_tw["base7"] > 0].copy()
                        state_tw["temp"] = state_tw["temp_w"] / state_tw["base7"]
                        state_tw["precip"] = state_tw["precip_w"] / state_tw["base7"]
                        state_tw["tailwind_pct"] = (state_tw["pred7"] / state_tw["base7"] - 1) * 100
                        state_tw = state_tw.sort_values("tailwind_pct", ascending=False)
                        show_tw = state_tw[["state", "tailwind_pct", "pred7", "stores", "temp", "precip"]].copy()
                        show_tw.columns = ["State", "Weather tailwind %", "Predicted (7d)",
                                           "Stores", "Avg High F", "Avg Precip"]
                        st.markdown("**State weather tailwind / headwind — next 7 days**")
                        st.dataframe(show_tw, width='stretch', hide_index=True, height=320,
                                     column_config={
                                         "Weather tailwind %": st.column_config.NumberColumn(format="%+.1f%%"),
                                         "Predicted (7d)": st.column_config.NumberColumn(format="%.0f"),
                                         "Avg High F": st.column_config.NumberColumn(format="%.0f"),
                                         "Avg Precip": st.column_config.NumberColumn(format="%.2f in"),
                                     })
                        st.caption(
                            "Tailwind % = predicted next-7-day demand vs each state's recent run-rate, from the "
                            "weather model. Positive = weather is helping that state; negative = hurting."
                        )


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
