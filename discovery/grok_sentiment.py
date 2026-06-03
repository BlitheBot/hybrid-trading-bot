"""
Grok X/Twitter stock sentiment scorer.

Every 30 minutes, scores the top 50 S&P 500 tickers (rank 1-50 from
active_tickers) using the xAI Grok API (grok-3-mini-fast). Scores are
written to the grok_sentiment table and read by the head bot's conviction
scoring gate in _process_symbol — same weighting as news sentiment.

Requires XAI_API_KEY in env (console.x.ai).
"""

import json
import time
from datetime import datetime, timedelta

import requests
from sqlalchemy import text as sql_text

from config import Config

_XAI_URL   = "https://api.x.ai/v1/responses"
_XAI_MODEL = "grok-3-mini-fast"
_BATCH_SIZE    = 10   # tickers per Grok API call
_TOP_N         = 50   # only rank 1-50 from active_tickers
_STALE_MINUTES = 35   # matches active_tickers and sentiment_scores TTL


def _ensure_table(db_engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS grok_sentiment (
                ticker       VARCHAR(10) PRIMARY KEY,
                direction    VARCHAR(10),
                score        INT,
                last_updated TIMESTAMPTZ DEFAULT NOW()
            )
        """))


def _get_top_tickers(db_engine) -> list[str]:
    """Return top 50 tickers from active_tickers updated within the last 35 minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=_STALE_MINUTES)
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT ticker FROM active_tickers
                WHERE last_updated > :cutoff
                ORDER BY rank ASC
                LIMIT :n
            """), {"cutoff": cutoff, "n": _TOP_N}).mappings().fetchall()
        return [r["ticker"] for r in rows]
    except Exception as e:
        print(f"[GrokSentiment] _get_top_tickers error: {e}")
        return []


def _call_grok_batch(tickers: list[str]) -> list[dict]:
    """
    Score a batch of tickers via the xAI API.
    Returns a list of {ticker, direction, score} dicts.
    Sync — called inside refresh_grok_sentiment which is called via asyncio.to_thread.
    """
    if not Config.XAI_API_KEY:
        return []

    ticker_csv = ", ".join(tickers)
    prompt = (
        f"You are a financial analyst with comprehensive knowledge of X/Twitter (formerly Twitter) "
        f"discussion and sentiment around US equities. Assess the current X/Twitter sentiment for "
        f"each of these stock tickers: {ticker_csv}.\n\n"
        f"For each ticker, determine whether the overall X/Twitter sentiment is bullish, bearish, "
        f"or neutral, and provide a score from 0 to 10 where 0 = extremely bearish, 5 = neutral, "
        f"10 = extremely bullish.\n\n"
        f"Respond with JSON only — no other text:\n"
        f'{{"tickers": [{{"ticker": "AAPL", "direction": "bullish", "score": 7}}, ...]}}'
    )

    try:
        resp = requests.post(
            _XAI_URL,
            headers={
                "Authorization": f"Bearer {Config.XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": _XAI_MODEL,
                "input": [{"role": "user", "content": prompt}],
                "max_output_tokens": 900,
                "temperature": 0.1,
            },
            timeout=30,
        )
        if resp.status_code == 401:
            print("[GrokSentiment] 401 UNAUTHORIZED — check XAI_API_KEY")
            return []
        if resp.status_code == 429:
            print("[GrokSentiment] 429 RATE LIMITED — skipping batch")
            return []
        resp.raise_for_status()

        data = resp.json()
        msg_item = next((o for o in data.get("output", []) if o.get("type") == "message"), None)
        if not msg_item:
            print("[GrokSentiment] No message item in response output")
            return []
        raw = msg_item["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        return parsed.get("tickers", [])

    except json.JSONDecodeError as e:
        print(f"[GrokSentiment] JSON parse error: {e}")
        return []
    except Exception as e:
        print(f"[GrokSentiment] batch call failed: {e}")
        return []


def _write_scores(db_engine, results: list[dict]) -> None:
    """Upsert scored ticker records to grok_sentiment. Sync."""
    if not results:
        return
    try:
        _ensure_table(db_engine)
        with db_engine.begin() as conn:
            for r in results:
                ticker    = str(r.get("ticker", "")).upper().strip()
                direction = str(r.get("direction", "neutral")).lower().strip()
                score     = int(r.get("score", 5))
                if not ticker or direction not in ("bullish", "bearish", "neutral"):
                    continue
                conn.execute(sql_text("""
                    INSERT INTO grok_sentiment (ticker, direction, score, last_updated)
                    VALUES (:ticker, :direction, :score, NOW())
                    ON CONFLICT (ticker) DO UPDATE SET
                        direction    = EXCLUDED.direction,
                        score        = EXCLUDED.score,
                        last_updated = EXCLUDED.last_updated
                """), {"ticker": ticker, "direction": direction, "score": score})
    except Exception as e:
        print(f"[GrokSentiment] _write_scores error: {e}")


def refresh_grok_sentiment(db_engine) -> int:
    """
    Full refresh: fetch top 50 tickers, score in batches of 10, write to DB.
    Sync — call via asyncio.to_thread from grok_sentiment_loop.
    Returns count of tickers successfully scored.
    """
    tickers = _get_top_tickers(db_engine)
    if not tickers:
        print("[GrokSentiment] No active tickers available — skipping refresh")
        return 0

    all_results: list[dict] = []
    total_batches = (len(tickers) + _BATCH_SIZE - 1) // _BATCH_SIZE
    for i in range(0, len(tickers), _BATCH_SIZE):
        batch = tickers[i : i + _BATCH_SIZE]
        batch_num = i // _BATCH_SIZE + 1
        results = _call_grok_batch(batch)
        all_results.extend(results)
        print(
            f"[GrokSentiment] Batch {batch_num}/{total_batches}: "
            f"{len(batch)} tickers → {len(results)} scored"
        )
        if i + _BATCH_SIZE < len(tickers):
            time.sleep(0.5)  # brief pause to avoid rate limits between batches

    _write_scores(db_engine, all_results)
    print(f"[GrokSentiment] Refresh complete — {len(all_results)} tickers updated")
    return len(all_results)


def get_grok_sentiment(db_engine, ticker: str) -> dict | None:
    """
    Return fresh grok_sentiment record for ticker if updated within 35 minutes.
    Sync — call via asyncio.to_thread. Returns None when absent or stale.
    """
    if db_engine is None:
        return None
    cutoff = datetime.utcnow() - timedelta(minutes=_STALE_MINUTES)
    try:
        with db_engine.connect() as conn:
            row = conn.execute(sql_text("""
                SELECT ticker, direction, score
                FROM grok_sentiment
                WHERE ticker = :ticker AND last_updated > :cutoff
            """), {"ticker": ticker, "cutoff": cutoff}).mappings().fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[GrokSentiment] get error for {ticker}: {e}")
        return None
