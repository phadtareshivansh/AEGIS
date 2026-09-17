"""DB-drop resilience semantics, in-process against the real test database.

Contract exercised here: transient failures are invisible (retried), permanent
failures surface as a terminal ``pipeline`` error that marks the scenario
'error', is pushed to the live queue, and deregisters it — so a websocket can
never hang on a queue that will never be written again.
"""
import asyncio

import pytest

from db.repo import call_with_retry, insert_scenario, persist_update
from testutils import unique_sid

FLOOD = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}


@pytest.fixture
def seeded_scenario(loop, test_db):
    async def _seed():
        row = await insert_scenario(unique_sid("resilience"), dict(FLOOD))
        return row

    return loop.run_until_complete(_seed())


def test_call_with_retry_hides_transient_failures(loop):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("connection reset")
        return "ok"

    result = loop.run_until_complete(
        call_with_retry(flaky, attempts=3, base_backoff=0.01)
    )
    assert result == "ok"
    assert calls["n"] == 3


def test_call_with_retry_raises_after_exhaustion(loop):
    calls = {"n": 0}

    async def always_fails():
        calls["n"] += 1
        raise OSError("db down")

    with pytest.raises(OSError):
        loop.run_until_complete(call_with_retry(always_fails, attempts=4, base_backoff=0.01))
    assert calls["n"] == 4


def test_persist_update_retries_transient_db_failure(loop, seeded_scenario, monkeypatch):
    import db.repo as repo

    real = repo._persist_update
    n = {"calls": 0}

    async def flaky_inner(*args, **kwargs):
        n["calls"] += 1
        if n["calls"] <= 2:
            raise OSError("connection lost mid-write")
        return await real(*args, **kwargs)

    monkeypatch.setattr(repo, "_persist_update", flaky_inner)

    update = {
        "prediction": {"at_risk_zones": [], "summary": "transient survived"},
    }
    loop.run_until_complete(persist_update(seeded_scenario.id, update))

    assert n["calls"] == 3

    async def _read():
        from db.repo import get_scenario

        row = await get_scenario(str(seeded_scenario.id))
        return row.prediction

    assert loop.run_until_complete(_read()) == update["prediction"]


def test_mark_scenario_error_persists_status_and_event(loop, seeded_scenario):
    from db.repo import get_scenario, mark_scenario_error, replay_scenario

    ok = loop.run_until_complete(
        mark_scenario_error(seeded_scenario.id, "pipeline exploded")
    )
    assert ok is True

    async def _verify():
        row = await get_scenario(str(seeded_scenario.id))
        replay = await replay_scenario(str(seeded_scenario.id))
        return row.status, replay

    status, replay = loop.run_until_complete(_verify())
    assert status == "error"
    error_events = [
        ev for ev in replay["events"] if ev["type"] == "error" and ev["agent"] == "pipeline"
    ]
    assert len(error_events) == 1
    assert error_events[0]["message"] == "pipeline exploded"


def test_fail_scenario_is_terminal_no_hang_and_delivers_alert(loop, seeded_scenario):
    """Simulate the mid-scenario DB outage path: the live queue must get the
    terminal pipeline error, the queue must be deregistered, and Postgres must
    record the error state — all without raising."""
    from main import _fail_scenario, event_queues

    sid = seeded_scenario.client_id
    queue = asyncio.Queue()
    event_queues[sid] = queue

    loop.run_until_complete(_fail_scenario(str(seeded_scenario.id), sid, queue, "db gone"))

    ev = loop.run_until_complete(queue.get())
    assert ev["type"] == "error"
    assert ev["agent"] == "pipeline"

    assert sid not in event_queues

    async def _verify():
        from db.repo import replay_scenario

        replay = await replay_scenario(sid)
        return replay

    replay = loop.run_until_complete(_verify())
    assert replay["scenario"]["status"] == "error"
    assert any(ev["type"] == "error" and ev["agent"] == "pipeline" for ev in replay["events"])


def test_is_terminal_semantics():
    from main import _is_terminal

    assert _is_terminal({"type": "scenario_complete", "agent": "briefing"}) is True
    assert _is_terminal({"type": "error", "agent": "pipeline"}) is True
    # Per-conflict negotiator fallback is NOT a feed terminator.
    assert _is_terminal({"type": "error", "agent": "negotiator"}) is False
    assert _is_terminal({"type": "approval_needed", "agent": "negotiator"}) is False
    assert _is_terminal({"type": "negotiation_turn", "agent": "evacuation_advocate"}) is False