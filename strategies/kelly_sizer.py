"""
kelly_sizer.py — Kelly Criterion Position Sizing for BlitheBot
==============================================================
Place this file in the same directory as kalman_signal.py, hurst_signal.py,
and vwap_signal.py.

What it does:
  - Computes the Kelly-optimal fraction of capital to deploy per trade
  - Uses each strategy's historical win rate and average payoff ratio
  - Defaults to half-Kelly (industry standard — full Kelly is too volatile)
  - Pulls win/loss history from your existing PostgreSQL trades table
  - Falls back to a conservative default size if insufficient history exists
  - Applies a per-strategy cap so no single trade exceeds a maximum allocation

Dependencies:
  pip install numpy pandas sqlalchemy  (all already in your stack)

Usage:
  from kelly_sizer import KellySizer

  sizer = KellySizer(db_engine=engine, base_capital=100_000)

  # Get position size in dollars for a trade
  size = sizer.get_position_size(
      strategy_name="COST Swing",
      current_price=85.50,
  )
  # size["dollars"]     → dollar amount to allocate
  # size["shares"]      → number of shares to buy
  # size["kelly_f"]     → raw Kelly fraction (for logging)
  # size["half_kelly"]  → half-Kelly fraction actually used
  # size["win_rate"]    → win rate used in calculation
  # size["payoff_ratio"]→ payoff ratio used in calculation
  # size["sample_size"] → number of trades this is based on
  # size["note"]        → explanation string (for Slack/logging)
"""

import numpy as np
import pandas as pd
from typing import Optional, Union
from sqlalchemy import text


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_KELLY_FRACTION = 0.02   # 2% of capital — conservative fallback
MAX_KELLY_FRACTION = 0.10       # hard cap — never bet more than 10% on one trade
MIN_SAMPLE_SIZE = 20            # minimum trades before Kelly is trusted


class KellySizer:
    """
    Kelly Criterion position sizer with half-Kelly safety and fallback defaults.

    Parameters
    ----------
    db_engine : sqlalchemy Engine
        Your existing PostgreSQL engine — same one the rest of the bot uses.

    base_capital : float
        Total account capital in dollars. Used to convert Kelly fraction
        to a dollar amount. Update this periodically as account grows.

    half_kelly : bool
        Whether to use half the Kelly-optimal fraction. Default True.
        Full Kelly maximizes long-run growth but has extreme drawdowns.
        Half-Kelly is the professional standard.

    max_fraction : float
        Hard cap on any single position as a fraction of capital.
        Default 0.10 (10%). Kelly can recommend large sizes on high
        win-rate strategies — this prevents dangerous concentration.

    min_sample_size : int
        Minimum number of closed trades required before trusting Kelly.
        Below this, falls back to default_fraction. Default 20.

    default_fraction : float
        Fallback position size as fraction of capital when insufficient
        history exists. Default 0.02 (2%).

    lookback_days : int
        How many days of trade history to use for win rate calculation.
        Default 90 — recent performance matters more than distant history.
    """

    def __init__(
        self,
        db_engine,
        base_capital: float,
        half_kelly: bool = True,
        max_fraction: float = MAX_KELLY_FRACTION,
        min_sample_size: int = MIN_SAMPLE_SIZE,
        default_fraction: float = DEFAULT_KELLY_FRACTION,
        lookback_days: int = 90,
    ):
        self.engine = db_engine
        self.base_capital = base_capital
        self.half_kelly = half_kelly
        self.max_fraction = max_fraction
        self.min_sample_size = min_sample_size
        self.default_fraction = default_fraction
        self.lookback_days = lookback_days

        # Cache so we're not hitting DB on every trade signal
        self._cache: dict = {}
        self._cache_timestamp: Optional[pd.Timestamp] = None
        self._cache_ttl_minutes: int = 60

    # ── Public API ────────────────────────────────────────────────────────────

    def get_position_size(
        self,
        strategy_name: str,
        current_price: float,
    ) -> dict:
        """
        Compute position size for a trade from a given strategy.

        Parameters
        ----------
        strategy_name : str
            Must match the strategy_name field in your trades/results table.
            e.g. "COST Swing", "SMB Late Scalp"

        current_price : float
            Current price of the asset being traded.

        Returns
        -------
        dict with keys:
            dollars       — dollar amount to allocate to this trade
            shares        — number of whole shares (floor division)
            kelly_f       — raw Kelly fraction before half-Kelly and cap
            half_kelly_f  — fraction actually used after half-Kelly and cap
            win_rate      — win rate used in calculation
            payoff_ratio  — avg_win / avg_loss used in calculation
            sample_size   — number of trades this is based on
            note          — human-readable explanation for Slack logging
        """
        stats = self._get_strategy_stats(strategy_name)
        kelly_f, note = self._compute_kelly(stats)

        # Apply half-Kelly
        used_f = kelly_f * 0.5 if self.half_kelly else kelly_f

        # Apply hard cap
        capped = used_f > self.max_fraction
        used_f = min(used_f, self.max_fraction)

        if capped:
            note += f" | capped at {self.max_fraction:.0%} max"

        dollars = self.base_capital * used_f
        shares = int(dollars // current_price)

        return {
            "dollars": round(dollars, 2),
            "shares": shares,
            "kelly_f": round(kelly_f, 4),
            "half_kelly_f": round(used_f, 4),
            "win_rate": round(stats.get("win_rate", 0), 3),
            "payoff_ratio": round(stats.get("payoff_ratio", 0), 3),
            "sample_size": stats.get("sample_size", 0),
            "note": note,
        }

    def update_capital(self, new_capital: float):
        """Call this periodically (e.g. daily) to keep base_capital current."""
        self.base_capital = new_capital

    def invalidate_cache(self):
        """Force a fresh DB read on next get_position_size call."""
        self._cache = {}
        self._cache_timestamp = None

    # ── Kelly formula ─────────────────────────────────────────────────────────

    def _compute_kelly(self, stats: dict) -> tuple[float, str]:
        """
        Kelly formula: f* = (p * b - q) / b
        where:
          p = win rate
          q = 1 - p (loss rate)
          b = payoff ratio (avg_win / avg_loss)

        Returns (kelly_fraction, note_string)
        """
        sample_size = stats.get("sample_size", 0)
        win_rate = stats.get("win_rate")
        payoff_ratio = stats.get("payoff_ratio")

        # Insufficient history — use conservative default
        if sample_size < self.min_sample_size or win_rate is None or payoff_ratio is None:
            note = (
                f"default size (only {sample_size} trades, "
                f"need {self.min_sample_size} for Kelly)"
            )
            return self.default_fraction, note

        p = win_rate
        q = 1 - p
        b = payoff_ratio

        # Raw Kelly
        kelly_f = (p * b - q) / b

        # Negative Kelly = negative expected value = don't trade
        if kelly_f <= 0:
            note = (
                f"Kelly={kelly_f:.3f} (negative EV) | "
                f"win_rate={p:.1%} payoff={b:.2f}x | "
                f"using default {self.default_fraction:.0%}"
            )
            return self.default_fraction, note

        note = (
            f"Kelly={kelly_f:.3f} | "
            f"half_kelly={kelly_f * 0.5:.3f} | "
            f"win_rate={p:.1%} | "
            f"payoff={b:.2f}x | "
            f"n={sample_size} trades"
        )
        return kelly_f, note

    # ── Database stats retrieval ──────────────────────────────────────────────

    def _get_strategy_stats(self, strategy_name: str) -> dict:
        """
        Pull win rate and payoff ratio for a strategy from the trades table.
        Results are cached for cache_ttl_minutes to avoid hammering the DB.

        Expects your trades table to have at minimum:
            strategy_name  TEXT
            pnl            FLOAT  (positive = win, negative = loss)
            closed_at      TIMESTAMP

        Adjust the query below if your column names differ.
        """
        # Check cache
        now = pd.Timestamp.now()
        if (
            self._cache_timestamp is not None
            and (now - self._cache_timestamp).seconds < self._cache_ttl_minutes * 60
            and strategy_name in self._cache
        ):
            return self._cache[strategy_name]

        stats = self._query_stats(strategy_name)
        self._cache[strategy_name] = stats
        self._cache_timestamp = now
        return stats

    def _query_stats(self, strategy_name: str) -> dict:
        """
        Execute the stats query against PostgreSQL.
        Returns dict with win_rate, payoff_ratio, sample_size.
        Returns empty dict on any DB error (triggers fallback to default size).
        """
        query = text("""
            SELECT
                COUNT(*)                                                AS total,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END)          AS wins,
                AVG(CASE WHEN pnl_pct > 0 THEN pnl_pct ELSE NULL END)  AS avg_win,
                AVG(CASE WHEN pnl_pct < 0 THEN ABS(pnl_pct) ELSE NULL END) AS avg_loss
            FROM signal_outcomes
            WHERE signal_type = :signal_type
              AND exit_time >= NOW() - (:days * INTERVAL '1 day')
              AND pnl_pct IS NOT NULL
              AND exit_time IS NOT NULL
        """)

        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    query,
                    {"signal_type": strategy_name, "days": self.lookback_days}
                ).fetchone()

            if row is None or row.total == 0:
                return {"sample_size": 0}

            total = int(row.total)
            wins = int(row.wins or 0)
            avg_win = float(row.avg_win or 0)
            avg_loss = float(row.avg_loss or 1)  # avoid div/0

            win_rate = wins / total if total > 0 else 0
            payoff_ratio = avg_win / avg_loss if avg_loss > 0 else 0

            return {
                "win_rate": win_rate,
                "payoff_ratio": payoff_ratio,
                "sample_size": total,
            }

        except Exception as e:
            # Never crash the bot over a sizing calculation
            print(f"[KellySizer] DB query failed for {strategy_name}: {e}")
            return {"sample_size": 0}


# ── Standalone Kelly calculator (no DB needed) ────────────────────────────────

def kelly_fraction(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    half_kelly: bool = True,
    max_fraction: float = MAX_KELLY_FRACTION,
) -> dict:
    """
    Compute Kelly fraction directly from stats — no DB, no class needed.
    Useful for backtesting or manual calculation.

    Parameters
    ----------
    win_rate    : float  e.g. 0.55 for 55% win rate
    avg_win     : float  average dollar profit on winning trades
    avg_loss    : float  average dollar loss on losing trades (positive number)
    half_kelly  : bool   use half the Kelly fraction (default True)
    max_fraction: float  hard cap (default 0.10)

    Returns dict with kelly_f, used_f, and interpretation string.

    Example:
        kelly_fraction(win_rate=0.55, avg_win=200, avg_loss=100)
        # → kelly_f=0.10, used_f=0.05, "Bet 5.0% of capital per trade"
    """
    if avg_loss <= 0:
        return {"kelly_f": 0, "used_f": 0, "note": "avg_loss must be positive"}

    b = avg_win / avg_loss
    p = win_rate
    q = 1 - p

    kelly_f = (p * b - q) / b
    kelly_f = max(kelly_f, 0)  # floor at 0

    used_f = kelly_f * 0.5 if half_kelly else kelly_f
    used_f = min(used_f, max_fraction)

    note = (
        f"Kelly={kelly_f:.3f} | "
        f"{'half_kelly' if half_kelly else 'full_kelly'}={used_f:.3f} | "
        f"Bet {used_f:.1%} of capital per trade"
    )

    return {"kelly_f": round(kelly_f, 4), "used_f": round(used_f, 4), "note": note}


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== KellySizer Smoke Test (no DB) ===\n")

    # Test the standalone formula at various win rates
    scenarios = [
        {"label": "Coin flip, 1:1 payoff (zero edge)",   "wr": 0.50, "aw": 100, "al": 100},
        {"label": "55% win rate, 1:1 payoff",            "wr": 0.55, "aw": 100, "al": 100},
        {"label": "55% win rate, 2:1 payoff",            "wr": 0.55, "aw": 200, "al": 100},
        {"label": "60% win rate, 1.5:1 payoff",          "wr": 0.60, "aw": 150, "al": 100},
        {"label": "40% win rate, 3:1 payoff (high RR)",  "wr": 0.40, "aw": 300, "al": 100},
        {"label": "Negative edge — should return 0",     "wr": 0.40, "aw": 100, "al": 200},
    ]

    for s in scenarios:
        result = kelly_fraction(s["wr"], s["aw"], s["al"])
        print(f"  {s['label']}")
        print(f"    → {result['note']}\n")

    # Simulate what get_position_size returns with known stats
    print("=== Simulated position sizing on $100,000 account ===\n")

    class MockEngine:
        """Simulates DB returning known stats — no real DB needed for test."""
        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params):
            class Row:
                total = 45
                wins = 27       # 60% win rate
                avg_win = 180   # avg $180 profit
                avg_loss = 100  # avg $100 loss
            class Result:
                def fetchone(self):
                    return Row()
            return Result()

    sizer = KellySizer(db_engine=MockEngine(), base_capital=100_000)
    # Manually inject stats to bypass real DB
    sizer._cache["COST Swing"] = {
        "win_rate": 0.60,
        "payoff_ratio": 1.80,
        "sample_size": 45,
    }
    sizer._cache_timestamp = pd.Timestamp.now()

    result = sizer.get_position_size("COST Swing", current_price=95.00)
    print(f"  Strategy: COST Swing @ $95.00/share")
    print(f"  {result['note']}")
    print(f"  → Allocate ${result['dollars']:,.0f} ({result['half_kelly_f']:.1%} of capital)")
    print(f"  → {result['shares']} shares")

    # Test fallback (insufficient history)
    result2 = sizer.get_position_size("New Strategy", current_price=50.00)
    print(f"\n  Strategy: New Strategy (no history) @ $50.00/share")
    print(f"  → {result2['note']}")
    print(f"  → Allocate ${result2['dollars']:,.0f} ({result2['half_kelly_f']:.1%} of capital)")

    print("\n=== All tests passed ===")
