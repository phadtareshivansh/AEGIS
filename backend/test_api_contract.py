"""HTTP contract for validation + human approvals — clear 4xx, never 500/no-op."""
import time
import uuid

from testutils import get, post, start_scenario, unique_sid


def _wait_for_awaiting(base_url: str, sid: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = get(base_url, f"/scenarios/{sid}")
        assert r.status_code == 200, r.text
        replay = r.json()
        if replay["scenario"]["status"] == "awaiting_approval" and len(replay["conflicts"]) == 3:
            return replay
        time.sleep(0.2)
    raise AssertionError(f"scenario {sid} never reached awaiting_approval with 3 conflicts")


def test_approval_contract_4xx_and_idempotent(server_stub):
    base = server_stub.base_url
    sid = unique_sid("contract")
    scenario_id = start_scenario(
        base,
        sid,
        {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0},
    )
    replay = _wait_for_awaiting(base, sid)
    keys = [c["key"] for c in replay["conflicts"]]
    assert keys == ["conflict_1", "conflict_2", "conflict_3"]

    c1, c2, c3 = keys

    def ok_(r, code):
        assert r.status_code == code, f"{r.status_code} != {code}: {r.text}"

    # --- approve -> idempotent repeat -> flip is 409 -------------
    r = post(base, f"/scenarios/{scenario_id}/conflicts/{c1}/approve", {"approved_by": "op-1"})
    ok_(r, 200)
    assert r.json()["all_resolved"] is False

    r2 = post(base, f"/scenarios/{scenario_id}/conflicts/{c1}/approve", {"approved_by": "op-1"})
    ok_(r2, 200)
    assert r2.json()["status"] == "approved"

    r = post(
        base,
        f"/scenarios/{scenario_id}/conflicts/{c1}/override",
        {"approved_by": "op-1", "override_reason": "switch", "override_decision": "logistics"},
    )
    ok_(r, 409)

    # --- blank human fields are 409 ------------------------------
    r = post(base, f"/scenarios/{scenario_id}/conflicts/{c2}/approve", {"approved_by": "  "})
    ok_(r, 409)

    # --- malformed override body is 422 --------------------------
    r = post(
        base, f"/scenarios/{scenario_id}/conflicts/{c2}/override", {"approved_by": "op"}
    )
    ok_(r, 422)

    # --- override with empty reason is 409 -----------------------
    r = post(
        base,
        f"/scenarios/{scenario_id}/conflicts/{c2}/override",
        {"approved_by": "op-2", "override_reason": "  ", "override_decision": "logistics"},
    )
    ok_(r, 409)

    # --- valid override proceeds ---------------------------------
    r = post(
        base,
        f"/scenarios/{scenario_id}/conflicts/{c2}/override",
        {"approved_by": "op-2", "override_reason": "traffic jam on route", "override_decision": "logistics"},
    )
    ok_(r, 200)
    assert r.json()["status"] == "overridden"
    assert r.json()["all_resolved"] is False

    # --- unknown conflict_id is 404 ------------------------------
    r = post(
        base,
        f"/scenarios/{scenario_id}/conflicts/{uuid.uuid4()}/approve",
        {"approved_by": "op"},
    )
    ok_(r, 404)

    # --- last approve completes -----------------------------------
    r = post(base, f"/scenarios/{scenario_id}/conflicts/{c3}/approve", {"approved_by": "op-3"})
    ok_(r, 200)
    assert r.json()["all_resolved"] is True

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if get(base, f"/scenarios/{scenario_id}").json()["scenario"]["status"] == "complete":
            break
        time.sleep(0.2)
    else:
        raise AssertionError("scenario never completed")


def test_unknown_scenario_and_conflict_return_404(server_stub):
    base = server_stub.base_url
    r = get(base, "/scenarios/no-such-scenario")
    assert r.status_code == 404

    r = post(base, "/scenarios/no-such-scenario/conflicts/c1/approve", {"approved_by": "op"})
    assert r.status_code == 404

    r = post(
        base,
        "/scenarios/no-such-scenario/conflicts/c1/override",
        {"approved_by": "op", "override_reason": "x", "override_decision": "logistics"},
    )
    assert r.status_code == 404