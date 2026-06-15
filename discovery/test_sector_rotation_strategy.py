"""
Unit tests for the Sector Rotation discovery family (Strategy Family 8, Task 6).

All offline (stubbed ETF history — no network). Verifies:
- the parameter grid is the documented 27 combos,
- position_vector is all-flat with no sector feed (fail-open),
- it goes LONG when the sector is top-ranked and the stock leads it,
- it goes SHORT when the sector is bottom-ranked and the stock lags it,
- volume confirmation blocks entries below threshold,
- attach_sector_rotation ranks the strongest sector ETF #1,
- the family is registered in the MCPT registry.

Run:  python -m discovery.test_sector_rotation_strategy
"""
import numpy as np
import pandas as pd

import discovery.data_feeds.sector_rotation_data as srd
from discovery.permutation_framework import _STRATEGY_REGISTRY
from discovery.strategies.sector_rotation_strategy import SectorRotationPositionStrategy


def _base_df(n=60, slope=0.5):
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    close = 100.0 + slope * np.arange(n)
    return pd.DataFrame({"close": close, "volume": np.full(n, 1e6)}, index=idx)


def test_param_grid_27():
    grid = SectorRotationPositionStrategy().param_grid()
    assert len(grid) == 27, len(grid)
    assert all({"rs_period", "top_n_sectors", "volume_threshold"} <= set(p) for p in grid)
    print("[test] sector-rotation param grid = 27 OK")


def test_all_flat_without_feed():
    s = SectorRotationPositionStrategy()
    df = _base_df()  # no sector_* columns
    pos = s.position_vector(df, {"rs_period": 20, "top_n_sectors": 3, "volume_threshold": 1.2})
    assert (pos == 0.0).all(), "no sector feed must yield all-flat"
    print("[test] sector-rotation all-flat without feed OK")


def test_long_when_top_sector_and_leading():
    s = SectorRotationPositionStrategy()
    df = _base_df(n=60, slope=1.0)  # strong stock uptrend → high own return
    n = len(df)
    df["sector_rank_20"] = np.full(n, 2.0)        # top-3
    df["sector_ret_20"] = np.full(n, 0.05)        # sector +5% over window
    df["sector_vol_ratio"] = np.full(n, 1.5)      # volume confirmed
    pos = s.position_vector(df, {"rs_period": 20, "top_n_sectors": 3, "volume_threshold": 1.2})
    assert pos.max() == 1.0, "should go long: top sector + stock leading + volume ok"
    assert pos.min() >= 0.0, "no shorts expected here"
    print("[test] sector-rotation LONG on top sector + leading OK")


def test_short_when_bottom_sector_and_lagging():
    s = SectorRotationPositionStrategy()
    df = _base_df(n=60, slope=-0.8)  # stock downtrend → very negative own return
    n = len(df)
    df["sector_rank_20"] = np.full(n, 11.0)       # bottom sector
    df["sector_ret_20"] = np.full(n, 0.0)         # sector flat; stock lags it (negative)
    df["sector_vol_ratio"] = np.full(n, 1.5)
    pos = s.position_vector(df, {"rs_period": 20, "top_n_sectors": 3, "volume_threshold": 1.2})
    assert pos.min() == -1.0, "should go short: bottom sector + stock lagging"
    print("[test] sector-rotation SHORT on bottom sector + lagging OK")


def test_volume_gate_blocks_entry():
    s = SectorRotationPositionStrategy()
    df = _base_df(n=60, slope=1.0)
    n = len(df)
    df["sector_rank_20"] = np.full(n, 1.0)
    df["sector_ret_20"] = np.full(n, 0.05)
    df["sector_vol_ratio"] = np.full(n, 1.0)      # below 1.2 threshold → no entry
    pos = s.position_vector(df, {"rs_period": 20, "top_n_sectors": 3, "volume_threshold": 1.2})
    assert (pos == 0.0).all(), "low sector volume must block entries"
    print("[test] sector-rotation volume gate blocks entry OK")


def test_attach_ranks_strongest_first():
    idx = pd.date_range("2023-01-02", periods=80, freq="B")
    srd._etf_history.clear()
    for etf in srd.SECTOR_ETFS:
        slope = 0.5 if etf == "XLK" else (0.0 if etf == "XLU" else 0.1)
        srd._etf_history[etf] = pd.DataFrame(
            {"close": 100 + slope * np.arange(80), "volume": np.full(80, 1e6)}, index=idx
        )
    srd.SYMBOL_TO_ETF["FAKEK"] = "XLK"
    bars = pd.DataFrame({"close": np.linspace(50, 70, 80), "volume": np.full(80, 5e5)}, index=idx)
    enr = srd.attach_sector_rotation(bars, "FAKEK")
    assert "sector_rank_20" in enr.columns
    assert enr["sector_rank_20"].dropna().iloc[-1] == 1.0, "XLK should rank #1"
    print("[test] attach_sector_rotation ranks strongest first OK")


def test_registered():
    assert (_STRATEGY_REGISTRY.get(SectorRotationPositionStrategy.name)
            is SectorRotationPositionStrategy)
    assert hasattr(SectorRotationPositionStrategy, "enrich")
    print("[test] sector-rotation registered in MCPT registry OK")


def _run_all():
    tests = [
        test_param_grid_27,
        test_all_flat_without_feed,
        test_long_when_top_sector_and_leading,
        test_short_when_bottom_sector_and_lagging,
        test_volume_gate_blocks_entry,
        test_attach_ranks_strongest_first,
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
