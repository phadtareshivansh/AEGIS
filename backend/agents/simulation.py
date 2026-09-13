"""Simulation visual for AEGIS.

Preferred route: Hugging Face Inference API free tier (Stable Diffusion /
SD-Turbo) images flat and purely decorative.
Fallback route: a deterministic animated SVG map overlay that color-codes
zones by flood_probability_12h (green → yellow → red) and animates the
affected zones "filling in" over a few seconds. Both are free; neither
requires Groq tokens.
"""

import asyncio
import base64
import html
import os
from typing import Any

import httpx

from agents.prediction import load_zones, rank_zones

HF_API_BASE = "https://api-inference.huggingface.co/models"
HF_MODEL = os.getenv("HF_SIM_MODEL", "stabilityai/sd-turbo").strip()
HF_TOKEN = os.getenv("HF_API_TOKEN", "").strip()
HF_TIMEOUT_SECONDS = 40.0

GREEN = (34, 197, 94)
YELLOW = (250, 204, 21)
RED = (239, 68, 68)

ZONE_TILE_W = 96
ZONE_TILE_H = 74
ZONE_GAP = 10
ZONE_COLS = 6

SVG_W = 720
SVG_H = 440
GRID_TOP = 100
GRID_LEFT = (SVG_W - (ZONE_COLS * (ZONE_TILE_W + ZONE_GAP) - ZONE_GAP)) // 2


def risk_color(probability: float) -> str:
    """Map a 0..1 flood probability onto a green → yellow → red gradient."""
    p = max(0.0, min(1.0, probability))
    if p < 0.5:
        start, end, t = GREEN, YELLOW, p / 0.5
    else:
        start, end, t = YELLOW, RED, (p - 0.5) / 0.5
    rgb = tuple(round(a + (b - a) * t) for a, b in zip(start, end))
    return "#%02x%02x%02x" % rgb


def _tile(y: int, x: int) -> tuple[int, int]:
    return GRID_TOP + y * (ZONE_TILE_H + ZONE_GAP), GRID_LEFT + x * (ZONE_TILE_W + ZONE_GAP)


def build_svg_overlay(
    ranked_zones: list[dict],
    at_risk_ids: set[str],
    raw_data: dict,
) -> str:
    """Deterministic, animated SVG flood-risk overlay.

    Zones are drawn as a schematic tile grid. Each tile's border and water
    are colored by flood_probability_12h; a water rect "fills in" from the
    bottom of the tile up to the probability height via SMIL animation
    (staggered so affected zones fill first, over a few seconds). SMIL
    (not CSS) is used so the animation runs inside an <img>.
    """
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_W} {SVG_H}" '
        f'width="{SVG_W}" height="{SVG_H}">',
        f'<rect x="0" y="0" width="{SVG_W}" height="{SVG_H}" fill="#0A0A0A"/>',
        f'<text x="{GRID_LEFT}" y="46" font-family="monospace" font-size="15" '
        'letter-spacing="3" fill="#E8542A">FLOOD SIMULATION</text>',
        f'<text x="{GRID_LEFT}" y="66" font-family="monospace" font-size="10" '
        'fill="#8A8A85">flood_probability_12h per zone · colour-coded overlay</text>',
        f'<line x1="{GRID_LEFT}" y1="78" x2="{SVG_W - GRID_LEFT}" y2="78" '
        'stroke="#1A1A18" stroke-width="1"/>',
    ]

    river_m = raw_data.get("river_level_m", 0.0)
    rain_mm = raw_data.get("rainfall_mm_24h", 0.0)

    for idx, zone in enumerate(ranked_zones):
        probability = float(zone["flood_probability_12h"])
        color = risk_color(probability)
        affected = zone["zone_id"] in at_risk_ids
        top, left = _tile(idx // ZONE_COLS, idx % ZONE_COLS)

        begin = f"{0.3 + idx * (0.25 if affected else 0.06):.2f}s"
        dur = "1.6s" if affected else "0.8s"

        parts.append(
            '<g transform="translate(%d %d)">' % (left, top)
        )
        parts.append(
            f'<rect x="0" y="0" width="{ZONE_TILE_W}" height="{ZONE_TILE_H}" '
            f'fill="#111110" stroke="{color}" stroke-width="1.5"/>'
        )
        parts.append(
            f'<rect x="2" y="2" width="{ZONE_TILE_W - 4}" height="{ZONE_TILE_H - 4}" '
            f'fill="none"/>'
        )
        parts.append(
            f'<text x="6" y="18" font-family="monospace" font-size="9" '
            f'fill="{"#E8542A" if affected else "#EDEDE6"}">'
            f"{html.escape(zone['zone_name'])}</text>"
        )
        parts.append(
            f'<text x="6" y="40" font-family="monospace" font-size="10" '
            f'fill="{color}">{probability:.0%}</text>'
        )
        if affected:
            parts.append(
                '<circle cx="%d" cy="12" r="3" fill="#E8542A">'
                '<animate attributeName="r" values="2;4;2" dur="1.2s" '
                'repeatCount="indefinite"/></circle>'
                % (ZONE_TILE_W - 10)
            )
        parts.append(
            '<g transform="translate(0 '
            f'{ZONE_TILE_H})"><rect width="{ZONE_TILE_W}" height="{ZONE_TILE_H}" '
            f'y="{-ZONE_TILE_H}" fill="{color}" opacity="0.82">'
            '<animateTransform attributeName="transform" type="scale" '
            f'from="1 0.001" to="1 {probability:.3f}" begin="{begin}" '
            f'dur="{dur}" fill="freeze"/></rect></g>'
        )
        parts.append("</g>")

    legend_y = GRID_TOP + 3 * (ZONE_TILE_H + ZONE_GAP) + 12
    for i, color in enumerate((risk_color(0.0), risk_color(0.5), risk_color(1.0))):
        lx = GRID_LEFT + i * 120
        parts.append(
            f'<rect x="{lx}" y="{legend_y}" width="18" height="8" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{lx + 24}" y="{legend_y + 8}" font-family="monospace" '
            f'font-size="9" fill="#8A8A85">{["0%", "50%", "100%"][i]}</text>'
        )
    parts.append(
        f'<text x="{GRID_LEFT}" y="{legend_y + 30}" font-family="monospace" '
        'font-size="9" fill="#8A8A85">'
        "water fill height = flood probability within 12h</text>"
    )
    parts.append(
        f'<text x="{SVG_W - GRID_LEFT}" y="{SVG_H - 18}" font-family="monospace" '
        f'font-size="9" fill="#565656" text-anchor="end">'
        f"rainfall {rain_mm:.0f}mm/24h · river {river_m:.1f}m</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _build_prompt(prediction: dict, raw_data: dict) -> str:
    zones = prediction.get("at_risk_zones", [])[:3]
    names = ", ".join(z["zone_name"] for z in zones) or "coastal neighbourhoods"
    probs = "; ".join(
        f"{z['zone_name']} at {z['flood_probability_12h']:.0%}"
        for z in prediction.get("at_risk_zones", [])[:3]
    )
    river_m = raw_data.get("river_level_m", 0.0)
    rain_mm = raw_data.get("rainfall_mm_24h", 0.0)
    return (
        f"photorealistic aerial satellite view of {names} flooding during a "
        f"severe storm, floodwater covering streets after {rain_mm:.0f}mm rain, "
        f"river at {river_m:.1f}m, rescue boats and emergency lights, dramatic "
        f"overcast sky, cinematic emergency-response imagery. Status: {probs}"
    )


async def _hf_generate_image(prompt: str, model: str, token: str) -> bytes:
    async with httpx.AsyncClient(timeout=HF_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{HF_API_BASE}/{model}",
            json={"inputs": prompt},
            headers={
                "Authorization": f"Bearer {token}",
                "x-wait-for-model": "true",
            },
        )
        response.raise_for_status()
        return response.content


async def render_simulation(
    raw_data: dict, prediction: dict | None
) -> tuple[dict[str, Any], str, str | None]:
    """Render the simulation visual. Prefers Hugging Face free tier.

    Returns (payload, source_label, error|None). When no HF token is set the
    SVG overlay is used directly (no error); when an HF attempt fails it
    falls back to the overlay and reports the error so the caller can note it.
    """
    prediction = prediction or {}
    zones = load_zones()
    ranked = rank_zones(
        zones,
        float(raw_data.get("rainfall_mm_24h", 0.0)),
        float(raw_data.get("river_level_m", 0.0)),
        float(raw_data.get("river_level_danger_threshold_m", 1.0)),
    )
    at_risk_ids = {z["zone_id"] for z in prediction.get("at_risk_zones", [])}

    if HF_TOKEN:
        model = HF_MODEL
        try:
            image = await asyncio.wait_for(
                _hf_generate_image(_build_prompt(prediction, raw_data), model, HF_TOKEN),
                timeout=HF_TIMEOUT_SECONDS + 5,
            )
            encoded = base64.b64encode(image).decode("ascii")
            payload = {
                "image_url": f"data:image/png;base64,{encoded}",
                "model": model,
                "source": "huggingface",
            }
            return payload, f"image · {model}", None
        except Exception as exc:  # noqa: BLE001 — degrade to the SVG overlay
            error = f"{type(exc).__name__}: {exc}"

    svg = build_svg_overlay(ranked, at_risk_ids, raw_data)
    payload = {"svg": svg, "source": "svg-fallback"}
    return payload, "animated svg overlay", error if HF_TOKEN else None