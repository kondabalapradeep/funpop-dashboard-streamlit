-- Store → DC mapping (currently effective alignments only).
-- Used to compute actual per-DC demand instead of the previous network-proxy.
SELECT
  store_nbr                AS store_number,
  dc_nbr                   AS distribution_center_number,
  whse_algn_type_cd        AS alignment_type
FROM `{project}.{dataset}.dc_alignment`
WHERE (algn_eff_dt IS NULL OR algn_eff_dt <= CURRENT_DATE('America/Chicago'))
  AND (algn_exp_dt IS NULL OR algn_exp_dt >= CURRENT_DATE('America/Chicago'))
ORDER BY store_nbr, whse_algn_type_cd, dc_nbr
