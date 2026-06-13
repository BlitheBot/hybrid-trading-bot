"""
Unit tests for the market regime classifier.

Verifies each of the four regimes is correctly identified on synthetic SPY/VIX
data, that HIGH_VOL overrides trend, and that VIX-missing input falls back to
SPY-only classification.

Run directly:   python discovery/test_regime_classifier.py
Or via pytest:  pytest discovery/test_regime_classifier.py
"""
import numpy as np
import pandas as pd

from discovery.regime_classifier import (
    BEAR_TREND,
    BULL_TREND,
    CHOPPY,
    HIGH_VOL,
    classify_regime,
)


def _bars_from_prices(prices: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=len(prices), freq="B")
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.005,
            "low": prices * 0.995,
            "close": prices,
            "volume": np.full(len(prices), 1_000_000.0),
        },
        index=idx,
    )


def _uptrend(n=300):
    return _bars_from_prices(100.0 * np.exp(np.arange(n) * 0.003))   # ~0.3%/day up


def _downtrend(n=300):
    return _bars_from_prices(300.0 * np.exp(-np.arange(n) * 0.003))  # ~0.3%/day down


def _flat(n=300):
    rng = np.random.default_rng(0)
    return _bars_from_prices(100.0 + rng.normal(0, 0.05, n))         # flat + tiny noise


def test_bull_trend():
    bars = _uptrend()
    out = classify_regime(bars, vix_value=15.0)   # VIX < 20
    assert out["regime"].iloc[-1] == BULL_TREND, out["regime"].iloc[-1]
    print("[test] BULL_TREND identified ✓")


def test_bear_trend():
    bars = _downtrend()
    out = classify_regime(bars, vix_value=27.0)   # 25 < VIX < 30
    assert out["regime"].iloc[-1] == BEAR_TREND, out["regime"].iloc[-1]
    print("[test] BEAR_TREND identified ✓")


def test_high_vol_overrides():
    # Strong uptrend that would be BULL, but VIX > 30 must override to HIGH_VOL.
    bars = _uptrend()
    out = classify_regime(bars, vix_value=35.0)
    assert out["regime"].iloc[-1] == HIGH_VOL, out["regime"].iloc[-1]
    print("[test] HIGH_VOL overrides trend ✓")


def test_choppy():
    bars = _flat()
    out = classify_regime(bars, vix_value=22.0)   # no strong trend/return
    assert out["regime"].iloc[-1] == CHOPPY, out["regime"].iloc[-1]
    print("[test] CHOPPY identified ✓")


def test_vix_missing_fallback():
    # SPY-only: uptrend -> BULL_TREND even without VIX; HIGH_VOL never assigned.
    bars = _uptrend()
    out = classify_regime(bars, vix_value=None)
    assert out["regime"].iloc[-1] == BULL_TREND, out["regime"].iloc[-1]
    assert (out["regime"] != HIGH_VOL).all(), "HIGH_VOL must not appear without VIX"
    print("[test] VIX-missing SPY-only fallback ✓")


def test_per_bar_vix_array():
    # A per-bar VIX series spiking at the end flips the last bar to HIGH_VOL.
    bars = _uptrend()
    vix = np.full(len(bars), 15.0)
    vix[-1] = 40.0
    out = classify_regime(bars, vix_value=vix)
    assert out["regime"].iloc[-2] == BULL_TREND
    assert out["regime"].iloc[-1] == HIGH_VOL
    print("[test] per-bar VIX array honored ✓")


def test_regime_column_length():
    bars = _uptrend()
    out = classify_regime(bars, vix_value=15.0)
    assert len(out) == len(bars)
    assert "regime" in out.columns
    print("[test] regime column shape valid ✓")


def _run_all():
    tests = [
        test_bull_trend,
        test_bear_trend,
        test_high_vol_overrides,
        test_choppy,
        test_vix_missing_fallback,
        test_per_bar_vix_array,
        test_regime_column_length,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"[ERROR] {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return failures


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_all() else 0)
