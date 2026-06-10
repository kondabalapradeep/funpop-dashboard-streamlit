-- Omni-channel sales with channel and service-channel breakdown.
-- "Shipped based" = actual fulfilled units (vs "Auth based" = ordered).
SELECT
  bus_dt                                     AS business_date,
  wm_yr_wk_nbr                               AS walmart_calendar_week,
  wm_item_nbr                                AS walmart_item_number,
  order_chnl_nm                              AS order_channel,
  svc_chnl_nm                                AS service_channel,
  COALESCE(ty_shpd_based_qty, 0)             AS units_ty,
  COALESCE(ly_shpd_based_qty, 0)             AS units_ly,
  COALESCE(ty_shpd_based_net_sales_amt, 0)   AS sales_ty,
  COALESCE(ly_shpd_based_net_sales_amt, 0)   AS sales_ly
FROM `{project}.{dataset}.omni_sales`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND bus_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL @lookback_days DAY)
