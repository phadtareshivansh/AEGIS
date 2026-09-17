import operator
from typing import Annotated, Literal

from pydantic import BaseModel

EventType = Literal[
    "agent_start",
    "agent_result",
    "negotiation_turn",
    "conflict_flagged",
    "resolution",
    "policy_disagreement",
    "approval_needed",
    "approval_timeout",
    "resolution_approved",
    "resolution_overridden",
    "briefing_ready",
    "scenario_complete",
    "error",
]

ScenarioStatus = Literal["running", "awaiting_approval", "complete", "error"]


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
    simulation: dict | None = None
    events: Annotated[list[dict], operator.add] = []
    status: ScenarioStatus = "running"