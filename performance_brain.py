"""
Enhanced Performance Brain (Task 7) — pure multiplier math.

The DB queries live in ``bot.TradingBot._get_performance_multiplier``; this module
holds the deterministic, unit-tested combination logic so the behavior can be
verified without a database.

Final multiplier = clamp(momentum_base + regime_bonus + time_bonus, 0.5, 1.5):

  * momentum_base — 1.2x (hot: 3+ wins of last 5), 0.7x (cold: 3+ losses of
    last 5), else 1.0x. Needs >= 3 recent closed signals to take a side.
  * regime_bonus  — +0.1x when the current regime is net-profitable for the
    strategy (>= min_n samples and average P&L > 0).
  * time_bonus    — +0.1x in the strategy's stronger session window, -0.1x in
    the weaker one, 0 otherwise / outside both windows.

Session windows (ET minutes since midnight): morning 9:30–11:30 = [570, 690],
afternoon 13:30–16:00 = [810, 960].
"""
from __future__ import annotations

MORNING = (570, 690)
AFTERNOON = (810, 960)

MULT_MIN = 0.5
MULT_MAX = 1.5
HOT = 1.2
COLD = 0.7
NEUTRAL = 1.0


def momentum_multiplier(recent_pnls: list[float], min_signals: int = 3) -> float:
    """1.2x hot / 0.7x cold / 1.0x neutral from the last few closed P&Ls."""
    if recent_pnls is None or len(recent_pnls) < min_signals:
        return NEUTRAL
    last5 = list(recent_pnls)[:5]
    wins = sum(1 for p in last5 if p > 0)
    losses = len(last5) - wins
    if wins >= 3:
        return HOT
    if losses >= 3:
        return COLD
    return NEUTRAL


def regime_bonus(avg_pnl: float | None, n: int, min_n: int = 5, bonus: float = 0.1) -> float:
    """+bonus when the current regime is net-profitable (enough samples)."""
    if avg_pnl is None or n < min_n:
        return 0.0
    return bonus if avg_pnl > 0 else 0.0


def _in_window(minute: int, window: tuple[int, int]) -> bool:
    return window[0] <= minute <= window[1]


def time_of_day_bonus(
    morning_avg: float | None, morning_n: int,
    afternoon_avg: float | None, afternoon_n: int,
    current_minute: int, min_n: int = 3, bonus: float = 0.1,
) -> float:
    """Weight the current session window by its historical relative strength."""
    if morning_n < min_n or afternoon_n < min_n:
        return 0.0
    m = morning_avg or 0.0
    a = afternoon_avg or 0.0
    if _in_window(current_minute, MORNING):
        return bonus if m > a else (-bonus if m < a else 0.0)
    if _in_window(current_minute, AFTERNOON):
        return bonus if a > m else (-bonus if a < m else 0.0)
    return 0.0


def combine(momentum: float, reg_bonus: float, time_bonus: float) -> float:
    """Clamp the summed multiplier to [MULT_MIN, MULT_MAX]."""
    return max(MULT_MIN, min(MULT_MAX, momentum + reg_bonus + time_bonus))
