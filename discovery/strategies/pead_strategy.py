"""
Discovery family 6 — Post-Earnings Announcement Drift (PEAD).

Position-vector adapter compatible with the permutation/regime validation pipeline
(``SwingPositionStrategy`` interface: ``name``, ``param_grid()``, ``position_vector()``).

Anomaly
-------
Stocks that beat earnings estimates tend to drift *up* for 30-60 days; stocks that
miss tend to drift *down*. We trade the drift while skipping the initial
post-announcement volatility spike.

Signal logic
------------
On each bar carrying an earnings announcement (non-zero ``earnings_surprise``):
  * surprise > +threshold  → schedule a LONG  ``entry_delay_days`` bars later,
                             entered only if close > EMA(``ema_period``) that day.
  * surprise < -threshold  → schedule a SHORT ``entry_delay_days`` bars later,
                             entered only if close < EMA(``ema_period``) that day.
Hold up to ``hold_days`` trading bars. Exit early if price moves against the
position by 2x ATR(14) from the entry price (close-based, permutation-safe).

Data requirement & fail-open
----------------------------
Needs a per-bar ``earnings_surprise`` column (fraction; 0.05 == +5%), injected by
``fmp_earnings_calendar.attach_earnings_surprise``. The discovery engine calls the
``enrich`` static method below to attach it. With no column / no FMP data the
strategy returns an all-flat vector (no trades) and simply never validates — it
does NOT crash the pipeline.

Posture vector S in {+1 long, -1 short, 0 flat}, one value per bar.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

EARNINGS_COLUMN = "earnings_surprise"
_ATR_PERIOD = 14
_ADVERSE_ATR_MULT = 2.0  # exit early if price moves this many ATRs against entry


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


def _compute_ema(close: pd.Series, span: int) -> np.ndarray:
    return close.ewm(span=span, adjust=False).mean().to_numpy(dtype=float)


class PEADPositionStrategy:
    name = "pead_earnings_drift"

    PARAM_GRID = {
        "surprise_threshold": [0.03, 0.05, 0.10],
        "entry_delay_days": [1, 2, 3],
        "hold_days": [15, 20, 30],
        "ema_period": [10, 20],
    }

    def param_grid(self) -> list[dict]:
        return [
            {
                "surprise_threshold": st,
                "entry_delay_days": ed,
                "hold_days": hd,
                "ema_period": ep,
            }
            for st, ed, hd, ep in itertools.product(
                self.PARAM_GRID["surprise_threshold"],
                self.PARAM_GRID["entry_delay_days"],
                self.PARAM_GRID["hold_days"],
                self.PARAM_GRID["ema_period"],
            )
        ]

    @staticmethod
    def enrich(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Attach the per-bar ``earnings_surprise`` column (used by the engine)."""
        try:
            from fmp_earnings_calendar import attach_earnings_surprise
            return attach_earnings_surprise(bars, symbol)
        except Exception:
            import traceback
            print(f"[PEAD] enrich failed for {symbol}:\n{traceback.format_exc()}")
            return bars

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        n = len(df)
        pos = np.zeros(n, dtype=float)

        if EARNINGS_COLUMN not in df.columns:
            return pos  # documented fail-open: no earnings feed → no trades

        threshold = float(params["surprise_threshold"])
        delay = int(params["entry_delay_days"])
        hold = int(params["hold_days"])
        ema_period = int(params["ema_period"])

        if n < ema_period + delay + 2:
            return pos

        close = df["close"].to_numpy(dtype=float)
        ema = _compute_ema(df["close"], ema_period)
        atr = _compute_atr(df, _ATR_PERIOD)
        surprise = df[EARNINGS_COLUMN].to_numpy(dtype=float)

        state = 0.0
        entry_price = np.nan
        entry_atr = np.nan
        bars_held = 0

        for i in range(1, n):
            if state != 0.0:
                bars_held += 1
                c = close[i]
                exit_now = False
                # Time stop.
                if bars_held >= hold:
                    exit_now = True
                # Adverse-move stop (2x ATR against the position).
                elif np.isfinite(entry_price) and np.isfinite(entry_atr) and entry_atr > 0:
                    if state > 0 and c <= entry_price - _ADVERSE_ATR_MULT * entry_atr:
                        exit_now = True
                    elif state < 0 and c >= entry_price + _ADVERSE_ATR_MULT * entry_atr:
                        exit_now = True
                if exit_now:
                    state = 0.0
                    entry_price = entry_atr = np.nan
                    bars_held = 0
                pos[i] = state
                continue

            # Flat: look for an announcement `delay` bars back that qualifies now.
            ann_idx = i - delay
            if ann_idx < 0:
                pos[i] = state
                continue
            s = surprise[ann_idx]
            if s == 0.0 or not np.isfinite(ema[i]) or not np.isfinite(atr[i]):
                pos[i] = state
                continue

            c = close[i]
            if s > threshold and c > ema[i]:
                state = 1.0
            elif s < -threshold and c < ema[i]:
                state = -1.0

            if state != 0.0:
                entry_price = c
                entry_atr = atr[i]
                bars_held = 0
            pos[i] = state

        return pos
