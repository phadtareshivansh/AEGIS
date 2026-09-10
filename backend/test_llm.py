import asyncio

from llm.client import LLM_PROVIDER, LLMUnavailableError, generate


async def main() -> None:
    system_prompt = "You are a terse assistant. Reply with only the requested output."
    messages = [{"role": "user", "content": "Say hello in exactly 5 words"}]

    print(f"Using provider: {LLM_PROVIDER}")
    result = await generate(system_prompt, messages, max_tokens=32, temperature=0.2)
    print(f"Result: {result!r}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except LLMUnavailableError as exc:
        print(f"LLM unavailable: {exc}")
        raise SystemExit(1)