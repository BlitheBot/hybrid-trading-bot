import asyncio
import json
from datetime import datetime, timedelta
import pytz

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from sqlalchemy import text as sql_text

from config import Config
from strategies.base_strategy import BaseStrategy
from llm_client import call_llm, call_llm_with_model, LLMError, MODEL_FLASH, MODEL_DEEPSEEK_CHAT
from utils import apply_http_timeout

# ── Trusted source multipliers ──────────────────────────────────────────────
HIGH_TRUST_SOURCES = {"bloomberg", "reuters", "wsj", "cnbc", "wall street journal", "financial times", "ft.com"}

# ── Keyword fallback scoring (used if Claude call fails or is skipped) ───────
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

_CLAUDE_BATCH_SIZE = 5   # headlines per LLM batch call


def get_sentiment_score(db_engine, ticker: str) -> dict | None:
    """Return the stored sentiment record for a ticker if updated within the last 35 minutes.

    Sync — call via asyncio.to_thread. Returns None when the table is absent,
    the ticker has no entry, or the entry is stale.
    """
    if db_engine is None:
        return None
    cutoff = datetime.utcnow() - timedelta(minutes=35)
    try:
        with db_engine.connect() as conn:
            row = conn.execute(sql_text("""
                SELECT ticker, direction, score, headline_count
                FROM sentiment_scores
                WHERE ticker = :ticker AND last_updated > :cutoff
            """), {"ticker": ticker, "cutoff": cutoff}).mappings().fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[SentimentScore] read error for {ticker}: {e}")
        return None


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


def _keyword_result(headline: str, reason: str = "") -> dict:
    """Build a full result dict from keyword scoring alone."""
    kw_score, kw_sentiment = _keyword_score(headline)
    return {
        "sentiment": kw_sentiment,
        "score":     kw_score,
        "confidence": 5,
        "reasoning": reason or "Keyword scoring (Claude not called).",
        "action": "buy" if kw_sentiment == "bullish" else ("sell" if kw_sentiment == "bearish" else "hold"),
    }


def _source_multiplier(source: str) -> float:
    """Return a trust multiplier based on the news source."""
    if any(trusted in source.lower() for trusted in HIGH_TRUST_SOURCES):
        return 1.5
    return 0.7


class NewsStrategy(BaseStrategy):
    """
    Polls Alpaca's Benzinga news feed for S&P 500 tickers, scores headlines
    using Anthropic Claude in batches of up to 5, and returns trade signals
    when the composite score passes configured thresholds.

    Claude is only called for headlines with keyword score > 3 (pre-filter).
    A daily call counter enforces CLAUDE_DAILY_CALL_LIMIT; once exceeded the
    strategy falls back to keyword scoring for the rest of the calendar day.
    """

    def __init__(self, name: str = "News Sentiment", db_engine=None):
        super().__init__(name)
        self._db_engine = db_engine
        self.news_client = NewsClient(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
        )
        apply_http_timeout(self.news_client)
        self._last_seen: dict[str, datetime] = {}
        self._last_articles_scanned: int = 0
        self._claude_calls_today: int = 0
        self._claude_calls_date: str = ""   # YYYY-MM-DD in EST

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _is_on_cooldown(self, ticker: str) -> bool:
        if ticker not in self._last_seen:
            return False
        return datetime.now(pytz.utc) - self._last_seen[ticker] < timedelta(hours=Config.NEWS_DEDUP_HOURS)

    def _mark_seen(self, ticker: str):
        self._last_seen[ticker] = datetime.now(pytz.utc)

    def _reset_daily_counter_if_needed(self) -> None:
        today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
        if today != self._claude_calls_date:
            if self._claude_calls_date:
                print(
                    f"[NewsStrategy] New day — resetting Claude call counter "
                    f"(yesterday: {self._claude_calls_today} calls)"
                )
            self._claude_calls_today = 0
            self._claude_calls_date = today

    def _over_daily_limit(self) -> bool:
        return self._claude_calls_today >= Config.CLAUDE_DAILY_CALL_LIMIT

    def _get_active_tickers_sync(self) -> list[str]:
        from discovery.ticker_prioritizer import get_active_tickers
        tickers = get_active_tickers(self._db_engine)
        if not tickers:
            print("[NewsStrategy] active_tickers is empty or stale (>35 min) — skipping scan")
        return tickers

    # ── Sentiment score persistence ──────────────────────────────────────────

    def _ensure_sentiment_table(self) -> None:
        with self._db_engine.begin() as conn:
            conn.execute(sql_text("""
                CREATE TABLE IF NOT EXISTS sentiment_scores (
                    ticker         VARCHAR(10) PRIMARY KEY,
                    direction      VARCHAR(10),
                    score          INT,
                    headline_count INT,
                    last_updated   TIMESTAMPTZ DEFAULT NOW()
                )
            """))

    def _write_sentiment_scores_sync(self, scores_by_ticker: dict) -> None:
        """Upsert per-ticker aggregate sentiment to the sentiment_scores table. Sync."""
        if not self._db_engine or not scores_by_ticker:
            return
        try:
            self._ensure_sentiment_table()
            with self._db_engine.begin() as conn:
                for ticker, agg in scores_by_ticker.items():
                    conn.execute(sql_text("""
                        INSERT INTO sentiment_scores
                            (ticker, direction, score, headline_count, last_updated)
                        VALUES (:ticker, :direction, :score, :headline_count, NOW())
                        ON CONFLICT (ticker) DO UPDATE SET
                            direction      = EXCLUDED.direction,
                            score          = EXCLUDED.score,
                            headline_count = EXCLUDED.headline_count,
                            last_updated   = EXCLUDED.last_updated
                    """), {
                        "ticker":        ticker,
                        "direction":     agg["direction"],
                        "score":         agg["score"],
                        "headline_count": agg["headline_count"],
                    })
            print(f"[NewsStrategy] sentiment_scores: updated {len(scores_by_ticker)} tickers")
        except Exception as e:
            print(f"[NewsStrategy] _write_sentiment_scores_sync error: {e}")

    # ── Batch Claude scoring ─────────────────────────────────────────────────

    async def _score_batch_with_claude(self, items: list[dict]) -> list[dict]:
        """
        Score up to _CLAUDE_BATCH_SIZE headlines in a single LLM call.
        Tries the free DeepSeek Flash tier first; falls back to paid tier on LLMError.
        Returns one result dict per item in the same order.
        Falls back to keyword scoring if both LLM tiers fail.
        """
        numbered = []
        for i, item in enumerate(items, 1):
            numbered.append(f"[{i}] Ticker: {item['ticker']}")
            numbered.append(f"Headline: {item['headline']}")
            if item["summary"]:
                numbered.append(f"Summary: {item['summary'][:300]}")

        prompt = (
            "You are an expert stock market analyst. Score each of the following "
            "news items for their likely price impact on the named ticker.\n\n"
            + "\n".join(numbered)
            + '\n\nReturn JSON only: {"items":[{"index":<1-based int>,'
            '"sentiment":"bullish"|"bearish"|"neutral","score":<0-10>,'
            '"confidence":<0-10>,"reasoning":"<one sentence>",'
            '"action":"buy"|"sell"|"hold"}]}'
        )

        raw = None
        try:
            resp = await call_llm_with_model(
                MODEL_DEEPSEEK_CHAT, prompt,
                response_format={"type": "json_object"},
                max_tokens=400,
            )
            raw = resp.text
        except LLMError as e:
            print(f"[NewsStrategy] LLM call failed: {e}")

        if raw is None:
            print(f"[NewsStrategy] LLM failed. Keyword fallback for {len(items)} items.")
            return [_keyword_result(item["headline"], "Keyword fallback (LLM failed).") for item in items]

        try:
            parsed_root = json.loads(raw)
            parsed = parsed_root.get("items", parsed_root) if isinstance(parsed_root, dict) else parsed_root
            result_map = {
                r["index"]: r
                for r in parsed
                if isinstance(r, dict) and "index" in r
            }
            results = []
            for i, item in enumerate(items, 1):
                if i in result_map:
                    results.append(result_map[i])
                else:
                    results.append(_keyword_result(
                        item["headline"],
                        "Keyword fallback — item missing from batch response.",
                    ))
            return results
        except Exception as e:
            print(f"[NewsStrategy] JSON parse failed: {e}. Keyword fallback for {len(items)} items.")
            return [_keyword_result(item["headline"], "Keyword fallback (JSON parse failed).") for item in items]

    # ── Main async scan ──────────────────────────────────────────────────────

    async def scan_once(self) -> list[dict]:
        """
        Fetch latest news for all S&P 500 tickers and return actionable signals.

        Flow:
          Pass 1 — fetch articles from Alpaca (unchanged)
          Pass 2 — collect candidates: dedup + keyword prefilter (score > 3)
          Pass 3 — batch-score candidates with Claude (up to 5 per call)
          Pass 4 — compute strength, build signal list
        """
        signals = []
        try:
            active_tickers = await asyncio.to_thread(self._get_active_tickers_sync)
            if not active_tickers:
                return signals
            active_ticker_set = set(active_tickers)

            # ── Pass 1: fetch articles ────────────────────────────────────────
            batch_size = Config.NEWS_BATCH_SIZE
            all_articles = []
            for i in range(0, len(active_tickers), batch_size):
                batch = active_tickers[i : i + batch_size]
                req = NewsRequest(
                    symbols=",".join(batch),
                    start=datetime.now(pytz.utc) - timedelta(hours=Config.NEWS_DEDUP_HOURS),
                    sort="desc",
                    limit=10,
                )
                batch_num = i // batch_size
                for attempt in range(3):
                    try:
                        resp = await asyncio.to_thread(self.news_client.get_news, req)
                        articles = resp.data.get("news", []) if resp else []
                        all_articles.extend(articles)
                        break
                    except Exception as e:
                        err_raw = str(e)
                        err = err_raw.lower()
                        if "403" in err_raw:
                            print(f"[NewsStrategy] Batch {batch_num} 403 FORBIDDEN — check Alpaca subscription tier")
                        elif "401" in err_raw:
                            print(f"[NewsStrategy] Batch {batch_num} 401 UNAUTHORIZED — check API key")
                        if "429" in err or "rate limit" in err or "too many" in err:
                            wait = 10 * (attempt + 1)
                            print(f"[NewsStrategy] Rate limited on batch {batch_num}, waiting {wait}s (attempt {attempt+1}/3)...")
                            await asyncio.sleep(wait)
                        else:
                            print(f"[NewsStrategy] Batch {batch_num} error: {err_raw}")
                            break

            self._last_articles_scanned = len(all_articles)
            print(f"[NewsStrategy] scan complete — {len(all_articles)} articles across {len(active_tickers)//batch_size} batches")

            # ── Pass 2: collect candidates ────────────────────────────────────
            self._reset_daily_counter_if_needed()
            candidates: list[dict] = []

            for article in all_articles:
                tickers = getattr(article, "symbols", []) or []
                headline = article.headline or ""
                summary  = article.summary  or ""
                source   = article.source   or ""

                kw_score, kw_sentiment = _keyword_score(headline)

                for ticker in tickers:
                    if ticker not in active_ticker_set:
                        continue
                    if self._is_on_cooldown(ticker):
                        elapsed = (datetime.now(pytz.utc) - self._last_seen[ticker]).total_seconds() / 60
                        print(f"[NewsStrategy] {ticker}: skipping — signal fired {elapsed:.0f}m ago (cooldown {Config.NEWS_DEDUP_HOURS}h)")
                        continue
                    if kw_score <= 3:
                        print(f"[NewsStrategy] {ticker}: keyword score {kw_score:.0f} <= 3 — skipping Claude")
                        continue

                    candidates.append({
                        "ticker":       ticker,
                        "headline":     headline,
                        "summary":      summary,
                        "source":       source,
                        "kw_score":     kw_score,
                        "kw_sentiment": kw_sentiment,
                        "result":       None,
                    })

            # ── Pass 3: batch-score with Claude (or keyword fallback) ─────────
            if not Config.NEWS_CLAUDE_SCORING_ENABLED:
                print(f"[NewsStrategy] NEWS_CLAUDE_SCORING_ENABLED=False — keyword scoring for all {len(candidates)} candidates")
                for item in candidates:
                    item["result"] = _keyword_result(item["headline"], "Keyword scoring (Claude disabled).")
            else:
                limit_logged = False
                for batch_start in range(0, len(candidates), _CLAUDE_BATCH_SIZE):
                    batch = candidates[batch_start : batch_start + _CLAUDE_BATCH_SIZE]

                    if self._over_daily_limit():
                        if not limit_logged:
                            print(
                                f"[NewsStrategy] Daily Claude API limit ({Config.CLAUDE_DAILY_CALL_LIMIT}) "
                                f"reached — keyword fallback for remaining headlines"
                            )
                            limit_logged = True
                        for item in batch:
                            item["result"] = _keyword_result(
                                item["headline"],
                                f"Daily Claude API limit ({Config.CLAUDE_DAILY_CALL_LIMIT}) reached — keyword fallback.",
                            )
                    else:
                        self._claude_calls_today += 1
                        print(
                            f"[NewsStrategy] Claude batch call #{self._claude_calls_today} "
                            f"({len(batch)} headlines, limit={Config.CLAUDE_DAILY_CALL_LIMIT})"
                        )
                        results = await self._score_batch_with_claude(batch)
                        for item, result in zip(batch, results):
                            item["result"] = result

            # ── Pass 4: compute strength and build signals ────────────────────
            for item in candidates:
                result = item["result"]
                if result is None:
                    continue

                ticker     = item["ticker"]
                headline   = item["headline"]
                source     = item["source"]
                raw_score  = result.get("score", 5)
                confidence = result.get("confidence", 5)
                sentiment  = result.get("sentiment", "neutral")
                action     = result.get("action", "hold")
                reasoning  = result.get("reasoning", "")

                multiplier = _source_multiplier(source)
                print(f"[NewsStrategy] {ticker}: source='{source or 'unknown'}' → multiplier={multiplier:.1f}x (raw={raw_score} confidence={confidence})")
                strength = (raw_score * confidence / 10.0) * multiplier
                strength = round(min(strength, 20.0), 2)

                if strength >= 5.0:
                    print(
                        f"[NewsStrength] {ticker}: raw={raw_score} conf={confidence} "
                        f"src_mult={multiplier:.1f}x -> strength={strength:.2f} "
                        f"(alert>={Config.NEWS_SIGNAL_ALERT_THRESHOLD} "
                        f"trade>={Config.NEWS_SIGNAL_AUTO_TRADE_THRESHOLD}) "
                        f"headline={headline[:80]!r}"
                    )

                if strength >= Config.NEWS_SIGNAL_ALERT_THRESHOLD:
                    self._mark_seen(ticker)
                    signals.append({
                        "ticker":     ticker,
                        "headline":   headline,
                        "source":     source,
                        "sentiment":  sentiment,
                        "score":      raw_score,
                        "confidence": confidence,
                        "strength":   strength,
                        "action":     action,
                        "reasoning":  reasoning,
                        "auto_trade": strength >= Config.NEWS_SIGNAL_AUTO_TRADE_THRESHOLD,
                    })

            # ── Pass 5: aggregate per-ticker scores → sentiment_scores table ─────
            if self._db_engine and candidates:
                raw_by_ticker: dict[str, dict] = {}
                for item in candidates:
                    result = item.get("result") or {}
                    ticker = item["ticker"]
                    raw_score = int(result.get("score", 0))
                    direction = result.get("sentiment", "neutral")
                    if ticker not in raw_by_ticker:
                        raw_by_ticker[ticker] = {"scores": [], "directions": [], "count": 0}
                    raw_by_ticker[ticker]["scores"].append(raw_score)
                    raw_by_ticker[ticker]["directions"].append(direction)
                    raw_by_ticker[ticker]["count"] += 1

                agg_by_ticker: dict[str, dict] = {}
                for ticker, data in raw_by_ticker.items():
                    avg_score = round(sum(data["scores"]) / len(data["scores"]))
                    bull = data["directions"].count("bullish")
                    bear = data["directions"].count("bearish")
                    agg_direction = "bullish" if bull > bear else ("bearish" if bear > bull else "neutral")
                    agg_by_ticker[ticker] = {
                        "direction":     agg_direction,
                        "score":         avg_score,
                        "headline_count": data["count"],
                    }

                await asyncio.to_thread(self._write_sentiment_scores_sync, agg_by_ticker)

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
