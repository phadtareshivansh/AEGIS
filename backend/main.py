import asyncio
import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.pipeline import build_graph
from schemas import ScenarioState

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

active_scenarios: dict[str, ScenarioState] = {}
event_queues: dict[str, asyncio.Queue] = {}


class RunScenarioRequest(BaseModel):
    scenario_id: str
    raw_data: dict


@app.get("/health")
def health():
    return {"status": "ok"}


async def _run_pipeline(scenario_id: str, queue: asyncio.Queue) -> None:
    graph = build_graph()
    state = active_scenarios[scenario_id]
    seen: set[str] = set()
    try:
        async for mode, chunk in graph.astream(state, stream_mode=["updates", "custom"]):
            if mode == "custom":
                await queue.put(chunk["event"])
            elif mode == "updates":
                for update in chunk.values():
                    for event in update.get("events", []):
                        key = json.dumps(event, sort_keys=True)
                        if key in seen:
                            continue
                        seen.add(key)
                        await queue.put(event)
                    if update.get("status"):
                        state.status = update["status"]
    except Exception as exc:
        await queue.put(
            {
                "type": "error",
                "agent": "pipeline",
                "message": f"[stub] pipeline failed: {type(exc).__name__}: {exc}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
    finally:
        await queue.put(
            {
                "type": "scenario_complete",
                "agent": "pipeline",
                "message": f"pipeline exited for scenario {scenario_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        active_scenarios[scenario_id] = state


@app.post("/run-scenario")
async def run_scenario(request: RunScenarioRequest):
    queue: asyncio.Queue = asyncio.Queue()
    event_queues[request.scenario_id] = queue
    active_scenarios[request.scenario_id] = ScenarioState(
        scenario_id=request.scenario_id,
        raw_data=request.raw_data,
    )
    asyncio.create_task(_run_pipeline(request.scenario_id, queue))
    return {"scenario_id": request.scenario_id, "status": "started"}


@app.websocket("/ws/feed/{scenario_id}")
async def ws_feed(websocket: WebSocket, scenario_id: str):
    await websocket.accept()
    if scenario_id not in event_queues:
        await websocket.send_json(
            {
                "type": "error",
                "agent": "system",
                "message": "scenario not found",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        await websocket.close(code=1008)
        return

    queue = event_queues[scenario_id]
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event.get("type") == "scenario_complete":
                break
        await websocket.close()
    except WebSocketDisconnect:
        return