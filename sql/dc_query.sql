-- Daily DC inventory from dc_item + dc_dim for the DC name.
-- Real table names confirmed against wmt-dv-bi-link-prod.dv_supplier.
--
-- Unit conversion: ty_on_hand_whpk_qty / ty_order_whpk_qty are warehouse PACKS,
-- not eaches. We expose ty_whpk_each_qty (eaches per pack) as a column so the app
-- converts packs→eaches using the real per-item value instead of a hardcoded
-- constant. NULLIF(...,0)→1 keeps inventory intact when the feed lacks a pack size.
--
-- dc_dim and item_dim are de-duplicated to one row per key first, so a multi-row
-- dimension can't fan out dc_item rows and inflate on-hand/on-order.

WITH dc_dim_d AS (
  SELECT dc_nbr, ANY_VALUE(dc_nm) AS dc_nm
  FROM `{project}.{dataset}.dc_dim`
  GROUP BY dc_nbr
),
item_dim_d AS (
  SELECT wm_item_nbr, ANY_VALUE(item_name) AS item_name
  FROM `{project}.{dataset}.item_dim`
  GROUP BY wm_item_nbr
)
SELECT
  dim.invt_dt                                AS inventory_date,
  dim.dc_nbr                                 AS distribution_center_number,
  COALESCE(dcd.dc_nm, CAST(dim.dc_nbr AS STRING)) AS name_of_the_dc,
  dim.wm_item_nbr                            AS walmart_item_number,
  COALESCE(idim.item_name, '')               AS item_name,
  COALESCE(dim.ty_on_hand_whpk_qty, 0)       AS on_hand_warehouse_inventory_in_units_this_year,
  COALESCE(dim.ly_on_hand_whpk_qty, 0)       AS on_hand_warehouse_inventory_in_units_last_year,
  COALESCE(dim.ty_order_whpk_qty, 0)         AS on_order_warehouse_quantity_in_units_this_year,
  COALESCE(dim.ly_order_whpk_qty, 0)         AS on_order_warehouse_quantity_in_units_last_year,
  COALESCE(dim.ty_outs_each_qty, 0)          AS out_of_stock_each_quantity_this_year,
  COALESCE(dim.ly_outs_each_qty, 0)          AS out_of_stock_each_quantity_last_year,
  COALESCE(NULLIF(dim.ty_whpk_each_qty, 0), 1) AS warehouse_pack_each_quantity
FROM `{project}.{dataset}.dc_item` AS dim
LEFT JOIN dc_dim_d AS dcd
  ON dim.dc_nbr = dcd.dc_nbr
LEFT JOIN item_dim_d AS idim
  ON dim.wm_item_nbr = idim.wm_item_nbr
WHERE
  dim.wm_item_nbr IN UNNEST(@active_items)
  AND dim.invt_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
