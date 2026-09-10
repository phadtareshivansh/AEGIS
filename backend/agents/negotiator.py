"""Live LLM negotiation for resource conflicts.

Runs a structured four-turn debate between the Evacuation and Logistics
Advocates (streaming every chunk live), then asks a neutral Arbiter for a
final JSON decision. Pure orchestration — the personas live in personas.py,
the LLM plumbing in llm/client.py.
"""

import json
from datetime import datetime, timezone

from llm.client import LLMUnavailableError, generate, generate_stream
from agents.personas import (
    ARBITER_SYSTEM,
    EVACUATION_ADVOCATE_SYSTEM,
    LOGISTICS_ADVOCATE_SYSTEM,
)

TURNS = [
    (1, "evacuation_advocate", EVACUATION_ADVOCATE_SYSTEM),
    (2, "logistics_advocate", LOGISTICS_ADVOCATE_SYSTEM),
    (3, "evacuation_advocate", EVACUATION_ADVOCATE_SYSTEM),
    (4, "logistics_advocate", LOGISTICS_ADVOCATE_SYSTEM),
]
TURN_MAX_TOKENS = 220
TURN_TEMPERATURE = 0.8
ARBITER_MAX_TOKENS = 256
ARBITER_TEMPERATURE = 0.3

NEUTRAL_RESOLUTION = {
    "decision": "Escalate to human coordinator",
    "justification": "Negotiation could not complete due to a provider failure.",
    "winning_side": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alloc_for(plan: dict, zone_name: str) -> dict:
    return next(a for a in plan["allocations"] if a["zone_name"] == zone_name)


def _conflict_context(conflict: dict, plan: dict, inventory: dict) -> str:
    """Build a factual briefing for both advocates. No invented numbers."""
    ca, cb = conflict["claim_a"], conflict["claim_b"]
    a = _alloc_for(plan, ca["zone"])
    b = _alloc_for(plan, cb["zone"])
    pool = (
        f"Pool totals available: {inventory['ambulances']['total']} ambulances, "
        f"{inventory['water_tankers']['total']} water tankers, depots at central_depot."
    )

    if conflict["type"] == "route_conflict":
        route = next(r for r in inventory["evacuation_routes"] if r["id"] == conflict["resource"])
        return (
            f"RESOURCE: evacuation route '{route['id']}' connecting "
            f"{', '.join(route['connects_zones'])}. It is a single convoy corridor "
            "that cannot carry evacuation convoys and supply convoys at the same time.\n"
            f"CLAIM A ({ca['purpose']}): {ca['zone']} — population {a['population']}, "
            f"flood_probability_12h {a['flood_probability_12h']:.2f}, assigned "
            f"{a['ambulances']} ambulances and {a['water_tankers']} water tankers, "
            f"shelter {a['shelter_name']}.\n"
            f"CLAIM B ({cb['purpose']}): {cb['zone']} — population {b['population']}, "
            f"flood_probability_12h {b['flood_probability_12h']:.2f}; water and medical "
            "convoys must cross the same route to resupply shelters.\n" + pool
        )

    shelter = next(s for s in inventory["shelters"] if s["id"] == conflict["resource"])
    return (
        f"RESOURCE: shelter '{shelter['id']}' ({shelter['name']}) with capacity "
        f"{shelter['capacity']}.\n"
        f"CLAIM A ({ca['purpose']}): {ca['zone']} — population {a['population']}, "
        f"flood_probability_12h {a['flood_probability_12h']:.2f}.\n"
        f"CLAIM B ({cb['purpose']}): {cb['zone']} — population {b['population']}, "
        f"flood_probability_12h {b['flood_probability_12h']:.2f}.\n"
        f"Combined demand is {a['population'] + b['population']} against capacity "
        f"{shelter['capacity']}.\n" + pool
    )


def _turn_messages(context: str, transcript: list[tuple[str, str]], speaker: str) -> list[dict]:
    head = [f"\n\nTurn {i}: [{agent}] {text}" for i, (agent, text) in enumerate(transcript, start=1)]
    transcript_block = "".join(head) if head else " (you speak first)"
    user = (
        f"DEBATE CONTEXT:\n{context}\n"
        f"TRANSCRIPT SO FAR:{transcript_block}"
        f"\n\nRespond now as {speaker}. Stay in character."
    )
    return [{"role": "user", "content": user}]


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of free text."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


async def _stream_turn(
    persona: str,
    context: str,
    transcript: list[tuple[str, str]],
    turn: int,
    agent: str,
    conflict_id: str,
    writer,
) -> str:
    """Stream one advocate turn; forward every chunk as a live event."""
    full = ""
    async for chunk in generate_stream(
        persona,
        _turn_messages(context, transcript, agent),
        max_tokens=TURN_MAX_TOKENS,
        temperature=TURN_TEMPERATURE,
    ):
        full += chunk
        writer(
            {
                "event": {
                    "type": "negotiation_turn",
                    "agent": agent,
                    "message": full,
                    "data": {"conflict_id": conflict_id, "turn": turn, "streaming": True},
                    "timestamp": _now(),
                }
            }
        )
    return full


async def _arbitrate(context: str, transcript: list[tuple[str, str]]) -> dict | None:
    transcript_block = "\n".join(
        f"Turn {i}: [{agent}]\n{text}" for i, (agent, text) in enumerate(transcript, start=1)
    )
    for attempt in (1, 2):
        user = (
            f"DEBATE CONTEXT:\n{context}\n\nFULL TRANSCRIPT:\n{transcript_block}"
            + ("" if attempt == 1 else "\n\nRemember: output ONLY the JSON object, nothing else.")
        )
        raw = await generate(
            ARBITER_SYSTEM,
            [{"role": "user", "content": user}],
            max_tokens=ARBITER_MAX_TOKENS,
            temperature=ARBITER_TEMPERATURE,
        )
        parsed = _extract_json(raw)
        if parsed and parsed.get("decision"):
            return parsed
    return None


async def run_debate(conflict: dict, plan: dict, writer) -> tuple[list[dict], dict, str | None]:
    """Run the full debate for one conflict.

    Returns (turns, resolution, error_text). error_text is None on success,
    otherwise the negotiator should emit an error event and use the neutral
    resolution.
    """
    from agents.logistics import load_inventory

    context = _conflict_context(conflict, plan, load_inventory())
    transcript: list[tuple[str, str]] = []
    turns: list[dict] = []

    for turn, agent, persona in TURNS:
        try:
            text = await _stream_turn(
                persona, context, transcript, turn, agent, conflict["id"], writer
            )
        except LLMUnavailableError as exc:
            return turns, dict(NEUTRAL_RESOLUTION), f"LLM unavailable during turn {turn}: {exc}"
        transcript.append((agent, text))
        turns.append({"turn": turn, "agent": agent, "text": text})

    try:
        resolution = await _arbitrate(context, transcript)
    except LLMUnavailableError as exc:
        return turns, dict(NEUTRAL_RESOLUTION), f"LLM unavailable during arbitration: {exc}"

    if resolution is None:
        return (
            turns,
            {
                "decision": "Escalate to human coordinator",
                "justification": "Arbiter output could not be parsed after retry.",
                "winning_side": None,
            },
            "arbiter returned unparseable output after retry",
        )
    return turns, resolution, None