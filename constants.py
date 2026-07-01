"""Shared item constants — the single source of truth for the dashboard
(streamlit_app.py), the dtype transforms (transforms.py), and the snapshot
builder (snapshot_build.py).

Keeping item IDs, labels, and case-pack sizes in one place prevents callers
drifting apart. The Full/Half assignment below is verified against the feed
itself: item_dim names 658442130 "FUN POPS FULL BIN" and 658442128
"FUN POPS 1/2 BIN 126", and dc_item's ty_whpk_each_qty carries 208 / 126
eaches per pack for them respectively. (An earlier revision had the two
swapped — if these ever look wrong, re-check against those feed columns.)"""

# Walmart item numbers for the active FunPop SKUs.
ACTIVE_ITEMS = [658442130, 666209064, 658442128]

BIN_ITEMS = [658442128, 658442130]   # Half + Full bins
SHELF_ITEMS = [666209064]

# Display labels.
ITEM_LABELS = {
    658442128: "Half Bin",
    658442130: "Full Bin",
    666209064: "Shelf",
}

# Coarser display grouping that rolls the Full + Half bins into a single "Bins"
# line. Used by the per-item breakouts on the Overview and Sales & Velocity tabs,
# where splitting the two bin packs is noise rather than signal. Shelf stays on
# its own.
BINS_GROUP_LABEL = "Bins"


def item_group_label(item):
    """Display group for an item: Full/Half bins collapse to 'Bins'; Shelf and
    anything else fall back to their per-item label."""
    if item in BIN_ITEMS:
        return BINS_GROUP_LABEL
    return ITEM_LABELS.get(item, str(item))


# Eaches per warehouse case-pack. Used to convert DC pack quantities to units
# when the feed's per-row ty_whpk_each_qty is missing.
CASE_PACK_UNITS = {
    658442128: 126,  # Half Bin ("FUN POPS 1/2 BIN 126")
    658442130: 208,  # Full Bin
    666209064: 6,    # Shelf
}

# Reference points for the Distribution tab's merchandising-zone map — major
# US cities, for orientation only (not store locations). (name, lat, lon).
MAJOR_US_CITIES = [
    ("New York", 40.7128, -74.0060),
    ("Los Angeles", 34.0522, -118.2437),
    ("Chicago", 41.8781, -87.6298),
    ("Houston", 29.7601, -95.3701),
    ("Phoenix", 33.4484, -112.0740),
    ("Philadelphia", 39.9526, -75.1652),
    ("San Antonio", 29.4241, -98.4936),
    ("Dallas", 32.7767, -96.7970),
    ("Seattle", 47.6062, -122.3321),
    ("Denver", 39.7392, -104.9903),
    ("Atlanta", 33.7490, -84.3880),
    ("Miami", 25.7617, -80.1918),
    ("Minneapolis", 44.9778, -93.2650),
    ("Boston", 42.3601, -71.0589),
    ("Detroit", 42.3314, -83.0458),
    ("Kansas City", 39.0997, -94.5786),
    ("Salt Lake City", 40.7608, -111.8910),
    ("New Orleans", 29.9511, -90.0715),
    ("Nashville", 36.1627, -86.7816),
    ("Portland", 45.5152, -122.6784),
]
