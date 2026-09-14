"""Live sensing provider for AEGIS.

Pulls current/recent weather + river data for a user-picked real location
from free public APIs (Open-Meteo — no API key, no account) and maps it into
the exact ``raw_data`` keys prediction.py already consumes:

    rainfall_mm_24h
    river_level_m
    river_level_danger_threshold_m

Notes on sources (honest):
- Rainfall: Open-Meteo Weather API ``precipitation_sum`` over the last day
  (real, mm/24h).
- River: there is no free, keyless, global river-gauge API. Open-Meteo's
  Flood API is GloFAS-backed and exposes river *discharge* (m3/s), not water
  level. We convert discharge -> level through a per-zone rating curve
  (deterministic power law) as a documented approximation, and derive the
  danger threshold from a danger discharge through the same curve.

Design: every I/O is isolated and wrapped. Any fetch failure returns
``live=False`` so the pipeline degrades to the demo PRESET instead of
hard-failing offline. Pure helpers (rating curve, raw_data builder) are
deterministic and unit-testable with zero network access.
"""

import asyncio
from datetime import datetime, timezone

import httpx

WEATHER_API = "https://api.open-meteo.com/v1/forecast"
FLOOD_API = "https://flood-api.open-meteo.com/v1/flood"

HTTPX_TIMEOUT = 15.0

# Curated real locations (lat/lon) + per-zone rating-curve coefficients.
# rating curve proxy:  river_level_m = rating_a * discharge_m3s ** rating_b
# (a deterministic, monotonic conversion from GloFAS discharge to a level.)
# danger_discharge_m3s: the flow above which the zone is considered at
# danger; danger level is the same curve applied to that discharge.
LIVE_LOCATIONS = {
    "pune": {
        "name": "Pune",
        "lat": 18.5204,
        "lon": 73.8567,
        "rating_a": 0.50,
        "rating_b": 0.30,
        "danger_discharge_m3s": 3000.0,
    },
    "kolhapur": {
        "name": "Kolhapur",
        "lat": 16.6913,
        "lon": 74.2447,
        "rating_a": 0.55,
        "rating_b": 0.28,
        "danger_discharge_m3s": 2800.0,
    },
    "surat": {
        "name": "Surat",
        "lat": 21.1702,
        "lon": 72.8311,
        "rating_a": 0.60,
        "rating_b": 0.26,
        "danger_discharge_m3s": 4500.0,
    },
    "kolkata": {
        "name": "Kolkata",
        "lat": 22.5726,
        "lon": 88.3639,
        "rating_a": 0.45,
        "rating_b": 0.32,
        "danger_discharge_m3s": 5500.0,
    },
    "guwahati": {
        "name": "Guwahati",
        "lat": 26.1445,
        "lon": 91.7362,
        "rating_a": 0.40,
        "rating_b": 0.34,
        "danger_discharge_m3s": 6500.0,
    },
}


def list_live_locations() -> list[dict]:
    return [
        {"key": key, "name": loc["name"]}
        for key, loc in sorted(LIVE_LOCATIONS.items())
    ]


def rating_curve_level_m(discharge_m3s: float, location: dict) -> float:
    """Map river discharge (m3/s) to a river level (m) via the zone curve."""
    if discharge_m3s <= 0:
        return 0.0
    return location["rating_a"] * (discharge_m3s ** location["rating_b"])


def _danger_level_m(location: dict) -> float:
    return rating_curve_level_m(location["danger_discharge_m3s"], location)


async def fetch_rainfall_mm_24h(lat: float, lon: float, client: httpx.AsyncClient) -> float:
    """Live 24h rainfall from Open-Meteo (no key)."""
    resp = await client.get(
        WEATHER_API,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "precipitation_sum",
            "timezone": "auto",
            "past_days": 1,
            "forecast_days": 0,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    sums = data["daily"]["precipitation_sum"]
    return float(sums[0])


async def fetch_river_discharge_m3s(lat: float, lon: float, client: httpx.AsyncClient) -> float:
    """Live river discharge (m3/s, GloFAS) from Open-Meteo Flood API."""
    resp = await client.get(
        FLOOD_API,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "river_discharge",
            "timezone": "auto",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    discharge = data["daily"]["river_discharge"]
    return float(discharge[0])


def build_raw_data(
    rainfall_mm_24h: float,
    discharge_m3s: float,
    location: dict,
) -> dict:
    """Map live telemetry into the exact raw_data shape prediction consumes."""
    return {
        "rainfall_mm_24h": rainfall_mm_24h,
        "river_level_m": rating_curve_level_m(discharge_m3s, location),
        "river_level_danger_threshold_m": _danger_level_m(location),
    }


async def run_live_sensing(
    location_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Top-level live-sensing call. Never raises on network failure.

    Returns {"live": True, "raw_data": {...}, "provenance": {...}} on success,
    or {"live": False, "error": "..."} on any failure so the caller can
    degrade to the demo scenario without a hard error.
    """
    location = LIVE_LOCATIONS.get(location_key)
    if not location:
        return {"live": False, "error": f"unknown location key: {location_key}"}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=HTTPX_TIMEOUT)
    try:
        try:
            rainfall, discharge = await asyncio.gather(
                fetch_rainfall_mm_24h(location["lat"], location["lon"], client),
                fetch_river_discharge_m3s(location["lat"], location["lon"], client),
            )
        except Exception as exc:
            return {"live": False, "error": f"{type(exc).__name__}: {exc}"}

        raw_data = build_raw_data(rainfall, discharge, location)
        return {
            "live": True,
            "raw_data": raw_data,
            "provenance": {
                "source": "open-meteo",
                "location": location["name"],
                "lat": location["lat"],
                "lon": location["lon"],
                "river_level_note": "derived from GloFAS discharge via rating curve",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    finally:
        if owns_client:
            await client.aclose()
