-- Tender mix for FunPop — how shoppers pay (cash / card / EBT-SNAP / gift card,
-- etc.) when the item is in the basket. Units, dollars, and visits per tender
-- type. A read on shopper demographics and price sensitivity at the register.
--
-- sales_tender carries no last-year columns, so we pull two date windows in one
-- shot and tag each row TY/LY: the recent @lookback_days window, plus the SAME
-- window shifted back ~one year. That lets the app build a true year-over-year
-- comparison by tender. The LY window is bounded so it can't overlap TY; if the
-- feed has no prior-year history the LY rows simply come back empty and the app
-- hides the YoY view.
SELECT
  bus_dt                                  AS business_date,
  CASE
    WHEN bus_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
      THEN 'TY' ELSE 'LY'
  END                                     AS period,
  store_nbr                               AS store_number,
  wm_item_nbr                             AS walmart_item_number,
  tndr_group                              AS tender_group,
  tndr_type_desc                          AS tender_type,
  COALESCE(sales_by_tender, 0)            AS sales_by_tender,
  COALESCE(units_by_tender, 0)            AS units_by_tender,
  COALESCE(visits_by_tender, 0)           AS visits_by_tender
FROM `{project}.{dataset}.sales_tender`
WHERE wm_item_nbr IN UNNEST(@active_items)
  AND (
    bus_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
    OR (
      bus_dt >= DATE_SUB(DATE_SUB(CURRENT_DATE(), INTERVAL 365 DAY), INTERVAL @lookback_days DAY)
      AND bus_dt < DATE_SUB(CURRENT_DATE(), INTERVAL 365 DAY)
    )
  )
