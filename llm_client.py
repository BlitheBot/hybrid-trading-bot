import httpx
from config import Config


async def call_llm(prompt: str, system: str = None, max_tokens: int = 1024) -> str:
    """Unified async LLM caller. Supports Anthropic and any OpenAI-compatible API."""
    try:
        provider = (Config.LLM_PROVIDER or "anthropic").lower()
        if provider == "anthropic":
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
            kwargs = {
                "model": "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            message = await client.messages.create(**kwargs)
            return message.content[0].text.strip()
        else:  # kimi / openai_compatible
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            async with httpx.AsyncClient(timeout=60.0) as http:
                resp = await http.post(
                    f"{Config.OPENAI_COMPATIBLE_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {Config.OPENAI_COMPATIBLE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": Config.OPENAI_COMPATIBLE_MODEL,
                        "messages": messages,
                        "max_tokens": max_tokens,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[LLMClient] call_llm failed: {e}")
        return ""
