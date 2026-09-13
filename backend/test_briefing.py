import asyncio
import json
import re

from agents.pipeline import build_graph
from schemas import ScenarioState

FLOOD = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}

MACHINE_WORDS = [
    "advocate", "arbiter", "negotiator", "negotiation", "debate",
    "ai", "agent", "llm", "model", "pipeline",
]

BRIEFING_KEYS = ("headline", "risk_summary", "resource_plan", "conflict_resolution", "recommended_actions")


async def main():
    state = ScenarioState(scenario_id="briefing-end-to-end", raw_data=FLOOD)
    graph = build_graph()

    live = []
    async for mode, chunk in graph.astream(state, stream_mode=["updates", "custom"]):
        if mode == "custom":
            live.append(chunk["event"])
        else:
            for update in chunk.values():
                for k, v in update.items():
                    if k == "events":
                        state.events = list(state.events) + v
                    else:
                        setattr(state, k, v)

    print("========== FINAL BRIEFING ==========")
    print(json.dumps(state.briefing, indent=2))

    tail = [e["type"] for e in state.events if e["type"] != "agent_result"]
    print("\n========== EVENT TAIL (state events) ==========")
    for t in tail:
        print(f"  {t}")

    assert state.status == "complete", f"status={state.status}"
    assert isinstance(state.briefing, dict), "state.briefing not set"

    for key in BRIEFING_KEYS:
        assert key in state.briefing, f"briefing missing key {key!r}"

    for key in ("headline", "risk_summary", "resource_plan"):
        assert isinstance(state.briefing[key], str) and state.briefing[key].strip(), f"{key} empty"
    actions = state.briefing["recommended_actions"]
    assert isinstance(actions, list) and actions, "recommended_actions empty"
    assert all(isinstance(a, str) and a for a in actions), "recommended_actions has invalid item"
    conflict = state.briefing["conflict_resolution"]
    assert conflict is None or (isinstance(conflict, str) and conflict.strip()), "conflict_resolution malformed"

    event_types = [e["type"] for e in state.events]
    assert "briefing_ready" in event_types, "no briefing_ready event"
    assert "scenario_complete" in event_types, "no scenario_complete event"
    bready = next(e for e in state.events if e["type"] == "briefing_ready")
    assert bready["agent"] == "briefing", f"briefing_ready agent={bready['agent']}"
    assert bready["data"] == state.briefing, "briefing_ready payload != state.briefing"
    assert event_types[-1] == "scenario_complete", "scenario_complete must be last state event"

    assert len(state.negotiation_log) == len(state.conflicts), "negotiation_log mismatch (regression)"

    text = " ".join(
        str(state.briefing[k]) for k in ("headline", "risk_summary", "resource_plan", "conflict_resolution")
    ).lower()

    warnings = []
    if len(state.briefing["headline"]) > 120:
        warnings.append("headline exceeds 120 chars")
    if len(actions) > 5:
        warnings.append(f"recommended_actions has {len(actions)} items (keep short)")
    for word in MACHINE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            warnings.append(f"machine/agent language leaked: {word!r}")

    print("\n========== QUALITY GATE SUMMARY ==========")
    if warnings:
        for w in warnings:
            print(f"  WARN: {w}")
        print("  RESULT: REVIEW")
    else:
        print("  no warnings — reads like a human briefing")
        print("  RESULT: PASS")


asyncio.run(main())