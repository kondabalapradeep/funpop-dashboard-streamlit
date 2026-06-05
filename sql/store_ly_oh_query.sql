-- Last year's actual store inventory levels for a fixed date window, summed to a
-- daily network total. Feeds the "next 7 days" projection on the inventory tab:
-- each upcoming day is estimated from last year's comparable day (a 52-week /
-- 364-day shift, preserving weekday), so this pulls that comparable last-year
-- window and the app shifts the dates forward.
--
-- Only the inventory table is touched (no store_sales join), so there is no
-- sales-side fan-out. Repeated intraday inventory snapshots are de-duplicated to
-- one row per store-day-item before summing, exactly like store_query.sql, so
-- the daily on-hand total is not double-counted.
WITH inv AS (
  SELECT
    bus_dt, store_nbr, wm_item_nbr,
    ty_on_hand_qty, ty_in_trnst_qty, ty_in_whse_qty
  FROM `{project}.{dataset}.store_invt`
  WHERE wm_item_nbr IN UNNEST(@active_items)
    AND bus_dt BETWEEN @ly_start AND @ly_end
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY bus_dt, store_nbr, wm_item_nbr
    ORDER BY ty_on_hand_qty DESC
  ) = 1
)
SELECT
  bus_dt                AS inventory_date,
  SUM(ty_on_hand_qty)   AS in_store_ly,
  SUM(ty_in_trnst_qty)  AS in_transit_ly,
  SUM(ty_in_whse_qty)   AS in_whse_ly
FROM inv
GROUP BY bus_dt
ORDER BY bus_dt
