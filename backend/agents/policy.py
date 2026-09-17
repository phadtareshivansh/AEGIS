"""Auditable resource-priority policy for conflict resolution.

A deterministic, documented ruleset (plain Python — no LLM) that runs
ALONGSIDE the LLM debate. Its job is not to replace the Arbiter but to give a
human approver an independent, inspectable recommendation so the debate's role
is "surface the tradeoff" and the policy's role is "check it against the rules".

Policy (all formulas below are intentionally simple and documented — nothing
here is a hidden model):

Rule 1 — Life-safety outranks logistics.
    If exactly one claim is life-safety-critical (evacuation of people in
    immediate danger) and the other is PURE logistics (no life-safety payload,
    e.g. restock of a storehouse), the life-safety claim wins regardless of the
    risk math. Exception: a logistics claim that is itself safety-critical
    (water/medical resupply to an already-sheltered population) is NOT "pure"
    and moves both claims to Rule 2.

Rule 2 — When both claims are safety-critical, prioritize by risk.
    risk(claim) = population(claim zone) x flood_probability_12h(claim zone),
    both read from the logistics plan allocations. The higher risk wins if the
    margin is real; near-ties escalate to a human rather than pretending the
    formula has precision it does not:
        margin = |risk_a - risk_b| / max(risk_a, risk_b)
        risk_a wins if margin >= NEAR_TIE_MARGIN and risk_a > risk_b
        risk_b wins if margin >= NEAR_TIE_MARGIN and risk_b > risk_a
        otherwise -> "Escalate to human coordinator", winning_side None

Rule 3 — No fabrication.
    If a claim's zone cannot be found in the logistics plan allocations, we do
    not invent population or probability. The claim is treated as having no
    risk data; if it is the only life-safety claim it still wins via Rule 1,
    otherwise it loses the math (and the justification states the missing data).

Agreement semantics:
    The Arbiter's decision text is free-form, so agreement is judged on the
    ``winning_side`` axis. Compromise == compromise agrees. If either side is
    None (incl. a failed/neutral arbitration) there is no side to dispute, so
    ``policy_agreement`` returns True and callers must NOT emit a
    policy_disagreement event.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Minimum relative margin before the risk formula is allowed to decide
#: (avoids a false sense of precision from a rough-but-documented formula).
NEAR_TIE_MARGIN = 0.05

#: Claim purposes produced by the logistics planner.
PURPOSE_EVACUATION = "evacuation"
PURPOSE_SUPPLY_DELIVERY = "supply_delivery"

#: Keywords marking a supply delivery as safety-critical (water/medical to
#: sheltered people). Route conflicts in AEGIS always carry these.
SAFETY_SUPPLY_KEYWORDS = ("water", "medical")

SIDE_EVACUATION = "evacuation"
SIDE_LOGISTICS = "logistics"
SIDE_COMPROMISE = "compromise"


@dataclass
class ClaimProfile:
    """What the policy knows about one side of a conflict."""
    zone: str
    purpose: str
    criticality: str  # "life_safety" | "safety_logistics" | "pure_logistics"
    population: int
    flood_probability: float
    risk_score: float  # population x flood_probability (0.0 if no data)


def claim_criticality(claim: dict) -> str:
    """Classify a claim's safety standing from its schema purpose + payload.

    ``purpose`` comes from the logistics planner:
      - "evacuation" -> life_safety (people in immediate danger)
      - "supply_delivery" -> safety_logistics if it carries water/medical
        resupply to shelters, otherwise pure_logistics.
    """
    purpose = str(claim.get("purpose", ""))
    if purpose == PURPOSE_EVACUATION:
        return "life_safety"
    if purpose == PURPOSE_SUPPLY_DELIVERY:
        text = " ".join(
            str(claim.get(k, "") or "") for k in ("justification", "purpose")
        ).lower()
        if any(kw in text for kw in SAFETY_SUPPLY_KEYWORDS):
            return "safety_logistics"
        return "pure_logistics"
    return "pure_logistics"


def _allocation_by_zone(plan: dict | None, zone: str) -> dict | None:
    allocations = (plan or {}).get("allocations", [])
    for alloc in allocations:
        if alloc.get("zone_name") == zone:
            return alloc
    return None


def claim_profile(claim: dict, plan: dict | None) -> ClaimProfile:
    """Build the risk profile for one claim — no fabricated numbers.

    If the plan has no allocation for the claim's zone, population and
    probability default to 0 (risk 0) and the justification will state that
    the zone was missing from the plan.
    """
    zone = str(claim.get("zone", ""))
    alloc = _allocation_by_zone(plan, zone) or {}
    population = int(alloc.get("population", 0) or 0)
    prob = float(alloc.get("flood_probability_12h", 0.0) or 0.0)
    return ClaimProfile(
        zone=zone,
        purpose=str(claim.get("purpose", "")),
        criticality=claim_criticality(claim),
        population=population,
        flood_probability=prob,
        risk_score=round(population * prob, 4),
    )


def evaluate_conflict(conflict: dict, plan: dict | None) -> dict:
    """Compute the policy recommendation for one conflict.

    Returns an Arbiter-shaped payload:
        {decision, justification, winning_side}
    where winning_side is one of "evacuation" | "logistics" | "compromise"
    | None (None only for escalate-on-near-tie / no decisive data).
    """
    claim_a = conflict.get("claim_a", {})
    claim_b = conflict.get("claim_b", {})
    a = claim_profile(claim_a, plan)
    b = claim_profile(claim_b, plan)

    # Rule 1 — life-safety beats PURE logistics outright.
    if a.criticality == "life_safety" and b.criticality == "pure_logistics":
        return {
            "decision": f"Reserve {conflict.get('resource', 'the resource')} "
                        f"for the evacuation of {a.zone}",
            "justification": (
                "Rule 1 (life-safety outranks logistics): "
                f"{a.zone} is evacuating people in immediate danger while "
                f"{b.zone}'s need is pure logistics with no safety-critical "
                "payload — life-safety wins regardless of the risk math."
            ),
            "winning_side": SIDE_EVACUATION,
        }
    if b.criticality == "life_safety" and a.criticality == "pure_logistics":
        return {
            "decision": f"Reserve {conflict.get('resource', 'the resource')} "
                        f"for the evacuation of {b.zone}",
            "justification": (
                "Rule 1 (life-safety outranks logistics): "
                f"{b.zone} is evacuating people in immediate danger while "
                f"{a.zone}'s need is pure logistics — life-safety wins."
            ),
            "winning_side": SIDE_EVACUATION,
        }

    # Rule 2 — both safety-critical (or the classification is a test/synthetic
    # mix that fell through): decide on population x probability.
    note = _missing_data_note(a, b, plan)

    if a.risk_score == b.risk_score:
        decision, winning_side = _near_tie(conflict, a, b, a.risk_score)
        just = _near_tie_justification(a, b, a.risk_score)
        if note:
            just += " " + note
        return {"decision": decision, "justification": just, "winning_side": winning_side}

    hi, lo = (a, b) if a.risk_score > b.risk_score else (b, a)
    margin = (hi.risk_score - lo.risk_score) / hi.risk_score

    if margin < NEAR_TIE_MARGIN:
        decision, winning_side = _near_tie(conflict, a, b, None)
        just = _near_tie_justification(a, b, None)
        if note:
            just += " " + note
        return {"decision": decision, "justification": just, "winning_side": winning_side}

    side = SIDE_EVACUATION if hi.criticality == "life_safety" else SIDE_LOGISTICS
    decision = (
        f"Reserve {conflict.get('resource', 'the resource')} for "
        f"{hi.zone} ({hi.purpose})"
    )
    justification = (
        "Rule 2 (both sides safety-critical — stake by "
        "population x flood_probability_12h): "
        f"{hi.zone} scores {_fmt_risk(hi.risk_score)} "
        f"({hi.population:,} people x {hi.flood_probability:.2f}) vs "
        f"{lo.zone} at {_fmt_risk(lo.risk_score)} "
        f"({lo.population:,} x {lo.flood_probability:.2f}); margin "
        f"{margin * 100:.1f}% is a real gap, so the higher-risk side wins."
    )
    note = _missing_data_note(a, b, plan)
    if note:
        justification += " " + note
    return {"decision": decision, "justification": justification, "winning_side": side}


def _near_tie(conflict: dict, a: ClaimProfile, b: ClaimProfile, score) -> tuple[str, str | None]:
    return (
        f"Escalate to human coordinator — {conflict.get('resource', 'the resource')} "
        "faces a near-tie between two safety-critical claims",
        None,
    )


def _near_tie_justification(a: ClaimProfile, b: ClaimProfile, score) -> str:
    if score is None:
        return (
            "Rule 2 (population x probability) produced NO decisive winner: "
            f"{a.zone} vs {b.zone} are within the {int(NEAR_TIE_MARGIN * 100)}% "
            "near-tie margin. The formula is rough by design, so the policy "
            "escalates rather than pretending to precision."
        )
    return (
        "Rule 2 (population x probability): both sides tie exactly "
        f"({_fmt_risk(score)} each). The policy escalates rather than "
        "arbitrating a tie by coin-flip."
    )


def _missing_data_note(a: ClaimProfile, b: ClaimProfile, plan: dict | None) -> str:
    missing = [prof for prof in (a, b) if _allocation_by_zone(plan, prof.zone) is None]
    if not missing:
        return ""
    names = " and ".join(p.zone for p in missing)
    return (
        f"Note: {names} had no allocation in the logistics plan, so their "
        "population and flood_probability default to zero. The formula "
        "therefore has incomplete data — treat the margin with appropriate "
        "caution."
    )


def _fmt_risk(score: float) -> str:
    return f"{score:,.0f} risk-points"


def policy_agreement(arbiter_resolution: dict, policy_recommendation: dict) -> bool:
    """Whether the Arbiter and the policy point the same way.

    Judged on ``winning_side`` (decision text is free-form). Compromise vs
    compromise agrees. If either side is None there is nothing to dispute,
    so this returns True (callers must not emit a disagreement event then).
    """
    arb_side = arbiter_resolution.get("winning_side")
    pol_side = policy_recommendation.get("winning_side")
    if arb_side is None or pol_side is None:
        return True
    return arb_side == pol_side