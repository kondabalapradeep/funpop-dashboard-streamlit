"""weather_source.py — live US weather grid (Open-Meteo) + NWS-style map render.

Powers the Weather tab. Pulls daily **high temperature** and **precipitation**
for a coarse CONUS grid from Open-Meteo's free Forecast API (no API key, free for
non-commercial use), then interpolates each day into a smooth filled-contour map
clipped to the US outline — the look of NOAA's National Digital Forecast Database.

One request covers the whole window: ``past_days=7`` + ``forecast_days=16`` returns
~23 daily values per point (7 days back, today, and up to 16 days ahead). Open-Meteo
accepts comma-separated coordinate lists, so the whole grid comes back in a handful
of batched calls.

Weather is real-time, so it is fetched **live on view** (the app caches it ~3h) and
is **never written to the parquet snapshot** — a stored copy would only go stale.
The boundary polygon ships in the repo (``data/us_states_conus.geojson``) so the
server makes no outbound call other than to Open-Meteo itself.
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CENTRAL_TZ = ZoneInfo("America/Chicago")

# Window: 7 past days + today + up to 16 forecast days (Open-Meteo's max horizon).
PAST_DAYS = 7
FORECAST_DAYS = 16

# Coarse CONUS sampling grid. ~2° spacing (~370 points) is plenty — the field is
# interpolated, so finer sampling mostly adds API load and edge noise. The bbox
# runs a little into the oceans/Canada/Mexico on purpose: those border points
# anchor the coastal gradient before we clip the raster back to the US silhouette.
GRID_STEP = 2.0
LAT_RANGE = (25.0, 49.0)
LON_RANGE = (-124.0, -67.0)

# Open-Meteo caps coordinates per request; stay well under and batch.
BATCH_SIZE = 100

BOUNDARY_PATH = Path(__file__).parent / "data" / "us_states_conus.geojson"

# Daily-high temperature ramp modelled on NWS/NDFD: violet→blue→cyan→green→
# yellow→orange→red→maroon across roughly 20–120 °F.
TEMP_COLORS = [
    "#7d00b3", "#2222cc", "#1d7fe0", "#19c0d6", "#21d07a", "#4fd11b",
    "#b6e000", "#ffe600", "#ffae00", "#ff6a00", "#e01f00", "#9b1010", "#5a0606",
]
TEMP_LEVELS = np.arange(20, 125, 5)
TEMP_TICKS = np.arange(20, 130, 10)

# Precipitation ramp: dry (white) → green → blue → purple, in inches/day.
PRECIP_COLORS = [
    "#ffffff", "#d7f0c8", "#9fe08a", "#4fc04f", "#2fa0c0", "#2060d0",
    "#3030b0", "#702090", "#a01070",
]
PRECIP_LEVELS = [0.0, 0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
PRECIP_TICKS = PRECIP_LEVELS


def _grid_points():
    """The list of (lat, lon) sample points we ask Open-Meteo for."""
    lats = np.arange(LAT_RANGE[0], LAT_RANGE[1] + 1e-9, GRID_STEP)
    lons = np.arange(LON_RANGE[0], LON_RANGE[1] + 1e-9, GRID_STEP)
    return [(round(float(la), 2), round(float(lo), 2)) for la in lats for lo in lons]


def _request(batch, timeout=60):
    """Fetch one batch of points. Returns the parsed JSON (a list of per-location
    records, or a single dict when the batch has one point)."""
    query = urllib.parse.urlencode({
        "latitude": ",".join(str(p[0]) for p in batch),
        "longitude": ",".join(str(p[1]) for p in batch),
        "daily": "temperature_2m_max,precipitation_sum",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "America/Chicago",
        "past_days": PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
    })
    req = urllib.request.Request(
        f"{OPEN_METEO_URL}?{query}",
        headers={"User-Agent": "funpop-dashboard/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_weather_grid():
    """Pull the full grid and return a compact dict:

        {
          "dates":  ["2026-06-14", …],          # length D (≈23)
          "lat":    np.ndarray (N,),            # model cell centres returned by the API
          "lon":    np.ndarray (N,),
          "temp":   np.ndarray (N, D),          # daily high °F
          "precip": np.ndarray (N, D),          # daily total inches
        }

    Raises on transport/HTTP errors so the caller can degrade gracefully.
    """
    points = _grid_points()
    dates = None
    lat_all, lon_all, temp_all, precip_all = [], [], [], []
    for i in range(0, len(points), BATCH_SIZE):
        data = _request(points[i:i + BATCH_SIZE])
        if isinstance(data, dict):
            data = [data]
        for rec in data:
            daily = rec["daily"]
            if dates is None:
                dates = daily["time"]
            # Use the cell centre the API snapped to — slightly truer than what we asked.
            lat_all.append(rec["latitude"])
            lon_all.append(rec["longitude"])
            temp_all.append(daily["temperature_2m_max"])
            precip_all.append(daily["precipitation_sum"])
        if i + BATCH_SIZE < len(points):
            time.sleep(1.0)  # be polite; well within the free per-minute limit
    return {
        "dates": dates,
        "lat": np.asarray(lat_all, dtype="float64"),
        "lon": np.asarray(lon_all, dtype="float64"),
        "temp": np.asarray(temp_all, dtype="float64"),
        "precip": np.asarray(precip_all, dtype="float64"),
    }


def today_index(dates):
    """Index of today (Central) in ``dates``, or the last past day if today is
    somehow missing — this is the day the slider should open on."""
    today = datetime.now(CENTRAL_TZ).date().isoformat()
    if today in dates:
        return dates.index(today)
    return min(PAST_DAYS, len(dates) - 1)


def day_label(date_str, index):
    """Slider label like ``'Sat Jun 14 · −7d'`` / ``'today'`` / ``'+5d'``."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    offset = index - PAST_DAYS
    if offset == 0:
        tag = "today"
    elif offset < 0:
        tag = f"{offset}d"
    else:
        tag = f"+{offset}d"
    return f"{d.strftime('%a %b %-d')} · {tag}"


def _load_polys():
    """State boundary rings as a list of (M,2) lon/lat arrays."""
    geo = json.loads(BOUNDARY_PATH.read_text())
    polys = []
    for feat in geo["features"]:
        geom = feat["geometry"]
        multi = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in multi:
            for ring in poly:
                polys.append(np.asarray(ring, dtype="float64"))
    return polys


def _clip_path(polys):
    """A single matplotlib Path spanning every ring — used to clip the fill and to
    test which points fall on US land."""
    from matplotlib.path import Path as MplPath

    verts, codes = [], []
    for ring in polys:
        verts.extend(ring)
        codes.append(MplPath.MOVETO)
        codes.extend([MplPath.LINETO] * (len(ring) - 1))
    return MplPath(np.asarray(verts), codes)


def render_map(grid, day_index, metric):
    """Render one day as a smooth NWS-style filled-contour Figure.

    ``metric`` is ``"temp"`` or ``"precip"``. Returns a matplotlib Figure ready
    for ``st.pyplot``.
    """
    import matplotlib
    matplotlib.use("Agg")  # headless server; the app uses no other matplotlib state
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap
    from matplotlib.patches import PathPatch
    from scipy.interpolate import griddata

    polys = _load_polys()
    clip = _clip_path(polys)

    values = grid["temp" if metric == "temp" else "precip"][:, day_index]
    seeds = np.c_[grid["lon"], grid["lat"]]
    ok = np.isfinite(values)
    seeds, values = seeds[ok], values[ok]
    if metric == "precip":
        values = np.clip(values, 0.0, None)

    # Dense raster, interpolate, backfill edge gaps with nearest so the clip is clean.
    gx = np.linspace(LON_RANGE[0], LON_RANGE[1], 600)
    gy = np.linspace(LAT_RANGE[0], LAT_RANGE[1], 320)
    mx, my = np.meshgrid(gx, gy)
    z = griddata(seeds, values, (mx, my), method="cubic")
    z_near = griddata(seeds, values, (mx, my), method="nearest")
    z = np.where(np.isnan(z), z_near, z)
    if metric == "precip":
        z = np.clip(z, 0.0, None)
    inside = clip.contains_points(np.c_[mx.ravel(), my.ravel()]).reshape(mx.shape)
    z = np.ma.array(z, mask=~inside)

    if metric == "temp":
        cmap = LinearSegmentedColormap.from_list("nws_temp", TEMP_COLORS)
        levels, ticks, unit = TEMP_LEVELS, TEMP_TICKS, "°F"
    else:
        cmap = LinearSegmentedColormap.from_list("nws_precip", PRECIP_COLORS)
        levels, ticks, unit = PRECIP_LEVELS, PRECIP_TICKS, "in"
    norm = BoundaryNorm(levels, cmap.N)

    fig = plt.figure(figsize=(10.5, 6.6))
    ax = fig.add_axes([0.02, 0.04, 0.96, 0.82])
    cf = ax.contourf(mx, my, z, levels=levels, cmap=cmap, norm=norm, extend="both")

    patch = PathPatch(clip, transform=ax.transData, facecolor="none", edgecolor="none")
    ax.add_patch(patch)
    cf.set_clip_path(patch)
    for ring in polys:
        ax.plot(ring[:, 0], ring[:, 1], color="#555555", lw=0.5, zorder=3)

    # Baked-in point labels (no hover on a static image), only over US land.
    on_land = clip.contains_points(seeds)
    fmt = "{:.0f}".format if metric == "temp" else (lambda v: f"{v:.2f}".lstrip("0") or "0")
    for (lon, lat), val, land in list(zip(seeds, values, on_land)):
        if land and not (metric == "precip" and val < 0.01):
            ax.annotate(fmt(val), (lon, lat), ha="center", va="center",
                        fontsize=6.0, color="black", zorder=4)

    ax.set_xlim(LON_RANGE[0], LON_RANGE[1])
    ax.set_ylim(LAT_RANGE[0] - 1, LAT_RANGE[1])
    ax.set_aspect(1.25)
    ax.axis("off")

    cax = fig.add_axes([0.10, 0.92, 0.80, 0.030])
    cbar = fig.colorbar(cf, cax=cax, orientation="horizontal", ticks=ticks)
    cbar.ax.tick_params(labelsize=8)
    cbar.ax.xaxis.set_ticks_position("top")
    cbar.set_label("")

    label = "Daytime High Temperature" if metric == "temp" else "Daily Precipitation"
    fig.text(0.5, 0.875, f"{label}  ({unit})  —  {day_label(grid['dates'][day_index], day_index)}",
             ha="center", fontsize=12, fontweight="bold")
    return fig
