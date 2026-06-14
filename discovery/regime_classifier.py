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
# Cross-asset confirmation cache (module-level): (detail_dict, timestamp).
_CROSS_ASSET_CACHE: tuple[dict, float] | None = None

# Sector-rotation baskets (Task 3 cross-asset confirmation).
_CYCLICAL_ETFS = ["XLK", "XLF"]    # risk-on leadership
_DEFENSIVE_ETFS = ["XLU", "XLP"]   # risk-off leadership
# Directional bias each regime "wants" confirmed: +1 risk-on, -1 risk-off, 0 none.
_REGIME_DIRECTION = {BULL_TREND: 1, BEAR_TREND: -1, HIGH_VOL: -1, CHOPPY: 0}


def _lookback_pct_change(data_client, symbol: str, lookback: int = 20) -> float | None:
    """20-day (default) percentage change of a symbol's close, or None on failure."""
    try:
        from alpaca.data.timeframe import TimeFrame
        from utils import get_historical_bars
        bars = get_historical_bars(symbol, TimeFrame.Day, lookback * 2 + 15, data_client, False)
        if bars is None or len(bars) < lookback + 1:
            return None
        closes = bars["close"]
        prev = float(closes.iloc[-1 - lookback])
        if prev <= 0:
            return None
        return float(closes.iloc[-1]) / prev - 1.0
    except Exception as e:
        print(f"[Regime] cross-asset fetch failed for {symbol} (non-fatal): {e}")
        return None


def _realized_vol_annualized(data_client, symbol: str = "SPY", window: int = 63) -> float | None:
    """3-month (default) annualized realized vol in VIX-like points — VIX3M proxy."""
    try:
        from alpaca.data.timeframe import TimeFrame
        from utils import get_historical_bars
        bars = get_historical_bars(symbol, TimeFrame.Day, window * 2 + 15, data_client, False)
        if bars is None or len(bars) < window + 1:
            return None
        rets = np.log(bars["close"] / bars["close"].shift(1)).dropna().tail(window)
        if len(rets) < 5:
            return None
        return float(rets.std() * np.sqrt(252) * 100.0)
    except Exception as e:
        print(f"[Regime] realized-vol proxy failed for {symbol} (non-fatal): {e}")
        return None


def compute_cross_asset_signals(
    data_client,
    vix_value: float | None = None,
    lookback: int = 20,
    cache_seconds: int | None = None,
) -> dict:
    """
    Supplementary cross-asset risk signals that adjust regime *confidence* without
    overriding the primary classification. Returns a dict::

        {
          "vix_structure": "backwardation" | "contango" | "neutral" | "n/a",
          "credit":        "risk_on" | "stress" | "neutral" | "n/a",
          "rotation":      "cyclical" | "defensive" | "neutral" | "n/a",
          "dollar":        "weak" | "strong" | "neutral" | "n/a",
          "scores": {name: -1 | 0 | +1},   # +1 risk-on, -1 risk-off
          "available": bool,
        }

    Fully fail-open: any unavailable feed contributes a neutral (0) score labelled
    "n/a". Cached for ``cache_seconds`` (default Config.REGIME_CACHE_SECONDS).
    """
    global _CROSS_ASSET_CACHE
    if not getattr(Config, "REGIME_CROSS_ASSET_ENABLED", True):
        return {"vix_structure": "n/a", "credit": "n/a", "rotation": "n/a",
                "dollar": "n/a", "scores": {}, "available": False}

    ttl = cache_seconds if cache_seconds is not None else Config.REGIME_CACHE_SECONDS
    if _CROSS_ASSET_CACHE is not None:
        detail, ts = _CROSS_ASSET_CACHE
        if time.time() - ts < ttl:
            return detail

    scores: dict[str, int] = {}

    # 1) VIX term structure: spot VIX (near) vs 3-month realized-vol proxy (far).
    #    Backwardation (near > far) = acute stress = risk-off.
    if vix_value is None:
        vix_value = _read_vix_from_macro()
    far = _realized_vol_annualized(data_client, "SPY", 63)
    if vix_value is not None and far is not None and far > 0:
        if vix_value > far * 1.05:
            vix_structure, scores["vix_structure"] = "backwardation", -1
        elif vix_value < far * 0.95:
            vix_structure, scores["vix_structure"] = "contango", 1
        else:
            vix_structure, scores["vix_structure"] = "neutral", 0
    else:
        vix_structure, scores["vix_structure"] = "n/a", 0

    # 2) Credit stress: HYG/LQD ratio 20-day change. Ratio falling = credit stress.
    hyg = _lookback_pct_change(data_client, "HYG", lookback)
    lqd = _lookback_pct_change(data_client, "LQD", lookback)
    if hyg is not None and lqd is not None:
        ratio_chg = hyg - lqd  # relative outperformance of high-yield vs investment-grade
        if ratio_chg > 0.003:
            credit, scores["credit"] = "risk_on", 1
        elif ratio_chg < -0.003:
            credit, scores["credit"] = "stress", -1
        else:
            credit, scores["credit"] = "neutral", 0
    else:
        credit, scores["credit"] = "n/a", 0

    # 3) Sector rotation: cyclical vs defensive relative strength vs SPY.
    spy_chg = _lookback_pct_change(data_client, "SPY", lookback)
    cyc = [_lookback_pct_change(data_client, s, lookback) for s in _CYCLICAL_ETFS]
    deff = [_lookback_pct_change(data_client, s, lookback) for s in _DEFENSIVE_ETFS]
    cyc = [c for c in cyc if c is not None]
    deff = [d for d in deff if d is not None]
    if spy_chg is not None and cyc and deff:
        cyc_rs = sum(cyc) / len(cyc) - spy_chg
        def_rs = sum(deff) / len(deff) - spy_chg
        if def_rs > cyc_rs + 0.005:
            rotation, scores["rotation"] = "defensive", -1
        elif cyc_rs > def_rs + 0.005:
            rotation, scores["rotation"] = "cyclical", 1
        else:
            rotation, scores["rotation"] = "neutral", 0
    else:
        rotation, scores["rotation"] = "n/a", 0

    # 4) Dollar: UUP 20-day trend. Strong dollar = headwind for risk assets.
    uup = _lookback_pct_change(data_client, "UUP", lookback)
    if uup is not None:
        if uup > 0.01:
            dollar, scores["dollar"] = "strong", -1
        elif uup < -0.01:
            dollar, scores["dollar"] = "weak", 1
        else:
            dollar, scores["dollar"] = "neutral", 0
    else:
        dollar, scores["dollar"] = "n/a", 0

    detail = {
        "vix_structure": vix_structure,
        "credit": credit,
        "rotation": rotation,
        "dollar": dollar,
        "scores": scores,
        "available": any(v != "n/a" for v in (vix_structure, credit, rotation, dollar)),
    }
    _CROSS_ASSET_CACHE = (detail, time.time())
    return detail


def regime_confidence(regime: str, signals: dict) -> int:
    """
    Confidence score 0-100 that the cross-asset signals confirm ``regime``.

    Baseline 50. Each cross-asset signal aligned with the regime's risk direction
    adds points; each opposed subtracts. CHOPPY (no direction) reports 50 adjusted
    by how *mixed* the signals are (a strong net bias lowers CHOPPY confidence).
    """
    scores = list(signals.get("scores", {}).values())
    direction = _REGIME_DIRECTION.get(regime, 0)
    if not scores:
        return 50
    if direction == 0:
        net = abs(sum(scores))            # strong net bias contradicts "choppy"
        return int(max(0, min(100, 50 - net * 10)))
    aligned = sum(1 for s in scores if s == direction)
    opposed = sum(1 for s in scores if s == -direction)
    step = 50.0 / max(len(scores), 1)     # full alignment → 100, full opposition → 0
    return int(max(0, min(100, round(50 + (aligned - opposed) * step))))


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

    # Cross-asset confirmation (Task 3): supplementary signals + confidence score.
    try:
        signals = compute_cross_asset_signals(alpaca_client, vix_value, cache_seconds=ttl)
        conf = regime_confidence(regime, signals)
        print(
            f"[Regime] {regime} confidence={conf}% | "
            f"VIX_structure={signals['vix_structure']} credit={signals['credit']} "
            f"rotation={signals['rotation']} dollar={signals['dollar']}"
        )
        _CURRENT_REGIME_DETAIL_CACHE_set(regime, conf, signals)
    except Exception as e:
        import traceback
        print(f"[Regime] cross-asset confirmation failed (non-fatal): {e}\n{traceback.format_exc()}")

    _CURRENT_REGIME_CACHE = (regime, time.time())
    return regime


# Detail cache (regime + confidence + cross-asset signals) for dashboards / callers.
_CURRENT_REGIME_DETAIL_CACHE: dict | None = None


def _CURRENT_REGIME_DETAIL_CACHE_set(regime: str, confidence: int, signals: dict) -> None:
    global _CURRENT_REGIME_DETAIL_CACHE
    _CURRENT_REGIME_DETAIL_CACHE = {
        "regime": regime,
        "confidence": confidence,
        "signals": signals,
        "timestamp": time.time(),
    }


def get_current_regime_detail() -> dict | None:
    """Return the last computed {regime, confidence, signals, timestamp} or None."""
    return _CURRENT_REGIME_DETAIL_CACHE
