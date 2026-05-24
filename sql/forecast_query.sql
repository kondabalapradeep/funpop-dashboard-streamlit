-- Daily store-item forecast quantity, joinable against store_sales for attainment.
SELECT
  fcst_dt                    AS forecast_date,
  store_nbr                  AS store_number,
  wm_item_nbr                AS walmart_item_number,
  COALESCE(fcst_tot_dmand_each_qty, 0) AS forecast_quantity
FROM `{project}.{dataset}.daily_demand_forecast`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND fcst_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
