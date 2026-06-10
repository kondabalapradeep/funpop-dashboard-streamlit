-- eComm on-hand and available-to-sell at the ship-node level.
-- ecom_invt is keyed by catlg_item_id; we bridge to wm_item_nbr via omni_item_dimensions.
-- Collapse to one wm_item_nbr per catlg_item_id so a catalog id mapped to multiple
-- item numbers can't fan out (double-count) the ecom_invt rows.
WITH catalog_items AS (
  SELECT catlg_item_id, ANY_VALUE(wm_item_nbr) AS wm_item_nbr
  FROM `{project}.{dataset}.omni_item_dimensions`
  WHERE wm_item_nbr IN UNNEST(@active_items)
  GROUP BY catlg_item_id
)
SELECT
  ei.rpt_dt                                  AS report_date,
  ei.ship_node_org_cd                        AS ship_node,
  ci.wm_item_nbr                             AS walmart_item_number,
  COALESCE(ei.ty_on_hand_unit_qty, 0)        AS on_hand_units,
  COALESCE(ei.ty_ats_qty, 0)                 AS available_to_sell,
  COALESCE(ei.ly_on_hand_unit_qty, 0)        AS on_hand_units_ly
FROM `{project}.{dataset}.ecom_invt` AS ei
JOIN catalog_items AS ci
  ON ei.catlg_item_id = ci.catlg_item_id
WHERE ei.rpt_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL @lookback_days DAY)
