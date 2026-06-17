-- Same-period-last-year DC on-hand for the Inventory tab's week look-ahead.
-- Mirrors the windowing of store_lookahead_query.sql: last year's distribution-
-- center on-hand for a window around the year-ago anchor (CURRENT_DATE - 364
-- days), which the app shifts forward 52 weeks (364 days) and combines with this
-- year's actuals so the look-ahead's "DC on-hand" / "Total network" columns
-- extend into the next 7 days. The main DC frame only spans the recent lookback,
-- so last year's upcoming-week levels aren't in it.
--
-- Unit conversion mirrors dc_query.sql: ty_on_hand_whpk_qty is warehouse PACKS,
-- and ty_whpk_each_qty (eaches per pack) is exposed RAW — NULL or 0 when the feed
-- lacks a pack size — so the app converts packs -> eaches with the real per-item
-- value and falls back to constants.CASE_PACK_UNITS only where the feed has none.
-- No dimension joins (DC name / state aren't needed for the daily network total),
-- so there is no fan-out to guard here; dc_item is one row per dc-item-date.
SELECT
  invt_dt                              AS inventory_date,
  wm_item_nbr                          AS walmart_item_number,
  COALESCE(ty_on_hand_whpk_qty, 0)     AS on_hand_warehouse_inventory_in_units_this_year,
  ty_whpk_each_qty                     AS warehouse_pack_each_quantity
FROM `{project}.{dataset}.dc_item`
WHERE
  wm_item_nbr IN UNNEST(@active_items)
  AND invt_dt BETWEEN
      DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL (364 + @lookback_days) DAY)
      AND DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL 355 DAY)
