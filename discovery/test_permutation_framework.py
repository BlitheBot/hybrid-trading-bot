"""
Unit tests for the permutation validation framework.

Focus: the bar permutation generator must preserve the statistical moments of the
close-to-close log-return distribution (the property that makes Monte Carlo
permutation testing valid), while destroying path memory.

Run directly:   python discovery/test_permutation_framework.py
Or via pytest:  pytest discovery/test_permutation_framework.py
"""
import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew

from discovery.permutation_framework import (
    SwingPositionStrategy,
    calculate_log_returns,
    calculate_objective_score,
    get_permutation,
)


def _make_ohlcv(n: int = 500, seed: int = 7) -> pd.DataFrame:
    """Synthetic geometric-random-walk OHLCV with realistic intra-bar structure."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0003, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.005, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.005, n)))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def _close_returns(df: pd.DataFrame) -> np.ndarray:
    return np.diff(np.log(df["close"].to_numpy()))


def test_moments_preserved():
    """Mean, std, skew, kurtosis of log returns preserved within 1%."""
    df = _make_ohlcv()
    perm = get_permutation(df, start_index=0, seed=123)

    o = _close_returns(df)
    p = _close_returns(perm)

    assert np.isclose(o.mean(), p.mean(), rtol=0.01, atol=1e-6), "mean drifted"
    assert np.isclose(o.std(), p.std(), rtol=0.01, atol=1e-6), "std drifted"
    assert np.isclose(skew(o), skew(p), rtol=0.01, atol=1e-6), "skew drifted"
    assert np.isclose(kurtosis(o), kurtosis(p), rtol=0.01, atol=1e-6), "kurtosis drifted"
    print("[test] moments preserved within 1% ✓")


def test_final_close_preserved():
    """Single-shuffle permutation preserves the final close exactly."""
    df = _make_ohlcv()
    perm = get_permutation(df, start_index=0, seed=99)
    assert np.isclose(df["close"].iloc[-1], perm["close"].iloc[-1], rtol=1e-9), \
        "final close changed"
    print("[test] final close preserved ✓")


def test_path_actually_changes():
    """The permutation must destroy path memory (interior bars differ)."""
    df = _make_ohlcv()
    perm = get_permutation(df, start_index=0, seed=42)
    interior_orig = df["close"].to_numpy()[1:-1]
    interior_perm = perm["close"].to_numpy()[1:-1]
    assert not np.allclose(interior_orig, interior_perm), "path did not change"
    print("[test] interior path changed ✓")


def test_training_period_preserved():
    """With start_index>0 the training region is left untouched."""
    df = _make_ohlcv()
    start = 200
    perm = get_permutation(df, start_index=start, seed=11)
    np.testing.assert_allclose(
        df["close"].to_numpy()[:start + 1],
        perm["close"].to_numpy()[:start + 1],
        rtol=1e-9,
    )
    print("[test] training period preserved ✓")


def test_correlated_assets_same_shuffle():
    """List input applies one shuffle index to all assets; each stays valid."""
    a = _make_ohlcv(seed=1)
    b = _make_ohlcv(seed=2)
    perm_a, perm_b = get_permutation([a, b], start_index=0, seed=55)
    for orig, perm in ((a, perm_a), (b, perm_b)):
        o, p = _close_returns(orig), _close_returns(perm)
        assert np.isclose(o.std(), p.std(), rtol=0.01, atol=1e-6)
    print("[test] correlated-asset permutation valid ✓")


def test_objective_score_directionality():
    """A position aligned with future returns scores higher than its inverse."""
    df = _make_ohlcv()
    returns = calculate_log_returns(df["close"].to_numpy())
    # Perfect-foresight position: long when next bar is up.
    perfect = np.sign(np.roll(returns, -1))
    sharpe_perfect = calculate_objective_score(perfect, returns, method="sharpe")
    sharpe_inverse = calculate_objective_score(-perfect, returns, method="sharpe")
    assert sharpe_perfect > sharpe_inverse, "objective not directional"
    assert sharpe_perfect > 0 > sharpe_inverse
    print("[test] objective score directional ✓")


def test_position_vector_shape_and_values():
    """Swing position vector matches data length and is in {0, 1}."""
    df = _make_ohlcv()
    strat = SwingPositionStrategy()
    params = strat.param_grid()[0]
    pos = strat.position_vector(df, params)
    assert pos.shape[0] == len(df)
    assert set(np.unique(pos)).issubset({0.0, 1.0})
    print("[test] position vector shape/values valid ✓")


def _run_all():
    tests = [
        test_moments_preserved,
        test_final_close_preserved,
        test_path_actually_changes,
        test_training_period_preserved,
        test_correlated_assets_same_shuffle,
        test_objective_score_directionality,
        test_position_vector_shape_and_values,
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
