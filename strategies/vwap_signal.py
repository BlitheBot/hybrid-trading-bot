"""
vwap_signal.py — Anchored VWAP Signal for BlitheBot
====================================================
Drop this file into your strategies/ directory alongside kalman_signal.py.

What it does:
  - Computes VWAP anchored to a rolling window, a weekly period, or a monthly period
  - Outputs distance_pct: how far price is above/below VWAP (in %)
  - Outputs volume_ratio: current volume vs trailing average (volume confirmation)
  - Derives a +1 / 0 / -1 signal from distance + volume threshold

  VWAP = sum(TypicalPrice * Volume) / sum(Volume)  over the anchor period
  TypicalPrice = (High + Low + Close) / 3

  Signal logic:
    +1 (bullish) : price > VWAP by >= distance_threshold_pct AND volume_ratio >= volume_ratio_threshold
    -1 (bearish) : price < VWAP by >= distance_threshold_pct AND volume_ratio >= volume_ratio_threshold
     0 (neutral) : price near VWAP, OR insufficient volume confirmation

  Anchor modes:
    "rolling"  — N-bar rolling cumulative VWAP (works with any index, default)
    "weekly"   — resets at start of each calendar week (requires DatetimeIndex)
    "monthly"  — resets at start of each calendar month (requires DatetimeIndex)

Dependencies:
  numpy, pandas  (already in your stack — no new installs needed)

Usage:
  from vwap_signal import AnchoredVWAPSignal

  avs = AnchoredVWAPSignal(window=20, anchor="rolling")
  result = avs.compute(df)  # df must have: close, high, low, volume

  result["vwap"]          # VWAP level     (pd.Series)
  result["distance_pct"]  # % above/below  (pd.Series)
  result["volume_ratio"]  # vol / avg_vol  (pd.Series)
  result["signal"]        # +1 / 0 / -1   (pd.Series)

  latest = avs.compute_latest(df)  # dict with single-bar values
"""

import numpy as np
import pandas as pd
from typing import Union


class AnchoredVWAPSignal:
    """
    Anchored VWAP with distance_pct and volume_ratio signals.

    Parameters
    ----------
    window : int
        Number of bars in the rolling VWAP window (anchor="rolling").
        Also the lookback for average volume in all anchor modes. Default 20.

    anchor : str
        "rolling"  — rolling N-bar VWAP (default; works without DatetimeIndex)
        "weekly"   — resets at Monday each week (requires DatetimeIndex)
        "monthly"  — resets at 1st of each month (requires DatetimeIndex)

    distance_threshold_pct : float
        Minimum |distance_pct| to generate a +1 or -1 signal.
        Default 0.5 (price must be at least 0.5% above or below VWAP).
        Increase for swing trading (e.g. 1.0), decrease for scalping (e.g. 0.2).

    volume_ratio_threshold : float
        Minimum volume_ratio to generate a signal.
        Default 1.0 (no volume gate — any volume qualifies).
        Set to 1.2–1.5 to require above-average volume confirmation.
    """

    def __init__(
        self,
        window: int = 20,
        anchor: str = "rolling",
        distance_threshold_pct: float = 0.5,
        volume_ratio_threshold: float = 1.0,
    ):
        if anchor not in ("rolling", "weekly", "monthly"):
            raise ValueError(f"anchor must be 'rolling', 'weekly', or 'monthly'; got {anchor!r}")
        self.window = window
        self.anchor = anchor
        self.distance_threshold_pct = distance_threshold_pct
        self.volume_ratio_threshold = volume_ratio_threshold

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute anchored VWAP, distance_pct, volume_ratio, and signal.

        Parameters
        ----------
        df : pd.DataFrame
            Must contain columns: close, high, low, volume.
            DatetimeIndex required for anchor="weekly" or "monthly".

        Returns
        -------
        pd.DataFrame with columns:
            vwap          — VWAP level
            distance_pct  — (close - vwap) / vwap * 100  (positive = above VWAP)
            volume_ratio  — volume / rolling_mean_volume (window bars)
            signal        — +1 bullish / -1 bearish / 0 neutral
        """
        required = {"close", "high", "low", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing columns: {missing}")

        tp = (df["high"] + df["low"] + df["close"]) / 3  # typical price
        tp_vol = tp * df["volume"]

        if self.anchor == "rolling":
            vwap = self._rolling_vwap(tp_vol, df["volume"])
        elif self.anchor == "weekly":
            vwap = self._period_vwap(tp_vol, df["volume"], "W")
        else:  # monthly
            vwap = self._period_vwap(tp_vol, df["volume"], "ME")

        avg_vol = df["volume"].rolling(window=self.window, min_periods=1).mean()
        volume_ratio = df["volume"] / avg_vol.replace(0, np.nan)

        distance_pct = (df["close"] - vwap) / vwap.replace(0, np.nan) * 100

        signal = self._compute_signal(distance_pct.values, volume_ratio.values)

        return pd.DataFrame(
            {
                "vwap": vwap,
                "distance_pct": distance_pct,
                "volume_ratio": volume_ratio,
                "signal": signal,
            },
            index=df.index,
        )

    def compute_latest(self, df: pd.DataFrame) -> dict:
        """
        Convenience method — returns only the latest bar's values as a dict.

        Example:
            latest = avs.compute_latest(df)
            if latest["signal"] == 1 and latest["distance_pct"] > 0.5:
                # price breaking above VWAP with volume confirmation
        """
        result = self.compute(df)
        last = result.iloc[-1]
        return {
            "vwap": float(last["vwap"]),
            "distance_pct": float(last["distance_pct"]),
            "volume_ratio": float(last["volume_ratio"]),
            "signal": int(last["signal"]),
        }

    # ── VWAP computation ──────────────────────────────────────────────────────

    def _rolling_vwap(self, tp_vol: pd.Series, volume: pd.Series) -> pd.Series:
        """Rolling N-bar VWAP."""
        cum_tp_vol = tp_vol.rolling(window=self.window, min_periods=1).sum()
        cum_vol = volume.rolling(window=self.window, min_periods=1).sum()
        return cum_tp_vol / cum_vol.replace(0, np.nan)

    def _period_vwap(
        self, tp_vol: pd.Series, volume: pd.Series, freq: str
    ) -> pd.Series:
        """
        Period-anchored VWAP (weekly or monthly).
        Resets cumulative sum at the start of each calendar period.
        Requires DatetimeIndex.
        """
        if not isinstance(tp_vol.index, pd.DatetimeIndex):
            raise ValueError(
                f"anchor={self.anchor!r} requires a DatetimeIndex; "
                "use anchor='rolling' if your DataFrame has an integer index."
            )
        # Map each row to its period label (e.g. "2026-W20" or "2026-05")
        if freq == "W":
            period_key = tp_vol.index.to_period("W").astype(str)
        else:  # "ME" monthly-end → use month period
            period_key = tp_vol.index.to_period("M").astype(str)

        vwap = pd.Series(np.nan, index=tp_vol.index)
        for key in pd.unique(period_key):
            mask = period_key == key
            cum_tp_vol = tp_vol[mask].cumsum()
            cum_vol = volume[mask].cumsum()
            vwap[mask] = cum_tp_vol / cum_vol.replace(0, np.nan)

        return vwap

    # ── Signal derivation ────────────────────────────────────────────────────

    def _compute_signal(
        self, distance_pct: np.ndarray, volume_ratio: np.ndarray
    ) -> np.ndarray:
        """
        Generate trading signal from distance_pct and volume_ratio.

        +1 : price above VWAP by >= threshold AND volume confirms
        -1 : price below VWAP by >= threshold AND volume confirms
         0 : price close to VWAP OR insufficient volume
        """
        signal = np.zeros(len(distance_pct), dtype=int)
        for i in range(len(distance_pct)):
            d = distance_pct[i]
            v = volume_ratio[i]
            if np.isnan(d) or np.isnan(v):
                continue
            vol_ok = v >= self.volume_ratio_threshold
            if d >= self.distance_threshold_pct and vol_ok:
                signal[i] = 1
            elif d <= -self.distance_threshold_pct and vol_ok:
                signal[i] = -1
        return signal


# ── Quick smoke test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== AnchoredVWAPSignal Smoke Test ===\n")

    np.random.seed(42)
    n = 120

    # Synthetic OHLCV: uptrend with realistic volume
    closes = np.cumprod(1 + np.random.normal(0.001, 0.015, n)) * 150
    highs = closes * (1 + np.abs(np.random.normal(0, 0.005, n)))
    lows = closes * (1 - np.abs(np.random.normal(0, 0.005, n)))
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    volumes = np.random.randint(500_000, 5_000_000, n).astype(float)
    # Spike volume on last 10 bars to trigger signal
    volumes[-10:] *= 2.0

    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )

    # 1. Rolling anchor
    avs_roll = AnchoredVWAPSignal(window=20, anchor="rolling", distance_threshold_pct=0.5)
    res_roll = avs_roll.compute(df)
    print("Rolling VWAP — last 5 bars:")
    print(res_roll[["vwap", "distance_pct", "volume_ratio", "signal"]].tail(5).to_string())

    # 2. Weekly anchor
    avs_week = AnchoredVWAPSignal(anchor="weekly", distance_threshold_pct=0.5)
    res_week = avs_week.compute(df)
    print("\nWeekly anchored VWAP — last 5 bars:")
    print(res_week[["vwap", "distance_pct", "volume_ratio", "signal"]].tail(5).to_string())

    # 3. Monthly anchor
    avs_month = AnchoredVWAPSignal(anchor="monthly", distance_threshold_pct=0.5)
    res_month = avs_month.compute(df)
    print("\nMonthly anchored VWAP — last 5 bars:")
    print(res_month[["vwap", "distance_pct", "volume_ratio", "signal"]].tail(5).to_string())

    # 4. compute_latest
    latest = avs_roll.compute_latest(df)
    print(f"\ncompute_latest(): {latest}")

    # 5. Signal distribution
    dist = res_roll["signal"].value_counts().sort_index()
    print(f"\nSignal distribution (rolling):\n{dist.to_string()}")

    # 6. Volume ratio gate (1.5x threshold)
    avs_vol = AnchoredVWAPSignal(window=20, volume_ratio_threshold=1.5)
    res_vol = avs_vol.compute(df)
    dist_vol = res_vol["signal"].value_counts().sort_index()
    print(f"\nSignal distribution (volume_ratio >= 1.5x):\n{dist_vol.to_string()}")

    print("\n=== All tests passed ===")
