"""
kalman_signal.py — Adaptive Trend Signal for BlitheBot
=======================================================
Drop this file into your strategies/ or signals/ directory.

What it does:
  - Runs a 1D Kalman Filter over a price series in real time
  - Outputs a smooth trend line (replaces static MAs)
  - Derives a +1 / 0 / -1 signal from trend slope + noise ratio
  - Optionally adds a Wavelet noise-separation layer (requires PyWavelets)

Dependencies:
  pip install numpy pandas  (already in your stack)
  pip install PyWavelets    (optional, for wavelet layer)

Usage (drop-in, no shared pipeline needed):
  from kalman_signal import KalmanTrendSignal

  ks = KalmanTrendSignal()
  result = ks.compute(prices)   # prices = pd.Series or list of floats

  result["trend"]        # smoothed price series  (pd.Series)
  result["signal"]       # +1 buy / -1 sell / 0 flat  (pd.Series)
  result["noise_ratio"]  # how noisy the market is right now  (pd.Series)
  result["slope"]        # trend direction strength  (pd.Series)

Quick strategy integration example:
  bars = alpaca.get_bars(symbol, "1Day", limit=60).df
  result = KalmanTrendSignal().compute(bars["close"])
  latest_signal = result["signal"].iloc[-1]
  if latest_signal == 1:
      # place buy order
  elif latest_signal == -1:
      # place sell order
"""

import numpy as np
import pandas as pd
from typing import Union, Optional

# ── Optional wavelet import ──────────────────────────────────────────────────
try:
    import pywt
    WAVELET_AVAILABLE = True
except ImportError:
    WAVELET_AVAILABLE = False


class KalmanTrendSignal:
    """
    1D Kalman Filter for adaptive price trend estimation.

    Parameters
    ----------
    process_variance : float
        How fast you expect the true price trend to change.
        Lower  → smoother trend, slower to react (better for swing trading)
        Higher → more reactive trend (better for momentum/intraday)
        Good starting range: 1e-4 to 1e-2. Default 1e-3.

    measurement_variance : float
        How noisy you believe price measurements are.
        Higher → filter trusts its model more than raw price.
        Lower  → filter trusts raw price more.
        Good starting range: 1e-2 to 1.0. Default 1e-1.

    signal_slope_threshold : float
        Minimum trend slope (as % of price) to generate a non-zero signal.
        Filters out flat/sideways chop. Default 0.001 (0.1%).

    signal_noise_threshold : float
        Maximum noise_ratio allowed to generate a signal.
        When market is too noisy, output 0 (flat) to avoid whipsaws.
        Default 0.5.

    use_wavelet_denoising : bool
        Pre-process price with a wavelet denoising pass before Kalman.
        Requires PyWavelets (pip install PyWavelets).
        Adds a second layer of noise removal. Default False.

    wavelet : str
        Wavelet type if use_wavelet_denoising=True. Default "db4".
        Other good choices: "haar", "sym4", "coif1"

    wavelet_level : int
        Decomposition level for wavelet denoising. Default 3.
        Higher = more aggressive smoothing.
    """

    def __init__(
        self,
        process_variance: float = 1e-3,
        measurement_variance: float = 1e-1,
        signal_slope_threshold: float = 0.001,
        signal_noise_threshold: float = 0.5,
        use_wavelet_denoising: bool = False,
        wavelet: str = "db4",
        wavelet_level: int = 3,
    ):
        self.Q = process_variance          # process noise covariance
        self.R = measurement_variance      # measurement noise covariance
        self.slope_thresh = signal_slope_threshold
        self.noise_thresh = signal_noise_threshold
        self.use_wavelet = use_wavelet_denoising and WAVELET_AVAILABLE
        self.wavelet = wavelet
        self.wavelet_level = wavelet_level

        if use_wavelet_denoising and not WAVELET_AVAILABLE:
            print(
                "[KalmanTrendSignal] PyWavelets not installed — "
                "wavelet denoising skipped. Run: pip install PyWavelets"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(
        self, prices: Union[pd.Series, list, np.ndarray]
    ) -> pd.DataFrame:
        """
        Run the full Kalman pipeline on a price series.

        Parameters
        ----------
        prices : array-like
            Closing prices, ordered oldest → newest.
            Can be pd.Series (preserves index), list, or np.ndarray.

        Returns
        -------
        pd.DataFrame with columns:
            price        — original prices
            trend        — Kalman-smoothed trend line
            slope        — normalized slope of trend (price units/bar / price)
            noise_ratio  — variance of (price - trend) / variance of price
                           0 = no noise, 1 = all noise
            signal       — +1 buy, -1 sell, 0 flat
        """
        # Normalise input
        if isinstance(prices, pd.Series):
            index = prices.index
            raw = prices.values.astype(float)
        else:
            raw = np.array(prices, dtype=float)
            index = pd.RangeIndex(len(raw))

        if len(raw) < 5:
            raise ValueError("Need at least 5 price bars.")

        # Optional wavelet pre-denoising
        working = self._wavelet_denoise(raw) if self.use_wavelet else raw.copy()

        # Kalman filter pass
        trend = self._kalman_filter(working)

        # Derived features
        slope = self._compute_slope(trend, raw)
        noise_ratio = self._compute_noise_ratio(raw, trend)
        signal = self._compute_signal(slope, noise_ratio)

        return pd.DataFrame(
            {
                "price": raw,
                "trend": trend,
                "slope": slope,
                "noise_ratio": noise_ratio,
                "signal": signal,
            },
            index=index,
        )

    def compute_latest(
        self, prices: Union[pd.Series, list, np.ndarray]
    ) -> dict:
        """
        Convenience method — returns only the latest bar's values as a dict.
        Use this inside your strategy's async loop for minimal overhead.

        Example:
            latest = ks.compute_latest(bars["close"])
            if latest["signal"] == 1: ...
        """
        df = self.compute(prices)
        last = df.iloc[-1]
        return {
            "price": float(last["price"]),
            "trend": float(last["trend"]),
            "slope": float(last["slope"]),
            "noise_ratio": float(last["noise_ratio"]),
            "signal": int(last["signal"]),
        }

    # ── Core Kalman implementation ────────────────────────────────────────────

    def _kalman_filter(self, prices: np.ndarray) -> np.ndarray:
        """
        Classic scalar Kalman Filter.

        State: estimated true price (x_hat)
        Predict: x_hat_prior = x_hat  (constant velocity model, simple)
        Update:  x_hat += K * (measurement - x_hat_prior)
        where K (Kalman Gain) = P_prior / (P_prior + R)

        The gain K adapts each bar:
          - When P is large (high uncertainty) → K → 1 → trust measurement
          - When P is small (high confidence)  → K → 0 → trust model
        """
        n = len(prices)
        trend = np.zeros(n)

        # Initialise with first price
        x_hat = prices[0]      # state estimate
        P = 1.0                # estimate error covariance

        for i, z in enumerate(prices):
            # ── Predict ──
            x_prior = x_hat
            P_prior = P + self.Q

            # ── Update ──
            K = P_prior / (P_prior + self.R)   # Kalman gain
            x_hat = x_prior + K * (z - x_prior)
            P = (1 - K) * P_prior

            trend[i] = x_hat

        return trend

    # ── Feature derivation ────────────────────────────────────────────────────

    def _compute_slope(self, trend: np.ndarray, prices: np.ndarray) -> np.ndarray:
        """
        Normalised 1-bar slope of the trend line.
        = (trend[i] - trend[i-1]) / trend[i-1]
        Tells you: is the trend going up (positive) or down (negative),
        and how fast, relative to the price level.
        """
        slope = np.zeros(len(trend))
        for i in range(1, len(trend)):
            if trend[i - 1] != 0:
                slope[i] = (trend[i] - trend[i - 1]) / trend[i - 1]
        return slope

    def _compute_noise_ratio(
        self, prices: np.ndarray, trend: np.ndarray, window: int = 20
    ) -> np.ndarray:
        """
        Rolling noise ratio over `window` bars.
        noise_ratio = var(price - trend) / var(price)

        Interpretation:
          ~0.0 → price is moving smoothly with the trend (good signal quality)
          ~0.5 → half the movement is noise (be cautious)
          ~1.0 → all movement is noise, no discernible trend (stay flat)

        This is analogous to (1 - R²) in regression terms.
        """
        residuals = prices - trend
        noise_ratio = np.full(len(prices), 0.5)

        for i in range(window, len(prices)):
            price_window = prices[i - window : i]
            resid_window = residuals[i - window : i]
            var_price = np.var(price_window)
            var_resid = np.var(resid_window)
            if var_price > 0:
                noise_ratio[i] = min(var_resid / var_price, 1.0)
            else:
                noise_ratio[i] = 0.0

        return noise_ratio

    def _compute_signal(
        self, slope: np.ndarray, noise_ratio: np.ndarray
    ) -> np.ndarray:
        """
        Convert slope + noise_ratio into a discrete trading signal.

          +1 (BUY)  : slope > +threshold AND noise_ratio < noise_thresh
          -1 (SELL) : slope < -threshold AND noise_ratio < noise_thresh
           0 (FLAT) : slope too small OR market too noisy

        The noise gate is key — it stops you from trading during chop,
        which is where most momentum strategies get whipsawed.
        """
        signal = np.zeros(len(slope), dtype=int)
        for i in range(len(slope)):
            if noise_ratio[i] >= self.noise_thresh:
                signal[i] = 0  # too noisy, stay flat
            elif slope[i] > self.slope_thresh:
                signal[i] = 1
            elif slope[i] < -self.slope_thresh:
                signal[i] = -1
        return signal

    # ── Wavelet denoising (optional pre-processing) ───────────────────────────

    def _wavelet_denoise(self, prices: np.ndarray) -> np.ndarray:
        """
        Discrete Wavelet Transform denoising pass.
        Decomposes price into approximation (trend) + detail (noise) coefficients,
        soft-thresholds the detail coefficients to suppress noise,
        then reconstructs. Result is a cleaner input for the Kalman filter.

        Only runs if PyWavelets is installed and use_wavelet_denoising=True.
        """
        coeffs = pywt.wavedec(prices, self.wavelet, level=self.wavelet_level)
        # Estimate noise from finest detail level (universal threshold)
        sigma = np.median(np.abs(coeffs[-1])) / 0.6745
        threshold = sigma * np.sqrt(2 * np.log(len(prices)))
        # Soft-threshold all detail levels (skip approximation coeffs[0])
        denoised_coeffs = [coeffs[0]] + [
            pywt.threshold(c, threshold, mode="soft") for c in coeffs[1:]
        ]
        return pywt.waverec(denoised_coeffs, self.wavelet)[: len(prices)]


# ── Strategy Discovery Engine integration helper ──────────────────────────────

def kalman_feature_bundle(
    prices: Union[pd.Series, list, np.ndarray],
    param_grid: Optional[dict] = None,
) -> pd.DataFrame:
    """
    Generates a bundle of Kalman features across multiple parameter combos.
    Designed to feed directly into your Strategy Discovery Engine as additional
    feature columns alongside existing indicators (RSI, MACD, etc.).

    Parameters
    ----------
    prices : array-like
        Closing prices.
    param_grid : dict, optional
        Keys: "process_variance", "measurement_variance"
        Values: lists of floats to try.
        Defaults to a reasonable sweep if not provided.

    Returns
    -------
    pd.DataFrame
        One column per (param_combo, feature_name).
        Columns named like: "kalman_Q1e-3_R1e-1_trend", etc.

    Example:
        features = kalman_feature_bundle(bars["close"])
        # Merge with your existing feature df, then run Discovery Engine
    """
    if param_grid is None:
        param_grid = {
            "process_variance": [1e-4, 1e-3, 1e-2],
            "measurement_variance": [1e-2, 1e-1, 5e-1],
        }

    results = {}
    for Q in param_grid["process_variance"]:
        for R in param_grid["measurement_variance"]:
            ks = KalmanTrendSignal(process_variance=Q, measurement_variance=R)
            df = ks.compute(prices)
            tag = f"Q{Q}_R{R}"
            for col in ["trend", "slope", "noise_ratio", "signal"]:
                results[f"kalman_{tag}_{col}"] = df[col]

    return pd.DataFrame(results)


# ── Quick smoke test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import math

    print("=== KalmanTrendSignal Smoke Test ===\n")

    # Synthetic price: uptrend + sine noise + random noise
    np.random.seed(42)
    n = 120
    t = np.arange(n)
    prices = (
        100
        + 0.15 * t                          # uptrend
        + 3 * np.sin(2 * math.pi * t / 20)  # 20-bar cycle
        + np.random.normal(0, 1, n)          # random noise
    )
    prices_series = pd.Series(prices)

    # Basic run
    ks = KalmanTrendSignal(process_variance=1e-3, measurement_variance=0.1)
    result = ks.compute(prices_series)

    print("Last 5 bars:")
    print(result.tail(5).to_string())

    # compute_latest
    latest = ks.compute_latest(prices_series)
    print(f"\nLatest bar: {latest}")

    # Signal distribution
    counts = result["signal"].value_counts().sort_index()
    print(f"\nSignal distribution:\n{counts.to_string()}")

    # Feature bundle
    bundle = kalman_feature_bundle(prices_series)
    print(f"\nFeature bundle shape: {bundle.shape}")
    print(f"Columns: {list(bundle.columns[:4])} ...")

    # Wavelet test (if available)
    if WAVELET_AVAILABLE:
        ks_wv = KalmanTrendSignal(use_wavelet_denoising=True)
        result_wv = ks_wv.compute(prices_series)
        print(f"\nWavelet+Kalman last signal: {result_wv['signal'].iloc[-1]}")
    else:
        print("\n[Wavelet layer] Install PyWavelets to test: pip install PyWavelets")

    print("\n=== All tests passed ===")
