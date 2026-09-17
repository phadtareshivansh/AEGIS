"""Concurrency: two simultaneous scenarios must not bleed into each other's
WS feeds or Postgres rows, and a single scenario with 3+ conflicts must stream
its negotiations grouped per conflict (never interleaved)."""
import asyncio

from agents.prediction import build_prediction
from testutils import (
    approve_all,
    event_counts,
    negotiation_turn_groups,
    read_completion,
    read_debate,
    start_scenario,
    ws_url,
    unique_sid,
)

RAW_A = {"rainfall_mm_24h": 250, "river_level_m": 5.5, "river_level_danger_threshold_m": 4.0}
RAW_B = {"rainfall_mm_24h": 40, "river_level_m": 3.0, "river_level_danger_threshold_m": 4.0}


def _prob_map(pred_data: dict) -> dict:
    return {z["zone_id"]: z["flood_probability_12h"] for z in pred_data["at_risk_zones"]}


def _find_prediction(events: list[dict]) -> dict:
    for ev in events:
        if ev["type"] == "agent_result" and ev.get("agent") == "prediction":
            return ev["data"]
    raise AssertionError("no prediction agent_result in feed")


def test_two_simultaneous_scenarios_have_isolated_feeds(server_stub):
    base = server_stub.base_url
    sid_a = unique_sid("conc-a")
    sid_b = unique_sid("conc-b")
    a_id = start_scenario(base, sid_a, RAW_A)
    b_id = start_scenario(base, sid_b, RAW_B)

    async def collect():
        events_a, events_b = await asyncio.gather(
            read_debate(base, a_id), read_debate(base, b_id)
        )
        return events_a, events_b

    events_a, events_b = asyncio.run(collect())

    expected_a = _prob_map(build_prediction(RAW_A))
    expected_b = _prob_map(build_prediction(RAW_B))
    assert expected_a != expected_b, "test inputs must yield distinguishable predictions"

    got_a = _prob_map(_find_prediction(events_a))
    got_b = _prob_map(_find_prediction(events_b))
    assert got_a == expected_a, got_a
    assert got_b == expected_b, got_b
    assert got_a != got_b, "feed A's prediction leaked into feed B (or vice versa)"

    # No A-flavored numbers anywhere in B's feed, and vice versa.
    flat_b = " ".join(str(ev) for ev in events_b)
    flat_a = " ".join(str(ev) for ev in events_a)
    for v in expected_a.values():
        assert f"{v}" not in flat_b, f"feed B contains A's probability {v}"
    for v in expected_b.values():
        assert f"{v}" not in flat_a, f"feed A contains B's probability {v}"

    # Both queues reach exactly their own 3 approvals.
    assert event_counts(events_a)["approval_needed"] == 3
    assert event_counts(events_b)["approval_needed"] == 3


def test_two_scenarios_persist_disjoint_predictions_in_postgres(server_stub):
    base = server_stub.base_url
    sid_a = unique_sid("seed-a")
    sid_b = unique_sid("seed-b")
    a_id = start_scenario(base, sid_a, RAW_A)
    b_id = start_scenario(base, sid_b, RAW_B)

    async def collect():
        return await asyncio.gather(read_debate(base, a_id), read_debate(base, b_id))

    a_events, b_events = asyncio.run(collect())

    a_p = get_replay_prediction(base, a_id)
    b_p = get_replay_prediction(base, b_id)
    assert _prob_map(a_p) == _prob_map(build_prediction(RAW_A))
    assert _prob_map(b_p) == _prob_map(build_prediction(RAW_B))
    assert _prob_map(a_p) != _prob_map(b_p)


def test_three_conflicts_stream_grouped_without_interleaving(server_stub):
    base = server_stub.base_url
    sid = unique_sid("ordered")
    scenario_id = start_scenario(base, sid, RAW_A)

    events = asyncio.run(read_debate(base, scenario_id))
    groups = negotiation_turn_groups(events)
    assert list(groups) == ["conflict_1", "conflict_2", "conflict_3"], list(groups)
    for cid, group in groups.items():
        turns = [ev["data"]["turn"] for ev in group]
        assert turns == sorted(turns), f"{cid} had out-of-order turn chunks: {turns}"
        assert sorted(set(turns)) == [1, 2, 3, 4], f"{cid} missing turns: {turns}"

    approvals = [
        ev
        for ev in events
        if ev["type"] == "approval_needed"
    ]
    assert [ev["data"]["conflict_id"] for ev in approvals] == [
        "conflict_1",
        "conflict_2",
        "conflict_3",
    ]

    assert "scenario_complete" not in event_counts(events)
    assert "briefing_ready" not in event_counts(events)

    approve_all(base, scenario_id)
    completion = asyncio.run(read_completion(base, scenario_id))
    total = event_counts(events + completion)
    assert total["scenario_complete"] >= 1
    assert total["briefing_ready"] >= 1


def get_replay_prediction(base_url: str, sid: str) -> dict:
    import time

    from testutils import get

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        r = get(base_url, f"/scenarios/{sid}")
        assert r.status_code == 200, r.text
        pred = r.json()["scenario"]["prediction"]
        if pred:
            return pred
        time.sleep(0.2)
    raise AssertionError(f"scenario {sid} never persisted a prediction")