from typing import Literal

from pydantic import BaseModel

EventType = Literal[
    "agent_start",
    "agent_result",
    "negotiation_turn",
    "conflict_flagged",
    "resolution",
    "briefing_ready",
    "scenario_complete",
    "error",
]

ScenarioStatus = Literal["running", "complete", "error"]


class Event(BaseModel):
    type: EventType
    agent: str
    message: str
    data: dict | None = None
    timestamp: str


class ScenarioState(BaseModel):
    scenario_id: str
    raw_data: dict
    prediction: dict | None = None
    logistics_plan: dict | None = None
    conflicts: list[dict] = []
    negotiation_log: list[dict] = []
    resolution: dict | None = None
    briefing: dict | None = None
    events: list[dict] = []
    status: ScenarioStatus = "running"