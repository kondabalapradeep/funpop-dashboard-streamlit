-- Daily store-item demand forecast (powers the Forecast tab), joinable against
-- store_sales for attainment.
--
-- NOTE: the physical table name is NOT hard-coded here. It varies per BI Link
-- export, and an earlier hard-coded `daily_demand_forecast` matched no real
-- table — which made the whole Forecast tab error with "Forecast data is
-- temporarily unavailable". The app and the snapshot builder now resolve the
-- real table at runtime from INFORMATION_SCHEMA (see forecast_source.py) using
-- the BI Link forecast columns below, then build this exact shape. Set
-- `[bigquery].forecast_table` in secrets (or the FORECAST_TABLE env var for the
-- builder) to pin a specific table and skip discovery.
--
-- This file is the canonical reference for the expected shape; it is kept in
-- sync with forecast_source.build_forecast_sql.
SELECT
  fcst_dt                    AS forecast_date,            -- Forecast Date (daily)
  store_nbr                  AS store_number,             -- Store Number
  wm_item_nbr                AS walmart_item_number,      -- Walmart Item Number
  COALESCE(fcst_tot_dmand_each_qty, 0) AS forecast_quantity  -- Forecast Total Demand Each Qty
FROM `{project}.{dataset}.<resolved at runtime>`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND fcst_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL @lookback_days DAY)
