-- Market-basket affinity: what else customers put in the cart on the same trip
-- as FunPop. One row per primary item × affinity (partner) item × store × week.
-- Bounded to the most recent ~13 weeks present so the pull stays small. Partner
-- item names come from item_dim. Self-pairs (partner == primary) are excluded so
-- the list is genuine cross-sell rather than the item with itself.
WITH item_dim_d AS (
  SELECT wm_item_nbr, ANY_VALUE(item_name) AS item_name
  FROM `{project}.{dataset}.item_dim`
  GROUP BY wm_item_nbr
),
recent AS (
  SELECT MAX(wm_yr_wk_nbr) AS max_wk
  FROM `{project}.{dataset}.item_affinity`
  WHERE wm_item_nbr IN UNNEST(@active_items)
)
SELECT
  ia.wm_yr_wk_nbr                                AS walmart_calendar_week,
  ia.wm_item_nbr                                 AS walmart_item_number,
  ia.affinity_wm_item_nbr                        AS affinity_item_number,
  COALESCE(idd.item_name, CAST(ia.affinity_wm_item_nbr AS STRING)) AS affinity_item_name,
  COALESCE(ia.affinity_basket_cnt, 0)            AS affinity_basket_count,
  COALESCE(ia.tot_basket_cnt, 0)                 AS total_basket_count,
  COALESCE(ia.affinity_item_avg_rtl_amt, 0)      AS affinity_item_avg_retail,
  COALESCE(ia.primary_item_avg_rtl_amt, 0)       AS primary_item_avg_retail,
  COALESCE(ia.avg_basket_affinity_item_cnt, 0)   AS avg_basket_affinity_item_count,
  COALESCE(ia.avg_basket_affinity_rtl_amt, 0)    AS avg_basket_affinity_retail,
  COALESCE(ia.avg_basket_uom_qty, 0)             AS avg_basket_units
FROM `{project}.{dataset}.item_affinity` AS ia
CROSS JOIN recent
LEFT JOIN item_dim_d AS idd
  ON ia.affinity_wm_item_nbr = idd.wm_item_nbr
WHERE ia.wm_item_nbr IN UNNEST(@active_items)
  AND ia.affinity_wm_item_nbr <> ia.wm_item_nbr
  AND ia.wm_yr_wk_nbr >= recent.max_wk - 12
