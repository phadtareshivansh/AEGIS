import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from agents.pipeline import build_debate_graph, build_briefing_graph
from agents.sensing import (
    geocode,
    list_live_locations,
    probe_location,
)
from db.models import Conflict, Scenario
from db.repo import (
    AWAITING_APPROVAL,
    DuplicateClientId,
    append_event,
    call_with_retry,
    get_final_resolutions,
    get_scenario as repo_get_scenario,
    insert_scenario,
    list_scenarios,
    mark_scenario_error,
    maybe_emit_approval_timeouts,
    persist_update,
    replay_scenario,
    resolve_conflict,
    set_scenario_status,
    try_begin_resume,
)
from db.session import async_session
from schemas import ScenarioState
from sqlalchemy import select

app = FastAPI(title="AEGIS")

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Streaming transport only. Scenario state and the event log live in Postgres;
# a fresh websocket after a restart replays stored events instead of this map.
event_queues: dict[str, asyncio.Queue] = {}

# In-memory one-shot watchdog per scenario (client_id key).
# Each task sleeps for the approval window then fires lazy timeout events.
approval_timers: dict[str, asyncio.Task] = {}

APPROVAL_TIMEOUT_MINUTES = float(os.getenv("APPROVAL_TIMEOUT_MINUTES", "15"))


REQUIRED_RAW_KEYS = ("rainfall_mm_24h", "river_level_m", "river_level_danger_threshold_m")


class RunScenarioRequest(BaseModel):
    scenario_id: str
    raw_data: dict

    @field_validator("raw_data")
    @classmethod
    def require_hydrology_fields(cls, v: dict) -> dict:
        missing = set(REQUIRED_RAW_KEYS) - set(v)
        if missing:
            raise ValueError(f"raw_data missing required keys: {sorted(missing)}")
        return v


class ApproveRequest(BaseModel):
    approved_by: str


class OverrideRequest(BaseModel):
    approved_by: str
    override_reason: str
    override_decision: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/locations")
async def get_locations():
    """Curated quick picks (plain coordinates, no fabricated rating curves)."""
    return list_live_locations()


@app.get("/geocode")
async def get_geocode(q: str = ""):
    """Free-text geocoding via keyless Open-Meteo. Empty list => no match."""
    if not q.strip():
        return []
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await geocode(q.strip(), client=client)


@app.get("/live/preview")
async def get_live_preview(lat: float = 0, lon: float = 0):
    """Pre-flight a location: rainfall + hydrology availability WITHOUT running
    a scenario, so the picker can show a status badge before Run."""
    if not lat or not lon:
        return JSONResponse(status_code=400, content={"detail": "lat and lon are required"})
    async with httpx.AsyncClient(timeout=20.0) as client:
        result = await probe_location(lat, lon, client=client)
    if not result.get("ok"):
        return JSONResponse(
            status_code=502, content={"detail": result.get("error", "sensing unavailable")}
        )
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Two-phase pipeline ----------------------------------------------------


async def _fail_scenario(
    scenario_uuid: str, client_id: str, queue: asyncio.Queue, message: str
) -> None:
    """Terminal, no-hang failure path for a scenario.

    Best-effort persists status='error' + an error event (isolated retried
    session), delivers the error event to the live queue so the connected
    websocket sees it, and deregisters the queue. The WS feed breaks on this
    terminal event, so a scenario NEVER hangs silently awaiting a queue that
    will never be written again. If the DB is fully unreachable the in-memory
    queue alert is still the source of truth (the error event is delivered
    regardless of persistence success).
    """
    try:
        await mark_scenario_error(scenario_uuid, message)
    except Exception:
        pass
    try:
        await queue.put(
            {
                "type": "error",
                "agent": "pipeline",
                "message": message,
                "timestamp": _now(),
            }
        )
    except Exception:
        pass
    event_queues.pop(client_id, None)


async def _run_debate(
    scenario_uuid: str,
    client_id: str,
    raw_data: dict,
    queue: asyncio.Queue,
) -> None:
    """Phase 1 — autonomous analysis + negotiation, ends at awaiting_approval."""
    graph = build_debate_graph()
    state = ScenarioState(scenario_id=client_id, raw_data=raw_data)
    seen: set[str] = set()
    try:
        async for mode, chunk in graph.astream(state, stream_mode=["updates", "custom"]):
            if mode == "custom":
                await queue.put(chunk["event"])
                continue
            for key, update in chunk.items():
                update = update or {}
                try:
                    await persist_update(scenario_uuid, update)
                except Exception as exc:
                    await _fail_scenario(
                        scenario_uuid, client_id, queue,
                        f"persistence failed: {type(exc).__name__}: {exc}",
                    )
                    return
                for event in update.get("events", []):
                    dedup_key = json.dumps(event, sort_keys=True)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    try:
                        await append_event(scenario_uuid, event)
                    except Exception as exc:
                        await _fail_scenario(
                            scenario_uuid, client_id, queue,
                            f"event write failed: {type(exc).__name__}: {exc}",
                        )
                        return
                    await queue.put(event)
    except Exception as exc:
        await _fail_scenario(
            scenario_uuid, client_id, queue,
            f"pipeline failed: {type(exc).__name__}: {exc}",
        )
        return

    # Debate phase complete — decide next step.
    async def _load_has_conflicts() -> bool:
        async with async_session() as session:
            conflicts = (
                await session.execute(
                    select(Conflict).where(Conflict.scenario_id == scenario_uuid)
                )
            ).scalars().all()
            return len(conflicts) > 0

    try:
        has_conflicts = await call_with_retry(_load_has_conflicts)
    except Exception as exc:
        await _fail_scenario(
            scenario_uuid, client_id, queue,
            f"conflict re-check failed after retries: {type(exc).__name__}: {exc}",
        )
        return

    if not has_conflicts:
        await _run_briefing(scenario_uuid, client_id, queue)
        return

    await call_with_retry(lambda: set_scenario_status(scenario_uuid, AWAITING_APPROVAL))
    task = asyncio.create_task(
        _approval_timer(client_id, scenario_uuid, queue)
    )
    approval_timers[client_id] = task


async def _approval_timer(
    client_id: str, scenario_uuid: str, queue: asyncio.Queue
) -> None:
    """One-shot watchdog — emit approval_timeout events if no human acts."""
    await asyncio.sleep(APPROVAL_TIMEOUT_MINUTES * 60)
    try:
        emitted = await maybe_emit_approval_timeouts(
            scenario_uuid, APPROVAL_TIMEOUT_MINUTES
        )
        for ev in emitted:
            ev["type"] = "approval_timeout"
            ev["agent"] = "negotiator"
            ev["message"] = (
                f"Approval pending for {ev['minutes']:.0f} min on "
                f"{ev['conflict_id']} — no decision yet"
            )
            ev["timestamp"] = _now()
            await queue.put(ev)
    except Exception:
        pass
    finally:
        approval_timers.pop(client_id, None)


async def _run_briefing(
    scenario_uuid: str, client_id: str, queue: asyncio.Queue
) -> None:
    """Phase 2 — all conflicts approved/overridden, briefing produced, done."""
    approval_timers.pop(client_id, None)
    graph = build_briefing_graph()

    async def _load_briefing_context():
        async with async_session() as session:
            scenario = await session.get(Scenario, scenario_uuid)
            if scenario is None:
                return None, None, None
            return (
                dict(scenario.raw_data),
                dict(scenario.prediction or {}),
                dict(scenario.logistics_plan or {}),
            )

    try:
        raw_data, prediction, logistics_plan = await call_with_retry(
            _load_briefing_context
        )
        resolution = await call_with_retry(
            lambda: get_final_resolutions(scenario_uuid)
        )
    except Exception as exc:
        await _fail_scenario(
            scenario_uuid, client_id, queue,
            f"briefing load failed after retries: {type(exc).__name__}: {exc}",
        )
        return

    if raw_data is None:
        event_queues.pop(client_id, None)
        return

    state = ScenarioState(
        scenario_id=client_id,
        raw_data=raw_data,
        prediction=prediction,
        logistics_plan=logistics_plan,
        resolution=resolution,
    )
    seen: set[str] = set()
    try:
        async for mode, chunk in graph.astream(state, stream_mode=["updates"]):
            for key, update in chunk.items():
                update = update or {}
                try:
                    await persist_update(scenario_uuid, update)
                except Exception as exc:
                    await _fail_scenario(
                        scenario_uuid, client_id, queue,
                        f"briefing persistence failed: {type(exc).__name__}: {exc}",
                    )
                    return
                for event in update.get("events", []):
                    dedup_key = json.dumps(event, sort_keys=True)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    try:
                        await append_event(scenario_uuid, event)
                    except Exception as exc:
                        await _fail_scenario(
                            scenario_uuid, client_id, queue,
                            f"event write failed: {type(exc).__name__}: {exc}",
                        )
                        return
                    await queue.put(event)
    except Exception as exc:
        await _fail_scenario(
            scenario_uuid, client_id, queue,
            f"briefing failed: {type(exc).__name__}: {exc}",
        )
        return
    finally:
        event_queues.pop(client_id, None)


# ---- HTTP endpoints ----------------------------------------------------------


@app.post("/run-scenario")
async def run_scenario(request: RunScenarioRequest):
    client_id = request.scenario_id
    try:
        row = await insert_scenario(client_id, request.raw_data)
    except DuplicateClientId:
        row = await repo_get_scenario(client_id)
        return {
            "scenario_id": str(row.id),
            "client_id": client_id,
            "status": row.status,
            "resumed": True,
        }

    queue: asyncio.Queue = asyncio.Queue()
    event_queues[client_id] = queue
    asyncio.create_task(
        _run_debate(str(row.id), client_id, request.raw_data, queue)
    )
    return {
        "scenario_id": str(row.id),
        "client_id": client_id,
        "status": "started",
        "resumed": False,
    }


@app.get("/scenarios")
async def get_scenarios():
    return await list_scenarios()


@app.get("/scenarios/{scenario_id}")
async def scenario_detail(scenario_id: str):
    replay = await replay_scenario(scenario_id)
    if replay is None:
        return JSONResponse(status_code=404, content={"detail": "scenario not found"})
    return replay


@app.post("/scenarios/{scenario_id}/conflicts/{conflict_id}/approve")
async def approve_conflict(scenario_id: str, conflict_id: str, body: ApproveRequest):
    scenario = await repo_get_scenario(scenario_id)
    if scenario is None:
        return JSONResponse(status_code=404, content={"detail": "scenario not found"})
    try:
        result = await resolve_conflict(
            scenario.id, conflict_id, approved_by=body.approved_by
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "conflict not found"})

    approval_event = {
        "type": "resolution_approved",
        "agent": "negotiator",
        "message": f"Approved by {body.approved_by}: {result['decision']}",
        "data": result,
        "timestamp": _now(),
    }
    try:
        await append_event(scenario.id, approval_event)
    except Exception:
        pass

    await _maybe_resume(scenario, result, queue_events=[approval_event])
    return result


@app.post("/scenarios/{scenario_id}/conflicts/{conflict_id}/override")
async def override_conflict(scenario_id: str, conflict_id: str, body: OverrideRequest):
    scenario = await repo_get_scenario(scenario_id)
    if scenario is None:
        return JSONResponse(status_code=404, content={"detail": "scenario not found"})
    try:
        result = await resolve_conflict(
            scenario.id,
            conflict_id,
            approved_by=body.approved_by,
            override_reason=body.override_reason,
            override_decision=body.override_decision,
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "conflict not found"})

    approval_event = {
        "type": "resolution_overridden",
        "agent": "negotiator",
        "message": f"Overridden by {body.approved_by}: {body.override_decision}",
        "data": result,
        "timestamp": _now(),
    }
    try:
        await append_event(scenario.id, approval_event)
    except Exception:
        pass

    await _maybe_resume(scenario, result, queue_events=[approval_event])
    return result


async def _push_to_queue(scenario, event: dict) -> None:
    """Deliver one event to the scenario's live queue, reattaching it if the
    backend restarted while the scenario sat awaiting approval."""
    queue = event_queues.get(scenario.client_id)
    if queue is None:
        queue = asyncio.Queue()
        event_queues[scenario.client_id] = queue
    await queue.put(event)
    return queue


async def _maybe_resume(scenario, result: dict, queue_events: list[dict]) -> None:
    """Emit the human-action event(s), then resume the briefing phase exactly
    once the moment the last conflict is resolved."""
    queue = None
    for event in queue_events:
        queue = await _push_to_queue(scenario, event)

    if not result["all_resolved"]:
        return

    if not await try_begin_resume(scenario.id):
        return  # another request already resumed

    approval_timers.pop(scenario.client_id, None)
    queue = queue or event_queues.get(scenario.client_id)
    if queue is None:
        queue = asyncio.Queue()
        event_queues[scenario.client_id] = queue
    asyncio.create_task(_run_briefing(str(scenario.id), scenario.client_id, queue))


# ---- WebSocket feed ----------------------------------------------------------


def _is_terminal(event: dict) -> bool:
    """Whether an event closes the live feed.

    scenario_complete closes a healthy pipeline; an ``error`` raised by the
    pipeline (not the per-conflict negotiator fallback, whose agent is
    "negotiator") closes a fatally-failed one. Both prevent the websocket
    from hanging silently on a queue that will produce nothing more.
    """
    if event.get("type") == "scenario_complete":
        return True
    return event.get("type") == "error" and event.get("agent") == "pipeline"


@app.websocket("/ws/feed/{scenario_id}")
async def ws_feed(websocket: WebSocket, scenario_id: str):
    await websocket.accept()

    queue = event_queues.get(scenario_id)
    if queue is None:
        row = await repo_get_scenario(scenario_id)
        if row is not None:
            queue = event_queues.get(row.client_id)

    if queue is None:
        replay = await replay_scenario(scenario_id)
        if replay is None:
            await websocket.send_json(
                {
                    "type": "error",
                    "agent": "system",
                    "message": "scenario not found",
                    "timestamp": _now(),
                }
            )
            await websocket.close(code=1008)
            return
        sent_complete = False
        for event in replay["events"]:
            await websocket.send_json(event)
            if event.get("type") == "scenario_complete":
                sent_complete = True
        # Only append a synthetic terminator for scenarios that are truly
        # done.  A scenario sitting in ``awaiting_approval`` should stay
        # interactive on reconnect — no fake complete.
        if not sent_complete and replay["scenario"]["status"] == "complete":
            await websocket.send_json(
                {
                    "type": "scenario_complete",
                    "agent": "pipeline",
                    "message": "replay complete",
                    "timestamp": _now(),
                }
            )
        await websocket.close()
        return

    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if _is_terminal(event):
                break
        await websocket.close()
    except WebSocketDisconnect:
        return
