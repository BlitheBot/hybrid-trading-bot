import re
import time
from datetime import datetime, timedelta

import pytz
import requests as _requests

from config import Config

try:
    from data.sp500_tickers import SP500_TICKERS
    _SP500_SET = set(SP500_TICKERS)
except Exception:
    _SP500_SET = set()

# Common English words that are valid uppercase but not tickers
_WORD_BLOCKLIST = {
    "IT", "ALL", "ARE", "NOW", "FOR", "THE", "AND", "BUT", "NOT",
    "YOU", "WAS", "HAS", "HIS", "HER", "ITS", "OUR", "NEW", "OLD",
    "CAN", "MAY", "HOW", "WHO", "WHY", "GET", "SET", "USE", "RUN",
    "BUY", "CEO", "CFO", "IPO", "FDA", "SEC", "FED", "GDP", "CPI",
    "USA", "US",  "UK",  "EU",  "AI",  "OR",  "AT",  "IN",  "ON",
    "IS",  "BE",  "DO",  "AN",  "UP",  "BY",  "TO",  "OF",  "IF",
    "AS",  "SO",  "NO",  "GO",  "OWN", "TAX", "EPS", "ROI", "ATH",
    "DD",  "OP",  "ER",  "EV",  "IV",  "DCA", "PT",  "EOD", "IMO",
    "YOLO", "TBH", "TBF", "IMHO", "IIRC",
}

_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')

_SUBREDDITS = ["wallstreetbets", "stocks"]
_USER_AGENT = "hybrid-trading-bot/1.0 (automated research; contact: trading-bot)"


class RedditStrategy:
    """
    Scans r/wallstreetbets and r/stocks hot posts for S&P 500 ticker mentions.
    Returns alert-only signals when ≥ REDDIT_MIN_MENTIONS posts mention a ticker
    and the combined score exceeds REDDIT_ALERT_THRESHOLD.
    """

    name = "Reddit Momentum"

    def __init__(self):
        self._seen_post_ids: dict[str, float] = {}  # id → first_seen epoch
        self._last_dedup_clear = time.time()

    def _clear_stale_ids(self):
        now = time.time()
        cutoff = now - 4 * 3600  # 4-hour dedup window
        self._seen_post_ids = {k: v for k, v in self._seen_post_ids.items() if v > cutoff}
        self._last_dedup_clear = now

    def _fetch_hot(self, subreddit: str, limit: int = 25) -> list[dict]:
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
        try:
            resp = _requests.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=10,
            )
            if resp.status_code == 429:
                return []
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("children", [])
        except Exception as e:
            print(f"[Reddit] Fetch failed for r/{subreddit}: {e}")
            return []

    def _extract_tickers(self, text: str) -> set[str]:
        candidates = set(_TICKER_RE.findall(text))
        return candidates - _WORD_BLOCKLIST

    async def scan_once(self) -> list[dict]:
        import asyncio

        # Clear stale dedup IDs every 4 hours
        if time.time() - self._last_dedup_clear > 3600:
            self._clear_stale_ids()

        # Fetch both subreddits in thread pool to avoid blocking
        posts_by_sub: dict[str, list] = {}
        for sub in _SUBREDDITS:
            posts = await asyncio.to_thread(self._fetch_hot, sub)
            posts_by_sub[sub] = posts

        # Aggregate: ticker → {score, mention_count, subreddits, titles}
        ticker_data: dict[str, dict] = {}
        now_epoch = time.time()

        for sub, posts in posts_by_sub.items():
            for child in posts:
                post = child.get("data", {})
                post_id = post.get("id", "")
                if not post_id or post_id in self._seen_post_ids:
                    continue
                self._seen_post_ids[post_id] = now_epoch

                title = post.get("title", "")
                selftext = post.get("selftext", "")[:500]
                post_score = float(post.get("score", 0))

                tickers = self._extract_tickers(title + " " + selftext)
                sp500_tickers = tickers & _SP500_SET if _SP500_SET else tickers

                for ticker in sp500_tickers:
                    if ticker not in ticker_data:
                        ticker_data[ticker] = {
                            "score": 0.0,
                            "mention_count": 0,
                            "subreddits": set(),
                            "sample_titles": [],
                        }
                    entry = ticker_data[ticker]
                    entry["score"] += post_score
                    entry["mention_count"] += 1
                    entry["subreddits"].add(sub)
                    if len(entry["sample_titles"]) < 3:
                        entry["sample_titles"].append(title)

        signals = []
        for ticker, data in ticker_data.items():
            if (data["mention_count"] >= Config.REDDIT_MIN_MENTIONS
                    and data["score"] >= Config.REDDIT_ALERT_THRESHOLD):
                signals.append({
                    "ticker": ticker,
                    "score": round(data["score"], 1),
                    "mention_count": data["mention_count"],
                    "subreddits": sorted(data["subreddits"]),
                    "sample_titles": data["sample_titles"],
                    "auto_trade": data["score"] >= Config.REDDIT_AUTO_TRADE_THRESHOLD,
                })

        signals.sort(key=lambda x: x["score"], reverse=True)
        print(f"[Reddit] Scanned {sum(len(v) for v in posts_by_sub.values())} posts — {len(signals)} ticker signals above threshold")
        return signals
