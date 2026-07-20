-- Sell-Through tab: daily DC position for the bin items over the same window as
-- sellthrough_store_query.sql (Walmart week 5 of the current fiscal year through
-- today). dc_item carries no fiscal-week column, so the start date is derived
-- from store_sales — the first data day of the start week.
--
-- Unit conversion: ty_on_hand_whpk_qty / ty_order_whpk_qty are warehouse PACKS.
-- ty_whpk_each_qty is exposed RAW — NULL or 0 when the feed lacks a pack size —
-- so the app converts packs → eaches per row and falls back to
-- constants.CASE_PACK_UNITS only where the feed has none (same contract as
-- dc_query.sql; see the warning there about COALESCE defeating the fallback).

WITH bounds AS (
  SELECT
    LEAST(DIV(MAX(wm_yr_wk_nbr), 100) * 100 + 5, MAX(wm_yr_wk_nbr)) AS start_wk
  FROM `{project}.{dataset}.store_sales`
  WHERE wm_item_nbr IN UNNEST(@bin_items)
    AND bus_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL 14 DAY)
),
start_dt AS (
  SELECT MIN(ss.bus_dt) AS first_day
  FROM `{project}.{dataset}.store_sales` AS ss, bounds AS b
  WHERE ss.wm_item_nbr IN UNNEST(@bin_items)
    AND ss.bus_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL 400 DAY)
    AND ss.wm_yr_wk_nbr >= b.start_wk
)
SELECT
  dim.invt_dt                          AS inventory_date,
  dim.dc_nbr                           AS distribution_center_number,
  dim.wm_item_nbr                      AS walmart_item_number,
  COALESCE(dim.ty_on_hand_whpk_qty, 0) AS on_hand_warehouse_inventory_in_units_this_year,
  COALESCE(dim.ty_order_whpk_qty, 0)   AS on_order_warehouse_quantity_in_units_this_year,
  dim.ty_whpk_each_qty                 AS warehouse_pack_each_quantity
FROM `{project}.{dataset}.dc_item` AS dim
WHERE dim.wm_item_nbr IN UNNEST(@bin_items)
  AND dim.invt_dt >= (SELECT first_day FROM start_dt)
