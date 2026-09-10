"""Human briefing generation for AEGIS.

Collapses the finished pipeline state into one plain-language JSON briefing
for a first responder. The LLM never sees raw dicts — prediction, logistics,
and resolution numbers are summarized into readable sentences first.

Mirrors the negociator's Arbiter pattern: one generate() call, two JSON
extraction attempts, and a deterministic fallback so the scenario still
completes under an LLM outage.
"""

import json
from datetime import datetime, timezone

from llm.client import LLMUnavailableError, generate

BRIEFING_SYSTEM = """You are the Human Briefing Agent in an emergency coordination system. You write for a first responder who has 30 seconds to read this before acting. Use short, plain sentences. No jargon, no mention of AI agents, models, or negotiation — just operational facts and clear reasoning. Output ONLY valid JSON matching this exact shape:
{"headline": "...", "risk_summary": "...", "resource_plan": "...", "conflict_resolution": "..." or null, "recommended_actions": ["...", "..."]}"""

BRIEFING_MAX_TOKENS = 512
BRIEFING_TEMPERATURE = 0.3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pct(probability: float) -> int:
    return round(probability * 100)


def _summarize(prediction: dict, logistics_plan: dict, resolution: dict | None) -> str:
    """Flatten pipeline state into readable text. No raw JSON dumps."""
    raw = prediction.get("raw_data") if isinstance(prediction, dict) else None
    lines: list[str] = []

    if raw:
        lines.append(
            f"SCENARIO INPUTS: rainfall {raw.get('rainfall_mm_24h')} mm/24h; river level "
            f"{raw.get('river_level_m')} m vs danger threshold "
            f"{raw.get('river_level_danger_threshold_m')} m."
        )

    ranked = (prediction or {}).get("at_risk_zones", [])
    if ranked:
        lines.append("PREDICTED RISK (12h flood probability, highest first):")
        for zone in ranked:
            lines.append(
                f"- {zone['zone_name']}: {zone['population']:,} people, "
                f"{_pct(zone['flood_probability_12h'])}% risk"
            )

    allocations = (logistics_plan or {}).get("allocations", [])
    if allocations:
        lines.append("RESOURCE PLAN:")
        for a in allocations:
            lines.append(
                f"- {a['zone_name']} -> {a['shelter_name'] or 'no shelter'} "
                f"({a.get('shelter_id') or 'pending'}): {a['population']:,} people, "
                f"{a['ambulances']} ambulances, {a['water_tankers']} water tankers"
                + (f" via {a['route_id']}" if a.get("route_id") else " (no route)"
)
            )

    if resolution:
        lines.append("RESOLVED CONFLICTS:")
        for cid, res in resolution.items():
            side = res.get("winning_side")
            lines.append(
                f"- {cid}: {res.get('decision', '')}"
                + (f" — winning side: {side}" if side else "")
            )
    else:
        lines.append("No resource conflicts were flagged.")

    return "\n".join(lines)


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


def _fallback_briefing(prediction: dict, logistics_plan: dict, resolution: dict | None) -> dict:
    """Deterministic briefing used when the LLM is unavailable or unparseable."""
    ranked = (prediction or {}).get("at_risk_zones", [])
    allocations = (logistics_plan or {}).get("allocations", [])

    if ranked:
        top = ranked[0]
        shelter = next(
            (a["shelter_name"] for a in allocations if a["zone_id"] == top["zone_id"]),
            "a local shelter",
        )
        headline = (
            f"{top['zone_name']} at {_pct(top['flood_probability_12h'])}% flood risk within "
            f"12 hours — start evacuation to {shelter}"
        )
        actions = [f"Evacuate {top['zone_name']} first", f"Move evacuees to {shelter}"]
    else:
        headline = "No high-risk zones detected"
        actions = ["Monitor river and rainfall levels"]

    for a in allocations[1:]:
        if a["population"] >= (ranked[0]["population"] if ranked else 1):
            continue
        actions.append(f"Stage {a['ambulances']} ambulances for {a['zone_name']}")

    resource_plan = "\n".join(
        f"{a['zone_name']}: {a['population']:,} people to "
        f"{a['shelter_name'] or 'pending'} with {a['ambulances']} ambulances and "
        f"{a['water_tankers']} water tankers"
        for a in allocations
    ) or "No allocations made."

    conflict_lines = [
        f"{cid}: {res.get('decision', '')}"
        for cid, res in (resolution or {}).items()
    ]
    return {
        "headline": headline,
        "risk_summary": (prediction or {}).get("summary", "No prediction available."),
        "resource_plan": resource_plan,
        "conflict_resolution": "\n".join(conflict_lines) or None,
        "recommended_actions": actions,
    }


async def generate_briefing(
    prediction: dict | None,
    logistics_plan: dict | None,
    resolution: dict | None,
) -> tuple[dict, str | None]:
    """Build the final briefing JSON. Returns (briefing, error_text).

    error_text is None on success; on failure the caller gets the fallback
    briefing and should surface the error event.
    """
    context = _summarize(prediction, logistics_plan, resolution)

    if not (prediction or logistics_plan):
        return (
            _fallback_briefing(prediction, logistics_plan, resolution),
            "missing prediction/logistics state",
        )
    if not (context.strip() or (prediction or {}).get("at_risk_zones")):
        return (
            _fallback_briefing(prediction, logistics_plan, resolution),
            "state too sparse to brief",
        )

    for attempt in (1, 2):
        user = f"SCENARIO BRIEFING CONTEXT:\n{context}" + (
            "" if attempt == 1 else "\n\nRemember: output ONLY the JSON object, nothing else."
        )
        try:
            raw = await generate(
                BRIEFING_SYSTEM,
                [{"role": "user", "content": user}],
                max_tokens=BRIEFING_MAX_TOKENS,
                temperature=BRIEFING_TEMPERATURE,
            )
        except LLMUnavailableError as exc:
            return _fallback_briefing(prediction, logistics_plan, resolution), f"LLM unavailable: {exc}"

        parsed = _extract_json(raw)
        if _valid_briefing(parsed):
            return parsed, None

    return (
        _fallback_briefing(prediction, logistics_plan, resolution),
        "briefing output unparseable after retry",
    )


def _valid_briefing(value: dict | None) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("headline", "risk_summary", "resource_plan"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            return False
    actions = value.get("recommended_actions")
    if not isinstance(actions, list) or not actions or not all(isinstance(a, str) and a for a in actions):
        return False
    conflict = value.get("conflict_resolution")
    return conflict is None or (isinstance(conflict, str) and conflict.strip())