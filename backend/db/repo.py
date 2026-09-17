import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from db.models import Conflict, Event, NegotiationTurn, Resolution, Scenario
from db.session import async_session

PENDING_NEGOTIATION = "pending_negotiation"
NEGOTIATED = "negotiated"
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
OVERRIDDEN = "overridden"

RESOLVED_STATUSES = (APPROVED, OVERRIDDEN)

#: Network/transient failures worth retrying with backoff. asyncpg surfaces
#: connection drops as OSError or OperationalError; timeouts are asyncio.TimeoutError.
RETRYABLE_EXCEPTIONS = (SQLAlchemyError, OSError, asyncio.TimeoutError)


class DuplicateClientId(Exception):
    pass


async def call_with_retry(
    fn, *, attempts: int = 3, base_backoff: float = 0.2
):
    """Run an async DB-bound callable, retrying transient failures.

    Exponential backoff between attempts (base_backoff * 2**n). The pipeline
    uses this around every persistence write so a momentary Postgres blip does
    not tear down a scenario — only a permanent outage surfaces (after the
    last attempt) to the caller, which then marks the scenario errored.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(base_backoff * (2**attempt))
    raise last_exc  # type: ignore[misc]


async def _ensure_conflicts(session, scenario_id: uuid.UUID, conflicts: list[dict]) -> None:
    for conflict in conflicts:
        stmt = (
            pg_insert(Conflict)
            .values(
                scenario_id=scenario_id,
                key=conflict["id"],
                type=conflict["type"],
                resource=conflict["resource"],
                claim_a=conflict["claim_a"],
                claim_b=conflict["claim_b"],
                status=PENDING_NEGOTIATION,
            )
            .on_conflict_do_nothing(index_elements=["scenario_id", "key"])
        )
        await session.execute(stmt)


async def insert_scenario(client_id: str, raw_data: dict) -> Scenario:
    async with async_session() as session:
        scenario = Scenario(
            client_id=client_id,
            raw_data=raw_data,
            data_mode=raw_data.get("data_mode", "demo"),
            location_key=raw_data.get("location_key"),
        )
        session.add(scenario)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateClientId(client_id) from exc
        await session.refresh(scenario)
        return scenario


async def get_scenario(id_ref: str) -> Scenario | None:
    """Resolve ``id_ref`` as a UUID primary key or a stable client_id."""
    async with async_session() as session:
        try:
            as_uuid = uuid.UUID(str(id_ref))
        except (ValueError, TypeError):
            as_uuid = None
        if as_uuid is not None:
            row = await session.get(Scenario, as_uuid)
            if row is not None:
                return row
        row = await session.execute(
            select(Scenario).where(Scenario.client_id == str(id_ref))
        )
        return row.scalar_one_or_none()


async def list_scenarios() -> list[dict]:
    latest_message = (
        select(Event.message)
        .where(Event.scenario_id == Scenario.id)
        .order_by(Event.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    async with async_session() as session:
        rows = (
            await session.execute(
                select(
                    Scenario.id,
                    Scenario.client_id,
                    Scenario.status,
                    Scenario.created_at,
                    Scenario.data_mode,
                    Scenario.location_key,
                    Scenario.briefing,
                    latest_message.label("latest_message"),
                )
                .order_by(Scenario.created_at.desc())
                .limit(200)
            )
        ).all()
        return [
            {
                "id": str(row.id),
                "client_id": row.client_id,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "data_mode": row.data_mode,
                "location_key": row.location_key,
                "summary": (
                    (row.briefing or {}).get("headline") or row.latest_message or row.status
                ),
            }
            for row in rows
        ]


async def append_event(scenario_id: uuid.UUID, event: dict) -> None:
    await call_with_retry(lambda: _append_event(scenario_id, event))


async def _append_event(scenario_id: uuid.UUID, event: dict) -> None:
    async with async_session() as session:
        session.add(
            Event(
                scenario_id=scenario_id,
                type=event["type"],
                agent=event.get("agent", ""),
                message=event.get("message", ""),
                data=event.get("data"),
            )
        )
        await session.commit()


async def persist_update(scenario_id: uuid.UUID, update: dict) -> None:
    await call_with_retry(lambda: _persist_update(scenario_id, update))


async def _persist_update(scenario_id: uuid.UUID, update: dict) -> None:
    """Persist the structural outputs of one graph node update.

    Streaming ``negotiation_turn`` chunks never reach this function — they are
    transport-only. The final accumulated turn text lands here via
    ``negotiation_log`` (partial transcripts included: whatever turns actually
    completed are written as-is, never fabricated up to 4).
    """
    async with async_session() as session:
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            return

        changed = False
        if update.get("prediction"):
            scenario.prediction = update["prediction"]
            changed = True
        if update.get("logistics_plan"):
            scenario.logistics_plan = update["logistics_plan"]
            changed = True
        if update.get("briefing"):
            scenario.briefing = update["briefing"]
            changed = True
        if update.get("status"):
            scenario.status = update["status"]
            changed = True

        conflicts = update.get("conflicts")
        if conflicts:
            await _ensure_conflicts(session, scenario_id, conflicts)
            changed = True

        negotiation_log = update.get("negotiation_log")
        if negotiation_log:
            for entry in negotiation_log:
                await _persist_negotiation(session, scenario_id, entry)
            changed = True

        if changed:
            await session.commit()


async def mark_scenario_error(scenario_id: uuid.UUID, message: str) -> bool:
    """Record a fatal pipeline failure: status='error' + an error event.

    Uses its own isolated (and internally retried) session so it can still
    succeed even when the write that triggered the failure did not. Returns
    whether the error state was persisted (False => DB was fully unreachable,
    the caller must rely on the in-memory queue alert alone).
    """
    async def _mark() -> None:
        async with async_session() as session:
            scenario = await session.get(Scenario, scenario_id)
            if scenario is None:
                return
            scenario.status = "error"
            session.add(
                Event(
                    scenario_id=scenario_id,
                    type="error",
                    agent="pipeline",
                    message=message,
                    data={"error_state": True},
                )
            )
            await session.commit()

    try:
        await call_with_retry(_mark)
        return True
    except RETRYABLE_EXCEPTIONS:
        return False


async def _persist_negotiation(session, scenario_id: uuid.UUID, entry: dict) -> None:
    key = entry["conflict_id"]
    result = (
        (
            await session.execute(
                select(Conflict).where(
                    Conflict.scenario_id == scenario_id, Conflict.key == key
                )
            )
        )
        .scalars()
        .first()
    )
    if result is None:
        return
    conflict = result
    conflict.status = AWAITING_APPROVAL

    transcript = entry.get("transcript") or []
    for turn in transcript:
        stmt = (
            pg_insert(NegotiationTurn)
            .values(
                conflict_id=conflict.id,
                turn_number=turn["turn"],
                agent=turn["agent"],
                message=turn["text"],
            )
            .on_conflict_do_update(
                constraint="uq_turns_conflict_turn",
                set_={"agent": turn["agent"], "message": turn["text"]},
            )
        )
        await session.execute(stmt)

    resolution = entry.get("resolution") or {}
    if resolution:
        stmt = (
            pg_insert(Resolution)
            .values(
                conflict_id=conflict.id,
                decision=resolution.get("decision", ""),
                justification=resolution.get("justification", ""),
                winning_side=resolution.get("winning_side"),
            )
            .on_conflict_do_update(
                constraint="resolutions_conflict_id_key",
                set_={
                    "decision": resolution.get("decision", ""),
                    "justification": resolution.get("justification", ""),
                    "winning_side": resolution.get("winning_side"),
                },
            )
        )
        await session.execute(stmt)


async def set_scenario_status(scenario_id: uuid.UUID, status: str) -> None:
    async with async_session() as session:
        scenario = await session.get(Scenario, scenario_id)
        if scenario is not None:
            scenario.status = status
            await session.commit()


async def try_begin_resume(scenario_id: uuid.UUID) -> bool:
    """Atomically flip awaiting_approval -> running as the resume trigger.

    Returns True only for the caller that wins the transition, so concurrent
    idempotent approve/override calls cannot spawn the briefing phase twice.
    """
    from sqlalchemy import update

    async with async_session() as session:
        result = await session.execute(
            update(Scenario)
            .where(
                Scenario.id == scenario_id,
                Scenario.status == AWAITING_APPROVAL,
            )
            .values(status="running")
        )
        await session.commit()
        return result.rowcount > 0


async def get_final_resolutions(scenario_id: uuid.UUID) -> dict[str, dict]:
    """Conflict key -> final resolution dict for the briefing phase.

    The stored decision is authoritative — a human override has already
    replaced the Arbiter's text at resolution time.
    """
    payload: dict[str, dict] = {}
    async with async_session() as session:
        conflicts = (
            await session.execute(
                select(Conflict).where(Conflict.scenario_id == scenario_id)
            )
        ).scalars().all()
        for conflict in conflicts:
            resolution = (
                await session.execute(
                    select(Resolution).where(Resolution.conflict_id == conflict.id)
                )
            ).scalar_one_or_none()
            payload[conflict.key] = {
                "decision": resolution.decision if resolution else "",
                "justification": resolution.justification if resolution else "",
                "winning_side": resolution.winning_side if resolution else None,
            }
    return payload


async def _conflict_and_resolution(session, scenario_id: uuid.UUID, conflict_ref: str):
    """Resolve a conflict by UUID or by its stable key within a scenario."""
    conflict = None
    try:
        as_uuid = uuid.UUID(str(conflict_ref))
    except (ValueError, TypeError):
        as_uuid = None
    if as_uuid is not None:
        conflict = (
            await session.execute(
                select(Conflict).where(
                    Conflict.id == as_uuid, Conflict.scenario_id == scenario_id
                )
            )
        ).scalar_one_or_none()
    if conflict is None:
        conflict = (
            await session.execute(
                select(Conflict).where(
                    Conflict.scenario_id == scenario_id, Conflict.key == str(conflict_ref)
                )
            )
        ).scalar_one_or_none()
    if conflict is None:
        return None, None
    resolution = (
        await session.execute(
            select(Resolution).where(Resolution.conflict_id == conflict.id)
        )
    ).scalar_one_or_none()
    return conflict, resolution


async def resolve_conflict(
    scenario_id: uuid.UUID,
    conflict_ref: str,
    *,
    approved_by: str,
    override_reason: str | None = None,
    override_decision: str | None = None,
) -> dict | None:
    """Transition one conflict to approved/overridden (with human fields).

    Idempotent by direction: approve->approve and override->override return the
    existing state; approve->override and override->approve raise ValueError
    (409 by the caller). Returns the updated conflict payload plus whether the
    whole scenario is now resolved. ``None`` if the scenario/conflict is unknown.
    """
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by is required")
    if override_reason is None and override_decision is None:
        status = APPROVED
    else:
        if not (override_reason and override_decision and override_reason.strip() and override_decision.strip()):
            raise ValueError("override requires both override_reason and override_decision")
        status = OVERRIDDEN

    async with async_session() as session:
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            return None
        conflict, resolution = await _conflict_and_resolution(session, scenario_id, conflict_ref)
        if conflict is None:
            return None

        if conflict.status == status:
            pass  # idempotent re-apply
        elif conflict.status in (APPROVED, OVERRIDDEN):
            raise ValueError(
                f"conflict already {conflict.status}; cannot switch to {status}"
            )
        elif conflict.status not in (AWAITING_APPROVAL, NEGOTIATED):
            raise ValueError(
                f"conflict in state {conflict.status!r} cannot be {status}"
            )

        now = datetime.now(timezone.utc)
        if resolution is None:
            resolution = Resolution(
                conflict_id=conflict.id,
                decision="",
                justification="",
                winning_side=None,
            )
            session.add(resolution)
        if status == APPROVED:
            resolution.approved_by = approved_by
            resolution.approved_at = now
        else:
            resolution.decision = override_decision
            resolution.justification = resolution.justification or override_reason
            resolution.winning_side = None
            resolution.approved_by = approved_by
            resolution.approved_at = now
            resolution.override_reason = override_reason
        conflict.status = status

        all_conflicts = (
            await session.execute(
                select(Conflict).where(Conflict.scenario_id == scenario_id)
            )
        ).scalars().all()
        all_resolved = all(c.status in RESOLVED_STATUSES for c in all_conflicts)
        await session.commit()
        await session.refresh(resolution)

    return {
        "conflict_id": conflict_ref,
        "key": conflict.key,
        "status": conflict.status,
        "decision": resolution.decision,
        "justification": resolution.justification,
        "winning_side": resolution.winning_side,
        "approved_by": resolution.approved_by,
        "approved_at": resolution.approved_at.isoformat() if resolution.approved_at else None,
        "override_reason": resolution.override_reason,
        "all_resolved": all_resolved,
    }


async def maybe_emit_approval_timeouts(
    scenario_id: uuid.UUID, minutes: float
) -> list[dict]:
    """Lazily flag conflicts still awaiting approval past the window.

    Emits one ``approval_timeout`` event per stale conflict (deduped by prior
    events). It deliberately does NOT change any status — an unanswered
    approval must escalate, never silently auto-approve.
    """
    emitted: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    async with async_session() as session:
        conflicts = (
            await session.execute(
                select(Conflict, Resolution)
                .join(Resolution, Resolution.conflict_id == Conflict.id, isouter=True)
                .where(
                    Conflict.scenario_id == scenario_id,
                    Conflict.status == AWAITING_APPROVAL,
                )
            )
        ).all()

        already = set(
            (
                await session.execute(
                    select(Event.data["conflict_id"].astext).where(
                        Event.scenario_id == scenario_id,
                        Event.type == "approval_timeout",
                    )
                )
            )
            .scalars()
            .all()
        )

        for _conflict, resolution in conflicts:
            if resolution is None:
                continue
            if _conflict.key in already:
                continue
            if resolution.proposed_at.replace(tzinfo=timezone.utc) > cutoff:
                continue
            already.add(_conflict.key)
            data = {
                "conflict_id": _conflict.key,
                "decision": resolution.decision,
                "justification": resolution.justification,
                "minutes": minutes,
            }
            session.add(
                Event(
                    scenario_id=scenario_id,
                    type="approval_timeout",
                    agent="negotiator",
                    message=(
                        f"Approval pending for {data['minutes']:.0f} min on "
                        f"{_conflict.key} — no decision yet"
                    ),
                    data=data,
                )
            )
            emitted.append(data)
        if emitted:
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent writer (watchdog timer + replay/GET) committed
                # the same approval_timeout rows first — the unique partial
                # index wins the race, so this batch is a no-op.
                await session.rollback()
                return []
    return emitted


async def replay_scenario(id_ref: str) -> dict | None:
    """Full replay for ``GET /scenarios/{id}`` — events ordered by PK, each
    conflict with its turns and resolution in natural order."""
    scenario = await get_scenario(id_ref)
    if scenario is None:
        return None

    await maybe_emit_approval_timeouts(
        scenario.id, float(os.getenv("APPROVAL_TIMEOUT_MINUTES", "15"))
    )

    async with async_session() as session:
        events = (
            await session.execute(
                select(Event)
                .where(Event.scenario_id == scenario.id)
                .order_by(Event.id.asc())
            )
        ).scalars().all()

        conflicts = (
            await session.execute(
                select(Conflict)
                .where(Conflict.scenario_id == scenario.id)
                .order_by(Conflict.key.asc())
            )
        ).scalars().all()

        conflict_payload = []
        for conflict in conflicts:
            turns = (
                await session.execute(
                    select(NegotiationTurn)
                    .where(NegotiationTurn.conflict_id == conflict.id)
                    .order_by(NegotiationTurn.turn_number.asc())
                )
            ).scalars().all()
            resolution = (
                await session.execute(
                    select(Resolution).where(Resolution.conflict_id == conflict.id)
                )
            ).scalar_one_or_none()
            conflict_payload.append(
                {
                    "id": str(conflict.id),
                    "key": conflict.key,
                    "type": conflict.type,
                    "resource": conflict.resource,
                    "claim_a": conflict.claim_a,
                    "claim_b": conflict.claim_b,
                    "status": conflict.status,
                    "turns": [
                        {
                            "turn_number": turn.turn_number,
                            "agent": turn.agent,
                            "message": turn.message,
                            "created_at": turn.created_at.isoformat(),
                        }
                        for turn in turns
                    ],
                    "resolution": (
                        {
                            "id": str(resolution.id),
                            "decision": resolution.decision,
                            "justification": resolution.justification,
                            "winning_side": resolution.winning_side,
                            "proposed_at": resolution.proposed_at.isoformat(),
                            "approved_by": resolution.approved_by,
                            "approved_at": (
                                resolution.approved_at.isoformat()
                                if resolution.approved_at
                                else None
                            ),
                            "override_reason": resolution.override_reason,
                        }
                        if resolution
                        else None
                    ),
                }
            )

    return {
        "scenario": {
            "id": str(scenario.id),
            "client_id": scenario.client_id,
            "status": scenario.status,
            "created_at": scenario.created_at.isoformat(),
            "data_mode": scenario.data_mode,
            "location_key": scenario.location_key,
            "raw_data": scenario.raw_data,
            "prediction": scenario.prediction,
            "logistics_plan": scenario.logistics_plan,
            "briefing": scenario.briefing,
        },
        "events": [
            {
                "id": event.id,
                "type": event.type,
                "agent": event.agent,
                "message": event.message,
                "data": event.data,
                "timestamp": event.created_at.isoformat(),
            }
            for event in events
        ],
        "conflicts": conflict_payload,
    }