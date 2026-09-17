"""Shared helpers for the AEGIS pytest E2E suites (not a test module)."""
import asyncio
import json
import uuid
from collections import defaultdict

import httpx
import websockets


def unique_sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def ws_url(base_url: str, scenario_id: str) -> str:
    return base_url.replace("http://", "ws://") + f"/ws/feed/{scenario_id}"


def post(base_url: str, path: str, json_body: dict | None = None):
    async def _post():
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(base_url + path, json=json_body)

    return asyncio.run(_post())


def get(base_url: str, path: str):
    async def _get():
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.get(base_url + path)

    return asyncio.run(_get())


def start_scenario(base_url: str, scenario_id: str, raw_data: dict) -> str:
    r = post(base_url, "/run-scenario", {"scenario_id": scenario_id, "raw_data": raw_data})
    assert r.status_code == 200, r.text
    return r.json()["scenario_id"]


async def _feed_until(scenario_id: str, base_url: str, predicate, timeout: float = 90.0) -> list[dict]:
    """Open the WS feed and collect events until ``predicate`` is true or timeout.

    Returns (events, hit_timeout). The feed closes on terminal events; we stop
    reading on those too so a scenario that wrongly completes early is caught.
    """
    events: list[dict] = []
    async with websockets.connect(ws_url(base_url, scenario_id)) as ws:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return events, True
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return events, True
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "scenario_complete" or (
                ev.get("type") == "error" and ev.get("agent") == "pipeline"
            ):
                return events, False
            if predicate(events):
                return events, False


async def read_debate(base_url: str, scenario_id: str, conflicts: int = 3, timeout: float = 90.0) -> list[dict]:
    """Read the WS feed until every expected conflict has an approval_needed."""
    approved = set()

    def done(events: list[dict]) -> bool:
        for ev in events:
            if ev.get("type") == "approval_needed":
                cid = (ev.get("data") or {}).get("conflict_id")
                if cid:
                    approved.add(cid)
        return len(approved) >= conflicts

    events, hit_timeout = await _feed_until(scenario_id, base_url, done, timeout=timeout)
    if hit_timeout:
        raise AssertionError(
            f"debate did not reach {conflicts} approval_needed for {scenario_id}; "
            f"saw {sorted(approved)}; feed end hits_timeout"
        )
    return events


async def read_completion(base_url: str, scenario_id: str, timeout: float = 90.0) -> list[dict]:
    """Read a feed from (re)connect until scenario_complete (or pipeline error)."""
    events, hit_timeout = await _feed_until(
        scenario_id, base_url, lambda _: False, timeout=timeout
    )
    if hit_timeout:
        raise AssertionError(f"no scenario_complete within {timeout}s for {scenario_id}")
    return events


def approve_all(base_url: str, scenario_id: str, approved_by: str = "pytest-operator") -> list[dict]:
    """Approve every conflict of a scenario (keys from approval_needed events)."""
    replay = get(base_url, f"/scenarios/{scenario_id}")
    assert replay.status_code == 200, replay.text
    keys = [c["key"] for c in replay.json()["conflicts"] if c["status"] != "approved"]
    responses = []
    for key in keys:
        r = post(
            base_url,
            f"/scenarios/{scenario_id}/conflicts/{key}/approve",
            {"approved_by": approved_by},
        )
        assert r.status_code == 200, r.text
        responses.append(r.json())
    return responses


def event_counts(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for ev in events:
        counts[ev["type"]] += 1
    return dict(counts)


def negotiation_turn_groups(events: list[dict]) -> dict[str, list[dict]]:
    """conflict_id -> negotiation_turn events in feed order."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        if ev["type"] == "negotiation_turn":
            cid = (ev.get("data") or {}).get("conflict_id")
            if cid:
                groups[cid].append(ev)
    return dict(groups)