-- Inventory adjustments by store-day. Large NEGATIVE adjustments suggest
-- phantom inventory: system claimed stock that wasn't really there.
SELECT
  invt_adj_dt                                  AS adjustment_date,
  store_nbr                                    AS store_number,
  wm_item_nbr                                  AS walmart_item_number,
  invt_adj_type_cd                             AS adjustment_type,
  invt_adj_type_desc                           AS adjustment_description,
  COALESCE(ty_invt_adj_each_qty, 0)            AS adjustment_qty_ty,
  COALESCE(ly_invt_adj_each_qty, 0)            AS adjustment_qty_ly
FROM `{project}.{dataset}.backroom_adjusted_inventory`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND invt_adj_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
