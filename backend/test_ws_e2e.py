"""Token-free WS E2E against the live stub server (AEGIS_LLM_STUB=1).

Runs the REAL pipeline graph over the REAL websocket feed and asserts the full
event contract: prediction first, {logistics, simulation} parallel second, the
12-turn negotiator stream, briefing, and scenario_complete. No LLM tokens.
"""
import asyncio
import json

import httpx
import websockets

API = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000"
FLOOD = {
    "rainfall_mm_24h": 250,
    "river_level_m": 6.5,
    "river_level_danger_threshold_m": 4.0,
}


async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{API}/run-scenario", json={"scenario_id": "tokfree-ws", "raw_data": FLOOD})
        r.raise_for_status()
        sid = r.json()["scenario_id"]
    print("sid:", sid, flush=True)

    agents = []
    sim_payload = None
    errors = []
    turns = 0
    briefing_head = None
    complete = False
    briefing_seen = False

    async with websockets.connect(f"{WS}/ws/feed/{sid}") as ws:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            ev = json.loads(raw)
            t = ev["type"]
            if t == "agent_result":
                agents.append(ev["agent"])
                if ev["agent"] == "simulation":
                    sim_payload = ev["data"]
            elif t == "negotiation_turn":
                turns += 1
                if ev["agent"] == "negotiator":
                    turns += 1
            elif t == "briefing_ready":
                briefing_seen = True
                briefing_head = (ev.get("data") or {}).get("headline")
            elif t == "scenario_complete":
                complete = True
                break
            elif t == "error":
                errors.append(ev.get("message"))

    print("agents:", agents, flush=True)
    print(f"turns={turns} briefing_seen={briefing_seen} complete={complete}", flush=True)
    print("sim:", sim_payload, flush=True)
    print("errors:", errors, flush=True)

    assert not errors, errors
    assert agents[0] == "prediction"
    assert agents[-1] == "briefing"
    assert "logistics" in agents and "simulation" in agents
    assert turns == 12, turns
    assert briefing_seen and complete
    assert sim_payload and sim_payload.get("source") == "svg-fallback"
    assert "<animate" in sim_payload.get("svg", "")
    print("TOKEN-FREE WS E2E: PASS")


asyncio.run(main())
