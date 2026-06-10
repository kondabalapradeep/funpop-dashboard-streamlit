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
