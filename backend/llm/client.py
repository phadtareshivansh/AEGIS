import asyncio
import os
from typing import AsyncGenerator

import httpx
from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.7
RETRY_DELAY_SECONDS = 1.0


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