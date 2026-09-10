import asyncio
import re
from datetime import datetime, timezone

from agents.pipeline import build_graph
from schemas import ScenarioState

FLOOD = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}
COMPROMISE_WORDS = [
    "let's split", "lets split", "let's find", "lets find", "how about",
    "we could share", "we should share", "middle ground", "take turns",
    "split the difference", "propose a split", "could we split", "agree to a split",
]
PIVOT_WORDS = ["but ", "however", "reserve", "prioritize", "until", "then ", "phase", "first", "instead"]


def consolidate(turn_events_by_key):
    """Keep the final (most complete) message per (conflict_id, turn)."""
    return {k: evs[-1]["message"] for k, evs in turn_events_by_key.items()}


async def main():
    state = ScenarioState(scenario_id="debate-end-to-end", raw_data=FLOOD)
    graph = build_graph()

    live = []
    turn_events: dict[tuple[str, int], list[dict]] = {}
    async for mode, chunk in graph.astream(state, stream_mode=["updates", "custom"]):
        if mode == "custom":
            event = chunk["event"]
            live.append(event)
            if event["type"] == "negotiation_turn":
                key = (event["data"]["conflict_id"], event["data"]["turn"])
                turn_events.setdefault(key, []).append(event)
            else:
                print(f"[custom] {event['agent']} {event['type']} streamed")
        else:
            for update in chunk.values():
                for k, v in update.items():
                    setattr(state, k, v)

    turns_by_conflict: dict[str, dict[int, str]] = {}
    for (cid, turn), events in turn_events.items():
        turns_by_conflict.setdefault(cid, {})[turn] = events[-1]["message"]

    print("\n========== LIVE EVENT LOG (as queued) ==========")
    for e in live:
        if e["type"] == "negotiation_turn":
            print(f"[streaming] {e['agent']:<18} turn={e['data']['turn']} "
                  f"conflict={e['data']['conflict_id']} msg_len={len(e['message'])}")
    print(f"streamed turn chunks total: {len(live)}")

    print("\n========== FINAL TRANSCRIPTS ==========")
    for cid in sorted(turns_by_conflict):
        print(f"\n--- {cid} ---")
        for turn in sorted(turns_by_conflict[cid]):
            text = turns_by_conflict[cid][turn]
            agent = "evac" if turn in (1, 3) else "logi"
            print(f"[T{turn} {agent}] {text}")

    print("\n========== RESOLUTIONS (from state) ==========")
    for cid, res in sorted((state.resolution or {}).items()):
        print(f"{cid}: decision={res.get('decision')!r} winning_side={res.get('winning_side')!r}")
        print(f"     justification={res.get('justification')!r}")

    # ---- quality gates ----
    warnings = []
    first = "conflict_1"
    ft = turns_by_conflict[first] if first in turns_by_conflict else {}
    assert len(ft) == 4, f"expected 4 turns for conflict_1, got {len(ft)}"

    for cid, turns in turns_by_conflict.items():
        for turn, text in turns.items():
            if not re.search(r"\d", text):
                warnings.append(f"{cid} T{turn}: no concrete number cited")
            if turn in (1, 2):
                hits = [w for w in COMPROMISE_WORDS if w in text.lower()]
                if hits:
                    warnings.append(f"{cid} T{turn}: EARLY compromise language: {hits[0]!r}")
    if not re.search(r"\d", ft[1]):
        warnings.append("conflict_1 T1 has no number")
    if not re.search(r"\d", ft[2]):
        warnings.append("conflict_1 T2 has no number")

    pivot = ft[3]
    pivot_ok = any(w in pivot.lower() for w in PIVOT_WORDS) and pivot.strip() != ft[1].strip()
    if not pivot_ok:
        warnings.append("conflict_1 T3 shows no clear pivot (no concession/offer marker)")

    for cid, res in (state.resolution or {}).items():
        assert isinstance(res, dict), f"{cid} resolution not a dict"
        for key in ("decision", "justification", "winning_side"):
            assert key in res, f"{cid} resolution missing {key!r}"
        assert res["decision"], f"{cid} empty decision"

    assert state.negotiation_log, "negotiation_log not populated"
    assert len(state.negotiation_log) == len(state.conflicts)

    print("\n========== QUALITY GATE SUMMARY ==========")
    if warnings:
        for w in warnings:
            print(f"  WARN: {w}")
    else:
        print("  no warnings — debate hygiene looks good")
    if any("EARLY compromise" in w or "no concrete number" in w or "no clear pivot" in w for w in warnings):
        print("  RESULT: NEEDS PERSONA HARDENING")
    else:
        print("  RESULT: PASS")


asyncio.run(main())