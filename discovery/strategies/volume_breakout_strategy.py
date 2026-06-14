"""
Discovery family 3 — Donchian volume breakout (Task 3).

Position-vector adapter compatible with the permutation/regime validation pipeline.

Entry:
    * long  when close breaks above the prior N-day high AND volume > mult * ADV
             AND OBV is rising over the last ``obv_lookback`` bars
    * short when close breaks below the prior N-day low  AND volume > mult * ADV
             AND OBV is falling over the last ``obv_lookback`` bars
Exit (Donchian channel exit, permutation-safe — close-only):
    * long  exits when close falls below the prior N-day low
    * short exits when close rises above the prior N-day high

The prior N-day high/low use ``shift(1)`` so the breakout bar is compared to a
channel that excludes itself (no lookahead). ADV is the rolling mean volume.
OBV is the standard on-balance-volume accumulation.

Posture vector S in {+1 long, -1 short, 0 flat}, one value per bar.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd


class VolumeBreakoutPositionStrategy:
    name = "volume_breakout_obv"

    PARAM_GRID = {
        "breakout_period": [15, 20, 25],
        "volume_mult": [1.5, 2.0, 2.5],
        "obv_lookback": [3, 5],
    }

    def param_grid(self) -> list[dict]:
        return [
            {"breakout_period": bp, "volume_mult": vm, "obv_lookback": ol}
            for bp, vm, ol in itertools.product(
                self.PARAM_GRID["breakout_period"],
                self.PARAM_GRID["volume_mult"],
                self.PARAM_GRID["obv_lookback"],
            )
        ]

    @staticmethod
    def _obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
        direction = np.sign(np.diff(close, prepend=close[0]))
        return np.cumsum(direction * volume)

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        n = len(df)
        pos = np.zeros(n, dtype=float)
        period = int(params["breakout_period"])
        mult = float(params["volume_mult"])
        ol = int(params["obv_lookback"])
        if n < period + ol + 1 or "volume" not in df.columns:
            return pos

        high = df["high"]
        low = df["low"]
        close = df["close"].to_numpy(dtype=float)
        volume = df["volume"].to_numpy(dtype=float)

        # Prior N-day channel (exclude current bar via shift(1)).
        prior_high = high.rolling(period).max().shift(1).to_numpy()
        prior_low = low.rolling(period).min().shift(1).to_numpy()
        adv = pd.Series(volume).rolling(period).mean().shift(1).to_numpy()
        obv = self._obv(close, volume)

        state = 0
        for i in range(1, n):
            if i < ol or any(not np.isfinite(v) for v in (prior_high[i], prior_low[i], adv[i])):
                pos[i] = state
                continue
            vol_ok = volume[i] > mult * adv[i] if adv[i] > 0 else False
            obv_rising = obv[i] > obv[i - ol]
            obv_falling = obv[i] < obv[i - ol]

            if state == 0:
                if close[i] > prior_high[i] and vol_ok and obv_rising:
                    state = 1
                elif close[i] < prior_low[i] and vol_ok and obv_falling:
                    state = -1
            elif state == 1:
                if close[i] < prior_low[i]:
                    state = 0
            elif state == -1:
                if close[i] > prior_high[i]:
                    state = 0
            pos[i] = state
        return pos
