"""
Unit tests for the PEAD earnings-drift discovery family (Task 1, family 6).

Verifies:
- param_grid() returns the 54-combo grid specified in the task,
- position_vector() returns a {-1,0,+1} array of len(df),
- with no earnings column the family degrades to all-flat (never crashes),
- a positive surprise above EMA produces a long entry after the configured delay,
- a negative surprise below EMA produces a short entry,
- the family is registered in the MCPT registry,
- the FMP loader parses surprise magnitudes and drops zero-estimate rows.

Run:  python -m discovery.test_pead_strategy
"""
import numpy as np
import pandas as pd

from discovery.permutation_framework import _STRATEGY_REGISTRY
from discovery.strategies.pead_strategy import (
    EARNINGS_COLUMN,
    PEADPositionStrategy,
)


def _make_ohlcv(n: int = 200, seed: int = 7, drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.004, n)))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_grid_is_54():
    s = PEADPositionStrategy()
    grid = s.param_grid()
    assert len(grid) == 3 * 3 * 3 * 2, f"expected 54 combos, got {len(grid)}"
    print("[test] PEAD grid == 54 OK")


def test_vector_shape_and_values():
    s = PEADPositionStrategy()
    df = _make_ohlcv()
    df[EARNINGS_COLUMN] = 0.0
    for params in s.param_grid()[:6]:
        pos = s.position_vector(df, params)
        assert pos.shape[0] == len(df)
        assert set(np.unique(pos)).issubset({-1.0, 0.0, 1.0})
    print("[test] PEAD vector shape/value-set OK")


def test_flat_without_earnings_column():
    s = PEADPositionStrategy()
    df = _make_ohlcv()  # no earnings column
    pos = s.position_vector(df, s.param_grid()[0])
    assert np.all(pos == 0.0), "PEAD must be flat with no earnings feed"
    print("[test] PEAD flat-without-data OK")


def test_long_on_positive_surprise():
    s = PEADPositionStrategy()
    # Steady uptrend so close stays above EMA on the entry bar.
    df = _make_ohlcv(seed=1, drift=0.002)
    surprise = np.zeros(len(df))
    surprise[80] = 0.12  # +12% beat
    df[EARNINGS_COLUMN] = surprise
    pos = s.position_vector(df, {"surprise_threshold": 0.05, "entry_delay_days": 2,
                                 "hold_days": 20, "ema_period": 20})
    assert pos.max() == 1.0, "expected a long after a positive surprise in an uptrend"
    print("[test] PEAD goes long on positive surprise OK")


def test_short_on_negative_surprise():
    s = PEADPositionStrategy()
    df = _make_ohlcv(seed=2, drift=-0.002)  # downtrend → close below EMA
    surprise = np.zeros(len(df))
    surprise[80] = -0.12  # -12% miss
    df[EARNINGS_COLUMN] = surprise
    pos = s.position_vector(df, {"surprise_threshold": 0.05, "entry_delay_days": 2,
                                 "hold_days": 20, "ema_period": 20})
    assert pos.min() == -1.0, "expected a short after a negative surprise in a downtrend"
    print("[test] PEAD goes short on negative surprise OK")


def test_registered():
    assert _STRATEGY_REGISTRY.get(PEADPositionStrategy.name) is PEADPositionStrategy
    print("[test] PEAD registered in MCPT registry OK")


def test_fmp_loader_parsing():
    from fmp_earnings_calendar import _parse_rows
    rows = [
        {"date": "2023-01-15", "eps": 1.10, "epsEstimated": 1.00},   # +10%
        {"date": "2023-04-15", "eps": 0.80, "epsEstimated": 1.00},   # -20%
        {"date": "2023-07-15", "eps": 1.00, "epsEstimated": 0.0},    # dropped
        {"date": "2023-10-15", "eps": None, "epsEstimated": 1.0},    # dropped
    ]
    df = _parse_rows(rows)
    assert len(df) == 2
    assert abs(df.iloc[0]["surprise_pct"] - 0.10) < 1e-9
    assert abs(df.iloc[1]["surprise_pct"] + 0.20) < 1e-9
    print("[test] FMP loader parsing + surprise math OK")


def _run_all():
    tests = [
        test_grid_is_54,
        test_vector_shape_and_values,
        test_flat_without_earnings_column,
        test_long_on_positive_surprise,
        test_short_on_negative_surprise,
        test_registered,
        test_fmp_loader_parsing,
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
