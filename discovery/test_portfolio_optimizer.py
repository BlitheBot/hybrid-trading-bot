"""
Unit tests for correlation-aware portfolio construction (Task 4).

Tests the DB-free pure functions:
- daily_returns_from_outcomes aggregates trade P&L into per-combo daily series,
- correlation_matrix is symmetric, unit-diagonal, and treats thin overlap as 0,
- greedy_select picks highest Sharpe first and rejects correlated additions,
- greedy_select respects the max_size cap,
- combined_sharpe returns a finite annualized number.

Run:  python -m discovery.test_portfolio_optimizer
"""
import numpy as np
import pandas as pd

from discovery.portfolio_optimizer import (
    combined_sharpe,
    correlation_matrix,
    daily_returns_from_outcomes,
    greedy_select,
)


def _outcomes():
    return pd.DataFrame({
        "signal_type": ["swing_long", "swing_long", "mr", "mr"],
        "symbol": ["AAA", "AAA", "BBB", "BBB"],
        "entry_time": pd.to_datetime(
            ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-03"]
        ),
        "pnl_pct": [1.0, 2.0, -1.0, 3.0],
    })


def test_daily_returns_aggregation():
    r = daily_returns_from_outcomes(_outcomes())
    # Two trades on 2024-01-01 for AAA sum to (1%+2%) = 0.03.
    aaa = r[("swing_long", "AAA")]
    assert np.isclose(aaa.iloc[0], 0.03)
    assert ("mr", "BBB") in r
    print("[test] daily returns aggregation OK")


def test_correlation_matrix_properties():
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    base = pd.Series(np.random.default_rng(0).normal(0, 0.01, 30), index=idx)
    keys = [("a", "A"), ("a", "B"), ("a", "C")]
    returns = {
        keys[0]: base,
        keys[1]: base * -1.0,          # perfectly anti-correlated
        keys[2]: pd.Series([0.01], index=idx[:1]),  # thin overlap -> treated as 0
    }
    corr = correlation_matrix(returns, keys, min_overlap=10)
    assert np.allclose(np.diag(corr), 1.0)
    assert np.allclose(corr, corr.T)
    assert np.isclose(corr[0, 1], -1.0, atol=1e-6)
    assert corr[0, 2] == 0.0  # thin overlap
    print("[test] correlation matrix properties OK")


def test_greedy_selects_highest_first_and_diversifies():
    candidates = [
        {"strategy_name": "s", "symbol": "A", "sharpe": 2.0},
        {"strategy_name": "s", "symbol": "B", "sharpe": 1.5},  # highly corr w/ A -> reject
        {"strategy_name": "s", "symbol": "C", "sharpe": 1.0},  # uncorrelated -> accept
    ]
    corr = np.array([
        [1.0, 0.95, 0.1],
        [0.95, 1.0, 0.1],
        [0.1, 0.1, 1.0],
    ])
    chosen = greedy_select(candidates, corr, max_corr=0.7, max_size=20)
    names = [(c["symbol"]) for c in chosen]
    assert names == ["A", "C"], f"expected A then C, got {names}"
    assert chosen[0]["rank"] == 1 and chosen[1]["rank"] == 2
    print("[test] greedy picks highest-Sharpe + diversifies OK")


def test_greedy_respects_max_size():
    candidates = [{"strategy_name": "s", "symbol": str(i), "sharpe": 10 - i} for i in range(10)]
    corr = np.eye(10)  # all uncorrelated
    chosen = greedy_select(candidates, corr, max_corr=0.7, max_size=3)
    assert len(chosen) == 3
    print("[test] greedy respects max_size OK")


def test_combined_sharpe_finite():
    idx = pd.date_range("2024-01-01", periods=60, freq="D")
    rng = np.random.default_rng(1)
    keys = [("s", "A"), ("s", "B")]
    returns = {
        keys[0]: pd.Series(rng.normal(0.001, 0.01, 60), index=idx),
        keys[1]: pd.Series(rng.normal(0.001, 0.01, 60), index=idx),
    }
    s = combined_sharpe(returns, keys, fallback_sharpes=[1.0, 1.0])
    assert np.isfinite(s)
    print(f"[test] combined Sharpe finite ({s:.2f}) OK")


def test_combined_sharpe_fallback():
    """No return history -> use the diversification-adjusted fallback."""
    s = combined_sharpe({}, [("s", "A"), ("s", "B")], fallback_sharpes=[1.0, 1.0])
    assert np.isfinite(s) and s > 0
    print("[test] combined Sharpe fallback OK")


def _run_all():
    tests = [
        test_daily_returns_aggregation,
        test_correlation_matrix_properties,
        test_greedy_selects_highest_first_and_diversifies,
        test_greedy_respects_max_size,
        test_combined_sharpe_finite,
        test_combined_sharpe_fallback,
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
