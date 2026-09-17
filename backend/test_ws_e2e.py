"""Token-free WS E2E against the live stub server (AEGIS_LLM_STUB=1).

Runs the REAL two-phase pipeline over the REAL websocket feed and asserts the
full HITL event contract: debate phase streams predictions, parallel
{logistics, simulation}, the negotiation stream (>= 12 completed turns) and
one approval_needed per conflict — then the pipeline GENUINELY pauses (no
briefing_ready, no scenario_complete). Only after a human approves every
conflict does the briefing phase resume and emit scenario_complete.
"""
import asyncio
import json
import time

import httpx
import websockets

API = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000"
FLOOD = {
    "rainfall_mm_24h": 250,
    "river_level_m": 6.5,
    "river_level_danger_threshold_m": 4.0,
}


async def _read_debate(ws, approvals: set[str]) -> tuple[int, list[str], dict, list[str], bool]:
    """Read until every conflict has emitted approval_needed (or an error).
    Returns (turns, agents, sim_payload, errors, hit_approval_limit)."""
    turns = 0
    agents = []
    sim_payload = None
    errors = []
    while len(approvals) < 3:
        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
        t = ev["type"]
        if t == "agent_result":
            agents.append(ev["agent"])
            if ev["agent"] == "simulation":
                sim_payload = ev["data"]
        elif t == "negotiation_turn":
            turns += 1
        elif t == "approval_needed":
            cid = (ev.get("data") or {}).get("conflict_id")
            if cid:
                approvals.add(cid)
        elif t == "briefing_ready":
            raise AssertionError("briefing_ready arrived before human approval")
        elif t == "scenario_complete":
            raise AssertionError("scenario_complete arrived before human approval")
        elif t == "error":
            errors.append(ev.get("message"))
    return turns, agents, sim_payload, errors, True


async def _read_briefing(ws) -> tuple[bool, str | None]:
    """Read from the resume phase until scenario_complete."""
    briefing_seen = False
    briefing_head = None
    while True:
        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
        t = ev["type"]
        if t == "briefing_ready":
            briefing_seen = True
            briefing_head = (ev.get("data") or {}).get("headline")
        elif t == "scenario_complete":
            break
    return briefing_seen, briefing_head


async def main():
    sid = f"tokfree-ws-{int(time.time() * 1000)}"
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{API}/run-scenario", json={"scenario_id": sid, "raw_data": FLOOD})
        r.raise_for_status()
        scenario_id = r.json()["scenario_id"]
    print("sid:", scenario_id, flush=True)

    approvals: set[str] = set()
    async with websockets.connect(f"{WS}/ws/feed/{scenario_id}") as ws:
        turns, agents, sim_payload, errors, _ = await _read_debate(ws, approvals)

    print(f"approval_needed for: {sorted(approvals)}", flush=True)
    print(f"turns={turns} agents_tail={agents[-4:]}", flush=True)
    assert len(approvals) == 3, f"expected 3 approval_needed, got {len(approvals)}"

    # Human acts over HTTP on all three conflicts, then the briefing resumes.
    async with httpx.AsyncClient() as c:
        for key in sorted(approvals):
            r = await c.post(
                f"{API}/scenarios/{scenario_id}/conflicts/{key}/approve",
                json={"approved_by": "e2e-operator"},
            )
            r.raise_for_status()

    async with websockets.connect(f"{WS}/ws/feed/{scenario_id}") as ws:
        briefing_seen, briefing_head = await _read_briefing(ws)
    print(f"briefing_seen={briefing_seen} headline={briefing_head!r}", flush=True)
    print("sim:", sim_payload, flush=True)
    print("errors:", errors, flush=True)

    assert not errors, errors
    assert agents[0] == "prediction"
    assert "logistics" in agents and "simulation" in agents
    assert turns >= 12, turns
    assert briefing_seen, "no briefing after approvals"
    assert sim_payload and sim_payload.get("source") == "svg-fallback"
    assert "<animate" in sim_payload.get("svg", "")
    print("TOKEN-FREE HITL WS E2E: PASS")


asyncio.run(main())