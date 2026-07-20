-- Sell-Through tab: store × Walmart-week roll-up of the two bin items combined,
-- spanning Walmart week 5 of the current fiscal year through the current
-- (in-progress) week. This window is much longer than the dashboard's rolling
-- lookback, so the frame is aggregated to store-week grain here in BigQuery
-- instead of shipping ~20+ weeks of store-day rows to the app (Community Cloud
-- RAM cap). Real table names confirmed against wmt-dv-bi-link-prod.dv_supplier.
--
-- Week bounds come from the feed itself: the current week is the max
-- wm_yr_wk_nbr on recent days, and the start week is week 5 of that same fiscal
-- year (wm_yr_wk_nbr = fiscal-year prefix × 100 + week number, so the prefix
-- encoding never needs to be known). LEAST() keeps the window valid during
-- weeks 1-4 of a new fiscal year, when "week 5 of the current year" doesn't
-- exist yet (the window collapses to just the current week). The 400-day date
-- guard bounds the scan: week 5 → week 53 is at most ~49 weeks back.
--
-- Fan-out guards mirror store_query.sql:
--  * store_sales carries several rows per store-day-item (split by a sales
--    dimension the dashboard never selects); POS is additive across the split,
--    so it is summed before anything joins to it.
--  * store_invt is de-duplicated to one row per store-day-item (repeated
--    snapshots keep the highest on-hand) before the eaches are summed.

WITH bounds AS (
  SELECT
    LEAST(DIV(MAX(wm_yr_wk_nbr), 100) * 100 + 5, MAX(wm_yr_wk_nbr)) AS start_wk,
    MAX(wm_yr_wk_nbr) AS end_wk
  FROM `{project}.{dataset}.store_sales`
  WHERE wm_item_nbr IN UNNEST(@bin_items)
    AND bus_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL 14 DAY)
),
store_dim_d AS (
  SELECT store_nbr, ANY_VALUE(state_prov_cd) AS state_prov_cd
  FROM `{project}.{dataset}.store_dim`
  GROUP BY store_nbr
),
-- One sales row per store-day, bins combined (POS summed across the hidden
-- split dimension AND the two bin items — the tab never separates them).
sales_d AS (
  SELECT
    ss.bus_dt, ss.wm_yr_wk_nbr, ss.store_nbr,
    SUM(ss.ty_qty) AS ty_qty,
    SUM(ss.ly_qty) AS ly_qty
  FROM `{project}.{dataset}.store_sales` AS ss, bounds AS b
  WHERE ss.wm_item_nbr IN UNNEST(@bin_items)
    AND ss.bus_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL 400 DAY)
    AND ss.wm_yr_wk_nbr BETWEEN b.start_wk AND b.end_wk
  GROUP BY ss.bus_dt, ss.wm_yr_wk_nbr, ss.store_nbr
),
-- Calendar shape of each week in the window (shared across stores): how many
-- data days it holds and its first/last data day. week_end is Friday for
-- complete weeks and the most recent data day for the in-progress one.
weeks AS (
  SELECT
    wm_yr_wk_nbr,
    COUNT(DISTINCT bus_dt) AS days_in_week,
    MIN(bus_dt)            AS week_first_day,
    MAX(bus_dt)            AS week_end
  FROM sales_d
  GROUP BY wm_yr_wk_nbr
),
-- Ending bin inventory per store: levels on each week's last data day.
invt_d AS (
  SELECT
    bus_dt, store_nbr, wm_item_nbr,
    ty_on_hand_qty, ly_on_hand_qty, ty_in_whse_qty, ty_in_trnst_qty
  FROM `{project}.{dataset}.store_invt`
  WHERE wm_item_nbr IN UNNEST(@bin_items)
    AND bus_dt IN (SELECT week_end FROM weeks)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY bus_dt, store_nbr, wm_item_nbr
    ORDER BY ty_on_hand_qty DESC
  ) = 1
),
eow AS (
  SELECT
    w.wm_yr_wk_nbr, i.store_nbr,
    SUM(COALESCE(i.ty_on_hand_qty, 0))  AS eow_on_hand_ty,
    SUM(COALESCE(i.ly_on_hand_qty, 0))  AS eow_on_hand_ly,
    SUM(COALESCE(i.ty_in_whse_qty, 0))  AS eow_in_whse_ty,
    SUM(COALESCE(i.ty_in_trnst_qty, 0)) AS eow_in_transit_ty
  FROM invt_d AS i
  JOIN weeks AS w
    ON i.bus_dt = w.week_end
  GROUP BY w.wm_yr_wk_nbr, i.store_nbr
),
weekly AS (
  SELECT
    wm_yr_wk_nbr, store_nbr,
    SUM(ty_qty) AS units_ty,
    SUM(ly_qty) AS units_ly
  FROM sales_d
  GROUP BY wm_yr_wk_nbr, store_nbr
)
SELECT
  wy.wm_yr_wk_nbr                  AS walmart_calendar_week,
  wy.store_nbr                     AS store_number,
  sd.state_prov_cd                 AS state_or_province_code,
  w.days_in_week,
  w.week_first_day,
  w.week_end,
  COALESCE(wy.units_ty, 0)         AS units_ty,
  COALESCE(wy.units_ly, 0)         AS units_ly,
  COALESCE(e.eow_on_hand_ty, 0)    AS eow_on_hand_ty,
  COALESCE(e.eow_on_hand_ly, 0)    AS eow_on_hand_ly,
  COALESCE(e.eow_in_whse_ty, 0)    AS eow_in_whse_ty,
  COALESCE(e.eow_in_transit_ty, 0) AS eow_in_transit_ty
FROM weekly AS wy
JOIN weeks AS w
  ON wy.wm_yr_wk_nbr = w.wm_yr_wk_nbr
LEFT JOIN eow AS e
  ON  wy.wm_yr_wk_nbr = e.wm_yr_wk_nbr
  AND wy.store_nbr    = e.store_nbr
LEFT JOIN store_dim_d AS sd
  ON wy.store_nbr = sd.store_nbr
