"""
Unit tests for the short-interest-momentum discovery family (family 7),
redesigned to gate on real FINRA short-interest LEVEL data (short_interest_levels)
instead of the daily short-VOLUME ratio (finra_historical.si_change).

Verifies:
- param_grid() returns the 3-combo grid (rsi_period only — the squeeze/short
  thresholds are now fixed constants, not swept params),
- position_vector() returns a {-1,0,+1} array of len(df),
- with no si_pct_of_float column the family degrades to all-flat,
- the legacy si_change column alone (even at an extreme value) can NOT trigger
  a squeeze long anymore — the core regression guard for the audit fix,
- an elevated SI level (pct-of-float OR days-to-cover) + a real up-day on
  volume produces a squeeze long,
- rising SI level + bearish RSI + downtrend produces a short (and RSI > 55
  alone, without a rising SI level, does NOT — the old contradiction is gone),
- the family is registered in the MCPT registry,
- the FINRA historical (daily-ratio) parser still works — kept as a secondary
  signal, not removed.

Run:  python -m discovery.test_short_interest_momentum_strategy
"""
import numpy as np
import pandas as pd

from discovery.permutation_framework import _STRATEGY_REGISTRY
from discovery.strategies.short_interest_momentum_strategy import (
    SI_COLUMN,
    ShortInterestMomentumPositionStrategy,
)
from discovery.data_feeds.finra_short_interest_levels import (
    SI_LEVEL_PCT_COLUMN,
    DAYS_TO_COVER_COLUMN,
    SI_LEVEL_RISING_COLUMN,
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


def test_grid_is_3():
    s = ShortInterestMomentumPositionStrategy()
    assert len(s.param_grid()) == 3
    print("[test] short-momentum grid == 3 (rsi_period only) OK")


def test_vector_shape_and_values():
    s = ShortInterestMomentumPositionStrategy()
    df = _make_ohlcv()
    df[SI_LEVEL_PCT_COLUMN] = 0.0
    df[DAYS_TO_COVER_COLUMN] = 0.0
    df[SI_LEVEL_RISING_COLUMN] = 0.0
    for params in s.param_grid():
        pos = s.position_vector(df, params)
        assert pos.shape[0] == len(df)
        assert set(np.unique(pos)).issubset({-1.0, 0.0, 1.0})
    print("[test] short-momentum vector shape/value-set OK")


def test_flat_without_level_column():
    s = ShortInterestMomentumPositionStrategy()
    df = _make_ohlcv()
    pos = s.position_vector(df, s.param_grid()[0])
    assert np.all(pos == 0.0)
    print("[test] short-momentum flat-without-LEVEL-data OK")


def test_si_change_alone_cannot_trigger_squeeze_long():
    """Regression guard for the audit fix: the legacy daily-ratio si_change
    column, even at an extreme value that would have fired the OLD
    si_change < -threshold condition, must NOT be able to open a long when
    the real SI level (pct-of-float / days-to-cover) precondition isn't met."""
    s = ShortInterestMomentumPositionStrategy()
    n = 320
    rng = np.random.default_rng(9)
    close = np.empty(n)
    close[0] = 100.0
    for i in range(1, n):
        if i == 270:
            close[i] = close[i - 1] * 1.03  # +3% pop, would satisfy the trigger
        else:
            close[i] = close[i - 1] * np.exp(rng.normal(0.0002, 0.006))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    vol = np.abs(rng.normal(1_000_000.0, 100_000.0, n))
    vol[270] = 8_000_000.0  # heavy volume, would satisfy the trigger
    df = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": vol},
        index=idx,
    )
    # SI level precondition explicitly NOT met.
    df[SI_LEVEL_PCT_COLUMN] = 0.02   # well below the 10%-of-float threshold
    df[DAYS_TO_COVER_COLUMN] = 1.0   # well below the 5-day threshold
    df[SI_LEVEL_RISING_COLUMN] = 0.0
    # Legacy daily-ratio signal at an extreme value that used to gate entry alone.
    si_change = np.zeros(n)
    si_change[265:275] = -0.50
    df[SI_COLUMN] = si_change

    for params in s.param_grid():
        pos = s.position_vector(df, params)
        assert pos.max() < 1.0, (
            "si_change alone must never trigger a squeeze long — "
            "it is secondary/informational only per the redesign"
        )
    print("[test] si_change alone cannot trigger squeeze long OK (audit fix verified)")


def _squeeze_price_path(n: int = 320, pop_idx: int = 270, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """A mildly noisy (realistic — some down-days, so RSI never degenerates to
    NaN from a zero-ever-loss streak) price path with a clean +3% pop on heavy
    volume at pop_idx."""
    rng = np.random.default_rng(seed)
    close = np.empty(n)
    close[0] = 100.0
    for i in range(1, n):
        if i == pop_idx:
            close[i] = close[i - 1] * 1.03  # +3% squeeze pop
        else:
            close[i] = close[i - 1] * np.exp(rng.normal(0.0002, 0.006))
    vol = np.abs(rng.normal(1_000_000.0, 100_000.0, n))
    vol[pop_idx] = 8_000_000.0  # heavy volume on the pop
    return close, vol


def test_squeeze_long_on_elevated_si_level():
    s = ShortInterestMomentumPositionStrategy()
    n = 320
    close, vol = _squeeze_price_path(n)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": vol},
        index=idx,
    )
    # SI level precondition genuinely met: 15% of float (well above 10%).
    df[SI_LEVEL_PCT_COLUMN] = 0.15
    df[DAYS_TO_COVER_COLUMN] = 2.0
    df[SI_LEVEL_RISING_COLUMN] = 0.0

    took_long = False
    for params in s.param_grid():
        pos = s.position_vector(df, params)
        if pos.max() == 1.0:
            took_long = True
            break
    assert took_long, "expected a squeeze long: elevated SI level + up-day on volume"
    print("[test] short-momentum squeeze long on elevated SI level OK")


def test_squeeze_long_via_days_to_cover_alone():
    """The precondition is an OR: days_to_cover > 5 alone (even with
    si_pct_of_float unavailable/NaN, e.g. no float_shares data yet) must be
    sufficient — days_to_cover never depends on Finnhub float data."""
    s = ShortInterestMomentumPositionStrategy()
    n = 320
    close, vol = _squeeze_price_path(n)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": vol},
        index=idx,
    )
    df[SI_LEVEL_PCT_COLUMN] = np.nan   # float_shares unknown
    df[DAYS_TO_COVER_COLUMN] = 7.5     # above the 5-day threshold
    df[SI_LEVEL_RISING_COLUMN] = 0.0

    took_long = any(
        s.position_vector(df, params).max() == 1.0 for params in s.param_grid()
    )
    assert took_long, "days_to_cover alone should satisfy the squeeze precondition"
    print("[test] short-momentum squeeze long via days_to_cover-only OK")


def test_short_on_rising_si_level_downtrend():
    s = ShortInterestMomentumPositionStrategy()
    df = _make_ohlcv(seed=3, drift=-0.0015)  # downtrend → EMA50 < EMA200
    df[SI_LEVEL_PCT_COLUMN] = 0.03
    df[DAYS_TO_COVER_COLUMN] = 1.0
    df[SI_LEVEL_RISING_COLUMN] = 1.0  # short interest rising this period vs last

    took_short = False
    for params in s.param_grid():
        pos = s.position_vector(df, params)
        if pos.min() == -1.0:
            took_short = True
            break
    assert took_short, "expected a short on rising SI level in a downtrend with bearish RSI"
    print("[test] short-momentum shorts on rising SI level + downtrend OK")


def test_short_requires_rising_si_not_just_downtrend():
    """The old RSI>55 contradiction is gone, but the short must still require
    si_level_rising > 0 — a downtrend alone (SI flat/falling) must not short."""
    s = ShortInterestMomentumPositionStrategy()
    df = _make_ohlcv(seed=3, drift=-0.0015)
    df[SI_LEVEL_PCT_COLUMN] = 0.03
    df[DAYS_TO_COVER_COLUMN] = 1.0
    df[SI_LEVEL_RISING_COLUMN] = 0.0  # NOT rising

    for params in s.param_grid():
        pos = s.position_vector(df, params)
        assert pos.min() > -1.0, "a downtrend without rising SI level must not trigger a short"
    print("[test] short-momentum requires si_level_rising, not just a downtrend OK")


def test_registered():
    assert (_STRATEGY_REGISTRY.get(ShortInterestMomentumPositionStrategy.name)
            is ShortInterestMomentumPositionStrategy)
    print("[test] short-momentum registered in MCPT registry OK")


def test_finra_daily_ratio_parser_still_works():
    """The legacy daily short-volume-ratio parser (finra_historical.py) is
    unchanged — still used as a secondary/informational feed via enrich()."""
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
    print("[test] FINRA daily-ratio (secondary) parser OK")


def _run_all():
    tests = [
        test_grid_is_3,
        test_vector_shape_and_values,
        test_flat_without_level_column,
        test_si_change_alone_cannot_trigger_squeeze_long,
        test_squeeze_long_on_elevated_si_level,
        test_squeeze_long_via_days_to_cover_alone,
        test_short_on_rising_si_level_downtrend,
        test_short_requires_rising_si_not_just_downtrend,
        test_registered,
        test_finra_daily_ratio_parser_still_works,
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
