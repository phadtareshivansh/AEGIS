"""Live sensing provider for AEGIS.

Pulls current/recent weather + river data for a user-picked real location
from free public APIs (Open-Meteo — no API key, no account) and maps it into
the exact ``raw_data`` keys prediction.py consumes:

    rainfall_mm_24h
    river_level_m
    river_level_danger_threshold_m

Location is free-text, geocoded via Open-Meteo's keyless Geocoding API. The
five curated demo cities still exist as convenience "quick picks", but the
river layer is now location-generic GloFAS, not a per-city lookup.

Notes on sources (honest):
- Rainfall: Open-Meteo Weather API ``precipitation_sum`` over the last day
  (real, mm/24h). Works at any geocoded coordinate.
- River: Open-Meteo's Flood API is GloFAS-backed and exposes river
  discharge (m3/s), not water level. It returns "the largest river within
  a 5 km area" of the requested coordinate and NEVER errors for a point
  with no river — it silently snaps to a neighbouring cell. We therefore:
  1. sample a small ring of 0.1-degree offsets and take the dominant reach
     (max climatological mean) — a geocoded town centre is rarely on the
     main channel, and GloFAS is ~5 km resolution;
  2. detect "no reach" from the data itself: a genuinely dry cell returns a
     constant residual discharge pinned to its climatology (e.g. 0.03 m3/s),
     while a real reach shows day-to-day variation. Below a documented
     magnitude floor we refuse to invent a riverine number.
- Danger threshold: there is no free, global, keyless gauge archive, so the
  "danger discharge" is derived location-generically as
  ``DANGER_Q_MEAN_MULTIPLE x GloFAS climatological mean discharge`` for the
  same calendar date (season-aware). Discharge -> level is a deterministic
  power-law rating curve; the *absolute* level is nominal — the scoring
  uses the overshoot ratio, which cancels the curve's scale. Both are
  documented approximations carried in ``provenance``.

Design: every I/O is isolated and wrapped. Any fetch failure returns
``live=False`` so the pipeline degrades to the demo PRESET instead of
hard-failing offline. Pure helpers (rating curve, reach detection, raw_data
builder) are deterministic and unit-testable with zero network access.
"""

import asyncio
from datetime import datetime, timezone

import httpx

WEATHER_API = "https://api.open-meteo.com/v1/forecast"
FLOOD_API = "https://flood-api.open-meteo.com/v1/flood"
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"

HTTPX_TIMEOUT = 15.0

# Location-generic river parameters (documented approximations).
# Rating curve proxy: river_level_m = RATING_A * discharge_m3s ** RATING_B.
# A deterministic, monotonic conversion from GloFAS discharge to a level.
# The scoring function divides level by the (same-curve) danger level, so
# the hazard ratio = (Q/Q_danger)**RATING_B - 1 — the exponent is preserved,
# the scale constant cancels.
RATING_A = 0.50
RATING_B = 0.30

# Danger discharge = MULTIPLE x climatological mean discharge for the date.
DANGER_Q_MEAN_MULTIPLE = 2.0

# Below this climatological mean we treat the cell as having no meaningful
# river reach in GloFAS (Jaisalmer/Thar returns ~0.03 m3/s residual; even a
# modest real river in Saurashtra sits ~2 m3/s).
NO_REACH_FLOOR_M3S = 1.0

# Ring of sample offsets (deg) around the requested coordinate; the dominant
# (max climatological mean) reach wins. 0.1 deg ~ 11 km, ~2 GloFAS cells.
REACH_RING_OFFSETS = [
    (0.0, 0.0),
    (0.1, 0.0),
    (-0.1, 0.0),
    (0.0, 0.1),
    (0.0, -0.1),
]

# Curated demo locations (quick picks). They are plain coordinates now —
# per-city rating curves and danger discharges were fabricated and have been
# removed in favour of the location-generic GloFAS derivation.
CURATED_LOCATIONS = {
    "pune": {"name": "Pune", "lat": 18.5204, "lon": 73.8567},
    "kolhapur": {"name": "Kolhapur", "lat": 16.6913, "lon": 74.2447},
    "surat": {"name": "Surat", "lat": 21.1702, "lon": 72.8311},
    "kolkata": {"name": "Kolkata", "lat": 22.5726, "lon": 88.3639},
    "guwahati": {"name": "Guwahati", "lat": 26.1445, "lon": 91.7362},
}

# Backwards-compatible alias so existing imports keep working.
LIVE_LOCATIONS = CURATED_LOCATIONS


def list_live_locations() -> list[dict]:
    return [
        {"key": key, "name": loc["name"]}
        for key, loc in sorted(CURATED_LOCATIONS.items())
    ]


# ---- Geocoding ----------------------------------------------------------


def parse_geocode_response(payload: dict) -> list[dict]:
    """Normalise the Open-Meteo geocoding response into clean location dicts."""
    matches = []
    for item in payload.get("results", []):
        matches.append(
            {
                "name": item.get("name", "Unknown"),
                "lat": float(item["latitude"]),
                "lon": float(item["longitude"]),
                "elevation_m": float(item.get("elevation") or 0.0),
                "admin1": item.get("admin1"),
                "country": item.get("country"),
            }
        )
    return matches


async def geocode(query: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Free-text geocoding via the keyless Open-Meteo Geocoding API."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=HTTPX_TIMEOUT)
    try:
        resp = await client.get(
            GEOCODING_API,
            params={"name": query, "count": 5, "language": "en", "format": "json"},
        )
        resp.raise_for_status()
        return parse_geocode_response(resp.json())
    finally:
        if owns_client:
            await client.aclose()


# ---- Rating curve / derived river fields -------------------------------


def rating_curve_level_m(discharge_m3s: float, location: dict | None = None) -> float:
    """Map river discharge (m3/s) to a river level (m) via the generic curve."""
    if discharge_m3s <= 0:
        return 0.0
    return RATING_A * (discharge_m3s ** RATING_B)


def danger_discharge_m3s(q_mean_today: float) -> float:
    """Location-generic danger discharge: a multiple of the day-of-year
    climatological mean flow. Below the no-reach floor this is meaningless
    and callers must not use it (sensing returns not_applicable instead)."""
    return DANGER_Q_MEAN_MULTIPLE * max(0.0, q_mean_today)


def derive_river_fields(discharge_m3s: float, q_mean_today: float) -> dict:
    """Derive level + danger threshold (same rating curve) from discharge.

    Hazard ratio = (Q/Q_danger)^RATING_B - 1 is scale-invariant, so absolute
    level values are nominal; provenance records the derivation basis.
    """
    q_danger = danger_discharge_m3s(q_mean_today)
    return {
        "river_level_m": rating_curve_level_m(discharge_m3s),
        "river_level_danger_threshold_m": rating_curve_level_m(q_danger),
        "river_discharge_m3s": discharge_m3s,
        "river_danger_discharge_m3s": q_danger,
        "river_ratio_to_danger": (
            (discharge_m3s / q_danger) ** RATING_B - 1.0
            if q_danger > 0 else 0.0
        ),
    }


# ---- Reach detection ----------------------------------------------------


def detect_hydrology(recent: list[tuple[str, float, float]]) -> dict:
    """Decide whether the sampled GloFAS cell is a real river reach.

    ``recent`` is a list of (iso_date, discharge, climatological_mean).
    Returns {"status": "available"|"insufficient"|"not_applicable", "reason"}.

    Heuristic (documented):
    - "no reach": max climatological mean below NO_REACH_FLOOR_M3S (the cell
      is a dry residual, e.g. Thar ~0.03 m3/s), OR the discharge is pinned
      to climatology with no day-to-day variation (a static cell carries no
      river signal even if its residual exceeds the floor).
    - "insufficient": a boundary cell with climate just above the floor but a
      static signature — conservative toward "not assessed".
    - "available": a live, varying reach above the floor.
    """
    if not recent:
        return {"status": "insufficient", "reason": "no flood-API samples returned"}

    q_max_mean = max(mean for _, _, mean in recent)
    static = all(abs(discharge - mean) < 0.01 for _, discharge, mean in recent) and (
        max(discharge for _, discharge, _ in recent)
        - min(discharge for _, discharge, _ in recent)
        < 0.01
    )

    if q_max_mean < NO_REACH_FLOOR_M3S:
        return {
            "status": "not_applicable",
            "reason": (
                f"no river reach near this location (max climatological flow "
                f"{q_max_mean:.2f} m3/s below floor {NO_REACH_FLOOR_M3S:.0f} m3/s)"
            ),
        }
    if static:
        return {
            "status": "insufficient",
            "reason": "GloFAS cell is static (no river dynamics) — cannot assess river flood risk",
        }
    return {"status": "available", "reason": None}


# ---- Open-Meteo fetchers -----------------------------------------------


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


async def _fetch_reach_cell(
    lat: float, lon: float, client: httpx.AsyncClient
) -> dict:
    """Query one GloFAS cell for a recent daily discharge + climatology window."""
    resp = await client.get(
        FLOOD_API,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "river_discharge,river_discharge_mean",
            "timezone": "auto",
            "past_days": 2,
            "forecast_days": 1,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    dates = data["daily"]["time"]
    discharge = data["daily"]["river_discharge"]
    q_mean = data["daily"]["river_discharge_mean"]
    # Ocean / data-less GloFAS cells return None; drop those days so they do
    # not poison reach detection or the danger derivation.
    recent: list[tuple[str, float, float]] = []
    for date, q, mean in zip(dates, discharge, q_mean):
        if q is not None and mean is not None:
            recent.append((date, float(q), float(mean)))
    return {
        "snapped_lat": float(data["latitude"]),
        "snapped_lon": float(data["longitude"]),
        "recent": recent,
    }


async def choose_reach(
    lat: float, lon: float, client: httpx.AsyncClient | None = None
) -> dict:
    """Find the dominant GloFAS reach near (lat, lon).

    Samples the coordinate plus a ring of 0.1-degree offsets and keeps the
    cell with the maximum climatological mean discharge (the main channel).
    Returns {"recent": [...], "snapped_lat", "snapped_lon", "requested_lat",
    "requested_lon", "cell_offset_km"} or None when no sampled cell carries
    river data (e.g. mid-ocean).
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=HTTPX_TIMEOUT)
    try:
        best = None
        for dlat, dlon in REACH_RING_OFFSETS:
            cell = await _fetch_reach_cell(lat + dlat, lon + dlon, client)
            if not cell["recent"]:
                continue  # no GloFAS data at this cell (e.g. ocean)
            q_mean = max(m for _, _, m in cell["recent"])
            cell["cell_offset_km"] = _offset_km(dlat, dlon)
            if best is None or q_mean > best["q_mean_max"]:
                best = {
                    **cell,
                    "q_mean_max": q_mean,
                    "offset": (dlat, dlon),
                }
        if best is None:
            return None
        best["requested_lat"] = lat
        best["requested_lon"] = lon
        return best
    finally:
        if owns_client:
            await client.aclose()


def _offset_km(dlat: float, dlon: float) -> float:
    import math

    return round(math.hypot(dlat, dlon * 0.8) * 111.0, 1)


# ---- Raw-data builder ---------------------------------------------------


def build_raw_data(
    rainfall_mm_24h: float,
    discharge_m3s: float,
    q_mean_today: float,
) -> dict:
    """Map live telemetry into the exact raw_data shape prediction consumes
    (rainfall plus the derived level/danger fields for a valid reach)."""
    return {
        "rainfall_mm_24h": rainfall_mm_24h,
        **derive_river_fields(discharge_m3s, q_mean_today),
    }


def build_rain_only_raw_data(rainfall_mm_24h: float) -> dict:
    """raw_data for a location with no usable river reach: real rainfall only,
    no river fields so prediction cannot fabricate a riverine number."""
    return {"rainfall_mm_24h": rainfall_mm_24h}


# ---- Top-level ---------------------------------------------------------


async def probe_location(
    lat: float,
    lon: float,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Fetch rainfall + GloFAS and report hydrology availability (preview)."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=HTTPX_TIMEOUT)
    try:
        try:
            rainfall, reach = await asyncio.gather(
                fetch_rainfall_mm_24h(lat, lon, client),
                choose_reach(lat, lon, client),
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        if reach is None:
            return {
                "ok": True,
                "rainfall_mm_24h": rainfall,
                "hydrology_status": "not_applicable",
                "hydrology_reason": "no GloFAS river cells carry data near this location",
                "river_discharge_m3s": None,
                "river_q_mean_m3s": None,
                "river_danger_discharge_m3s": None,
                "reach_offset_km": None,
            }

        detection = detect_hydrology(reach["recent"])
        q_today = reach["recent"][-1][1]
        q_mean_today = reach["recent"][-1][2]
        if detection["status"] != "available":
            # Honest preview: don't hand the UI numbers we refuse to stand on.
            return {
                "ok": True,
                "rainfall_mm_24h": rainfall,
                "hydrology_status": detection["status"],
                "hydrology_reason": detection["reason"],
                "river_discharge_m3s": None,
                "river_q_mean_m3s": None,
                "river_danger_discharge_m3s": None,
                "reach_offset_km": None,
            }
        return {
            "ok": True,
            "rainfall_mm_24h": rainfall,
            "hydrology_status": detection["status"],
            "hydrology_reason": detection["reason"],
            "river_discharge_m3s": q_today,
            "river_q_mean_m3s": q_mean_today,
            "river_danger_discharge_m3s": danger_discharge_m3s(q_mean_today),
            "reach_offset_km": reach["cell_offset_km"],
            "snapped_lat": reach["snapped_lat"],
            "snapped_lon": reach["snapped_lon"],
        }
    finally:
        if owns_client:
            await client.aclose()


def _location_from_input(location: dict | str) -> dict | None:
    """Accept a resolved location dict or a curated key (backwards compat)."""
    if isinstance(location, dict):
        lat = location.get("lat")
        lon = location.get("lon")
        name = location.get("name") or "location"
        if lat is None or lon is None:
            return None
        return {
            "name": name,
            "lat": float(lat),
            "lon": float(lon),
            "elevation_m": float(location.get("elevation_m") or 0.0),
        }
    curated = CURATED_LOCATIONS.get(location)
    if curated:
        return {**curated, "elevation_m": 0.0}
    return None


async def run_live_sensing(
    location: dict | str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Top-level live-sensing call. Never raises on network failure.

    Returns {"live": True, "raw_data": {...}, "hydrology_status": ...,
    "hydrology_reason": ..., "provenance": {...}} on success, or
    {"live": False, "error": "..."} on any failure so the caller can degrade
    to the demo scenario without a hard error.

    ``hydrology_status`` is one of:
      - "available": real rainfall + derived river fields are in raw_data.
      - "insufficient": real rainfall only; no river signal (e.g. static
        cell) — riverine risk must not be presented as a number.
      - "not_applicable": no river reach at all (e.g. inland desert) —
        flood probability is not applicable, never fabricated.
    """
    resolved = _location_from_input(location)
    if not resolved:
        return {"live": False, "error": f"unknown location: {location!r}"}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=HTTPX_TIMEOUT)
    try:
        try:
            rainfall, reach = await asyncio.gather(
                fetch_rainfall_mm_24h(resolved["lat"], resolved["lon"], client),
                choose_reach(resolved["lat"], resolved["lon"], client),
            )
        except Exception as exc:
            return {"live": False, "error": f"{type(exc).__name__}: {exc}"}

        now = datetime.now(timezone.utc).isoformat()
        base_provenance = {
            "source": "open-meteo",
            "location": resolved["name"],
            "lat": resolved["lat"],
            "lon": resolved["lon"],
            "elevation_m": resolved["elevation_m"],
            "fetched_at": now,
        }

        if reach is None:
            raw_data = build_rain_only_raw_data(rainfall)
            base_provenance.update(
                {
                    "river_level_note": (
                        "NOT ASSESSED — no GloFAS river cells carry data near "
                        "this location. Flood probability is not applicable."
                    ),
                }
            )
            return {
                "live": True,
                "raw_data": raw_data,
                "hydrology_status": "not_applicable",
                "hydrology_reason": "no GloFAS river cells carry data near this location",
                "provenance": base_provenance,
            }

        base_provenance.update(
            {
                "reach_snapped_lat": reach["snapped_lat"],
                "reach_snapped_lon": reach["snapped_lon"],
                "reach_offset_km": reach["cell_offset_km"],
            }
        )
        detection = detect_hydrology(reach["recent"])
        status = detection["status"]

        if status != "available":
            raw_data = build_rain_only_raw_data(rainfall)
            base_provenance.update(
                {
                    "river_level_note": (
                        "NOT ASSESSED — no usable GloFAS river reach. "
                        "Flood probability is not applicable; no riverine "
                        "number is fabricated."
                    ),
                    "hydrology_reason": detection["reason"],
                }
            )
            return {
                "live": True,
                "raw_data": raw_data,
                "hydrology_status": status,
                "hydrology_reason": detection["reason"],
                "provenance": base_provenance,
            }

        q_today = reach["recent"][-1][1]
        q_mean_today = reach["recent"][-1][2]
        raw_data = build_raw_data(rainfall, q_today, q_mean_today)
        base_provenance.update(
            {
                "river_level_note": (
                    "derived from GloFAS discharge via rating curve; danger "
                    f"discharge = {DANGER_Q_MEAN_MULTIPLE}x climatological "
                    "mean for the date (no free gauge archive)"
                ),
                "river_discharge_m3s": q_today,
                "river_q_mean_m3s": q_mean_today,
                "river_danger_discharge_m3s": danger_discharge_m3s(q_mean_today),
            }
        )
        return {
            "live": True,
            "raw_data": raw_data,
            "hydrology_status": status,
            "hydrology_reason": None,
            "provenance": base_provenance,
        }
    finally:
        if owns_client:
            await client.aclose()