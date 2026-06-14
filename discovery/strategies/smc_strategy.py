"""
Discovery family 5 -- Smart Money Concepts (SMC).

Position-vector adapter compatible with the permutation/regime validation pipeline
(``SwingPositionStrategy`` interface: ``name``, ``param_grid()``, ``position_vector()``).

Entry:
    * long  when close is within a bullish order block zone AND at least one
            unfilled bullish FVG target exists above current price AND RSI < 55
            AND EMA50 > EMA200.
    * short when close is within a bearish order block zone AND at least one
            unfilled bearish FVG target exists below current price AND RSI > 45
            AND EMA50 < EMA200.

Exit (permutation-safe close-only):
    * long  exits on EMA50 < EMA200 (trend reversal) OR close < entry OB low
            (order block invalidated by price).
    * short exits on EMA50 > EMA200 OR close > entry OB high.

Order Block:
    The candle immediately before an impulse bar (single-bar move > atr_multiplier *
    ATR). Bearish OB = last bullish candle before a large down move; bullish OB =
    last bearish candle before a large up move. Invalidated when price closes through
    the OB's high (bearish) or low (bullish).

Fair Value Gap (FVG):
    3-candle imbalance: candle[i].low > candle[i-2].high (bullish FVG) or
    candle[i].high < candle[i-2].low (bearish FVG), with gap >= min_gap_pct of
    price. An unfilled bullish FVG above current price acts as a magnet for longs;
    an unfilled bearish FVG below acts as a magnet for shorts. A FVG is marked
    filled when price closes within or through the gap zone.

Posture vector S in {+1 long, -1 short, 0 flat}, one value per bar.

Public API (for live bot confirmation gate):
    detect_order_blocks(df, atr_multiplier, lookback) -> list[dict]
    detect_fair_value_gaps(df, min_gap_pct)           -> list[dict]
"""
from __future__ import annotations

import itertools
from typing import NamedTuple

import numpy as np
import pandas as pd

_ATR_PERIOD = 14
_EMA_SHORT = 50
_EMA_LONG = 200


# ---------------------------------------------------------------------------
# Private indicator helpers (pure pandas/numpy, no TA-Lib)
# ---------------------------------------------------------------------------

def _compute_atr(df: pd.DataFrame, period: int = _ATR_PERIOD) -> np.ndarray:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean().to_numpy(dtype=float)


def _compute_rsi(close: pd.Series, period: int) -> np.ndarray:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).to_numpy(dtype=float)


def _compute_ema(close: pd.Series, span: int) -> np.ndarray:
    return close.ewm(span=span, adjust=False).mean().to_numpy(dtype=float)


# ---------------------------------------------------------------------------
# Public standalone detection functions (used by live bot confirmation gate)
# ---------------------------------------------------------------------------

def detect_order_blocks(
    df: pd.DataFrame,
    atr_multiplier: float = 1.5,
    lookback: int = 20,
) -> list[dict]:
    """
    Scan the last *lookback* bars of *df* for single-bar impulse moves
    (abs move > atr_multiplier * ATR) and tag the candle immediately before
    each impulse as an order block.

    Returns active (non-invalidated) order blocks sorted by descending strength
    (impulse size / ATR).  Each entry is a dict:
        {high, low, direction ('bullish'|'bearish'), strength}

    A bearish OB is the last bullish candle before a big down move.
    A bullish OB is the last bearish candle before a big up move.
    An OB is invalidated when price subsequently closes through it.
    """
    n = len(df)
    min_bars = _ATR_PERIOD + 2
    if n < min_bars:
        return []

    close = df["close"].to_numpy(dtype=float)
    open_ = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    atr = _compute_atr(df)

    # Scan the last `lookback` bars for IMPULSE bars.
    scan_start = max(1, n - lookback)
    obs: list[dict] = []

    for i in range(scan_start, n):
        if not np.isfinite(atr[i]) or atr[i] <= 0.0:
            continue
        move = close[i] - close[i - 1]
        if abs(move) < atr_multiplier * atr[i]:
            continue  # not an impulse bar

        ob_idx = i - 1
        if ob_idx < 0:
            continue

        ob_h = high[ob_idx]
        ob_l = low[ob_idx]

        if move > 0:
            # Bullish impulse -> OB must be a bearish (down) candle
            if open_[ob_idx] <= close[ob_idx]:
                continue
            direction = "bullish"
        else:
            # Bearish impulse -> OB must be a bullish (up) candle
            if close[ob_idx] <= open_[ob_idx]:
                continue
            direction = "bearish"

        strength = abs(move) / atr[i]

        # Invalidation check: did price close through the OB after it formed?
        invalidated = False
        for k in range(i, n):
            if direction == "bullish" and close[k] < ob_l:
                invalidated = True
                break
            if direction == "bearish" and close[k] > ob_h:
                invalidated = True
                break
        if invalidated:
            continue

        obs.append({"high": ob_h, "low": ob_l, "direction": direction, "strength": strength})

    obs.sort(key=lambda x: x["strength"], reverse=True)
    return obs


def detect_fair_value_gaps(
    df: pd.DataFrame,
    min_gap_pct: float = 0.10,
) -> list[dict]:
    """
    Scan *df* for 3-candle fair value gap (FVG) patterns where the gap size
    exceeds *min_gap_pct* of the reference price.

    Returns unfilled FVGs sorted by recency (most recent first).  Each entry:
        {upper, lower, direction ('bullish'|'bearish'), bar_index}

    Bullish FVG: candle[i].low > candle[i-2].high -- gap above candle i-2.
    Bearish FVG: candle[i].high < candle[i-2].low -- gap below candle i-2.

    A FVG is "filled" when price closes within/through the gap zone after it
    formed (bullish: close <= upper; bearish: close >= lower).
    """
    n = len(df)
    if n < 3:
        return []

    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)

    fvgs: list[dict] = []

    for i in range(2, n):
        if low[i] > high[i - 2]:
            # Bullish FVG: gap between high[i-2] and low[i]
            gap_upper = low[i]
            gap_lower = high[i - 2]
            direction = "bullish"
        elif high[i] < low[i - 2]:
            # Bearish FVG: gap between high[i] and low[i-2]
            gap_upper = low[i - 2]
            gap_lower = high[i]
            direction = "bearish"
        else:
            continue

        if gap_upper <= gap_lower:
            continue

        ref = close[i - 1] if np.isfinite(close[i - 1]) and close[i - 1] > 0 else close[i]
        if ref <= 0 or (gap_upper - gap_lower) / ref < min_gap_pct:
            continue

        # A FVG is filled when price reaches the zone from the opposite side after
        # formation.  Bullish FVG (gap above): filled when close rallies up to
        # at least the bottom of the zone (close >= lower).  Bearish FVG (gap
        # below): filled when close drops to at least the top of the zone (close
        # <= upper).
        filled = False
        for k in range(i + 1, n):
            if direction == "bullish" and close[k] >= gap_lower:
                filled = True
                break
            if direction == "bearish" and close[k] <= gap_upper:
                filled = True
                break
        if filled:
            continue

        fvgs.append({
            "upper": gap_upper,
            "lower": gap_lower,
            "direction": direction,
            "bar_index": i,
        })

    fvgs.sort(key=lambda x: x["bar_index"], reverse=True)
    return fvgs


# ---------------------------------------------------------------------------
# Position-vector strategy class
# ---------------------------------------------------------------------------

class SMCPositionStrategy:
    """Discovery family 5 -- Smart Money Concepts order block + FVG strategy."""

    name = "smc_order_block_fvg"

    PARAM_GRID = {
        "atr_multiplier": [1.2, 1.5, 2.0],
        "lookback":       [15, 20, 30],
        "min_gap_pct":    [0.05, 0.10, 0.15],
        "rsi_period":     [10, 14],
    }

    def param_grid(self) -> list[dict]:
        return [
            {
                "atr_multiplier": am,
                "lookback": lb,
                "min_gap_pct": gp,
                "rsi_period": rp,
            }
            for am, lb, gp, rp in itertools.product(
                self.PARAM_GRID["atr_multiplier"],
                self.PARAM_GRID["lookback"],
                self.PARAM_GRID["min_gap_pct"],
                self.PARAM_GRID["rsi_period"],
            )
        ]

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        """
        Bar-by-bar simulation. At each bar only data up to that bar is used
        (no look-ahead). Uses close-only exits so the vector is permutation-safe.
        """
        n = len(df)
        pos = np.zeros(n, dtype=float)

        atr_mult = float(params["atr_multiplier"])
        lookback = int(params["lookback"])
        min_gap_pct = float(params["min_gap_pct"])
        rsi_period = int(params["rsi_period"])

        # Need enough bars for EMA200 warmup + lookback + 3-bar FVG pattern.
        min_bars = _EMA_LONG + lookback + 3
        if n < min_bars:
            return pos

        close = df["close"].to_numpy(dtype=float)
        open_ = df["open"].to_numpy(dtype=float)
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)

        atr = _compute_atr(df, _ATR_PERIOD)
        rsi = _compute_rsi(df["close"], rsi_period)
        ema50 = _compute_ema(df["close"], _EMA_SHORT)
        ema200 = _compute_ema(df["close"], _EMA_LONG)

        state: float = 0.0
        entry_ob_high = np.nan
        entry_ob_low = np.nan

        for i in range(_EMA_LONG, n):
            if not all(
                np.isfinite(v) for v in (atr[i], rsi[i], ema50[i], ema200[i])
            ):
                pos[i] = state
                continue

            c = close[i]
            lb_start = max(1, i - lookback)

            # ---- Detect active order blocks up to bar i ----
            obs_bullish: list[tuple[float, float, float]] = []  # (high, low, strength)
            obs_bearish: list[tuple[float, float, float]] = []

            for j in range(lb_start, i):
                if not np.isfinite(atr[j]) or atr[j] <= 0.0:
                    continue
                move = close[j] - close[j - 1]
                if abs(move) < atr_mult * atr[j]:
                    continue
                ob_idx = j - 1
                if ob_idx < 0:
                    continue
                ob_h = high[ob_idx]
                ob_l = low[ob_idx]

                if move > 0:
                    if open_[ob_idx] <= close[ob_idx]:
                        continue
                    direction_ob = "bullish"
                else:
                    if close[ob_idx] <= open_[ob_idx]:
                        continue
                    direction_ob = "bearish"

                strength = abs(move) / atr[j]

                # Check invalidation between j and i (inclusive).
                invalidated = False
                for k in range(j, i + 1):
                    if direction_ob == "bullish" and close[k] < ob_l:
                        invalidated = True
                        break
                    if direction_ob == "bearish" and close[k] > ob_h:
                        invalidated = True
                        break
                if invalidated:
                    continue

                if direction_ob == "bullish":
                    obs_bullish.append((ob_h, ob_l, strength))
                else:
                    obs_bearish.append((ob_h, ob_l, strength))

            # ---- Detect unfilled FVGs in lookback window up to bar i ----
            bull_fvg_above = False  # unfilled bullish FVG whose lower edge > close[i]
            bear_fvg_below = False  # unfilled bearish FVG whose upper edge < close[i]

            fvg_start = max(2, lb_start)
            for j in range(fvg_start, i):
                if low[j] > high[j - 2]:
                    gap_u = low[j]
                    gap_l = high[j - 2]
                    fvg_dir = "bullish"
                elif high[j] < low[j - 2]:
                    gap_u = low[j - 2]
                    gap_l = high[j]
                    fvg_dir = "bearish"
                else:
                    continue

                if gap_u <= gap_l:
                    continue
                ref = close[j - 1] if np.isfinite(close[j - 1]) and close[j - 1] > 0 else close[j]
                if ref <= 0 or (gap_u - gap_l) / ref < min_gap_pct:
                    continue

                # Check if filled between j+1 and i (inclusive).
                filled = False
                for k in range(j + 1, i + 1):
                    if fvg_dir == "bullish" and close[k] >= gap_l:
                        filled = True
                        break
                    if fvg_dir == "bearish" and close[k] <= gap_u:
                        filled = True
                        break
                if filled:
                    continue

                if fvg_dir == "bullish" and gap_l > c:
                    bull_fvg_above = True
                elif fvg_dir == "bearish" and gap_u < c:
                    bear_fvg_below = True

            # ---- State machine ----
            if state == 0.0:
                # LONG entry: in bullish OB + bullish FVG target above + RSI < 55 + uptrend
                in_bull_ob = any(ob_l <= c <= ob_h for ob_h, ob_l, _ in obs_bullish)
                if in_bull_ob and bull_fvg_above and rsi[i] < 55.0 and ema50[i] > ema200[i]:
                    state = 1.0
                    matching = [(ob_h, ob_l) for ob_h, ob_l, _ in obs_bullish if ob_l <= c <= ob_h]
                    entry_ob_high = max(h for h, _ in matching) if matching else np.nan
                    entry_ob_low = min(l for _, l in matching) if matching else np.nan
                else:
                    # SHORT entry: in bearish OB + bearish FVG target below + RSI > 45 + downtrend
                    in_bear_ob = any(ob_l <= c <= ob_h for ob_h, ob_l, _ in obs_bearish)
                    if in_bear_ob and bear_fvg_below and rsi[i] > 45.0 and ema50[i] < ema200[i]:
                        state = -1.0
                        matching = [(ob_h, ob_l) for ob_h, ob_l, _ in obs_bearish if ob_l <= c <= ob_h]
                        entry_ob_high = max(h for h, _ in matching) if matching else np.nan
                        entry_ob_low = min(l for _, l in matching) if matching else np.nan

            elif state == 1.0:
                # Exit long: trend reversal or OB invalidated
                if ema50[i] < ema200[i] or (np.isfinite(entry_ob_low) and c < entry_ob_low):
                    state = 0.0

            elif state == -1.0:
                # Exit short: trend reversal or OB invalidated
                if ema50[i] > ema200[i] or (np.isfinite(entry_ob_high) and c > entry_ob_high):
                    state = 0.0

            pos[i] = state

        return pos
