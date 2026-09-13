import asyncio
import base64

import httpx

from agents.pipeline import build_graph
from agents.prediction import build_prediction, load_zones, rank_zones
from agents.simulation import (
    HF_TOKEN,
    build_svg_overlay,
    render_simulation,
    risk_color,
)
from schemas import ScenarioState

FLOOD = {"rainfall_mm_24h": 250, "river_level_m": 6.5, "river_level_danger_threshold_m": 4.0}


def test_risk_color_boundaries():
    assert risk_color(0.0) == "#22c55e"
    assert risk_color(1.0) == "#ef4444"
    assert risk_color(0.5) == "#facc15"
    for p in (0.0, 0.12, 0.5, 0.77, 1.0):
        c = risk_color(p)
        assert len(c) == 7 and c.startswith("#")


def test_svg_overlay_content():
    ranked = rank_zones(
        load_zones(),
        FLOOD["rainfall_mm_24h"],
        FLOOD["river_level_m"],
        FLOOD["river_level_danger_threshold_m"],
    )
    at_risk_ids = {
        z["zone_id"] for z in build_prediction(FLOOD)["at_risk_zones"]
    }
    svg = build_svg_overlay(ranked, at_risk_ids, FLOOD)

    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert f'viewBox="0 0 720 440"' in svg
    assert svg.count("<g") >= len(ranked), "one group per zone"
    assert svg.count("<animateTransform") == len(ranked), "water fills every zone"
    assert "fill=\"freeze\"" in svg, "animations freeze at final level"
    assert "repeatCount=\"indefinite\"" in svg, "at-risk pulse markers animate"

    top = sorted(ranked, key=lambda z: z["flood_probability_12h"], reverse=True)
    assert top[0]["zone_id"] in at_risk_ids
    bottom = top[-1]

    def channels(hex_color: str) -> tuple[int, int, int]:
        return (
            int(hex_color[1:3], 16),
            int(hex_color[3:5], 16),
            int(hex_color[5:7], 16),
        )

    top_rgb = channels(risk_color(top[0]["flood_probability_12h"]))
    bottom_rgb = channels(risk_color(bottom["flood_probability_12h"]))
    assert top_rgb[0] > top_rgb[1], "highest-risk zone must be warm/red"
    assert bottom_rgb[1] > bottom_rgb[0], "lowest-risk zone must be green"

    for zone in ranked:
        probability = float(zone["flood_probability_12h"])
        assert f'>{probability:.0%}</text>' in svg, f"{zone['zone_name']} % label missing"
        assert f"to=\"1 {probability:.3f}\"" in svg, f"{zone['zone_name']} water level missing"

    assert "rainfall 250mm/24h" in svg, "scenario footer embedded"


async def test_parallel_graph_preserves_both_results():
    import agents.pipeline as pipeline

    def build_resolution(conflict: dict) -> dict:
        return {
            "decision": f"Resolved — time-share {conflict['resource']}.",
            "justification": "stub (LLM-free parallel test)",
            "winning_side": "evacuation_advocate",
        }

    async def fake_run_debate(conflict, logistics_plan, writer):
        turns = []
        for t in (1, 2, 3, 4):
            agent = "evacuation_advocate" if t % 2 == 1 else "logistics_advocate"
            message = (
                f"{conflict['id']} T{t} — {conflict['claim_a']['zone']} vs "
                f"{conflict['claim_b']['zone']}."
            )
            turns.append({"turn": t, "agent": agent, "message": message})
            if writer is not None:
                sent = writer({
                    "type": "negotiation_turn",
                    "agent": "arbiter",
                    "message": message,
                    "data": {"conflict_id": conflict["id"], "turn": t, "streaming": True},
                    "timestamp": "2026-01-01T00:00:00Z",
                })
                if sent is not None:
                    await sent
        return turns, build_resolution(conflict), None

    async def fake_generate_briefing(prediction, logistics_plan, resolution):
        return {
            "headline": "Major flood imminent in 5 districts.",
            "risk_summary": "Deterministic stub for the parallel branch test.",
            "resource_plan": "Ambulances and tankers staged.",
            "conflict_resolution": "Bridge shared across shifts.",
            "recommended_actions": ["Evacuate", "Stage shelters", "Pre-position pumps"],
        }, None

    original_debate = pipeline.run_debate
    original_briefing = pipeline.generate_briefing
    pipeline.run_debate = fake_run_debate
    pipeline.generate_briefing = fake_generate_briefing
    try:
        state = ScenarioState(scenario_id="sim-parallel", raw_data=FLOOD)
        graph = build_graph()

        seen_nodes: list[str] = []
        events = []
        async for mode, chunk in graph.astream(state, stream_mode=["updates", "custom"]):
            if mode == "updates":
                for node_name, update in chunk.items():
                    seen_nodes.append(node_name)
                    events.extend(update.get("events", []))
                    for k, v in update.items():
                        if k == "events":
                            state.events.extend(v)
                        else:
                            setattr(state, k, v)
    finally:
        pipeline.run_debate = original_debate
        pipeline.generate_briefing = original_briefing

    agents = {e["agent"] for e in events if e["type"] == "agent_result"}
    assert {"prediction", "logistics", "simulation"} <= agents, f"missing results: {agents}"
    assert sum(1 for e in events if e["type"] == "resolution") == len(state.conflicts)
    assert len(state.negotiation_log) == len(state.conflicts)
    assert state.simulation is not None, "state.simulation not set"
    assert state.status == "complete"

    sim_events = [e for e in events if e["agent"] == "simulation"]
    assert len(sim_events) == 1, "exactly one simulation result event"
    assert sim_events[0]["data"]["svg"], "no SVG payload"
    assert sim_events[0]["data"]["source"] == "svg-fallback"

    pred_at = seen_nodes.index("prediction")
    logi_at = seen_nodes.index("logistics")
    sim_at = seen_nodes.index("simulation")
    nego_at = seen_nodes.index("negotiator")
    assert pred_at < logi_at and pred_at < sim_at
    assert max(logi_at, sim_at) < nego_at, "negotiator must wait for the fan-in join"


async def test_hf_route_returns_data_uri():
    import agents.simulation as sim

    fake_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )

    original = (sim.HF_TOKEN, sim.HF_MODEL, sim.HF_MODE)
    sim.HF_TOKEN = "hf_test"
    sim.HF_MODEL = "stabilityai/sd-turbo"
    sim.HF_MODE = "image"

    async def fake_post(self, url, **kwargs):
        assert url.endswith("/hf-inference/stabilityai/sd-turbo"), url
        assert kwargs["headers"]["x-wait-for-model"] == "true"
        class FakeResponse:
            status_code = 200
            def raise_for_status(self):
                return None
        response = FakeResponse()
        response.content = fake_png
        return response

    sim.httpx.AsyncClient.post = fake_post
    try:
        payload, source, error = await sim.render_simulation(FLOOD, build_prediction(FLOOD))
    finally:
        sim.HF_TOKEN, sim.HF_MODEL, sim.HF_MODE = original
        import httpx as real_httpx
        sim.httpx.AsyncClient.post = real_httpx.AsyncClient.post

    assert error is None
    assert payload["image_url"].startswith("data:image/png;base64,")
    assert "sd-turbo" in source


async def main():
    test_risk_color_boundaries()
    test_svg_overlay_content()
    await test_parallel_graph_preserves_both_results()

    print("========== SIMULATION NODE (svg-fallback path) ==========")
    state = ScenarioState(scenario_id="sim-viz", raw_data=FLOOD)
    payload, source, error = await render_simulation(FLOOD, build_prediction(FLOOD))
    print(f"source: {source}")
    print(f"error: {error or 'none'}")
    print(f"svg: {len(payload['svg'])} chars, starts {payload['svg'][:34]!r}")

    if not HF_TOKEN:
        print("\n(no HF_API_TOKEN in .env — real HF call skipped; use mocked test)")

    await test_hf_route_returns_data_uri()
    print("\nALL SIMULATION ASSERTIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())