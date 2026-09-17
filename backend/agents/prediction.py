"""Flood-risk scoring for AEGIS.

Pure, deterministic functions — no LLM involved. Kept separate from the
LangGraph nodes so the scoring can be unit-tested independently.
"""

import csv
from pathlib import Path

ZONE_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "flood_zones.csv"

FACTOR_WEIGHTS = {
    "river": 0.35,
    "proximity": 0.25,
    "elevation": 0.20,
    "rainfall": 0.20,
}
HISTORICAL_WEIGHT = 0.30
DYNAMIC_WEIGHT = 0.70
RAINFALL_REFERENCE_MM = 200.0
HORIZON_HOURS = 24


def load_zones(path: Path | str | None = None) -> list[dict]:
    """Load zone attributes from CSV into typed dicts."""
    path = Path(path) if path else ZONE_CSV_PATH
    zones = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            zones.append(
                {
                    "zone_id": row["zone_id"],
                    "zone_name": row["zone_name"],
                    "elevation_m": float(row["elevation_m"]),
                    "distance_to_river_km": float(row["distance_to_river_km"]),
                    "population": int(row["population"]),
                    "historical_flood_risk": float(row["historical_flood_risk"]),
                }
            )
    return zones


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_zone(
    zone: dict,
    rainfall_mm_24h: float,
    river_level_m: float,
    river_level_danger_threshold_m: float,
) -> dict:
    """Score a single zone's flood probability over 12h and 24h horizons.

    Returns the zone's static attributes plus the computed factors and
    probabilities. Deterministic and side-effect free.
    """
    # Danger threshold <= 0 must not crash the pipeline: a degenerate (or
    # user-typed) threshold means there is no "safe" margin to normalize
    # against, so the overshoot is expressed directly in meters instead of
    # dividing by zero.
    denominator = river_level_danger_threshold_m or 1.0
    river_overshoot = max(
        0.0, (river_level_m - river_level_danger_threshold_m) / denominator
    )
    proximity_factor = 1.0 / (1.0 + zone["distance_to_river_km"])
    elevation_factor = 1.0 / (1.0 + zone["elevation_m"] / 10.0)
    rainfall_factor = min(1.0, rainfall_mm_24h / RAINFALL_REFERENCE_MM)

    weighted = (
        FACTOR_WEIGHTS["river"] * river_overshoot
        + FACTOR_WEIGHTS["proximity"] * proximity_factor
        + FACTOR_WEIGHTS["elevation"] * elevation_factor
        + FACTOR_WEIGHTS["rainfall"] * rainfall_factor
    )
    flood_probability_12h = _clamp(
        DYNAMIC_WEIGHT * weighted + HISTORICAL_WEIGHT * zone["historical_flood_risk"]
    )
    flood_probability_24h = min(1.0, flood_probability_12h * 1.15)

    return {
        **zone,
        "river_overshoot": river_overshoot,
        "proximity_factor": proximity_factor,
        "elevation_factor": elevation_factor,
        "rainfall_factor": rainfall_factor,
        "flood_probability_12h": flood_probability_12h,
        "flood_probability_24h": flood_probability_24h,
    }


def rank_zones(
    zones: list[dict],
    rainfall_mm_24h: float,
    river_level_m: float,
    river_level_danger_threshold_m: float,
) -> list[dict]:
    """Score all zones and sort descending by flood_probability_12h."""
    scored = []
    for zone in zones:
        scored.append(
            score_zone(zone, rainfall_mm_24h, river_level_m, river_level_danger_threshold_m)
        )
    return sorted(scored, key=lambda z: z["flood_probability_12h"], reverse=True)


def build_prediction(
    raw_data: dict,
    zones: list[dict] | None = None,
    top_n: int = 5,
) -> dict:
    """Build the state.prediction payload from scenario raw_data."""
    missing = {
        key
        for key in ("rainfall_mm_24h", "river_level_m", "river_level_danger_threshold_m")
        if key not in raw_data
    }
    if missing:
        raise ValueError(f"raw_data missing required keys: {sorted(missing)}")

    zones = zones if zones is not None else load_zones()
    ranked = rank_zones(
        zones,
        raw_data["rainfall_mm_24h"],
        raw_data["river_level_m"],
        raw_data["river_level_danger_threshold_m"],
    )
    at_risk = [dict(z) for z in ranked[:top_n]]

    names = ", ".join(z["zone_name"] for z in at_risk[:3])
    summary = (
        f"Zones {names} show high flood probability within 12 hours "
        "given current rainfall and river levels."
    )

    return {
        "at_risk_zones": at_risk,
        "horizon_hours": HORIZON_HOURS,
        "summary": summary,
    }


# Sentinel values: a location with no usable river data must never present a
# confident-looking probability. These shapes carry an explicit non-numeric
# hazard status instead, and downstream nodes check for it.
UNAVAILABLE = "not_applicable"
INSUFFICIENT = "insufficient_hydrology_data"


def build_unassessable_prediction(
    reason: str,
    detail: str | None = None,
    location_name: str | None = None,
) -> dict:
    """Prediction payload for a location with no usable hydrology.

    flood_probability is the literal string not_applicable — there is NO
    numeric probability and NO at_risk_zones, so no consumer can render a
    confident-looking number.
    """
    where = f" for {location_name}" if location_name else ""
    detail = detail or (
        "no river reach within GloFAS coverage and no coastal/flash-flood "
        "model applies"
    )
    return {
        "flood_probability": UNAVAILABLE,
        "reason": reason,
        "detail": detail,
        "location_name": location_name,
        "at_risk_zones": [],
        "horizon_hours": None,
        "summary": f"Flood assessment not applicable{where}: {detail}",
    }


def build_rain_watch_prediction(
    rainfall_mm_24h: float,
    reason: str | None = None,
    location_name: str | None = None,
) -> dict:
    """Explicitly labeled rainfall-only flash/urban flood watch.

    Not a riverine probability: the hazard status is the string
    insufficient_hydrology_data and no numeric probability is emitted —
    downstream consumers treat this as "rainfall observed, river dynamics
    unknown", never as a calibrated flood chance.
    """
    where = f" for {location_name}" if location_name else ""
    return {
        "flood_probability": INSUFFICIENT,
        "assessment": "flash_rain_watch",
        "rainfall_mm_24h": round(float(rainfall_mm_24h), 1),
        "reason": reason or "no river reach within GloFAS coverage",
        "location_name": location_name,
        "at_risk_zones": [],
        "horizon_hours": None,
        "summary": (
            f"Flash/urban flood watch{where}: {rainfall_mm_24h:.0f} mm/24h "
            "observed (rainfall only). Riverine risk is NOT assessed — "
            f"{reason or 'no river reach within GloFAS coverage'}."
        ),
    }