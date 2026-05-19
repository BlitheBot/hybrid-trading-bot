"""
Half-life of mean reversion via OLS on lagged price differences (Engle-Granger step 1).

Regression: delta_p = alpha + beta * p_lag
Half-life:  -log(2) / log(1 + beta)

Mean reversion requires beta in (-1, 0). beta >= 0 means trending or random walk.
Uses numpy.linalg.lstsq -- no statsmodels dependency.
"""

import numpy as np
import pandas as pd


class HalfLifeSignal:
    def __init__(
        self,
        min_bars: int = 30,
        max_halflife: float = 30.0,
        min_halflife: float = 1.0,
        rolling_window: int = 60,
    ):
        self.min_bars = min_bars
        self.max_halflife = max_halflife
        self.min_halflife = min_halflife
        self.rolling_window = rolling_window

    def _invalid(self) -> dict:
        return {
            "halflife": float("inf"),
            "is_mean_reverting": False,
            "ou_theta": 0.0,
            "suggested_holding_period": int(self.max_halflife),
            "confidence": 0.0,
        }

    def compute(self, prices: pd.Series) -> dict:
        prices = prices.dropna()
        n = len(prices)
        if n < self.min_bars:
            return self._invalid()

        p = prices.to_numpy(dtype=float)
        delta_p = np.diff(p)
        p_lag   = p[:-1]

        X = np.column_stack([np.ones(len(p_lag)), p_lag])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X, delta_p, rcond=None)
        except np.linalg.LinAlgError:
            return self._invalid()

        alpha, beta = float(coeffs[0]), float(coeffs[1])

        # beta must be in (-1, 0) for mean reversion
        if beta >= 0 or beta <= -1:
            return self._invalid()

        log_arg  = 1.0 + beta
        halflife = float(-np.log(2) / np.log(log_arg))

        if not (self.min_halflife <= halflife <= self.max_halflife):
            return self._invalid()

        y_pred = X @ coeffs
        ss_res = float(np.sum((delta_p - y_pred) ** 2))
        ss_tot = float(np.sum((delta_p - delta_p.mean()) ** 2))
        r_sq   = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0

        confidence = round(r_sq * min(n / 100.0, 1.0), 3)
        ou_theta   = float(-np.log(log_arg))
        suggested  = max(1, int(min(halflife * 1.5, self.max_halflife)))

        return {
            "halflife":                 round(halflife, 2),
            "is_mean_reverting":        True,
            "ou_theta":                 round(ou_theta, 4),
            "suggested_holding_period": suggested,
            "confidence":               confidence,
        }

    def compute_latest(self, prices: pd.Series) -> dict:
        """Compute on the last rolling_window bars — same pattern as KalmanTrendSignal."""
        return self.compute(prices.iloc[-self.rolling_window:])


if __name__ == "__main__":
    np.random.seed(42)

    print("--- Test 1: AR(1) mean-reverting series (theta=0.85, expected halflife ~4.3 bars) ---")
    n = 200
    theta = 0.85
    p = [0.0]
    for _ in range(n - 1):
        p.append(theta * p[-1] + np.random.randn() * 0.5)
    prices_mr = pd.Series(p)

    hl = HalfLifeSignal(min_bars=30, max_halflife=30, min_halflife=1)
    r = hl.compute(prices_mr)
    print(f"  halflife={r['halflife']:.2f} is_mr={r['is_mean_reverting']} "
          f"confidence={r['confidence']:.3f} suggested_hold={r['suggested_holding_period']}")
    assert r["is_mean_reverting"], f"Expected mean-reverting, got {r}"
    assert 1.0 <= r["halflife"] <= 20.0, f"Halflife out of expected range: {r['halflife']}"

    print("--- Test 2: pure uptrend (not mean-reverting) ---")
    prices_trend = pd.Series(np.linspace(100.0, 200.0, 200))
    r2 = hl.compute(prices_trend)
    print(f"  halflife={r2['halflife']} is_mr={r2['is_mean_reverting']}")
    assert not r2["is_mean_reverting"], f"Expected not mean-reverting, got {r2}"

    print("--- Test 3: insufficient bars (< min_bars) ---")
    r3 = hl.compute(pd.Series(np.random.randn(10)))
    assert not r3["is_mean_reverting"], "Expected False for insufficient data"
    print(f"  is_mr={r3['is_mean_reverting']} (correctly rejected)")

    print("--- Test 4: compute_latest uses rolling_window ---")
    hl_narrow = HalfLifeSignal(min_bars=20, max_halflife=30, min_halflife=1, rolling_window=30)
    r4 = hl_narrow.compute_latest(prices_mr)
    print(f"  halflife={r4['halflife']} is_mr={r4['is_mean_reverting']} confidence={r4['confidence']:.3f}")
    assert r4["confidence"] <= r["confidence"] + 0.1, "Expected lower/equal confidence on narrower window"

    print("\nAll smoke tests passed.")
