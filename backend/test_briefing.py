import asyncio
import json
import re

from agents.pipeline import build_briefing_graph, build_debate_graph
from schemas import ScenarioState

FLOOD = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}

MACHINE_WORDS = [
    "advocate", "arbiter", "negotiator", "negotiation", "debate",
    "ai", "agent", "llm", "model", "pipeline",
]

BRIEFING_KEYS = ("headline", "risk_summary", "resource_plan", "conflict_resolution", "recommended_actions")


async def run_debate() -> ScenarioState:
    """Phase 1: analysis + negotiation. Ends with every conflict awaiting
    human approval — briefing is deliberately NOT reached."""
    state = ScenarioState(scenario_id="briefing-end-to-end", raw_data=FLOOD)
    graph = build_debate_graph()
    async for mode, chunk in graph.astream(state, stream_mode=["updates", "custom"]):
        if mode == "custom":
            continue
        for update in chunk.values():
            update = update or {}
            for k, v in update.items():
                if k == "events":
                    state.events = list(state.events) + v
                else:
                    setattr(state, k, v)
    return state


async def main():
    """Full HITL briefing test: pause at approval_needed, simulate a human
    approving every conflict, then run the briefing phase against the final
    resolution set — exactly what the resume endpoint does."""
    state = await run_debate()

    print(f"conflicts after debate: {len(state.conflicts)}")
    print(f"negotiation_log entries: {len(state.negotiation_log)}")
    assert len(state.negotiation_log) == len(state.conflicts), "negotiation_log mismatch (regression)"
    assert state.briefing is None, "briefing must NOT exist while awaiting approval"
    assert state.status == "running", f"status before approval: {state.status}"
    types = [e["type"] for e in state.events]
    assert "approval_needed" in types, "no approval_needed event emitted"
    assert "briefing_ready" not in types, "briefing ran before human approval"
    print("PAUSE VERIFIED: no briefing until human acts")

    # Human approves all conflicts (in the real flow resolutions are written
    # from the DB with approved/overridden status — simulate the same final set).
    final_resolutions = dict(state.resolution or {})
    for conflict in state.conflicts:
        final_resolutions[conflict["id"]] = dict(state.resolution[conflict["id"]])

    resume_state = ScenarioState(
        scenario_id=state.scenario_id,
        raw_data=state.raw_data,
        prediction=state.prediction,
        logistics_plan=state.logistics_plan,
        resolution=final_resolutions,
    )
    graph = build_briefing_graph()
    async for mode, chunk in graph.astream(resume_state, stream_mode=["updates", "custom"]):
        if mode == "custom":
            continue
        for update in chunk.values():
            update = update or {}
            for k, v in update.items():
                if k == "events":
                    resume_state.events = list(resume_state.events) + v
                else:
                    setattr(resume_state, k, v)

    print("========== FINAL BRIEFING ==========")
    print(json.dumps(resume_state.briefing, indent=2))

    tail = [e["type"] for e in resume_state.events if e["type"] != "agent_result"]
    print("\n========== EVENT TAIL (state events) ==========")
    for t in tail:
        print(f"  {t}")

    assert resume_state.status == "complete", f"status={resume_state.status}"
    assert isinstance(resume_state.briefing, dict), "state.briefing not set"

    for key in BRIEFING_KEYS:
        assert key in resume_state.briefing, f"briefing missing key {key!r}"

    for key in ("headline", "risk_summary", "resource_plan"):
        assert isinstance(resume_state.briefing[key], str) and resume_state.briefing[key].strip(), f"{key} empty"
    actions = resume_state.briefing["recommended_actions"]
    assert isinstance(actions, list) and actions, "recommended_actions empty"
    assert all(isinstance(a, str) and a for a in actions), "recommended_actions has invalid item"
    conflict = resume_state.briefing["conflict_resolution"]
    assert conflict is None or (isinstance(conflict, str) and conflict.strip()), "conflict_resolution malformed"

    event_types = [e["type"] for e in resume_state.events]
    assert "briefing_ready" in event_types, "no briefing_ready event"
    assert "scenario_complete" in event_types, "no scenario_complete event"
    bready = next(e for e in resume_state.events if e["type"] == "briefing_ready")
    assert bready["agent"] == "briefing", f"briefing_ready agent={bready['agent']}"
    assert bready["data"] == resume_state.briefing, "briefing_ready payload != state.briefing"
    assert event_types[-1] == "scenario_complete", "scenario_complete must be last state event"

    text = " ".join(
        str(resume_state.briefing[k]) for k in ("headline", "risk_summary", "resource_plan", "conflict_resolution")
    ).lower()

    warnings = []
    if len(resume_state.briefing["headline"]) > 120:
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