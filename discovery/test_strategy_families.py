"""
Unit tests for the multi-factor discovery families (Task 3).

Verifies each family implements the SwingPositionStrategy interface:
- param_grid() returns the exact grids specified in the task,
- position_vector() returns a {-1,0,+1} (or {0,+1}) array of len(df),
- the family is registered in the permutation framework registry,
- each family is wired into the regime/permutation scoring without error,
- insider-flow degrades to all-flat when no insider column is present, and
  actually takes a position when a synthetic insider series is supplied.

Run:  python -m discovery.test_strategy_families
"""
import numpy as np
import pandas as pd

from discovery.permutation_framework import (
    _STRATEGY_REGISTRY,
    calculate_log_returns,
    calculate_objective_score,
)
from discovery.strategies.insider_flow_strategy import (
    INSIDER_COLUMN,
    InsiderFlowPositionStrategy,
)
from discovery.strategies.mean_reversion_strategy import MeanReversionPositionStrategy
from discovery.strategies.volume_breakout_strategy import VolumeBreakoutPositionStrategy


def _make_ohlcv(n: int = 600, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.015, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
    vol = rng.integers(1_000_000, 8_000_000, n).astype(float)
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def _check_vector(strat, df):
    for params in strat.param_grid():
        pos = strat.position_vector(df, params)
        assert pos.shape[0] == len(df), f"{strat.name}: wrong length"
        assert set(np.unique(pos)).issubset({-1.0, 0.0, 1.0}), f"{strat.name}: bad values"


def test_mean_reversion_grid_and_vector():
    s = MeanReversionPositionStrategy()
    grid = s.param_grid()
    assert len(grid) == 3 * 3 * 2  # bb_period x bb_std x rsi_period
    _check_vector(s, _make_ohlcv())
    print("[test] mean-reversion grid (18) + vector OK")


def test_volume_breakout_grid_and_vector():
    s = VolumeBreakoutPositionStrategy()
    grid = s.param_grid()
    assert len(grid) == 3 * 3 * 2  # breakout_period x volume_mult x obv_lookback
    _check_vector(s, _make_ohlcv())
    print("[test] volume-breakout grid (18) + vector OK")


def test_insider_grid_and_no_data_flat():
    s = InsiderFlowPositionStrategy()
    grid = s.param_grid()
    assert len(grid) == 3 * 3 * 2  # threshold x lookback x ema_period
    df = _make_ohlcv()
    # No insider column => never trades (documented limitation), never crashes.
    pos = s.position_vector(df, grid[0])
    assert np.all(pos == 0.0), "insider flow should be flat without insider data"
    print("[test] insider-flow grid (18) + flat-without-data OK")


def test_insider_takes_position_with_data():
    s = InsiderFlowPositionStrategy()
    df = _make_ohlcv()
    insider = np.zeros(len(df))
    insider[100:105] = 200_000.0  # a cluster of insider buys above the $100k tier
    df[INSIDER_COLUMN] = insider
    pos = s.position_vector(df, {"insider_threshold": 100_000, "lookback": 5, "ema_period": 20})
    assert pos.max() == 1.0, "insider flow should go long after a qualifying buy cluster"
    assert pos.min() >= 0.0, "insider flow is long-only"
    print("[test] insider-flow takes a long with synthetic data OK")


def test_families_registered():
    for cls in (MeanReversionPositionStrategy, VolumeBreakoutPositionStrategy,
                InsiderFlowPositionStrategy):
        assert _STRATEGY_REGISTRY.get(cls.name) is cls, f"{cls.name} not registered"
    print("[test] all families registered in MCPT registry OK")


def test_families_scoreable():
    """Position vectors flow through calculate_objective_score without error."""
    df = _make_ohlcv()
    returns = calculate_log_returns(df["close"].to_numpy())
    for cls in (MeanReversionPositionStrategy, VolumeBreakoutPositionStrategy):
        s = cls()
        pos = s.position_vector(df, s.param_grid()[0])
        score = calculate_objective_score(pos, returns, "sharpe")
        assert np.isfinite(score)
    print("[test] families produce finite objective scores OK")


def _run_all():
    tests = [
        test_mean_reversion_grid_and_vector,
        test_volume_breakout_grid_and_vector,
        test_insider_grid_and_no_data_flat,
        test_insider_takes_position_with_data,
        test_families_registered,
        test_families_scoreable,
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
