#!/usr/bin/env python3
"""Empirical backtest of the AEGIS flood-risk scoring function.

This runs the *actual* scoring artifact in ``agents/prediction.py`` against a
real, public historical flood dataset and reports how well its predicted
``flood_probability_12h`` ranking matches reported outcomes.

Datasets used
-------------
1. Bangladesh weather-station flood dataset (primary, quantitative)
   - Source: https://github.com/n-gauhar/Flood-prediction
     (Gauhar, N., Das, S., Moury, K.S. et al., "Prediction of Flood in
     Bangladesh using k-Nearest Neighbors Algorithm", 2021)
   - 33 real climate stations, monthly rows 1948-2013, fields incl. real
     rainfall (mm) and ``Flood?`` (1 = floods reported that month; blank/0 =
     none). Station elevation (ALT), lat/lon are real.
   - Raw file vendored at data/bangladesh_stations.csv.

2. India Flood Inventory - Impacts, v4 (secondary, Kerala 2018 case)
   - HydroSense Lab IIT Delhi x IMD, Zenodo 10.5281/zenodo.16994648
     (Saharia et al., Nat. Hazards 2021; Saharia et al. 2025)
   - District-level affected lists for real IMD-sourced flood events 1967-2023.
     The August 2018 Kerala flood is events UEI-IMD-FL-2018-0035/0036
     (all 14 districts affected).
   - District-wise realised rainfall for Kerala (1 Jun - 22 Aug 2018) from the
     official CWC "Study Report: Kerala Floods of August 2018" (Table-3, IMD
     records). Values embedded below with exact figures from that report.

Explicit, documented modelling choices (see RESULTS.md for the discussion)
-------------------------------------------------------------------------
- ``distance_to_river_km`` is not in either source; it is derived as the
  haversine distance from each station/district to the nearest point on
  embedded, hand-traced main-river centerlines (approximate; a relative
  proximity feature only).
- ``population`` uses district/division-level census figures (documented
  source per table) assigned to stations/districts.
- ``historical_flood_risk`` is empirical: fraction of flood-flagged months at
  that zone in ALL years BEFORE the validation event (no leakage).
- The model's dynamic inputs are *spatially uniform* in the shipped artifact
  (score_zone/rank_zones apply one rainfall and one river level to every zone).
  An honest consequence, measured below, is that within a single event the
  ranking is driven only by terrain + history and the dynamic factors only
  shift all probabilities together.
- river level / danger threshold: no free bulk gauge archive covers the
  1948-2013 window (FFWC exposes ~40-day rolling windows; the only public
  FFWC-derived archive is a July-2026 snapshot). We therefore use a documented
  proxy: stage index = percentile of event rainfall in each zone's own
  climatology, with the proxy danger level at the 90th percentile. Only the
  overshoot ratio matters to the model (it divides by the threshold), so the
  absolute gauge datum is arbitrary.
- real inputs only where they exist. Any assumption is stated here or in the
  output -- this is a validation, not an endorsement.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RESULTS_DIR = DATA_DIR / "results"
BACKEND_DIR = ROOT.parent  # backend/

sys.path.insert(0, str(BACKEND_DIR))

from agents import prediction as MODEL  # the artifact under test (import only)

# ---------------------------------------------------------------------------
# Sources / URLs (used only if a raw file is missing from data/)
# ---------------------------------------------------------------------------
BANGLADESH_CSV_URL = (
    "https://raw.githubusercontent.com/n-gauhar/Flood-prediction/master/FloodPrediction.csv"
)
IFI_CSV_URL = (
    "https://zenodo.org/records/16994648/files/India_Flood_Inventory_v3.csv?download=1"
)


#: Expected leading header cells of each raw dataset. A downloaded file must
#: match these and contain at least one data row or it is rejected loudly (an
#: HTML error page, a truncated body, or an empty dataset must never survive on
#: disk as if it were valid). The IFI file begins with a UTF-8 BOM; ``strip``
#: on both sides makes the comparison BOM-safe, and matching starts at ``UEI``
#: to ignore the leading ``Unnamed: 0`` index column.
BANGLADESH_EXPECTED_HEADER = ("Sl", "Station_Names", "Year", "Month")
IFI_EXPECTED_HEADER = ("UEI", "Start Date", "End Date")


def ensure_data() -> None:
    """Make sure vendored raw data exists; download if not (offline after first cache)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _fetch_if_missing(DATA_DIR / "bangladesh_stations.csv", BANGLADESH_CSV_URL, BANGLADESH_EXPECTED_HEADER)
    _fetch_if_missing(DATA_DIR / "india_flood_inventory_v3.csv", IFI_CSV_URL, IFI_EXPECTED_HEADER)


def _fetch_if_missing(path: Path, url: str, expected_header: tuple[str, ...]) -> None:
    import urllib.request

    if path.exists() and path.stat().st_size > 0:
        return
    print(f"  downloading {url.split('/')[-1]}")
    try:
        urllib.request.urlretrieve(url, path)
    except Exception as exc:
        raise RuntimeError(
            "expected dataset not found at " + str(path) +
            " and could not be downloaded from " + url +
            "; see backend/validation/DATA_SOURCE.md or trust RESULTS.md"
        ) from exc
    _validate_download(path, expected_header)


def _validate_download(path: Path, expected_header: tuple[str, ...]) -> None:
    """Reject a download that is not a usable CSV.

    Checks, on the file we just wrote: the leading header cells match the
    expected header AND there is at least one data row. Any failure unlinks the
    bad file BEFORE raising, so a later run cannot silently treat the corrupt
    download as valid data.
    """
    header = None
    data_row = None
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            data_row = next(reader, None)
    except OSError:
        header = data_row = None
    ok = (
        header is not None
        and len(header) >= len(expected_header)
        and all(
            str(h).strip() == str(e).strip()
            for h, e in zip(header, expected_header)
        )
        and data_row is not None
    )
    if not ok:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            "downloaded dataset " + path.name +
            " failed validation (expected header " + repr(expected_header) +
            " plus at least one data row). The fetch likely returned an HTML "
            "error page, a truncated body, or an empty dataset. Remove " +
            str(path) + " and re-run with a working connection, or see "
            "backend/validation/DATA_SOURCE.md / trust RESULTS.md"
        )


# ---------------------------------------------------------------------------
# River geometry (approximate centerlines, hand-traced from public river maps)
# Order: (lon, lat). Used only to approximate a relative "distance to river".
# ---------------------------------------------------------------------------
RIVERS = {
    "Padma_Ganges": [
        (88.29, 24.68), (88.60, 24.50), (88.90, 24.35), (89.15, 24.10),
        (89.40, 23.95), (89.60, 23.80), (90.00, 23.60), (90.30, 23.55),
        (90.55, 23.40), (90.72, 23.25),
    ],
    "Jamuna_Brahmaputra": [
        (89.60, 25.85), (89.70, 25.40), (89.80, 25.10), (89.70, 24.85),
        (89.65, 24.60), (89.75, 24.45), (89.80, 24.20), (89.85, 24.00),
        (89.90, 23.88),
    ],
    "Old_Brahmaputra": [
        (89.72, 24.90), (90.15, 24.80), (90.40, 24.72), (90.50, 24.40),
        (90.55, 24.10), (90.60, 23.95),
    ],
    "Surma_UpperMeghna": [
        (91.85, 24.90), (91.55, 24.78), (91.25, 24.68), (91.00, 24.55),
        (90.95, 24.40), (90.80, 24.30), (90.85, 24.25),
    ],
    "Meghna_main": [
        (90.85, 24.25), (90.75, 23.90), (90.70, 23.60), (90.70, 23.30),
        (90.62, 23.05),
    ],
    "Lower_Meghna": [
        (90.62, 23.05), (90.62, 22.80), (90.55, 22.50), (90.45, 22.20),
        (90.55, 21.90),
    ],
    "Arial_Khan": [
        (90.35, 23.50), (90.25, 23.30), (90.20, 23.05), (90.10, 22.85),
        (90.00, 22.70),
    ],
    "Gorai_Madhumati": [
        (89.45, 23.95), (89.42, 23.70), (89.38, 23.45), (89.32, 23.15),
        (89.22, 22.85), (89.12, 22.70),
    ],
    "Karnaphuli": [
        (92.25, 22.70), (92.10, 22.55), (91.95, 22.45), (91.85, 22.40),
    ],
}
EARTH_RADIUS_KM = 6371.0
STAGE_REFERENCE_M = 10.0  # arbitrary datum; model divides by threshold so ratio only
PCTL_DANGER = 90.0  # proxy danger level = 90th percentile rainfall


def _haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _point_to_segment_km(p, a, b):
    """Distance from point to segment using a local equirectangular projection."""
    y0, x0 = p
    y1, x1 = a
    y2, x2 = b
    cosy = math.cos(math.radians(y0)) or 1e-9
    px, py = x0 * cosy, y0
    ax, ay = x1 * cosy, y1
    bx, by = x2 * cosy, y2
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else ((px - ax) * dx + (py - ay) * dy) / seg2
    t = max(0.0, min(1.0, t))
    proj = (ay + t * dy) / cosy, ax + t * dx  # (lat, lon) approx
    proj = (proj[0], proj[1] / (cosy or 1e-9))
    return _haversine_km(p, proj)


def distance_to_rivers_km(lat: float, lon: float) -> float:
    best = float("inf")
    for pts in RIVERS.values():
        for a, b in zip(pts, pts[1:]):
            d = _point_to_segment_km((lat, lon), (a[1], a[0]), (b[1], b[0]))
            if d < best:
                best = d
    return best if math.isfinite(best) else float("nan")


# ---------------------------------------------------------------------------
# Bangladesh static feature tables (real, sourced)
# ---------------------------------------------------------------------------
# Station -> district, and district population (approx., Bangladesh Bureau of
# Statistics, Population & Housing Census 2011).
STATION_DISTRICT = {
    "Barisal": "Barisal", "Bhola": "Bhola", "Bogra": "Bogra",
    "Chandpur": "Chandpur", "Chittagong (City-Ambagan)": "Chittagong",
    "Chittagong (IAP-Patenga)": "Chittagong", "Comilla": "Comilla",
    "Cox's Bazar": "Cox's Bazar", "Dhaka": "Dhaka", "Dinajpur": "Dinajpur",
    "Faridpur": "Faridpur", "Feni": "Feni", "Hatiya": "Noakhali",
    "Ishurdi": "Pabna", "Jessore": "Jessore",
    "Khepupara": "Patuakhali", "Khulna": "Khulna",
    "Kutubdia": "Cox's Bazar", "Madaripur": "Madaripur",
    "Maijdee Court": "Noakhali", "Mongla": "Bagerhat",
    "Mymensingh": "Mymensingh", "Patuakhali": "Patuakhali",
    "Rajshahi": "Rajshahi", "Rangamati": "Rangamati", "Rangpur": "Rangpur",
    "Sandwip": "Chittagong", "Satkhira": "Satkhira",
    "Sitakunda": "Chittagong", "Srimangal": "Moulvibazar",
    "Sylhet": "Sylhet", "Tangail": "Tangail", "Teknaf": "Cox's Bazar",
}

CENSUS_2011_POP = {
    "Barisal": 2324310, "Bhola": 1776795, "Bogra": 3400874, "Chandpur": 2416101,
    "Chittagong": 7613352, "Comilla": 5391147, "Cox's Bazar": 2291558,
    "Dhaka": 12043977, "Dinajpur": 2990128, "Faridpur": 1912969, "Feni": 1442371,
    "Noakhali": 3108083, "Pabna": 2523179, "Jessore": 2764547,
    "Patuakhali": 1535854, "Khulna": 2318527, "Madaripur": 1165442,
    "Bagerhat": 1481390, "Mymensingh": 5110606, "Rajshahi": 2596233,
    "Rangamati": 595999, "Rangpur": 2881808, "Satkhira": 1985959,
    "Moulvibazar": 1919495, "Sylhet": 3434358, "Tangail": 3605516,
}

# ---------------------------------------------------------------------------
# Validation events: (year, month) with real labels and spread of impact.
# ---------------------------------------------------------------------------
DEFAULT_EVENTS = [
    ("1998", "7"),   # Great Flood of 1998, 29/33 stations flooded
    ("2004", "9"),   # 2004 flood, 28 stations
    ("2011", "8"),   # 30 stations
    ("1997", "6"),   # moderate
    ("2005", "8"),   # moderate-high, 19 stations
    ("1956", "6"),   # all-out, older/partial coverage
    ("2009", "10"),  # control: 1 station flooded (dry season)
]


def load_bangladesh_rows():
    rows = []
    with (DATA_DIR / "bangladesh_stations.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            rain = r["Rainfall"].strip()
            if not rain:
                continue
            rows.append(
                {
                    "station": r["Station_Names"].strip(),
                    "year": str(r["Year"]),
                    "month": str(r["Month"]),
                    "rain_mm": float(rain),
                    "flood": 1 if r["Flood?"].strip() == "1" else 0,
                    "alt_m": float(r["ALT"]),
                    "lat": float(r["LATITUDE"]),
                    "lon": float(r["LONGITUDE"]),
                }
            )
    return rows


def build_bd_zones(rows, event_year: str):
    """Build one zone dict per station, with pre-event historical risk (no leakage)."""
    stations = sorted({r["station"] for r in rows})
    zones = []
    for st in stations:
        st_rows = [r for r in rows if r["station"] == st]
        lat, lon, alt = st_rows[0]["lat"], st_rows[0]["lon"], st_rows[0]["alt_m"]
        hist_rows = [r for r in st_rows if r["year"] < event_year]
        hist_risk = (
            sum(1 for r in hist_rows if r["flood"]) / len(hist_rows)
            if hist_rows
            else 0.0
        )
        zones.append(
            {
                "zone_id": st,
                "zone_name": st,
                "elevation_m": alt,
                "distance_to_river_km": distance_to_rivers_km(lat, lon),
                "population": CENSUS_2011_POP[STATION_DISTRICT[st]],
                "historical_flood_risk": hist_risk,
                "lat": lat,
                "lon": lon,
            }
        )
    return zones


def _percentile_rank(value: float, sample) -> float:
    if not sample:
        return 50.0
    return 100.0 * sum(1 for x in sample if x < value) / len(sample)


def stage_from_rainfall(rain_mm, climatology_mm):
    """Documented proxy: map rainfall into a river 'overshoot' ratio.

    Danger level sits at the 90th percentile of the zone's (regional)
    rainfall climatology; stage grows linearly past danger with percentile.
    """
    pctl = _percentile_rank(rain_mm, climatology_mm)
    overshoot = max(0.0, (pctl - PCTL_DANGER) / (100.0 - PCTL_DANGER))
    return STAGE_REFERENCE_M * (1.0 + overshoot), STAGE_REFERENCE_M


def regional_climatology(rows, event_year: str):
    """Per-month median rainfall across stations, all years before event."""
    months = {}
    for r in rows:
        if r["year"] < event_year:
            months.setdefault((r["year"], r["month"]), []).append(r["rain_mm"])
    return [statistics.median(v) for v in months.values() if v]


def station_climatologies(rows, event_year: str):
    out = {}
    for r in rows:
        if r["year"] < event_year:
            out.setdefault(r["station"], []).append(r["rain_mm"])
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def spearman(xs, ys):
    def rankdata(v):
        idx = sorted(range(len(v)), key=lambda i: v[i])
        ranks = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[idx[k]] = avg
            i = j + 1
        return ranks

    rx, ry = rankdata(list(xs)), rankdata(list(ys))
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (sx * sy) if sx and sy else float("nan")


def evaluate_ranking(ranked, flooded_stations):
    """ranked: list scored zones desc by flood_probability_12h."""
    top5 = ranked[:5]
    top_ids = {z["zone_name"] for z in top5}
    hits = top_ids & flooded_stations
    precision5 = len(hits) / 5
    recall5 = len(hits) / len(flooded_stations) if flooded_stations else float("nan")
    preds = [z["flood_probability_12h"] for z in ranked]
    labels = [1 if z["zone_name"] in flooded_stations else 0 for z in ranked]
    return {
        "precision@5": round(precision5, 3),
        "recall@5": round(recall5, 3),
        "spearman": round(spearman(preds, labels), 3),
        "hits": sorted(hits),
        "misses": sorted(flooded_stations - top_ids),
        "false_alarms": sorted(top_ids - flooded_stations),
        "top5": [z["zone_name"] for z in top5],
    }


def score_event_bd(rows, zones, year, month, spatial=False):
    ev = [r for r in rows if r["year"] == year and r["month"] == month]
    ev_by_station = {r["station"]: r for r in ev}
    present = [r for r in ev]
    zone_subset = [z for z in zones if z["zone_name"] in ev_by_station]
    flooded = {r["station"] for r in present if r["flood"]}

    if not spatial:
        regional_rain = statistics.median([r["rain_mm"] for r in present])
        climatology = regional_climatology(rows, year)
        level, danger = stage_from_rainfall(regional_rain, climatology)
        ranked = MODEL.rank_zones(zone_subset, regional_rain, level, danger)
    else:
        climo = station_climatologies(rows, year)
        ranked = []
        for z in zone_subset:
            r_ev = ev_by_station[z["zone_name"]]
            rain = r_ev["rain_mm"]
            level, danger = stage_from_rainfall(rain, climo.get(z["zone_name"], []))
            ranked.append(MODEL.score_zone(z, rain, level, danger))
        ranked.sort(key=lambda z: z["flood_probability_12h"], reverse=True)

    out = evaluate_ranking(ranked, flooded)
    out["year"] = year
    out["month"] = month
    out["n_stations"] = len(zone_subset)
    out["n_flooded"] = len(flooded)
    out["predicted"] = [
        {"zone": z["zone_name"], "p12": round(z["flood_probability_12h"], 3)}
        for z in ranked
    ]
    return out


# ---------------------------------------------------------------------------
# Kerala 2018 secondary case
# ---------------------------------------------------------------------------
# District-wise realised rainfall (mm), 1 Jun - 22 Aug 2018, IMD records as
# compiled in CWC "Study Report: Kerala Floods of August 2018" (Table-3).
KERALA_2018_RAIN = {
    "Alappuzha": 1784.0, "Kannur": 2573.3, "Ernakulam": 2477.8,
    "Idukki": 3555.5, "Kasaragod": 2287.1, "Kollam": 1579.3,
    "Kottayam": 2307.0, "Kozhikode": 2898.0, "Malappuram": 2637.2,
    "Palakkad": 2285.6, "Pathanamthitta": 1968.0, "Thrissur": 2077.6,
    "Thiruvananthapuram": 966.7, "Wayanad": 2884.5,
}

# Approximate average district terrain elevation (m) and distance from the
# district HQ / centroid to its main river or backwater system (km). These are
# documented approximations from district geography (terrain stats / maps) --
# see RESULTS.md limitation notes. Population: Census of India 2011 (real).
KERALA_STATIC = {
    "Alappuzha":          {"elevation_m": 1.0,  "distance_to_river_km": 2.0,  "population": 2127789},
    "Kannur":             {"elevation_m": 10.0, "distance_to_river_km": 3.0,  "population": 2523003},
    "Ernakulam":          {"elevation_m": 4.0,  "distance_to_river_km": 2.0,  "population": 3282388},
    "Idukki":             {"elevation_m": 1050.0,"distance_to_river_km": 6.0, "population": 1108974},
    "Kasaragod":          {"elevation_m": 4.0,  "distance_to_river_km": 2.0,  "population": 1307375},
    "Kollam":             {"elevation_m": 20.0, "distance_to_river_km": 3.0,  "population": 2635375},
    "Kottayam":           {"elevation_m": 25.0, "distance_to_river_km": 3.0,  "population": 1974551},
    "Kozhikode":          {"elevation_m": 9.0,  "distance_to_river_km": 4.0,  "population": 3086293},
    "Malappuram":         {"elevation_m": 20.0, "distance_to_river_km": 8.0,  "population": 4112920},
    "Palakkad":           {"elevation_m": 80.0, "distance_to_river_km": 12.0, "population": 2809934},
    "Pathanamthitta":     {"elevation_m": 35.0, "distance_to_river_km": 7.0,  "population": 1197412},
    "Thrissur":           {"elevation_m": 10.0, "distance_to_river_km": 10.0, "population": 3121200},
    "Thiruvananthapuram": {"elevation_m": 30.0, "distance_to_river_km": 4.0,  "population": 3301427},
    "Wayanad":            {"elevation_m": 750.0,"distance_to_river_km": 12.0, "population": 817420},
}


def kerala_historical_risk(inventory_path) -> dict[str, float]:
    """Fraction of IFI flood events (1967-2017) listing each Kerala district.

    Real, IMD-sourced incidence frequency used as the pre-2018 historical risk.
    """
    year_span = 51  # 1967-2017 inclusive
    hits = {d: 0 for d in KERALA_STATIC}
    with inventory_path.open(newline="") as f:
        for row in csv.DictReader(f):
            dist = row.get("Districts") or ""
            state = row.get("State") or ""
            start = row.get("Start Date") or ""
            if "Kerala" not in state:
                continue
            try:
                year = int(start.split("-")[-1][:4])
            except (ValueError, IndexError):
                continue
            if year >= 2018 or year < 1967:
                continue
            parts = {p.strip() for p in dist.split(",") if p.strip()}
            for d in KERALA_STATIC:
                if d in parts:
                    hits[d] += 1
    n_events_span = max(1, year_span)
    return {d: min(1.0, c / n_events_span) for d, c in hits.items()}


def score_kerala(inventory_path, affected_all_14=True):
    hist = kerala_historical_risk(inventory_path)
    districts = list(KERALA_STATIC)
    zones = []
    for d in districts:
        zones.append(
            {
                "zone_id": d,
                "zone_name": d,
                **KERALA_STATIC[d],
                "historical_flood_risk": hist[d],
            }
        )
    # Dynamic inputs: district-specific rainfall (spatial, CWC/IMD).
    # The shipped scoring function accepts one rainfall per rank call; Kerala
    # forcing clearly varies by district, so we score each district directly
    # with its own rainfall + a stage proxy based on its rainfall percentile
    # among the 14 districts.
    rains = [KERALA_2018_RAIN[d] for d in districts]
    ranked = []
    for z in zones:
        rain = KERALA_2018_RAIN[z["zone_name"]]
        pctl = _percentile_rank(rain, rains)
        overshoot = max(0.0, (pctl - PCTL_DANGER) / (100.0 - PCTL_DANGER))
        level = STAGE_REFERENCE_M * (1.0 + overshoot)
        ranked.append(MODEL.score_zone(z, rain, level, STAGE_REFERENCE_M))
    ranked.sort(key=lambda z: z["flood_probability_12h"], reverse=True)

    flooded = set(districts) if affected_all_14 else set(districts) - {"Kasaragod"}
    out = evaluate_ranking(ranked, flooded)
    out["case"] = "Kerala Aug-2018 (IFI all-14 affected; CWC notes 13/14 w/ Kasaragod mild)"
    out["hist_freq"] = hist
    out["rain_mm"] = KERALA_2018_RAIN
    out["ranked"] = [
        {
            "district": z["zone_name"],
            "p12": round(z["flood_probability_12h"], 3),
            "rain_mm": KERALA_2018_RAIN[z["zone_name"]],
            "hist": round(z["historical_flood_risk"], 3),
            "elev_m": z["elevation_m"],
            "dist_km": z["distance_to_river_km"],
        }
        for z in ranked
    ]
    return out


# ---------------------------------------------------------------------------
# Tuning: grid-search weights against the SAME events (in-sample; flagged).
# ---------------------------------------------------------------------------
def grid_search(rows, zones_by_event, events):
    blend_options = [0.30, 0.40, 0.50, 0.20]
    prox_options = [0.15, 0.25, 0.35]
    results = []
    default = {
        "blend_hist": 0.30,
        "w_prox": 0.25,
        "w_elev": 0.20,
        "w_river": 0.35,
        "w_rain": 0.20,
    }
    for bh in blend_options:
        for wp in prox_options:
            we = 0.45 - wp  # keep dynamic sum = 1 with default river+rainfall fixed
            w_river, w_rain = 0.35, 0.20
            MODEL.HISTORICAL_WEIGHT = bh
            MODEL.DYNAMIC_WEIGHT = 1.0 - bh
            MODEL.FACTOR_WEIGHTS = {
                "river": w_river,
                "proximity": wp,
                "elevation": we,
                "rainfall": w_rain,
            }
            ev_metrics = []
            for (y, m) in events:
                ev_metrics.append(score_event_bd(rows, zones_by_event[y], y, m))
            results.append(
                {
                    "blend_hist": bh,
                    "w_prox": wp,
                    "w_elev": round(we, 2),
                    "mean_precision@5": round(
                        sum(e["precision@5"] for e in ev_metrics) / len(ev_metrics), 3
                    ),
                    "mean_recall@5": round(
                        sum(e["recall@5"] for e in ev_metrics) / len(ev_metrics), 3
                    ),
                    "mean_spearman": round(
                        sum(e["spearman"] for e in ev_metrics) / len(ev_metrics), 3
                    ),
                }
            )
    _restore_default_weights(default)
    results.sort(key=lambda r: (r["mean_precision@5"], r["mean_recall@5"]), reverse=True)
    return default, results


def _restore_default_weights(default):
    MODEL.HISTORICAL_WEIGHT = default["blend_hist"]
    MODEL.DYNAMIC_WEIGHT = 1.0 - default["blend_hist"]
    MODEL.FACTOR_WEIGHTS = {
        "river": default["w_river"],
        "proximity": default["w_prox"],
        "elevation": default["w_elev"],
        "rainfall": default["w_rain"],
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _fmt_table(rows):
    w = 12
    header = ("event", "n", "flooded", "precision@5", "recall@5", "spearman")
    lines = ["  " + "".join(str(h).ljust(w) for h in header)]
    for r in rows:
        lines.append(
            "  "
            + f"{r['year']}-{r['month']}".ljust(w)
            + str(r["n_stations"]).ljust(w)
            + str(r["n_flooded"]).ljust(w)
            + f"{r['precision@5']}".ljust(w)
            + f"{r['recall@5']}".ljust(w)
            + f"{r['spearman']}".ljust(w)
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--events",
        nargs="+",
        default=None,
        help="space-separated year.month validation events (default: built-in set)",
    )
    ap.add_argument("--spatial", action="store_true",
                    help="also run per-zone (spatial) rainfall/stage variant")
    ap.add_argument("--tune", action="store_true",
                    help="grid-search weights (in-sample; for diagnostics only)")
    ap.add_argument("--no-kerala", action="store_true", help="skip Kerala case")
    args = ap.parse_args()

    print("== AEGIS flood-scoring backtest ==")
    print("Setting up data ...")
    ensure_data()
    rows = load_bangladesh_rows()
    print(f"  Bangladesh rows: {len(rows)}")

    events = []
    if args.events:
        for e in args.events:
            y, m = e.split(".")
            events.append((y, m))
    else:
        events = DEFAULT_EVENTS

    # build zone set per event year (historical risk must not peek ahead)
    zones_by_year = {}
    for (y, m) in events:
        zones_by_year.setdefault(y, build_bd_zones(rows, y))

    print("\n-- Bangladesh events: default weights (shipped artifact) --")
    per_event = [score_event_bd(rows, zones_by_year[y], y, m) for (y, m) in events]
    print(_fmt_table(per_event))
    for r in per_event:
        print(f"\n  {r['year']}-{r['month']}: "
              f"hits={r['hits']} misses={r['misses']} "
              f"false_alarms={r['false_alarms']}")
        print(f"    top5={r['top5']}")

    agg = {
        "mean_precision@5": round(
            statistics.mean(e["precision@5"] for e in per_event), 3),
        "mean_recall@5": round(
            statistics.mean(e["recall@5"] for e in per_event), 3),
        "mean_spearman": round(
            statistics.mean(e["spearman"] for e in per_event), 3),
    }
    print("\n  AGGREGATE (default weights):")
    print(f"    mean precision@5={agg['mean_precision@5']}  "
          f"mean recall@5={agg['mean_recall@5']}  "
          f"mean spearman={agg['mean_spearman']}")

    report = {
        "default_weights": {
            "factor_weights": dict(MODEL.FACTOR_WEIGHTS),
            "historical_weight": MODEL.HISTORICAL_WEIGHT,
            "dynamic_weight": MODEL.DYNAMIC_WEIGHT,
        },
        "events": per_event,
        "aggregate": agg,
        "assumptions": {
            "rainfall_input_is_monthly_or_cumulative": True,
            "stage_proxy": "90th-percentile-rainfall == danger; overshoot linear in percentile",
            "distance_to_river": "haversine to approximate river centerlines (hand-traced)",
            "historical_risk": "pre-event flood-month frequency at that zone",
            "spatial_forcing": "shipped rank_zones applies one rainfall/stage to all zones",
        },
    }

    if args.spatial:
        print("\n-- Bangladesh events: per-zone (spatial) rainfall/stage variant --")
        spatial_events = [
            score_event_bd(rows, zones_by_year[y], y, m, spatial=True)
            for (y, m) in events
        ]
        print(_fmt_table(spatial_events))
        report["spatial_variant_events"] = spatial_events
        report["spatial_aggregate"] = {
            "mean_precision@5": round(
                statistics.mean(e["precision@5"] for e in spatial_events), 3),
            "mean_recall@5": round(
                statistics.mean(e["recall@5"] for e in spatial_events), 3),
            "mean_spearman": round(
                statistics.mean(e["spearman"] for e in spatial_events), 3),
        }

    if args.tune:
        print("\n-- Weight grid search (IN-SAMPLE, not a held-out estimate) --")
        default, grid = grid_search(rows, zones_by_year, events)
        print(f"  default: {default}")
        print("  top-5 configs by mean precision@5:")
        for g in grid[:5]:
            print(
                f"    blend_hist={g['blend_hist']} w_prox={g['w_prox']} "
                f"w_elev={g['w_elev']} -> p@5={g['mean_precision@5']} "
                f"r@5={g['mean_recall@5']} rho={g['mean_spearman']}"
            )
        report["grid_search"] = {"default": default, "results": grid[:10]}

    if not args.no_kerala:
        print("\n-- Kerala Aug-2018 case (secondary) --")
        inv_path = DATA_DIR / "india_flood_inventory_v3.csv"
        kr = score_kerala(inv_path)
        print(f"  affected = all 14 districts (IFI); CWC notes 13/14, "
              f"Kasaragod mild.")
        print(f"  precision@5={kr['precision@5']} recall@5={kr['recall@5']} "
              f"spearman={kr['spearman']}")
        print(f"  top5={kr['top5']} misses={kr['misses']} "
              f"false_alarms={kr['false_alarms']}")
        print("  ranked:")
        for rd in kr["ranked"]:
            print(
                f"    {rd['district']:18s} p12={rd['p12']:.3f} "
                f"rain={rd['rain_mm']:7.1f} hist={rd['hist']:.3f} "
                f"elev={rd['elev_m']:7.1f} dist={rd['dist_km']:5.1f}"
            )
        report["kerala_2018"] = kr

    out_path = RESULTS_DIR / "backtest.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nWrote results to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())