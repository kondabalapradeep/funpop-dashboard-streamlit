"""Build the static store -> merchandising-zone + geo lookup used by the
Distribution tab's zone map.

Merchandise zone assignment and a store's lat/long are reference data that
essentially never change, so — like store_directory.py — the snapshot job
builds this once and commits it; the deployed app reads the committed parquet
straight from disk instead of querying store_dim on every load.

``store_nbr``/``state_prov_cd``/``mdse_maj_zone_nbr`` are confirmed BI Link
columns on store_dim (store_query.sql already joins them from the same
table). ``mdse_sub_zone_nbr``/``lat_dgr``/``long_dgr`` are the glossary's BI
Link names but were never exercised against this deployment's actual schema,
so — like store_directory.py's address fields — those three are resolved via
INFORMATION_SCHEMA rather than hard-coded, so a naming difference degrades to
a missing (optional) column instead of throwing.
"""
from pathlib import Path

import pandas as pd

MAP_FILENAME = "store_zone_map"          # parquet key (no extension)
MAP_PATH = Path(__file__).resolve().parent / "snapshot_data" / f"{MAP_FILENAME}.parquet"

# Candidate substrings per optional field, most specific first — see
# store_directory.best_col for the matching rule.
_CANDIDATES = {
    "mdse_sub_zone_number": ["mdse_sub_zone_nbr", "sub_zone_nbr", "sub_zone"],
    "latitude": ["lat_dgr", "latitude"],
    "longitude": ["long_dgr", "longitude", "lon_dgr", "lng_dgr"],
}


def best_col(columns_lower: dict, candidates: list) -> str | None:
    """Return the actual column whose lowercased name first contains one of the
    `candidates` substrings, tried in priority order (most specific first)."""
    for sub in candidates:
        for low, actual in columns_lower.items():
            if sub in low:
                return actual
    return None


def clean_zone_map_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise dtypes and drop stores missing a zone or a location fix."""
    if df.empty:
        return df
    df = df.copy()
    df["store_number"] = pd.to_numeric(df["store_number"], errors="coerce").fillna(0).astype("int32")
    df["state_or_province_code"] = df["state_or_province_code"].astype("string").str.strip()
    df["mdse_major_zone_number"] = pd.to_numeric(df["mdse_major_zone_number"], errors="coerce").astype("Int32")
    if "mdse_sub_zone_number" in df.columns:
        df["mdse_sub_zone_number"] = pd.to_numeric(df["mdse_sub_zone_number"], errors="coerce").astype("Int32")
    for c in ["latitude", "longitude"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=[c for c in ["mdse_major_zone_number", "latitude", "longitude"]
                              if c in df.columns])


def build_zone_map_df(run_query, project: str, dataset: str) -> pd.DataFrame:
    """One row per store: zone assignment + lat/long. ``run_query`` is a
    callable taking a SQL string and returning a DataFrame, so the app
    (Streamlit BQ client) and the builder (raw BQ client) can share this.
    Raises if store_dim/lat/long aren't resolvable — the loader's caller
    treats that as a section-local error, not a page crash."""
    schema = run_query(
        f"SELECT column_name FROM `{project}.{dataset}`.INFORMATION_SCHEMA.COLUMNS "
        "WHERE LOWER(table_name) = 'store_dim'"
    )
    if schema.empty:
        raise ValueError("store_dim schema not found")
    cols = {str(c).lower(): c for c in schema["column_name"].tolist()}

    lat_col = best_col(cols, _CANDIDATES["latitude"])
    lon_col = best_col(cols, _CANDIDATES["longitude"])
    if not lat_col or not lon_col:
        raise ValueError("no latitude/longitude column found on store_dim")
    sub_col = best_col(cols, _CANDIDATES["mdse_sub_zone_number"])

    selects = [
        "  store_nbr                    AS store_number",
        "  ANY_VALUE(state_prov_cd)     AS state_or_province_code",
        "  ANY_VALUE(mdse_maj_zone_nbr) AS mdse_major_zone_number",
        f"  ANY_VALUE({lat_col})         AS latitude",
        f"  ANY_VALUE({lon_col})         AS longitude",
    ]
    if sub_col:
        selects.append(f"  ANY_VALUE({sub_col}) AS mdse_sub_zone_number")
    sql = ("SELECT\n" + ",\n".join(selects) +
           f"\nFROM `{project}.{dataset}.store_dim`\nGROUP BY store_nbr")
    return clean_zone_map_df(run_query(sql))
