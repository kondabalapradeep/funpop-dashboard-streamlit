-- Daily DC inventory from dc_item + dc_dim for the DC name.
-- Real table names confirmed against wmt-dv-bi-link-prod.dv_supplier.
--
-- NOTE on unit conversion:
--   ty_on_hand_whpk_qty = warehouse PACKS (not eaches).
--   If dashboard numbers look ~6x or ~208x too small, multiply by ty_whpk_each_qty
--   to convert packs to eaches.

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
  COALESCE(dim.ly_outs_each_qty, 0)          AS out_of_stock_each_quantity_last_year
FROM `{project}.{dataset}.dc_item` AS dim
LEFT JOIN `{project}.{dataset}.dc_dim` AS dcd
  ON dim.dc_nbr = dcd.dc_nbr
LEFT JOIN `{project}.{dataset}.item_dim` AS idim
  ON dim.wm_item_nbr = idim.wm_item_nbr
WHERE
  dim.wm_item_nbr IN UNNEST(@active_items)
  AND dim.invt_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
