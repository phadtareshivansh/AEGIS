import asyncio
import json
import os
from typing import AsyncGenerator

import httpx
from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

AEGIS_LLM_STUB = os.getenv("AEGIS_LLM_STUB", "0") == "1"

DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.7
RETRY_DELAY_SECONDS = float(os.getenv("AEGIS_LLM_RETRY_DELAY", "1.0"))


def _stub_content(system_prompt: str, full_messages: list[dict]) -> str:
    """Deterministic canned output for the AEGIS_LLM_STUB test seam.

    Keyed off the system prompt so the same stub correctly feeds the arbiter
    (JSON resolution), the briefing agent (JSON briefing), and advocate turns
    (plain streaming text). Never reached unless AEGIS_LLM_STUB=1.
    """
    sp = system_prompt.lower()
    if "neutral arbiter" in sp or "winning_side" in sp:
        side = os.getenv("AEGIS_LLM_STUB_ARBITER_SIDE", "").strip().lower() or "evacuation"
        return json.dumps(
            {
                "decision": "Time-share the route: evening evacuation window 18:00-20:00, "
                "then logistics convoys overnight.",
                "justification": "Banking on the 12h window, both advocates have merit; "
                "the time-split meets both.",
                "winning_side": side,
            }
        )
    if "briefing" in sp and "headline" in sp:
        context = next(
            (m.get("content", "") for m in reversed(full_messages) if m.get("role") == "user"),
            "",
        )
        conflict_resolution = _stub_conflict_lines(context)
        return json.dumps(
            {
                "headline": "tutta tutti tutti STUB — flood response underway",
                "risk_summary": "stub: 3 zones at elevated flood risk (SVG simulation)",
                "resource_plan": "stub: allocation matching logistics plan",
                "conflict_resolution": conflict_resolution,
                "recommended_actions": [
                    "Begin evacuations from highest-risk zones",
                    "Hold logistics convoys for evening window",
                ],
            }
        )
    last_user = next(
        (m.get("content", "") for m in reversed(full_messages) if m.get("role") == "user"), ""
    )
    return f"STUB ADVOCATE TURN: this flood is urgent; allocate capacity to the exposed zone. ({last_user[:80]})"


def _stub_conflict_lines(context: str) -> str:
    """Echo the RESOLVED CONFLICTS block from the summarized briefing context.

    Keeps the stub briefing faithful to the resolution decisions actually fed
    to the agent, so the override/approve verification can assert on the final
    briefing content.
    """
    marker = "RESOLVED CONFLICTS:"
    idx = context.find(marker)
    if idx == -1:
        return "stub: no conflict resolution lines parsed"
    rest = context[idx + len(marker):].strip()
    lines = [ln.strip("- ").strip() for ln in rest.splitlines() if ln.strip()]
    return "stub: " + " / ".join(lines) if lines else "stub: none listed"


def _stub_tuple(
    system_prompt: str, full_messages: list[dict]
) -> tuple[list[str], str]:
    """Return (chunks, full_text) deterministically derived from the request."""
    text = _stub_content(system_prompt, full_messages)
    mid = max(1, len(text) // 2)
    return [text[:mid], text[mid:]], text


class LLMUnavailableError(Exception):
    """Raised when the configured LLM provider cannot be reached or returns an error."""


def _groq_client():
    from groq import AsyncGroq

    return AsyncGroq(api_key=GROQ_API_KEY)


def _build_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    return [{"role": "system", "content": system_prompt}, *messages]


async def _groq_generate(
    full_messages: list[dict], max_tokens: int, temperature: float
) -> str:
    client = _groq_client()
    completion = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=full_messages,
        temperature=temperature,
        max_completion_tokens=max_tokens,
    )
    return completion.choices[0].message.content or ""


async def _groq_generate_stream(
    full_messages: list[dict], max_tokens: int, temperature: float
) -> AsyncGenerator[str, None]:
    client = _groq_client()
    stream = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=full_messages,
        temperature=temperature,
        max_completion_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


async def _ollama_generate(
    full_messages: list[dict], max_tokens: int, temperature: float
) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": full_messages,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            },
        )
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "")


async def _ollama_generate_stream(
    full_messages: list[dict], max_tokens: int, temperature: float
) -> AsyncGenerator[str, None]:
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": full_messages,
                "stream": True,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = __import__("json").loads(line)
                content = chunk.get("message", {}).get("content", "")
                if content:
                    yield content


def _make_call(full_messages: list[dict], max_tokens: int, temperature: float):
    if LLM_PROVIDER == "groq":
        return _groq_generate(full_messages, max_tokens, temperature)
    if LLM_PROVIDER == "ollama":
        return _ollama_generate(full_messages, max_tokens, temperature)
    raise LLMUnavailableError(
        f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r} (expected 'groq' or 'ollama')"
    )


def _make_stream(
    full_messages: list[dict], max_tokens: int, temperature: float
):
    if LLM_PROVIDER == "groq":
        return _groq_generate_stream(full_messages, max_tokens, temperature)
    if LLM_PROVIDER == "ollama":
        return _ollama_generate_stream(full_messages, max_tokens, temperature)
    raise LLMUnavailableError(
        f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r} (expected 'groq' or 'ollama')"
    )


async def generate(
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
) -> str:
    """Generate a full text completion. Provider-agnostic."""
    full_messages = _build_messages(system_prompt, messages)
    if AEGIS_LLM_STUB:
        return _stub_content(system_prompt, full_messages)

    for attempt in range(1, 3):
        try:
            return await asyncio.wait_for(
                _make_call(full_messages, max_tokens, temperature), timeout=30
            )
        except Exception as exc:
            if attempt >= 2:
                raise LLMUnavailableError(
                    f"{LLM_PROVIDER} failed after retry: {type(exc).__name__}: {exc}"
                ) from exc
            await asyncio.sleep(RETRY_DELAY_SECONDS)

    raise LLMUnavailableError(f"{LLM_PROVIDER} failed before any attempt")


async def generate_stream(
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
) -> AsyncGenerator[str, None]:
    """Stream text chunks as they arrive. Provider-agnostic."""
    full_messages = _build_messages(system_prompt, messages)
    if AEGIS_LLM_STUB:
        chunks, _ = _stub_tuple(system_prompt, full_messages)
        for chunk in chunks:
            yield chunk
        return

    attempt = 0
    while True:
        attempt += 1
        try:
            async for chunk in _make_stream(full_messages, max_tokens, temperature):
                yield chunk
            return
        except Exception as exc:
            if attempt >= 2:
                raise LLMUnavailableError(
                    f"{LLM_PROVIDER} stream failed after retry: {type(exc).__name__}: {exc}"
                ) from exc
            await asyncio.sleep(RETRY_DELAY_SECONDS)