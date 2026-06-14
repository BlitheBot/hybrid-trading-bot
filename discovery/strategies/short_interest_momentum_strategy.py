"""
Discovery family 7 — Short-Interest Momentum.

Position-vector adapter compatible with the permutation/regime validation pipeline
(``SwingPositionStrategy`` interface: ``name``, ``param_grid()``, ``position_vector()``).

Thesis
------
Short-interest *changes* are predictive: rising short interest on deteriorating
technicals is bearish; sharply declining short interest on improving technicals is
a short-squeeze setup (bullish, higher conviction).

Signal logic
------------
  * SHORT  : si_change > +short_increase_threshold AND RSI > 55 AND EMA50 < EMA200
  * LONG   : si_change < -short_decrease_threshold AND RSI < 65 AND EMA50 > EMA200
             AND today's return > +2% AND volume > volume_mult * 20-bar avg volume
Exit (permutation-safe close-only): trend flip (EMA50 crossing the other side of
EMA200) or the entry condition's RSI band breaking.

Squeeze longs are higher-conviction; ``squeeze_size_multiplier`` (1.5x) is exposed
so the live wiring can size them up. The position vector itself is unit-magnitude
(MCPT scores posture, not size).

Data requirement & fail-open
----------------------------
Needs a per-bar ``si_change`` column (fractional WoW change in short-volume ratio),
injected by ``discovery.data_feeds.finra_historical.attach_short_interest_change``
via the ``enrich`` hook below. With no column / no data the strategy returns an
all-flat vector (no trades) and never validates — it does NOT crash the pipeline.

Posture vector S in {+1 long, -1 short, 0 flat}, one value per bar.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

SI_COLUMN = "si_change"
_EMA_SHORT = 50
_EMA_LONG = 200
_SQUEEZE_PRICE_MOVE = 0.02      # squeeze long requires today's return > +2%
SQUEEZE_SIZE_MULTIPLIER = 1.5   # higher conviction on squeeze longs (live sizing)


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


class ShortInterestMomentumPositionStrategy:
    name = "short_interest_momentum"

    PARAM_GRID = {
        "short_increase_threshold": [0.10, 0.15, 0.20],
        "short_decrease_threshold": [0.15, 0.20, 0.25],
        "rsi_period": [10, 14],
        "volume_multiplier": [1.3, 1.5, 2.0],
    }

    def param_grid(self) -> list[dict]:
        return [
            {
                "short_increase_threshold": si,
                "short_decrease_threshold": sd,
                "rsi_period": rp,
                "volume_multiplier": vm,
            }
            for si, sd, rp, vm in itertools.product(
                self.PARAM_GRID["short_increase_threshold"],
                self.PARAM_GRID["short_decrease_threshold"],
                self.PARAM_GRID["rsi_period"],
                self.PARAM_GRID["volume_multiplier"],
            )
        ]

    @staticmethod
    def enrich(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Attach the per-bar ``si_change`` column (used by the engine)."""
        try:
            from discovery.data_feeds.finra_historical import attach_short_interest_change
            return attach_short_interest_change(bars, symbol)
        except Exception:
            import traceback
            print(f"[ShortMomo] enrich failed for {symbol}:\n{traceback.format_exc()}")
            return bars

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        n = len(df)
        pos = np.zeros(n, dtype=float)

        if SI_COLUMN not in df.columns:
            return pos  # documented fail-open: no short-interest feed → no trades

        inc_thresh = float(params["short_increase_threshold"])
        dec_thresh = float(params["short_decrease_threshold"])
        rsi_period = int(params["rsi_period"])
        vol_mult = float(params["volume_multiplier"])

        if n < _EMA_LONG + 2:
            return pos

        close = df["close"].to_numpy(dtype=float)
        si_change = df[SI_COLUMN].to_numpy(dtype=float)
        rsi = _compute_rsi(df["close"], rsi_period)
        ema50 = _compute_ema(df["close"], _EMA_SHORT)
        ema200 = _compute_ema(df["close"], _EMA_LONG)

        has_volume = "volume" in df.columns
        if has_volume:
            vol = df["volume"].to_numpy(dtype=float)
            vol_avg = pd.Series(vol).rolling(20).mean().to_numpy(dtype=float)
        else:
            vol = np.zeros(n)
            vol_avg = np.full(n, np.nan)

        state = 0.0
        for i in range(1, n):
            if not all(np.isfinite(v) for v in (rsi[i], ema50[i], ema200[i])):
                pos[i] = state
                continue

            if state == 0.0:
                # SHORT: short interest rising on a downtrend with momentum.
                if (si_change[i] > inc_thresh and rsi[i] > 55.0
                        and ema50[i] < ema200[i]):
                    state = -1.0
                else:
                    # LONG (squeeze): short interest falling sharply on an uptrend
                    # with a >2% up-day on above-average volume.
                    ret = (close[i] - close[i - 1]) / close[i - 1] if close[i - 1] > 0 else 0.0
                    vol_ok = (
                        has_volume and np.isfinite(vol_avg[i]) and vol_avg[i] > 0
                        and vol[i] > vol_mult * vol_avg[i]
                    )
                    if (si_change[i] < -dec_thresh and rsi[i] < 65.0
                            and ema50[i] > ema200[i] and ret > _SQUEEZE_PRICE_MOVE and vol_ok):
                        state = 1.0
            elif state == 1.0:
                # Exit long: trend flip or momentum exhaustion.
                if ema50[i] < ema200[i] or rsi[i] >= 80.0:
                    state = 0.0
            elif state == -1.0:
                # Exit short: trend flip or oversold bounce risk.
                if ema50[i] > ema200[i] or rsi[i] <= 30.0:
                    state = 0.0

            pos[i] = state

        return pos
