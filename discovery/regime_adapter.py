"""
Reads approved discovery_results from PostgreSQL and returns the best strategy
for a given symbol under the current market regime.

Imported by bot.py swing_loop as a drop-in upgrade to hardcoded strategies.
Falls back gracefully (returns None) if DB is unavailable or no approved results exist.
"""
import json
from typing import Any

import pandas as pd
import pandas_ta as ta
from sqlalchemy import create_engine, text as sql_text

from config import Config

_VALID_REGIME_COLS = {"bull_sharpe", "bear_sharpe", "high_vol_sharpe", "test_sharpe"}


def _detect_regime(spy_bars: pd.DataFrame | None) -> str:
    """Returns 'bull', 'bear', or 'high_vol' based on SPY vs EMA200."""
    if spy_bars is None or spy_bars.empty or len(spy_bars) < 200:
        return "bull"
    ema200 = ta.ema(spy_bars["close"], length=200)
    if ema200 is None or pd.isna(ema200.iloc[-1]):
        return "bull"
    return "bull" if spy_bars["close"].iloc[-1] > ema200.iloc[-1] else "bear"


def get_best_strategy(
    symbol: str,
    spy_bars: pd.DataFrame | None = None,
    regime: str | None = None,
) -> dict[str, Any] | None:
    """
    Returns the best approved discovery strategy for the given symbol and regime.

    Return dict keys:
        strategy_type   str   e.g. "ema_trend"
        parameters      dict  e.g. {"ema_short": 20, "ema_long": 100, ...}
        test_sharpe     float
        best_regime     str
        current_regime  str

    Returns None if DATABASE_URL is not set, no approved results exist, or DB fails.
    """
    db_url = Config.DATABASE_URL
    if not db_url:
        return None

    current_regime = regime or _detect_regime(spy_bars)
    sharpe_col     = f"{current_regime}_sharpe"
    if sharpe_col not in _VALID_REGIME_COLS:
        sharpe_col = "test_sharpe"

    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(sql_text(f"""
                SELECT strategy_type, parameters::text, test_sharpe,
                       best_regime, bull_sharpe, bear_sharpe, high_vol_sharpe
                FROM discovery_results
                WHERE symbol = :sym AND status = 'approved'
                ORDER BY {sharpe_col} DESC NULLS LAST
                LIMIT 1
            """), {"sym": symbol}).mappings().fetchone()
        engine.dispose()

        if row is None:
            return None

        params_raw = row["parameters"]
        params_dict = json.loads(params_raw) if isinstance(params_raw, str) else dict(params_raw)

        return {
            "strategy_type":  row["strategy_type"],
            "parameters":     params_dict,
            "test_sharpe":    row["test_sharpe"],
            "best_regime":    row["best_regime"],
            "current_regime": current_regime,
        }

    except Exception as e:
        print(f"[RegimeAdapter] DB query failed for {symbol}: {e}")
        return None


def apply_to_swing_strategy(
    symbol: str,
    spy_bars: pd.DataFrame | None = None,
) -> tuple[str, dict] | None:
    """
    Convenience wrapper for swing_loop.
    Returns (strategy_type, parameters) or None.
    Only ema_trend maps directly to SwingStrategy params — other types return None
    so the caller falls back to the hardcoded strategy.
    Phase 3 will add full multi-strategy live execution for all types.
    """
    result = get_best_strategy(symbol, spy_bars)
    if result is None:
        return None
    return result["strategy_type"], result["parameters"]
