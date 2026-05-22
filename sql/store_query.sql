-- Daily store-level POS + inventory + store metadata + item name.
-- Output columns are aliased back to the original CSV header names so that
-- funpop_core.prepare_data() runs without modification.
--
-- Joins:
--   store_sales      (daily POS, has bus_dt, wm_yr_wk_nbr, store_nbr, wm_item_nbr)
--   store_inventory  (daily on-hand/in-warehouse/in-transit + store-specific retail)
--   store_dimensions (store name, city, state — small lookup, no date)
--   item_dimensions  (item_name — small lookup)
--
-- Replace {project}, {dataset}, and table names as needed for your environment.
-- ACTIVE_ITEMS = (658442130, 666209064, 658442128)

SELECT
  ss.bus_dt                                 AS business_date,
  ss.wm_yr_wk_nbr                           AS walmart_calendar_week,
  ss.store_nbr                              AS store_number,
  sd.store_nm                               AS store_name,
  sd.city_nm                                AS city_name,
  sd.state_prov_cd                          AS state_or_province_code,
  ss.wm_item_nbr                            AS walmart_item_number,
  COALESCE(idim.item_name, '')              AS item_name,
  COALESCE(ss.ty_qty, 0)                    AS pos_quantity_this_year,
  COALESCE(ss.ly_qty, 0)                    AS pos_quantity_last_year,
  COALESCE(si.ty_on_hand_qty, 0)            AS store_on_hand_quantity_this_year,
  COALESCE(si.ly_on_hand_qty, 0)            AS store_on_hand_quantity_last_year,
  COALESCE(si.ty_in_whse_qty, 0)            AS store_in_warehouse_quantity_this_year,
  COALESCE(si.ty_in_trnst_qty, 0)           AS store_in_transit_quantity_this_year,
  COALESCE(si.ty_store_specific_rtl_amt, 0) AS store_specific_retail_amount_this_year,
  COALESCE(ss.ty_sales_amt, 0)              AS pos_sales_this_year,
  COALESCE(ss.ly_sales_amt, 0)              AS pos_sales_last_year
FROM `{project}.{dataset}.store_sales` AS ss
LEFT JOIN `{project}.{dataset}.store_inventory` AS si
  ON  ss.bus_dt     = si.bus_dt
  AND ss.store_nbr  = si.store_nbr
  AND ss.wm_item_nbr = si.wm_item_nbr
LEFT JOIN `{project}.{dataset}.store_dimensions` AS sd
  ON ss.store_nbr = sd.store_nbr
LEFT JOIN `{project}.{dataset}.item_dimensions` AS idim
  ON ss.wm_item_nbr = idim.wm_item_nbr
WHERE
  ss.wm_item_nbr IN UNNEST(@active_items)
  AND ss.bus_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
