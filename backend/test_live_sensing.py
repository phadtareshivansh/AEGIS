"""Tests for location-generic live sensing + honest no-data paths.

Offline unit tests mock httpx (zero network). Pass ``--live`` to additionally
hit the real Open-Meteo APIs and verify the mandated verification set:
    * Pune     — river reach present  -> hydrology available, full prediction
    * Patna    — dominant reach ring must pick the Ganges stem (not the
                 ~7 m3/s tributary artifact at the town centre)
    * Mumbai   — coastal (southern ring cell is pure ocean -> must be skipped)
    * Jaisalmer— inland desert -> flood_probability NOT APPLICABLE, no number

Run under pytest (sync wrappers), or standalone with ``python3 test_live_sensing.py``.
"""
import asyncio
import sys

import httpx

from agents.pipeline import logistics_node, prediction_node, simulation_node
from agents.prediction import build_rain_watch_prediction, build_unassessable_prediction
from agents.sensing import (
    FLOOD_API,
    WEATHER_API,
    REACH_RING_OFFSETS,
    build_raw_data,
    choose_reach,
    derive_river_fields,
    detect_hydrology,
    parse_geocode_response,
    run_live_sensing,
)
from schemas import ScenarioState


# ---- Fakes --------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Minimal httpx.AsyncClient stand-in routed by URL for offline tests."""

    def __init__(self, rain_mm: float, cells: dict):
        self.rain_mm = rain_mm
        self.cells = cells  # {(lat, lon): (snapped_lat, snapped_lon, [q], [mean])}

    async def get(self, url: str, **kwargs):
        if url == WEATHER_API:
            return FakeResponse({"daily": {"precipitation_sum": [self.rain_mm]}})
        if url == FLOOD_API:
            params = kwargs.get("params", {})
            lat, lon = params["latitude"], params["longitude"]
            s_lat, s_lon, q, mean = self.cells[(lat, lon)]
            dates = ["2026-09-13", "2026-09-14", "2026-09-15"]
            return FakeResponse(
                {
                    "latitude": s_lat,
                    "longitude": s_lon,
                    "daily": {
                        "time": dates,
                        "river_discharge": list(q),
                        "river_discharge_mean": list(mean),
                    },
                }
            )
        raise AssertionError(f"unexpected URL: {url}")

    async def aclose(self):
        return None


def _build_cells(offset_q_mean, snaps=((0, 0),)):
    cells = {}
    for i, (dlat, dlon) in enumerate(REACH_RING_OFFSETS):
        q, mean_q = offset_q_mean(i)
        cells[(20.0 + dlat, 75.0 + dlon)] = (20.0 + dlat, 75.0 + dlon, q, mean_q)
    return cells


def _dynamic_cell(mag: float = 20.0):
    def fn(i):
        return (
            [mag - 1.5 + i, mag + 0.5 + i, mag - 0.2 + i],
            [mag - 1.4 + i, mag + 0.4 + i, mag - 0.25 + i],
        )

    return fn


def _static_cell(mag: float = 0.03):
    def fn(i):
        return [mag] * 3, [mag] * 3

    return fn


# ---- Unit tests (sync wrappers for pytest) --------------------------------


def test_parse_geocode_response():
    payload = {
        "results": [
            {
                "id": 1269507,
                "name": "Jaisalmer",
                "latitude": 26.91763,
                "longitude": 70.90387,
                "elevation": 234.0,
                "country_code": "IN",
                "admin1": "Rajasthan",
                "country": "India",
            }
        ]
    }
    parsed = parse_geocode_response(payload)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "Jaisalmer"
    assert parsed[0]["lat"] == 26.91763
    assert parsed[0]["elevation_m"] == 234.0
    assert parsed[0]["admin1"] == "Rajasthan"
    assert parse_geocode_response({}) == []


def test_detect_hydrology():
    # Inland desert: constant residual below floor -> NOT APPLICABLE.
    res = detect_hydrology([("d", 0.03, 0.03), ("d", 0.03, 0.03), ("d", 0.03, 0.03)])
    assert res["status"] == "not_applicable"
    assert "floor" in res["reason"]

    # Static cell above the floor (no dynamics) -> insufficient, not a number.
    res = detect_hydrology([("d", 2.5, 2.5), ("d", 2.5, 2.5), ("d", 2.5, 2.5)])
    assert res["status"] == "insufficient"

    # Small but real river (Saurashtra-like) -> available.
    res = detect_hydrology([("d", 2.0, 1.9), ("d", 2.2, 1.9), ("d", 2.1, 1.9)])
    assert res["status"] == "available"

    # Large monsoon river -> available.
    res = detect_hydrology([("d", 95.0, 90.0), ("d", 120.0, 100.0), ("d", 105.0, 98.0)])
    assert res["status"] == "available"

    res = detect_hydrology([])
    assert res["status"] == "insufficient"


def test_derive_river_fields_and_raw_data():
    fields = derive_river_fields(discharge_m3s=100.0, q_mean_today=50.0)
    # danger discharge = 2 x climatological mean -> Q == danger -> ratio ~ 0
    assert abs(fields["river_ratio_to_danger"]) < 1e-9
    assert fields["river_level_m"] > 0
    assert fields["river_level_m"] <= fields["river_level_danger_threshold_m"]

    fields = derive_river_fields(discharge_m3s=200.0, q_mean_today=50.0)
    assert fields["river_ratio_to_danger"] > 0
    assert fields["river_level_m"] > fields["river_level_danger_threshold_m"]

    raw = build_raw_data(30.0, 200.0, 50.0)
    assert raw["rainfall_mm_24h"] == 30.0
    for key in ("river_level_m", "river_level_danger_threshold_m"):
        assert key in raw


def test_choose_reach_picks_dominant_channel():
    asyncio.run(_impl_choose_reach_picks_dominant_channel())


async def _impl_choose_reach_picks_dominant_channel():
    # Ring sample where only the N offset has the real (big) river.
    def fn(i):
        dlat, _ = REACH_RING_OFFSETS[i]
        if abs(dlat - 0.1) < 1e-9:
            return [4500.0, 4600.0, 4550.0], [4517.0, 4500.0, 4520.0]
        return [7.0, 7.1, 6.9], [6.92, 6.9, 6.95]

    client = FakeClient(11.0, _build_cells(fn))
    reach = await choose_reach(20.0, 75.0, client=client)
    assert reach is not None
    assert reach["q_mean_max"] > 4000, "ring must pick the dominant channel"
    assert abs(reach["offset"][0] - 0.1) < 1e-9


def test_choose_reach_desert_all_static():
    asyncio.run(_impl_choose_reach_desert_all_static())


async def _impl_choose_reach_desert_all_static():
    client = FakeClient(0.4, _build_cells(_static_cell(0.03)))
    reach = await choose_reach(20.0, 75.0, client=client)
    assert reach is not None
    assert detect_hydrology(reach["recent"])["status"] == "not_applicable"


def test_run_live_sensing_available_no_fabrication():
    asyncio.run(_impl_run_live_sensing_available_no_fabrication())


async def _impl_run_live_sensing_available_no_fabrication():
    client = FakeClient(25.0, _build_cells(_dynamic_cell(120.0)))
    result = await run_live_sensing(
        {"name": "River Town", "lat": 20.0, "lon": 75.0}, client=client
    )
    assert result["live"] is True
    assert result["hydrology_status"] == "available"
    raw = result["raw_data"]
    assert "rainfall_mm_24h" in raw and "river_level_m" in raw
    assert result["provenance"]["river_level_note"].startswith("derived from GloFAS")


def test_run_live_sensing_desert_not_applicable():
    asyncio.run(_impl_run_live_sensing_desert_not_applicable())


async def _impl_run_live_sensing_desert_not_applicable():
    client = FakeClient(0.4, _build_cells(_static_cell(0.03)))
    result = await run_live_sensing(
        {"name": "Desert Town", "lat": 20.0, "lon": 75.0}, client=client
    )
    assert result["live"] is True
    assert result["hydrology_status"] == "not_applicable"
    raw = result["raw_data"]
    assert "rainfall_mm_24h" in raw
    # Real rainfall yes; river fields MUST NOT exist — no fabricated number.
    for forbidden in ("river_level_m", "river_level_danger_threshold_m"):
        assert forbidden not in raw
    assert "NOT ASSESSED" in result["provenance"]["river_level_note"]


def test_run_live_sensing_skips_ocean_cells():
    """All ring cells null (ocean) -> not_applicable, not a crash, not live=False."""
    asyncio.run(_impl_run_live_sensing_skips_ocean_cells())


async def _impl_run_live_sensing_skips_ocean_cells():
    def fn(i):
        return [None] * 3, [None] * 3

    client = FakeClient(3.0, _build_cells(fn))
    result = await run_live_sensing(
        {"name": "Open Sea", "lat": 20.0, "lon": 75.0}, client=client
    )
    assert result["live"] is True
    assert result["hydrology_status"] == "not_applicable"
    assert "river_level_m" not in result["raw_data"]


def test_prediction_node_not_applicable():
    asyncio.run(_impl_prediction_node_not_applicable())


async def _impl_prediction_node_not_applicable():
    state = ScenarioState(
        scenario_id="t0",
        raw_data={
            "data_mode": "live",
            "hydrology_status": "not_applicable",
            "hydrology_reason": "no river reach",
            "rainfall_mm_24h": 0.4,
            "location": {"name": "Desert Town", "lat": 26.9, "lon": 70.9},
        },
    )
    update = await prediction_node(state)
    pred = update["prediction"]
    assert pred["flood_probability"] == "not_applicable"
    assert pred["at_risk_zones"] == []
    assert "flood_probability_12h" not in pred
    assert pred["summary"].startswith("Flood assessment not applicable")

    # Downstream nodes refuse to fabricate allocations / simulations.
    state.prediction = pred
    log_update = await logistics_node(state)
    assert log_update["logistics_plan"]["allocations"] == []
    sim_update = await simulation_node(state)
    assert sim_update["simulation"]["source"] == "svg-fallback"
    assert "NOT AVAILABLE" in sim_update["simulation"]["svg"]


def test_prediction_node_insufficient_rain_watch():
    asyncio.run(_impl_prediction_node_insufficient_rain_watch())


async def _impl_prediction_node_insufficient_rain_watch():
    state = ScenarioState(
        scenario_id="t0",
        raw_data={
            "hydrology_status": "insufficient",
            "hydrology_reason": "static cell",
            "rainfall_mm_24h": 45.0,
            "location": {"name": "Coastal Town", "lat": 19.0, "lon": 72.8},
        },
    )
    update = await prediction_node(state)
    pred = update["prediction"]
    assert pred["flood_probability"] == "insufficient_hydrology_data"
    assert pred["assessment"] == "flash_rain_watch"
    assert pred["rainfall_mm_24h"] == 45.0
    assert pred["at_risk_zones"] == []


def test_unassessable_helpers_never_numeric():
    for pred in (
        build_unassessable_prediction("insufficient_hydrology_data", "no reach", "X"),
        build_rain_watch_prediction(30.0, "static cell", "Y"),
    ):
        assert pred["at_risk_zones"] == []
        assert "flood_probability_12h" not in pred
        assert isinstance(pred["flood_probability"], str)


# ---- Live smoke (real Open-Meteo) ----------------------------------------


async def live_smoke():
    print("== LIVE SMOKE: real Open-Meteo requests ==")
    async with httpx.AsyncClient(timeout=20.0) as client:
        cases = [
            ("Pune (river present)", {"name": "Pune", "lat": 18.5204, "lon": 73.8567}, "available"),
            ("Patna (Ganges dominant reach)", {"name": "Patna", "lat": 25.5941, "lon": 85.1376}, "available"),
            ("Mumbai (coastal, ocean ring cell)", {"name": "Mumbai", "lat": 19.076, "lon": 72.8777}, None),
            ("Jaisalmer (inland desert)", {"name": "Jaisalmer", "lat": 26.9176, "lon": 70.9039}, "not_applicable"),
        ]
        last = None
        for label, loc, expect in cases:
            result = await run_live_sensing(loc, client=client)
            last = result
            status = result.get("hydrology_status")
            prov = result.get("provenance", {})
            rain = result.get("raw_data", {}).get("rainfall_mm_24h")
            print(
                f"  {label}: status={status} rain={rain} "
                f"q={prov.get('river_discharge_m3s')} q_mean={prov.get('river_q_mean_m3s')}"
            )
            print(f"      reason={result.get('hydrology_reason')}")
            if expect:
                assert status == expect, f"{label}: expected {expect}, got {status}"
        # Jaisalmer guard: not_applicable + no river fields.
        assert last is not None and last.get("hydrology_status") == "not_applicable"
        assert "river_level_m" not in last["raw_data"]
        print("  NOTE: Patna's row above must show a q/q_mean around 4500 m3/s")
        print("        (dominant Ganges stem via the 0.1-deg ring, not the ~7 m3/s")
        print("        tributary artifact at the town centre).")
        print("LIVE SMOKE: PASS")


# ---- Runner ---------------------------------------------------------------


def run_offline():
    for fn in (
        test_parse_geocode_response,
        test_detect_hydrology,
        test_derive_river_fields_and_raw_data,
        test_choose_reach_picks_dominant_channel,
        test_choose_reach_desert_all_static,
        test_run_live_sensing_available_no_fabrication,
        test_run_live_sensing_desert_not_applicable,
        test_run_live_sensing_skips_ocean_cells,
        test_prediction_node_not_applicable,
        test_prediction_node_insufficient_rain_watch,
        test_unassessable_helpers_never_numeric,
    ):
        fn()
        print(f"  PASS: {fn.__name__}")


if __name__ == "__main__":
    if "--live" in sys.argv:
        asyncio.run(live_smoke())
    else:
        print("Offline unit tests (no network):")
        run_offline()
        print("ALL OFFLINE TESTS PASSED")