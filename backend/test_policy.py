"""Tests for the auditable policy check.

Covers every documented rule, the agreement logic, the hybrid resolution shape,
and a forced-disagreement scenario where the policy and the (stub) Arbiter
deliberately split.
"""
import asyncio
import os
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SMALL_VILLAGE = {
    "purpose": "evacuation",
    "zone": "ZoneVillage",
    "justification": "ZoneVillage evacuating 120 people",
}
BIG_SHELTERED_CITY = {
    "purpose": "supply_delivery",
    "zone": "ZoneCity",
    "justification": "ZoneCity requires water and medical resupply via route_main",
}
PURE_LOGISTICS = {
    "purpose": "supply_delivery",
    "zone": "ZoneWarehouse",
    "justification": "ZoneWarehouse restocks non-critical warehouse stock",
}
EVAC_B = {
    "purpose": "evacuation",
    "zone": "ZoneCity",
    "justification": "ZoneCity (2,000 people) also evacuating",
}

def alloc(zone, pop, p12h):
    return {"zone_name": zone, "population": pop, "flood_probability_12h": p12h}

ROUTE_CONFLICT = {
    "id": "conflict_test",
    "type": "route_conflict",
    "resource": "route_main",
    "claim_a": SMALL_VILLAGE,
    "claim_b": BIG_SHELTERED_CITY,
}
SHELTER_CONFLICT = {
    "id": "conflict_shelter",
    "type": "shelter_capacity_conflict",
    "resource": "shelter_1",
    "claim_a": SMALL_VILLAGE,
    "claim_b": EVAC_B,
}
PLAN_1 = {"allocations": [
    alloc("ZoneVillage", 120, 0.42),
    alloc("ZoneCity", 2000, 0.90),
]}
PLAN_TIE = {"allocations": [
    alloc("ZoneVillage", 2000, 0.90),
    alloc("ZoneCity", 2000, 0.90),
]}
PLAN_NEAR_TIE = {"allocations": [
    alloc("ZoneVillage", 180, 0.82),  # 147.6
    alloc("ZoneCity",   190, 0.83),  # 157.7 → margin 6.4% > 5%
]}
PLAN_TIGHT_NEAR_TIE = {"allocations": [
    alloc("ZoneVillage", 180, 0.82),  # 147.6
    alloc("ZoneCity",   184, 0.82),  # 150.88 → margin 2.2%
]}


from agents.policy import (
    NEAR_TIE_MARGIN,
    claim_criticality,
    claim_profile,
    evaluate_conflict,
    policy_agreement,
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_claim_criticality():
    assert claim_criticality(SMALL_VILLAGE) == "life_safety"
    assert claim_criticality(BIG_SHELTERED_CITY) == "safety_logistics"
    assert claim_criticality(PURE_LOGISTICS) == "pure_logistics"
    assert claim_criticality(EVAC_B) == "life_safety"
    assert claim_criticality({"purpose": "other"}) == "pure_logistics"


def test_claim_profile_with_plan():
    prof = claim_profile(SMALL_VILLAGE, PLAN_1)
    assert prof.zone == "ZoneVillage"
    assert prof.criticality == "life_safety"
    assert prof.population == 120
    assert prof.flood_probability == 0.42
    assert abs(prof.risk_score - 50.4) < 1e-9


def test_claim_profile_no_allocation():
    prof = claim_profile(SMALL_VILLAGE, {"allocations": []})
    assert prof.population == 0
    assert prof.risk_score == 0.0


def test_rule1_life_safety_beats_pure_logistics():
    conflict = {
        "id": "c1",
        "type": "route_conflict",
        "resource": "route_a",
        "claim_a": SMALL_VILLAGE,                              # life_safety
        "claim_b": {**BIG_SHELTERED_CITY, "justification": "pure logistics delivery"},  # falls to pure
    }
    # Override criticality: justification no longer has water/medical
    rec = evaluate_conflict(conflict, PLAN_1)
    assert rec["winning_side"] == "evacuation"
    assert "Rule 1" in rec["justification"]


def test_rule1_flipped():
    conflict = {
        "id": "c1",
        "type": "route_conflict",
        "resource": "route_a",
        "claim_a": {**BIG_SHELTERED_CITY, "justification": "pure logistics delivery"},
        "claim_b": SMALL_VILLAGE,
    }
    rec = evaluate_conflict(conflict, PLAN_1)
    assert rec["winning_side"] == "evacuation"
    assert "ZoneVillage" in rec["decision"]


def test_rule2_high_risk_wins():
    """ZoneCity (risk ~1800) clearly beats ZoneVillage (risk ~50)."""
    rec = evaluate_conflict(ROUTE_CONFLICT, PLAN_1)
    vp = claim_profile(SMALL_VILLAGE, PLAN_1)
    cp = claim_profile(BIG_SHELTERED_CITY, PLAN_1)
    assert cp.risk_score > vp.risk_score
    # Both safety-critical → higher score wins; City has higher score and is
    # supply_delivery → winning_side "logistics".
    assert rec["winning_side"] == "logistics"
    assert "Rule 2" in rec["justification"]
    assert f"{cp.risk_score:,.0f}" in rec["justification"]


def test_rule2_exact_tie_escalates():
    conflict = {
        "id": "c1",
        "type": "route_conflict",
        "resource": "route_a",
        "claim_a": SMALL_VILLAGE,
        "claim_b": EVAC_B,
    }
    rec = evaluate_conflict(conflict, PLAN_TIE)
    assert rec["winning_side"] is None
    assert "Escalate" in rec["decision"]


def test_rule2_near_tie_escalates():
    """Margin < 5% -> escalate."""
    rec = evaluate_conflict(ROUTE_CONFLICT, PLAN_TIGHT_NEAR_TIE)
    assert rec["winning_side"] is None
    assert "near-tie" in rec["justification"].lower()


def test_rule2_real_gap_decides():
    """Margin >= 5% (6.4%) -> higher side wins."""
    rec = evaluate_conflict(ROUTE_CONFLICT, PLAN_NEAR_TIE)
    assert rec["winning_side"] is not None
    assert "Rule 2" in rec["justification"]
    assert "real gap" in rec["justification"].lower()


def test_shelter_conflict_both_evacuation():
    """Both claims evacuation -> both life_safety, rule 2 decides."""
    rec = evaluate_conflict(SHELTER_CONFLICT, PLAN_1)
    # Village: 120x0.42=50.4, City: 2000x0.90=1800 → City wins decisively
    assert rec["winning_side"] is not None
    assert "ZoneCity" in rec["decision"] or "City" in rec["decision"]


def test_no_allocation_policy_loses_not_fabricates():
    """Zone missing from plan -> risk 0, does not win rule 2."""
    conflict = {
        "id": "c1",
        "type": "route_conflict",
        "resource": "route_a",
        "claim_a": SMALL_VILLAGE,
        "claim_b": BIG_SHELTERED_CITY,
    }
    plan_no_city = {"allocations": [alloc("ZoneVillage", 120, 0.42)]}
    rec = evaluate_conflict(conflict, plan_no_city)
    # ZoneCity missing → risk 0 → Village has higher score → Village wins
    assert rec["winning_side"] == "evacuation"
    assert "no allocation" in rec["justification"].lower() or "missing" in rec["justification"].lower()


# ---------------------------------------------------------------------------
# Agreement logic
# ---------------------------------------------------------------------------

def test_agreement_same_side():
    a = {"winning_side": "evacuation"}
    p = {"winning_side": "evacuation"}
    assert policy_agreement(a, p) is True


def test_agreement_different_sides():
    a = {"winning_side": "evacuation"}
    p = {"winning_side": "logistics"}
    assert policy_agreement(a, p) is False


def test_agreement_compromise_to_compromise():
    a = {"winning_side": "compromise"}
    p = {"winning_side": "compromise"}
    assert policy_agreement(a, p) is True


def test_agreement_either_none_true():
    assert policy_agreement({"winning_side": None}, {"winning_side": "logistics"}) is True
    assert policy_agreement({"winning_side": "evacuation"}, {"winning_side": None}) is True
    assert policy_agreement({"winning_side": None}, {"winning_side": None}) is True


# ---------------------------------------------------------------------------
# Forced-disagreement graph-level scenario (AEGIS_LLM_STUB=1)
# ---------------------------------------------------------------------------

def test_forced_disagreement_stub_evac_vs_policy_logistics():
    """Build a conflict where the documented policy says LOGISTICS but the
    (stub) Arbiter returns EVACUATION. The pipeline must attach both,
    set agree=False, and emit a policy_disagreement event — never silently
    resolve it.

    Force disagreement: claim A evacuates a tiny village (risk ~50) vs claim B
    supplies a huge sheltered city (risk ~1800) → policy (rule 2) says
    logistics wins. Stub arbiter (default / AEGIS_LLM_STUB_ARBITER_SIDE unset)
    always returns winning_side "evacuation".
    """
    from agents.policy import evaluate_conflict as _eval
    from schemas import ScenarioState

    # Build a state with a custom conflict and plan that triggers disagreement.
    state = ScenarioState(
        scenario_id="policy-test",
        raw_data={"rainfall_mm_24h": 180, "river_level_m": 3.5,
                  "river_level_danger_threshold_m": 3.0},
        conflicts=[ROUTE_CONFLICT],
        logistics_plan=PLAN_1,
    )

    from agents.pipeline import negotiator_node

    result = None

    # Mock run_debate so our synthetic conflict (whose zone names don't exist
    # in the demo inventory) never reaches the real debater — this test is
    # about the policy layer, not the LLM.
    arbiter_resolution = {
        "decision": "Reserve the resource for evacuation",
        "justification": "Life-safety must come first.",
        "winning_side": "evacuation",
    }

    async def run():
        nonlocal result
        mock_writer = MagicMock()
        with patch("agents.pipeline.get_stream_writer", return_value=mock_writer), \
             patch("agents.pipeline.run_debate",
                   return_value=([], arbiter_resolution, None)):
            result = await negotiator_node(state)

    asyncio.run(run())

    events = result["events"]
    resolution = result["resolution"]

    # --- assertions -------------------------------------------------------

    # 1. Agreement check: policy says logistics, arbiter says evacuation
    pol_rec = _eval(ROUTE_CONFLICT, PLAN_1)
    assert pol_rec["winning_side"] == "logistics"
    assert arbiter_resolution["winning_side"] == "evacuation"
    assert policy_agreement(arbiter_resolution, pol_rec) is False

    # 2. Stored resolution has hybrid shape with agree=False
    stored = resolution["conflict_test"]
    assert stored.get("agree") is False
    assert stored.get("arbiter_decision", {}).get("winning_side") == "evacuation"
    assert stored.get("policy_recommendation", {}).get("winning_side") == "logistics"
    # Top-level decision/justification/winning_side remain arbiter's
    assert stored["winning_side"] == "evacuation"

    # 3. policy_disagreement event was emitted
    disagree_events = [e for e in events if e.get("type") == "policy_disagreement"]
    assert len(disagree_events) >= 1, (
        "policy_disagreement event must appear when agree=False"
    )
    ddata = disagree_events[0].get("data", {})
    assert ddata.get("conflict_id") == "conflict_test"
    assert ddata.get("arbiter_decision", {}).get("winning_side") == "evacuation"
    assert ddata.get("policy_recommendation", {}).get("winning_side") == "logistics"

    # 4. Conflict is NOT silently resolved — still awaits approval
    assert stored["winning_side"] != pol_rec["winning_side"]
    approval_needed = [e for e in events if e.get("type") == "approval_needed"]
    assert len(approval_needed) >= 1
    assert approval_needed[0]["data"].get("agree") is False


def test_stub_arbiter_side_override():
    """When the arbiter agrees with the policy (both logistics), no
    disagreement event fires."""
    from schemas import ScenarioState

    state = ScenarioState(
        scenario_id="policy-test-agree",
        raw_data={"rainfall_mm_24h": 180, "river_level_m": 3.5,
                  "river_level_danger_threshold_m": 3.0},
        conflicts=[ROUTE_CONFLICT],
        logistics_plan=PLAN_1,
    )

    from agents.pipeline import negotiator_node

    arbiter_resolution = {
        "decision": "Reserve the resource for supply delivery",
        "justification": "Supply lines keep sheltered people alive.",
        "winning_side": "logistics",
    }

    result = None

    async def run():
        nonlocal result
        mock_writer = MagicMock()
        with patch("agents.pipeline.get_stream_writer", return_value=mock_writer), \
             patch("agents.pipeline.run_debate",
                   return_value=([], arbiter_resolution, None)):
            result = await negotiator_node(state)

    asyncio.run(run())

    events = result["events"]
    stored = result["resolution"]["conflict_test"]
    assert stored.get("agree") is True
    assert stored.get("policy_recommendation", {}).get("winning_side") == "logistics"
    assert stored.get("arbiter_decision", {}).get("winning_side") == "logistics"
    disagree_events = [e for e in events if e.get("type") == "policy_disagreement"]
    assert len(disagree_events) == 0


def test_neutral_arbiter_attaches_policy_no_disagreement():
    """Failed arbitration (winning_side None) → policy attaches but the
    disagreement event must NOT fire — there is no side to dispute."""
    from schemas import ScenarioState

    state = ScenarioState(
        scenario_id="policy-test-neutral",
        raw_data={"rainfall_mm_24h": 180, "river_level_m": 3.5,
                  "river_level_danger_threshold_m": 3.0},
        conflicts=[ROUTE_CONFLICT],
        logistics_plan=PLAN_1,
    )

    from agents.pipeline import negotiator_node

    neutral_resolution = {
        "decision": "Escalate to human coordinator",
        "justification": "Negotiation could not complete.",
        "winning_side": None,
    }

    result = None

    async def run():
        nonlocal result
        mock_writer = MagicMock()
        with patch("agents.pipeline.get_stream_writer", return_value=mock_writer), \
             patch("agents.pipeline.run_debate",
                   return_value=([], neutral_resolution, None)):
            result = await negotiator_node(state)

    asyncio.run(run())

    events = result["events"]
    stored = result["resolution"]["conflict_test"]
    assert stored.get("policy_recommendation") is not None
    assert stored.get("agree") is True  # None side → nothing to dispute
    disagree_events = [e for e in events if e.get("type") == "policy_disagreement"]
    assert len(disagree_events) == 0


def test_stub_seam_changes_arbiter_side_end_to_end():
    """Real seam check: AEGIS_LLM_STUB_ARBITER_SIDE drives the stub Arbtiter's
    winning_side through the real debate path (inventory-valid conflict)."""
    os.environ["AEGIS_LLM_STUB"] = "1"
    import llm.client as _client

    _client.AEGIS_LLM_STUB = True  # flag is read at import; force it live

    from agents.logistics import build_logistics_plan
    from agents.negotiator import run_debate
    from agents.prediction import build_prediction

    prediction = build_prediction(
        {"rainfall_mm_24h": 180, "river_level_m": 3.5,
         "river_level_danger_threshold_m": 3.0}
    )
    plan, conflicts = build_logistics_plan(prediction)
    conflict = next(c for c in conflicts if c["type"] == "route_conflict")

    async def _debate():
        return await run_debate(conflict, plan, MagicMock())

    os.environ["AEGIS_LLM_STUB_ARBITER_SIDE"] = "logistics"
    _, res_logistics, err = asyncio.run(_debate())
    assert err is None
    assert res_logistics["winning_side"] == "logistics"

    os.environ.pop("AEGIS_LLM_STUB_ARBITER_SIDE", None)
    _, res_evac, err = asyncio.run(_debate())
    assert err is None
    assert res_evac["winning_side"] == "evacuation"

    # Confirm the real demo conflicts all have evacuation on policy side too,
    # so the default stub pipeline agrees and emits no disagreement events.
    from agents.policy import evaluate_conflict
    for c in conflicts:
        assert evaluate_conflict(c, plan)["winning_side"] == "evacuation"
    _client.AEGIS_LLM_STUB = False
    os.environ.pop("AEGIS_LLM_STUB", None)


def test_debate_graph_e2e_hybrid_shape_and_agreement():
    """Full graph run (stub): every demo conflict resolves to the hybrid shape,
    policy and (default-stub) arbiter agree on evacuation, and no
    policy_disagreement event is emitted."""
    os.environ["AEGIS_LLM_STUB"] = "1"
    os.environ.pop("AEGIS_LLM_STUB_ARBITER_SIDE", None)
    import llm.client as _client

    _client.AEGIS_LLM_STUB = True

    from agents.prediction import build_prediction
    from agents.logistics import build_logistics_plan
    from agents.pipeline import build_debate_graph
    from schemas import ScenarioState

    prediction = build_prediction(
        {"rainfall_mm_24h": 180, "river_level_m": 3.5,
         "river_level_danger_threshold_m": 3.0}
    )

    state = ScenarioState(
        scenario_id="e2e-policy",
        raw_data={"rainfall_mm_24h": 180, "river_level_m": 3.5,
                  "river_level_danger_threshold_m": 3.0},
        prediction=prediction,
        # logistics_node builds (and flags) the conflicts in-graph; seeding
        # them here would make logistics_node append on top → duplicates.
        conflicts=[],
    )

    final = asyncio.run(build_debate_graph().ainvoke(state))

    resolution = final["resolution"]
    events = final["events"]
    conflicts = build_logistics_plan(prediction)[1]
    assert set(resolution.keys()) == {c["id"] for c in conflicts}

    for cid, stored in resolution.items():
        assert "arbiter_decision" in stored, f"{cid} missing arbiter_decision"
        assert "policy_recommendation" in stored, f"{cid} missing policy_recommendation"
        assert stored["agree"] is True, f"{cid} should agree under default stub"
        # top-level fields preserved for backward compat
        assert stored["winning_side"] == "evacuation"
        assert stored["arbiter_decision"]["winning_side"] == "evacuation"
        assert stored["policy_recommendation"]["winning_side"] == "evacuation"

    disagrees = [
        e for e in events if e.get("type") == "policy_disagreement"
    ]
    assert not disagrees, "default stub arbiter and policy both say evacuation"

    approvals = [
        e for e in events if e.get("type") == "approval_needed"
    ]
    assert len(approvals) == len(conflicts)
    for a in approvals:
        assert a["data"]["agree"] is True
        assert "policy_recommendation" in a["data"]

    _client.AEGIS_LLM_STUB = False
    os.environ.pop("AEGIS_LLM_STUB", None)


# ---------------------------------------------------------------------------
# Runner (standalone + pytest)
# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_claim_criticality,
    test_claim_profile_with_plan,
    test_claim_profile_no_allocation,
    test_rule1_life_safety_beats_pure_logistics,
    test_rule1_flipped,
    test_rule2_high_risk_wins,
    test_rule2_exact_tie_escalates,
    test_rule2_near_tie_escalates,
    test_rule2_real_gap_decides,
    test_shelter_conflict_both_evacuation,
    test_no_allocation_policy_loses_not_fabricates,
    test_agreement_same_side,
    test_agreement_different_sides,
    test_agreement_compromise_to_compromise,
    test_agreement_either_none_true,
    test_forced_disagreement_stub_evac_vs_policy_logistics,
    test_stub_arbiter_side_override,
    test_neutral_arbiter_attaches_policy_no_disagreement,
    test_stub_seam_changes_arbiter_side_end_to_end,
    test_debate_graph_e2e_hybrid_shape_and_agreement,
]

if __name__ == "__main__":
    for t in ALL_TESTS:
        t()
        print(f"  PASS: {t.__name__}")
    print("ALL POLICY TESTS PASSED")