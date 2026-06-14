"""
Unit tests for the short-interest-momentum discovery family (Task 2, family 7).

Verifies:
- param_grid() returns the 54-combo grid specified in the task,
- position_vector() returns a {-1,0,+1} array of len(df),
- with no si_change column the family degrades to all-flat,
- rising short interest on a downtrend produces a short,
- falling short interest + up-day on volume in an uptrend produces a squeeze long,
- the family is registered in the MCPT registry,
- the FINRA historical parser handles malformed rows and computes ratios.

Run:  python -m discovery.test_short_interest_momentum_strategy
"""
import numpy as np
import pandas as pd

from discovery.permutation_framework import _STRATEGY_REGISTRY
from discovery.strategies.short_interest_momentum_strategy import (
    SI_COLUMN,
    ShortInterestMomentumPositionStrategy,
)


def _make_ohlcv(n: int = 320, seed: int = 11, drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.004, n)))
    vol = rng.integers(1_000_000, 3_000_000, n).astype(float)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_grid_is_54():
    s = ShortInterestMomentumPositionStrategy()
    assert len(s.param_grid()) == 3 * 3 * 2 * 3
    print("[test] short-momentum grid == 54 OK")


def test_vector_shape_and_values():
    s = ShortInterestMomentumPositionStrategy()
    df = _make_ohlcv()
    df[SI_COLUMN] = 0.0
    for params in s.param_grid()[:6]:
        pos = s.position_vector(df, params)
        assert pos.shape[0] == len(df)
        assert set(np.unique(pos)).issubset({-1.0, 0.0, 1.0})
    print("[test] short-momentum vector shape/value-set OK")


def test_flat_without_si_column():
    s = ShortInterestMomentumPositionStrategy()
    df = _make_ohlcv()
    pos = s.position_vector(df, s.param_grid()[0])
    assert np.all(pos == 0.0)
    print("[test] short-momentum flat-without-data OK")


def test_short_on_rising_si_downtrend():
    s = ShortInterestMomentumPositionStrategy()
    df = _make_ohlcv(seed=3, drift=-0.0015)  # downtrend → EMA50 < EMA200
    si = np.zeros(len(df))
    si[260:280] = 0.30  # +30% WoW short interest, sustained
    df[SI_COLUMN] = si
    # Force RSI > 55 region check is data-dependent; use the easiest threshold.
    took_short = False
    for params in s.param_grid():
        pos = s.position_vector(df, params)
        if pos.min() == -1.0:
            took_short = True
            break
    assert took_short, "expected a short on rising short interest in a downtrend"
    print("[test] short-momentum shorts on rising SI + downtrend OK")


def test_squeeze_long_setup():
    s = ShortInterestMomentumPositionStrategy()
    # Deterministic path: long strong uptrend (EMA50 >> EMA200), a mild pullback to
    # pull RSI back below 65, then a >2% squeeze pop on heavy volume.
    n = 320
    close = np.empty(n)
    close[0] = 100.0
    for i in range(1, n):
        if i <= 260:
            close[i] = close[i - 1] * 1.004        # steady uptrend
        elif i <= 269:
            close[i] = close[i - 1] * 0.99         # mild pullback (cool RSI)
        elif i == 270:
            close[i] = close[i - 1] * 1.03         # +3% squeeze pop
        else:
            close[i] = close[i - 1] * 1.001
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    vol = np.full(n, 1_000_000.0)
    vol[270] = 8_000_000.0                          # heavy volume on the pop
    df = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": vol},
        index=idx,
    )
    si = np.zeros(n)
    si[265:275] = -0.30                             # -30% WoW short interest (squeeze)
    df[SI_COLUMN] = si

    took_long = False
    for params in s.param_grid():
        pos = s.position_vector(df, params)
        if pos.max() == 1.0:
            took_long = True
            break
    assert took_long, "expected a squeeze long on falling SI + up-day on volume in uptrend"
    print("[test] short-momentum squeeze long OK")


def test_registered():
    assert (_STRATEGY_REGISTRY.get(ShortInterestMomentumPositionStrategy.name)
            is ShortInterestMomentumPositionStrategy)
    print("[test] short-momentum registered in MCPT registry OK")


def test_finra_parser():
    from discovery.data_feeds.finra_historical import _parse_content
    content = (
        "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
        "20240115|AAPL|600000|0|1000000|Q\n"
        "20240115|ZERO|0|0|0|Q\n"        # total 0 → dropped
        "20240115|JUNK\n"                # malformed → dropped
    )
    parsed = _parse_content(content)
    assert abs(parsed["AAPL"] - 0.6) < 1e-9
    assert "ZERO" not in parsed and "JUNK" not in parsed
    print("[test] FINRA historical parser OK")


def _run_all():
    tests = [
        test_grid_is_54,
        test_vector_shape_and_values,
        test_flat_without_si_column,
        test_short_on_rising_si_downtrend,
        test_squeeze_long_setup,
        test_registered,
        test_finra_parser,
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
