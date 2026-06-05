-- Last year's actual DC on-hand for a fixed date window: one row per
-- (date, DC, item) with the raw warehouse-pack on-hand and the per-row
-- eaches-per-pack, mirroring dc_query.sql. The app converts packs -> eaches the
-- same way load_dc_data does, then sums by date. Feeds the inventory-tab
-- "next 7 days" projection: this is the comparable last-year window (52-week
-- shift); the app shifts the dates forward.
--
-- dc_item is one row per (invt_dt, dc, item); the app sums across DCs per date,
-- so no de-duplication is required here. NULLIF(...,0)->1 keeps inventory intact
-- when the feed lacks a pack size, exactly like dc_query.sql.
SELECT
  invt_dt                                      AS inventory_date,
  wm_item_nbr                                  AS walmart_item_number,
  COALESCE(ty_on_hand_whpk_qty, 0)             AS on_hand_whpk_qty,
  COALESCE(NULLIF(ty_whpk_each_qty, 0), 1)     AS warehouse_pack_each_quantity
FROM `{project}.{dataset}.dc_item`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND invt_dt BETWEEN @ly_start AND @ly_end
