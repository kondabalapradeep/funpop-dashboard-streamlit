-- Which stores have this item in their modular plan (currently valid only).
-- Gives us the "should-be-stocked" universe to compare against actual sellers.
SELECT DISTINCT
  store_nbr                              AS store_number,
  wm_item_nbr                            AS walmart_item_number,
  store_item_valid_ind                   AS item_valid_flag
FROM `{project}.{dataset}.modular_plan_upc`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND (store_actual_effective_date IS NULL OR store_actual_effective_date <= CURRENT_DATE())
  AND (store_actual_expiry_date IS NULL OR store_actual_expiry_date >= CURRENT_DATE())
