-- Markdown / markup (MUMD) events on FunPop — where stores are cutting (or
-- raising) the shelf price, the dollars given up, and the units affected. Margin
-- leakage a rep can challenge: unsanctioned markdowns erode the price point.
SELECT
  mumd_dt                                 AS markdown_date,
  store_nbr                               AS store_number,
  wm_item_nbr                             AS walmart_item_number,
  mumd_type_desc                          AS markdown_type,
  mumd_event_desc                         AS markdown_event,
  COALESCE(ty_mumd_amt, 0)                AS markdown_amount,
  COALESCE(ty_item_qty, 0)                AS markdown_units,
  COALESCE(ty_prev_rtl_amt, 0)            AS previous_retail,
  COALESCE(ty_curr_rtl_amt, 0)            AS current_retail
FROM `{project}.{dataset}.mumd`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND mumd_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
