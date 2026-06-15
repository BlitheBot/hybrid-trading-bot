"""
Discovery family 4 — SEC Form 4 insider-flow momentum (Task 3, long only).

Position-vector adapter compatible with the permutation/regime validation pipeline.

Entry (long only):
    * cumulative insider *buy* dollar value over the last ``lookback`` days
      exceeds ``insider_threshold`` AND close > EMA(``ema_period``).
Exit:
    * close falls below EMA(``ema_period``) (trend exit, permutation-safe).

Data requirement & known limitation
------------------------------------
This family needs a per-bar ``insider_buy_value`` column (net Form 4 buy dollars
that day). The Discovery Engine's OHLCV loader does not currently backfill a
historical Form 4 series, so on plain OHLCV bars ``position_vector`` returns an
all-flat vector (no trades) and the family will simply never validate — it does
NOT crash the pipeline. The live SEC EDGAR strategy supplies real-time insider
signals (consumed by the Task 5 composite score); wiring a historical Form 4
feed into the backtester is tracked as future work.

Posture vector S in {+1 long, 0 flat}, one value per bar.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import pandas_ta as ta

INSIDER_COLUMN = "insider_buy_value"


class InsiderFlowPositionStrategy:
    name = "insider_flow_form4"

    PARAM_GRID = {
        "insider_threshold": [50_000, 100_000, 250_000],
        "lookback": [3, 5, 7],
        "ema_period": [20, 50],
    }

    def param_grid(self) -> list[dict]:
        return [
            {"insider_threshold": it, "lookback": lb, "ema_period": ep}
            for it, lb, ep in itertools.product(
                self.PARAM_GRID["insider_threshold"],
                self.PARAM_GRID["lookback"],
                self.PARAM_GRID["ema_period"],
            )
        ]

    @staticmethod
    def enrich(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """
        Attach the per-bar ``insider_buy_value`` column from the historical SEC
        Form 4 feed (Task 5). Gated by DISCOVERY_INSIDER_FEED_ENABLED; on any error
        or when disabled, returns ``bars`` unchanged so the family stays all-flat.
        """
        from config import Config
        if not getattr(Config, "DISCOVERY_INSIDER_FEED_ENABLED", True):
            return bars
        try:
            from discovery.data_feeds.edgar_historical import attach_insider_buy_value
            return attach_insider_buy_value(bars, symbol)
        except Exception:
            import traceback
            print(f"[InsiderFlow] enrich failed for {symbol}:\n{traceback.format_exc()}")
            return bars

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        n = len(df)
        pos = np.zeros(n, dtype=float)

        # No historical insider series → no trades (documented limitation).
        if INSIDER_COLUMN not in df.columns:
            return pos

        period = int(params["ema_period"])
        lookback = int(params["lookback"])
        threshold = float(params["insider_threshold"])
        if n < period + 1:
            return pos

        close = df["close"]
        ema = ta.ema(close, length=period)
        if ema is None:
            return pos
        ema = ema.to_numpy()
        c = close.to_numpy()
        insider_window = df[INSIDER_COLUMN].rolling(lookback, min_periods=1).sum().to_numpy()

        state = 0
        for i in range(1, n):
            if not np.isfinite(ema[i]):
                pos[i] = state
                continue
            if state == 0:
                if insider_window[i] >= threshold and c[i] > ema[i]:
                    state = 1
            elif state == 1:
                if c[i] < ema[i]:
                    state = 0
            pos[i] = state
        return pos
