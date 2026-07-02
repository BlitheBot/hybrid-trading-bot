"""
Discovery family 7 — Short-Interest Momentum.

Position-vector adapter compatible with the permutation/regime validation pipeline
(``SwingPositionStrategy`` interface: ``name``, ``param_grid()``, ``position_vector()``).

REDESIGN NOTE (audit fix)
--------------------------
The original version used ``si_change`` — the week-over-week change in FINRA's
daily short-VOLUME ratio (ShortVolume / TotalVolume) — as the PRIMARY signal
for both legs. An audit found this measures order flow, not open short
positions, and does not match the short-interest anomaly documented in the
literature (Asquith/Pathak/Ritter 2005; Boehmer/Jones/Zhang 2008; Rapach/
Ringgenberg/Zhou 2016), which is measured on short interest as a fraction of
float and days-to-cover — both now available from the real biweekly FINRA
consolidated short-interest report (see
``discovery.data_feeds.finra_short_interest_levels``). This version:

  * Gates both legs on the real short-interest LEVEL (pct of float /
    days-to-cover), not the daily volume ratio.
  * Keeps the old daily-ratio feed wired in via ``enrich`` as an available
    column, but it no longer gates entry or exit anywhere in
    ``position_vector`` — secondary/informational only, per the audit finding
    that a falling ratio is (at best) exit confirmation, never an entry signal.
  * Fixes the SHORT leg's internal contradiction: the old code required
    ``RSI > 55`` (rising momentum) for a signal whose docstring called it
    "deteriorating technicals" — removed. Squeeze-continuation shorts now
    require bearish, not bullish, momentum.

Thesis
------
  * SQUEEZE LONG: short interest is genuinely elevated (>10% of float OR
    days-to-cover > 5) — the raw material for a squeeze exists — THEN a >2%
    up-day on >1.5x average volume triggers the entry (the squeeze actually
    starting, not already-finished covering).
  * SHORT (informed-short continuation): short interest is *rising*
    period-over-period on a stock already in a confirmed downtrend with
    bearish momentum (RSI < 45) — shorts are adding to a position the
    technicals agree with.

Exit (permutation-safe close-only, unchanged by the redesign): trend flip
(EMA50 crossing the other side of EMA200) or momentum exhaustion (RSI >= 80
for longs, RSI <= 30 for shorts).

Squeeze longs are higher-conviction; ``SQUEEZE_SIZE_MULTIPLIER`` (1.5x) is
exposed so live wiring can size them up. The position vector itself is
unit-magnitude (MCPT scores posture, not size).

Universe
--------
The documented edge lives in small/mid-cap names, not the megacap-heavy
top-250-by-dollar-volume universe the rest of the discovery engine screens.
``discovery_engine.py`` validates this family over its own dedicated universe
(``discovery.short_interest_universe`` — symbols with FINRA short-interest
data AND a $500M-$10B market cap) instead of the general per-symbol loop.

Data requirement & fail-open
-----------------------------
Needs per-bar ``si_pct_of_float`` / ``days_to_cover`` / ``si_level_rising``
columns from ``finra_short_interest_levels.attach_short_interest_level``
(primary) and ``si_change`` from ``finra_historical.attach_short_interest_change``
(secondary, informational). With neither feed available the strategy returns
an all-flat vector (no trades) and never validates — it does NOT crash the
pipeline.

Posture vector S in {+1 long, -1 short, 0 flat}, one value per bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from discovery.data_feeds.finra_short_interest_levels import (
    SI_LEVEL_PCT_COLUMN,
    DAYS_TO_COVER_COLUMN,
    SI_LEVEL_RISING_COLUMN,
)

# Legacy daily short-VOLUME ratio column (finra_historical.py) — still attached
# by `enrich` and available on the frame, but intentionally not read anywhere
# in position_vector(). Kept as a named constant for clarity/documentation.
SI_COLUMN = "si_change"

_EMA_SHORT = 50
_EMA_LONG = 200
_SQUEEZE_PRICE_MOVE = 0.02             # squeeze-long trigger: today's return > +2%
_SQUEEZE_VOLUME_MULT = 1.5             # squeeze-long trigger: volume > 1.5x 20-bar avg
_SQUEEZE_PCT_FLOAT_THRESHOLD = 0.10    # squeeze-long precondition: SI > 10% of float
_SQUEEZE_DAYS_TO_COVER_THRESHOLD = 5.0 # squeeze-long precondition (OR): days-to-cover > 5
_SHORT_RSI_MAX = 45.0                  # informed-short entry requires bearish momentum
SQUEEZE_SIZE_MULTIPLIER = 1.5          # higher conviction on squeeze longs (live sizing)


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

    # short_increase_threshold / short_decrease_threshold / volume_multiplier
    # were removed — they gated on si_change, which no longer participates in
    # entry/exit logic (see module docstring). rsi_period is the only
    # remaining swept parameter; the squeeze/short thresholds are now fixed
    # constants (_SQUEEZE_PCT_FLOAT_THRESHOLD etc.) per the redesign spec.
    PARAM_GRID = {
        "rsi_period": [10, 14, 21],
    }

    def param_grid(self) -> list[dict]:
        return [{"rsi_period": rp} for rp in self.PARAM_GRID["rsi_period"]]

    @staticmethod
    def enrich(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """
        Attach both short-interest feeds:
          * LEVEL data (si_pct_of_float, days_to_cover, si_level_rising) from
            the real FINRA biweekly consolidated short-interest report — the
            PRIMARY signal for both legs.
          * The legacy daily short-VOLUME ratio (si_change) — kept available
            as a secondary/confirmation column; position_vector() does not
            gate on it.
        Each enrichment fails open independently (network/DB error -> that
        source's columns are simply absent/NaN, family degrades gracefully).
        """
        import traceback
        out = bars
        try:
            from discovery.data_feeds.finra_short_interest_levels import attach_short_interest_level
            out = attach_short_interest_level(out, symbol)
        except Exception:
            print(f"[ShortMomo] LEVEL enrich failed for {symbol}:\n{traceback.format_exc()}")
        try:
            from discovery.data_feeds.finra_historical import attach_short_interest_change
            out = attach_short_interest_change(out, symbol)
        except Exception:
            print(f"[ShortMomo] daily-ratio enrich failed for {symbol}:\n{traceback.format_exc()}")
        return out

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        n = len(df)
        pos = np.zeros(n, dtype=float)

        if SI_LEVEL_PCT_COLUMN not in df.columns:
            return pos  # documented fail-open: no short-interest LEVEL feed → no trades

        rsi_period = int(params["rsi_period"])
        if n < _EMA_LONG + 2:
            return pos

        close = df["close"].to_numpy(dtype=float)
        rsi = _compute_rsi(df["close"], rsi_period)
        ema50 = _compute_ema(df["close"], _EMA_SHORT)
        ema200 = _compute_ema(df["close"], _EMA_LONG)

        si_pct_of_float = df[SI_LEVEL_PCT_COLUMN].to_numpy(dtype=float)
        days_to_cover = (
            df[DAYS_TO_COVER_COLUMN].to_numpy(dtype=float)
            if DAYS_TO_COVER_COLUMN in df.columns else np.full(n, np.nan)
        )
        si_level_rising = (
            df[SI_LEVEL_RISING_COLUMN].to_numpy(dtype=float)
            if SI_LEVEL_RISING_COLUMN in df.columns else np.zeros(n)
        )

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
                # SQUEEZE LONG — precondition: short interest is genuinely
                # elevated (level, not a daily-ratio delta). Trigger: a real
                # up-move on above-average volume, i.e. the squeeze starting.
                squeeze_precondition = (
                    (np.isfinite(si_pct_of_float[i]) and si_pct_of_float[i] > _SQUEEZE_PCT_FLOAT_THRESHOLD)
                    or (np.isfinite(days_to_cover[i]) and days_to_cover[i] > _SQUEEZE_DAYS_TO_COVER_THRESHOLD)
                )
                if squeeze_precondition:
                    ret = (close[i] - close[i - 1]) / close[i - 1] if close[i - 1] > 0 else 0.0
                    vol_ok = (
                        has_volume and np.isfinite(vol_avg[i]) and vol_avg[i] > 0
                        and vol[i] > _SQUEEZE_VOLUME_MULT * vol_avg[i]
                    )
                    if ret > _SQUEEZE_PRICE_MOVE and vol_ok:
                        state = 1.0

                if state == 0.0:
                    # INFORMED-SHORT CONTINUATION — short interest rising
                    # period-over-period, confirmed by bearish momentum
                    # (RSI < 45, not the old contradictory RSI > 55) and an
                    # established downtrend.
                    if si_level_rising[i] > 0 and rsi[i] < _SHORT_RSI_MAX and ema50[i] < ema200[i]:
                        state = -1.0

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
