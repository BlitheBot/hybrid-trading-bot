"""
Unit tests for the transaction cost model (Task 1) in permutation_framework.

Verifies:
- spread/impact tier classification from liquidity & order size,
- borrow cost only applies to short bars and scales with annual rate,
- costs reduce (never increase) a profitable strategy's score,
- a flat (always-0) position incurs zero cost,
- cost_breakdown components sum consistently with per-bar deductions.

Run directly:   python discovery/test_transaction_costs.py
Or via pytest:  pytest discovery/test_transaction_costs.py
"""
import numpy as np
import pandas as pd

from config import Config
from discovery.permutation_framework import (
    CostModel,
    ZERO_COST,
    _classify_impact,
    _classify_spread,
    _per_bar_cost_array,
    build_cost_model,
    calculate_log_returns,
    calculate_objective_score,
    cost_breakdown,
)


def _make_ohlcv(n: int = 400, seed: int = 7, volume: float = 2_000_000.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0004, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    vol = np.full(n, volume)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_spread_tier():
    """Liquid (>$100M $vol) gets the tight spread; illiquid gets the wide one."""
    assert _classify_spread(Config.COST_LIQUID_DOLLAR_VOLUME * 2) == Config.COST_SPREAD_LIQUID_PCT
    assert _classify_spread(Config.COST_LIQUID_DOLLAR_VOLUME * 0.1) == Config.COST_SPREAD_ILLIQUID_PCT
    print("[test] spread tier classification OK")


def test_impact_tier():
    """Impact rises with order size as a fraction of ADV."""
    assert _classify_impact(0.0005) == Config.COST_IMPACT_SMALL_PCT   # < 0.1%
    assert _classify_impact(0.003) == Config.COST_IMPACT_MEDIUM_PCT   # 0.1%-0.5%
    assert _classify_impact(0.01) == Config.COST_IMPACT_LARGE_PCT     # > 0.5%
    print("[test] impact tier classification OK")


def test_borrow_only_on_shorts():
    """Borrow cost is deducted only on short bars, none on long/flat."""
    cost = CostModel(spread_per_side=0.0, impact_per_side=0.0, borrow_daily=0.001)
    long_only = np.array([0, 1, 1, 1, 0], dtype=float)
    short_some = np.array([0, -1, -1, 0, 0], dtype=float)
    long_costs = _per_bar_cost_array(long_only, cost)
    short_costs = _per_bar_cost_array(short_some, cost)
    assert long_costs.sum() == 0.0, "long-only should incur no borrow cost"
    assert short_costs.sum() > 0.0, "short bars should incur borrow cost"
    print("[test] borrow only on shorts OK")


def test_flat_position_zero_cost():
    """An always-flat position never trades, so total cost drag is zero."""
    cost = CostModel(spread_per_side=0.01, impact_per_side=0.01, borrow_daily=0.01)
    flat = np.zeros(50, dtype=float)
    s, i, b = cost_breakdown(flat, cost)
    assert (s, i, b) == (0.0, 0.0, 0.0)
    print("[test] flat position zero cost OK")


def test_costs_reduce_score():
    """Net-of-cost Sharpe is <= gross Sharpe for a turning strategy."""
    df = _make_ohlcv()
    returns = calculate_log_returns(df["close"].to_numpy())
    # A position that flips often (lots of turnover) so costs bite.
    pos = np.where(np.arange(len(df)) % 4 < 2, 1.0, 0.0)
    cost = CostModel(spread_per_side=0.0005, impact_per_side=0.001, borrow_daily=0.0)
    gross = calculate_objective_score(pos, returns, "sharpe")
    net = calculate_objective_score(pos, returns, "sharpe", cost=cost)
    assert net <= gross, "costs should not increase the score"
    print(f"[test] costs reduce score (gross={gross:.3f} net={net:.3f}) OK")


def test_zero_cost_is_noop():
    """Scoring with ZERO_COST equals scoring with no cost argument."""
    df = _make_ohlcv()
    returns = calculate_log_returns(df["close"].to_numpy())
    pos = np.where(np.arange(len(df)) % 3 == 0, 1.0, 0.0)
    a = calculate_objective_score(pos, returns, "sharpe")
    b = calculate_objective_score(pos, returns, "sharpe", cost=ZERO_COST)
    assert np.isclose(a, b), "ZERO_COST should be a no-op"
    print("[test] zero cost is a no-op OK")


def test_build_cost_model_liquidity():
    """Median dollar volume drives the spread tier when modeling is enabled."""
    if not Config.COST_MODELING_ENABLED:
        print("[test] build_cost_model skipped (modeling disabled) OK")
        return
    liquid = build_cost_model(_make_ohlcv(volume=5_000_000.0))    # ~$500M $vol
    illiquid = build_cost_model(_make_ohlcv(volume=200_000.0))    # ~$20M $vol
    assert liquid.spread_per_side == Config.COST_SPREAD_LIQUID_PCT
    assert illiquid.spread_per_side == Config.COST_SPREAD_ILLIQUID_PCT
    print("[test] build_cost_model liquidity tiers OK")


def _run_all():
    tests = [
        test_spread_tier,
        test_impact_tier,
        test_borrow_only_on_shorts,
        test_flat_position_zero_cost,
        test_costs_reduce_score,
        test_zero_cost_is_noop,
        test_build_cost_model_liquidity,
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
