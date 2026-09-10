"""Deterministic logistics planning for AEGIS.

Pure functions — no LLM. Converts flood predictions into shelter, vehicle,
and route allocations, and flags resource conflicts for the negotiator to
resolve later. Kept separate from the LangGraph nodes for testability.
"""

import json
from pathlib import Path

INVENTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "inventory.json"


def load_inventory(path: Path | str | None = None) -> dict:
    """Load the resource inventory from JSON."""
    path = Path(path) if path else INVENTORY_PATH
    with path.open() as f:
        return json.load(f)


def _weight(zone: dict) -> float:
    """Allocation weight: population scaled by imminent flood probability."""
    return zone["population"] * zone["flood_probability_12h"]


def _allocate_vehicles(ranked: list[dict], pool: int) -> list[int]:
    """Split a vehicle pool across zones proportional to population*risk.

    Floor each share, then hand the remainder to the highest-weight zones
    first until the pool is exhausted.
    """
    total = sum(_weight(z) for z in ranked) or 1.0
    shares = [pool * _weight(z) / total for z in ranked]
    allocs = [int(s) for s in shares]
    remaining = pool - sum(allocs)
    order = sorted(range(len(ranked)), key=lambda i: shares[i], reverse=True)
    for i in order:
        if remaining <= 0:
            break
        allocs[i] += 1
        remaining -= 1
    return allocs


def build_logistics_plan(prediction: dict, inventory: dict | None = None) -> tuple[dict, list[dict]]:
    """Allocate resources for the at-risk zones and detect conflicts.

    Returns (logistics_plan, conflicts). Conflicts are numbered
    conflict_1, conflict_2, ... and meant to be appended to
    ScenarioState.conflicts.
    """
    inventory = inventory if inventory is not None else load_inventory()
    ranked = prediction["at_risk_zones"]
    planned_ids = {z["zone_id"] for z in ranked}

    shelters = inventory["shelters"]
    ambulances = inventory["ambulances"]["total"]
    tankers = inventory["water_tankers"]["total"]
    routes = inventory["evacuation_routes"]

    shelter_capacity = {s["id"]: s["capacity"] for s in shelters}
    shelter_remaining = dict(shelter_capacity)

    ambulances_by_zone = _allocate_vehicles(ranked, ambulances)
    tankers_by_zone = _allocate_vehicles(ranked, tankers)

    route_evacuees: dict[str, dict] = {}
    allocations = []
    for idx, zone in enumerate(ranked):
        serving = [s for s in shelters if zone["zone_id"] in s["serves_zones"]]
        chosen = None
        if serving:
            extra = [s for s in serving if shelter_remaining[s["id"]] > 0]
            chosen = (extra or serving)[0]
            shelter_remaining[chosen["id"]] -= zone["population"]

        route = next(
            (r for r in routes if zone["zone_id"] in r["connects_zones"]), {}
        )
        if route and zone["zone_id"] not in route_evacuees:
            route_evacuees[route["id"]] = zone

        allocations.append(
            {
                "zone_id": zone["zone_id"],
                "zone_name": zone["zone_name"],
                "population": zone["population"],
                "flood_probability_12h": zone["flood_probability_12h"],
                "shelter_id": chosen["id"] if chosen else None,
                "shelter_name": chosen["name"] if chosen else None,
                "ambulances": ambulances_by_zone[idx],
                "water_tankers": tankers_by_zone[idx],
                "route_id": route.get("id"),
            }
        )

    conflicts: list[dict] = []

    for alloc in allocations:
        route_id = alloc["route_id"]
        if not route_id:
            continue
        route = next(r for r in routes if r["id"] == route_id)
        if route.get("also_required_for") != "supply_delivery":
            continue
        supply_zone_id = route.get("supply_delivery_to_zone")
        if supply_zone_id not in planned_ids or supply_zone_id == alloc["zone_id"]:
            continue
        if route_id in {c["resource"] for c in conflicts if c["type"] == "route_conflict"}:
            continue
        supply_zone = next(z for z in ranked if z["zone_id"] == supply_zone_id)
        conflicts.append(
            {
                "id": f"conflict_{len(conflicts) + 1}",
                "type": "route_conflict",
                "resource": route_id,
                "claim_a": {
                    "purpose": "evacuation",
                    "zone": alloc["zone_name"],
                    "justification": (
                        f"{alloc['zone_name']} (flood probability "
                        f"{alloc['flood_probability_12h']:.2f}) requires full "
                        f"evacuation via {route_id}"
                    ),
                },
                "claim_b": {
                    "purpose": "supply_delivery",
                    "zone": supply_zone["zone_name"],
                    "justification": (
                        f"{supply_zone['zone_name']} requires water and medical "
                        f"resupply via {route_id}"
                    ),
                },
            }
        )

    assigned: dict[str, list[dict]] = {}
    for alloc in allocations:
        if alloc["shelter_id"]:
            assigned.setdefault(alloc["shelter_id"], []).append(alloc)

    for shelter in shelters:
        members = assigned.get(shelter["id"], [])
        if len(members) < 2:
            continue
        demand = sum(m["population"] for m in members)
        if demand <= shelter["capacity"]:
            continue
        ordered = sorted(members, key=lambda m: m["population"], reverse=True)
        a, b = ordered[0], ordered[1]
        conflicts.append(
            {
                "id": f"conflict_{len(conflicts) + 1}",
                "type": "shelter_capacity_conflict",
                "resource": shelter["id"],
                "claim_a": {
                    "purpose": "evacuation",
                    "zone": a["zone_name"],
                    "justification": (
                        f"{a['zone_name']} ({a['population']} people) is assigned "
                        f"to {shelter['name']}"
                    ),
                },
                "claim_b": {
                    "purpose": "evacuation",
                    "zone": b["zone_name"],
                    "justification": (
                        f"{b['zone_name']} ({b['population']} people) is also "
                        f"assigned to {shelter['name']}"
                    ),
                },
            }
        )

    plan = {"allocations": allocations, "conflicts_flagged": len(conflicts)}
    return plan, conflicts