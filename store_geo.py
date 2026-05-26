"""store_geo.py — Streamlit-free helpers for turning store postal codes into
coordinates and clustering stores onto a coarse grid for weather lookups.

The weather APIs can't take one request per store when there are thousands of
them, so we snap each store to a grid cell (~GRID_DEG degrees ≈ 45–50 mi) and
fetch one weather series per occupied cell. That's local enough to separate, say,
Miami from Tallahassee, while keeping the number of API points in the hundreds.

Pure functions, unit-tested by the __main__ self-test at the bottom.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

# Grid resolution for clustering stores into weather cells. 0.75° ≈ 45–50 miles.
GRID_DEG = 0.75

# Candidate postal-code column names in store_dim, best first. Auto-detection
# falls back to a substring match if none of these are present.
POSTAL_CANDIDATES = (
    "postal_cd", "postal_code", "zip_cd", "zip_code", "postal", "zip",
    "store_postal_cd", "postal_cd_5", "zip5",
)


def pick_postal_column(columns) -> str | None:
    """Choose the store_dim column most likely to hold a US ZIP code."""
    cols = list(columns)
    lower = {c.lower(): c for c in cols}
    for cand in POSTAL_CANDIDATES:
        if cand in lower:
            return lower[cand]
    for c in cols:
        cl = c.lower()
        if "postal" in cl or ("zip" in cl and "4" not in cl and "plus" not in cl):
            return c
    return None


def normalize_zip(values) -> pd.Series:
    """Coerce a messy postal column to a 5-char US ZIP string (or <NA>).

    Handles ZIP+4 ('12345-6789' → '12345'), zero-loss from numeric storage
    ('1234' → '01234'), and floats ('12345.0' → '12345'). Non-US formats (e.g.
    Canadian 'K1A 0B1') collapse to something that won't match a US centroid and
    therefore drops out downstream."""
    s = pd.Series(list(values)).astype("string").str.strip()
    digits = s.str.extract(r"(\d+)", expand=False)

    def _fix(d):
        if not isinstance(d, str) or d == "":
            return pd.NA
        if len(d) >= 5:
            return d[:5]
        return d.zfill(5)

    return digits.map(_fix).astype("string")


def attach_coords(store_df: pd.DataFrame, zip_centroids: pd.DataFrame, postal_col: str) -> pd.DataFrame:
    """Add zip5/lat/lon to a store frame by joining postal codes to ZIP centroids.
    zip_centroids must have columns: zip, lat, lng."""
    out = store_df.copy()
    out["zip5"] = normalize_zip(out[postal_col])
    cz = zip_centroids.rename(columns={"lng": "lon"})[["zip", "lat", "lon"]].copy()
    cz["zip"] = cz["zip"].astype("string").str.zfill(5)
    out = out.merge(cz, left_on="zip5", right_on="zip", how="left").drop(columns=["zip"])
    return out


def assign_cells(geo: pd.DataFrame, grid_deg: float = GRID_DEG) -> pd.DataFrame:
    """Snap each lat/lon to a grid cell id (e.g. '42_-118'). Rows without
    coordinates get <NA>."""
    out = geo.copy()
    ok = out["lat"].notna() & out["lon"].notna()
    lat_idx = np.floor(out["lat"] / grid_deg)
    lon_idx = np.floor(out["lon"] / grid_deg)
    cell = lat_idx.astype("Int64").astype("string") + "_" + lon_idx.astype("Int64").astype("string")
    out["cell_id"] = cell.where(ok, other=pd.NA)
    return out


def cell_coords(geo: pd.DataFrame, store_key: str = "store_number") -> pd.DataFrame:
    """Representative weather point per cell = mean of its stores' coordinates."""
    g = geo.dropna(subset=["cell_id", "lat", "lon"])
    return g.groupby("cell_id", as_index=False).agg(
        lat=("lat", "mean"), lon=("lon", "mean"), stores=(store_key, "nunique"))


def chunk(items, size: int):
    """Split an iterable into lists of at most `size` (for batched API calls)."""
    items = list(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


# ─── Offline self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    assert pick_postal_column(["store_nbr", "POSTAL_CD", "city_nm"]) == "POSTAL_CD"
    assert pick_postal_column(["store_nbr", "zip_code5"]) == "zip_code5"
    assert pick_postal_column(["a", "b"]) is None
    assert pick_postal_column(["store_nbr", "mailing_zip"]) == "mailing_zip"

    z = normalize_zip(["12345", "12345-6789", 1234, "  90210 ", "K1A 0B1", 33009.0, None, ""])
    assert list(z[:4]) == ["12345", "12345", "01234", "90210"], list(z[:4])
    assert z[5] == "33009", z[5]
    assert pd.isna(z[6]) and pd.isna(z[7])
    print("zip normalize:", list(z))

    centroids = pd.DataFrame({
        "zip": ["12345", "90210", "33009", "01234"],
        "lat": [42.81, 34.10, 25.98, 42.34],
        "lng": [-73.93, -118.41, -80.13, -72.20]})
    stores = pd.DataFrame({
        "store_number": [1, 2, 3, 4, 5],
        "postal_cd": ["12345", "90210-1234", 33009, "K1A 0B1", "01234"]})
    g = attach_coords(stores, centroids, "postal_cd")
    g = assign_cells(g)
    print(g[["store_number", "zip5", "lat", "lon", "cell_id"]].to_string(index=False))
    assert g["lat"].notna().sum() == 4, "4 of 5 stores should map (Canada drops)"
    assert pd.isna(g.loc[g["store_number"] == 4, "cell_id"].iloc[0])
    cells = cell_coords(g)
    assert len(cells) == g["cell_id"].dropna().nunique()
    assert cells["stores"].sum() == 4
    print("cells:\n", cells.to_string(index=False))

    assert chunk(range(7), 3) == [[0, 1, 2], [3, 4, 5], [6]]
    assert chunk([], 3) == []

    # Sanity-check against the bundled centroid file if present.
    from pathlib import Path
    p = Path(__file__).parent / "data" / "zip_centroids.csv"
    if p.exists():
        zc = pd.read_csv(p, dtype={"zip": str})
        demo = pd.DataFrame({"store_number": [10, 11, 12],
                             "postal_cd": ["33139", "98101", "10001"]})  # Miami, Seattle, NYC
        dg = assign_cells(attach_coords(demo, zc, "postal_cd"))
        print("\nbundled-file demo:\n", dg[["postal_cd", "lat", "lon", "cell_id"]].to_string(index=False))
        assert dg["cell_id"].nunique() == 3, "three far-apart cities → three cells"
    print("\nAll store_geo self-tests passed.")
