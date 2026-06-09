-- Hourly register sales for FunPop — POS scans and dollars by store, item, and
-- hour of the business day. Reveals the dayparts (and weekday-by-hour pockets)
-- when the product actually moves, so a rep can time displays/restocks to demand.
SELECT
  bus_dt                                  AS business_date,
  bus_hr_nbr                              AS business_hour,
  store_nbr                               AS store_number,
  wm_item_nbr                             AS walmart_item_number,
  svc_chnl_nm                             AS service_channel,
  COALESCE(scan_cnt, 0)                   AS scan_count,
  COALESCE(sales_amt, 0)                  AS sales_amount
FROM `{project}.{dataset}.hourly_register_sales`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND bus_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
