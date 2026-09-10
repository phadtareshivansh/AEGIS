from datetime import datetime, timezone

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from agents.briefing import generate_briefing
from agents.logistics import build_logistics_plan
from agents.negotiator import run_debate
from agents.prediction import build_prediction
from schemas import ScenarioState


async def prediction_node(state: ScenarioState) -> dict:
    prediction = build_prediction(state.raw_data)
    event = {
        "type": "agent_result",
        "agent": "prediction",
        "message": prediction["summary"],
        "data": prediction,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "prediction": prediction,
        "events": state.events + [event],
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
    event = {
        "type": "agent_result",
        "agent": "logistics",
        "message": message,
        "data": plan,
        "timestamp": timestamp,
    }
    flagged = [
        {
            "type": "conflict_flagged",
            "agent": "logistics",
            "message": line,
            "data": conflict,
            "timestamp": timestamp,
        }
        for conflict, line in zip(conflicts, lines[1:])
    ]
    return {
        "logistics_plan": plan,
        "conflicts": state.conflicts + conflicts,
        "events": state.events + [event] + flagged,
    }


async def negotiator_node(state: ScenarioState) -> dict:
    if not state.conflicts:
        event = {
            "type": "agent_result",
            "agent": "negotiator",
            "message": "No conflicts detected — no negotiation was needed.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return {"events": state.events + [event]}

    writer = get_stream_writer()
    negotiation_log = list(state.negotiation_log)
    resolutions = dict(state.resolution or {})
    events = list(state.events)

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
                {
                    "type": "error",
                    "agent": "negotiator",
                    "message": f"Negotiation could not complete — {error}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        events.append(
            {
                "type": "resolution",
                "agent": "arbiter",
                "message": resolution["decision"],
                "data": {**resolution, "conflict_id": conflict["id"]},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
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
    events = list(state.events)
    events.append(
        {
            "type": "briefing_ready",
            "agent": "briefing",
            "message": briefing["headline"],
            "data": briefing,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    if error:
        events.append(
            {
                "type": "error",
                "agent": "briefing",
                "message": f"Briefing fell back to deterministic content — {error}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
    events.append(
        {
            "type": "scenario_complete",
            "agent": "briefing",
            "message": briefing["headline"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    return {"briefing": briefing, "events": events, "status": "complete"}


def build_graph():
    graph = StateGraph(ScenarioState)
    graph.add_node("prediction", prediction_node)
    graph.add_node("logistics", logistics_node)
    graph.add_node("negotiator", negotiator_node)
    graph.add_node("briefing", briefing_node)

    graph.add_edge(START, "prediction")
    graph.add_edge("prediction", "logistics")
    graph.add_edge("logistics", "negotiator")
    graph.add_edge("negotiator", "briefing")
    graph.add_edge("briefing", END)
    return graph.compile()