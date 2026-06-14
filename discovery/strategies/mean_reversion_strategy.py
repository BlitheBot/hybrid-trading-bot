"""
Discovery family 2 — Bollinger-Band mean reversion (Task 3).

Position-vector adapter compatible with the permutation/regime validation pipeline
(`SwingPositionStrategy` interface: ``name``, ``param_grid()``, ``position_vector()``).

Entry:
    * long  when close <= lower band AND RSI < 35 (oversold stretch)
    * short when close >= upper band AND RSI > 65 (overbought stretch)
Exit:
    * revert to flat when close crosses back through the middle band (the mean).

Like ``SwingPositionStrategy`` the exit is bar-granular on the close (no intra-bar
stop/target) so the backtest stays permutation-safe — permuted high/low ordering
would otherwise bias intra-bar stop fills. ATR-based stops are applied in *live*
execution; here the mean-reversion-to-middle exit is the permutation-safe analogue.

Posture vector S in {+1 long, -1 short, 0 flat}, one value per bar.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import pandas_ta as ta

RSI_LONG_MAX = 35.0   # RSI must be below this to confirm a long
RSI_SHORT_MIN = 65.0  # RSI must be above this to confirm a short


class MeanReversionPositionStrategy:
    name = "mean_reversion_bb_rsi"

    PARAM_GRID = {
        "bb_period": [15, 20, 25],
        "bb_std": [1.5, 2.0, 2.5],
        "rsi_period": [10, 14],
    }

    def param_grid(self) -> list[dict]:
        return [
            {"bb_period": bp, "bb_std": bs, "rsi_period": rp}
            for bp, bs, rp in itertools.product(
                self.PARAM_GRID["bb_period"],
                self.PARAM_GRID["bb_std"],
                self.PARAM_GRID["rsi_period"],
            )
        ]

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        close = df["close"]
        n = len(df)
        pos = np.zeros(n, dtype=float)

        period = int(params["bb_period"])
        nstd = float(params["bb_std"])
        rsi = ta.rsi(close, length=int(params["rsi_period"]))
        if rsi is None or n < period + 1:
            return pos

        middle = close.rolling(period).mean()
        sd = close.rolling(period).std(ddof=0)
        upper = (middle + nstd * sd).to_numpy()
        lower = (middle - nstd * sd).to_numpy()
        mid = middle.to_numpy()
        c = close.to_numpy()
        r = rsi.to_numpy()

        state = 0  # -1 short, 0 flat, +1 long
        for i in range(1, n):
            if any(not np.isfinite(v) for v in (upper[i], lower[i], mid[i], r[i])):
                pos[i] = state
                continue
            if state == 0:
                if c[i] <= lower[i] and r[i] < RSI_LONG_MAX:
                    state = 1
                elif c[i] >= upper[i] and r[i] > RSI_SHORT_MIN:
                    state = -1
            elif state == 1:
                if c[i] >= mid[i]:
                    state = 0
            elif state == -1:
                if c[i] <= mid[i]:
                    state = 0
            pos[i] = state
        return pos
