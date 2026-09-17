"""Failure injection: an unreachable LLM provider mid-debate must degrade to
the neutral resolution (still policy-checked, still parked for human approval),
stream AND persist the failure events, and finish the scenario after approvals
— never hang a websocket."""
import asyncio

import pytest

from testutils import (
    approve_all,
    event_counts,
    get,
    read_completion,
    read_debate,
    start_scenario,
    unique_sid,
)

RAW = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}


@pytest.fixture(scope="session")
def dead_llm_base(server_dead_llm):
    return server_dead_llm


def test_dead_llm_provider_falls_back_without_hanging(dead_llm_base):
    base = dead_llm_base.base_url
    sid = unique_sid("dead-llm")
    scenario_id = start_scenario(base, sid, RAW)

    events = asyncio.run(read_debate(base, scenario_id, conflicts=3, timeout=120.0))

    counts = event_counts(events)
    assert counts["approval_needed"] == 3, counts
    assert counts.get("negotiation_turn", 0) == 0, counts  # provider down on turn 1

    errors = [ev for ev in events if ev["type"] == "error"]
    assert len(errors) == 3, [e["message"] for e in errors]
    assert all(ev["agent"] == "negotiator" for ev in errors)
    assert all("LLM unavailable during turn" in ev["message"] for ev in errors)

    # Every killed debate still produced a neutral resolution + no disagreement.
    resolutions = [ev for ev in events if ev["type"] == "resolution"]
    assert len(resolutions) == 3
    for ev in resolutions:
        assert ev["data"]["winning_side"] is None
        assert ev["data"]["agree"] is True
    assert counts.get("policy_disagreement", 0) == 0

    # The pipeline GENUINELY pauses for the human — no briefing/complete yet.
    assert counts.get("briefing_ready", 0) == 0
    assert counts.get("scenario_complete", 0) == 0


def test_dead_llm_failure_events_persisted_to_postgres(dead_llm_base):
    base = dead_llm_base.base_url
    sid = unique_sid("dead-llm-persist")
    scenario_id = start_scenario(base, sid, RAW)
    asyncio.run(read_debate(base, scenario_id, conflicts=3, timeout=120.0))

    approve_all(base, scenario_id, approved_by="failtest")
    asyncio.run(read_completion(base, scenario_id, timeout=120.0))

    r = get(base, f"/scenarios/{scenario_id}")
    assert r.status_code == 200, r.text
    replay = r.json()

    assert replay["scenario"]["status"] == "complete"
    assert len(replay["conflicts"]) == 3

    errors = [ev for ev in replay["events"] if ev["type"] == "error"]
    negotiator_errors = [ev for ev in errors if ev["agent"] == "negotiator"]
    assert len(negotiator_errors) == 3, [e["message"] for e in errors]
    assert all("LLM unavailable during turn" in ev["message"] for ev in negotiator_errors)

    res = [c["resolution"] for c in replay["conflicts"]]
    assert all(r2 is not None and r2["winning_side"] is None for r2 in res), res

    types = [ev["type"] for ev in replay["events"]]
    assert "scenario_complete" in types