-- Out-of-stock root cause for FunPop — the reason hierarchy (high / mid / low)
-- behind each store-day the item is unavailable, plus the fractional in-stock
-- percent. Turns "we're OOS" into a specific, fixable cause a rep can escalate.
SELECT
  bus_dt                                  AS business_date,
  store_nbr                               AS store_number,
  wm_item_nbr                             AS walmart_item_number,
  hi_lvl_oos_rsn_desc                     AS oos_reason_high,
  med_lvl_oos_rsn_desc                    AS oos_reason_mid,
  low_lvl_oos_rsn_desc                    AS oos_reason_low,
  COALESCE(frac_instk_pct, 0)             AS fractional_instock_pct,
  repl_ind                                AS replenishable_flag
FROM `{project}.{dataset}.out_of_stock_root_cause`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND bus_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
