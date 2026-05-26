"""Shared item constants — the single source of truth for both the live
dashboard (streamlit_app.py) and the legacy data-prep module (funpop_core.py).

Keeping item IDs, labels, and case-pack sizes in one place prevents the two
files from drifting apart (which is how the Full/Half bin pack sizes once got
swapped between them)."""

# Walmart item numbers for the active FunPop SKUs.
ACTIVE_ITEMS = [658442130, 666209064, 658442128]

BIN_ITEMS = [658442128, 658442130]   # Full + Half bins
SHELF_ITEMS = [666209064]

# Display labels.
ITEM_LABELS = {
    658442128: "Full Bin",
    658442130: "Half Bin",
    666209064: "Shelf",
}

# Eaches per warehouse case-pack. Used to convert DC pack quantities to units.
CASE_PACK_UNITS = {
    658442128: 208,  # Full Bin
    658442130: 126,  # Half Bin
    666209064: 6,    # Shelf
}

# Container metadata for the legacy HTML path: units (derived from CASE_PACK_UNITS
# so they can't disagree) plus the pack-type label.
_CONTAINER_LABELS = {658442128: "bins", 658442130: "bins", 666209064: "cases"}
CONTAINER = {
    item: {"units": CASE_PACK_UNITS[item], "label": _CONTAINER_LABELS[item]}
    for item in CASE_PACK_UNITS
}
