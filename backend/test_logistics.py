from agents.logistics import build_logistics_plan, load_inventory
from agents.prediction import build_prediction

FLOOD = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}

prediction = build_prediction(FLOOD)
plan, conflicts = build_logistics_plan(prediction, load_inventory())

print("=== Logistics allocation (flood scenario) ===")
print(f"{'zone':<16}{'pop':>6}{'risk':>7}  {'shelter':<24}{'amb':>4}{'tank':>5}{'route':<12}")
for a in plan["allocations"]:
    print(f"{a['zone_name']:<16}{a['population']:>6}{a['flood_probability_12h']:>7.3f}  "
          f"{a['shelter_name'] or '-':<24}{a['ambulances']:>4}{a['water_tankers']:>5}{a['route_id'] or '-':<12}")
print("ambulances used:", sum(a["ambulances"] for a in plan["allocations"]))
print("water tankers used:", sum(a["water_tankers"] for a in plan["allocations"]))
print("conflicts_flagged:", plan["conflicts_flagged"])

print("\n=== Conflicts produced ===")
for c in conflicts:
    print(f"{c['id']} [{c['type']}] resource={c['resource']}")
    print(f"  claim_a: {c['claim_a']['purpose']} for {c['claim_a']['zone']} — {c['claim_a']['justification']}")
    print(f"  claim_b: {c['claim_b']['purpose']} for {c['claim_b']['zone']} — {c['claim_b']['justification']}")

assert len(conflicts) == 3, f"expected 3 conflicts, got {len(conflicts)}"
ids = {c["id"] for c in conflicts}
assert ids == {"conflict_1", "conflict_2", "conflict_3"}
assert {"route_conflict", "shelter_capacity_conflict"} <= {c["type"] for c in conflicts}
route = next(c for c in conflicts if c["type"] == "route_conflict")
assert route["resource"] == "east_bridge"
assert (route["claim_a"]["purpose"], route["claim_a"]["zone"]) == ("evacuation", "Harbor District")
assert (route["claim_b"]["purpose"], route["claim_b"]["zone"]) == ("supply_delivery", "Lowtown")
shelters = [c for c in conflicts if c["type"] == "shelter_capacity_conflict"]
assert {c["resource"] for c in shelters} == {"shelter_1", "shelter_2"}
assert sum(a["ambulances"] for a in plan["allocations"]) == 5
assert sum(a["water_tankers"] for a in plan["allocations"]) == 10
print("\nALL LOGISTICS ASSERTIONS PASSED")