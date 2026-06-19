"""Locate the store demand-forecast source table at runtime.

Why this exists
---------------
The Forecast tab needs a *daily* store-item demand forecast shaped as:

    forecast_date, store_number, walmart_item_number, forecast_quantity

The original ``sql/forecast_query.sql`` hard-coded the table name
``daily_demand_forecast`` — a name that matches no BI Link dataset (every other
query in the app was "confirmed against wmt-dv-bi-link-prod.dv_supplier"; this
one never was). When that table doesn't exist in the deployed dataset the whole
query throws, which is exactly what surfaced on the tab as
"Forecast data is temporarily unavailable." The *columns* it selected
(``fcst_dt`` Forecast Date, ``fcst_tot_dmand_each_qty`` Forecast Total Demand
Each Quantity, keyed by ``store_nbr`` + ``wm_item_nbr``) are valid BI Link
daily-forecast fields per the data-element glossary (see
``docs/data_element_glossary.csv``) — so only the physical table name was wrong.

Rather than swap one unverifiable guess for another, we ask BigQuery's
``INFORMATION_SCHEMA`` which table in the dataset actually carries the forecast
columns and build the query against it. The physical name can differ between BI
Link exports, so discovery makes the tab self-healing: whatever the forecast
table is called, as long as it carries the standard BI Link forecast columns the
tab finds it.

Import-safe: no ``streamlit`` and no BigQuery client here. Callers pass a
``run_query(sql) -> DataFrame`` callable plus the project/dataset, so the live
app (streamlit_app.py) and the snapshot builder (snapshot_build.py) share one
implementation.
"""
from __future__ import annotations

import re

# BI Link forecast vocabulary, lowercased. Names verified against the
# data-element glossary's "Technical Name" column (Products = BI Link).
_DATE_COL = "fcst_dt"                      # Forecast Date (daily, YYYY-MM-DD)
_WEEK_COL = "fcst_wm_yr_wk_nbr"            # Forecast Walmart Year Week (weekly grain)
_STORE_COL = "store_nbr"                   # Store Number
_ITEM_COL = "wm_item_nbr"                  # Walmart Item Number
# Forecast quantity, in preference order. fcst_tot_dmand_each_qty is the daily
# "total demand" measure the original query used; final_fcst_each_qty is the
# store_demand_forecast dataset's measure; fcst_qty is the API-feed name.
_QTY_COLS = ["fcst_tot_dmand_each_qty", "final_fcst_each_qty", "fcst_qty"]

# All columns we ask INFORMATION_SCHEMA about (keeps the metadata read tiny).
_WANTED = [_DATE_COL, _WEEK_COL, _STORE_COL, _ITEM_COL, *_QTY_COLS]

# A real BigQuery table name is letters/digits/underscores; guard the only
# identifier we interpolate that doesn't come from our own fixed vocab.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")

# Hints that a table is the forecast table when several tables expose the same
# key columns; used only to break ties deterministically.
_NAME_HINTS = ("forecast", "fcst", "demand", "dmand")


def _best_table(candidates: list[dict]) -> dict | None:
    """Deterministically pick among same-grain candidate tables: prefer a name
    that looks like a forecast table, then the shortest name, then alphabetical."""
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda s: (
            0 if any(h in s["table"].lower() for h in _NAME_HINTS) else 1,
            len(s["table"]),
            s["table"].lower(),
        ),
    )[0]


def discover_forecast_spec(run_query, project: str, dataset: str,
                           override_table: str | None = None) -> dict | None:
    """Find the dataset's store demand-forecast table by its columns.

    ``run_query(sql)`` must execute SQL and return a DataFrame. Returns a spec
    dict ``{table, date_col, qty_col, week_col, grain}`` (grain is "daily" when a
    ``fcst_dt`` feed is available, else "weekly"), or None when no table carries
    the forecast columns. Never raises for a "not found"; only a genuine query
    failure propagates.

    ``override_table`` (from ``[bigquery].forecast_table`` in secrets, or the
    ``FORECAST_TABLE`` env var in the builder) pins the table name when set and
    valid, skipping the dataset scan.
    """
    cols_in = ", ".join(f"'{c}'" for c in _WANTED)
    where_tbl = ""
    if override_table:
        if not _SAFE_NAME.match(override_table):
            override_table = None
        else:
            where_tbl = f"AND LOWER(table_name) = LOWER('{override_table}')"
    # Keep table_name in its original case — BigQuery table identifiers are
    # case-sensitive, so the FROM clause must match exactly. Only the column
    # match is case-folded (column references are case-insensitive in BQ).
    sql = (
        "SELECT table_name AS t, LOWER(column_name) AS c\n"
        f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`\n"
        f"WHERE LOWER(column_name) IN ({cols_in})\n{where_tbl}"
    )
    df = run_query(sql)
    if df is None or df.empty:
        return None

    by_table: dict[str, set] = {}
    for t, c in zip(df["t"], df["c"]):
        by_table.setdefault(str(t), set()).add(str(c))

    daily, weekly = [], []
    for table, cols in by_table.items():
        if not _SAFE_NAME.match(table):
            continue
        if _STORE_COL not in cols or _ITEM_COL not in cols:
            continue
        qty_col = next((q for q in _QTY_COLS if q in cols), None)
        if qty_col is None:
            continue
        if _DATE_COL in cols:
            daily.append({"table": table, "date_col": _DATE_COL, "qty_col": qty_col,
                          "week_col": None, "grain": "daily"})
        elif _WEEK_COL in cols:
            weekly.append({"table": table, "date_col": None, "qty_col": qty_col,
                           "week_col": _WEEK_COL, "grain": "weekly"})

    # Daily wins: the tab is built on a daily forecast_date. A weekly-only table
    # is reported back so the caller can surface a precise message rather than a
    # blind fiscal-week→date conversion.
    return _best_table(daily) or _best_table(weekly)


def build_forecast_sql(spec: dict, project: str, dataset: str) -> str:
    """Build the parameterised daily forecast query for a discovered spec.

    Uses the same ``@active_items`` / ``@lookback_days`` parameters as the rest
    of the app, and the same Central-time anchor, so the snapshot key and the
    cache behaviour are unchanged. Only emits identifiers from the fixed
    vocabulary above plus the validated table name, so it is injection-safe.
    """
    if spec.get("grain") != "daily":
        raise ValueError(f"build_forecast_sql supports daily grain only, got {spec.get('grain')!r}")
    table = spec["table"]
    if not _SAFE_NAME.match(table):
        raise ValueError(f"unsafe forecast table name: {table!r}")
    date_col, qty_col = spec["date_col"], spec["qty_col"]
    return (
        "-- Daily store-item forecast quantity (table resolved at runtime by\n"
        "-- forecast_source.discover_forecast_spec). Joinable against store_sales\n"
        "-- for attainment.\n"
        "SELECT\n"
        f"  {date_col}   AS forecast_date,\n"
        f"  {_STORE_COL} AS store_number,\n"
        f"  {_ITEM_COL}  AS walmart_item_number,\n"
        f"  COALESCE({qty_col}, 0) AS forecast_quantity\n"
        f"FROM `{project}.{dataset}.{table}`\n"
        f"WHERE {_ITEM_COL} IN UNNEST(@active_items)\n"
        f"  AND {date_col} >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL @lookback_days DAY)\n"
    )
