"""Pure dtype-normalisation transforms shared by the live app (streamlit_app.py)
and the snapshot builder (snapshot_build.py).

Why this exists
---------------
BigQuery → pandas hands NUMERIC columns back as boxed Python ``decimal.Decimal``
objects, integer columns as nullable ``Int64``, and DATE columns as ``dbdate``.
Left untouched, the dashboard's heaviest frame (~544k store-day-item rows)
*reads back from its parquet snapshot at ~375 MB in memory* and costs ~4 s of
``pd.to_numeric`` work to collapse — paid on every cold start, and a repeat
cause of the app's Community Cloud RAM-limit trips.

The loaders already shrink these to native ``int32`` / ``float32`` / ``category``
/ ``datetime64`` dtypes. By moving that work here and applying it in the builder
*before* the parquet is committed, a cold start reads a small (~31 MB) frame
that's already in final form, dropping store-frame prep from ~4,100 ms to
~95 ms. The app still re-applies the transform after every read, so live pulls
and any older (un-shrunk) snapshot keep working unchanged.

Every function here is **idempotent**: re-running it on an already-shrunk frame
is a cheap no-op that returns identical data (verified for the store frame's
unit-price correction, which never re-triggers once retail amounts are unit
prices). That idempotence is what lets the builder and the app both apply it.

Import-safe: no ``streamlit`` import, so the builder can share it.
"""
import numpy as np
import pandas as pd


def shrink_frame(df: pd.DataFrame, int_cols=(), float_cols=(), cat_cols=()) -> pd.DataFrame:
    """Shrink a frame's dtypes in place: counts → int32 (pandas still sums them
    in an int64 accumulator, so no overflow), money/rates → float32, and repeated
    codes/strings → category. Idempotent — re-running on shrunk columns is a
    no-op."""
    for c in int_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).round().astype("int32")
    for c in float_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("float32")
    for c in cat_cols:
        if c in df.columns:
            df[c] = df[c].astype("category")
    return df


def process_store_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise + shrink the heavy store frame (store_query.sql).

    Drops columns the dashboard never reads (defensive — the SQL already omits
    them), categoricalises the low-cardinality state code, applies the unit-price
    correction, and downcasts counts → int32 / money → float32. This is the
    single most expensive read in the app; see the module docstring."""
    if df.empty:
        return df
    df = df.drop(columns=["store_name", "city_name", "item_name"], errors="ignore")
    if "state_or_province_code" in df.columns:
        df["state_or_province_code"] = df["state_or_province_code"].astype("category")
    df["business_date"] = pd.to_datetime(df["business_date"])
    int_cols = [
        "pos_quantity_this_year", "pos_quantity_last_year",
        "store_on_hand_quantity_this_year", "store_on_hand_quantity_last_year",
        "store_in_warehouse_quantity_this_year", "store_in_transit_quantity_this_year",
    ]
    money_cols = [
        "store_specific_retail_amount_this_year",
        "pos_sales_this_year", "pos_sales_last_year",
    ]
    for c in int_cols + money_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    # Collapse the sales-side fan-out before anything sums these columns.
    # store_sales carries several rows per store-day-item (split by a sales
    # dimension the dashboard never selects); the LEFT JOIN to the
    # one-row-per-key inventory feed copies each inventory level onto every one
    # of those rows. POS is additive across the split and must be summed, but the
    # inventory levels (on-hand / in-warehouse / in-transit / retail) are
    # identical copies — summing them reads store on-hand ~2x high. Collapse to
    # one row per store-day-item: sum POS, take each level column once.
    # Idempotent: an already-collapsed frame has no duplicate keys, so the guard
    # skips the groupby and returns the data unchanged.
    key = ["business_date", "store_number", "walmart_item_number"]
    if df.duplicated(subset=key).any():
        cols = list(df.columns)
        sum_cols = ["pos_quantity_this_year", "pos_quantity_last_year",
                    "pos_sales_this_year", "pos_sales_last_year"]
        agg = {c: ("sum" if c in sum_cols else "first")
               for c in cols if c not in key}
        df = df.groupby(key, as_index=False, sort=False).agg(agg)[cols]
    # Unit-price correction (float division — must run before we shrink dtypes).
    # Idempotent: once applied, retail amounts are unit prices (~$3), so the
    # median > 10 guard never re-triggers on a second pass.
    sold = df[df["pos_quantity_this_year"] > 0]
    if len(sold) and sold["store_specific_retail_amount_this_year"].median() > 10:
        unit_price = np.where(
            df["pos_quantity_this_year"] > 0,
            (df["pos_sales_this_year"] / df["pos_quantity_this_year"].replace(0, np.nan)).round(2),
            0,
        )
        df["store_specific_retail_amount_this_year"] = np.where(np.isnan(unit_price), 0, unit_price)
    for c in int_cols:
        df[c] = df[c].round().astype("int32")
    for c in money_cols:
        df[c] = df[c].astype("float32")
    for c in ["walmart_item_number", "store_number", "walmart_calendar_week"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int32")
    return df


def process_forecast_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise + shrink the daily demand-forecast frame (forecast_query.sql).
    forecast_quantity arrives as boxed Decimals over ~273k rows, so the same
    pre-shrink win applies here (~720 ms → ~50 ms on cold start)."""
    if df.empty:
        return df
    df["forecast_date"] = pd.to_datetime(df["forecast_date"])
    shrink_frame(df, int_cols=["store_number", "walmart_item_number"],
                 float_cols=["forecast_quantity"])
    return df


# Builder hook: snapshot_build.py applies these to the matching query result
# before committing its parquet, so the deployed app reads an already-shrunk
# frame on cold start. Only the frames whose raw read is expensive are listed
# (the store frame inflates to ~375 MB / ~4 s; forecast to ~38 MB / ~0.7 s). The
# rest read small and their loaders shrink them cheaply, so pre-shrinking them
# would add churn for no measurable gain. NOTE: do not add dc_query.sql here —
# its loader's warehouse-pack → eaches conversion is not idempotent (it drops the
# pack-size column it depends on), so it must run exactly once, in the app.
SNAPSHOT_TRANSFORMS = {
    "store_query.sql": process_store_frame,
    "forecast_query.sql": process_forecast_frame,
}
