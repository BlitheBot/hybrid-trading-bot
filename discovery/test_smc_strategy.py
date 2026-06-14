"""
Unit tests for SMC strategy family (Task 9 / family 5).

Tests cover:
- Order block detection on synthetic impulse data
- FVG detection on explicit 3-candle patterns
- OB and FVG invalidation logic
- Signal generation via position_vector ({-1, 0, +1} contract)
- param_grid size (54 combos)
- Registry registration in permutation framework
- Scoreable by calculate_objective_score

Run:  python -m discovery.test_smc_strategy
"""
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 600, seed: int = 7, drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.015, n)
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


def _flat_df(n: int, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open":   np.full(n, price),
        "high":   np.full(n, price + 0.01),
        "low":    np.full(n, price - 0.01),
        "close":  np.full(n, price),
        "volume": np.full(n, 1_000_000.0),
    }, index=idx)


# ---------------------------------------------------------------------------
# Order block tests
# ---------------------------------------------------------------------------

def test_ob_detects_bullish_ob_on_synthetic_impulse():
    """
    Manually plant a bearish candle followed by a large upward impulse bar.
    detect_order_blocks should return that candle as a bullish order block.
    """
    from discovery.strategies.smc_strategy import detect_order_blocks

    n = 50
    df = _flat_df(n)
    atr_est = 0.5  # ATR approx for flat $100 data

    # At bar 30: bearish OB candle (open > close)
    df.at[df.index[30], "open"] = 101.0
    df.at[df.index[30], "close"] = 99.5
    df.at[df.index[30], "high"] = 101.5
    df.at[df.index[30], "low"] = 99.0

    # At bar 31: bullish impulse (close[31] - close[30] > 1.5 * ATR)
    # We'll use a move of ~5 points (big enough for any reasonable ATR)
    df.at[df.index[31], "open"] = 99.5
    df.at[df.index[31], "close"] = 106.0
    df.at[df.index[31], "high"] = 106.5
    df.at[df.index[31], "low"] = 99.3

    obs = detect_order_blocks(df, atr_multiplier=1.5, lookback=30)
    bullish = [o for o in obs if o["direction"] == "bullish"]
    assert len(bullish) >= 1, "Expected at least one bullish order block"
    # The OB zone should be around the bearish candle at bar 30
    ob = bullish[0]
    assert 99.0 <= ob["low"] <= 100.0, f"OB low out of range: {ob['low']}"
    assert 101.0 <= ob["high"] <= 102.0, f"OB high out of range: {ob['high']}"
    assert ob["strength"] > 1.0, "Expected strength > 1.0 for a large impulse"
    print("[test] OB detects bullish order block on synthetic impulse OK")


def test_ob_detects_bearish_ob_on_synthetic_impulse():
    """
    Manually plant a bullish candle followed by a large downward impulse bar.
    detect_order_blocks should return that candle as a bearish order block.
    """
    from discovery.strategies.smc_strategy import detect_order_blocks

    n = 50
    df = _flat_df(n)

    # At bar 30: bullish OB candle (close > open)
    df.at[df.index[30], "open"] = 99.0
    df.at[df.index[30], "close"] = 101.5
    df.at[df.index[30], "high"] = 102.0
    df.at[df.index[30], "low"] = 98.8

    # At bar 31: bearish impulse (close[31] - close[30] is very negative)
    df.at[df.index[31], "open"] = 101.5
    df.at[df.index[31], "close"] = 94.5
    df.at[df.index[31], "high"] = 101.8
    df.at[df.index[31], "low"] = 94.2

    obs = detect_order_blocks(df, atr_multiplier=1.5, lookback=30)
    bearish = [o for o in obs if o["direction"] == "bearish"]
    assert len(bearish) >= 1, "Expected at least one bearish order block"
    ob = bearish[0]
    assert 98.0 <= ob["low"] <= 100.0, f"OB low out of range: {ob['low']}"
    assert 101.0 <= ob["high"] <= 103.0, f"OB high out of range: {ob['high']}"
    print("[test] OB detects bearish order block on synthetic impulse OK")


def test_ob_invalidation():
    """
    A bullish OB should be absent when price closes below its low after formation.
    """
    from discovery.strategies.smc_strategy import detect_order_blocks

    n = 60
    df = _flat_df(n)

    # Plant bullish OB at bar 20 + impulse at bar 21
    df.at[df.index[20], "open"] = 101.0
    df.at[df.index[20], "close"] = 99.0
    df.at[df.index[20], "high"] = 101.5
    df.at[df.index[20], "low"] = 98.5  # OB low = 98.5

    df.at[df.index[21], "open"] = 99.0
    df.at[df.index[21], "close"] = 106.0
    df.at[df.index[21], "high"] = 106.5
    df.at[df.index[21], "low"] = 98.8

    # Price closes BELOW ob_low at bar 35 -> invalidates the bullish OB.
    # Keep open == close so bar 35 is not a bearish candle itself (avoids
    # a spurious bullish OB being created by the subsequent recovery bar).
    df.at[df.index[35], "open"] = 97.0
    df.at[df.index[35], "close"] = 97.0
    df.at[df.index[35], "high"] = 97.5
    df.at[df.index[35], "low"] = 96.5

    obs = detect_order_blocks(df, atr_multiplier=1.5, lookback=50)
    bullish = [o for o in obs if o["direction"] == "bullish"]
    assert len(bullish) == 0, "Bullish OB should be invalidated after price closes through its low"
    print("[test] OB invalidation removes OB when price closes through OK")


def test_ob_sorted_by_strength():
    """detect_order_blocks returns OBs sorted by descending strength."""
    from discovery.strategies.smc_strategy import detect_order_blocks

    df = _make_ohlcv(300, seed=42)
    obs = detect_order_blocks(df, atr_multiplier=1.2, lookback=50)
    if len(obs) >= 2:
        strengths = [o["strength"] for o in obs]
        assert strengths == sorted(strengths, reverse=True), "OBs must be sorted by strength desc"
    print("[test] OB sorted by descending strength OK")


# ---------------------------------------------------------------------------
# FVG tests
# ---------------------------------------------------------------------------

def test_fvg_detects_bullish_gap():
    """
    3-candle pattern where candle[i].low > candle[i-2].high should be bullish FVG.
    """
    from discovery.strategies.smc_strategy import detect_fair_value_gaps

    n = 10
    df = _flat_df(n, price=100.0)

    # Gap pattern at bars 4, 5, 6: bar 6 low > bar 4 high
    # Bar 4: high = 101
    df.at[df.index[4], "high"] = 101.0
    df.at[df.index[4], "low"] = 99.5
    df.at[df.index[4], "close"] = 100.5
    df.at[df.index[4], "open"] = 99.8

    # Bar 5: middle candle (doesn't matter for FVG definition)
    df.at[df.index[5], "high"] = 103.0
    df.at[df.index[5], "low"] = 101.5
    df.at[df.index[5], "close"] = 102.5
    df.at[df.index[5], "open"] = 101.8

    # Bar 6: low = 113 > bar 4 high = 101 -> bullish FVG; gap = 12 (>10% of ~102)
    df.at[df.index[6], "low"] = 113.0
    df.at[df.index[6], "high"] = 115.0
    df.at[df.index[6], "close"] = 114.0
    df.at[df.index[6], "open"] = 113.5

    fvgs = detect_fair_value_gaps(df, min_gap_pct=0.05)
    bullish = [f for f in fvgs if f["direction"] == "bullish"]
    assert len(bullish) >= 1, "Expected at least one bullish FVG"
    fvg = bullish[0]
    assert abs(fvg["upper"] - 113.0) < 0.01, f"FVG upper should be low[6]=113: {fvg['upper']}"
    assert abs(fvg["lower"] - 101.0) < 0.01, f"FVG lower should be high[4]=101: {fvg['lower']}"
    print("[test] FVG detects bullish gap on 3-candle pattern OK")


def test_fvg_detects_bearish_gap():
    """
    3-candle pattern where candle[i].high < candle[i-2].low should be bearish FVG.
    """
    from discovery.strategies.smc_strategy import detect_fair_value_gaps

    n = 10
    df = _flat_df(n, price=100.0)

    # Bar 4: low = 99
    df.at[df.index[4], "low"] = 99.0
    df.at[df.index[4], "high"] = 100.5
    df.at[df.index[4], "close"] = 99.5
    df.at[df.index[4], "open"] = 100.2

    # Bar 5: middle
    df.at[df.index[5], "high"] = 98.0
    df.at[df.index[5], "low"] = 96.0
    df.at[df.index[5], "close"] = 97.0
    df.at[df.index[5], "open"] = 97.5

    # Bar 6: high = 87 < bar 4 low = 99 -> bearish FVG; gap = 12 (>10% of ~97)
    df.at[df.index[6], "high"] = 87.0
    df.at[df.index[6], "low"] = 85.0
    df.at[df.index[6], "close"] = 86.0
    df.at[df.index[6], "open"] = 87.0

    fvgs = detect_fair_value_gaps(df, min_gap_pct=0.05)
    bearish = [f for f in fvgs if f["direction"] == "bearish"]
    assert len(bearish) >= 1, "Expected at least one bearish FVG"
    fvg = bearish[0]
    assert abs(fvg["lower"] - 87.0) < 0.01, f"FVG lower should be high[6]=87: {fvg['lower']}"
    assert abs(fvg["upper"] - 99.0) < 0.01, f"FVG upper should be low[4]=99: {fvg['upper']}"
    print("[test] FVG detects bearish gap on 3-candle pattern OK")


def test_fvg_filled_excluded():
    """
    A bullish FVG is marked filled and excluded when price closes back into the zone.
    """
    from discovery.strategies.smc_strategy import detect_fair_value_gaps

    n = 15
    df = _flat_df(n, price=100.0)

    # Bullish FVG: bar 4 high=101, bar 6 low=113 -> zone [101,113]
    df.at[df.index[4], "high"] = 101.0
    df.at[df.index[4], "low"] = 99.5
    df.at[df.index[4], "close"] = 100.5
    df.at[df.index[4], "open"] = 99.8

    df.at[df.index[5], "high"] = 103.0
    df.at[df.index[5], "low"] = 101.5
    df.at[df.index[5], "close"] = 102.5
    df.at[df.index[5], "open"] = 101.8

    df.at[df.index[6], "low"] = 113.0
    df.at[df.index[6], "high"] = 115.0
    df.at[df.index[6], "close"] = 114.0
    df.at[df.index[6], "open"] = 113.5

    # Price rallies from below into the gap at bar 8 (close >= lower=101 -> filled).
    df.at[df.index[8], "close"] = 112.0
    df.at[df.index[8], "high"] = 113.5
    df.at[df.index[8], "low"] = 111.5
    df.at[df.index[8], "open"] = 113.0

    fvgs = detect_fair_value_gaps(df, min_gap_pct=0.05)
    bullish = [f for f in fvgs if f["direction"] == "bullish"]
    assert len(bullish) == 0, "Bullish FVG should be marked filled when price enters zone"
    print("[test] FVG filled — excluded from results when price retraces OK")


def test_fvg_min_gap_filter():
    """Gaps smaller than min_gap_pct are excluded."""
    from discovery.strategies.smc_strategy import detect_fair_value_gaps

    n = 10
    df = _flat_df(n, price=100.0)

    # Very tiny gap: bar4.high=100.01, bar6.low=100.02 -> gap=0.01 (~0.01% of 100)
    df.at[df.index[4], "high"] = 100.01
    df.at[df.index[4], "low"] = 99.9
    df.at[df.index[4], "close"] = 99.95
    df.at[df.index[4], "open"] = 99.92

    df.at[df.index[5], "close"] = 100.015
    df.at[df.index[5], "open"] = 100.005
    df.at[df.index[5], "high"] = 100.02
    df.at[df.index[5], "low"] = 100.0

    df.at[df.index[6], "low"] = 100.02
    df.at[df.index[6], "high"] = 100.05
    df.at[df.index[6], "close"] = 100.04
    df.at[df.index[6], "open"] = 100.025

    fvgs = detect_fair_value_gaps(df, min_gap_pct=0.05)  # 5% threshold
    assert len(fvgs) == 0, "Tiny gap should be filtered by min_gap_pct"
    print("[test] FVG min_gap_pct filter correctly excludes small gaps OK")


def test_fvg_sorted_by_recency():
    """detect_fair_value_gaps returns FVGs sorted most recent first."""
    from discovery.strategies.smc_strategy import detect_fair_value_gaps

    df = _make_ohlcv(500, seed=13)
    fvgs = detect_fair_value_gaps(df, min_gap_pct=0.05)
    if len(fvgs) >= 2:
        indices = [f["bar_index"] for f in fvgs]
        assert indices == sorted(indices, reverse=True), "FVGs must be sorted by recency desc"
    print("[test] FVG sorted by descending recency OK")


# ---------------------------------------------------------------------------
# Signal generation / position_vector tests
# ---------------------------------------------------------------------------

def test_param_grid_count():
    """param_grid returns exactly 54 combos (3 x 3 x 3 x 2)."""
    from discovery.strategies.smc_strategy import SMCPositionStrategy
    s = SMCPositionStrategy()
    grid = s.param_grid()
    assert len(grid) == 54, f"Expected 54 combos, got {len(grid)}"
    print("[test] param_grid returns 54 combos OK")


def test_position_vector_shape_and_values():
    """position_vector returns array of len(df) with values in {-1, 0, +1}."""
    from discovery.strategies.smc_strategy import SMCPositionStrategy
    s = SMCPositionStrategy()
    df = _make_ohlcv(600, seed=99)
    for params in s.param_grid()[:3]:
        pos = s.position_vector(df, params)
        assert pos.shape[0] == len(df), f"Wrong length for params {params}"
        assert set(np.unique(pos)).issubset({-1.0, 0.0, 1.0}), (
            f"Bad values for params {params}: {np.unique(pos)}"
        )
    print("[test] position_vector shape and {-1,0,+1} contract OK")


def test_position_vector_short_data_returns_zeros():
    """position_vector returns all zeros when df is too short for warmup."""
    from discovery.strategies.smc_strategy import SMCPositionStrategy
    s = SMCPositionStrategy()
    df = _make_ohlcv(100, seed=1)  # too short for EMA200
    params = s.param_grid()[0]
    pos = s.position_vector(df, params)
    assert np.all(pos == 0.0), "Short data should produce all-zero vector"
    print("[test] position_vector all-zero on short data OK")


def test_position_vector_scoreable():
    """position_vector output is scoreable by calculate_objective_score."""
    from discovery.strategies.smc_strategy import SMCPositionStrategy
    from discovery.permutation_framework import calculate_log_returns, calculate_objective_score

    s = SMCPositionStrategy()
    df = _make_ohlcv(600, seed=5)
    params = s.param_grid()[0]
    pos = s.position_vector(df, params)
    returns = calculate_log_returns(df["close"].to_numpy())
    score = calculate_objective_score(pos, returns, "sharpe")
    assert np.isfinite(score) or score == 0.0, f"Score should be finite: {score}"
    print("[test] position_vector output scoreable by calculate_objective_score OK")


# ---------------------------------------------------------------------------
# Registry test
# ---------------------------------------------------------------------------

def test_smc_registered_in_framework():
    """SMCPositionStrategy is registered in the MCPT worker registry."""
    from discovery.permutation_framework import _STRATEGY_REGISTRY
    from discovery.strategies.smc_strategy import SMCPositionStrategy

    assert SMCPositionStrategy.name in _STRATEGY_REGISTRY, (
        f"{SMCPositionStrategy.name} not found in _STRATEGY_REGISTRY"
    )
    assert _STRATEGY_REGISTRY[SMCPositionStrategy.name] is SMCPositionStrategy
    print("[test] SMCPositionStrategy registered in MCPT registry OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all():
    tests = [
        test_ob_detects_bullish_ob_on_synthetic_impulse,
        test_ob_detects_bearish_ob_on_synthetic_impulse,
        test_ob_invalidation,
        test_ob_sorted_by_strength,
        test_fvg_detects_bullish_gap,
        test_fvg_detects_bearish_gap,
        test_fvg_filled_excluded,
        test_fvg_min_gap_filter,
        test_fvg_sorted_by_recency,
        test_param_grid_count,
        test_position_vector_shape_and_values,
        test_position_vector_short_data_returns_zeros,
        test_position_vector_scoreable,
        test_smc_registered_in_framework,
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
