"""Persistence + human-in-the-loop verification harness for AEGIS.

Usage:
    # Phase 1 — run a scenario, assert the pipeline pauses for approval,
    # approve/override via the API, then assert DB rows + briefing
    python verify_persistence.py --run
    python verify_persistence.py --run --override
    python verify_persistence.py --run --timeout

    # Phase 2 — restart the backend, then re-run to assert replay survived
    python verify_persistence.py --replay

Phase 1 calls the running backend over HTTP/WS, queries the DB directly via
SQLAlchemy, and writes a state file to /tmp/aegis_verify_state.json.

Phase 2 re-loads that state file and confirms GET /scenarios/{id} still
returns the full record after a backend restart.

Set AEGIS_LLM_STUB=1 (or rely on the real Groq provider when GROQ_API_KEY is
configured in .env). --run --override asserts the human's decision — not the
Arbiter's — lands in the final briefing; that text-level assertion is only
enforced under the stub, since a real LLM may paraphrase. --timeout runs the
watchdog check and requires APPROVAL_TIMEOUT_MINUTES to be small on the server.
"""

import argparse
import asyncio
import json
import os
import time
import sys
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import func, select

from db.models import (
    Conflict,
    Event,
    NegotiationTurn,
    Resolution,
    Scenario,
)
from db.session import async_session

API = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000"
STATE_FILE = Path("/tmp/aegis_verify_state.json")

DEMO_RAW = {
    "rainfall_mm_24h": 250,
    "river_level_m": 6.5,
    "river_level_danger_threshold_m": 4.0,
}

OVERRIDE_DECISION = "HUMAN_OVERRIDE: evacuate zone_highland first, empty convoys"
IS_STUB = os.getenv("AEGIS_LLM_STUB", "0") == "1"


def _assert(cond: bool, msg: str):
    assert cond, msg
    print(f"  PASS: {msg}")


async def _post_scenario(client_id: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{API}/run-scenario",
            json={"scenario_id": client_id, "raw_data": DEMO_RAW},
        )
        r.raise_for_status()
        body = r.json()
        print(f"  POST /run-scenario -> {body}")
        return body


async def _read_until(client_id: str, counts: dict[str, int], timeout: float = 300) -> list[dict]:
    """Read the WS feed until each type in ``counts`` has been seen at least
    ``counts[type]`` times (used to detect the approval pause), then close.
    """
    events: list[dict] = []
    seen: dict[str, int] = {}
    async with websockets.connect(f"{WS}/ws/feed/{client_id}") as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except websockets.exceptions.ConnectionClosedOK:
                break
            ev = json.loads(raw)
            events.append(ev)
            t = ev.get("type")
            if t in counts:
                seen[t] = seen.get(t, 0) + 1
                if seen[t] >= counts[t] and all(seen.get(k, 0) >= c for k, c in counts.items()):
                    break
    return events


async def _consume_ws(client_id: str, timeout: float = 300) -> list[dict]:
    """Connect to the WS feed (may land on live queue or DB replay) and
    return the full list of received events up to scenario_complete."""
    events: list[dict] = []
    async with websockets.connect(f"{WS}/ws/feed/{client_id}") as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except websockets.exceptions.ConnectionClosedOK:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "scenario_complete":
                break
    return events


async def _db_counts(scenario_uuid: str) -> dict:
    async with async_session() as s:
        scenario = await s.get(Scenario, scenario_uuid)
        events_count = (
            await s.execute(
                select(func.count()).select_from(Event).where(Event.scenario_id == scenario.id)
            )
        ).scalar_one()
        conflicts = (
            await s.execute(
                select(Conflict).where(Conflict.scenario_id == scenario.id).order_by(Conflict.key)
            )
        ).scalars().all()
        turns_per_conflict = {}
        resolutions_count = 0
        approved_details = {}
        for conflict in conflicts:
            turns = (
                await s.execute(
                    select(func.count())
                    .select_from(NegotiationTurn)
                    .where(NegotiationTurn.conflict_id == conflict.id)
                )
            ).scalar_one()
            turns_per_conflict[conflict.key] = turns
            res = (
                await s.execute(
                    select(Resolution).where(Resolution.conflict_id == conflict.id)
                )
            ).scalar_one_or_none()
            if res is not None:
                resolutions_count += 1
            approved_details[conflict.key] = {
                "status": conflict.status,
                "approved_by": res.approved_by if res else None,
                "decision": res.decision if res else None,
            }
    return {
        "status": scenario.status,
        "data_mode": scenario.data_mode,
        "location_key": scenario.location_key,
        "client_id": scenario.client_id,
        "events": events_count,
        "conflicts": len(conflicts),
        "turns_per_conflict": turns_per_conflict,
        "total_turns": sum(turns_per_conflict.values()),
        "resolutions": resolutions_count,
        "conflict_status": {
            k: v["status"] for k, v in approved_details.items()
        },
        "approved_by": {
            k: v["approved_by"] for k, v in approved_details.items()
        },
        "decisions": {k: v["decision"] for k, v in approved_details.items()},
    }


async def _replay_get(id_ref: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{API}/scenarios/{id_ref}")
        assert r.status_code == 200, f"GET returned {r.status_code}: {r.text}"
        return r.json()


async def _list_scenarios() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{API}/scenarios")
        r.raise_for_status()
        return r.json()


async def _approve(scenario_ref: str, key: str, approved_by: str = "verifier") -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{API}/scenarios/{scenario_ref}/conflicts/{key}/approve",
            json={"approved_by": approved_by},
        )
        assert r.status_code == 200, f"approve {key} -> {r.status_code}: {r.text}"
        return r.json()


async def _override(scenario_ref: str, key: str, approved_by: str = "verifier") -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{API}/scenarios/{scenario_ref}/conflicts/{key}/override",
            json={
                "approved_by": approved_by,
                "override_reason": "evacuation is life-critical, must not share the route",
                "override_decision": OVERRIDE_DECISION,
            },
        )
        assert r.status_code == 200, f"override {key} -> {r.status_code}: {r.text}"
        return r.json()


async def _assert_replay_consistency(replay: dict, expected_db: dict, label: str):
    _assert(
        replay["scenario"]["status"] == "complete",
        f"[{label}] status == complete",
    )
    _assert(
        len(replay["events"]) == expected_db["events"],
        f"[{label}] events count {len(replay['events'])} == {expected_db['events']}",
    )
    _assert(
        len(replay["conflicts"]) == expected_db["conflicts"],
        f"[{label}] conflicts count {len(replay['conflicts'])} == {expected_db['conflicts']}",
    )
    replay_turns = sum(len(c["turns"]) for c in replay["conflicts"])
    _assert(
        replay_turns == expected_db["total_turns"],
        f"[{label}] total turns {replay_turns} == {expected_db['total_turns']}",
    )
    replay_resolutions = sum(1 for c in replay["conflicts"] if c["resolution"] is not None)
    _assert(
        replay_resolutions == expected_db["resolutions"],
        f"[{label}] resolutions {replay_resolutions} == {expected_db['resolutions']}",
    )


async def _assert_human_state(replay: dict, expected_db: dict, label: str):
    """Every conflict is approved/overridden with the human's identity attached."""
    statuses = {c["key"]: c["status"] for c in replay["conflicts"]}
    _assert(
        all(s in ("approved", "overridden") for s in statuses.values()),
        f"[{label}] all conflicts resolved by a human {statuses}",
    )
    for conflict in replay["conflicts"]:
        res = conflict["resolution"]
        _assert(
            res["approved_by"] is not None,
            f"[{label}] {conflict['key']} approved_by set",
        )
        _assert(
            res["approved_at"] is not None,
            f"[{label}] {conflict['key']} approved_at set",
        )
    for key in expected_db["approved_by"]:
        _assert(
            expected_db["approved_by"][key] is not None,
            f"[DB:{label}] approved_by persisted for {key}",
        )


async def _assert_no_fks_broken(replay: dict, label: str):
    scenario_id = replay["scenario"]["id"]
    for conflict in replay["conflicts"]:
        _assert(
            len(conflict["turns"]) >= 1,
            f"[{label}] {conflict['key']} has >= 1 turn",
        )
        _assert(
            conflict["resolution"] is not None,
            f"[{label}] {conflict['key']} has resolution",
        )
        _assert(
            conflict["resolution"]["winning_side"] is not None
            or conflict["resolution"]["decision"] != "",
            f"[{label}] {conflict['key']} resolution has decision",
        )


async def run_phase(client_id: str | None, override_first: bool = False, timeout_check: bool = False):
    label = "override" if override_first else ("timeout" if timeout_check else "approve")
    if client_id is None:
        client_id = f"verify-{label}-{int(time.time() * 1000)}"
    print(f"\n=== Phase 1: RUN {label.upper()} (client_id={client_id}) ===\n")

    body = await _post_scenario(client_id)
    scenario_uuid = body["scenario_id"]
    _assert(body["client_id"] == client_id, "client_id echoed")
    _assert(body["status"] == "started", "status == started")

    if timeout_check:
        await asyncio.sleep(2.0)

    print("  Reading WS until all approval_needed...\n")
    debate_events = await _read_until(client_id, {"approval_needed": 3})
    live_turn_events = [e for e in debate_events if e.get("type") == "negotiation_turn"]
    print(f"  Received {len(debate_events)} debate events, {len(live_turn_events)} negotiation_turn chunks")
    _assert(
        len(live_turn_events) >= 12,
        f"live stream delivered >= 12 negotiation_turn chunks ({len(live_turn_events)})",
    )
    debate_types = {e.get("type") for e in debate_events}
    _assert(
        "approval_needed" in debate_types,
        "approval_needed present in live stream",
    )
    _assert(
        "briefing_ready" not in debate_types and "scenario_complete" not in debate_types,
        "pipeline genuinely paused — no briefing before human action",
    )

    print("\n  Checking pause state in DB...\n")
    db_paused = await _db_counts(scenario_uuid)
    print(f"  DB counts (paused): {json.dumps(db_paused, indent=2)}")
    _assert(
        db_paused["status"] == "awaiting_approval",
        f"scenario.status == awaiting_approval (got {db_paused['status']})",
    )
    _assert(
        set(db_paused["conflict_status"].values()) == {"awaiting_approval"},
        "all conflicts awaiting_approval",
    )
    _assert(db_paused["conflicts"] == 3, f"conflicts == 3 ({db_paused['conflicts']})")
    _assert(db_paused["resolutions"] == 3, f"resolutions == 3 ({db_paused['resolutions']})")
    _assert(
        db_paused["total_turns"] >= 12,
        f"total negotiation_turns >= 12 ({db_paused['total_turns']})",
    )
    _assert(db_paused["events"] > 0, f"events table has > 0 rows ({db_paused['events']})")

    if timeout_check:
        print("\n  Timeout safeguard window elapsed — re-reading state...\n")
        replay_paused = await _replay_get(scenario_uuid)
        timeout_types = [
            e["type"] for e in replay_paused["events"]
        ]
        _assert(
            timeout_types.count("approval_timeout") == 3,
            f"approval_timeout emitted for every stale conflict ({timeout_types.count('approval_timeout')})",
        )
        _assert(
            replay_paused["scenario"]["status"] == "awaiting_approval",
            "timeout never auto-approves — still awaiting_approval",
        )
        _assert(
            replay_paused["scenario"]["briefing"] is None,
            "timeout never auto-runs the briefing",
        )

    print("\n  Human acting via API...\n")
    keys = ["conflict_1", "conflict_2", "conflict_3"]
    result = None
    for key in keys:
        if override_first and key == "conflict_1":
            result = await _override(scenario_uuid, key)
        else:
            result = await _approve(scenario_uuid, key)
    print(f"  last action response: {result}")
    _assert(result["all_resolved"] is True, "last action reports all_resolved")

    print("\n  Reading resume stream (approvals, briefing, complete)...\n")
    resume_events = await _consume_ws(client_id)
    resume_types = [e.get("type") for e in resume_events]
    _assert(
        "scenario_complete" in resume_types,
        "resume stream ends with scenario_complete",
    )
    _assert(
        "briefing_ready" in resume_types,
        "briefing_ready present in resume stream",
    )
    _assert(
        ("resolution_approved" in resume_types) or ("resolution_overridden" in resume_types),
        "human-action event streamed",
    )
    total_live = len(debate_events) + len(resume_events)

    print("\n  Checking final DB state...\n")
    db = await _db_counts(scenario_uuid)
    print(f"  DB counts (final): {json.dumps(db, indent=2)}")
    _assert(db["status"] == "complete", f"scenario.status == complete (got {db['status']})")
    _assert(db["conflicts"] == 3, f"conflicts == 3 ({db['conflicts']})")
    _assert(db["resolutions"] == 3, f"resolutions == 3 ({db['resolutions']})")
    _assert(db["total_turns"] >= 12, f"total negotiation_turns >= 12 ({db['total_turns']})")
    await _assert_human_state(await _replay_get(scenario_uuid), db, "final")

    if override_first:
        _assert(
            db["decisions"]["conflict_1"] == OVERRIDE_DECISION,
            "Resolution.decision == human override (Arbiter decision replaced)",
        )
        _assert(
            db["approved_by"]["conflict_1"] is not None,
            "override approved_by persisted",
        )

    print("\n  Checking GET /scenarios/{id} replay consistency...\n")
    replay_uuid = await _replay_get(scenario_uuid)
    await _assert_replay_consistency(replay_uuid, db, "uuid")
    await _assert_no_fks_broken(replay_uuid, "uuid")

    if override_first and IS_STUB:
        cr = (replay_uuid["scenario"].get("briefing") or {}).get("conflict_resolution") or ""
        line1 = cr.split("conflict_1:")[1].split("/ conflict_2:")[0]
        _assert(OVERRIDE_DECISION in line1, "override decision (not Arbiter's) in final briefing")
        _assert(
            "Time-share the route" not in line1,
            "Arbiter decision absent from overridden conflict's briefing line",
        )
    elif not override_first:
        cr = (replay_uuid["scenario"].get("briefing") or {}).get("conflict_resolution") or ""
        _assert(bool(cr), "final briefing includes conflict_resolution")

    replay_client = await _replay_get(client_id)
    _assert(
        replay_client["scenario"]["id"] == scenario_uuid,
        f"GET /scenarios/{client_id} resolves to same uuid",
    )

    print("\n  Checking GET /scenarios (list)...\n")
    scenarios_list = await _list_scenarios()
    matches = [s for s in scenarios_list if s["id"] == scenario_uuid]
    _assert(len(matches) == 1, f"list contains our scenario ({len(matches)} matches)")
    _assert(
        matches[0]["status"] == "complete",
        f"list status == complete ({matches[0]['status']})",
    )

    print("\n  Checking idempotent POST (resumed)...\n")
    body2 = await _post_scenario(client_id)
    _assert(body2["resumed"] is True, "resumed == True on duplicate client_id")
    _assert(body2["scenario_id"] == scenario_uuid, "same scenario_id returned")

    state = {
        "client_id": client_id,
        "scenario_uuid": scenario_uuid,
        "events": db["events"],
        "total_turns": db["total_turns"],
        "conflicts": db["conflicts"],
        "resolutions": db["resolutions"],
        "conflict_status": db["conflict_status"],
        "approved_by": db["approved_by"],
    }
    STATE_FILE.write_text(json.dumps(state))
    print(f"\n  State saved to {STATE_FILE}")

    print(f"\n=== Phase 1 COMPLETE ({label.upper()}) ===\n")
    return state


async def replay_phase():
    print("\n=== Phase 2: REPLAY (restart survival) ===\n")
    state = json.loads(STATE_FILE.read_text())
    client_id = state["client_id"]
    scenario_uuid = state["scenario_uuid"]

    print("  GET /scenarios/{uuid}...\n")
    replay_uuid = await _replay_get(scenario_uuid)
    await _assert_replay_consistency(replay_uuid, state, "uuid")
    await _assert_human_state(replay_uuid, state, "uuid")
    await _assert_no_fks_broken(replay_uuid, "uuid")

    print("  GET /scenarios/{client_id}...\n")
    replay_client = await _replay_get(client_id)
    _assert(
        replay_client["scenario"]["id"] == scenario_uuid,
        "client_id resolves to correct uuid",
    )

    print("  Checking WS replay fallback...\n")
    ws_events = await _consume_ws(scenario_uuid)
    _assert(
        len(ws_events) == state["events"],
        f"ws replay delivered {len(ws_events)} events (expected {state['events']})",
    )
    _assert(
        ws_events[-1]["type"] == "scenario_complete",
        "ws replay ends with scenario_complete",
    )

    print("\n=== Phase 2 COMPLETE — Restart survival VERIFIED ===\n")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--override", action="store_true", help="override conflict_1 instead of approving")
    parser.add_argument("--timeout", action="store_true", help="exercise the approval-timeout safeguard")
    parser.add_argument("--id", default=None, help="client_id (for --run phase)")
    args = parser.parse_args()
    if not args.run and not args.replay:
        parser.error("Specify --run or --replay")
    if args.replay:
        await replay_phase()
    else:
        if args.timeout:
            window = float(os.getenv("APPROVAL_TIMEOUT_MINUTES", "15"))
            if window >= 1.0:
                print(f"NOTE: APPROVAL_TIMEOUT_MINUTES={window} is large; --timeout needs a tiny "
                      f"window (e.g. 0.01) set on the SERVER — restart with it set.")
        await run_phase(args.id, override_first=args.override, timeout_check=args.timeout)


if __name__ == "__main__":
    asyncio.run(main())