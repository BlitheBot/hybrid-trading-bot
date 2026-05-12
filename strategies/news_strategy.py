import asyncio
import json
from datetime import datetime, timedelta
import pytz
import anthropic

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from config import Config
from data.sp500_tickers import SP500_TICKERS
from strategies.base_strategy import BaseStrategy

# ── Trusted source multipliers ──────────────────────────────────────────────
HIGH_TRUST_SOURCES = {"bloomberg", "reuters", "wsj", "cnbc", "wall street journal", "financial times", "ft.com"}

# ── Keyword fallback scoring (used if Claude call fails) ────────────────────
BULLISH_KEYWORDS = {
    "earnings beat": 8, "record revenue": 8, "raised guidance": 7, "buyback": 6,
    "dividend increase": 6, "acquisition": 5, "upgrade": 6, "partnership": 5,
    "fda approval": 9, "patent": 5, "beat estimates": 7, "strong quarter": 7,
}
BEARISH_KEYWORDS = {
    "earnings miss": 2, "lawsuit": 3, "downgrade": 3, "recall": 2, "investigation": 3,
    "layoffs": 4, "guidance cut": 2, "missed estimates": 2, "regulatory action": 3,
    "bankruptcy": 1, "fraud": 1, "weak quarter": 3,
}


def _get_scan_sleep_seconds() -> float:
    """Return a dynamic sleep interval based on the current market time (EST)."""
    now = datetime.now(pytz.timezone("America/New_York"))
    hour = now.hour + now.minute / 60.0
    weekday = now.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun

    if weekday >= 5:
        return 15 * 60  # Weekends — 15 minutes
    if 9.5 <= hour < 10.5:
        return 60  # First hour of open — 60 seconds (highest volume)
    if 10.5 <= hour < 12.0:
        return 2 * 60  # Normal trading hours — 2 minutes
    if 12.0 <= hour < 14.0:
        return 10 * 60  # Midday lull — 10 minutes
    if 14.0 <= hour < 16.0:
        return 2 * 60  # Afternoon session — 2 minutes
    return 15 * 60  # After-hours / pre-market — 15 minutes


def _keyword_score(headline: str) -> tuple[float, str]:
    """Fallback: score a headline using keyword matching. Returns (score, sentiment)."""
    lower = headline.lower()
    bull_score = max((v for k, v in BULLISH_KEYWORDS.items() if k in lower), default=0)
    bear_score = max((v for k, v in BEARISH_KEYWORDS.items() if k in lower), default=0)
    if bull_score > bear_score:
        return float(bull_score), "bullish"
    if bear_score > bull_score:
        return float(bear_score), "bearish"
    return 0.0, "neutral"


def _source_multiplier(source: str) -> float:
    """Return a trust multiplier based on the news source."""
    if any(trusted in source.lower() for trusted in HIGH_TRUST_SOURCES):
        return 1.5
    return 0.7


class NewsStrategy(BaseStrategy):
    """
    Polls Alpaca's Benzinga news feed for S&P 500 tickers, scores each headline
    using Anthropic Claude (with keyword fallback), and returns trade signals when
    the composite score passes configured thresholds.
    """

    def __init__(self, name: str = "News Sentiment"):
        super().__init__(name)
        self.news_client = NewsClient(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
        )
        self._claude = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self._last_seen: dict[str, datetime] = {}
        self._last_articles_scanned: int = 0

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _is_on_cooldown(self, ticker: str) -> bool:
        if ticker not in self._last_seen:
            return False
        return datetime.now(pytz.utc) - self._last_seen[ticker] < timedelta(hours=Config.NEWS_DEDUP_HOURS)

    def _mark_seen(self, ticker: str):
        self._last_seen[ticker] = datetime.now(pytz.utc)

    def _score_with_claude(self, ticker: str, headline: str, summary: str) -> dict:
        """
        Ask Claude to score the headline. Returns a dict with keys:
          sentiment, score (0-10), confidence (0-10), reasoning, action
        Falls back to keyword scoring on any error.
        """
        prompt = f"""You are an expert stock market analyst. Analyze the following news headline and summary for {ticker}.

Headline: {headline}
Summary: {summary if summary else "(no summary available)"}

Respond with ONLY a valid JSON object using exactly this schema:
{{
  "sentiment": "bullish" | "bearish" | "neutral",
  "score": <integer 0-10, where 10=extremely bullish, 0=extremely bearish, 5=neutral>,
  "confidence": <integer 0-10, confidence in the score>,
  "reasoning": "<one sentence explanation>",
  "action": "buy" | "sell" | "hold"
}}"""
        try:
            message = self._claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            # Strip markdown code fences if Claude wraps output
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as e:
            print(f"[NewsStrategy] Claude call failed for {ticker}: {e}. Using keyword fallback.")
            kw_score, kw_sentiment = _keyword_score(headline)
            return {
                "sentiment": kw_sentiment,
                "score": kw_score,
                "confidence": 5,
                "reasoning": "Keyword fallback scoring (Claude unavailable).",
                "action": "buy" if kw_sentiment == "bullish" else ("sell" if kw_sentiment == "bearish" else "hold"),
            }

    # ── Main async scan ──────────────────────────────────────────────────────

    async def scan_once(self) -> list[dict]:
        """
        Fetch the latest news for all S&P 500 tickers and return a list of
        actionable signal dicts for any article that crosses the alert threshold.
        """
        signals = []
        try:
            batch_size = Config.NEWS_BATCH_SIZE
            all_articles = []
            for i in range(0, len(SP500_TICKERS), batch_size):
                batch = SP500_TICKERS[i : i + batch_size]
                req = NewsRequest(
                    symbols=",".join(batch),
                    start=datetime.now(pytz.utc) - timedelta(hours=Config.NEWS_DEDUP_HOURS),
                    sort="desc",
                    limit=10,
                )
                for attempt in range(3):
                    try:
                        resp = await asyncio.to_thread(self.news_client.get_news, req)
                        if resp and hasattr(resp, "news"):
                            all_articles.extend(resp.news)
                        break  # success
                    except Exception as e:
                        err = str(e).lower()
                        if "429" in err or "rate limit" in err or "too many" in err:
                            wait = 10 * (attempt + 1)
                            print(f"[NewsStrategy] Rate limited on batch {i//batch_size}, waiting {wait}s (attempt {attempt+1}/3)...")
                            await asyncio.sleep(wait)
                        else:
                            print(f"[NewsStrategy] Batch fetch error (batch {i//batch_size}): {e}")
                            break

            self._last_articles_scanned = len(all_articles)

            for article in all_articles:
                tickers = getattr(article, "symbols", []) or []
                for ticker in tickers:
                    if ticker not in SP500_TICKERS:
                        continue
                    if self._is_on_cooldown(ticker):
                        continue

                    headline = article.headline or ""
                    summary = article.summary or ""
                    source = article.source or ""

                    result = await asyncio.to_thread(
                        self._score_with_claude, ticker, headline, summary
                    )

                    raw_score = result.get("score", 5)
                    confidence = result.get("confidence", 5)
                    sentiment = result.get("sentiment", "neutral")
                    action = result.get("action", "hold")
                    reasoning = result.get("reasoning", "")

                    # Normalise score to 0-10 range and apply source multiplier
                    multiplier = _source_multiplier(source)
                    print(f"[NewsStrategy] {ticker}: source='{source or 'unknown'}' → multiplier={multiplier:.1f}x (raw={raw_score} confidence={confidence})")
                    strength = (raw_score * confidence / 10.0) * multiplier
                    strength = round(min(strength, 20.0), 2)

                    if strength >= Config.NEWS_SIGNAL_ALERT_THRESHOLD:
                        self._mark_seen(ticker)
                        signals.append({
                            "ticker": ticker,
                            "headline": headline,
                            "source": source,
                            "sentiment": sentiment,
                            "score": raw_score,
                            "confidence": confidence,
                            "strength": strength,
                            "action": action,
                            "reasoning": reasoning,
                            "auto_trade": strength >= Config.NEWS_SIGNAL_AUTO_TRADE_THRESHOLD,
                        })

        except Exception as e:
            print(f"[NewsStrategy] scan_once error: {e}")

        return signals

    # ── BaseStrategy interface stubs (unused for this polling strategy) ──────

    def generate_signals(self, data, *args, **kwargs):
        """Not used — NewsStrategy is event-driven via scan_once()."""
        return None

    def execute_trade(self, signal, trading_client, risk_percent, stop_loss_percent,
                      take_profit_percent, max_buying_power_utilization_percent):
        """Not used directly — bot.py routes execution."""
        pass
