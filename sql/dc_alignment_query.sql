-- Store → DC mapping (currently effective alignments only).
-- Used to compute actual per-DC demand instead of the previous network-proxy.
SELECT
  store_nbr                AS store_number,
  dc_nbr                   AS distribution_center_number,
  whse_align_type_cd       AS alignment_type
FROM `{project}.{dataset}.dc_alignment`
WHERE (algn_eff_dt IS NULL OR algn_eff_dt <= CURRENT_DATE())
  AND (algn_exp_dt IS NULL OR algn_exp_dt >= CURRENT_DATE())
ORDER BY store_nbr, whse_align_type_cd, dc_nbr
