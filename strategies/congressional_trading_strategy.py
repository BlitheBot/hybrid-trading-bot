import asyncio
import json
import urllib.request
from datetime import datetime, timedelta
import pytz

from config import Config
from data.sp500_tickers import SP500_TICKERS
from strategies.base_strategy import BaseStrategy

_API_URL = "https://api.quiverquant.com/beta/live/congresstrading"
_SP500_SET = set(SP500_TICKERS)

# Static committee membership for the 119th Congress (2025-2026).
# Covers Senate Finance, Senate Banking, House Financial Services,
# Senate Armed Services, and House Armed Services committees.
# This list drifts as members rotate — update each new Congress.
_COMMITTEE_MEMBERS: frozenset[str] = frozenset({
    # Senate Finance
    "Mike Crapo", "John Cornyn", "John Thune", "Tim Scott", "Bill Cassidy",
    "James Lankford", "Steve Daines", "Todd Young", "Marsha Blackburn", "Thom Tillis",
    "Ron Wyden", "Maria Cantwell", "Michael Bennet", "Mark Warner", "Sheldon Whitehouse",
    "Maggie Hassan", "Catherine Cortez Masto",
    # Senate Banking
    "John Kennedy", "Bill Hagerty", "Cynthia Lummis", "Kevin Cramer", "Katie Britt",
    "Bernie Moreno", "Jim Banks",
    "Elizabeth Warren", "Jack Reed", "Chris Van Hollen", "Tina Smith",
    "John Fetterman", "Andy Kim",
    # House Financial Services
    "French Hill", "Bill Huizenga", "Andy Barr", "Roger Williams", "Ann Wagner",
    "Pete Sessions", "Barry Loudermilk", "Warren Davidson", "Tom Emmer",
    "Blaine Luetkemeyer", "John Rose", "Bryan Steil", "William Timmons", "Mike Flood",
    "Maxine Waters", "Brad Sherman", "Gregory Meeks", "David Scott",
    "Stephen Lynch", "Joyce Beatty", "Jim Himes",
    # Senate Armed Services
    "Roger Wicker", "Deb Fischer", "Tom Cotton", "Mike Rounds", "Joni Ernst",
    "Dan Sullivan", "Rick Scott", "Tommy Tuberville", "Eric Schmitt", "Tim Sheehy",
    "Mazie Hirono", "Gary Peters", "Jeanne Shaheen", "Kirsten Gillibrand",
    "Richard Blumenthal", "Tammy Duckworth",
    # House Armed Services
    "Mike Rogers", "Rob Wittman", "Joe Wilson", "Austin Scott", "Doug Lamborn",
    "Trent Kelly", "Don Bacon", "Michael McCaul",
    "Adam Smith", "Rick Larsen", "John Garamendi", "Donald Norcross",
    "Seth Moulton", "Ruben Gallego", "Salud Carbajal", "Veronica Escobar",
})


def _parse_amount_lower_bound(range_str: str) -> float:
    """
    Parse lower bound from Quiver range strings.
    '$250,001 - $500,000' → 250001.0
    'Over $5,000,000'     → 5000000.0
    """
    if not range_str:
        return 0.0
    try:
        lower = range_str.split(" - ")[0]
        lower = (
            lower.replace("$", "")
                 .replace(",", "")
                 .replace("+", "")
                 .replace("Over ", "")
                 .replace("over ", "")
                 .strip()
        )
        return float(lower)
    except (ValueError, IndexError):
        return 0.0


def _score_buy(amount: float, is_committee: bool, days_old: int) -> tuple[float, float]:
    """
    Returns (base_score, strength).
    Multipliers: committee member × 1.3, transaction ≤ 7 days old × 1.2.
    Confidence fixed at 9 (factual, verified government disclosure).
    Max achievable strength: 8 × 1.3 × 1.2 × 9/10 ≈ 11.23 — alert-only in practice.
    """
    if amount > 250_000:
        base = 8.0
    elif amount > 50_000:
        base = 7.0
    else:
        base = 6.0

    adjusted = base
    if is_committee:
        adjusted *= 1.3
    if days_old <= 7:
        adjusted *= 1.2

    return base, round(adjusted * 9 / 10, 2)


class CongressionalTradingStrategy(BaseStrategy):
    """
    Polls Quiver Quantitative for congressional stock trades every 60 minutes.
    Generates buy signals for S&P 500 purchases and informational sell signals.
    Sells are never auto-traded. Buys auto-trade only at strength >= 13,
    which is mathematically unreachable with current scoring (max ~11.2) —
    all signals are effectively alert-only.

    Gracefully disables itself on 401/403 (no API key) with a single log line.
    """

    def __init__(self, name: str = "Congressional Trading"):
        super().__init__(name)
        self._seen_tx_ids: set[str] = set()
        self._ticker_cooldowns: dict[str, datetime] = {}
        self._disabled = False

    def _on_cooldown(self, ticker: str) -> bool:
        ts = self._ticker_cooldowns.get(ticker)
        return ts is not None and datetime.now(pytz.utc) - ts < timedelta(hours=4)

    def _mark_cooldown(self, ticker: str):
        self._ticker_cooldowns[ticker] = datetime.now(pytz.utc)

    def _fetch_trades(self) -> list[dict] | None:
        headers = {"User-Agent": "HybridTradingBot/1.0 contact@hybridtradingbot.com"}
        if Config.QUIVER_API_KEY:
            headers["Authorization"] = f"Token {Config.QUIVER_API_KEY}"
        req = urllib.request.Request(_API_URL, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(f"[Congress] API key missing or rejected (HTTP {e.code}) — loop disabled")
                self._disabled = True
                return None
            print(f"[Congress] HTTP {e.code}: {e}")
            return None
        except Exception as e:
            print(f"[Congress] Fetch failed: {e}")
            return None

    def _scan_sync(self) -> list[dict]:
        if self._disabled:
            return []

        raw = self._fetch_trades()
        if raw is None:
            return []

        today = datetime.now(pytz.utc).date()
        signals: list[dict] = []

        for item in raw:
            ticker = (item.get("Ticker") or "").strip().upper()
            if not ticker or ticker not in _SP500_SET:
                continue

            transaction = (item.get("Transaction") or "").strip()
            tx_lower = transaction.lower()
            is_buy  = "purchase" in tx_lower
            is_sell = "sale" in tx_lower or "sell" in tx_lower
            if not is_buy and not is_sell:
                continue

            rep          = (item.get("Representative") or "").strip()
            party        = (item.get("Party") or "").strip()
            chamber      = (item.get("Chamber") or "").strip()
            amount_range = (item.get("Range") or "").strip()
            date_str     = (item.get("Date") or "").strip()

            tx_id = f"{ticker}-{rep}-{date_str}-{amount_range}-{transaction}"
            if tx_id in self._seen_tx_ids:
                continue
            self._seen_tx_ids.add(tx_id)

            if self._on_cooldown(ticker):
                continue

            try:
                tx_date  = datetime.strptime(date_str, "%Y-%m-%d").date()
                days_old = (today - tx_date).days
            except ValueError:
                days_old = 999

            amount       = _parse_amount_lower_bound(amount_range)
            is_committee = rep in _COMMITTEE_MEMBERS

            if is_buy:
                base_score, strength = _score_buy(amount, is_committee, days_old)
                self._mark_cooldown(ticker)
                signals.append({
                    "ticker":         ticker,
                    "representative": rep,
                    "party":          party,
                    "chamber":        chamber,
                    "amount_range":   amount_range,
                    "transaction":    "Purchase",
                    "date":           date_str,
                    "headline": (
                        f"Congressional BUY: {rep} ({party}) — "
                        f"{ticker} {amount_range}"
                        + (" [Committee]" if is_committee else "")
                    ),
                    "source":        "Quiver Quantitative",
                    "sentiment":     "bullish",
                    "score":         base_score,
                    "strength":      strength,
                    "action":        "buy",
                    "reasoning": (
                        f"{rep} purchased {ticker} ({amount_range})"
                        + (f", {days_old}d ago" if days_old < 999 else "")
                        + (" [committee member]" if is_committee else "")
                    ),
                    "auto_trade":    strength >= Config.CONGRESSIONAL_AUTO_TRADE_THRESHOLD,
                    "informational": False,
                })

            elif is_sell:
                # Score at half buy value; informational only, never auto-trade.
                base_score, buy_strength = _score_buy(amount, is_committee, days_old)
                sell_strength = round(buy_strength / 2, 2)
                if not self._on_cooldown(ticker):
                    self._mark_cooldown(ticker)
                signals.append({
                    "ticker":         ticker,
                    "representative": rep,
                    "party":          party,
                    "chamber":        chamber,
                    "amount_range":   amount_range,
                    "transaction":    transaction,
                    "date":           date_str,
                    "headline": (
                        f"Congressional SELL — informational only: "
                        f"{rep} ({party}) — {ticker} {amount_range}"
                    ),
                    "source":        "Quiver Quantitative",
                    "sentiment":     "bearish",
                    "score":         round(base_score / 2, 2),
                    "strength":      sell_strength,
                    "action":        "sell",
                    "reasoning": (
                        f"{rep} sold {ticker} ({amount_range})"
                        + (f", {days_old}d ago" if days_old < 999 else "")
                        + (" [committee member]" if is_committee else "")
                    ),
                    "auto_trade":    False,
                    "informational": True,
                })

        return signals

    async def scan_once(self) -> list[dict]:
        return await asyncio.to_thread(self._scan_sync)

    # ── BaseStrategy stubs ────────────────────────────────────────────────────

    def generate_signals(self, data, *args, **kwargs):
        return None

    def execute_trade(self, signal, trading_client, risk_percent, stop_loss_percent,
                      take_profit_percent, max_buying_power_utilization_percent):
        pass
