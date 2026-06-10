-- Daily returns by store with TY/LY comparison.
SELECT
  rtn_dt                                  AS return_date,
  store_nbr                               AS store_number,
  wm_item_nbr                             AS walmart_item_number,
  rtn_rsn_cd                              AS return_reason,
  COALESCE(ty_rtn_qty, 0)                 AS returns_ty,
  COALESCE(ly_rtn_qty, 0)                 AS returns_ly,
  COALESCE(ty_rtn_sales_amt, 0)           AS return_sales_ty,
  COALESCE(ly_rtn_sales_amt, 0)           AS return_sales_ly
FROM `{project}.{dataset}.store_returns`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND rtn_dt >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL @lookback_days DAY)
