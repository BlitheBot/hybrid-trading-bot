"""
Unit tests for the strategy decay monitor.

Covers the pure decay-ratio / tier-classification logic and the helper math
(annualized Sharpe, profit factor, trailing loss streak) — no DB required.

Run directly:   python discovery/test_decay_monitor.py
Or via pytest:  pytest discovery/test_decay_monitor.py
"""
import numpy as np

from config import Config
from discovery.decay_monitor import (
    CRITICAL,
    DECAYING,
    DEGRADED,
    HEALTHY,
    _annualized_sharpe,
    _profit_factor,
    _trailing_loss_streak,
    classify_decay,
)


def test_healthy():
    d = classify_decay(live_sharpe=1.8, backtested_sharpe=2.0, n_signals=40)
    assert d["status"] == HEALTHY, d
    assert d["position_multiplier"] == 1.0
    assert abs(d["decay_ratio"] - 0.9) < 1e-9
    assert not d["is_decaying"] and not d["is_critical"]
    print("[test] HEALTHY tier ✓")


def test_degraded():
    # ratio = 1.3 / 2.0 = 0.65 → DEGRADED
    d = classify_decay(live_sharpe=1.3, backtested_sharpe=2.0, n_signals=40)
    assert d["status"] == DEGRADED, d
    assert d["position_multiplier"] == Config.DECAY_DEGRADED_MULT
    print("[test] DEGRADED tier ✓")


def test_decaying():
    # ratio = 0.6 / 2.0 = 0.3 < 0.5 with >=30 signals → DECAYING
    d = classify_decay(live_sharpe=0.6, backtested_sharpe=2.0, n_signals=35)
    assert d["status"] == DECAYING, d
    assert d["position_multiplier"] == Config.DECAY_DECAYING_MULT
    assert d["is_decaying"]
    print("[test] DECAYING tier ✓")


def test_critical_overrides_ratio():
    # Negative recent live Sharpe → CRITICAL even though ratio band would be lower tier.
    d = classify_decay(live_sharpe=-0.4, backtested_sharpe=2.0, n_signals=40,
                       live_sharpe_recent=-0.4)
    assert d["status"] == CRITICAL, d
    assert d["position_multiplier"] == 0.0
    assert d["is_critical"]
    print("[test] CRITICAL overrides ratio ✓")


def test_critical_needs_min_signals():
    # Negative Sharpe but too few signals for the critical window → not critical.
    d = classify_decay(live_sharpe=-0.4, backtested_sharpe=2.0,
                       n_signals=Config.DECAY_CRITICAL_MIN_SIGNALS - 1,
                       live_sharpe_recent=-0.4)
    assert d["status"] != CRITICAL, d
    assert not d["is_critical"]
    print("[test] CRITICAL requires min signals ✓")


def test_no_baseline_is_healthy_unless_losing():
    # No positive backtested baseline → not penalized (HEALTHY) unless losing.
    d = classify_decay(live_sharpe=0.2, backtested_sharpe=None, n_signals=40)
    assert d["status"] == HEALTHY and d["decay_ratio"] is None, d
    d2 = classify_decay(live_sharpe=-0.2, backtested_sharpe=0.0, n_signals=40,
                        live_sharpe_recent=-0.2)
    assert d2["status"] == CRITICAL, d2
    print("[test] no-baseline handling ✓")


def test_decaying_requires_30_signals():
    # ratio < 0.5 but fewer than DECAY_MIN_SIGNALS → falls back to DEGRADED band.
    d = classify_decay(live_sharpe=0.6, backtested_sharpe=2.0,
                       n_signals=Config.DECAY_MIN_SIGNALS - 1)
    assert d["status"] == DEGRADED, d
    print("[test] DECAYING requires >=30 signals ✓")


def test_annualized_sharpe_sign():
    winners = np.array([0.01] * 20 + [-0.005] * 5)
    losers = -winners
    assert _annualized_sharpe(winners, 1.0) > 0
    assert _annualized_sharpe(losers, 1.0) < 0
    assert _annualized_sharpe(np.array([0.01]), 1.0) == 0.0  # too few
    print("[test] annualized Sharpe sign ✓")


def test_annualized_sharpe_capped():
    # Near-identical winning returns (std≈0) must not blow up — capped at +50.
    identical = np.full(30, 0.02)
    s = _annualized_sharpe(identical, 1.0)
    assert s == 50.0, s
    identical_loss = np.full(30, -0.02)
    assert _annualized_sharpe(identical_loss, 1.0) == -50.0
    assert _annualized_sharpe(np.zeros(30), 1.0) == 0.0
    print("[test] annualized Sharpe capped ✓")


def test_profit_factor():
    r = np.array([0.02, 0.02, -0.01, -0.01, -0.01])
    assert abs(_profit_factor(r) - (0.04 / 0.03)) < 1e-9
    assert _profit_factor(np.array([0.01, 0.02])) > 1e4  # no losses → large finite PF
    assert _profit_factor(np.array([-0.01, -0.02])) == 0.0  # no gains → 0
    print("[test] profit factor ✓")


def test_trailing_loss_streak():
    assert _trailing_loss_streak(np.array([0.01, -0.01, -0.02, -0.01])) == 3
    assert _trailing_loss_streak(np.array([-0.01, 0.02])) == 0
    assert _trailing_loss_streak(np.array([-0.01, -0.01])) == 2
    print("[test] trailing loss streak ✓")


def _run_all():
    tests = [
        test_healthy,
        test_degraded,
        test_decaying,
        test_critical_overrides_ratio,
        test_critical_needs_min_signals,
        test_no_baseline_is_healthy_unless_losing,
        test_decaying_requires_30_signals,
        test_annualized_sharpe_sign,
        test_annualized_sharpe_capped,
        test_profit_factor,
        test_trailing_loss_streak,
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
