"""
Grok (xAI) X/Twitter crypto sentiment strategy.

Calls the xAI Grok API every 30 minutes with live X/Twitter search enabled
to score current BTC and ETH sentiment on the platform.

Signals fire when sentiment is strongly one-sided (score ≥ GROK_ALERT_THRESHOLD
or ≤ 10 - GROK_ALERT_THRESHOLD). Alert-only — no auto-trading.

Requires:
  - GROK_API_KEY in env (from console.x.ai)
  - GROK_ENABLED = True in config
"""
import asyncio
import json

import pytz
import requests
from datetime import datetime

from config import Config
from strategies.base_strategy import BaseStrategy

_GROK_URL = "https://api.x.ai/v1/responses"
_GROK_MODEL = "grok-3-mini"

_PROMPT = """You are a financial market analyst monitoring crypto sentiment on X (Twitter).

Analyze current sentiment on X/Twitter for Bitcoin (BTC) and Ethereum (ETH).
Focus on: price predictions, bullish/bearish posts, whale activity mentions,
fear/greed indicators, and dominant retail narrative right now.

Respond with ONLY valid JSON using exactly this schema — no other text:
{
  "btc": {
    "sentiment": "bullish" | "bearish" | "neutral",
    "score": <integer 0-10, where 10=extremely bullish, 0=extremely bearish, 5=neutral>,
    "confidence": <integer 0-10>,
    "reasoning": "<two sentence summary of dominant X narrative>",
    "dominant_theme": "<one of: moon, dip_buy, fear, fomo, whale_alert, macro_concern, neutral>"
  },
  "eth": {
    "sentiment": "bullish" | "bearish" | "neutral",
    "score": <integer 0-10>,
    "confidence": <integer 0-10>,
    "reasoning": "<two sentence summary of dominant X narrative>",
    "dominant_theme": "<one of: moon, dip_buy, fear, fomo, whale_alert, macro_concern, neutral>"
  }
}"""


class GrokStrategy(BaseStrategy):
    """
    Uses Grok's live X/Twitter search to score BTC/ETH sentiment.
    Alert-only — never auto-trades.
    """

    def __init__(self, name: str = "Grok X/Twitter Sentiment"):
        super().__init__(name)

    def _call_grok(self) -> dict | None:
        """Synchronous Grok API call — called via asyncio.to_thread."""
        if not Config.GROK_API_KEY:
            return None
        try:
            payload = {
                "model": _GROK_MODEL,
                "input": [{"role": "user", "content": _PROMPT}],
                "max_output_tokens": 512,
                "temperature": 0.1,
                "tools": [{"type": "x_search"}],
            }
            resp = requests.post(
                _GROK_URL,
                headers={
                    "Authorization": f"Bearer {Config.GROK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            if resp.status_code == 401:
                print("[GrokStrategy] 401 UNAUTHORIZED — check GROK_API_KEY")
                return None
            if resp.status_code == 403:
                print("[GrokStrategy] 403 FORBIDDEN — API key may lack live search access")
                return None
            resp.raise_for_status()
            data = resp.json()
            msg_item = next((o for o in data.get("output", []) if o.get("type") == "message"), None)
            if not msg_item:
                print("[GrokStrategy] No message item in response output")
                return None
            raw = msg_item["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[GrokStrategy] JSON parse error: {e}")
            return None
        except Exception as e:
            print(f"[GrokStrategy] API call failed: {e}")
            return None

    async def scan_once(self) -> list[dict]:
        """
        Calls Grok with live X search and returns signal dicts for any coin
        whose sentiment score crosses the alert threshold.
        """
        result = await asyncio.to_thread(self._call_grok)
        if not result:
            return []

        signals = []
        threshold = Config.GROK_ALERT_THRESHOLD
        for coin_key, display in (("btc", "BTC/USD"), ("eth", "ETH/USD")):
            coin_data = result.get(coin_key, {})
            score      = int(coin_data.get("score", 5))
            confidence = int(coin_data.get("confidence", 5))
            sentiment  = coin_data.get("sentiment", "neutral")
            reasoning  = coin_data.get("reasoning", "")
            theme      = coin_data.get("dominant_theme", "neutral")

            bullish_hit = score >= threshold
            bearish_hit = score <= (10 - threshold)

            if bullish_hit or bearish_hit:
                print(
                    f"[GrokStrategy] {display}: score={score} confidence={confidence} "
                    f"sentiment={sentiment} theme={theme}"
                )
                signals.append({
                    "coin":       display,
                    "sentiment":  sentiment,
                    "score":      score,
                    "confidence": confidence,
                    "reasoning":  reasoning,
                    "theme":      theme,
                    "auto_trade": False,
                })

        return signals

    # ── BaseStrategy stubs ───────────────────────────────────────────────────

    def generate_signals(self, data, *args, **kwargs):
        return None

    def execute_trade(self, signal, trading_client, risk_percent, stop_loss_percent,
                      take_profit_percent, max_buying_power_utilization_percent):
        pass
