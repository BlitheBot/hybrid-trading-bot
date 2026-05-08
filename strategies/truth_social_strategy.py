import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
import requests
import anthropic

from config import Config
from data.sp500_tickers import SP500_TICKERS
from strategies.base_strategy import BaseStrategy

# Include crypto pairs alongside equities
VALID_TICKERS = set(SP500_TICKERS) | {"BTC/USD", "ETH/USD"}

TRUTH_RSS_URL = "https://truthsocial.com/@realDonaldTrump/feed.rss"


class TruthSocialStrategy(BaseStrategy):
    """
    Polls Trump's Truth Social RSS feed every 60 seconds, scores posts with
    Anthropic Claude for market relevance, and returns delayed-entry trade signals
    for affected S&P 500 tickers / BTC / ETH.
    """

    def __init__(self, name: str = "Truth Social Sentiment"):
        super().__init__(name)
        self._claude = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        # Set of already-processed post GUIDs / links
        self._seen_posts: set = set()
        # {ticker: baseline_price} captured just before we wait for confirmation
        self._baseline_prices: dict[str, float] = {}

    # ── RSS helpers ──────────────────────────────────────────────────────────

    def _fetch_rss(self) -> list[dict]:
        """Fetch and parse the RSS feed. Returns list of {guid, title, description, link}."""
        posts = []
        try:
            resp = requests.get(TRUTH_RSS_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                return posts
            for item in channel.findall("item"):
                guid_el = item.find("guid")
                link_el = item.find("link")
                guid = (guid_el.text if guid_el is not None else None) or (link_el.text if link_el is not None else "")
                title_el = item.find("title")
                desc_el = item.find("description")
                posts.append({
                    "guid": guid,
                    "title": title_el.text if title_el is not None else "",
                    "description": desc_el.text if desc_el is not None else "",
                    "link": link_el.text if link_el is not None else "",
                })
        except Exception as e:
            print(f"[TruthSocialStrategy] RSS fetch error: {e}")
        return posts

    # ── Claude analysis ──────────────────────────────────────────────────────

    def _analyse_post(self, post_text: str) -> dict:
        """
        Score a Truth Social post for market relevance.
        Returns dict with keys: is_market_relevant, tickers, sentiment, score,
        confidence, reasoning, action.
        Falls back to safe defaults on error.
        """
        prompt = f"""You are an expert quantitative analyst monitoring political statements for market impact.
Analyse the following post by Donald Trump on Truth Social for stock market relevance.

Post:
\"\"\"{post_text}\"\"\"

Respond with ONLY a valid JSON object using exactly this schema:
{{
  "is_market_relevant": true | false,
  "tickers": ["<TICKER>", ...],
  "sentiment": "bullish" | "bearish" | "neutral",
  "score": <integer 0-10>,
  "confidence": <integer 0-10>,
  "reasoning": "<one sentence>",
  "action": "buy" | "sell" | "hold"
}}

Rules:
- Only include US equity tickers (e.g. AAPL, TSLA) or "BTC/USD" / "ETH/USD" that are directly named or strongly implied.
- Set is_market_relevant=false for posts about golf, personal matters, rally dates, or other non-market topics.
- score: 10=extremely bullish (tariff removed, huge deal), 0=extremely bearish, 5=neutral."""

        try:
            message = self._claude.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as e:
            print(f"[TruthSocialStrategy] Claude call failed: {e}")
            return {
                "is_market_relevant": False,
                "tickers": [],
                "sentiment": "neutral",
                "score": 5,
                "confidence": 0,
                "reasoning": f"Analysis unavailable: {e}",
                "action": "hold",
            }

    # ── Delayed entry validation ──────────────────────────────────────────────

    async def _validate_and_signal(
        self,
        ticker: str,
        post_text: str,
        result: dict,
        strength: float,
        trading_client,
    ) -> dict | None:
        """
        Wait 60 seconds after first detecting the post, then confirm price has
        risen >= 1% from baseline before generating a trade signal.
        Returns a signal dict or None.
        """
        try:
            # Capture baseline price
            position_or_price = trading_client.get_latest_trade(ticker)
            baseline = float(position_or_price.price)
        except Exception as e:
            print(f"[TruthSocialStrategy] Could not fetch baseline price for {ticker}: {e}")
            return None

        print(f"[TruthSocialStrategy] Waiting 60s for price confirmation on {ticker} (baseline ${baseline:.2f})...")
        await asyncio.sleep(60)

        try:
            latest = trading_client.get_latest_trade(ticker)
            current_price = float(latest.price)
        except Exception as e:
            print(f"[TruthSocialStrategy] Could not fetch current price for {ticker}: {e}")
            return None

        pct_change = (current_price - baseline) / baseline * 100
        if pct_change < 1.0:
            print(f"[TruthSocialStrategy] Price confirmation failed for {ticker}: only {pct_change:.2f}% move.")
            return None

        print(f"[TruthSocialStrategy] ✅ Price confirmed +{pct_change:.2f}% for {ticker}. Generating signal.")
        return {
            "ticker": ticker,
            "post_text": post_text,
            "sentiment": result.get("sentiment", "neutral"),
            "score": result.get("score", 5),
            "confidence": result.get("confidence", 5),
            "strength": strength,
            "action": result.get("action", "buy"),
            "reasoning": result.get("reasoning", ""),
            "baseline_price": baseline,
            "current_price": current_price,
            "pct_change": pct_change,
            "auto_trade": strength >= Config.TRUTH_SOCIAL_AUTO_TRADE_THRESHOLD,
            # Risk overrides for Truth Social trades
            "stop_loss_percent": Config.TRUTH_SOCIAL_STOP_LOSS,
            "take_profit_percent": Config.TRUTH_SOCIAL_TAKE_PROFIT,
            "position_size_multiplier": Config.TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER,
        }

    # ── Main async scan ──────────────────────────────────────────────────────

    async def scan_once(self, trading_client=None) -> list[dict]:
        """
        Fetch the RSS feed, analyse each new post, and return confirmed signal
        dicts for any post that passes the alert threshold and the 60-second
        price confirmation check.
        """
        signals = []
        posts = await asyncio.to_thread(self._fetch_rss)

        for post in posts:
            guid = post["guid"]
            if guid in self._seen_posts:
                continue  # Already processed

            self._seen_posts.add(guid)
            post_text = f"{post['title']} {post['description']}".strip()

            result = await asyncio.to_thread(self._analyse_post, post_text)

            if not result.get("is_market_relevant", False):
                continue

            affected_tickers = [t for t in result.get("tickers", []) if t in VALID_TICKERS]
            if not affected_tickers:
                continue

            raw_score = result.get("score", 5)
            confidence = result.get("confidence", 5)
            strength = round((raw_score * confidence / 10.0), 2)

            if strength < Config.TRUTH_SOCIAL_ALERT_THRESHOLD:
                continue

            for ticker in affected_tickers:
                if trading_client and strength >= Config.TRUTH_SOCIAL_AUTO_TRADE_THRESHOLD:
                    # Run delayed confirmation in parallel — bot.py collects results
                    confirmed = await self._validate_and_signal(
                        ticker, post_text, result, strength, trading_client
                    )
                    if confirmed:
                        signals.append(confirmed)
                else:
                    # Alert-only signal (below auto-trade threshold)
                    signals.append({
                        "ticker": ticker,
                        "post_text": post_text,
                        "sentiment": result.get("sentiment", "neutral"),
                        "score": raw_score,
                        "confidence": confidence,
                        "strength": strength,
                        "action": result.get("action", "hold"),
                        "reasoning": result.get("reasoning", ""),
                        "auto_trade": False,
                        "stop_loss_percent": Config.TRUTH_SOCIAL_STOP_LOSS,
                        "take_profit_percent": Config.TRUTH_SOCIAL_TAKE_PROFIT,
                        "position_size_multiplier": Config.TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER,
                    })

        return signals

    # ── BaseStrategy interface stubs ─────────────────────────────────────────

    def generate_signals(self, data, *args, **kwargs):
        """Not used — TruthSocialStrategy is event-driven via scan_once()."""
        return None

    def execute_trade(self, signal, trading_client, risk_percent, stop_loss_percent,
                      take_profit_percent, max_buying_power_utilization_percent):
        """Not used directly — bot.py routes execution."""
        pass
