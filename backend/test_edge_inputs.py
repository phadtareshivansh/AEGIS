"""Malformed / boundary / degenerate inputs for the pure agents.

Covered here: missing hydrology keys, negative rainfall, river level exactly
at (and below zero around) the danger threshold, an inventory with zero
resources and zero-capacity shelters, and a self-conflicting claim pair.
Plus an integration test — a genuinely conflict-free scenario (rain-watch /
insufficient-hydrology path) run end-to-end against the real Postgres test DB.
"""
import asyncio

import pytest

from agents.logistics import build_logistics_plan, load_inventory
from agents.policy import evaluate_conflict, policy_agreement
from agents.prediction import build_prediction

FLOOD = {
    "rainfall_mm_24h": 250.0,
    "river_level_m": 5.2,
    "river_level_danger_threshold_m": 4.0,
}
BOUNDARY = {"rainfall_mm_24h": 120.0, "river_level_m": 4.0, "river_level_danger_threshold_m": 4.0}


def _all_probs(prediction: dict) -> list[float]:
    return [z["flood_probability_12h"] for z in prediction["at_risk_zones"]]


def test_missing_keys_raise_with_clear_message():
    with pytest.raises(ValueError) as ei:
        build_prediction({})
    for key in ("rainfall_mm_24h", "river_level_m", "river_level_danger_threshold_m"):
        assert key in str(ei.value)

    with pytest.raises(ValueError) as ei:
        build_prediction({"rainfall_mm_24h": 100})
    assert "river_level_m" in str(ei.value)


def test_endpoint_validator_catches_empty_and_partial_raw_data(server_stub):
    from testutils import post

    r = post(server_stub.base_url, "/run-scenario", {"scenario_id": "edges-empty", "raw_data": {}})
    assert r.status_code == 422
    assert "raw_data missing required keys" in r.text

    r = post(
        server_stub.base_url,
        "/run-scenario",
        {"scenario_id": "edges-partial", "raw_data": {"rainfall_mm_24h": 100}},
    )
    assert r.status_code == 422


def test_negative_rainfall_produces_sane_bounded_probabilities():
    pred = build_prediction(
        {"rainfall_mm_24h": -50.0, "river_level_m": 3.5, "river_level_danger_threshold_m": 4.0}
    )
    probs = _all_probs(pred)
    assert len(pred["at_risk_zones"]) == 5
    assert all(0.0 <= p <= 1.0 for p in probs), probs


def test_river_level_exactly_at_threshold():
    pred = build_prediction(BOUNDARY)
    probs = _all_probs(pred)
    assert len(pred["at_risk_zones"]) == 5
    assert all(0.0 <= p <= 1.0 for p in probs), probs
    assert all(z["river_overshoot"] == 0.0 for z in pred["at_risk_zones"])


def test_zero_danger_threshold_does_not_divide_by_zero():
    pred = build_prediction(
        {"rainfall_mm_24h": 100.0, "river_level_m": 1.2, "river_level_danger_threshold_m": 0.0}
    )
    probs = _all_probs(pred)
    assert all(0.0 <= p <= 1.0 for p in probs), probs


def test_zero_inventory_plans_without_crash():
    pred = build_prediction(FLOOD)
    inv = load_inventory()
    for shelter in inv["shelters"]:
        shelter["capacity"] = 0
    inv["ambulances"]["total"] = 0
    inv["water_tankers"]["total"] = 0

    plan, conflicts = build_logistics_plan(pred, inv)
    assert plan["conflicts_flagged"] == len(conflicts)
    assert all(a["ambulances"] == 0 for a in plan["allocations"])
    assert all(a["water_tankers"] == 0 for a in plan["allocations"])


def test_self_conflict_is_exact_tie_and_escalates():
    pred = build_prediction(FLOOD)
    plan, _ = build_logistics_plan(pred)
    conflict = {
        "id": "conflict_self",
        "type": "route_conflict",
        "resource": "south_bridge",
        "claim_a": {
            "purpose": "evacuation",
            "zone": plan["allocations"][0]["zone_name"],
            "justification": "evacuation of people",
        },
        "claim_b": {
            "purpose": "supply_delivery",
            "zone": plan["allocations"][0]["zone_name"],
            "justification": "medical resupply via south_bridge",
        },
    }
    rec = evaluate_conflict(conflict, plan)
    assert rec["winning_side"] is None
    assert "Escalate" in rec["decision"]
    assert policy_agreement({**rec, "winning_side": None}, rec) is True


def test_zero_conflicts_completes_through_real_postgres(loop, test_db):
    """Hydrology insufficient -> rain-watch -> logistics skips allocation
    -> negotiator finds no conflicts -> straight to briefing. Reconfirmed
    against a real migrated Postgres database."""
    from db.repo import insert_scenario, replay_scenario
    from main import _run_debate, event_queues
    from testutils import unique_sid

    sid = unique_sid("zero-conflict")

    async def run():
        raw = {
            "data_mode": "demo",
            "rainfall_mm_24h": 120.0,
            "river_level_m": 2.0,
            "river_level_danger_threshold_m": 3.0,
            "hydrology_status": "insufficient",
            "hydrology_reason": "no river reach in tests",
        }
        row = await insert_scenario(sid, raw)
        queue = asyncio.Queue()
        await _run_debate(str(row.id), sid, raw, queue)
        queued = []
        while not queue.empty():
            queued.append(queue.get_nowait())
        replay = await replay_scenario(sid)
        return row, queued, replay

    row, queued, replay = loop.run_until_complete(run())

    assert sid not in event_queues
    assert replay["conflicts"] == []
    assert replay["scenario"]["status"] == "complete"
    assert replay["scenario"]["prediction"]["flood_probability"] == "insufficient_hydrology_data"
    types = [ev["type"] for ev in replay["events"]]
    assert "scenario_complete" in types and "briefing_ready" in types
    assert not any(t == "approval_needed" for t in types)
    headline = replay["scenario"]["briefing"]["headline"]
    assert "watch" in headline.lower(), headline

    qtypes = [ev["type"] for ev in queued]
    assert "scenario_complete" in qtypes
    assert "briefing_ready" in qtypes