"""weather_model.py — the weather→sales engine behind the Weather & Demand tab.

Pure functions and a small model class, with **no Streamlit or BigQuery imports**,
so the statistics can be unit-tested offline (see the __main__ self-test at the
bottom). streamlit_app.py owns all the DataFrame plumbing, API calls, and UI;
this module owns the maths.

The business reality we are modelling: FunPop sells more when it is hot, and more
still when it is hot *and* dry. We turn that intuition into a transparent linear
model of daily per-store velocity:

    units_per_store ≈ b0
                    + b_heat   · heat        (degrees above the 70°F selling line)
                    + b_rain   · rain        (inches of precip; expected negative)
                    + b_hotdry · hot_dry      (heat that lands on a dry day — the synergy)
                    + b_wknd   · is_weekend   (Sat/Sun lift, so we don't credit weather for it)
                    + b_trend  · trend         (gentle within-window drift: seasonal ramp,
                                               distribution gains — so a summer build-up
                                               isn't mistaken for a weather effect)

Everything the tab shows — driver explanations, the "why did sales move"
decomposition, and the forward forecast — is derived from these coefficients, so
the numbers always reconcile with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# The temperature at which heat starts to pull demand. Below this, warmth does
# not move the needle; above it, every degree is "selling heat".
HEAT_BASE_F = 70.0
# Precip (inches/day) at or above which the "dryness bonus" is fully gone. A day
# with < this much rain is partially-to-fully dry on a 0..1 scale.
DRY_REF_IN = 0.25

WEATHER_FEATURES = ("heat", "rain", "hot_dry")


def add_weather_features(wx: pd.DataFrame) -> pd.DataFrame:
    """Attach the model's input features to a frame that has temperature_max_f
    and precipitation_in columns. Safe on an empty frame."""
    if wx.empty:
        out = wx.copy()
        for col in ("heat", "rain", "dryness", "hot_dry", "heat_dry_score", "weather_label"):
            out[col] = pd.Series(dtype="float64" if col != "weather_label" else "object")
        return out
    out = wx.copy()
    temp = pd.to_numeric(out["temperature_max_f"], errors="coerce")
    precip = pd.to_numeric(out["precipitation_in"], errors="coerce").clip(lower=0)
    out["heat"] = (temp - HEAT_BASE_F).clip(lower=0).fillna(0)
    out["rain"] = precip.fillna(0)
    out["dryness"] = (1 - precip / DRY_REF_IN).clip(lower=0, upper=1).fillna(0)
    out["hot_dry"] = out["heat"] * out["dryness"]
    out["heat_dry_score"] = heat_dry_score(temp, precip)
    out["weather_label"] = score_label(out["heat_dry_score"])
    return out


def heat_dry_score(temp_f, precip_in) -> np.ndarray:
    """A 0–100 'selling weather' index blending heat (70%) and dryness (30%).
    Vectorised; accepts scalars, Series, or arrays. Used for labels and maps —
    the regression model uses the raw features above, not this composite."""
    temp = pd.to_numeric(pd.Series(temp_f), errors="coerce").fillna(0).to_numpy(dtype=float)
    precip = pd.to_numeric(pd.Series(precip_in), errors="coerce").fillna(0).to_numpy(dtype=float)
    heat = np.clip((temp - HEAT_BASE_F) / 25.0, 0, 1.4)
    dry = np.clip(1 - (precip / 0.50), 0, 1)
    return np.round((0.7 * heat + 0.3 * dry) * 100, 0)


def score_label(score) -> np.ndarray:
    """Map a heat/dry score to a salesperson-facing label."""
    score = np.asarray(score, dtype=float)
    return np.select(
        [score >= 90, score >= 65, score >= 40],
        ["Prime", "Good", "Neutral"],
        default="Soft",
    )


def verdict_phrase(temp_f: float, precip_in: float) -> str:
    """One-line plain-English read on a single day's conditions."""
    hot = temp_f >= 85
    warm = temp_f >= 75
    dry = precip_in <= 0.05
    wet = precip_in >= 0.25
    if hot and dry:
        return "hot and dry — peak selling weather"
    if hot and wet:
        return "hot but wet — heat helps, rain holds it back"
    if hot:
        return "hot — a strong demand day"
    if warm and dry:
        return "warm and dry — solid conditions"
    if warm:
        return "mild — roughly average demand"
    if wet:
        return "cool and wet — expect a soft day"
    return "cool — below-average selling weather"


@dataclass
class VelocityModel:
    """Fitted per-store-per-day velocity model. ``coef`` holds the guarded
    coefficients used for every prediction and decomposition (signs forced to be
    economically sensible). ``raw_coef`` keeps the unguarded fit for transparency."""

    coef: dict
    raw_coef: dict
    r2: float
    n: int
    n_areas: int
    resid_std: float
    units_per_store_mean: float
    weekend_share: float
    guarded: bool = False
    notes: list = field(default_factory=list)

    # ── components ────────────────────────────────────────────────────────────
    def weather_component(self, feats: pd.DataFrame) -> pd.Series:
        """The weather-only contribution to units/store/day (heat + rain + synergy)."""
        return (
            self.coef["heat"] * feats["heat"]
            + self.coef["rain"] * feats["rain"]
            + self.coef["hot_dry"] * feats["hot_dry"]
        )

    def weekend_component(self, dates) -> pd.Series:
        is_wknd = pd.to_datetime(pd.Series(dates).reset_index(drop=True)).dt.weekday.isin([5, 6])
        return self.coef["weekend"] * is_wknd.astype(float)

    # ── trust ─────────────────────────────────────────────────────────────────
    @property
    def confidence(self) -> str:
        if self.coef["heat"] <= 0 or self.n < 14:
            return "Low"
        if self.r2 >= 0.35 and self.n >= 21:
            return "High"
        if self.r2 >= 0.15:
            return "Moderate"
        return "Low"

    @property
    def confidence_note(self) -> str:
        return (
            f"Fit on {self.n:,} area-days across {self.n_areas} local weather areas · "
            f"R²={self.r2:.2f} (share of day-to-day velocity swings the model explains)."
        )

    def driver_summary(self) -> list:
        """Plain-English bullets describing what the model learned, in units/store/day."""
        bullets = []
        per10 = self.coef["heat"] * 10.0
        bullets.append(
            f"Every **10°F above {HEAT_BASE_F:.0f}°F** adds about **{per10:+.2f}** units/store/day."
        )
        per_inch = self.coef["rain"]
        bullets.append(
            f"Each **inch of rain** moves velocity about **{per_inch:+.2f}** units/store/day."
        )
        # Synergy: a hot (90°F → heat=20) fully-dry day vs the same heat fully wet.
        synergy = self.coef["hot_dry"] * 20.0
        if synergy >= 0.01:
            bullets.append(
                f"**Hot *and* dry** stacks on top: a 90°F bone-dry day beats a 90°F wet day "
                f"by roughly **{synergy:+.2f}** units/store/day, on top of the rain effect."
            )
        wknd = self.coef["weekend"]
        if abs(wknd) >= 0.01:
            bullets.append(
                f"Weekends run **{wknd:+.2f}** units/store/day vs. weekdays (held separate so "
                f"it isn't blamed on weather)."
            )
        return bullets


def _build_design(feats: pd.DataFrame, trend: np.ndarray):
    """Design matrix and column names shared by the fit. Columns:
    intercept, heat, rain, hot_dry, weekend, trend."""
    is_wknd = pd.to_datetime(feats["date"]).dt.weekday.isin([5, 6]).astype(float).to_numpy()
    cols = [
        np.ones(len(feats)),
        feats["heat"].to_numpy(dtype=float),
        feats["rain"].to_numpy(dtype=float),
        feats["hot_dry"].to_numpy(dtype=float),
        is_wknd,
        trend,
    ]
    names = ["intercept", "heat", "rain", "hot_dry", "weekend", "trend"]
    return np.column_stack(cols), names


def fit_velocity_model(panel: pd.DataFrame) -> VelocityModel | None:
    """Fit the velocity model on a state-day panel.

    ``panel`` must have: units_per_store, heat, rain, hot_dry, date (datetime).
    Returns None when there is not enough signal to fit (caller falls back to the
    heat/dry-score heuristic)."""
    needed = ["units_per_store", "heat", "rain", "hot_dry", "date"]
    if panel.empty or any(c not in panel.columns for c in needed):
        return None
    data = panel.dropna(subset=needed).copy()
    data = data[np.isfinite(data["units_per_store"])]
    if len(data) < 14 or data["units_per_store"].std(ddof=0) == 0:
        return None

    y = data["units_per_store"].to_numpy(dtype=float)
    # Centre + scale a day index to [-1, 1] so the trend term is well-conditioned.
    day_idx = (pd.to_datetime(data["date"]) - pd.to_datetime(data["date"]).min()).dt.days.to_numpy(dtype=float)
    span = day_idx.max() if day_idx.max() > 0 else 1.0
    trend = (day_idx / span) * 2 - 1

    X, names = _build_design(data, trend)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    raw_coef = dict(zip(names, beta))

    yhat = X @ beta
    resid = y - yhat
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    resid_std = float(np.std(resid, ddof=0))

    # Guard the coefficients used downstream so the customer-facing predictor and
    # decomposition stay economically sane even when a short/noisy window throws
    # an implausible sign: heat & synergy can't hurt sales, rain can't help.
    coef = dict(raw_coef)
    notes = []
    if coef["heat"] < 0:
        coef["heat"] = 0.0
        coef["hot_dry"] = 0.0
        notes.append("Heat signal was flat/negative in this window — heat effects neutralised.")
    if coef["hot_dry"] < 0:
        coef["hot_dry"] = 0.0
    if coef["rain"] > 0:
        coef["rain"] = 0.0
        notes.append("Rain signal was non-negative in this window — rain penalty neutralised.")
    guarded = coef != raw_coef

    return VelocityModel(
        coef=coef,
        raw_coef=raw_coef,
        r2=max(0.0, r2),
        n=len(data),
        n_areas=int(data["area"].nunique()) if "area" in data else 0,
        resid_std=resid_std,
        units_per_store_mean=float(y.mean()),
        weekend_share=float(pd.to_datetime(data["date"]).dt.weekday.isin([5, 6]).mean()),
        guarded=guarded,
        notes=notes,
    )


def decompose_change(recent: dict, prior: dict, model: VelocityModel | None) -> dict:
    """Exact (mid-point / Shapley) split of the change in daily units between two
    equal-footing periods into store-count, weather, and everything-else effects.

    ``recent`` and ``prior`` are dicts with:
        stores  — avg stores selling per day
        vel     — avg units per store per day
        heat, rain, hot_dry — mean weather features over the period

    All effects are expressed as **units per day** and sum exactly to the change
    in daily units (store_effect + weather_effect + other_effect)."""
    s_r, s_p = recent["stores"], prior["stores"]
    v_r, v_p = recent["vel"], prior["vel"]
    s_mid = (s_r + s_p) / 2
    v_mid = (v_r + v_p) / 2

    daily_recent = s_r * v_r
    daily_prior = s_p * v_p
    total_delta = daily_recent - daily_prior

    store_effect = (s_r - s_p) * v_mid          # change driven by # of stores selling
    vel_effect = (v_r - v_p) * s_mid            # change driven by per-store velocity

    if model is not None:
        feats_r = pd.DataFrame([{k: recent[k] for k in WEATHER_FEATURES}])
        feats_p = pd.DataFrame([{k: prior[k] for k in WEATHER_FEATURES}])
        wc_r = float(model.weather_component(feats_r).iloc[0])
        wc_p = float(model.weather_component(feats_p).iloc[0])
        weather_vel_delta = wc_r - wc_p
    else:
        wc_r = wc_p = 0.0
        weather_vel_delta = 0.0

    weather_effect = weather_vel_delta * s_mid
    other_effect = vel_effect - weather_effect

    return {
        "daily_recent": daily_recent,
        "daily_prior": daily_prior,
        "total_delta": total_delta,
        "store_effect": store_effect,
        "weather_effect": weather_effect,
        "other_effect": other_effect,
        "vel_effect": vel_effect,
        "weather_vel_delta": weather_vel_delta,
        "wc_recent": wc_r,
        "wc_prior": wc_p,
    }


# ─── Offline self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    rng = np.random.default_rng(7)
    # Synthesise a state-day panel where the TRUE relationship is known:
    #   vel = 6 + 0.20·heat - 1.5·rain + 0.10·hot_dry + 0.8·weekend + noise
    dates = pd.date_range("2025-04-01", periods=60, freq="D")
    states = ["TX", "FL", "GA", "AZ"]
    rows = []
    for st_code in states:
        for d in dates:
            temp = rng.normal(86, 9)
            precip = max(0, rng.normal(0.08, 0.18))
            rows.append({"area": st_code, "date": d,
                         "temperature_max_f": temp, "precipitation_in": precip})
    panel = add_weather_features(pd.DataFrame(rows))
    is_wknd = panel["date"].dt.weekday.isin([5, 6]).astype(float)
    true_vel = (6 + 0.20 * panel["heat"] - 1.5 * panel["rain"]
                + 0.10 * panel["hot_dry"] + 0.8 * is_wknd
                + rng.normal(0, 0.6, len(panel)))
    panel["units_per_store"] = true_vel.clip(lower=0)

    model = fit_velocity_model(panel)
    assert model is not None, "model should fit on 240 state-days"
    print("=== Fit ===")
    print("coef:", {k: round(v, 3) for k, v in model.coef.items()})
    print("R²:", round(model.r2, 3), "n:", model.n, "areas:", model.n_areas,
          "confidence:", model.confidence)
    for b in model.driver_summary():
        print("  •", b.replace("**", ""))
    # Recovered coefficients should be in the right ballpark / right signs.
    assert model.coef["heat"] > 0.1, model.coef
    assert model.coef["rain"] < -0.5, model.coef
    assert model.coef["weekend"] > 0.3, model.coef

    print("\n=== Decomposition (hotter+drier recent week, fewer stores) ===")
    prior = {"stores": 1000, "vel": 6.0, "heat": 12.0, "rain": 0.20, "hot_dry": 6.0}
    recent = {"stores": 980, "vel": 7.2, "heat": 18.0, "rain": 0.05, "hot_dry": 14.0}
    dec = decompose_change(recent, prior, model)
    print("  total Δ/day:", round(dec["total_delta"], 1))
    print("  store effect:", round(dec["store_effect"], 1))
    print("  weather effect:", round(dec["weather_effect"], 1))
    print("  other effect:", round(dec["other_effect"], 1))
    recombined = dec["store_effect"] + dec["weather_effect"] + dec["other_effect"]
    assert abs(recombined - dec["total_delta"]) < 1e-6, (recombined, dec["total_delta"])
    assert dec["weather_effect"] > 0, "hotter+drier week should be a weather tailwind"

    print("\n=== Edge cases ===")
    assert fit_velocity_model(pd.DataFrame()) is None
    tiny = panel.head(5)
    assert fit_velocity_model(tiny) is None, "should refuse to fit on 5 rows"
    empty_feat = add_weather_features(pd.DataFrame())
    assert empty_feat.empty and "hot_dry" in empty_feat.columns
    print("scores:", heat_dry_score([95, 72, 60], [0.0, 0.1, 0.4]))
    print("labels:", score_label(heat_dry_score([95, 72, 60], [0.0, 0.1, 0.4])))
    print("verdict:", verdict_phrase(92, 0.0))
    print("\nAll self-tests passed.")
