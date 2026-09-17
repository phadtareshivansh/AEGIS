from datetime import datetime, timezone

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from agents.briefing import generate_briefing
from agents.logistics import build_logistics_plan
from agents.negotiator import run_debate
from agents.policy import evaluate_conflict, policy_agreement
from agents.prediction import (
    build_prediction,
    build_rain_watch_prediction,
    build_unassessable_prediction,
)
from agents.sensing import run_live_sensing
from agents.simulation import build_unavailable_svg, render_simulation
from schemas import ScenarioState

NO_ASSESSMENT = ("not_applicable", "insufficient_hydrology_data")


def _event(type_: str, agent: str, message: str, data: dict | None = None) -> dict:
    return {
        "type": type_,
        "agent": agent,
        "message": message,
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def sensing_node(state: ScenarioState) -> dict:
    """Optional: pull live rain+river data for a real location (Open-Meteo, keyless).

    Reads the raw_data shape trusted by prediction_node. With
    ``data_mode == "live"`` and a resolved ``location`` (or curated
    ``location_key`` for backwards compatibility) we replace the demo
    telemetry with real values (rainfall from Open-Meteo weather; river
    fields derived from GloFAS discharge when a reach exists). Any failure
    (or ``demo`` mode) is a no-op — the pipeline keeps the demo scenario and
    simply emits an informative event, so the system still works offline
    with zero API key.

    When no usable GloFAS reach exists the merge still carries the REAL
    rainfall but records ``hydrology_status``/``hydrology_reason`` so the
    downstream nodes refuse to fabricate a riverine number.
    """
    if state.raw_data.get("data_mode") != "live":
        return {}

    location = state.raw_data.get("location") or state.raw_data.get("location_key")
    result = await run_live_sensing(location)
    if not result.get("live"):
        return {
            "events": [
                _event(
                    "sensing_fallback",
                    "sensing",
                    f"Live sensing unavailable for {location!r} "
                    f"({result.get('error')}) — using demo scenario.",
                )
            ]
        }

    merged = {**state.raw_data, **result["raw_data"]}
    merged["hydrology_status"] = result.get("hydrology_status", "insufficient")
    merged["hydrology_reason"] = result.get("hydrology_reason")
    prov = result.get("provenance", {})
    name = prov.get("location", location)
    status = result["hydrology_status"]

    if status == "available":
        message = (
            f"Live telemetry for {name} @ {prov.get('lat')},{prov.get('lon')}: "
            f"{result['raw_data']['rainfall_mm_24h']:.0f} mm/24h, river "
            f"{result['raw_data']['river_level_m']:.2f} m vs danger "
            f"{result['raw_data']['river_level_danger_threshold_m']:.2f} m "
            f"({prov.get('river_level_note', 'rating curve')})."
        )
    else:
        message = (
            f"Live rainfall for {name}: {result['raw_data']['rainfall_mm_24h']:.0f} "
            f"mm/24h observed. River risk NOT assessed — "
            f"{result.get('hydrology_reason') or 'no usable GloFAS reach'}."
        )
    return {
        "raw_data": merged,
        "events": [_event("agent_result", "sensing", message)],
    }

async def prediction_node(state: ScenarioState) -> dict:
    status = state.raw_data.get("hydrology_status")
    location = state.raw_data.get("location")
    location_name = location.get("name") if isinstance(location, dict) else None

    if status == "not_applicable":
        prediction = build_unassessable_prediction(
            reason="insufficient_hydrology_data",
            detail=state.raw_data.get("hydrology_reason"),
            location_name=location_name,
        )
    elif status == "insufficient":
        prediction = build_rain_watch_prediction(
            rainfall_mm_24h=state.raw_data.get("rainfall_mm_24h", 0.0),
            reason=state.raw_data.get("hydrology_reason"),
            location_name=location_name,
        )
    else:
        prediction = build_prediction(state.raw_data)

    event = _event("agent_result", "prediction", prediction["summary"], prediction)
    return {
        "prediction": prediction,
        "events": [event],
    }


async def logistics_node(state: ScenarioState) -> dict:
    if state.prediction.get("flood_probability") in NO_ASSESSMENT:
        event = _event(
            "agent_result",
            "logistics",
            "No allocation plan produced — " + state.prediction["summary"],
            {"allocations": [], "conflicts_flagged": 0},
        )
        return {"logistics_plan": {"allocations": [], "conflicts_flagged": 0}, "events": [event]}

    plan, conflicts = build_logistics_plan(state.prediction)
    lines = [f"allocated {sum(a['ambulances'] for a in plan['allocations'])} ambulances "
             f"and {sum(a['water_tankers'] for a in plan['allocations'])} water tankers "
             f"across {len(plan['allocations'])} zones"]
    for conflict in conflicts:
        lines.append(
            f"{conflict['id']}: {conflict['claim_a']['zone']} needs "
            f"{conflict['claim_a']['purpose']} via {conflict['resource']} but "
            f"{conflict['claim_b']['zone']} also needs that resource for "
            f"{conflict['claim_b']['purpose']}"
            if conflict["type"] == "route_conflict"
            else f"{conflict['id']}: {conflict['claim_a']['zone']} and "
                 f"{conflict['claim_b']['zone']} exceed {conflict['resource']} capacity"
        )
    message = "; ".join(lines)
    timestamp = datetime.now(timezone.utc).isoformat()
    event = _event("agent_result", "logistics", message, plan)
    flagged = [
        _event("conflict_flagged", "logistics", line, conflict)
        for conflict, line in zip(conflicts, lines[1:])
    ]
    return {
        "logistics_plan": plan,
        "conflicts": state.conflicts + conflicts,
        "events": [event] + flagged,
    }


async def simulation_node(state: ScenarioState) -> dict:
    if state.prediction.get("flood_probability") in NO_ASSESSMENT:
        reason = state.prediction.get("detail") or state.prediction.get("reason")
        svg = build_unavailable_svg(reason or "hydrology data unavailable")
        event = _event(
            "agent_result",
            "simulation",
            "Simulation shows assessment unavailable — hydrology data insufficient.",
            {"svg": svg, "source": "svg-fallback"},
        )
        return {"simulation": {"svg": svg, "source": "svg-fallback"}, "events": [event]}

    payload, source, error = await render_simulation(
        state.raw_data, state.prediction
    )
    events = []
    if error:
        events.append(
            _event(
                "error",
                "simulation",
                f"Image generation failed ({error}) — used animated SVG overlay.",
            )
        )
    at_risk = len(state.prediction.get("at_risk_zones", [])) if state.prediction else 0
    events.append(
        _event(
            "agent_result",
            "simulation",
            f"Simulation rendered for {at_risk} at-risk zones — {source}.",
            payload,
        )
    )
    return {"simulation": payload, "events": events}


async def negotiator_node(state: ScenarioState) -> dict:
    if not state.conflicts:
        event = _event(
            "agent_result",
            "negotiator",
            "No conflicts detected — no negotiation was needed.",
        )
        return {"events": [event]}

    writer = get_stream_writer()
    negotiation_log = list(state.negotiation_log)
    resolutions = dict(state.resolution or {})
    events: list[dict] = []

    for conflict in state.conflicts:
        turns, arbiter_resolution, error = await run_debate(
            conflict, state.logistics_plan, writer
        )
        if error:
            events.append(
                _event(
                    "error",
                    "negotiator",
                    f"Negotiation could not complete — {error}",
                )
            )

        # --- policy check (independent of the LLM debate) ------------------
        policy_rec = evaluate_conflict(conflict, state.logistics_plan)
        agree = policy_agreement(arbiter_resolution, policy_rec)

        resolution = {
            **arbiter_resolution,
            "arbiter_decision": dict(arbiter_resolution),
            "policy_recommendation": policy_rec,
            "agree": agree,
        }

        negotiation_log.append(
            {
                "conflict_id": conflict["id"],
                "transcript": turns,
                "resolution": resolution,
            }
        )
        resolutions[conflict["id"]] = resolution

        events.append(
            _event(
                "resolution",
                "arbiter",
                arbiter_resolution["decision"],
                resolution,
            )
        )

        if not agree:
            events.append(
                _event(
                    "policy_disagreement",
                    "policy",
                    (
                        f"Policy and Arbiter disagree on {conflict['id']}: "
                        f"arbiter chose {arbiter_resolution['winning_side']!r} "
                        f"but the ruleset recommends "
                        f"{policy_rec['winning_side']!r}"
                    ),
                    {
                        "conflict_id": conflict["id"],
                        "arbiter_decision": dict(arbiter_resolution),
                        "policy_recommendation": policy_rec,
                    },
                )
            )

        # Human-in-the-loop: the proposal is not final. Park the conflict in
        # awaiting_approval and surface an actionable approval_needed event so
        # the briefing phase cannot run until a human approves or overrides.
        events.append(
            _event(
                "approval_needed",
                "negotiator",
                arbiter_resolution["decision"],
                {
                    "conflict_id": conflict["id"],
                    **arbiter_resolution,
                    "policy_recommendation": policy_rec,
                    "agree": agree,
                },
            )
        )

    return {
        "negotiation_log": negotiation_log,
        "resolution": resolutions,
        "events": events,
    }


async def briefing_node(state: ScenarioState) -> dict:
    briefing, error = await generate_briefing(
        state.prediction, state.logistics_plan, state.resolution
    )
    events: list[dict] = [
        _event("briefing_ready", "briefing", briefing["headline"], briefing)
    ]
    if error:
        events.append(
            _event(
                "error",
                "briefing",
                f"Briefing fell back to deterministic content — {error}",
            )
        )
    events.append(
        _event("scenario_complete", "briefing", briefing["headline"])
    )
    return {"briefing": briefing, "events": events, "status": "complete"}


def build_graph():
    graph = StateGraph(ScenarioState)
    graph.add_node("sensing", sensing_node)
    graph.add_node("prediction", prediction_node)
    graph.add_node("logistics", logistics_node)
    graph.add_node("simulation", simulation_node)
    graph.add_node("negotiator", negotiator_node)
    graph.add_node("briefing", briefing_node)

    graph.add_edge(START, "sensing")
    graph.add_edge("sensing", "prediction")
    graph.add_edge("prediction", "logistics")
    graph.add_edge("prediction", "simulation")
    graph.add_edge("logistics", "negotiator")
    graph.add_edge("simulation", "negotiator")
    graph.add_edge("negotiator", "briefing")
    graph.add_edge("briefing", END)
    return graph.compile()


def build_debate_graph():
    """Phase 1 — autonomous analysis + negotiation.

    Ends right after the negotiator. Every conflict is parked in
    ``awaiting_approval``; ``briefing`` is intentionally NOT connected so the
    scenario waits for human approval/override via the HTTP endpoints.
    """
    graph = StateGraph(ScenarioState)
    graph.add_node("sensing", sensing_node)
    graph.add_node("prediction", prediction_node)
    graph.add_node("logistics", logistics_node)
    graph.add_node("simulation", simulation_node)
    graph.add_node("negotiator", negotiator_node)

    graph.add_edge(START, "sensing")
    graph.add_edge("sensing", "prediction")
    graph.add_edge("prediction", "logistics")
    graph.add_edge("prediction", "simulation")
    graph.add_edge("logistics", "negotiator")
    graph.add_edge("simulation", "negotiator")
    graph.add_edge("negotiator", END)
    return graph.compile()


def build_briefing_graph():
    """Phase 2 — human-approved decisions collapse into the final briefing."""
    graph = StateGraph(ScenarioState)
    graph.add_node("briefing", briefing_node)

    graph.add_edge(START, "briefing")
    graph.add_edge("briefing", END)
    return graph.compile()