"""
Unit tests for the crypto momentum scalp strategy (Task 6).

Run:  python -m strategies.test_crypto_momentum_strategy
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz

import pandas_ta as ta

from config import Config
from strategies.crypto_momentum_strategy import CryptoMomentumStrategy


def _bars(closes, volumes=None, symbol="BTC/USD"):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    if volumes is None:
        volumes = np.full(n, 1000.0)
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    high = closes * 1.001
    low = closes * 0.999
    return pd.DataFrame(
        {"open": closes, "high": high, "low": low, "close": closes,
         "volume": np.asarray(volumes, dtype=float), "symbol": symbol},
        index=idx,
    )


def _bullish_cross_closes():
    """A close series truncated so its LAST bar is a fresh EMA9>EMA21 bullish cross."""
    down = np.linspace(100, 90, 50)
    up = np.linspace(90, 115, 25)
    closes = np.concatenate([down, up])
    fast = ta.ema(pd.Series(closes), length=Config.CRYPTO_MOMENTUM_EMA_FAST)
    slow = ta.ema(pd.Series(closes), length=Config.CRYPTO_MOMENTUM_EMA_SLOW)
    for i in range(Config.CRYPTO_MOMENTUM_EMA_SLOW + 1, len(closes)):
        if fast.iloc[i - 1] <= slow.iloc[i - 1] and fast.iloc[i] > slow.iloc[i]:
            return closes[: i + 1]
    raise AssertionError("no bullish cross found in synthetic series")


def test_no_signal_when_insufficient_bars():
    s = CryptoMomentumStrategy("t")
    assert s.generate_signals(_bars(np.linspace(100, 101, 10))) is None
    print("[test] insufficient bars -> None OK")


def test_bullish_cross_generates_buy():
    s = CryptoMomentumStrategy("t")
    closes = _bullish_cross_closes()
    vols = np.full(len(closes), 1000.0)
    vols[-1] = 5000.0  # volume surge confirms
    sig = s.generate_signals(_bars(closes, vols))
    assert sig is not None and sig["signal"] == "buy"
    # R/R ~ 2.0 from 1.5x/3x ATR.
    rr = (sig["target_price"] - sig["entry_price"]) / (sig["entry_price"] - sig["stop_price"])
    assert abs(rr - 2.0) < 0.01
    print(f"[test] bullish cross -> buy, R/R={rr:.2f} OK")


def test_volume_gate_blocks():
    s = CryptoMomentumStrategy("t")
    closes = _bullish_cross_closes()
    vols = np.full(len(closes), 1000.0)
    vols[-1] = 1000.0  # no surge -> blocked
    assert s.generate_signals(_bars(closes, vols)) is None
    print("[test] volume gate blocks low-volume cross OK")


def test_cooldown_throttle():
    s = CryptoMomentumStrategy("t")
    # Pretend a signal just fired for BTC/USD at price 100.
    s._last_signal["BTC/USD"] = {"time": datetime.now(pytz.utc), "price": 100.0}
    closes = _bullish_cross_closes()
    vols = np.full(len(closes), 1000.0)
    vols[-1] = 5000.0
    assert s.generate_signals(_bars(closes, vols)) is None  # within cooldown
    # Expire the cooldown -> signal allowed again.
    s._last_signal["BTC/USD"]["time"] = datetime.now(pytz.utc) - timedelta(
        minutes=Config.CRYPTO_MOMENTUM_COOLDOWN_MINUTES + 1
    )
    s._last_signal["BTC/USD"]["price"] = 50.0  # large move so min-move passes
    assert s.generate_signals(_bars(closes, vols)) is not None
    print("[test] cooldown throttle OK")


def test_min_move_throttle():
    s = CryptoMomentumStrategy("t")
    closes = _bullish_cross_closes()
    last_price = float(closes[-1])
    # Last signal long ago (no cooldown) but at essentially the same price.
    s._last_signal["BTC/USD"] = {
        "time": datetime.now(pytz.utc) - timedelta(hours=1),
        "price": last_price * (1 - Config.CRYPTO_MOMENTUM_MIN_MOVE_PCT / 2),
    }
    vols = np.full(len(closes), 1000.0)
    vols[-1] = 5000.0
    assert s.generate_signals(_bars(closes, vols)) is None
    print("[test] min-move throttle OK")


def _run_all():
    tests = [
        test_no_signal_when_insufficient_bars,
        test_bullish_cross_generates_buy,
        test_volume_gate_blocks,
        test_cooldown_throttle,
        test_min_move_throttle,
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
