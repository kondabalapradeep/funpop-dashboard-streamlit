-- Same-period-last-year store inventory levels for the Inventory tab's
-- "week look-ahead". The dashboard's main store frame only spans the rolling
-- Standard lookback (~4-5 weeks; 120 days max via the slider), so last year's
-- levels for the *upcoming* week aren't in it. This pulls last year's in-store
-- on-hand ("in house") and in-warehouse for a window around the year-ago anchor
-- (CURRENT_DATE - 364 days = the same Walmart fiscal weekday one year prior),
-- which the app shifts forward 52 weeks (364 days) and combines with this year's
-- actuals: actuals up to the latest data day, then last year's levels for the
-- next 7 days as a planning proxy.
--
-- Window: from (CURRENT_DATE - 364 - @lookback_days) back-anchor up to
-- (CURRENT_DATE - 355). After the +364-day shift the app applies, that maps to
-- roughly the recent lookback through ~9 days ahead of today — enough to cover
-- the recent actuals (for a year-ago reference) plus the next 7 days, with a
-- couple of days of margin for the feed's reporting lag.
--
-- Returned at date x item x state grain so the app can apply the same item and
-- state filters as the rest of the tab. Fan-out is guarded the same way as
-- store_query.sql: store_dim is de-duplicated to one row per store before the
-- join, and store_invt is collapsed to one row per store-day-item (highest
-- on-hand) before aggregation, so a multi-row dimension can't inflate the sums.
WITH store_dim_d AS (
  SELECT
    store_nbr,
    ANY_VALUE(state_prov_cd) AS state_prov_cd
  FROM `{project}.{dataset}.store_dim`
  GROUP BY store_nbr
),
store_invt_d AS (
  SELECT
    bus_dt, store_nbr, wm_item_nbr,
    ty_on_hand_qty, ty_in_whse_qty
  FROM `{project}.{dataset}.store_invt`
  WHERE wm_item_nbr IN UNNEST(@active_items)
    AND bus_dt BETWEEN
        DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL (364 + @lookback_days) DAY)
        AND DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL 355 DAY)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY bus_dt, store_nbr, wm_item_nbr
    ORDER BY ty_on_hand_qty DESC
  ) = 1
)
SELECT
  si.bus_dt                            AS inventory_date,
  si.wm_item_nbr                       AS walmart_item_number,
  sd.state_prov_cd                     AS state_or_province_code,
  SUM(COALESCE(si.ty_on_hand_qty, 0))  AS store_on_hand_quantity,
  SUM(COALESCE(si.ty_in_whse_qty, 0))  AS store_in_warehouse_quantity
FROM store_invt_d AS si
LEFT JOIN store_dim_d AS sd
  ON si.store_nbr = sd.store_nbr
GROUP BY si.bus_dt, si.wm_item_nbr, sd.state_prov_cd
