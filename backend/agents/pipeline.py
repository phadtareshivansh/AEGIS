from datetime import datetime, timezone

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from agents.briefing import generate_briefing
from agents.logistics import build_logistics_plan
from agents.negotiator import run_debate
from agents.prediction import build_prediction
from agents.sensing import list_live_locations, run_live_sensing
from agents.simulation import render_simulation
from schemas import ScenarioState


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
    ``data_mode == "live"`` and a known ``location_key`` we replace the demo
    telemetry with live-sourced values (rainfall from Open-Meteo weather;
    river level derived from GloFAS discharge via a rating curve). Any
    failure (or ''demo'' mode) is a no-op — the pipeline keeps the demo
    scenario and simply emits an informative event, so the system still works
    offline with zero API key.
    """
    if state.raw_data.get("data_mode") != "live":
        return {}

    location_key = state.raw_data.get("location_key")
    result = await run_live_sensing(location_key)
    if not result.get("live"):
        return {
            "events": [
                _event(
                    "sensing_fallback",
                    "sensing",
                    f"Live sensing unavailable for {location_key!r} "
                    f"({result.get('error')}) — using demo scenario.",
                )
            ]
        }

    merged = {**state.raw_data, **result["raw_data"]}
    prov = result.get("provenance", {})
    message = (
        f"Live telemetry for {prov.get('location', location_key)} @ "
        f"{prov.get('lat')},{prov.get('lon')}: {result['raw_data']['rainfall_mm_24h']:.0f} mm/24h, "
        f"river {result['raw_data']['river_level_m']:.2f} m vs danger "
        f"{result['raw_data']['river_level_danger_threshold_m']:.2f} m "
        f"({prov.get('river_level_note', 'rating curve')})."
    )
    return {
        "raw_data": merged,
        "events": [_event("agent_result", "sensing", message)],
    }

async def prediction_node(state: ScenarioState) -> dict:
    prediction = build_prediction(state.raw_data)
    event = _event("agent_result", "prediction", prediction["summary"], prediction)
    return {
        "prediction": prediction,
        "events": [event],
    }


async def logistics_node(state: ScenarioState) -> dict:
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
        turns, resolution, error = await run_debate(
            conflict, state.logistics_plan, writer
        )
        negotiation_log.append(
            {
                "conflict_id": conflict["id"],
                "transcript": turns,
                "resolution": resolution,
            }
        )
        resolutions[conflict["id"]] = resolution

        if error:
            events.append(
                _event(
                    "error",
                    "negotiator",
                    f"Negotiation could not complete — {error}",
                )
            )
        events.append(
            _event(
                "resolution",
                "arbiter",
                resolution["decision"],
                {**resolution, "conflict_id": conflict["id"]},
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