"""
streamlit_app.py — FunPop Sales Dashboard

Tab structure:
  Overview          — KPIs, last 10 days, item performance, stockout risk (incl.
                      estimated lost sales $ and in-stock % service level)
  Sales & Velocity  — Weekly sales, AUR/price trend, YoY units bridge (distribution
                      vs velocity), U/S/W, velocity-tier heatmap, store leaderboard
  Sales Drivers     — Why sales are up/down vs LY for the selected window: a
                      distribution/velocity/price sales-dollar bridge, a demand-vs-
                      availability (stockout) diagnosis that weighs the inventory
                      feed against sales, item/state/store contributors, and the
                      weekly trajectory (sales gap vs in-stock rate)
  Forecast          — Upcoming demand forecast vs sales, attainment & bias, store
                      replenishment watchlist, and DC demand coverage
  Inventory & DC    — Weekly inventory, phantom inventory, DC pipeline (true alignment)
  Channels          — Omni sales (+ channel-mix over time), eComm inventory, store
                      returns (+ returns by reason)
  Distribution      — Modular coverage, state performance (choropleth + ranked bar)
  Store Actions     — Flags stores with a rep-fixable problem over the last 7 days
                      (phantom/backroom stock, sales collapse, chronic OOS) and
                      exports a ranked dispatch list with mailing address for a
                      field-service vendor

Data sources (BigQuery, dv_supplier dataset):
  store_sales + store_invt + store_dim + item_dim ─→ load_store_data
  store_dim (addresses, static committed directory) ─→ load_store_directory
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

import io
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
import store_directory
import transforms

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
        div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
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
    /* KPI metrics as bordered cards — groups each number with its label/delta
       so a strip of five reads as five tiles, not floating digits. Translucent
       grey keeps it legible in both light and dark themes. */
    div[data-testid="stMetric"] {
        background-color: rgba(128, 128, 128, 0.06);
        border: 1px solid rgba(128, 128, 128, 0.18);
        border-radius: 10px;
        padding: 0.6rem 0.9rem;
    }
    /* Long metric labels wrap instead of truncating with an ellipsis. */
    div[data-testid="stMetric"] label {
        white-space: normal;
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

# If the repo is ever made private, the raw-URL snapshot downloads need a
# token; allow one via secrets without a code change. Public repo: leave unset.
try:
    _snap_token = st.secrets.get("snapshot_remote_token", "")
    if _snap_token:
        snapshot.SNAPSHOT_REMOTE_TOKEN = _snap_token
except Exception:  # noqa: BLE001 - secrets file may be absent locally
    pass


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


def _standard_lookback(d) -> int:
    """The Standard view's rolling lookback for date ``d`` (Central): 4 full
    Walmart weeks (Sat-Fri) + days into the current week. The +1 keeps today's
    date inside the ``bus_dt >= today - lookback`` filter even though the feed
    lags a day, so the window starts exactly on the right Saturday. Mirrored by
    snapshot_build.lookback_for_today so the snapshot key matches."""
    return 27 + ((d.weekday() - 5) % 7) + 1


def _read_prior_day_snapshot(sql_filename: str, params):
    """Early-morning snapshot fallback. The snapshot key embeds the day's
    Standard lookback, which changes at midnight Central — but the matching
    parquet isn't published until the ~6-9am build runs. Without this, every
    visit between midnight and the build paid the full live multi-query load.
    Yesterday's still-fresh snapshot is what a live pull would return anyway
    until the feed lands (~7am), so serve it instead. Restricted to before 8am
    (after that, live is plausibly fresher) and to the rolling Standard
    lookback — a custom slider value never remaps to a different key."""
    now = datetime.now(CENTRAL_TZ)
    if now.hour >= 8:
        return None
    lb = next((p for p in params if getattr(p, "name", "") == "lookback_days"), None)
    if lb is None or lb.value != _standard_lookback(now.date()):
        return None
    prior_lb = _standard_lookback(now.date() - timedelta(days=1))
    alt = [bigquery.ScalarQueryParameter("lookback_days", "INT64", prior_lb)
           if getattr(p, "name", "") == "lookback_days" else p
           for p in params]
    return snapshot.read_snapshot(snapshot.snapshot_key(sql_filename, alt))


def _cached_query(sql_filename: str, params=None, meta: dict | None = None) -> pd.DataFrame:
    """Fetch a query result, preferring the durable BigQuery snapshot written
    by snapshot_build.py (run on a schedule by GitHub Actions) over a live pull.

    Community Cloud drops in-memory @st.cache_data whenever the app process
    restarts, so without this a visitor arriving after a restart pays the full
    multi-query cost — that's why the page wasn't already loaded at 8am. The
    snapshot makes a cold load fast and survives restarts. Any snapshot
    miss/staleness/error falls through to a live query, so behaviour is never
    worse than a direct pull.

    When ``meta`` is given, meta["source"] is set to "snapshot" or "live" so
    the caller can report where the data actually came from.

    The sidebar "Refresh data" button sets _force_live_refresh so it bypasses
    the snapshot and still confirms the very latest data."""
    params = params or []
    if not _FORCE_LIVE_REFRESH:
        try:
            df = snapshot.read_snapshot(snapshot.snapshot_key(sql_filename, params))
            if df is None:
                df = _read_prior_day_snapshot(sql_filename, params)
            if df is not None:
                if meta is not None:
                    meta["source"] = "snapshot"
                return df
        except Exception as e:  # noqa: BLE001 - snapshot is best-effort
            logger.warning("snapshot lookup skipped for %s: %s", sql_filename, e)
    if meta is not None:
        meta["source"] = "live"
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
    meta = {}
    df = _cached_query("store_query.sql", [
        bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
    ], meta=meta)
    # Record what this cache miss actually did: a live BigQuery pull is stamped
    # "now", but a snapshot read is stamped with the snapshot's own build time —
    # the header must never claim a fresh pull for hours-old snapshot data.
    state = _freshness_state()
    if meta.get("source") == "snapshot":
        built = snapshot.built_at()
        state["last_refresh_at"] = built.isoformat() if built else None
        state["last_refresh_source"] = "snapshot"
    else:
        state["last_refresh_at"] = datetime.now(CENTRAL_TZ).isoformat()
        state["last_refresh_source"] = "live pull"
    # Normalise + shrink dtypes. This is the dashboard's heaviest frame (~544k
    # rows): a raw BigQuery read boxes its NUMERIC columns as Python Decimals and
    # balloons to ~375 MB, so this step both halves the cached footprint (keeping
    # the app under Community Cloud's RAM cap) and is the bulk of cold-start cost.
    # The scheduled snapshot builder now applies the same idempotent transform
    # before committing the parquet, so a cold start reads an already-shrunk
    # ~31 MB frame and this call is a cheap no-op (~4,100 ms → ~95 ms); a live
    # pull or an older un-shrunk snapshot still gets fully processed here.
    return transforms.process_store_frame(df)


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
            df[c] = (df[c] * multiplier).round().astype("int32")
        # OOS columns are already eaches; shrink to int32 and drop the pack-size
        # helper column now that the multiplier has been applied (memory hygiene).
        for c in ["out_of_stock_each_quantity_this_year", "out_of_stock_each_quantity_last_year"]:
            df[c] = df[c].round().astype("int32")
        df = df.drop(columns=["warehouse_pack_each_quantity"], errors="ignore")
        for c in ["walmart_item_number", "distribution_center_number"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int32")
        # name_of_the_dc is deliberately left as object, not category: it's a
        # groupby key alongside the (non-category) DC number in two sections, and
        # a categorical key there would fan out into phantom name×number combos.
        return df
    except Exception as e:
        _section_error("DC data", e)
        return pd.DataFrame()


# ─── Secondary loaders (fault-tolerant) ──────────────────────────────────────
# Shared with the snapshot builder via transforms.py so the two never drift; see
# that module for why dtype-shrinking matters (Community Cloud RAM cap).
_shrink_frame = transforms.shrink_frame


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_dc_alignment(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("dc_alignment_query.sql")
        # alignment_type is left as-is (not categoricalized): it drives the
        # deterministic primary-DC sort, and the frame is small anyway.
        if not df.empty:
            _shrink_frame(df, int_cols=["store_number", "distribution_center_number"])
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
        # Shared with the snapshot builder (transforms.py): forecast_quantity
        # arrives as boxed Decimals over ~273k rows, so the builder pre-shrinks
        # this frame too and the cold-start read here is a cheap no-op.
        df = transforms.process_forecast_frame(df)
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
            _shrink_frame(df,
                int_cols=["walmart_item_number", "walmart_calendar_week", "units_ty", "units_ly"],
                float_cols=["sales_ty", "sales_ly"],
                cat_cols=["order_channel", "service_channel"])
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
            _shrink_frame(df,
                int_cols=["walmart_item_number", "on_hand_units", "available_to_sell", "on_hand_units_ly"],
                cat_cols=["ship_node"])
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
            _shrink_frame(df,
                int_cols=["store_number", "walmart_item_number", "returns_ty", "returns_ly"],
                float_cols=["return_sales_ty", "return_sales_ly"],
                cat_cols=["return_reason"])
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_modular_data(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    try:
        df = _cached_query("modular_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
        ])
        if not df.empty:
            _shrink_frame(df, int_cols=["store_number", "walmart_item_number"],
                          cat_cols=["item_valid_flag"])
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
            _shrink_frame(df,
                int_cols=["store_number", "walmart_item_number", "adjustment_qty_ty", "adjustment_qty_ly"],
                cat_cols=["adjustment_type", "adjustment_description"])
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_store_lookahead_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    """Same-period-last-year store inventory (in-store on-hand, in-warehouse, and
    in-transit) for the Inventory tab's week look-ahead. The main store frame only
    spans the recent lookback, so last year's levels for the upcoming week aren't
    in it; this dedicated query fetches them at date×item×state grain. The app
    shifts the dates forward 52 weeks and combines them with this year's actuals.
    Small frame, so no pre-shrink transform is registered for it."""
    try:
        df = _cached_query("store_lookahead_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if not df.empty:
            df["inventory_date"] = pd.to_datetime(df["inventory_date"])
            _shrink_frame(df,
                int_cols=["walmart_item_number", "store_on_hand_quantity",
                          "store_in_warehouse_quantity", "store_in_transit_quantity"],
                cat_cols=["state_or_province_code"])
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=86400, max_entries=3, show_spinner=False)
def load_dc_lookahead_data(lookback_days: int, refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    """Same-period-last-year DC on-hand (eaches) for the Inventory tab's week
    look-ahead, at date×item grain (network-wide, like the rest of the DC views).
    Converts warehouse packs → eaches the same way as load_dc_data: prefer the
    feed's per-row eaches-per-pack, fall back to constants.CASE_PACK_UNITS only
    where it's missing. Not registered in SNAPSHOT_TRANSFORMS — like dc_query.sql,
    the pack→eaches conversion isn't idempotent (it drops the pack-size column it
    depends on), so it must run exactly once, here, after the snapshot read."""
    try:
        df = _cached_query("dc_lookahead_query.sql", [
            bigquery.ArrayQueryParameter("active_items", "INT64", ACTIVE_ITEMS),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ])
        if df.empty:
            return df, None
        df["inventory_date"] = pd.to_datetime(df["inventory_date"])
        oh = pd.to_numeric(
            df["on_hand_warehouse_inventory_in_units_this_year"], errors="coerce").fillna(0)
        if "warehouse_pack_each_quantity" in df.columns:
            pack_each = pd.to_numeric(df["warehouse_pack_each_quantity"], errors="coerce").fillna(0)
            fallback = df["walmart_item_number"].map(CASE_PACK_UNITS).fillna(1)
            multiplier = pack_each.where(pack_each > 0, fallback)
        else:
            multiplier = df["walmart_item_number"].map(CASE_PACK_UNITS).fillna(1)
        df["dc_on_hand"] = (oh * multiplier).round().astype("int32")
        df = df.drop(columns=["warehouse_pack_each_quantity",
                              "on_hand_warehouse_inventory_in_units_this_year"], errors="ignore")
        _shrink_frame(df, int_cols=["walmart_item_number"])
        return df, None
    except Exception as e:
        return pd.DataFrame(), str(e)


# ─── Store directory (mailing addresses for the field-ops export) ────────────
# The daily store_query intentionally drops store name/city/address to keep the
# heavy ~550k-row frame small (see load_store_data), so it only carries the
# state code. The Store Actions tab's vendor export needs a full mailing address
# per store, so we pull the store dimension separately. Addresses are static
# reference data, so the scheduled snapshot job builds this once and commits it
# (store_directory.DIRECTORY_PATH); the app reads that committed parquet straight
# from disk and only queries BigQuery as a fallback when the file is absent.
#
# The build logic (INFORMATION_SCHEMA introspection + column matching) lives in
# the import-safe store_directory module so the snapshot builder can share it.
@st.cache_data(ttl=86400, max_entries=2, show_spinner=False)
def load_store_directory(refresh_slot: str = "") -> tuple[pd.DataFrame, str | None]:
    # Prefer the static committed directory: addresses don't change, so the
    # snapshot job builds it once and publishes it (local file in a dev
    # checkout, snapshot-data branch in deployment) — no live query on cold
    # start. It is read directly (not via the freshness-gated snapshot reader)
    # because a stale address is still correct.
    try:
        if store_directory.DIRECTORY_PATH.exists():
            df = pd.read_parquet(store_directory.DIRECTORY_PATH)
        else:
            raw = snapshot.fetch_remote(store_directory.DIRECTORY_PATH.name)
            df = pd.read_parquet(io.BytesIO(raw)) if raw is not None else pd.DataFrame()
        if not df.empty:
            return store_directory.clean_directory_df(df), None
    except Exception as e:  # noqa: BLE001 - a bad file must fall back to live
        logger.warning("store directory snapshot read failed: %s", e)
    # Fallback: build it live (until the snapshot job has committed the file).
    try:
        project = _get_sa_info()["project_id"]
        dataset = st.secrets["bigquery"]["dataset"]
        return store_directory.build_directory_df(_run_query, project, dataset), None
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


# US state/territory → Census FIPS id, used to join state POS aggregates onto the
# us-10m TopoJSON for the choropleth on the Distribution tab. The TopoJSON keys
# features by these numeric ids; the feed gives us 2-letter codes, so we bridge.
STATE_FIPS = {
    "AL": 1, "AK": 2, "AZ": 4, "AR": 5, "CA": 6, "CO": 8, "CT": 9, "DE": 10,
    "DC": 11, "FL": 12, "GA": 13, "HI": 15, "ID": 16, "IL": 17, "IN": 18,
    "IA": 19, "KS": 20, "KY": 21, "LA": 22, "ME": 23, "MD": 24, "MA": 25,
    "MI": 26, "MN": 27, "MS": 28, "MO": 29, "MT": 30, "NE": 31, "NV": 32,
    "NH": 33, "NJ": 34, "NM": 35, "NY": 36, "NC": 37, "ND": 38, "OH": 39,
    "OK": 40, "OR": 41, "PA": 42, "RI": 44, "SC": 45, "SD": 46, "TN": 47,
    "TX": 48, "UT": 49, "VT": 50, "VA": 51, "WA": 53, "WV": 54, "WI": 55,
    "WY": 56, "PR": 72,
}
# Client-side fetch (Vega renders in the browser, so the server never reaches out).
US_STATES_TOPO_URL = "https://vega.github.io/vega-datasets/data/us-10m.json"


def _waterfall_chart(steps: list, value_fmt: str = ",.0f", height: int = 300,
                     y_title: str = "Units", currency: bool = False):
    """Build a left-to-right waterfall from a list of {label, amount, kind} dicts.
    kind='total' draws a full-height bar from zero (anchors); kind='delta' floats
    from the running total. Used for the YoY units bridge (LY → distribution →
    velocity → TY) and the Sales Drivers sales-dollar bridge. Pass currency=True
    to label/tooltip amounts as dollars and y_title to relabel the axis; the
    defaults keep the original unit-bridge behaviour."""
    def _label(amt):
        if currency:
            return f"{'+' if amt >= 0 else '-'}${abs(amt):,.0f}"
        return f"{amt:+{value_fmt}}"
    tip_fmt = "$,.0f" if currency else value_fmt
    running = 0.0
    rows = []
    for s in steps:
        if s["kind"] == "total":
            start, end, running = 0.0, s["amount"], s["amount"]
            band = "Total"
        else:
            start = running
            running += s["amount"]
            end = running
            band = "Up" if s["amount"] >= 0 else "Down"
        rows.append({**s, "start": start, "end": end, "band": band,
                     "label_amt": ("" if s["kind"] == "total" else _label(s["amount"]))})
    wf = pd.DataFrame(rows)
    order = list(wf["label"])
    base = alt.Chart(wf).encode(
        x=alt.X("label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)))
    bars = base.mark_bar(size=46).encode(
        y=alt.Y("start:Q", title=y_title),
        y2="end:Q",
        color=alt.Color("band:N", scale=alt.Scale(
            domain=["Total", "Up", "Down"], range=["#185FA5", "#27500A", "#791F1F"]),
            legend=None),
        tooltip=[alt.Tooltip("label:N", title=""),
                 alt.Tooltip("amount:Q", format=tip_fmt, title="Effect"),
                 alt.Tooltip("end:Q", format=tip_fmt, title="Cumulative")])
    labels = base.mark_text(dy=-6, color="#333", fontWeight="bold").encode(
        y=alt.Y("end:Q"), text=alt.Text("label_amt:N"))
    return (bars + labels).properties(height=height)


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
        _today_central = datetime.now(CENTRAL_TZ).date()
        _days_since_sat = (_today_central.weekday() - 5) % 7  # 0 on a Saturday
        # Window starts on the Saturday 4 weeks before the current WM week;
        # see _standard_lookback for the formula. Because the feed lags a day,
        # the window holds exactly `lookback` days of data: 28 for the 4 full
        # weeks + `_days_since_sat` of the current week.
        lookback = _standard_lookback(_today_central)
        st.caption(
            f"📅 Showing **4 full Walmart weeks + {_days_since_sat} day(s)** "
            f"of the current week ({lookback} calendar days)"
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
        help="Scopes the KPI strips and the item / store / state / channel summary "
             "tables on every tab. Trend charts (weekly and daily series) always "
             "span the full date range so the window has context.",
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

# ─── Geography filter ────────────────────────────────────────────────────────
# Rendered after the primary load because the option list comes from the data.
# Scopes every store-level view (Overview through Store Actions) to the chosen
# states; DC sections stay network-wide since one DC serves stores in many states.
with st.sidebar:
    st.divider()
    _state_opts = sorted(df_all["state_or_province_code"].astype(str).unique())
    state_sel = st.multiselect(
        "States (empty = all)",
        options=_state_opts,
        default=[],
        help="Scope the dashboard to specific states. DC sections remain "
             "network-wide — a DC serves stores across several states.",
    )
if state_sel:
    df_all = df_all[df_all["state_or_province_code"].isin(state_sel)].copy()
    if df_all.empty:
        st.warning("No store data for the selected states.")
        st.stop()

# Apply item filter. st.cache_data hands back a fresh copy of df_all on every
# run, so the Total view (a no-op filter — the query is already restricted to
# ACTIVE_ITEMS) can use it directly; only a real subset needs its own copy.
# Skipping the redundant copy avoids duplicating the heaviest frame in memory.
if item_view == "Total (all items)":
    df = df_all
else:
    df = df_all[df_all["walmart_item_number"].isin(item_filter)].copy()
# Coarse display group (Bins = Full + Half) for the per-item breakouts that
# shouldn't split the two bin packs. Carried into df_window via its slices.
# Stored as category: as plain object strings this column alone is ~15 MB
# across the full frame.
df["item_group"] = df["walmart_item_number"].map(item_group_label).astype("category")
dc_df = (dc_df_all if dc_df_all.empty or item_view == "Total (all items)"
         else dc_df_all[dc_df_all["walmart_item_number"].isin(item_filter)])

if df.empty:
    st.warning("No data matches the current item filter.")
    st.stop()

most_recent = df["business_date"].max()
period_days = max(1, df["business_date"].dt.normalize().nunique())
weeks_in_period = max(1 / 7, period_days / 7)

# Compute the performance window slice (read-only views — no copy needed)
if perf_window == "Most recent day":
    df_window = df[df["business_date"] == most_recent]
    window_label = f"Day of {most_recent.strftime('%b %d, %Y')}"
    weeks_in_window = 1 / 7
elif perf_window == "Last 7 days":
    cutoff_7d = most_recent - timedelta(days=6)
    df_window = df[df["business_date"] >= cutoff_7d]
    window_label = f"Last 7 days ({cutoff_7d.strftime('%b %d')}–{most_recent.strftime('%b %d')})"
    weeks_in_window = 1.0
else:
    df_window = df
    window_label = f"Full lookback ({df['business_date'].min().strftime('%b %d')}–{most_recent.strftime('%b %d')}, {period_days} data days)"
    weeks_in_window = weeks_in_period

# First day of the performance window. Secondary feeds (omni, returns) carry
# their own date columns, so sections built from them apply the same calendar
# window with `frame[date_col] >= window_start` instead of re-deriving it.
window_start = df_window["business_date"].min()


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
last_refresh_source = _freshness_state().get("last_refresh_source")
if last_refresh_iso:
    try:
        last_refresh_dt = datetime.fromisoformat(last_refresh_iso)
        # Convert to Central if needed; isoformat preserves tz
        if last_refresh_dt.tzinfo is None:
            last_refresh_dt = last_refresh_dt.replace(tzinfo=CENTRAL_TZ)
        last_refresh_str = last_refresh_dt.astimezone(CENTRAL_TZ).strftime("%b %d, %Y at %I:%M %p %Z")
        if last_refresh_source:
            # "snapshot" = stamped with the scheduled build's own time, not the
            # moment it was read — so the stamp is honest either way.
            last_refresh_str += f" ({last_refresh_source})"
    except Exception:
        last_refresh_str = "unknown"
else:
    last_refresh_str = "not yet (cached)"

_state_note = (f"  ·  {len(state_sel)} state{'s' if len(state_sel) != 1 else ''} selected"
               if state_sel else "")
st.caption(
    f"**Last refreshed:** {last_refresh_str}  ·  "
    f"**Data range:** {df['business_date'].min().strftime('%b %d, %Y')} – "
    f"{most_recent.strftime('%b %d, %Y')} ({period_days} data days){freshness_note}  ·  "
    f"{df['store_number'].nunique():,} stores  ·  {item_view}{_state_note}  \n"
    f"Walmart's BI Link feed runs on a 1-day lag, so the most recent data point is "
    f"**{most_recent.strftime('%b %d')}**. Auto-refresh runs hourly from 6am Central "
    f"until new data lands."
)


# ─── Tabs ────────────────────────────────────────────────────────────────────
tab_overview, tab_sales, tab_drivers, tab_forecast, tab_inv, tab_channels, tab_dist, tab_actions = st.tabs([
    "📊 Overview",
    "📈 Sales & Velocity",
    "🔍 Sales Drivers",
    "🔮 Forecast",
    "📦 Inventory & DC",
    "🛒 Channels",
    "🗺️ Distribution",
    "🚩 Store Actions",
])


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 1 — OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════
# Each tab body is an st.fragment: a widget inside a tab (scope radio, slider,
# search box) reruns only that tab's code instead of recomputing all eight tabs'
# aggregations over the ~544k-row frame — the difference between a sub-second
# and a multi-second interaction. Sidebar widgets still trigger a full rerun.
@st.fragment
def _render_overview():
    # ── Executive alert strip ────────────────────────────────────────────────
    # Auto-generated "what changed" headlines so a GM gets the signal before the
    # detail. Everything here is derived from the store frame already in memory —
    # no extra queries. Alerts are prioritised by severity; the top few render.
    _alerts = []  # (priority, kind, message); lower priority = more urgent

    _sa_ty = float(df_window["pos_sales_this_year"].sum())
    _sa_ly = float(df_window["pos_sales_last_year"].sum())
    _sa_yoy = ((_sa_ty - _sa_ly) / _sa_ly * 100) if _sa_ly else 0.0
    if _sa_ly:
        if _sa_yoy <= -10:
            _alerts.append((0, "error", f"**Sales down {_sa_yoy:.0f}% YoY** this period (${_sa_ty:,.0f} vs ${_sa_ly:,.0f})."))
        elif _sa_yoy < -2:
            _alerts.append((2, "warning", f"Sales softening — **{_sa_yoy:.0f}% YoY** (${_sa_ty:,.0f})."))
        elif _sa_yoy >= 10:
            _alerts.append((5, "success", f"**Sales up {_sa_yoy:.0f}% YoY** this period (${_sa_ty:,.0f})."))

    _snap0 = df[df["business_date"] == most_recent]
    _soh0 = _snap0.groupby("store_number")["store_on_hand_quantity_this_year"].sum()
    _tot0 = int(_soh0.shape[0]); _oos0 = int((_soh0 == 0).sum())
    _instock0 = (100 * (1 - _oos0 / _tot0)) if _tot0 else 100.0
    if _tot0:
        if _instock0 < 90:
            _alerts.append((1, "error", f"**In-stock {_instock0:.0f}%** — {_oos0:,} of {_tot0:,} stores out of stock today."))
        elif _instock0 < 95:
            _alerts.append((3, "warning", f"In-stock {_instock0:.0f}% — {_oos0:,} stores OOS today (below the 95% target)."))
        else:
            _alerts.append((6, "success", f"In-stock {_instock0:.0f}% — service level healthy."))

    _vu_ty = float(df["pos_quantity_this_year"].sum()); _vu_ly = float(df["pos_quantity_last_year"].sum())
    _vs_ty = df[df["pos_quantity_this_year"] > 0]["store_number"].nunique()
    _vs_ly = df[df["pos_quantity_last_year"] > 0]["store_number"].nunique()
    _vr_ty = (_vu_ty / _vs_ty) if _vs_ty else 0.0
    _vr_ly = (_vu_ly / _vs_ly) if _vs_ly else 0.0
    _vr_yoy = ((_vr_ty - _vr_ly) / _vr_ly * 100) if _vr_ly else 0.0
    if _vr_ly and _vr_yoy <= -10:
        _alerts.append((2, "warning", f"Per-store velocity **{_vr_yoy:.0f}% YoY** — sell-through weakening, not just distribution."))
    if _vs_ly and (_vs_ty - _vs_ly) <= -0.05 * _vs_ly:
        _alerts.append((2, "warning", f"**{_vs_ly - _vs_ty:,} fewer stores selling** vs LY ({_vs_ty:,} vs {_vs_ly:,}) — distribution slipping."))

    _stp = df.groupby("state_or_province_code", observed=True).agg(
        ty=("pos_quantity_this_year", "sum"), ly=("pos_quantity_last_year", "sum")).reset_index()
    _stp = _stp[_stp["ly"] > 50]
    if not _stp.empty:
        _stp["d"] = _stp["ty"] - _stp["ly"]
        _w = _stp.sort_values("d").iloc[0]
        if _w["d"] < 0 and _w["ly"]:
            _alerts.append((4, "warning", f"**{_w['state_or_province_code']}** is the weakest state — units {((_w['ty'] - _w['ly']) / _w['ly'] * 100):.0f}% YoY."))

    if _alerts:
        _kindfn = {"error": st.error, "warning": st.warning, "success": st.success, "info": st.info}
        _icon = {"error": "🔴", "warning": "🟡", "success": "🟢", "info": "ℹ️"}
        st.markdown("#### 📌 What needs attention")
        for _p, _k, _m in sorted(_alerts, key=lambda a: a[0])[:4]:
            _kindfn[_k](_m, icon=_icon[_k])
        st.divider()

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
    last10 = df[df["business_date"] >= cutoff_10d]

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
    # display group (Bins = Full on-hand + Half on-hand). Single masked pass
    # instead of re-slicing the frame once per item.
    _latest_mask = (df.groupby("walmart_item_number")["business_date"].transform("max")
                    == df["business_date"])
    _oh_by_item = df[_latest_mask].groupby("walmart_item_number")[
        "store_on_hand_quantity_this_year"].sum()
    group_oh = {}
    group_items = {}
    for item, oh in _oh_by_item.items():
        g = item_group_label(int(item))
        group_oh[g] = group_oh.get(g, 0) + int(oh)
        group_items.setdefault(g, []).append(int(item))

    item_perf = df_window.groupby("item_group", as_index=False, observed=True).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        sales_ly=("pos_sales_last_year", "sum"),
    ).rename(columns={"item_group": "item"})
    # item_group is categorical (memory win on the big frame); cast the 2-3
    # aggregated rows back to str so .map(...).fillna(0) below doesn't raise
    # pandas' "Cannot setitem on a Categorical with a new category" error.
    item_perf["item"] = item_perf["item"].astype(str)
    item_perf["sales_yoy_pct"] = _yoy_pct(item_perf["sales_ty"], item_perf["sales_ly"])
    item_perf["on_hand"] = item_perf["item"].map(group_oh).fillna(0).astype(int)
    item_perf["yoy_pct"] = _yoy_pct(item_perf["units_ty"], item_perf["units_ly"])
    item_perf["yoy_units"] = item_perf["units_ty"] - item_perf["units_ly"]
    full_units_per_group = df.groupby("item_group", observed=True)["pos_quantity_this_year"].sum()
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
    oos_daily["in_stock_pct"] = (100 - oos_daily["oos_pct"]).round(1)

    # ── Estimated lost sales from stockouts (last 7 days) ────────────────────
    # For every OOS store-day in the last 7 days, credit the store's OWN average
    # daily velocity over the window (so we never invent demand for a store that
    # wouldn't have moved product anyway), priced at the blended average unit
    # retail. Conservative by construction: a store with no sales in the window
    # has zero velocity and contributes nothing.
    recent_cut7 = most_recent - timedelta(days=6)
    # True average daily velocity per store: window units ÷ window data days
    # (zero-sale days included). A per-row mean here would average
    # store-day-item rows — not a daily figure, and it undercounts multi-item
    # stores. Stores with no sales get 0 and contribute nothing — conservative.
    store_vel = df.groupby("store_number")["pos_quantity_this_year"].sum() / period_days
    _units_all = float(df["pos_quantity_this_year"].sum())
    _sales_all = float(df["pos_sales_this_year"].sum())
    blended_aur = (_sales_all / _units_all) if _units_all else 0.0
    oos_recent = store_day_oh[(store_day_oh["is_oos"] == 1)
                              & (store_day_oh["business_date"] >= recent_cut7)].copy()
    oos_recent["exp_units"] = oos_recent["store_number"].map(store_vel).fillna(0.0)
    lost_units_7d = float(oos_recent["exp_units"].sum())
    lost_sales_7d = lost_units_7d * blended_aur

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
            st.metric("Est. lost sales (last 7 days)", f"${lost_sales_7d:,.0f}",
                      delta=f"≈ {lost_units_7d:,.0f} units", delta_color="off",
                      help="Sum over OOS store-days of each store's own average "
                           "daily velocity (window units ÷ days), priced at the "
                           "blended unit retail. Conservative — never credits "
                           "demand a store hasn't shown.")
            st.metric("Stores OOS today", f"{int(latest_oos['oos_stores']):,}",
                      delta=f"{oos_delta:+,} vs week ago", delta_color="inverse")
            st.metric("In-stock % today", f"{latest_oos['in_stock_pct']:.1f}%",
                      help="Share of stores carrying on-hand > 0. Retail service-level standard.")
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

    # In-stock % service-level trend with a 95% target band. Speaks the retail
    # KPI language directly (vs the raw OOS count above).
    if not oos_daily.empty:
        target = 95.0
        y_min = float(min(oos_daily["in_stock_pct"].min(), target)) - 1
        svc_line = alt.Chart(oos_daily).mark_line(color="#185FA5", strokeWidth=2.5, point=True).encode(
            x=alt.X("business_date:T", title="Date"),
            y=alt.Y("in_stock_pct:Q", title="In-stock %",
                    scale=alt.Scale(domain=[max(0, y_min), 100])),
            tooltip=[alt.Tooltip("business_date:T", title="Date"),
                     alt.Tooltip("in_stock_pct:Q", format=".1f", title="In-stock %")])
        svc_rule = alt.Chart(pd.DataFrame({"y": [target]})).mark_rule(
            color="#27500A", strokeDash=[5, 4]).encode(y="y:Q")
        svc_txt = alt.Chart(pd.DataFrame({"y": [target], "t": [f"{target:.0f}% target"]})).mark_text(
            align="left", dx=4, dy=-6, color="#27500A").encode(y="y:Q", text="t:N")
        st.altair_chart((svc_line + svc_rule + svc_txt).properties(height=220), width='stretch')


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 2 — SALES & VELOCITY
# ═══════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_sales():
    st.caption(
        f"Weekly trend charts span the full date range. The summary sections — "
        f"YoY bridge, U/S/W by item, velocity distribution, store leaderboard — "
        f"follow the sidebar **Performance window**, currently *{window_label}*."
    )

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

    # ── Average unit retail (AUR / price realization) ────────────────────────
    st.divider()
    st.subheader("Average unit retail (AUR)")
    st.caption(
        "Sales ÷ units, by week. Falling AUR with steady units means price erosion or "
        "unplanned markdowns eating margin — a units-only view hides it."
    )
    if not weekly_sales.empty:
        aur = weekly_sales.copy()
        aur["aur_ty"] = np.where(aur["units_ty_raw"] > 0, aur["sales_ty_raw"] / aur["units_ty_raw"], np.nan)
        aur["aur_ly"] = np.where(aur["units_ly_raw"] > 0, aur["sales_ly_raw"] / aur["units_ly_raw"], np.nan)
        am = aur.melt(id_vars=["week_label", "walmart_calendar_week"],
                      value_vars=["aur_ty", "aur_ly"], var_name="Period", value_name="AUR")
        am["Period"] = am["Period"].map({"aur_ty": "This Year", "aur_ly": "Last Year"})
        am = am.dropna(subset=["AUR"])
        aur_l, aur_r = st.columns([3, 2])
        with aur_l:
            st.altair_chart((alt.Chart(am).mark_line(point=True, strokeWidth=2.5).encode(
                x=alt.X("week_label:N", sort=list(aur["week_label"]), title="Week",
                        axis=alt.Axis(labelAngle=-30)),
                y=alt.Y("AUR:Q", title="Avg unit retail ($)", scale=alt.Scale(zero=False)),
                color=alt.Color("Period:N", scale=alt.Scale(domain=["This Year", "Last Year"],
                                range=["#185FA5", "#A0A09A"])),
                tooltip=["week_label", "Period", alt.Tooltip("AUR:Q", format="$.2f")],
            ).properties(height=280)), width='stretch')
        with aur_r:
            cur_aur = aur["aur_ty"].dropna()
            cur, prev = (cur_aur.iloc[-1] if len(cur_aur) else np.nan,
                         cur_aur.iloc[0] if len(cur_aur) else np.nan)
            blended_now = (aur["sales_ty_raw"].sum() / aur["units_ty_raw"].sum()
                           if aur["units_ty_raw"].sum() else 0)
            blended_ly = (aur["sales_ly_raw"].sum() / aur["units_ly_raw"].sum()
                          if aur["units_ly_raw"].sum() else 0)
            st.metric("Blended AUR (period)", f"${blended_now:,.2f}",
                      delta=f"{((blended_now - blended_ly) / blended_ly * 100) if blended_ly else 0:+.1f}% vs LY")
            if np.isfinite(cur) and np.isfinite(prev) and prev:
                st.metric("Latest week AUR", f"${cur:,.2f}",
                          delta=f"{(cur - prev) / prev * 100:+.1f}% vs first week shown")

    # ── YoY units bridge (distribution vs velocity) ──────────────────────────
    st.divider()
    st.subheader(f"What moved units YoY — {window_label}")
    st.caption(
        "Decomposes the year-over-year unit change into the **distribution effect** "
        "(change in the number of stores selling × last year's per-store rate) and the "
        "**velocity effect** (change in units per selling store × this year's store count). "
        "Tells you whether to chase shelf placement or sell-through. "
        "Follows the sidebar performance window."
    )
    _u_ty = float(df_window["pos_quantity_this_year"].sum())
    _u_ly = float(df_window["pos_quantity_last_year"].sum())
    _s_ty = int(df_window[df_window["pos_quantity_this_year"] > 0]["store_number"].nunique())
    _s_ly = int(df_window[df_window["pos_quantity_last_year"] > 0]["store_number"].nunique())
    _r_ty = (_u_ty / _s_ty) if _s_ty else 0.0
    _r_ly = (_u_ly / _s_ly) if _s_ly else 0.0
    dist_effect = (_s_ty - _s_ly) * _r_ly
    velo_effect = (_r_ty - _r_ly) * _s_ty
    bridge_steps = [
        {"label": "LY units", "amount": _u_ly, "kind": "total"},
        {"label": "Distribution", "amount": dist_effect, "kind": "delta"},
        {"label": "Velocity", "amount": velo_effect, "kind": "delta"},
        {"label": "TY units", "amount": _u_ty, "kind": "total"},
    ]
    br_l, br_r = st.columns([3, 2])
    with br_l:
        st.altair_chart(_waterfall_chart(bridge_steps, height=300), width='stretch')
    with br_r:
        st.metric("Selling stores", f"{_s_ty:,}", delta=f"{_s_ty - _s_ly:+,} vs LY")
        st.metric("Units / selling store", f"{_r_ty:,.1f}",
                  delta=f"{((_r_ty - _r_ly) / _r_ly * 100) if _r_ly else 0:+.1f}% vs LY")
        _driver = ("distribution (fewer/more stores carrying)"
                   if abs(dist_effect) >= abs(velo_effect) else
                   "velocity (per-store sell-through)")
        st.caption(f"Net YoY change of **{_u_ty - _u_ly:+,.0f} units** is driven mainly by **{_driver}**.")

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

    # ── Weekly historical U/S/W by merchandise major zone ────────────────────
    # Major zones horizontally divide the country (numbered in multiples of 10;
    # see mdse_maj_zone_nbr in the BI Link glossary), so this tracks each zone's
    # velocity week over week and surfaces regional momentum shifts the national
    # line hides. The denominator is the stores actually *selling* in that zone
    # that week (pos_quantity_this_year > 0) — never the full store count — so it
    # matches the "Units per Store per Week (w/o zeros)" definition. The current,
    # in-progress Walmart week stays live: it's flagged with ' *' and its units
    # are extrapolated to a 7-day equivalent for a fair read against full weeks.
    if "mdse_major_zone_number" in df.columns:
        st.markdown("**Weekly U/S/W by merchandise major zone**")
        st.caption(
            "Units per store per week for each major zone, normalized by the stores "
            "actually selling in that zone (not total stores). Zones horizontally "
            "divide the country; the in-progress week (*) is extrapolated to a full 7 days."
        )
        zdf = df[df["mdse_major_zone_number"] > 0]
        if not zdf.empty:
            zone_week = zdf.groupby(
                ["walmart_calendar_week", "mdse_major_zone_number"], as_index=False,
                observed=True,
            ).agg(
                units_raw=("pos_quantity_this_year", "sum"),
                days_in_week=("business_date", "nunique"),
            )
            # Stores selling = distinct stores in the zone that moved a unit that
            # week. This is the U/S/W denominator; zero-selling stores are excluded.
            zone_stores = (zdf[zdf["pos_quantity_this_year"] > 0]
                           .groupby(["walmart_calendar_week", "mdse_major_zone_number"],
                                    observed=True)["store_number"].nunique()
                           .rename("stores_selling").reset_index())
            zone_week = zone_week.merge(
                zone_stores, on=["walmart_calendar_week", "mdse_major_zone_number"], how="left")
            zone_week["stores_selling"] = zone_week["stores_selling"].fillna(0).astype(int)
            zone_week["units"] = zone_week["units_raw"] * _week_norm_factor(zone_week["days_in_week"])
            zone_week["uspw"] = np.where(
                zone_week["stores_selling"] > 0,
                (zone_week["units"] / zone_week["stores_selling"]).round(2), 0)
            zone_week["is_partial"] = zone_week["days_in_week"] < 7
            zone_week["week_label"] = _wm_week_label(
                zone_week["walmart_calendar_week"], zone_week["is_partial"])
            zone_week["Zone"] = "Zone " + zone_week["mdse_major_zone_number"].astype(str)
            zone_week = zone_week.sort_values(
                ["walmart_calendar_week", "mdse_major_zone_number"]).reset_index(drop=True)
            week_order = (zone_week[["walmart_calendar_week", "week_label"]].drop_duplicates()
                          .sort_values("walmart_calendar_week")["week_label"].tolist())
            st.altair_chart((alt.Chart(zone_week).mark_line(
                point=alt.OverlayMarkDef(size=55), strokeWidth=2.5).encode(
                x=alt.X("week_label:N", sort=week_order, title="Week",
                        axis=alt.Axis(labelAngle=-30)),
                y=alt.Y("uspw:Q", title="Units per Store per Week (stores selling)"),
                color=alt.Color("Zone:N", title="Major zone"),
                tooltip=["week_label", "Zone",
                         alt.Tooltip("uspw:Q", title="U/S/W", format=".2f"),
                         alt.Tooltip("stores_selling:Q", title="Stores selling", format=","),
                         alt.Tooltip("units:Q", title="Units (7-day eq.)", format=",.0f")],
            ).properties(height=320)), width='stretch')

            # Companion table: zones down the side, weeks across, latest U/S/W per cell.
            ztab = (zone_week.pivot(index="Zone", columns="week_label", values="uspw")
                    .reindex(columns=week_order))
            st.dataframe(ztab, width='stretch')
        else:
            st.info("No merchandise major-zone data is available for the current selection.")

    # ── U/S/W by item ────────────────────────────────────────────────────────
    # Full + Half bins are combined into a single "Bins" line; Shelf stays
    # separate. Active stores for "Bins" counts distinct stores that moved
    # either pack (not the sum of the per-pack store counts, which would
    # double-count stores selling both).
    st.markdown(f"**U/S/W by item — {window_label}**")
    item_uspw = df_window.groupby("item_group", as_index=False, observed=True).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
    ).rename(columns={"item_group": "item"})
    # Same str cast as item_perf: keep the categorical out of the tiny agg
    # frames so downstream merges/fills behave like plain strings.
    item_uspw["item"] = item_uspw["item"].astype(str)
    ty_active_item = (df_window[df_window["pos_quantity_this_year"] > 0]
                      .groupby("item_group", observed=True)["store_number"].nunique()
                      .rename("stores_ty").reset_index().rename(columns={"item_group": "item"}))
    ty_active_item["item"] = ty_active_item["item"].astype(str)
    ly_active_item = (df_window[df_window["pos_quantity_last_year"] > 0]
                      .groupby("item_group", observed=True)["store_number"].nunique()
                      .rename("stores_ly").reset_index().rename(columns={"item_group": "item"}))
    ly_active_item["item"] = ly_active_item["item"].astype(str)
    item_uspw = item_uspw.merge(ty_active_item, on="item", how="left")
    item_uspw = item_uspw.merge(ly_active_item, on="item", how="left")
    item_uspw["stores_ty"] = item_uspw["stores_ty"].fillna(0).astype(int)
    item_uspw["stores_ly"] = item_uspw["stores_ly"].fillna(0).astype(int)
    item_uspw["uspw_ty"] = np.where(item_uspw["stores_ty"] > 0,
        (item_uspw["units_ty"] / item_uspw["stores_ty"] / weeks_in_window).round(2), 0)
    item_uspw["uspw_ly"] = np.where(item_uspw["stores_ly"] > 0,
        (item_uspw["units_ly"] / item_uspw["stores_ly"] / weeks_in_window).round(2), 0)
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
    st.markdown(f"**Store-level velocity distribution — {window_label}**")
    st.caption(
        "Each store's units over the performance window, scaled to a weekly rate "
        "(so a 1-day or 7-day window stays comparable to the full lookback)."
    )
    store_uspw = df_window.groupby("store_number", as_index=False).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
    )
    store_uspw["uspw_ty"] = (store_uspw["units_ty"] / weeks_in_window).round(1)
    store_uspw["uspw_ly"] = (store_uspw["units_ly"] / weeks_in_window).round(1)

    ORDER = ["0 (none)", "0-1", "1-3", "3-5", "5-10", "10-20", "20+"]

    def _bucket_series(v: pd.Series) -> pd.Series:
        """Vectorised U/S/W tier label (pd.cut beats a per-row apply here)."""
        out = pd.cut(v, bins=[-np.inf, 1, 3, 5, 10, 20, np.inf], right=False,
                     labels=ORDER[1:]).astype(object)
        out[v == 0] = ORDER[0]
        return out

    store_uspw["bucket_ty"] = _bucket_series(store_uspw["uspw_ty"])
    store_uspw["bucket_ly"] = _bucket_series(store_uspw["uspw_ly"])
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

    # ── Velocity tier migration heatmap (week × tier) ────────────────────────
    st.markdown("**How the store base moves between velocity tiers, week by week**")
    st.caption(
        "Each cell = number of stores in that U/S/W tier that week. Watch stores drift "
        "down toward the '0 (none)' row (losing momentum) or climb into 5-10 / 10-20 / 20+."
    )
    _days_map = weekly_sales.set_index("walmart_calendar_week")["days_in_week"]
    sw = df.groupby(["walmart_calendar_week", "store_number"], as_index=False)[
        "pos_quantity_this_year"].sum()
    sw["days"] = sw["walmart_calendar_week"].map(_days_map).fillna(7).clip(lower=1)
    sw["uspw"] = sw["pos_quantity_this_year"] * (7 / sw["days"])
    sw["tier"] = _bucket_series(sw["uspw"])
    heat = sw.groupby(["walmart_calendar_week", "tier"], as_index=False).size().rename(
        columns={"size": "stores"})
    heat["week_label"] = _wm_week_label(heat["walmart_calendar_week"])
    if not heat.empty:
        week_order = (heat[["walmart_calendar_week", "week_label"]].drop_duplicates()
                      .sort_values("walmart_calendar_week")["week_label"].tolist())
        st.altair_chart((alt.Chart(heat).mark_rect().encode(
            x=alt.X("week_label:N", sort=week_order, title="Week", axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("tier:N", sort=list(reversed(ORDER)), title="U/S/W tier"),
            color=alt.Color("stores:Q", scale=alt.Scale(scheme="blues"),
                            legend=alt.Legend(title="Stores")),
            tooltip=["week_label", "tier", alt.Tooltip("stores:Q", format=",")],
        ).properties(height=300)), width='stretch')

    # ── Store leaderboard ────────────────────────────────────────────────────
    st.divider()
    st.subheader(f"Store leaderboard — {window_label}")
    st.caption(
        "Per-store performance over the sidebar **performance window** (switch it to "
        "most recent day / last 7 days / full lookback). Sort any column; download the "
        "full list for offline review. Decliner/mover callouts limited to stores that "
        "sold last year. On-hand is always each store's latest snapshot."
    )
    store_rank = df_window.groupby(["store_number", "state_or_province_code"],
                                   as_index=False, observed=True).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
    )
    # On-hand stays point-in-time (latest day in the full frame): a 1-day sales
    # window shouldn't change what's sitting on the shelf right now.
    _lm = df.groupby("store_number")["business_date"].transform("max") == df["business_date"]
    _oh = df[_lm].groupby("store_number", as_index=False)[
        "store_on_hand_quantity_this_year"].sum().rename(
            columns={"store_on_hand_quantity_this_year": "on_hand"})
    store_rank = store_rank.merge(_oh, on="store_number", how="left")
    store_rank["on_hand"] = store_rank["on_hand"].fillna(0)
    store_rank["yoy_units"] = store_rank["units_ty"] - store_rank["units_ly"]
    store_rank["yoy_pct"] = _yoy_pct(store_rank["units_ty"], store_rank["units_ly"])
    store_rank["wos"] = np.where(store_rank["units_ty"] > 0,
        (store_rank["on_hand"] / (store_rank["units_ty"] / weeks_in_window)).round(1), np.nan)

    lb_l, lb_r = st.columns(2)
    _sold_ly = store_rank[store_rank["units_ly"] > 0]
    with lb_l:
        st.markdown("**Biggest YoY decliners** (sold last year)")
        decl = _sold_ly.sort_values("yoy_units").head(10)[
            ["store_number", "state_or_province_code", "units_ty", "units_ly", "yoy_units", "yoy_pct"]].copy()
        decl.columns = ["Store", "State", "Units TY", "Units LY", "YoY Units", "YoY %"]
        st.dataframe(decl, width='stretch', hide_index=True, column_config={
            "YoY %": st.column_config.NumberColumn(format="%.1f%%")})
    with lb_r:
        st.markdown("**Biggest YoY gainers**")
        gain = store_rank.sort_values("yoy_units", ascending=False).head(10)[
            ["store_number", "state_or_province_code", "units_ty", "units_ly", "yoy_units", "yoy_pct"]].copy()
        gain.columns = ["Store", "State", "Units TY", "Units LY", "YoY Units", "YoY %"]
        st.dataframe(gain, width='stretch', hide_index=True, column_config={
            "YoY %": st.column_config.NumberColumn(format="%.1f%%")})

    full_tbl = store_rank.sort_values("units_ty", ascending=False)[
        ["store_number", "state_or_province_code", "units_ty", "units_ly",
         "yoy_units", "yoy_pct", "sales_ty", "on_hand", "wos"]].copy()
    full_tbl.columns = ["Store", "State", "Units TY", "Units LY", "YoY Units",
                        "YoY %", "Sales TY ($)", "On hand", "WOS"]
    lb_query = st.text_input(
        "Find a store", "", key="lb_store_search",
        placeholder="Store number (e.g. 1234) or state code (e.g. TX)",
        help="Filters the table below. Leave empty to show every store.")
    show_tbl = full_tbl
    if lb_query.strip():
        q = lb_query.strip().upper()
        show_tbl = full_tbl[
            full_tbl["Store"].astype(str).str.contains(q, regex=False)
            | (full_tbl["State"].astype(str).str.upper() == q)]
        st.caption(f"{len(show_tbl):,} of {len(full_tbl):,} stores match “{lb_query.strip()}”.")
    st.dataframe(show_tbl, width='stretch', hide_index=True, height=380, column_config={
        "YoY %": st.column_config.NumberColumn(format="%.1f%%"),
        "Sales TY ($)": st.column_config.NumberColumn(format="$%.0f"),
        "WOS": st.column_config.NumberColumn(format="%.1f wks"),
    })
    _win_slug = {"Most recent day": "day", "Last 7 days": "7d",
                 "Full lookback": "full"}.get(perf_window, "window")
    st.download_button(
        "⬇ Download full store list (CSV)",
        data=full_tbl.to_csv(index=False).encode("utf-8"),
        file_name=f"store_leaderboard_{_win_slug}_{most_recent.strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 3 — SALES DRIVERS (what's behind the change, incl. the inventory cause)
# ═══════════════════════════════════════════════════════════════════════════
# Answers one question for the current filters: *why* are sales up or down vs the
# same period last year? It works entirely off the POS + store-inventory frame
# already in memory — no extra queries — and (a) splits the sales-dollar change
# into distribution, velocity and price effects (an exact, additive bridge),
# (b) weighs how much of any move is a fixable availability (stockout) problem
# vs a true demand shift by reading the inventory feed against sales, and
# (c) localises the change to the items, states and stores moving it. Every
# number follows the sidebar item/state filters and the performance window;
# only the closing trajectory chart spans the full date range, for context.
@st.fragment
def _render_drivers():
    # Per-tab performance window: scope this whole analysis to a single day, the
    # last 7 days, or the full lookback — independent of the sidebar, and (being
    # inside the fragment) switchable without rerunning the other tabs. Defaults
    # to the sidebar's current Performance window. Reassigning df_window /
    # window_label here shadows the module-level pair for the rest of this
    # function, so every section below picks up the local window automatically.
    _win_opts = ["Most recent day", "Last 7 days", "Full lookback"]
    win_choice = st.radio(
        "Performance window",
        options=_win_opts,
        index=_win_opts.index(perf_window) if perf_window in _win_opts else 0,
        horizontal=True,
        key="drivers_perf_window",
        help="Scope this tab's analysis to a single day, the last 7 days, or the full "
             "lookback. Independent of the sidebar Performance window (defaults to it).",
    )
    if win_choice == "Most recent day":
        df_window = df[df["business_date"] == most_recent]
        window_label = f"Day of {most_recent.strftime('%b %d, %Y')}"
    elif win_choice == "Last 7 days":
        _cut7 = most_recent - timedelta(days=6)
        df_window = df[df["business_date"] >= _cut7]
        window_label = (f"Last 7 days ({_cut7.strftime('%b %d')}–"
                        f"{most_recent.strftime('%b %d')})")
    else:
        df_window = df
        window_label = (f"Full lookback ({df['business_date'].min().strftime('%b %d')}–"
                        f"{most_recent.strftime('%b %d')}, {period_days} data days)")

    st.caption(
        f"Why sales moved versus the **same period last year**, for the current selection "
        f"({item_view}{_state_note}) over the **{window_label}**. Reads the inventory feed "
        f"against sales to separate a fixable availability problem from a true demand shift, "
        f"then localises the change to items, states and stores. Follows the sidebar item / "
        f"state filters; pick the time window above. The trajectory chart at the bottom spans "
        f"the full date range for context."
    )

    # ── Window aggregates (TY vs LY) ─────────────────────────────────────────
    units_ty = float(df_window["pos_quantity_this_year"].sum())
    units_ly = float(df_window["pos_quantity_last_year"].sum())
    sales_ty = float(df_window["pos_sales_this_year"].sum())
    sales_ly = float(df_window["pos_sales_last_year"].sum())
    d_units = units_ty - units_ly
    d_sales = sales_ty - sales_ly
    units_pct = (d_units / units_ly * 100) if units_ly else 0.0
    sales_pct = (d_sales / sales_ly * 100) if sales_ly else 0.0

    if not sales_ly and not units_ly:
        st.info(
            "No same-period-last-year sales in this window, so there's no baseline to explain "
            "a change against. Widen the lookback or clear the item/state filters."
        )
        return

    # ── Sales-dollar bridge: distribution × velocity × price ─────────────────
    # Δsales = Δunits·AUR_ly + ΔAUR·units_ty, and Δunits splits exactly into a
    # distribution effect (Δ selling-stores × LY per-store rate) and a velocity
    # effect (Δ per-store rate × TY selling-stores). Multiplying the two unit
    # effects by LY AUR puts all three on a dollar footing that sums to Δsales.
    stores_ty = int(df_window[df_window["pos_quantity_this_year"] > 0]["store_number"].nunique())
    stores_ly = int(df_window[df_window["pos_quantity_last_year"] > 0]["store_number"].nunique())
    rate_ty = (units_ty / stores_ty) if stores_ty else 0.0   # units per selling store
    rate_ly = (units_ly / stores_ly) if stores_ly else 0.0
    aur_ty = (sales_ty / units_ty) if units_ty else 0.0
    aur_ly = (sales_ly / units_ly) if units_ly else 0.0
    dist_units = (stores_ty - stores_ly) * rate_ly
    velo_units = (rate_ty - rate_ly) * stores_ty
    dist_dollars = dist_units * aur_ly
    velo_dollars = velo_units * aur_ly
    price_dollars = (aur_ty - aur_ly) * units_ty

    # Primary driver = the biggest contributor in the direction of the net change.
    _drivers = [
        ("distribution — the number of stores selling", dist_dollars),
        ("velocity — units sold per selling store", velo_dollars),
        ("price — average unit retail", price_dollars),
    ]
    primary_name, primary_val = (max(_drivers, key=lambda x: x[1]) if d_sales >= 0
                                 else min(_drivers, key=lambda x: x[1]))

    # ── Verdict banner ───────────────────────────────────────────────────────
    if sales_pct <= -10:
        _fn, _icon = st.error, "🔴"
    elif sales_pct < -2:
        _fn, _icon = st.warning, "🟡"
    elif sales_pct > 2:
        _fn, _icon = st.success, "🟢"
    else:
        _fn, _icon = st.info, "ℹ️"
    _dir = "up" if d_sales >= 0 else "down"
    _fn(
        f"**Sales are {_dir} {abs(sales_pct):.1f}% YoY** this window "
        f"(${sales_ty:,.0f} vs ${sales_ly:,.0f}, {d_sales:+,.0f}). "
        f"The biggest driver is **{primary_name}** ({primary_val:+,.0f}).",
        icon=_icon,
    )

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Sales TY", f"${sales_ty:,.0f}", f"{sales_pct:+.1f}% YoY")
    k2.metric("Sales LY", f"${sales_ly:,.0f}", help="Same period last year.")
    k3.metric("Units TY", f"{units_ty:,.0f}", f"{units_pct:+.1f}% YoY")
    k4.metric("Net sales change", f"${d_sales:+,.0f}", delta_color="off",
              help="This year minus last year, for this window.")

    # ── What moved sales dollars ─────────────────────────────────────────────
    st.divider()
    st.subheader(f"What moved sales dollars — {window_label}")
    st.caption(
        "Splits the year-over-year **sales-dollar** change into three exact, additive effects: "
        "**distribution** (change in stores selling × last year's per-store rate), "
        "**velocity** (change in units per selling store × this year's store count), and "
        "**price** (change in average unit retail × this year's units). Distribution and "
        "velocity are the demand/availability levers; price is margin / markdown."
    )
    steps = [
        {"label": "LY sales", "amount": sales_ly, "kind": "total"},
        {"label": "Distribution", "amount": dist_dollars, "kind": "delta"},
        {"label": "Velocity", "amount": velo_dollars, "kind": "delta"},
        {"label": "Price", "amount": price_dollars, "kind": "delta"},
        {"label": "TY sales", "amount": sales_ty, "kind": "total"},
    ]
    bcol, scol = st.columns([3, 2])
    with bcol:
        st.altair_chart(
            _waterfall_chart(steps, height=320, y_title="Sales ($)", currency=True),
            width='stretch')
    with scol:
        st.metric("Selling stores", f"{stores_ty:,}", delta=f"{stores_ty - stores_ly:+,} vs LY")
        st.metric("Units / selling store", f"{rate_ty:,.1f}",
                  delta=f"{((rate_ty - rate_ly) / rate_ly * 100) if rate_ly else 0:+.1f}% vs LY")
        st.metric("Avg unit retail", f"${aur_ty:,.2f}",
                  delta=f"{((aur_ty - aur_ly) / aur_ly * 100) if aur_ly else 0:+.1f}% vs LY")
    st.caption(
        f"Distribution **${dist_dollars:+,.0f}**  ·  velocity **${velo_dollars:+,.0f}**  ·  "
        f"price **${price_dollars:+,.0f}**  →  net **${d_sales:+,.0f}**."
    )

    # ── Demand vs availability (the inventory cause) ──────────────────────────
    # The bridge above can't tell a true demand drop from product simply not being
    # on the shelf — both show up as lost velocity. This reads the inventory feed
    # to size the recoverable (stockout) piece and judge whether stock levels are
    # the constraint.
    st.divider()
    st.subheader("Is it demand or availability?")
    st.caption(
        "Velocity falls for two very different reasons: customers buying less (a demand "
        "problem) or product not on the shelf (a fixable availability problem). This weighs "
        "the inventory feed against sales — estimated sales lost to stockouts, the in-stock "
        "rate, and whether on-hand is the constraint — to tell them apart."
    )

    # Per-store normal daily velocity from the full lookback (a stable baseline),
    # credited to every out-of-stock store-day inside the window — so we never
    # invent demand a store hasn't shown. Mirrors the Overview tab's method.
    store_vel = df.groupby("store_number")["pos_quantity_this_year"].sum() / period_days
    win_sd_oh = df_window.groupby(["store_number", "business_date"], as_index=False)[
        "store_on_hand_quantity_this_year"].sum()
    win_sd_oh["is_oos"] = win_sd_oh["store_on_hand_quantity_this_year"] == 0
    oos_sd = win_sd_oh[win_sd_oh["is_oos"]].copy()
    oos_sd["exp_units"] = oos_sd["store_number"].map(store_vel).fillna(0.0)
    lost_units = float(oos_sd["exp_units"].sum())
    lost_sales = lost_units * (aur_ty if aur_ty else aur_ly)
    oos_store_days = int(len(oos_sd))
    total_store_days = int(len(win_sd_oh))
    instock_pct = (100 * (1 - oos_store_days / total_store_days)) if total_store_days else 100.0

    # On-hand YoY at the window's latest day (the frame carries LY on-hand), plus
    # weeks of supply on the recent run-rate (last 14 days, as the Inventory tab).
    _snap = df_window[df_window["business_date"] == df_window["business_date"].max()]
    oh_ty = float(_snap["store_on_hand_quantity_this_year"].sum())
    oh_ly = float(_snap["store_on_hand_quantity_last_year"].sum())
    oh_yoy = ((oh_ty - oh_ly) / oh_ly * 100) if oh_ly else 0.0
    inv_recent = df[df["business_date"] >= most_recent - timedelta(days=13)]
    inv_recent_days = max(1, inv_recent["business_date"].dt.normalize().nunique())
    weekly_runrate = float(inv_recent["pos_quantity_this_year"].sum()) / inv_recent_days * 7
    wos = (oh_ty / weekly_runrate) if weekly_runrate else float("inf")

    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Est. sales lost to stockouts", f"${lost_sales:,.0f}",
              delta=f"≈ {lost_units:,.0f} units", delta_color="off",
              help="Every out-of-stock store-day in the window credited the store's own "
                   "normal daily velocity (full-lookback rate), priced at this year's AUR. "
                   "Conservative — never credits demand a store hasn't shown.")
    a2.metric("In-stock % (window)", f"{instock_pct:.1f}%",
              help="Share of store-days with on-hand > 0. 95%+ is the retail service-level target.")
    a3.metric("Store on-hand YoY", f"{oh_yoy:+.1f}%", delta_color="off",
              help=f"On-hand {oh_ty:,.0f} TY vs {oh_ly:,.0f} LY at the latest day in the window.")
    a4.metric("Weeks of supply", (f"{wos:.1f} wks" if np.isfinite(wos) else "—"),
              help="Store on-hand ÷ recent weekly sell-through (last 14 days).")

    if d_sales < 0:
        gap = -d_sales
        share = (lost_sales / gap) if gap > 0 else 0.0
        if instock_pct < 96.0 or share >= 0.25:
            st.warning(
                f"**Availability is a meaningful driver of the decline.** Stockouts cost an "
                f"estimated **${lost_sales:,.0f}** this window — about **{min(share, 1) * 100:.0f}% "
                f"of the ${gap:,.0f} shortfall** — with in-stock at **{instock_pct:.1f}%**. Much of "
                f"this is recoverable through replenishment and on-shelf execution; the Store "
                f"Actions tab lists the specific stores to dispatch.",
                icon="📦")
        else:
            _overstocked = (np.isfinite(wos) and wos >= 6) or oh_yoy > 10
            _tail = (f", yet on-hand is **{oh_yoy:+.0f}% YoY** at **{wos:.1f} wks** of supply — "
                     f"stock is building against softer demand (markdown / return risk)."
                     if _overstocked else
                     ". The product is on the shelf; customers are simply buying less per store.")
            st.info(
                f"**This looks demand-driven, not a supply problem.** In-stock is healthy at "
                f"**{instock_pct:.1f}%** and stockouts explain only ~{share * 100:.0f}% of the "
                f"shortfall{_tail} The lever here is demand (assortment, price, promotion), not "
                f"replenishment.",
                icon="🛒")
    elif d_sales > 0:
        if instock_pct < 96.0 and lost_sales > 0:
            st.success(
                f"**Sales are growing**, but stockouts still left an estimated **${lost_sales:,.0f}** "
                f"on the table (in-stock {instock_pct:.1f}%) — closing that gap is additional upside.",
                icon="🟢")
        else:
            _tail = (f" On-hand is **{oh_yoy:+.0f}% YoY** at {wos:.1f} wks of supply — keep it "
                     f"positioned to sustain the run." if np.isfinite(wos) else "")
            st.success(
                f"**Sales are growing** with in-stock at **{instock_pct:.1f}%** — demand is being "
                f"met, not constrained by supply.{_tail}",
                icon="🟢")
    else:
        st.info("Sales are essentially flat versus last year for this window.")

    # ── Where the change is concentrated ──────────────────────────────────────
    st.divider()
    st.subheader("Where the change is concentrated")
    st.caption(
        "The same net change, broken out by item, state and store so you can see what's "
        "actually moving it — and, at store level, whether each mover is a stockout or a "
        "demand story."
    )
    metric_choice = st.radio(
        "Measure contributions in", ["Sales ($)", "Units"], index=0, horizontal=True,
        key="drivers_contrib_metric")
    use_dollars = metric_choice == "Sales ($)"
    ty_col = "pos_sales_this_year" if use_dollars else "pos_quantity_this_year"
    ly_col = "pos_sales_last_year" if use_dollars else "pos_quantity_last_year"
    val_fmt = "$%.0f" if use_dollars else "%d"
    chart_fmt = "$,.0f" if use_dollars else ",.0f"

    def _diverging(frame, cat_col, cat_title, height):
        order = list(frame.sort_values("change")[cat_col].astype(str))
        return alt.Chart(frame).mark_bar().encode(
            x=alt.X("change:Q", title=f"YoY change ({metric_choice})",
                    axis=alt.Axis(format=chart_fmt)),
            y=alt.Y(f"{cat_col}:N", sort=order, title=cat_title),
            color=alt.Color("dir:N", scale=alt.Scale(domain=["Up", "Down"],
                            range=["#27500A", "#791F1F"]), legend=None),
            tooltip=[alt.Tooltip(f"{cat_col}:N", title=cat_title),
                     alt.Tooltip("ty:Q", title="TY", format=chart_fmt),
                     alt.Tooltip("ly:Q", title="LY", format=chart_fmt),
                     alt.Tooltip("change:Q", title="Change", format=chart_fmt),
                     alt.Tooltip("yoy_pct:Q", title="YoY %", format=".1f")],
        ).properties(height=height)

    # By item (Full + Half bins collapse to "Bins").
    st.markdown("**By item**")
    item_chg = df_window.groupby("item_group", as_index=False, observed=True).agg(
        ty=(ty_col, "sum"), ly=(ly_col, "sum")).rename(columns={"item_group": "item"})
    item_chg["item"] = item_chg["item"].astype(str)
    item_chg["change"] = item_chg["ty"] - item_chg["ly"]
    item_chg["yoy_pct"] = _yoy_pct(item_chg["ty"], item_chg["ly"])
    item_chg["dir"] = np.where(item_chg["change"] >= 0, "Up", "Down")
    st.altair_chart(_diverging(item_chg, "item", "Item", max(120, len(item_chg) * 46)),
                    width='stretch')

    # By state — biggest drags and lifts.
    st.markdown("**By state — biggest drags and lifts**")
    state_chg = df_window.groupby("state_or_province_code", as_index=False, observed=True).agg(
        ty=(ty_col, "sum"), ly=(ly_col, "sum"))
    state_chg["state"] = state_chg["state_or_province_code"].astype(str)
    state_chg["change"] = state_chg["ty"] - state_chg["ly"]
    state_chg["yoy_pct"] = _yoy_pct(state_chg["ty"], state_chg["ly"])
    top_states = pd.concat([state_chg.nsmallest(8, "change"),
                            state_chg.nlargest(8, "change")]).drop_duplicates(subset="state")
    top_states["dir"] = np.where(top_states["change"] >= 0, "Up", "Down")
    if not top_states.empty:
        st.altair_chart(
            _diverging(top_states, "state", "State", max(160, len(top_states) * 26)),
            width='stretch')

    # By store — biggest movers, with a likely cause tagged from the inventory feed.
    st.markdown("**By store — biggest movers (with likely cause)**")
    store_chg = df_window.groupby(["store_number", "state_or_province_code"],
                                  as_index=False, observed=True).agg(
        ty_u=("pos_quantity_this_year", "sum"), ly_u=("pos_quantity_last_year", "sum"),
        ty_s=("pos_sales_this_year", "sum"), ly_s=("pos_sales_last_year", "sum"))
    store_chg["change"] = ((store_chg["ty_s"] - store_chg["ly_s"]) if use_dollars
                           else (store_chg["ty_u"] - store_chg["ly_u"]))
    _lm = (df_window.groupby("store_number")["business_date"].transform("max")
           == df_window["business_date"])
    latest_oh = df_window[_lm].groupby("store_number")["store_on_hand_quantity_this_year"].sum()
    oos_days_by_store = oos_sd.groupby("store_number").size()
    store_chg["on_hand"] = store_chg["store_number"].map(latest_oh).fillna(0).astype(int)
    store_chg["oos_days"] = store_chg["store_number"].map(oos_days_by_store).fillna(0).astype(int)
    # A decliner that's currently out of stock (or went OOS during the window) is an
    # availability story; one that's still stocked but selling less is a demand story.
    store_chg["cause"] = np.where(
        store_chg["change"] >= 0, "—",
        np.where((store_chg["on_hand"] == 0) | (store_chg["oos_days"] >= 1),
                 "📦 Stockout", "🛒 Demand"))
    dcol, gcol = st.columns(2)
    with dcol:
        st.markdown("Top decliners")
        dec = store_chg.nsmallest(12, "change")[
            ["store_number", "state_or_province_code", "change", "on_hand", "cause"]].copy()
        dec.columns = ["Store", "State", "Change", "On-hand", "Likely cause"]
        st.dataframe(dec, width='stretch', hide_index=True, column_config={
            "Change": st.column_config.NumberColumn(format=val_fmt),
            "On-hand": st.column_config.NumberColumn(format="%d")})
    with gcol:
        st.markdown("Top gainers")
        gn = store_chg.nlargest(12, "change")[
            ["store_number", "state_or_province_code", "change", "on_hand"]].copy()
        gn.columns = ["Store", "State", "Change", "On-hand"]
        st.dataframe(gn, width='stretch', hide_index=True, column_config={
            "Change": st.column_config.NumberColumn(format=val_fmt),
            "On-hand": st.column_config.NumberColumn(format="%d")})

    # ── Trajectory: weekly sales gap vs in-stock rate ─────────────────────────
    # Whole-range context the window can't show: is the YoY deficit (or surplus)
    # widening or closing, and does it track the in-stock rate? A deepening gap
    # that moves with falling in-stock points to availability; a gap while
    # in-stock holds points to demand.
    st.divider()
    st.subheader("Trajectory — is the gap widening or closing?")
    st.caption(
        "Weekly YoY sales gap (this year minus last year) across the full date range, with the "
        "in-stock rate overlaid. Partial weeks are scaled to a 7-day equivalent. Spans the full "
        "range regardless of the performance window, for context."
    )
    wk = df.groupby("walmart_calendar_week", as_index=False).agg(
        sales_ty=("pos_sales_this_year", "sum"),
        sales_ly=("pos_sales_last_year", "sum"),
        days_in_week=("business_date", "nunique"))
    _norm = _week_norm_factor(wk["days_in_week"])
    wk["sales_ty"] = wk["sales_ty"] * _norm
    wk["sales_ly"] = wk["sales_ly"] * _norm
    wk["gap"] = wk["sales_ty"] - wk["sales_ly"]
    wk["dir"] = np.where(wk["gap"] >= 0, "Up", "Down")
    wk["is_partial"] = wk["days_in_week"] < 7
    wk["week_label"] = _wm_week_label(wk["walmart_calendar_week"], wk["is_partial"])
    wk = wk.sort_values("walmart_calendar_week").reset_index(drop=True)
    # Weekly in-stock %: share of store-days with on-hand > 0.
    _sdoh = df.groupby(["walmart_calendar_week", "business_date", "store_number"],
                       as_index=False)["store_on_hand_quantity_this_year"].sum()
    _sdoh["oos"] = _sdoh["store_on_hand_quantity_this_year"] == 0
    _wk_is = _sdoh.groupby("walmart_calendar_week", as_index=False).agg(
        oos_sd=("oos", "sum"), tot_sd=("oos", "size"))
    _wk_is["instock_pct"] = 100 * (1 - _wk_is["oos_sd"] / _wk_is["tot_sd"])
    wk = wk.merge(_wk_is[["walmart_calendar_week", "instock_pct"]],
                  on="walmart_calendar_week", how="left")

    if not wk.empty:
        _order = list(wk["week_label"])
        _x = alt.X("week_label:N", sort=_order, title="Week", axis=alt.Axis(labelAngle=-30))
        gap_bars = alt.Chart(wk).mark_bar().encode(
            x=_x,
            y=alt.Y("gap:Q", title="Weekly YoY sales gap ($)"),
            color=alt.Color("dir:N", scale=alt.Scale(domain=["Up", "Down"],
                            range=["#27500A", "#791F1F"]), legend=None),
            tooltip=["week_label",
                     alt.Tooltip("sales_ty:Q", title="Sales TY", format="$,.0f"),
                     alt.Tooltip("sales_ly:Q", title="Sales LY", format="$,.0f"),
                     alt.Tooltip("gap:Q", title="YoY gap", format="$,.0f"),
                     alt.Tooltip("instock_pct:Q", title="In-stock %", format=".1f")])
        instock_line = alt.Chart(wk).mark_line(point=True, strokeWidth=2.5, color="#185FA5").encode(
            x=_x,
            y=alt.Y("instock_pct:Q", title="In-stock %", scale=alt.Scale(zero=False)),
            tooltip=["week_label", alt.Tooltip("instock_pct:Q", title="In-stock %", format=".1f")])
        st.altair_chart(
            alt.layer(gap_bars, instock_line).resolve_scale(y="independent").properties(height=340),
            width='stretch')
        st.caption("Bars = weekly YoY sales gap (green = ahead of LY, red = behind, left axis). "
                   "Blue line = in-stock % (right axis).")


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 4 — FORECAST
# ═══════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_forecast():
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
        fc = fcst_df[fcst_df["walmart_item_number"].isin(item_filter)]
        # Actuals lag ~1 day, so "upcoming" = forecast dated after the latest actual.
        future = fc[fc["forecast_date"] > most_recent]
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

            # Forecast accuracy (WMAPE). Bias above tells you direction; this tells
            # you magnitude — how far off the forecast was day-to-day, volume-weighted
            # so a couple of tiny-volume days can't blow up the error.
            st.markdown("**Forecast accuracy**")
            st.caption(
                "WMAPE = total absolute error ÷ total forecast (volume-weighted). "
                "Accuracy = 100 − WMAPE. Complements bias: you can be net-on-target "
                "(low bias) yet still inaccurate day-to-day (high WMAPE)."
            )
            acc_abs = attn.assign(ae=(attn["actual_quantity"] - attn["forecast_quantity"]).abs())
            ae_item = acc_abs.groupby("walmart_item_number")["ae"].sum()
            acc_item = attn.groupby("walmart_item_number", as_index=False).agg(
                forecast=("forecast_quantity", "sum"), actual=("actual_quantity", "sum"))
            acc_item["abs_err"] = acc_item["walmart_item_number"].map(ae_item).fillna(0.0)
            acc_item["wmape"] = np.where(acc_item["forecast"] > 0,
                                         acc_item["abs_err"] / acc_item["forecast"] * 100, np.nan)
            acc_item["accuracy"] = (100 - acc_item["wmape"]).clip(lower=0)
            acc_item["Item"] = acc_item["walmart_item_number"].map(ITEM_LABELS).fillna(
                acc_item["walmart_item_number"].astype(str))
            tot_ae = float(acc_abs["ae"].sum())
            tot_fc = float(attn["forecast_quantity"].sum())
            overall_wmape = (tot_ae / tot_fc * 100) if tot_fc else float("nan")
            overall_acc = max(0.0, 100 - overall_wmape) if np.isfinite(overall_wmape) else float("nan")
            ac_l, ac_r = st.columns([2, 3])
            with ac_l:
                st.metric("Overall forecast accuracy",
                          f"{overall_acc:.0f}%" if np.isfinite(overall_acc) else "—",
                          help="100 − volume-weighted MAPE over the overlapping forecast/actual window")
            with ac_r:
                acc_show = acc_item[["Item", "accuracy", "wmape"]].copy()
                acc_show.columns = ["Item", "Accuracy %", "WMAPE %"]
                st.dataframe(acc_show, width='stretch', hide_index=True, column_config={
                    "Accuracy %": st.column_config.NumberColumn(format="%.0f%%"),
                    "WMAPE %": st.column_config.NumberColumn(format="%.0f%%")})

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
#   TAB 5 — INVENTORY & DC
# ═══════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_inventory():
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

    # Inventory productivity — is the stock actually working?
    period_units = float(df["pos_quantity_this_year"].sum())
    sell_through = (period_units / (period_units + store_oh) * 100) if (period_units + store_oh) else 0.0
    inv_turn_wk = (weekly_units / store_oh) if store_oh else 0.0
    pr1, pr2, pr3 = st.columns(3)
    pr1.metric("Sell-through % (period)", f"{sell_through:.1f}%",
               help="Units sold ÷ (units sold + current store on-hand). Higher = inventory turning, "
                    "not piling up on shelves.")
    pr2.metric("Weekly inventory turn", f"{inv_turn_wk:.2f}×",
               help="Recent weekly sell-through ÷ store on-hand. ~0.25× ≈ 4 weeks of supply.")
    pr3.metric("In-stock % (today)", f"{100 - oos_pct:.1f}%",
               help="Share of stores with on-hand > 0 — retail service-level standard.")

    # ── Inventory by location (latest snapshot) ──────────────────────────────
    st.divider()
    st.subheader("Inventory by location (latest snapshot)")
    st.caption(
        "Actual unit counts at each stage of the pipeline on the most recent day of data. "
        "Use the scope toggle to isolate shelf vs. bin items. This section ignores the "
        "sidebar item filter so you can slice it independently."
    )

    inv_scope = st.radio(
        "Item scope",
        options=["All", "Bins (full + half)", "Shelf"],
        index=0,
        horizontal=True,
        key="inv_location_scope",
    )
    if inv_scope == "Bins (full + half)":
        inv_items = list(BIN_ITEMS)
    elif inv_scope == "Shelf":
        inv_items = list(SHELF_ITEMS)
    else:
        inv_items = list(ACTIVE_ITEMS)

    # Store-side: most recent business day only, scoped to the chosen items.
    store_latest_date = df_all["business_date"].max()
    store_snap = df_all[
        (df_all["business_date"] == store_latest_date)
        & (df_all["walmart_item_number"].isin(inv_items))
    ]

    # DC-side: most recent inventory date only, scoped to the chosen items.
    if not dc_df_all.empty:
        dc_latest_snap_date = dc_df_all["inventory_date"].max()
        dc_snap = dc_df_all[
            (dc_df_all["inventory_date"] == dc_latest_snap_date)
            & (dc_df_all["walmart_item_number"].isin(inv_items))
        ]
    else:
        dc_latest_snap_date = None
        dc_snap = pd.DataFrame()

    store_oh_n = float(store_snap["store_on_hand_quantity_this_year"].sum())
    in_whse_n = float(store_snap["store_in_warehouse_quantity_this_year"].sum())
    in_transit_n = float(store_snap["store_in_transit_quantity_this_year"].sum())
    dc_oh_n = float(dc_snap["on_hand_warehouse_inventory_in_units_this_year"].sum()) if not dc_snap.empty else 0.0
    dc_oo_n = float(dc_snap["on_order_warehouse_quantity_in_units_this_year"].sum()) if not dc_snap.empty else 0.0
    # Total units physically in the network = store on-hand + in-transit + DC on-hand.
    # In-warehouse is DC stock earmarked for stores (would double-count DC on-hand)
    # and on-order hasn't arrived yet — both excluded from the physical total.
    total_network = store_oh_n + in_transit_n + dc_oh_n

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("In store (on-hand)", f"{store_oh_n:,.0f}")
    c2.metric("In warehouse", f"{in_whse_n:,.0f}", help="DC stock earmarked for stores")
    c3.metric("In transit", f"{in_transit_n:,.0f}", help="On the way to stores")
    c4.metric("DC on-hand", f"{dc_oh_n:,.0f}", help="Distribution-center on-hand (eaches)")
    c5.metric("DC on-order", f"{dc_oo_n:,.0f}", help="Inbound to the DCs (eaches)")
    c6.metric("Total network", f"{total_network:,.0f}",
              help="Store on-hand + in-transit + DC on-hand (no double-count)")

    # Per-item breakdown table with a totals row.
    store_by_item = store_snap.groupby("walmart_item_number", as_index=False).agg(
        store_oh=("store_on_hand_quantity_this_year", "sum"),
        in_whse=("store_in_warehouse_quantity_this_year", "sum"),
        in_transit=("store_in_transit_quantity_this_year", "sum"),
    )
    if not dc_snap.empty:
        dc_by_item = dc_snap.groupby("walmart_item_number", as_index=False).agg(
            dc_oh=("on_hand_warehouse_inventory_in_units_this_year", "sum"),
            dc_oo=("on_order_warehouse_quantity_in_units_this_year", "sum"),
        )
    else:
        dc_by_item = pd.DataFrame(columns=["walmart_item_number", "dc_oh", "dc_oo"])

    breakdown = store_by_item.merge(dc_by_item, on="walmart_item_number", how="outer").fillna(0)
    if not breakdown.empty:
        breakdown["Item"] = breakdown["walmart_item_number"].map(ITEM_LABELS).fillna(
            breakdown["walmart_item_number"].astype(str))
        breakdown["Total"] = breakdown["store_oh"] + breakdown["in_transit"] + breakdown["dc_oh"]
        breakdown = breakdown.sort_values("Total", ascending=False)

        show_inv = breakdown[["Item", "store_oh", "in_whse", "in_transit",
                              "dc_oh", "dc_oo", "Total"]].copy()
        show_inv.columns = ["Item", "In store", "In warehouse", "In transit",
                            "DC on-hand", "DC on-order", "Total network"]
        totals_row = {
            "Item": "Total", "In store": store_oh_n, "In warehouse": in_whse_n,
            "In transit": in_transit_n, "DC on-hand": dc_oh_n, "DC on-order": dc_oo_n,
            "Total network": total_network,
        }
        show_inv = pd.concat([show_inv, pd.DataFrame([totals_row])], ignore_index=True)
        _numcols = ["In store", "In warehouse", "In transit",
                    "DC on-hand", "DC on-order", "Total network"]
        st.dataframe(
            show_inv, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="%d") for c in _numcols},
        )

    _dc_note = (f" · DC snapshot **{dc_latest_snap_date.strftime('%b %d, %Y')}**"
                if dc_latest_snap_date is not None else " · DC data unavailable")
    st.caption(f"Store snapshot **{store_latest_date.strftime('%b %d, %Y')}**{_dc_note}.")

    # ── Daily system inventory flow (last full week + week to date) ───────────
    # The two sections above are point-in-time snapshots; this one trends the
    # whole-network position day by day so you can see inventory building or
    # draining across the pipeline, alongside the daily sell-through that drives
    # it. Window = the last full Walmart week (Sat–Fri) plus the current week to
    # date, anchored on the most recent day of store data.
    st.divider()
    st.subheader("Daily system inventory flow")

    most_recent_norm = most_recent.normalize()
    days_since_sat = (most_recent_norm.weekday() - 5) % 7          # Walmart week = Sat–Fri
    cur_week_start = most_recent_norm - timedelta(days=days_since_sat)
    flow_start = cur_week_start - timedelta(days=7)                # start of the last full week
    last_week_end = cur_week_start - timedelta(days=1)
    st.caption(
        f"Total units in the network each day across the **last full week** "
        f"({flow_start.strftime('%b %d')}–{last_week_end.strftime('%b %d')}) and the "
        f"**current week to date** ({cur_week_start.strftime('%b %d')}–"
        f"{most_recent.strftime('%b %d')}). Total network = store on-hand + in-transit + "
        "DC on-hand. In-warehouse is shown for context but excluded from the total (it "
        "would double-count DC on-hand), as is on-order (not yet arrived). "
        "Respects the sidebar item filter."
    )

    # Respect the sidebar item filter: use the filtered frames (df/dc_df) so this
    # trend tracks whichever items the user has scoped to, consistent with the
    # rest of the dashboard.
    flow = df[df["business_date"] >= flow_start]
    store_daily = flow.groupby("business_date", as_index=False).agg(
        in_store=("store_on_hand_quantity_this_year", "sum"),
        in_whse=("store_in_warehouse_quantity_this_year", "sum"),
        in_transit=("store_in_transit_quantity_this_year", "sum"),
        units_sold=("pos_quantity_this_year", "sum"),
    )
    if not dc_df.empty:
        dc_daily = (
            dc_df[dc_df["inventory_date"] >= flow_start]
            .groupby("inventory_date", as_index=False)
            .agg(dc_oh=("on_hand_warehouse_inventory_in_units_this_year", "sum"))
            .rename(columns={"inventory_date": "business_date"})
        )
    else:
        dc_daily = pd.DataFrame(columns=["business_date", "dc_oh"])

    fd = store_daily.merge(dc_daily, on="business_date", how="left").sort_values("business_date")
    # DC on-hand is a level that carries over; if the DC feed lags a day, carry the
    # last known value forward rather than dropping the network total to zero.
    fd["dc_oh"] = fd["dc_oh"].ffill().fillna(0)
    fd["total_network"] = fd["in_store"] + fd["in_transit"] + fd["dc_oh"]
    fd["delta"] = fd["total_network"].diff()
    fd["date_str"] = fd["business_date"].dt.strftime("%b %d")
    fd["weekday"] = fd["business_date"].dt.strftime("%a")

    if fd.empty:
        st.info("Not enough recent data to chart the daily inventory flow.")
    else:
        latest_total = float(fd["total_network"].iloc[-1])
        net_change = latest_total - float(fd["total_network"].iloc[0])
        dod = fd["delta"].iloc[-1]
        dod = float(dod) if pd.notna(dod) else 0.0
        units_window = float(fd["units_sold"].sum())

        f1, f2, f3 = st.columns(3)
        f1.metric("Total system inventory", f"{latest_total:,.0f}", f"{dod:+,.0f} vs prior day")
        f2.metric("Net change over window", f"{net_change:+,.0f}",
                  help="Latest total network minus the first day shown.")
        f3.metric("Units sold (window)", f"{units_window:,.0f}",
                  help=f"Total POS units across the {len(fd)} days shown.")

        # Stacked bars = inventory position by stage (left axis); overlaid line =
        # daily units sold (right, independent axis) so the throughput driving the
        # level is visible alongside it.
        stage_order = ["In store", "In transit", "DC on-hand"]
        chart_df = fd.rename(columns={
            "in_store": "In store", "in_transit": "In transit", "dc_oh": "DC on-hand"})
        pos_long = chart_df.melt(id_vars=["date_str"], value_vars=stage_order,
                                 var_name="Stage", value_name="Units")
        x_enc = alt.X("date_str:N", sort=list(fd["date_str"]), title=None,
                      axis=alt.Axis(labelAngle=-30))
        bars = alt.Chart(pos_long).mark_bar().encode(
            x=x_enc,
            y=alt.Y("Units:Q", title="Units in network", stack="zero"),
            color=alt.Color("Stage:N", title="Stage",
                            scale=alt.Scale(domain=stage_order,
                                            range=["#185FA5", "#C49A2E", "#27500A"])),
            tooltip=["date_str", "Stage", alt.Tooltip("Units:Q", format=",")],
        )
        line = alt.Chart(fd).mark_line(point=True, strokeWidth=2.5, color="#791F1F").encode(
            x=x_enc,
            y=alt.Y("units_sold:Q", title="Units sold / day"),
            tooltip=["date_str", "weekday",
                     alt.Tooltip("units_sold:Q", format=",", title="Units sold")],
        )
        st.altair_chart(
            alt.layer(bars, line).resolve_scale(y="independent").properties(height=340),
            width='stretch',
        )
        st.caption("Bars = inventory position by stage (left axis). "
                   "Red line = units sold per day (right axis).")

        show_flow = fd[["weekday", "date_str", "in_store", "in_whse", "in_transit", "dc_oh",
                        "total_network", "units_sold", "delta"]].iloc[::-1].copy()
        show_flow.columns = ["Day", "Date", "In store", "In warehouse", "In transit", "DC on-hand",
                             "Total network", "Units sold", "Δ network"]
        _intcols = ["In store", "In warehouse", "In transit", "DC on-hand", "Total network", "Units sold"]
        show_flow[_intcols] = show_flow[_intcols].round().astype(int)
        # Nullable int keeps the oldest day's blank Δ (no prior day) rendering empty.
        show_flow["Δ network"] = show_flow["Δ network"].round().astype("Int64")
        st.dataframe(
            show_flow, width='stretch', hide_index=True,
            column_config={
                **{c: st.column_config.NumberColumn(format="%d") for c in _intcols},
                "Δ network": st.column_config.NumberColumn(
                    format="%+d", help="Day-over-day change in total network units."),
            },
        )

    # ── Week look-ahead: actuals so far + last-year for the next 7 days ────────
    # The daily flow above is history only. For planning the coming week, this
    # extends the same pipeline stages — in store, in warehouse, in transit, DC
    # on-hand, total network — past the latest data day: this year's actuals
    # through that day, then last year's levels (shifted forward 52 weeks / 364
    # days to the same Walmart fiscal weekday) for the next 7 days. Last year's
    # levels aren't in the main store/DC frames (they span only the recent
    # lookback), so they come from the dedicated same-period-last-year loaders.
    st.divider()
    st.subheader("Inventory week look-ahead (actuals + last-year)")

    la_today = most_recent.normalize()
    la_actual_start = la_today - timedelta(days=6)   # last 7 actual days, for context
    la_future_end = la_today + timedelta(days=7)     # the next 7 days
    st.caption(
        f"Daily inventory by stage — in store, in warehouse, in transit, DC on-hand, "
        f"and total network (in store + in transit + DC on-hand): this year's "
        f"**actuals** through {la_today.strftime('%b %d')}, then **last year's** levels for "
        f"the next 7 days ({(la_today + timedelta(days=1)).strftime('%b %d')}–"
        f"{la_future_end.strftime('%b %d')}) as a same-week-last-year planning proxy. "
        "Last-year days are matched by Walmart fiscal weekday (52 weeks back). Store "
        "stages respect the sidebar item + state filters; DC on-hand is network-wide "
        "(item-filtered), as elsewhere on the tab."
    )

    la_df, la_err = load_store_lookahead_data(lookback, slot)
    dla_df, dla_err = load_dc_lookahead_data(lookback, slot)

    # This year's actuals for the recent window. Store stages come from the already
    # loaded (item + state filtered) store frame; DC on-hand from the network-wide
    # dc frame — exactly the split the daily flow above uses.
    act = (
        df[(df["business_date"] >= la_actual_start) & (df["business_date"] <= la_today)]
        .groupby("business_date", as_index=False)
        .agg(in_store=("store_on_hand_quantity_this_year", "sum"),
             in_whse=("store_in_warehouse_quantity_this_year", "sum"),
             in_transit=("store_in_transit_quantity_this_year", "sum"))
    )
    if not dc_df.empty:
        dc_act = (
            dc_df[dc_df["inventory_date"] >= la_actual_start]
            .groupby("inventory_date", as_index=False)
            .agg(dc_oh=("on_hand_warehouse_inventory_in_units_this_year", "sum"))
            .rename(columns={"inventory_date": "business_date"})
        )
        act = act.merge(dc_act, on="business_date", how="left")
    if "dc_oh" not in act.columns:
        act["dc_oh"] = 0.0
    act["source"] = "Actual"

    # Last year's store stages: scope to the same items/states, shift each date
    # forward 364 days (52 weeks → same weekday), and total by day.
    ly_store = pd.DataFrame(columns=["business_date", "in_store", "in_whse", "in_transit"])
    if la_err:
        _section_error("Last-year store inventory look-ahead", la_err)
    elif not la_df.empty:
        ly = la_df[la_df["walmart_item_number"].isin(item_filter)]
        if state_sel:
            ly = ly[ly["state_or_province_code"].astype(str).isin(state_sel)]
        if not ly.empty:
            ly = ly.assign(business_date=ly["inventory_date"] + pd.Timedelta(days=364))
            ly_store = (
                ly.groupby("business_date", as_index=False)
                .agg(in_store=("store_on_hand_quantity", "sum"),
                     in_whse=("store_in_warehouse_quantity", "sum"),
                     in_transit=("store_in_transit_quantity", "sum"))
            )

    # Last year's DC on-hand (network-wide, item-filtered), shifted the same way.
    ly_dc = pd.DataFrame(columns=["business_date", "dc_oh"])
    if dla_err:
        _section_error("Last-year DC inventory look-ahead", dla_err)
    elif not dla_df.empty:
        d = dla_df[dla_df["walmart_item_number"].isin(item_filter)]
        if not d.empty:
            d = d.assign(business_date=d["inventory_date"] + pd.Timedelta(days=364))
            ly_dc = d.groupby("business_date", as_index=False).agg(dc_oh=("dc_on_hand", "sum"))

    ly_daily = ly_store.merge(ly_dc, on="business_date", how="outer")
    fut = ly_daily[(ly_daily["business_date"] > la_today)
                   & (ly_daily["business_date"] <= la_future_end)].copy()
    fut["source"] = "Last yr"

    if act.empty and fut.empty:
        st.info("Not enough data to build the inventory look-ahead.")
    else:
        combined = pd.concat([act, fut], ignore_index=True)
        # Concatenating a datetime column with an empty (object-dtype) look-ahead
        # frame — when both LY sources are missing — coerces business_date to
        # object; restore it so the .dt accessors below work.
        combined["business_date"] = pd.to_datetime(combined["business_date"])
        combined = combined.sort_values("business_date")
        _stages = ["in_store", "in_whse", "in_transit", "dc_oh"]
        for c in _stages:
            combined[c] = pd.to_numeric(combined.get(c), errors="coerce").fillna(0)
        # Total network = in store + in transit + DC on-hand. In-warehouse is
        # excluded (it would double-count DC stock earmarked for stores), matching
        # the daily flow above.
        combined["total_network"] = (
            combined["in_store"] + combined["in_transit"] + combined["dc_oh"])
        combined["weekday"] = combined["business_date"].dt.strftime("%a")
        combined["date_str"] = combined["business_date"].dt.strftime("%b %d")

        fut_rows = combined[combined["source"] == "Last yr"]
        n_fut = len(fut_rows)
        latest = combined[(combined["source"] == "Actual")
                          & (combined["business_date"] == la_today)]
        latest_net = float(latest["total_network"].iloc[0]) if not latest.empty else 0.0
        m1, m2, m3 = st.columns(3)
        m1.metric("Total network now (actual)", f"{latest_net:,.0f}",
                  help=f"In store + in transit + DC on-hand on {la_today.strftime('%b %d')}.")
        m2.metric("Next 7 days network — last yr (avg/day)",
                  f"{fut_rows['total_network'].mean():,.0f}" if n_fut else "—",
                  help="Average daily total network over the coming 7 days, last year.")
        m3.metric("Next 7 days in store — last yr (avg/day)",
                  f"{fut_rows['in_store'].mean():,.0f}" if n_fut else "—",
                  help="Average daily in-store on-hand over the coming 7 days, last year.")

        # Chart: stacked bars = inventory position by stage (in store + in transit +
        # DC on-hand = total network), matching the daily flow above. The future
        # (last-year) days are shaded so they read as a projection, not actuals.
        chart_src = combined.sort_values("business_date")  # chronological left→right
        order = list(chart_src["date_str"])
        stage_order = ["In store", "In transit", "DC on-hand"]
        chart_df = chart_src.rename(columns={
            "in_store": "In store", "in_transit": "In transit", "dc_oh": "DC on-hand"})
        pos_long = chart_df.melt(id_vars=["date_str"], value_vars=stage_order,
                                 var_name="Stage", value_name="Units")
        x_enc = alt.X("date_str:N", sort=order, title=None,
                      axis=alt.Axis(labelAngle=-30))
        layers = []
        fut_days = chart_src.loc[chart_src["source"] == "Last yr", ["date_str"]]
        if not fut_days.empty:
            layers.append(
                alt.Chart(fut_days).mark_rect(color="#791F1F", opacity=0.10).encode(x=x_enc)
            )
        layers.append(
            alt.Chart(pos_long).mark_bar().encode(
                x=x_enc,
                y=alt.Y("Units:Q", title="Units in network", stack="zero"),
                color=alt.Color("Stage:N", title="Stage",
                                scale=alt.Scale(domain=stage_order,
                                                range=["#185FA5", "#C49A2E", "#27500A"])),
                tooltip=["date_str", "Stage", alt.Tooltip("Units:Q", format=",")],
            )
        )
        st.altair_chart(alt.layer(*layers).properties(height=340), width='stretch')
        st.caption("Bars = inventory position by stage (in store + in transit + DC on-hand "
                   "= total network); in-warehouse is shown in the table only, to avoid "
                   "double-counting DC stock. Shaded days = last year's levels for the "
                   "next 7 days.")

        if fut.empty and not (la_err or dla_err):
            st.info("Last-year levels for the next 7 days aren't available yet — "
                    "showing actuals only.")

        # Table — same columns as the daily flow above, ordered latest first so the
        # furthest look-ahead day sits on top. Future (last-year) rows are shaded to
        # set them apart from the actuals.
        show_la = combined.sort_values("business_date", ascending=False)[
            ["weekday", "date_str", "source", "in_store", "in_whse", "in_transit",
             "dc_oh", "total_network"]].copy()
        show_la.columns = ["Day", "Date", "Source", "In store", "In warehouse",
                           "In transit", "DC on-hand", "Total network"]
        _whole = ["In store", "In warehouse", "In transit", "DC on-hand", "Total network"]
        show_la[_whole] = show_la[_whole].round().astype(int)
        _future_mask = (show_la["Source"] == "Last yr").to_numpy()
        styled_la = show_la.style.apply(
            lambda _col: ["background-color: rgba(121, 31, 31, 0.10)" if f else ""
                          for f in _future_mask],
            axis=0,
        )
        st.dataframe(
            styled_la, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="%d") for c in _whole},
        )

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
        br_filt = br_df[br_df["walmart_item_number"].isin(item_filter)]
        if br_filt.empty:
            st.info("No adjustments for the selected items.")
        else:
            # Daily total adjustment volume and net direction
            br_daily = br_filt.groupby("adjustment_date", as_index=False).agg(
                net_adj=("adjustment_qty_ty", "sum"),
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
#   TAB 6 — CHANNELS (omni sales, ecom inventory, returns)
# ═══════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_channels():
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
        omni_filt = omni_df[omni_df["walmart_item_number"].isin(item_filter)]
        # KPIs and the channel bar follow the sidebar performance window, applied
        # to the omni feed's own dates; the mix-over-time chart below always spans
        # the full range so the window has context.
        omni_w = omni_filt[omni_filt["business_date"] >= window_start]

        omni_total_ty = float(omni_w["units_ty"].sum())
        omni_total_ly = float(omni_w["units_ly"].sum())
        omni_yoy = ((omni_total_ty - omni_total_ly) / omni_total_ly * 100) if omni_total_ly else 0

        in_store_ty = float(df_window["pos_quantity_this_year"].sum())
        in_store_ly = float(df_window["pos_quantity_last_year"].sum())
        in_store_yoy = ((in_store_ty - in_store_ly) / in_store_ly * 100) if in_store_ly else 0

        st.caption(f"KPIs and channel totals: **{window_label}**.")
        ch_k1, ch_k2, ch_k3 = st.columns(3)
        ch_k1.metric("In-store units", f"{int(in_store_ty):,}", f"{in_store_yoy:+.1f}% YoY")
        ch_k2.metric("Omni units (all channels)", f"{int(omni_total_ty):,}", f"{omni_yoy:+.1f}% YoY")
        non_store = max(0, omni_total_ty - in_store_ty)
        ch_k3.metric("Non-in-store omni", f"{int(non_store):,}",
                     help="Online, pickup, ship-from-store, etc.")

        # Channel mix
        st.markdown(f"**Sales by service channel — {window_label}**")
        by_chan = omni_w.groupby("service_channel", as_index=False, observed=True).agg(
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

        # ── Channel mix over time ────────────────────────────────────────────
        st.markdown("**Channel mix over time**")
        st.caption(
            "Weekly units by service channel. A shrinking in-store slice with a growing "
            "pickup/delivery slice is channel migration, not lost demand — read it before "
            "calling a unit decline a problem."
        )
        chan_tl = omni_filt.copy()
        chan_tl["week_start"] = _walmart_week_start(chan_tl["business_date"])
        chan_wk = chan_tl.groupby(["week_start", "service_channel"], as_index=False,
                                  observed=True)["units_ty"].sum()
        if not chan_wk.empty:
            mix_mode = st.radio("Show as", ["Units", "% mix"], index=0, horizontal=True,
                                key="chan_mix_mode")
            y_enc = (alt.Y("units_ty:Q", stack="normalize", title="Share of units",
                           axis=alt.Axis(format="%"))
                     if mix_mode == "% mix"
                     else alt.Y("units_ty:Q", stack="zero", title="Units"))
            st.altair_chart((alt.Chart(chan_wk).mark_area().encode(
                x=alt.X("week_start:T", title="Week"),
                y=y_enc,
                color=alt.Color("service_channel:N", legend=alt.Legend(title="Channel")),
                tooltip=[alt.Tooltip("week_start:T", title="Week"), "service_channel:N",
                         alt.Tooltip("units_ty:Q", format=",", title="Units")],
            ).properties(height=300)), width='stretch')

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
        ecom_filt = ecom_df[ecom_df["walmart_item_number"].isin(item_filter)]
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
            # KPIs follow the sidebar performance window; the weekly trend and
            # reason breakdown below keep the full range (a 1-day reason split
            # is too sparse to point at a root cause).
            ret_w = ret_filt[ret_filt["return_date"] >= window_start]
            ret_ty_total = int(ret_w["returns_ty"].sum())
            ret_ly_total = int(ret_w["returns_ly"].sum())
            ret_yoy = ((ret_ty_total - ret_ly_total) / ret_ly_total * 100) if ret_ly_total else 0
            # Return rate = returns / units sold, both over the same window
            units_total = float(df_window["pos_quantity_this_year"].sum())
            ret_rate = (ret_ty_total / units_total * 100) if units_total else 0

            st.caption(f"KPIs: **{window_label}**.")
            r_k1, r_k2, r_k3 = st.columns(3)
            r_k1.metric("Returns (units)", f"{ret_ty_total:,}", f"{ret_yoy:+.1f}% YoY",
                        delta_color="inverse")
            r_k2.metric("Return rate", f"{ret_rate:.2f}%",
                        help="returns / units sold over the selected performance window")
            r_k3.metric("Return $ (TY)", f"${ret_w['return_sales_ty'].sum():,.0f}")

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

            # ── Returns by reason ────────────────────────────────────────────
            st.markdown("**Why product comes back (return reason)**")
            st.caption(
                "Breaks returns down by Walmart's return-reason code over the **full date "
                "range** (a short window is too sparse to read). A spike concentrated in "
                "one reason (damage, quality, wrong item) points at a fixable root cause "
                "rather than general softness."
            )
            by_reason = ret_filt.groupby("return_reason", as_index=False, observed=True).agg(
                returns_ty=("returns_ty", "sum"),
                return_sales_ty=("return_sales_ty", "sum"),
            )
            by_reason = by_reason[by_reason["returns_ty"] != 0].sort_values(
                "returns_ty", ascending=False).head(12)
            # return_reason is categorical; go through object so fillna can add a
            # label that isn't an existing category without raising.
            by_reason["return_reason"] = by_reason["return_reason"].astype("object").fillna("(blank)").astype(str)
            if not by_reason.empty:
                st.altair_chart((alt.Chart(by_reason).mark_bar(color="#791F1F").encode(
                    x=alt.X("returns_ty:Q", title="Returns (units)"),
                    y=alt.Y("return_reason:N", sort="-x", title="Return reason code"),
                    tooltip=[alt.Tooltip("return_reason:N", title="Reason"),
                             alt.Tooltip("returns_ty:Q", format=",", title="Returns"),
                             alt.Tooltip("return_sales_ty:Q", format="$,.0f", title="Return $")],
                ).properties(height=max(160, 26 * len(by_reason)))), width='stretch')


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 7 — DISTRIBUTION
# ═══════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_distribution():
    # ── NEW: Modular coverage (distribution gap) ─────────────────────────────
    st.subheader("Modular coverage gap")
    st.caption(
        "Stores in the active modular plan vs. stores that actually sold this item "
        "anywhere in the **full date range**. The performance window doesn't apply "
        "here — one quiet day shouldn't count a store as a coverage gap. "
        "The gap = execution failure (item should be carried but isn't moving)."
    )

    mod_df, mod_err = load_modular_data(slot)
    if mod_err:
        _section_error("Modular plan data", mod_err)
    elif mod_df.empty:
        st.info("No modular plan records returned.")
    else:
        mod_filt = mod_df[mod_df["walmart_item_number"].isin(item_filter)]
        # Planogram universe = currently-valid placements only. The query keeps
        # the validity flag; rows explicitly marked invalid would otherwise
        # overstate "In plan" (and understate coverage).
        if "item_valid_flag" in mod_filt.columns:
            _invalid = (mod_filt["item_valid_flag"].astype(str).str.strip().str.lower()
                        .isin(["n", "no", "0", "false", "f"]))
            mod_filt = mod_filt[~_invalid]
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
    st.subheader(f"Performance by state — {window_label}")
    st.caption("Follows the sidebar performance window (most recent day / last 7 days / full lookback).")

    state_perf_all = df_window.groupby("state_or_province_code", as_index=False, observed=True).agg(
        units_ty=("pos_quantity_this_year", "sum"),
        units_ly=("pos_quantity_last_year", "sum"),
        sales_ty=("pos_sales_this_year", "sum"),
        stores=("store_number", "nunique"),
    )
    state_perf_all["yoy_pct"] = _yoy_pct(state_perf_all["units_ty"], state_perf_all["units_ly"])
    state_perf_all["state"] = state_perf_all["state_or_province_code"].astype(str)
    state_perf_all["fips"] = state_perf_all["state"].map(STATE_FIPS)

    # Geographic heat map (choropleth). The TopoJSON is fetched by the viewer's
    # browser when Vega renders, so the server makes no outbound call; if a viewer
    # can't reach the CDN the map simply doesn't draw — the ranked bar below always
    # does, so no information is lost.
    map_metric = st.radio("Map color", ["Units TY", "YoY %"], index=0, horizontal=True,
                          key="state_map_metric")
    geo = state_perf_all.dropna(subset=["fips"]).copy()
    geo["fips"] = geo["fips"].astype(int)
    if not geo.empty:
        if map_metric == "YoY %":
            color_enc = alt.Color("yoy_pct:Q", scale=alt.Scale(scheme="redyellowgreen", domainMid=0),
                                  legend=alt.Legend(title="YoY %"))
        else:
            color_enc = alt.Color("units_ty:Q", scale=alt.Scale(scheme="blues"),
                                  legend=alt.Legend(title="Units TY"))
        states_topo = alt.topo_feature(US_STATES_TOPO_URL, "states")
        choropleth = (alt.Chart(states_topo).mark_geoshape(stroke="white", strokeWidth=0.5).encode(
            color=color_enc,
            tooltip=[alt.Tooltip("state:N", title="State"),
                     alt.Tooltip("units_ty:Q", format=",", title="Units TY"),
                     alt.Tooltip("units_ly:Q", format=",", title="Units LY"),
                     alt.Tooltip("yoy_pct:Q", format=".1f", title="YoY %"),
                     alt.Tooltip("stores:Q", format=",", title="Stores")],
        ).transform_lookup(
            lookup="id",
            from_=alt.LookupData(geo, "fips",
                                 ["units_ty", "units_ly", "yoy_pct", "stores", "state"]),
        ).project("albersUsa").properties(height=380))
        st.altair_chart(choropleth, width='stretch')

    st.markdown("**Top 20 states by units**")
    state_perf = state_perf_all.sort_values("units_ty", ascending=False).head(20)

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


# ═══════════════════════════════════════════════════════════════════════════
#   TAB 8 — STORE ACTIONS (field-intervention list + vendor export)
# ═══════════════════════════════════════════════════════════════════════════
@st.fragment
def _render_actions():
    st.subheader("Stores flagged for an in-person visit")
    st.caption(
        "Surfaces stores with a **physically fixable** problem — product that should be "
        "selling but isn't — so you can dispatch a rep. Compares each store's **recent 3-day** rate "
        "against its own **trailing run-rate**, so it reacts within 1–3 days without chasing daily "
        "noise. Export the ranked list (with mailing address) to hand to the field-service company. "
        "This tab has its own item scope below (independent of the sidebar item filter); "
        "the sidebar **state** filter still applies, so you can pull a regional dispatch list."
    )

    # This tab carries its own item scope (independent of the sidebar View) so a
    # dispatch list can be pulled for all items, just bins, or just shelf without
    # disturbing the rest of the dashboard. Built from the unfiltered df_all.
    scope = st.radio(
        "Item scope", ["All items", "Both Bins (full + half)", "Shelf only"],
        index=0, horizontal=True, key="actions_item_scope",
        help="Which SKUs to analyze for store-level problems.")
    if scope == "All items":
        scope_items = list(ACTIVE_ITEMS)
    elif scope == "Both Bins (full + half)":
        scope_items = list(BIN_ITEMS)
    else:
        scope_items = list(SHELF_ITEMS)
    dfa = (df_all if scope == "All items"
           else df_all[df_all["walmart_item_number"].isin(scope_items)])

    if dfa.empty:
        st.info("No store data for the selected item scope.")
    else:
        mr = dfa["business_date"].max()    # most recent day within this scope

        # ── Analysis windows ──────────────────────────────────────────────────
        # A short recent window reacts fast; a long, stable trailing run-rate as
        # the baseline keeps low daily volume from whipsawing the comparison (a
        # 3-day-vs-3-day check would fire constantly on lumpy single-SKU sales).
        recent_start = mr - timedelta(days=2)         # last 3 days (incl. mr)
        base_end = mr - timedelta(days=3)             # baseline ends before the recent window
        base_start = mr - timedelta(days=23)          # ~21-day trailing run-rate
        last7_start = mr - timedelta(days=6)          # for the OOS-of-7 check

        recent = dfa[dfa["business_date"] >= recent_start]
        baseline = dfa[(dfa["business_date"] >= base_start) & (dfa["business_date"] <= base_end)]
        last7 = dfa[dfa["business_date"] >= last7_start]
        recent_days = max(1, recent["business_date"].nunique())
        baseline_days = max(1, baseline["business_date"].nunique())

        # Per-store recent & baseline volumes.
        rec = recent.groupby("store_number", as_index=False).agg(
            recent_units=("pos_quantity_this_year", "sum"))
        bas = baseline.groupby("store_number", as_index=False).agg(
            baseline_units=("pos_quantity_this_year", "sum"))
        # OOS days and LY volume over the last 7 days (chronic-OOS supply check).
        sday7 = last7.groupby(["store_number", "business_date"], as_index=False).agg(
            day_oh=("store_on_hand_quantity_this_year", "sum"))
        oos_days = (sday7[sday7["day_oh"] == 0].groupby("store_number").size()
                    .rename("oos_days").reset_index())
        ly7 = last7.groupby("store_number", as_index=False).agg(
            units_7d_ly=("pos_quantity_last_year", "sum"))
        # Latest on-hand / in-warehouse / in-transit at each store's most recent day.
        latest_mask = dfa.groupby("store_number")["business_date"].transform("max") == dfa["business_date"]
        latest = dfa[latest_mask].groupby("store_number", as_index=False).agg(
            on_hand=("store_on_hand_quantity_this_year", "sum"),
            in_warehouse=("store_in_warehouse_quantity_this_year", "sum"),
            in_transit=("store_in_transit_quantity_this_year", "sum"),
        )
        # Dark streak = consecutive most-recent data-days with zero sales (the run
        # after each store's last sale). Drives the velocity-scaled "went dark" flag.
        sd_all = dfa.groupby(["store_number", "business_date"], as_index=False).agg(
            day_units=("pos_quantity_this_year", "sum"))
        last_sell = (sd_all[sd_all["day_units"] > 0].groupby("store_number")["business_date"]
                     .max().rename("last_sell").reset_index())
        sd_all = sd_all.merge(last_sell, on="store_number", how="left")
        sd_all["after_last_sell"] = sd_all["business_date"] > sd_all["last_sell"].fillna(pd.Timestamp.min)
        dark = (sd_all.groupby("store_number")["after_last_sell"].sum()
                .rename("dark_streak").reset_index())
        state_map = (dfa.groupby("store_number", observed=True)["state_or_province_code"]
                     .agg(lambda s: s.iloc[0]).astype(str).rename("state_code").reset_index())

        s = pd.DataFrame({"store_number": dfa["store_number"].unique()})
        for part in [rec, bas, oos_days, ly7, latest, dark, state_map]:
            s = s.merge(part, on="store_number", how="left")
        for col in ["recent_units", "baseline_units", "oos_days", "units_7d_ly",
                    "on_hand", "in_warehouse", "in_transit", "dark_streak"]:
            s[col] = s[col].fillna(0)
        s["recent_daily"] = s["recent_units"] / recent_days
        s["baseline_daily"] = s["baseline_units"] / baseline_days

        st.caption(
            f"Recent window: **last 3 days** ({recent_start.strftime('%b %d')} – {mr.strftime('%b %d, %Y')}) "
            f"· baseline run-rate: trailing {baseline_days} days "
            f"({base_start.strftime('%b %d')} – {base_end.strftime('%b %d')})."
        )

        # ── Tuning controls ───────────────────────────────────────────────────
        c1, c2, c3 = st.columns(3)
        with c1:
            min_oh = st.slider(
                "Min. on-hand to count as 'has stock'", 1, 50, 4,
                help="A store needs at least this many units on hand before zero sales "
                     "counts as a problem (filters out stores that simply don't carry it).")
        with c2:
            decline_drop = st.slider(
                "Decline sensitivity (drop vs normal rate)", 10, 95, 20, step=5, format="%d%%",
                help="Flag a still-stocked store whose recent daily rate fell at least this much "
                     "below its trailing run-rate. Lower = more sensitive (flags more stores).")
        with c3:
            dark_floor = st.slider(
                "'Went dark' sensitivity (lost units)", 1, 15, 4,
                help="A stocked store that stopped selling is flagged once its expected lost units "
                     "(normal daily rate × silent days) reach this. Lower = reacts faster, "
                     "especially for high-velocity stores.")

        # ── Benchmarks for the impact estimate ───────────────────────────────
        sellers = s[s["recent_daily"] > 0]
        peer_med_day = float(sellers["recent_daily"].median()) if len(sellers) else 0.0
        _units_all = float(dfa["pos_quantity_this_year"].sum())
        _sales_all = float(dfa["pos_sales_this_year"].sum())
        blended_aur = (_sales_all / _units_all) if _units_all else 0.0
        # Expected daily velocity: the store's own run-rate if it has one, else the
        # peer median — never invents demand beyond what's demonstrated.
        s["expected_daily"] = np.where(s["baseline_units"] > 0, s["baseline_daily"], peer_med_day)

        BASE_FLOOR = 5   # min baseline units over the trailing window to count as an established seller

        # ── Classification ────────────────────────────────────────────────────
        # Severity: 3 = High, 2 = Medium, 1 = Low. A store may trip several rules;
        # we keep the max severity, list every reason, and take the worst impact.
        def _classify(r):
            """r is an itertuples row — attribute access over a plain tuple keeps
            this loop ~10× faster than a per-row apply building a pd.Series,
            which matters because every threshold-slider change re-runs it."""
            issues, sev, lost = [], 0, 0.0
            has_stock = r.on_hand >= min_oh
            established = r.baseline_units >= BASE_FLOOR
            recent_zero = r.recent_units == 0
            # A. Went dark — established seller, stocked, that has stopped selling.
            #    Keyed off the trailing zero-sale streak with a velocity-scaled gate:
            #    a brisk seller (where even one zero day is statistically rare) flags
            #    after ~1 silent day, a slow seller only after a longer gap.
            dark_fired = False
            if (has_stock and r.baseline_units > 0 and r.dark_streak >= 1
                    and r.baseline_daily * r.dark_streak >= dark_floor):
                issues.append(
                    f"Was selling ~{r.baseline_daily:.1f}/day but 0 sales for the last "
                    f"{int(r.dark_streak)} day(s) with stock on hand — likely off the floor")
                sev = max(sev, 3)
                lost = max(lost, r.baseline_daily * 7)
                dark_fired = True
            # B. Idle backroom stock — units in the back, nothing selling.
            if recent_zero and r.in_warehouse > 0:
                issues.append(f"{r.in_warehouse:.0f} units sitting in the store's back room with no recent sales")
                sev = max(sev, 3)
                lost = max(lost, r.expected_daily * 7)
            # C. Declining vs its own normal run-rate (still selling, but down). Skipped
            #    when "went dark" already fired, so the two don't contradict each other.
            if (established and has_stock and not dark_fired and not recent_zero
                    and r.recent_daily <= (1 - decline_drop / 100.0) * r.baseline_daily):
                drop_pct = (1 - r.recent_daily / r.baseline_daily) * 100
                issues.append(
                    f"Selling {drop_pct:.0f}% below its normal rate "
                    f"({r.baseline_daily:.1f}→{r.recent_daily:.1f} units/day) despite stock")
                sev = max(sev, 3 if drop_pct >= 70 else 2 if drop_pct >= 40 else 1)
                lost = max(lost, (r.baseline_daily - r.recent_daily) * 7)
            # D. Stuck stock — holding stock but no movement anywhere in the lookback.
            if has_stock and recent_zero and r.baseline_units == 0:
                issues.append("Holding on-hand stock but no sales in the lookback — stock may be stranded / never set on the floor")
                sev = max(sev, 2)
                lost = max(lost, peer_med_day * 7)
            # E. Chronic OOS with supply available upstream (or proven LY demand).
            if r.oos_days >= 5 and (r.in_warehouse > 0 or r.in_transit > 0 or r.units_7d_ly >= 3):
                why = ("replenishment available (in back room / in transit)"
                       if (r.in_warehouse > 0 or r.in_transit > 0)
                       else f"LY demand of {int(r.units_7d_ly)} units this week")
                issues.append(f"Out of stock {int(r.oos_days)} of 7 days despite {why}")
                sev = max(sev, 2)
                lost = max(lost, r.expected_daily * r.oos_days)
            # F. Underperforming vs comparable stores.
            if (not recent_zero and peer_med_day > 0 and has_stock and established
                    and r.recent_daily < 0.25 * peer_med_day):
                issues.append("Selling far below comparable stores (under 25% of the peer daily rate)")
                sev = max(sev, 1)
                lost = max(lost, (peer_med_day - r.recent_daily) * 7)
            return sev, "  •  ".join(issues), round(lost)

        _results = [_classify(r) for r in s.itertuples(index=False)]
        s["severity"], s["issues"], s["lost_units"] = (
            zip(*_results) if _results else ((), (), ()))
        s["lost_units"] = pd.to_numeric(s["lost_units"], errors="coerce").fillna(0)
        s["lost_sales"] = (s["lost_units"] * blended_aur).round(0)

        flagged = s[s["severity"] > 0].copy()
        sev_label = {3: "🔴 High", 2: "🟠 Medium", 1: "🟡 Low"}
        flagged["priority"] = flagged["severity"].map(sev_label)

        # ── Priority filter ───────────────────────────────────────────────────
        levels = st.multiselect(
            "Priority levels to include",
            options=["🔴 High", "🟠 Medium", "🟡 Low"],
            default=["🔴 High", "🟠 Medium", "🟡 Low"],
            help="Trim the list to the urgency you want to dispatch.")
        if levels:
            flagged = flagged[flagged["priority"].isin(levels)]
        flagged = flagged.sort_values(["severity", "lost_units"], ascending=[False, False]).reset_index(drop=True)

        # ── KPI strip ─────────────────────────────────────────────────────────
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Stores flagged", f"{len(flagged):,}",
                  help="Stores with at least one rep-fixable problem right now.")
        k2.metric("High priority", f"{int((flagged['severity'] == 3).sum()):,}", delta_color="inverse")
        k3.metric("Est. lost units (/wk)", f"{flagged['lost_units'].sum():,.0f}",
                  help="Sum of each flagged store's estimated weekly shortfall vs its expected velocity.")
        k4.metric("Est. lost sales (/wk)", f"${flagged['lost_sales'].sum():,.0f}",
                  help="Estimated weekly lost units priced at the blended average unit retail.")

        with st.expander("How stores are flagged (methodology)"):
            st.markdown(
                f"""
Each store compares its **recent 3-day** selling rate against its own **trailing {baseline_days}-day
run-rate** (a stable baseline that won't whipsaw on low daily volume), plus a fast "went dark" check.
Only problems a **rep can physically fix in the store** are flagged:

| Flag | Trigger | Priority |
|---|---|---|
| **Went dark (stocked)** | Established seller with stock on hand but 0 recent sales; fires once expected lost units (normal rate × silent days) reach **{dark_floor}** — so a brisk seller flags after ~1 day, a slow seller only after a longer gap | 🔴 High |
| **Idle backroom stock** | No recent sales while units sit in the store's back room | 🔴 High |
| **Declining vs normal** | Recent daily rate ≥ {decline_drop}% below the store's run-rate, with stock | 🔴 ≥70% · 🟠 ≥40% · 🟡 ≥{decline_drop}% |
| **Stuck stock** | Holding on-hand stock but no sales anywhere in the lookback | 🟠 Medium |
| **Chronic OOS w/ supply** | Out of stock ≥ 5 of 7 days with replenishment available (or proven LY demand) | 🟠 Medium |
| **Underperforming vs peers** | Established, stocked store selling under 25% of the peer daily rate | 🟡 Low |

**Ranking** — stores sort by estimated **lost units per week**: the gap between the store's expected
rate (its run-rate, or the peer median of {peer_med_day:.1f}/day if it has no history) and its recent rate,
scaled to a week. Lost sales price that at the blended unit retail of ${blended_aur:.2f}.

The thresholds above are adjustable. Stores that simply don't carry the item (no stock, no history,
no upstream supply) are **not** flagged. The short recent window reacts within 1–3 days; the long
baseline keeps low-volume daily noise from triggering false dispatches.
"""
            )

        if flagged.empty:
            st.success("No stores meet the intervention criteria for the current filters. Nothing to dispatch. 🎉")
        else:
            # ── Enrich with mailing address for the vendor export ────────────
            dir_df, dir_err = load_store_directory(slot)
            if dir_err or dir_df.empty:
                _section_error("Store address directory", dir_err or "no rows")
                st.info(
                    "Mailing addresses couldn't be loaded from `store_dim`, so the export below "
                    "lists store number + state only. The rest of the analysis is unaffected."
                )
                out = flagged.copy()
                out["address"] = pd.NA
                out["city"] = pd.NA
                out["state"] = out["state_code"]
                out["zip"] = pd.NA
            else:
                out = flagged.merge(
                    dir_df[["store_number"] + [c for c in ["address", "city", "state", "zip"] if c in dir_df.columns]],
                    on="store_number", how="left")
                # Fall back to the sales feed's state code where the dimension lacks it.
                if "state" in out.columns:
                    out["state"] = out["state"].fillna(out["state_code"])
                else:
                    out["state"] = out["state_code"]
                for c in ["address", "city", "zip"]:
                    if c not in out.columns:
                        out[c] = pd.NA
                missing_addr = int(out["address"].isna().sum()) if "address" in out.columns else len(out)
                if missing_addr:
                    st.caption(f"⚠ {missing_addr:,} of {len(out):,} flagged stores have no address on file in `store_dim`.")

            # ── Build the export / display frame, address columns first ──────
            export = pd.DataFrame({
                "Store Number": out["store_number"],
                "Address": out.get("address"),
                "City": out.get("city"),
                "State": out.get("state"),
                "Zip": out.get("zip"),
                "Priority": out["priority"],
                "Issue(s)": out["issues"],
                "Recent units/day": out["recent_daily"].round(2),
                "Normal units/day": out["baseline_daily"].round(2),
                "Days silent": out["dark_streak"].astype(int),
                "On hand": out["on_hand"].astype(int),
                "In back room": out["in_warehouse"].astype(int),
                "OOS days (of 7)": out["oos_days"].astype(int),
                "Est. lost units/wk": out["lost_units"].astype(int),
                "Est. lost $/wk": out["lost_sales"].astype(int),
            })

            addr_only = st.checkbox(
                "Export addresses only (Store #, Address, City, State, Zip + Priority)",
                value=False,
                help="Tick for a clean dispatch list with just the columns the field-service "
                     "company needs; untick to include the supporting metrics.")
            export_for_download = (
                export[["Store Number", "Address", "City", "State", "Zip", "Priority", "Issue(s)"]]
                if addr_only else export
            )
            csv_bytes = export_for_download.to_csv(index=False).encode("utf-8")
            fname = f"store_intervention_list_{mr.strftime('%Y%m%d')}.csv"
            dl1, dl2 = st.columns([1, 3])
            with dl1:
                st.download_button(
                    "⬇ Download dispatch list (CSV)", data=csv_bytes, file_name=fname,
                    mime="text/csv", type="primary",
                    help="Ranked list of flagged stores with mailing address — ready to send to the field-service vendor.")
            with dl2:
                st.caption(f"**{len(export_for_download):,} stores** · file: `{fname}`")

            st.dataframe(
                export, width='stretch', hide_index=True, height=460,
                column_config={
                    "Issue(s)": st.column_config.TextColumn(width="large"),
                    "Recent units/day": st.column_config.NumberColumn(format="%.2f"),
                    "Normal units/day": st.column_config.NumberColumn(format="%.2f"),
                    "Est. lost $/wk": st.column_config.NumberColumn(format="$%d"),
                })
            st.caption(
                "Sorted by priority, then estimated lost units. Hand the CSV to the field-service "
                "company; the address columns come straight from `store_dim`."
            )


# ─── Render the tabs ─────────────────────────────────────────────────────────
with tab_overview:
    _render_overview()
with tab_sales:
    _render_sales()
with tab_drivers:
    _render_drivers()
with tab_forecast:
    _render_forecast()
with tab_inv:
    _render_inventory()
with tab_channels:
    _render_channels()
with tab_dist:
    _render_distribution()
with tab_actions:
    _render_actions()


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
