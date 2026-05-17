"""
hurst_signal.py — Hurst Exponent Regime Detector for BlitheBot
===============================================================
Drop this file into your strategies/ directory alongside kalman_signal.py.

What it does:
  - Estimates the Hurst exponent H via Rescaled Range (R/S) analysis
  - Rolling window version: recomputes H at every bar
  - Classifies the current market regime:
      +1  trending      H > 0.6  — price has long-term memory, trend-following works
       0  random walk   0.4 <= H <= 0.6  — no persistent edge
      -1  mean-reverting H < 0.4  — price reverts; mean-reversion strategies work

  Hurst quick-reference:
    H ≈ 0.5  → geometric Brownian motion (random walk, efficient market)
    H > 0.5  → persistent trend — past up moves make future up moves more likely
    H < 0.5  → anti-persistent — past up moves make future down moves more likely

Dependencies:
  numpy, pandas  (already in your stack — no new installs needed)

Usage:
  from hurst_signal import HurstSignal

  hs = HurstSignal()
  result = hs.compute(prices)   # prices = pd.Series or list of floats

  result["hurst"]        # rolling H values  (pd.Series)
  result["regime_code"]  # +1 / 0 / -1       (pd.Series)
  result["regime"]       # human-readable label (pd.Series)

  latest = hs.compute_latest(prices)  # dict with single-bar values
"""

import numpy as np
import pandas as pd
from typing import Union


class HurstSignal:
    """
    Rolling Hurst Exponent via Rescaled Range (R/S) Analysis.

    Parameters
    ----------
    rolling_window : int
        Number of bars over which each H estimate is computed.
        Minimum useful value ~60 for daily bars; 100 is the default.
        Bars before this window are initialised to H=0.5 (random walk).

    min_lag : int
        Smallest lag used in R/S regression. Must be >= 5.
        Default 10 (10-bar minimum block size).

    trending_threshold : float
        H above this is classified as trending. Default 0.6.

    mean_reverting_threshold : float
        H below this is classified as mean-reverting. Default 0.4.
    """

    def __init__(
        self,
        rolling_window: int = 100,
        min_lag: int = 10,
        trending_threshold: float = 0.6,
        mean_reverting_threshold: float = 0.4,
    ):
        if rolling_window < min_lag * 2:
            raise ValueError(
                f"rolling_window ({rolling_window}) must be >= 2 * min_lag ({min_lag * 2})"
            )
        self.rolling_window = rolling_window
        self.min_lag = min_lag
        self.trending_threshold = trending_threshold
        self.mean_reverting_threshold = mean_reverting_threshold

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(
        self, prices: Union[pd.Series, list, np.ndarray]
    ) -> pd.DataFrame:
        """
        Compute rolling Hurst exponent and regime classification.

        Parameters
        ----------
        prices : array-like
            Closing prices, ordered oldest → newest.

        Returns
        -------
        pd.DataFrame with columns:
            hurst        — rolling Hurst exponent in [0, 1]; 0.5 during warmup
            regime_code  — +1 trending / 0 random / -1 mean-reverting
            regime       — human-readable label string
        """
        if isinstance(prices, pd.Series):
            index = prices.index
            raw = prices.values.astype(float)
        else:
            raw = np.array(prices, dtype=float)
            index = pd.RangeIndex(len(raw))

        n = len(raw)
        hurst_arr = np.full(n, 0.5)

        for i in range(self.rolling_window, n):
            window = raw[i - self.rolling_window : i + 1]
            hurst_arr[i] = self._rs_hurst(window)

        regime_codes = np.zeros(n, dtype=int)
        for i in range(n):
            h = hurst_arr[i]
            if h > self.trending_threshold:
                regime_codes[i] = 1
            elif h < self.mean_reverting_threshold:
                regime_codes[i] = -1

        _labels = {1: "trending", 0: "random_walk", -1: "mean_reverting"}
        regimes = [_labels[c] for c in regime_codes]

        return pd.DataFrame(
            {"hurst": hurst_arr, "regime_code": regime_codes, "regime": regimes},
            index=index,
        )

    def compute_latest(
        self, prices: Union[pd.Series, list, np.ndarray]
    ) -> dict:
        """
        Convenience method — returns only the latest bar's values as a dict.

        Example:
            latest = hs.compute_latest(bars["close"])
            if latest["regime_code"] == 1:
                # market is trending — use trend-following strategies
        """
        df = self.compute(prices)
        last = df.iloc[-1]
        return {
            "hurst": float(last["hurst"]),
            "regime_code": int(last["regime_code"]),
            "regime": str(last["regime"]),
        }

    # ── R/S implementation ────────────────────────────────────────────────────

    def _rs_hurst(self, prices: np.ndarray) -> float:
        """
        Compute H for a single price window using R/S analysis.

        Method:
          1. Convert prices to log returns.
          2. For each lag value, split log-return series into non-overlapping
             blocks of that length, compute R/S for each block, average them.
          3. OLS regression of log(mean_RS) vs log(lag) → slope = H.

        Returns H in [0, 1], or 0.5 on any failure (conservative fallback).
        """
        log_ret = np.diff(np.log(prices))
        n = len(log_ret)
        if n < self.min_lag * 2:
            return 0.5

        max_lag = n // 2
        if max_lag <= self.min_lag:
            return 0.5

        # 6 lags spaced logarithmically between min_lag and max_lag
        raw_lags = np.logspace(
            np.log10(self.min_lag), np.log10(max_lag), num=6
        ).astype(int)
        lags = sorted(set(raw_lags.tolist()))

        valid_lags, rs_means = [], []
        for lag in lags:
            if lag < 2:
                continue
            block_rs = []
            for start in range(0, n - lag + 1, lag):
                block = log_ret[start : start + lag]
                if len(block) < lag:
                    continue
                mean = np.mean(block)
                dev = np.cumsum(block - mean)
                R = dev.max() - dev.min()
                S = np.std(block, ddof=1)
                if S > 0 and R > 0:
                    block_rs.append(R / S)
            if block_rs:
                rs_means.append(np.mean(block_rs))
                valid_lags.append(lag)

        if len(valid_lags) < 2:
            return 0.5

        try:
            log_lags = np.log(valid_lags)
            log_rs = np.log(rs_means)
            H = np.polyfit(log_lags, log_rs, 1)[0]
            return float(np.clip(H, 0.0, 1.0))
        except Exception:
            return 0.5


# ── Quick smoke test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import math

    print("=== HurstSignal Smoke Test ===\n")
    np.random.seed(42)
    n = 250  # ~1 year of daily bars

    # 1. Trending series (GBM with strong drift)
    trending = np.cumprod(1 + np.random.normal(0.0015, 0.01, n)) * 100
    hs = HurstSignal(rolling_window=100)
    res_trend = hs.compute(pd.Series(trending))
    latest_trend = res_trend.iloc[-1]
    print(f"Trending series  — H={latest_trend['hurst']:.3f}  regime={latest_trend['regime']}")

    # 2. Mean-reverting series (Ornstein-Uhlenbeck)
    theta, mu, sigma = 0.15, 100.0, 1.0
    ou = [mu]
    for _ in range(n - 1):
        ou.append(ou[-1] + theta * (mu - ou[-1]) + sigma * np.random.normal())
    res_mr = hs.compute(pd.Series(ou))
    latest_mr = res_mr.iloc[-1]
    print(f"Mean-reverting   — H={latest_mr['hurst']:.3f}  regime={latest_mr['regime']}")

    # 3. Pure random walk
    rw = np.cumprod(1 + np.random.normal(0, 0.01, n)) * 100
    res_rw = hs.compute(pd.Series(rw))
    latest_rw = res_rw.iloc[-1]
    print(f"Random walk      — H={latest_rw['hurst']:.3f}  regime={latest_rw['regime']}")

    # 4. compute_latest
    latest = hs.compute_latest(pd.Series(trending))
    print(f"\ncompute_latest(): {latest}")

    # 5. Regime distribution on trending series
    dist = res_trend["regime"].value_counts()
    print(f"\nRegime distribution (trending series, last 250 bars):\n{dist.to_string()}")

    print("\n=== All tests passed ===")
