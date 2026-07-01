"""Build the static store -> merchandising-zone + geo lookup used by the
Distribution tab's zone map.

Merchandise zone assignment and a store's lat/long are reference data that
essentially never change, so — like store_directory.py — the snapshot job
builds this once and commits it; the deployed app reads the committed parquet
straight from disk instead of querying store_dim on every load.
``store_nbr``, ``state_prov_cd``, ``mdse_maj_zone_nbr``, ``mdse_sub_zone_nbr``,
``lat_dgr`` and ``long_dgr`` are confirmed BI Link columns on store_dim
(store_query.sql already joins state_prov_cd/mdse_maj_zone_nbr from the same
table), so — unlike store_directory.py's address fields — no column-name
discovery is needed here.
"""
from pathlib import Path

import pandas as pd

MAP_FILENAME = "store_zone_map"          # parquet key (no extension)
MAP_PATH = Path(__file__).resolve().parent / "snapshot_data" / f"{MAP_FILENAME}.parquet"


def clean_zone_map_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise dtypes and drop stores missing a zone or a location fix."""
    if df.empty:
        return df
    df = df.copy()
    df["store_number"] = pd.to_numeric(df["store_number"], errors="coerce").fillna(0).astype("int32")
    df["state_or_province_code"] = df["state_or_province_code"].astype("string").str.strip()
    df["mdse_major_zone_number"] = pd.to_numeric(df["mdse_major_zone_number"], errors="coerce").astype("Int32")
    df["mdse_sub_zone_number"] = pd.to_numeric(df["mdse_sub_zone_number"], errors="coerce").astype("Int32")
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    return df.dropna(subset=["mdse_major_zone_number", "latitude", "longitude"])


def build_zone_map_df(run_query, project: str, dataset: str) -> pd.DataFrame:
    """One row per store: zone assignment + lat/long. ``run_query`` is a
    callable taking a SQL string and returning a DataFrame, so the app
    (Streamlit BQ client) and the builder (raw BQ client) can share this."""
    sql = f"""
        SELECT
          store_nbr                    AS store_number,
          ANY_VALUE(state_prov_cd)     AS state_or_province_code,
          ANY_VALUE(mdse_maj_zone_nbr) AS mdse_major_zone_number,
          ANY_VALUE(mdse_sub_zone_nbr) AS mdse_sub_zone_number,
          ANY_VALUE(lat_dgr)           AS latitude,
          ANY_VALUE(long_dgr)          AS longitude
        FROM `{project}.{dataset}.store_dim`
        GROUP BY store_nbr
    """
    return clean_zone_map_df(run_query(sql))
