"""
Unit tests for the live week-over-week short-interest confirmation bonus (Task 4).

Covers the pure decision helper used by bot._process_symbol / _execute_short.

Run:  python -m discovery.test_short_interest_live
"""
from discovery.data_feeds.finra_historical import short_interest_size_adjustment

RISING = 0.10
FALLING = 0.15
SHORT_BONUS = 0.2
LONG_BONUS = 0.3


def test_short_confirmed_by_rising_si():
    adj = short_interest_size_adjustment("sell", 0.20, RISING, FALLING, SHORT_BONUS, LONG_BONUS)
    assert adj == SHORT_BONUS
    print("[test] short confirmed by rising SI -> +0.2x OK")


def test_long_squeeze_by_falling_si():
    adj = short_interest_size_adjustment("buy", -0.25, RISING, FALLING, SHORT_BONUS, LONG_BONUS)
    assert adj == LONG_BONUS
    print("[test] long squeeze by falling SI -> +0.3x OK")


def test_no_bonus_below_threshold():
    assert short_interest_size_adjustment("sell", 0.05, RISING, FALLING, SHORT_BONUS, LONG_BONUS) == 0.0
    assert short_interest_size_adjustment("buy", -0.10, RISING, FALLING, SHORT_BONUS, LONG_BONUS) == 0.0
    print("[test] no bonus below threshold OK")


def test_wrong_direction_no_bonus():
    # Rising SI on a long, or falling SI on a short, must not add size.
    assert short_interest_size_adjustment("buy", 0.30, RISING, FALLING, SHORT_BONUS, LONG_BONUS) == 0.0
    assert short_interest_size_adjustment("sell", -0.30, RISING, FALLING, SHORT_BONUS, LONG_BONUS) == 0.0
    print("[test] wrong-direction SI change -> no bonus OK")


def test_none_is_fail_open():
    assert short_interest_size_adjustment("sell", None, RISING, FALLING, SHORT_BONUS, LONG_BONUS) == 0.0
    assert short_interest_size_adjustment("buy", None, RISING, FALLING, SHORT_BONUS, LONG_BONUS) == 0.0
    print("[test] None WoW change -> fail-open 0.0 OK")


def _run_all():
    tests = [
        test_short_confirmed_by_rising_si,
        test_long_squeeze_by_falling_si,
        test_no_bonus_below_threshold,
        test_wrong_direction_no_bonus,
        test_none_is_fail_open,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"[FAIL] {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return failures


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_all() else 0)
