"""
Market regime classifier — labels each trading day as one of four regimes so the
Discovery Engine can validate strategies *within* each regime and the live bot can
gate signals to only the regime(s) a strategy was validated for.

Regimes (priority order — HIGH_VOL overrides trend):

    HIGH_VOL    VIX > 30 regardless of trend
    BULL_TREND  SPY EMA50 > EMA200  AND  VIX < 20  AND  SPY 20-day return >  2%
    BEAR_TREND  SPY EMA50 < EMA200  AND  VIX > 25  AND  SPY 20-day return < -2%
    CHOPPY      everything else (low directional conviction)

VIX handling is graceful: if VIX is unavailable (FRED timeout / missing), the
classifier falls back to SPY-only rules (HIGH_VOL can never be assigned, and the
VIX sub-conditions of BULL/BEAR are dropped).

For historical backtest tagging where a per-bar VIX series is not available, use
``realized_vol_proxy`` to derive a VIX-like series from SPY returns.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from config import Config

BULL_TREND = "BULL_TREND"
BEAR_TREND = "BEAR_TREND"
HIGH_VOL = "HIGH_VOL"
CHOPPY = "CHOPPY"

REGIMES = [BULL_TREND, BEAR_TREND, HIGH_VOL, CHOPPY]

# Live regime cache (module-level): (regime, timestamp).
_CURRENT_REGIME_CACHE: tuple[str, float] | None = None


def realized_vol_proxy(spy_bars: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    VIX-like series from SPY close-to-close returns: annualized rolling realized
    volatility expressed in VIX points (≈ percent). Used only for historical
    regime tagging in the backtester when a true per-bar VIX series is absent.
    """
    rets = np.log(spy_bars["close"] / spy_bars["close"].shift(1))
    return rets.rolling(window).std() * np.sqrt(252) * 100.0


def classify_regime(spy_bars: pd.DataFrame, vix_value) -> pd.DataFrame:
    """
    Return a copy of ``spy_bars`` with an added ``regime`` column.

    ``vix_value`` may be:
      * a scalar (broadcast to every bar — typical for live classification),
      * an array / Series aligned to ``spy_bars`` (per-bar — backtest tagging),
      * None / NaN (SPY-only fallback — HIGH_VOL disabled, VIX gates dropped).
    """
    df = spy_bars.copy()
    close = df["close"]

    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    ret20 = close.pct_change(20)

    n = len(df)
    # Normalize VIX into a per-bar float array (NaN where unknown).
    if vix_value is None:
        vix = np.full(n, np.nan)
    elif np.isscalar(vix_value):
        vix = np.full(n, float(vix_value) if vix_value is not None else np.nan)
    else:
        vix = np.asarray(pd.Series(vix_value).to_numpy(), dtype=float)
        if vix.shape[0] != n:
            raise ValueError(f"vix_value length {vix.shape[0]} != spy_bars length {n}")

    up_trend = (ema50 > ema200).to_numpy()
    down_trend = (ema50 < ema200).to_numpy()
    strong_up = (ret20 > Config.REGIME_BULL_RETURN_PCT).to_numpy()
    strong_down = (ret20 < Config.REGIME_BEAR_RETURN_PCT).to_numpy()

    vix_known = ~np.isnan(vix)
    high_vol = vix_known & (vix > Config.REGIME_HIGH_VOL_VIX)
    bull_vix = (~vix_known) | (vix < Config.REGIME_BULL_VIX_MAX)
    bear_vix = (~vix_known) | (vix > Config.REGIME_BEAR_VIX_MIN)

    regime = np.full(n, CHOPPY, dtype=object)
    # CHOPPY default; assign in reverse priority so HIGH_VOL wins last (overrides).
    is_bull = up_trend & strong_up & bull_vix
    is_bear = down_trend & strong_down & bear_vix
    regime[is_bull] = BULL_TREND
    regime[is_bear] = BEAR_TREND
    regime[high_vol] = HIGH_VOL  # override trend regimes

    df["regime"] = regime
    return df


def _read_vix_from_macro() -> float | None:
    """Pull the latest VIX from the FRED-sourced MACRO_SNAPSHOT, if present."""
    try:
        from strategies.fred_strategy import MACRO_SNAPSHOT
        vix = MACRO_SNAPSHOT.get("vix")
        return float(vix) if vix is not None else None
    except Exception as e:
        print(f"[Regime] VIX read from MACRO_SNAPSHOT failed (non-fatal): {e}")
        return None


def get_current_regime(
    alpaca_client,
    fred_client=None,
    vix_value: float | None = None,
    cache_seconds: int | None = None,
) -> str:
    """
    Live current regime. Fetches recent SPY daily bars via ``alpaca_client`` and
    the current VIX (explicit ``vix_value`` > FRED-sourced MACRO_SNAPSHOT).
    Result cached for ``cache_seconds`` (default Config.REGIME_CACHE_SECONDS, 4h).
    Falls back to SPY-only classification when VIX is unavailable.
    """
    global _CURRENT_REGIME_CACHE
    ttl = cache_seconds if cache_seconds is not None else Config.REGIME_CACHE_SECONDS

    if _CURRENT_REGIME_CACHE is not None:
        regime, ts = _CURRENT_REGIME_CACHE
        if time.time() - ts < ttl:
            return regime

    if vix_value is None:
        vix_value = _read_vix_from_macro()

    regime = CHOPPY
    ema50 = ema200 = float("nan")
    try:
        from alpaca.data.timeframe import TimeFrame
        from utils import get_historical_bars

        spy = get_historical_bars("SPY", TimeFrame.Day, 260, alpaca_client, is_crypto=False)
        if spy is not None and len(spy) >= 200:
            tagged = classify_regime(spy, vix_value)
            regime = str(tagged["regime"].iloc[-1])
            ema50 = float(spy["close"].ewm(span=50, adjust=False).mean().iloc[-1])
            ema200 = float(spy["close"].ewm(span=200, adjust=False).mean().iloc[-1])
        else:
            print("[Regime] Insufficient SPY history (<200 bars) — defaulting to CHOPPY")
    except Exception as e:
        import traceback
        print(f"[Regime] get_current_regime failed — defaulting to CHOPPY: {e}\n{traceback.format_exc()}")

    vix_disp = vix_value if vix_value is not None else float("nan")
    print(
        f"[Regime] Current market regime: {regime} | "
        f"SPY EMA50={ema50:.2f} EMA200={ema200:.2f} VIX={vix_disp:.1f}"
    )

    _CURRENT_REGIME_CACHE = (regime, time.time())
    return regime
