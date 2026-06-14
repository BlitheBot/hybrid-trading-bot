"""
Unit tests for cross-asset regime confirmation (Task 3).

Verifies:
- compute_cross_asset_signals fails open (all "n/a") when the data client returns
  nothing, and never raises,
- it classifies credit/rotation/dollar/vix-structure correctly from a stub client,
- regime_confidence rewards aligned signals and penalizes opposed ones,
- CHOPPY confidence drops as the net cross-asset bias strengthens,
- the returned dict always carries the four labelled signals + a scores map.

Run:  python -m discovery.test_cross_asset_regime
"""
import numpy as np
import pandas as pd

import discovery.regime_classifier as rc
from discovery.regime_classifier import (
    BULL_TREND, BEAR_TREND, CHOPPY,
    compute_cross_asset_signals, regime_confidence,
)


class _StubClient:
    """Returns canned daily-close DataFrames keyed by symbol via get_historical_bars."""
    def __init__(self, series_by_symbol):
        self._series = series_by_symbol


def _patch_bars(monkeypatch_map):
    """Monkeypatch regime_classifier's lazy get_historical_bars import path."""
    def fake_get_historical_bars(symbol, timeframe, days_back, client, is_crypto=False):
        vals = monkeypatch_map.get(symbol)
        if vals is None:
            return None
        idx = pd.date_range("2024-01-01", periods=len(vals), freq="B")
        return pd.DataFrame({"close": vals}, index=idx)
    import utils
    utils.get_historical_bars = fake_get_historical_bars


def _reset_cache():
    rc._CROSS_ASSET_CACHE = None


def test_fail_open_no_data():
    _reset_cache()
    _patch_bars({})  # every symbol → None
    sig = compute_cross_asset_signals(_StubClient({}), vix_value=None, cache_seconds=0)
    assert sig["credit"] == "n/a"
    assert sig["rotation"] == "n/a"
    assert sig["dollar"] == "n/a"
    assert sig["available"] is False
    print("[test] cross-asset fail-open OK")


def test_risk_off_signals():
    _reset_cache()
    n = 90
    flat = list(np.full(n, 100.0))
    # HYG down, LQD flat → credit stress; defensives up vs cyclicals → defensive
    # rotation; UUP up → strong dollar.
    rising = list(np.linspace(100, 110, n))
    falling = list(np.linspace(100, 95, n))
    _patch_bars({
        "SPY": flat,
        "HYG": falling, "LQD": flat,
        "XLK": flat, "XLF": flat,
        "XLU": rising, "XLP": rising,
        "UUP": rising,
    })
    sig = compute_cross_asset_signals(_StubClient({}), vix_value=35.0, cache_seconds=0)
    assert sig["credit"] == "stress", sig
    assert sig["rotation"] == "defensive", sig
    assert sig["dollar"] == "strong", sig
    # All three risk-off (-1) signals align with a BEAR_TREND.
    conf_bear = regime_confidence(BEAR_TREND, sig)
    conf_bull = regime_confidence(BULL_TREND, sig)
    assert conf_bear > conf_bull, (conf_bear, conf_bull)
    print(f"[test] risk-off cross-asset signals OK (bear={conf_bear}% bull={conf_bull}%)")


def test_risk_on_signals():
    _reset_cache()
    n = 90
    flat = list(np.full(n, 100.0))
    rising = list(np.linspace(100, 110, n))
    falling = list(np.linspace(100, 95, n))
    _patch_bars({
        "SPY": flat,
        "HYG": rising, "LQD": flat,        # credit risk_on
        "XLK": rising, "XLF": rising,      # cyclical leadership
        "XLU": flat, "XLP": flat,
        "UUP": falling,                    # weak dollar
    })
    sig = compute_cross_asset_signals(_StubClient({}), vix_value=12.0, cache_seconds=0)
    assert sig["credit"] == "risk_on", sig
    assert sig["rotation"] == "cyclical", sig
    assert sig["dollar"] == "weak", sig
    conf_bull = regime_confidence(BULL_TREND, sig)
    assert conf_bull >= 75, conf_bull
    print(f"[test] risk-on cross-asset signals OK (bull={conf_bull}%)")


def test_confidence_baseline_and_choppy():
    # No scores → baseline 50.
    assert regime_confidence(BULL_TREND, {"scores": {}}) == 50
    # CHOPPY confidence drops as net bias grows.
    strong_bias = {"scores": {"a": 1, "b": 1, "c": 1, "d": 1}}
    weak_bias = {"scores": {"a": 1, "b": -1, "c": 1, "d": -1}}
    assert regime_confidence(CHOPPY, strong_bias) < regime_confidence(CHOPPY, weak_bias)
    print("[test] confidence baseline + choppy penalty OK")


def test_signals_dict_shape():
    _reset_cache()
    _patch_bars({})
    sig = compute_cross_asset_signals(_StubClient({}), vix_value=None, cache_seconds=0)
    for key in ("vix_structure", "credit", "rotation", "dollar", "scores", "available"):
        assert key in sig
    print("[test] cross-asset dict shape OK")


def _run_all():
    tests = [
        test_fail_open_no_data,
        test_risk_off_signals,
        test_risk_on_signals,
        test_confidence_baseline_and_choppy,
        test_signals_dict_shape,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            import traceback
            failures += 1
            print(f"[ERROR] {t.__name__}: {e}\n{traceback.format_exc()}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return failures


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_all() else 0)
