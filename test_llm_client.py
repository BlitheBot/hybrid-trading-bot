"""
Manual test for the OpenRouter LLM client.

Usage:
    python test_llm_client.py

Tests:
  1. All three model tiers (free flash, paid flash, pro)
  2. News batch scoring (JSON response_format)
  3. Bull/bear debate with web search + JSON synthesis
  4. Friday macro brief (web search)

Set OPENROUTER_API_KEY in .env before running.
"""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from llm_client import (
    call_llm_with_model, LLMError, LLMResponse,
    MODEL_FLASH, MODEL_PRO, MODEL_DEEPSEEK_CHAT,
)


def _hr(label: str):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)


async def test_model_tiers():
    _hr("Test 1 — Model tiers")
    for model_id, label in [
        (MODEL_DEEPSEEK_CHAT, "DeepSeek Chat (default)"),
        (MODEL_FLASH,         "DeepSeek Flash"),
        (MODEL_PRO,           "DeepSeek Pro"),
    ]:
        try:
            resp = await call_llm_with_model(
                model_id,
                "Say 'pong' and nothing else.",
                max_tokens=20,
            )
            print(f"  [{label}] OK: {resp.text!r}")
        except LLMError as e:
            print(f"  [{label}] FAIL: {e}")


async def test_news_batch():
    _hr("Test 2 — News batch scoring (JSON)")
    prompt = (
        "You are an expert stock market analyst. Score each of the following "
        "news items for their likely price impact on the named ticker.\n\n"
        "[1] Ticker: AAPL\nHeadline: Apple beats Q2 earnings by 15%, raises full-year guidance\n"
        "[2] Ticker: META\nHeadline: Meta misses revenue estimate, announces 10,000 layoffs\n\n"
        'Return JSON only: {"items":[{"index":<1-based int>,"sentiment":"bullish"|"bearish"|"neutral",'
        '"score":<0-10>,"confidence":<0-10>,"reasoning":"<one sentence>","action":"buy"|"sell"|"hold"}]}'
    )
    try:
        resp = await call_llm_with_model(
            MODEL_DEEPSEEK_CHAT, prompt,
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        parsed = json.loads(resp.text)
        items = parsed.get("items", parsed)
        for item in items:
            print(f"  [{item['index']}] {item['sentiment'].upper()} score={item['score']} — {item['reasoning']}")
    except LLMError as e:
        print(f"  FAIL: {e}")
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed: {e}")


async def test_debate_with_citations():
    _hr("Test 3 — Bull/bear debate with web search + JSON synthesis")
    symbol = "AAPL"
    web_plugin = [{"id": "web", "max_results": 1}]

    try:
        bull_resp, bear_resp = await asyncio.gather(
            call_llm_with_model(
                MODEL_FLASH,
                f"You are a bullish analyst. Search for the latest news on {symbol} "
                "and make the strongest 2-sentence case FOR buying it now.",
                max_tokens=200,
                plugins=web_plugin,
            ),
            call_llm_with_model(
                MODEL_FLASH,
                f"You are a bearish analyst. Search for the latest news on {symbol} "
                "and make the strongest 2-sentence case AGAINST buying it now.",
                max_tokens=200,
                plugins=web_plugin,
            ),
        )
        print(f"  Bull: {bull_resp.text[:120]}...")
        print(f"  Bear: {bear_resp.text[:120]}...")
        print(f"  Bull citations: {bull_resp.citations}")
        print(f"  Bear citations: {bear_resp.citations}")

        synthesis = await call_llm_with_model(
            MODEL_FLASH,
            f"Bull case: {bull_resp.text}\nBear case: {bear_resp.text}\n\n"
            f"Should we buy {symbol} right now?\n"
            'Return JSON only: {"verdict":"proceed"|"skip"|"reduce_size","conviction":0.0-1.0,"reasoning":"one sentence"}',
            response_format={"type": "json_object"},
            max_tokens=150,
        )
        parsed = json.loads(synthesis.text)
        print(f"  Verdict: {parsed.get('verdict')} (conviction {parsed.get('conviction')}) — {parsed.get('reasoning')}")
    except LLMError as e:
        print(f"  FAIL: {e}")
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed: {e}")


async def test_macro_brief():
    _hr("Test 4 — Friday macro brief (web search)")
    try:
        resp = await call_llm_with_model(
            MODEL_FLASH,
            "Summarize the key macroeconomic themes and market-moving events from this week. "
            "Focus on: Fed policy signals, earnings surprises, sector rotation, and geopolitical risks "
            "affecting US equities. Write 3-4 concise bullet points.",
            plugins=[{"id": "web", "max_results": 3}],
            max_tokens=400,
        )
        print(f"  Brief:\n{resp.text}")
        if resp.citations:
            print(f"  Sources:")
            for c in resp.citations:
                print(f"    • {c['title']} — {c['url']}")
    except LLMError as e:
        print(f"  FAIL: {e}")


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set. Add it to .env and re-run.")
        sys.exit(1)
    print(f"OPENROUTER_API_KEY present (last 4: ...{api_key[-4:]})")

    await test_model_tiers()
    await test_news_batch()
    await test_debate_with_citations()
    await test_macro_brief()

    print(f"\n{'='*60}")
    print("  All tests complete. Check output above for any FAILs.")
    print('='*60)


asyncio.run(main())
