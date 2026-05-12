"""
Webull contrarian retail-crowding strategy.

Polls Webull's public top-active and top-gainer lists every 15 minutes
during market hours. Generates a contrarian BEARISH alert when an S&P 500
stock appears in the top-10 with a single-day gain ≥ WEBULL_ALERT_THRESHOLD%.

Rationale: stocks dominating retail activity on Webull often exhibit mean-
reversion after the crowd piles in. Signal is alert-only — no auto-trading.

Self-disables after two consecutive 403 / connection failures (endpoint
may have moved or been blocked).

Requires: no API key — uses Webull's unauthenticated public market data API.
"""
import asyncio
import requests

from config import Config
from data.sp500_tickers import SP500_TICKERS
from strategies.base_strategy import BaseStrategy

_ACTIVE_URL = (
    "https://quotes-gw.webullfintech.com/api/bgw/market/topactive"
    "?exchangeId=11&rankType=1&pageIndex=1&pageSize=50&regionId=6"
)
_GAINER_URL = (
    "https://quotes-gw.webullfintech.com/api/bgw/market/topgainer"
    "?exchangeId=11&rankType=1&pageIndex=1&pageSize=50&regionId=6"
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "did": "trading-bot-monitor",
}


class WebullStrategy(BaseStrategy):
    """
    Contrarian retail-crowding signal from Webull top-active/top-gainer lists.
    Alert-only — never auto-trades.
    """

    def __init__(self, name: str = "Webull Contrarian"):
        super().__init__(name)
        self._consecutive_failures = 0
        self.disabled = False

    def _fetch_list(self, url: str) -> list[dict]:
        """Synchronous fetch of one Webull endpoint. Returns [] on any failure."""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            if resp.status_code == 403:
                self._consecutive_failures += 1
                print(f"[WebullStrategy] 403 FORBIDDEN (failure #{self._consecutive_failures})")
                return []
            resp.raise_for_status()
            self._consecutive_failures = 0
            data = resp.json()
            items = data.get("data", {})
            if isinstance(items, dict):
                return items.get("items", [])
            if isinstance(items, list):
                return items
            return []
        except Exception as e:
            self._consecutive_failures += 1
            print(f"[WebullStrategy] Fetch error: {e} (failure #{self._consecutive_failures})")
            return []

    async def scan_once(self) -> list[dict]:
        if self.disabled:
            return []

        active_items, gainer_items = await asyncio.gather(
            asyncio.to_thread(self._fetch_list, _ACTIVE_URL),
            asyncio.to_thread(self._fetch_list, _GAINER_URL),
        )

        if self._consecutive_failures >= 2:
            print("[WebullStrategy] 2+ consecutive failures — disabling loop permanently.")
            self.disabled = True
            return []

        # Merge: top-active items take precedence over top-gainer for rank
        seen: dict[str, dict] = {}
        for rank, item in enumerate(active_items[:10], start=1):
            ticker = item.get("disSymbol") or item.get("symbol") or ""
            if ticker:
                seen[ticker] = {"rank": rank, "list": "active", "item": item}
        for rank, item in enumerate(gainer_items[:10], start=1):
            ticker = item.get("disSymbol") or item.get("symbol") or ""
            if ticker and ticker not in seen:
                seen[ticker] = {"rank": rank, "list": "gainer", "item": item}

        signals = []
        threshold = Config.WEBULL_ALERT_THRESHOLD
        for ticker, meta in seen.items():
            if ticker not in SP500_TICKERS:
                continue
            item = meta["item"]
            try:
                # API returns decimal ratio (0.05 = 5%) for changeRatio
                raw = item.get("changeRatio") or item.get("pctChange") or 0
                change_pct = float(raw) * 100
            except (TypeError, ValueError):
                continue
            if change_pct < threshold:
                continue

            rank = meta["rank"]
            src = meta["list"]
            price = item.get("close") or item.get("price") or "N/A"
            volume = item.get("volume") or "N/A"
            score = round(min(change_pct / 2.0, 10.0), 1)  # 10% gain → 5.0, 20% → cap at 10.0

            reasoning = (
                f"{ticker} is #{rank} on Webull {src} list with +{change_pct:.1f}% intraday gain "
                f"(price ${price}, volume {volume}). Retail crowding detected — "
                f"contrarian mean-reversion likely after spike."
            )
            print(
                f"[WebullStrategy] CONTRARIAN ALERT: {ticker} #{rank} on {src} "
                f"+{change_pct:.1f}% → bearish contrarian score={score}"
            )
            signals.append({
                "ticker":     ticker,
                "rank":       rank,
                "change_pct": round(change_pct, 2),
                "score":      score,
                "reasoning":  reasoning,
                "auto_trade": False,
            })

        return signals

    def generate_signals(self, data, *args, **kwargs):
        return None

    def execute_trade(self, signal, trading_client, risk_percent, stop_loss_percent,
                      take_profit_percent, max_buying_power_utilization_percent):
        pass
