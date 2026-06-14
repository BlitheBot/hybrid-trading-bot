"""
Unit tests for the enhanced Performance Brain math (Task 7).

Run:  python test_performance_brain.py
"""
import performance_brain as pb


def test_momentum_hot_cold_neutral():
    assert pb.momentum_multiplier([1, 2, 3, -1, 0.5]) == pb.HOT     # 4 wins
    assert pb.momentum_multiplier([-1, -2, -3, 1, -0.5]) == pb.COLD  # 4 losses
    assert pb.momentum_multiplier([1, 1, -1, -1]) == pb.NEUTRAL      # 2 wins / 2 losses
    print("[test] momentum hot/cold/neutral OK")


def test_momentum_exactly_three():
    assert pb.momentum_multiplier([1, 1, 1, -1, -1]) == pb.HOT       # exactly 3 wins
    assert pb.momentum_multiplier([-1, -1, -1, 1, 1]) == pb.COLD     # exactly 3 losses
    print("[test] momentum threshold at 3 OK")


def test_momentum_thin_data_neutral():
    assert pb.momentum_multiplier([1, 1]) == pb.NEUTRAL              # < 3 signals
    assert pb.momentum_multiplier([]) == pb.NEUTRAL
    print("[test] momentum thin data neutral OK")


def test_regime_bonus():
    assert pb.regime_bonus(0.5, 10) == 0.1     # profitable + enough samples
    assert pb.regime_bonus(-0.5, 10) == 0.0    # unprofitable
    assert pb.regime_bonus(0.5, 2) == 0.0      # too few samples
    assert pb.regime_bonus(None, 10) == 0.0    # no data
    print("[test] regime bonus OK")


def test_time_of_day_bonus():
    # Morning stronger; currently in morning -> +0.1.
    assert pb.time_of_day_bonus(1.0, 5, 0.2, 5, current_minute=600) == 0.1
    # Currently in morning but afternoon stronger -> -0.1.
    assert pb.time_of_day_bonus(0.2, 5, 1.0, 5, current_minute=600) == -0.1
    # Afternoon window, afternoon stronger -> +0.1.
    assert pb.time_of_day_bonus(0.2, 5, 1.0, 5, current_minute=900) == 0.1
    # Outside both windows -> 0.
    assert pb.time_of_day_bonus(1.0, 5, 0.2, 5, current_minute=720) == 0.0
    # Thin data -> 0.
    assert pb.time_of_day_bonus(1.0, 1, 0.2, 5, current_minute=600) == 0.0
    print("[test] time-of-day bonus OK")


def test_combine_clamps():
    assert abs(pb.combine(1.2, 0.1, 0.1) - 1.4) < 1e-9
    assert pb.combine(1.2, 0.1, 0.1 + 0.5) <= 1.5     # clamp high
    assert abs(pb.combine(0.7, -0.1, -0.5) - 0.5) < 1e-9   # clamp low
    print("[test] combine clamps to [0.5,1.5] OK")


def _run_all():
    tests = [
        test_momentum_hot_cold_neutral,
        test_momentum_exactly_three,
        test_momentum_thin_data_neutral,
        test_regime_bonus,
        test_time_of_day_bonus,
        test_combine_clamps,
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
