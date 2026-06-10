-- Daily store-level POS + inventory + store state.
-- Real table names confirmed against wmt-dv-bi-link-prod.dv_supplier.
-- ACTIVE_ITEMS = (658442130, 666209064, 658442128)
--
-- Fan-out is guarded on both sides of the join:
--  * Dimension/inventory tables are de-duplicated to one row per join key
--    BEFORE joining, so a multi-row dimension (e.g. a slowly-changing store_dim
--    with several effective-dated rows per store) cannot fan out the sales rows.
--  * store_sales itself carries several rows per store-day-item (split by a
--    sales dimension the dashboard never selects), so it is pre-aggregated to
--    one row per key BEFORE the inventory join. Otherwise the join copies each
--    inventory level (on-hand / in-warehouse / in-transit) across every sales
--    row and a downstream SUM reads store on-hand ~2x high.
--
-- Only the columns the dashboard actually consumes are selected. store_name,
-- city_name and item_name were dropped: the app never reads them and, repeated
-- across ~550k rows, they were ~120 MB of dead weight in the cached frame (and
-- in the committed snapshot parquet) — a contributor to the Community Cloud
-- memory-limit trips.

WITH store_dim_d AS (
  SELECT
    store_nbr,
    ANY_VALUE(state_prov_cd)       AS state_prov_cd,
    ANY_VALUE(mdse_maj_zone_nbr)   AS mdse_maj_zone_nbr
  FROM `{project}.{dataset}.store_dim`
  GROUP BY store_nbr
),
store_sales_d AS (
  -- Collapse the sales-side fan-out to one row per store-day-item before the
  -- inventory join. POS measures are additive across the split, so they are
  -- summed here; this is what keeps the inventory levels from being repeated
  -- (and double-counted) across the duplicate sales rows.
  SELECT
    bus_dt, wm_yr_wk_nbr, store_nbr, wm_item_nbr,
    SUM(ty_qty)       AS ty_qty,
    SUM(ly_qty)       AS ly_qty,
    SUM(ty_sales_amt) AS ty_sales_amt,
    SUM(ly_sales_amt) AS ly_sales_amt
  FROM `{project}.{dataset}.store_sales`
  WHERE wm_item_nbr IN UNNEST(@active_items)
    AND bus_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL @lookback_days DAY)
  GROUP BY bus_dt, wm_yr_wk_nbr, store_nbr, wm_item_nbr
),
store_invt_d AS (
  -- One inventory row per store-day-item. Assumes duplicate keys are repeated
  -- snapshots and keeps the highest on-hand; verify the table grain if unsure.
  SELECT
    bus_dt, store_nbr, wm_item_nbr,
    ty_on_hand_qty, ly_on_hand_qty, ty_in_whse_qty,
    ty_in_trnst_qty, ty_store_specific_rtl_amt
  FROM `{project}.{dataset}.store_invt`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY bus_dt, store_nbr, wm_item_nbr
    ORDER BY ty_on_hand_qty DESC
  ) = 1
)
SELECT
  ss.bus_dt                                 AS business_date,
  ss.wm_yr_wk_nbr                           AS walmart_calendar_week,
  ss.store_nbr                              AS store_number,
  sd.state_prov_cd                          AS state_or_province_code,
  sd.mdse_maj_zone_nbr                      AS mdse_major_zone_number,
  ss.wm_item_nbr                            AS walmart_item_number,
  COALESCE(ss.ty_qty, 0)                    AS pos_quantity_this_year,
  COALESCE(ss.ly_qty, 0)                    AS pos_quantity_last_year,
  COALESCE(si.ty_on_hand_qty, 0)            AS store_on_hand_quantity_this_year,
  COALESCE(si.ly_on_hand_qty, 0)            AS store_on_hand_quantity_last_year,
  COALESCE(si.ty_in_whse_qty, 0)            AS store_in_warehouse_quantity_this_year,
  COALESCE(si.ty_in_trnst_qty, 0)           AS store_in_transit_quantity_this_year,
  COALESCE(si.ty_store_specific_rtl_amt, 0) AS store_specific_retail_amount_this_year,
  COALESCE(ss.ty_sales_amt, 0)              AS pos_sales_this_year,
  COALESCE(ss.ly_sales_amt, 0)              AS pos_sales_last_year
FROM store_sales_d AS ss
LEFT JOIN store_invt_d AS si
  ON  ss.bus_dt     = si.bus_dt
  AND ss.store_nbr  = si.store_nbr
  AND ss.wm_item_nbr = si.wm_item_nbr
LEFT JOIN store_dim_d AS sd
  ON ss.store_nbr = sd.store_nbr
-- Item/date filter now lives in store_sales_d (applied before aggregation).
