"""Build the static store address directory from store_dim.

The Store Actions tab exports a per-store mailing address (number, address,
city, state, zip) for the field-service vendor. Addresses are reference data
that essentially never change, so rather than query store_dim on every cold
start the scheduled snapshot job (snapshot_build.py) builds the directory once
and commits it as a parquet under snapshot_data/; the deployed app reads that
committed file straight from disk. If the file is missing the app falls back to
building it live.

store_dim's exact column names for address/zip differ between feeds, so instead
of hard-coding them we introspect INFORMATION_SCHEMA and pick the best-matching
column for each field. This module is import-safe (no streamlit) so both the app
and the builder can share it.
"""
from pathlib import Path

import pandas as pd

# The committed directory lives alongside the other snapshot data so the
# deployed app finds it on disk. It is read directly (not through the
# freshness-gated snapshot reader) because addresses are static reference data —
# an "old" directory is still correct.
DIRECTORY_FILENAME = "store_directory"          # parquet key (no extension)
DIRECTORY_PATH = Path(__file__).resolve().parent / "snapshot_data" / f"{DIRECTORY_FILENAME}.parquet"

# Candidate substrings per field, most specific first. The first store_dim
# column whose lowercased name contains one of these wins.
_CANDIDATES = {
    "address": ["addr_line_1", "address_line_1", "address1", "addr_ln_1", "addr_1",
                "street_addr", "addr_line", "address", "addr", "street"],
    "city": ["city_name", "city"],
    "state": ["state_prov_cd", "state_cd", "state_prov", "state", "prov"],
    "zip": ["postal_cd", "postal", "zip_cd", "zip"],
    "store_name": ["store_name", "store_nm", "location_name"],
}
_STORE_KEY_CANDIDATES = ["store_nbr", "store_num", "store_id"]


def best_col(columns_lower: dict, candidates: list) -> str | None:
    """Return the actual column whose lowercased name first contains one of the
    `candidates` substrings, tried in priority order (most specific first)."""
    for sub in candidates:
        for low, actual in columns_lower.items():
            if sub in low:
                return actual
    return None


def clean_directory_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise dtypes and repair ZIP leading zeros a numeric column would drop."""
    if df.empty:
        return df
    df = df.copy()
    df["store_number"] = pd.to_numeric(df["store_number"], errors="coerce").fillna(0).astype("int32")
    for c in ["store_name", "address", "city", "state", "zip"]:
        if c in df.columns:
            df[c] = df[c].astype("string").str.strip()
    if "zip" in df.columns:
        z = df["zip"].fillna("").str.replace(r"\.0+$", "", regex=True).str.strip()
        # New England ZIPs (0xxxx) lose their leading zero if store_dim stored
        # the column as a number; pad a purely-numeric short value back to 5.
        df["zip"] = z.where(~z.str.fullmatch(r"\d{1,4}"), z.str.zfill(5))
    return df


def build_directory_df(run_query, project: str, dataset: str) -> pd.DataFrame:
    """Build the deduped one-row-per-store directory.

    `run_query` is a callable taking a SQL string and returning a DataFrame, so
    the app (Streamlit BQ client) and the builder (raw BQ client) can share this.
    Raises on a missing store_dim / store key; column gaps for individual address
    fields are tolerated (that field is simply omitted).
    """
    schema = run_query(
        f"SELECT column_name FROM `{project}.{dataset}`.INFORMATION_SCHEMA.COLUMNS "
        "WHERE LOWER(table_name) = 'store_dim'"
    )
    if schema.empty:
        raise ValueError("store_dim schema not found")
    cols = {str(c).lower(): c for c in schema["column_name"].tolist()}
    store_col = best_col(cols, _STORE_KEY_CANDIDATES)
    if not store_col:
        raise ValueError("no store-number column on store_dim")

    selects = [f"  {store_col} AS store_number"]
    for alias in ["store_name", "address", "city", "state", "zip"]:
        col = best_col(cols, _CANDIDATES[alias])
        if col:
            selects.append(f"  ANY_VALUE(CAST({col} AS STRING)) AS {alias}")
    sql = ("SELECT\n" + ",\n".join(selects) +
           f"\nFROM `{project}.{dataset}.store_dim`\nGROUP BY {store_col}")
    return clean_directory_df(run_query(sql))
