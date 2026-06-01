import asyncio
from dataclasses import dataclass, field
import httpx
from config import Config


class LLMError(Exception):
    """Raised by call_llm_with_model() when all retry attempts fail."""


@dataclass
class LLMResponse:
    """Structured return value from call_llm_with_model()."""
    text: str
    citations: list = field(default_factory=list)  # list of {"url": str, "title": str}

    def __str__(self) -> str:
        return self.text


# Task-specific model IDs (OpenRouter path)
MODEL_FLASH_FREE    = "deepseek/deepseek-v4-flash:free"   # news scoring (free tier)
MODEL_FLASH         = "deepseek/deepseek-v4-flash"         # debate + news fallback
MODEL_PRO           = "deepseek/deepseek-v4-pro"           # discovery debate
MODEL_DEEPSEEK_CHAT = "deepseek/deepseek-chat"             # Chat V3 — sentiment aggregation
MODEL_GEMINI_FLASH  = "google/gemini-2.5-flash"            # Gemini 2.5 Flash — swing debate (thinking)

# Approximate cost per million tokens (input, output) in USD — updated May 2026
_COST_TABLE = {
    MODEL_FLASH_FREE:    (0.0, 0.0),
    MODEL_FLASH:         (0.14, 0.28),
    MODEL_PRO:           (0.27, 1.10),
    MODEL_DEEPSEEK_CHAT: (0.07, 0.28),
    MODEL_GEMINI_FLASH:  (0.15, 0.60),
    "_default":          (0.5, 1.5),
}

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENROUTER_SITE_HEADERS = {
    "HTTP-Referer": "https://blithebot.app",
    "X-Title": "BlitheBot",
}


def get_llm_cost_estimate(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Returns estimated USD cost for a single call based on approximate published rates."""
    in_rate, out_rate = _COST_TABLE.get(model_id, _COST_TABLE["_default"])
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


async def call_llm_with_model(
    model_id: str,
    prompt: str,
    system_prompt: str = None,
    response_format: dict = None,
    plugins: list = None,
    max_tokens: int = 1000,
    extra_body: dict = None,
) -> LLMResponse:
    """
    Call any OpenRouter-hosted model by explicit model_id.

    Retries up to 3 times with 2s / 4s / 8s exponential backoff.
    Raises LLMError on final failure — never returns empty string silently.
    Logs model, token usage, and estimated cost on every successful call.
    Parses url_citation annotations when plugins=[{"id":"web",...}] is used.
    """
    base_url = getattr(Config, "OPENROUTER_BASE_URL", _OPENROUTER_BASE)
    api_key = getattr(Config, "OPENROUTER_API_KEY", None)
    if not api_key:
        raise LLMError("OPENROUTER_API_KEY is not configured")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body: dict = {"model": model_id, "messages": messages, "max_tokens": max_tokens}
    if response_format:
        body["response_format"] = response_format
    if plugins:
        body["plugins"] = plugins
    if extra_body:
        body.update(extra_body)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **_OPENROUTER_SITE_HEADERS,
    }

    delays = [2, 4, 8]
    last_exc: Exception = RuntimeError("unknown")
    for attempt, delay in enumerate(delays, start=1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                resp = await http.post(f"{base_url}/chat/completions", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
                message = data["choices"][0]["message"]
                content = message.get("content", "").strip()

                # Parse url_citation annotations (present when web search plugin fires)
                citations = []
                for ann in message.get("annotations", []):
                    if ann.get("type") == "url_citation":
                        c = ann.get("url_citation", {})
                        citations.append({"url": c.get("url", ""), "title": c.get("title", "")})

                usage = data.get("usage", {})
                in_tok = usage.get("prompt_tokens", 0)
                out_tok = usage.get("completion_tokens", 0)
                cost = get_llm_cost_estimate(model_id, in_tok, out_tok)
                print(
                    f"[LLMClient] {model_id} | in={in_tok} out={out_tok} | "
                    f"est ${cost:.5f}" + (f" | {len(citations)} citation(s)" if citations else "")
                )
                return LLMResponse(text=content, citations=citations)

        except Exception as exc:
            last_exc = exc
            if attempt < len(delays):
                print(f"[LLMClient] attempt {attempt} failed ({exc}), retry in {delay}s")
                await asyncio.sleep(delay)
            else:
                print(f"[LLMClient] all {len(delays)} attempts failed: {exc}")

    raise LLMError(f"call_llm_with_model failed after {len(delays)} attempts: {last_exc}") from last_exc


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
