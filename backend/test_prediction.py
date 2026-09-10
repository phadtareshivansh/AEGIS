from agents.prediction import build_prediction, rank_zones, load_zones

ZONES = load_zones()

SAFE = {"rainfall_mm_24h": 10, "river_level_m": 2.0, "river_level_danger_threshold_m": 4.0}
FLOOD = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}


def run(label: str, raw_data: dict) -> dict:
    ranked = rank_zones(ZONES, raw_data["rainfall_mm_24h"], raw_data["river_level_m"], raw_data["river_level_danger_threshold_m"])
    pred = build_prediction(raw_data, ZONES)
    print(f"\n=== Scenario: {label} | rain={raw_data['rainfall_mm_24h']}mm, "
          f"river={raw_data['river_level_m']}m, thr={raw_data['river_level_danger_threshold_m']}m ===")
    print("Summary:", pred["summary"])
    print(f"{'zone':<18}{'pop':>6}{'elev':>6}{'dist':>6}{'12h':>7}{'24h':>7}")
    for z in ranked:
        print(f"{z['zone_name']:<18}{z['population']:>6}{z['elevation_m']:>6.0f}"
              f"{z['distance_to_river_km']:>6.1f}{z['flood_probability_12h']:>7.3f}"
              f"{z['flood_probability_24h']:>7.3f}")
    print("Top-5 at_risk:", [z["zone_name"] for z in pred["at_risk_zones"]])
    return ranked, pred


ranked_safe, pred_safe = run("(a) LOW rain + SAFE river", SAFE)
ranked_flood, pred_flood = run("(b) HIGH rain + river OVER threshold", FLOOD)


names = lambda r: [z["zone_name"] for z in r]
safe_names = names(ranked_safe)
flood_names = names(ranked_flood)

assert pred_safe["horizon_hours"] == 24

# (a) safe scenario: no zone alarmingly high; obviously-safe zones near zero
assert ranked_safe[0]["flood_probability_12h"] < 0.55, "safe max exceeds 0.55"
assert ranked_safe[-1]["flood_probability_12h"] < 0.10, "safest zone not < 0.10"
assert "Red Bluff" in safe_names[-5:] and "High Plains" in safe_names[-5:]
assert "{:.3f}".format(max(z["flood_probability_12h"] for z in ranked_safe)) == "0.510"

# (b) flood scenario: vulnerable zones top the list, sorted descending
top5 = ranked_flood[:5]
assert all(top5[i]["flood_probability_12h"] >= top5[i + 1]["flood_probability_12h"] for i in range(4))
assert all(z["flood_probability_12h"] >= 0.65 for z in top5)
assert ranked_flood[0]["flood_probability_12h"] >= 0.79
assert flood_names[:3] == ["Harbor District", "Riverside", "Lowtown"]
assert "Red Bluff" in flood_names[-3:] and "High Plains" in flood_names[-3:]
assert "{:.3f}".format(max(z["flood_probability_12h"] for z in ranked_flood)) == "0.796"

# 24h horizon scales up but stays capped at 1.0
assert all(0 <= z["flood_probability_12h"] <= z["flood_probability_24h"] <= 1.0 for z in ranked_flood)

print("\nALL ASSERTIONS PASSED")