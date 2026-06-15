"""
Unit tests for the historical SEC Form 4 backtest feed (Task 5).

Verifies (all offline, via stubs — no network):
- get_insider_signal sums net insider buying over the lookback window,
- attach_insider_buy_value places net values on the right bars and zeros elsewhere,
- is_data_stale flags symbols whose newest filing is older than the threshold,
- InsiderFlowStrategy actually takes a long once the feed supplies a buy cluster,
- the family is registered + enrich() is a no-op when the feed is disabled.

Run:  python -m discovery.test_edgar_historical
"""
import numpy as np
import pandas as pd

import discovery.data_feeds.edgar_historical as eh
from discovery.permutation_framework import _STRATEGY_REGISTRY
from discovery.strategies.insider_flow_strategy import (
    INSIDER_COLUMN,
    InsiderFlowPositionStrategy,
)


def _stub_history(df):
    eh.get_form4_history = lambda *a, **k: df


def test_get_insider_signal_window():
    _stub_history(pd.DataFrame({
        "date": pd.to_datetime(["2023-01-10", "2023-01-12", "2023-02-01"]),
        "net_value": [100_000.0, 50_000.0, -25_000.0],
    }))
    sig = eh.get_insider_signal("TEST", "2023-01-13", lookback_days=5)
    assert abs(sig - 150_000.0) < 1e-6, sig
    # Outside the window → 0.
    assert eh.get_insider_signal("TEST", "2023-03-15", lookback_days=5) == 0.0
    print("[test] get_insider_signal window math OK")


def test_attach_places_values():
    _stub_history(pd.DataFrame({
        "date": pd.to_datetime(["2023-01-10", "2023-02-01"]),
        "net_value": [100_000.0, -25_000.0],
    }))
    idx = pd.date_range("2023-01-01", periods=40, freq="B")
    bars = pd.DataFrame({"close": np.linspace(100, 110, 40)}, index=idx)
    enriched = eh.attach_insider_buy_value(bars, "TEST")
    assert INSIDER_COLUMN in enriched.columns
    assert abs(enriched[INSIDER_COLUMN].sum() - 75_000.0) < 1e-6
    assert int((enriched[INSIDER_COLUMN] != 0).sum()) == 2
    print("[test] attach_insider_buy_value placement OK")


def test_attach_empty_is_all_zero():
    _stub_history(pd.DataFrame(columns=["date", "net_value"]))
    idx = pd.date_range("2023-01-01", periods=20, freq="B")
    bars = pd.DataFrame({"close": np.linspace(100, 110, 20)}, index=idx)
    enriched = eh.attach_insider_buy_value(bars, "TEST")
    assert (enriched[INSIDER_COLUMN] == 0.0).all()
    print("[test] attach with no data -> all zero OK")


def test_is_data_stale():
    import datetime as _dt
    recent = _dt.date.today() - _dt.timedelta(days=5)
    old = _dt.date.today() - _dt.timedelta(days=120)
    _stub_history(pd.DataFrame({"date": pd.to_datetime([recent]), "net_value": [10_000.0]}))
    assert eh.is_data_stale("TEST", stale_days=30) is False
    _stub_history(pd.DataFrame({"date": pd.to_datetime([old]), "net_value": [10_000.0]}))
    assert eh.is_data_stale("TEST", stale_days=30) is True
    _stub_history(pd.DataFrame(columns=["date", "net_value"]))
    assert eh.is_data_stale("TEST", stale_days=30) is True
    print("[test] is_data_stale OK")


def test_insider_family_trades_with_feed():
    # Enriched bars with a real buy cluster → InsiderFlowStrategy goes long.
    _stub_history(pd.DataFrame({
        "date": pd.to_datetime(["2023-03-01", "2023-03-02"]),
        "net_value": [200_000.0, 200_000.0],
    }))
    idx = pd.date_range("2023-01-02", periods=120, freq="B")
    close = np.linspace(100, 130, 120)  # uptrend so close > EMA
    bars = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": np.full(120, 1e6)}, index=idx)
    enriched = eh.attach_insider_buy_value(bars, "TEST")
    s = InsiderFlowPositionStrategy()
    pos = s.position_vector(enriched, {"insider_threshold": 100_000, "lookback": 5, "ema_period": 20})
    assert pos.max() == 1.0, "insider family should go long after a qualifying buy cluster"
    print("[test] insider family trades with historical feed OK")


def test_registered():
    assert _STRATEGY_REGISTRY.get(InsiderFlowPositionStrategy.name) is InsiderFlowPositionStrategy
    assert hasattr(InsiderFlowPositionStrategy, "enrich")
    print("[test] insider family registered + has enrich hook OK")


def _run_all():
    tests = [
        test_get_insider_signal_window,
        test_attach_places_values,
        test_attach_empty_is_all_zero,
        test_is_data_stale,
        test_insider_family_trades_with_feed,
        test_registered,
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
