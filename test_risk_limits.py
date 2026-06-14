"""
Unit tests for the Risk Management Upgrade decision functions (Task 8).

Run:  python test_risk_limits.py
"""
import risk_limits as rl

SECTORS = {
    "JPM": "Financials", "V": "Financials", "BRK.B": "Financials",
    "COST": "Consumer/Defensive", "PG": "Consumer/Defensive", "SPY": "Broad market",
}


def test_sector_blocks_overconcentration():
    # 70% of exposure already in Financials -> a new Financials name is blocked.
    positions = [("JPM", 7000.0), ("COST", 3000.0)]
    ok, sector, share, _ = rl.sector_exposure_ok(SECTORS, "V", positions, 30.0)
    assert not ok and sector == "Financials" and share >= 30.0
    print(f"[test] sector over-concentration blocked ({share:.0f}%) OK")


def test_sector_allows_when_under_cap():
    positions = [("JPM", 2000.0), ("COST", 8000.0)]
    ok, _sector, share, _ = rl.sector_exposure_ok(SECTORS, "V", positions, 30.0)
    assert ok and share < 30.0
    print("[test] sector under cap allowed OK")


def test_sector_unknown_symbol_allowed():
    ok, sector, _share, _ = rl.sector_exposure_ok(SECTORS, "NVDA", [("JPM", 5000.0)], 30.0)
    assert ok and sector == "unknown"
    print("[test] unknown sector allowed OK")


def test_sector_no_positions_allowed():
    ok, _s, share, _ = rl.sector_exposure_ok(SECTORS, "JPM", [], 30.0)
    assert ok and share == 0.0
    print("[test] no existing exposure allowed OK")


def test_single_position_cap():
    # 5% of $100k / $50 price = $5000 / 50 = 100 shares.
    assert rl.single_position_share_cap(100_000, 50.0, 5.0) == 100
    assert rl.single_position_share_cap(0, 50.0, 5.0) == 0
    assert rl.single_position_share_cap(100_000, 0, 5.0) == 0
    print("[test] single-position share cap OK")


def test_weekly_loss_reduction():
    assert rl.weekly_loss_reduction(-4.0, -3.0, 0.5) == 0.5   # breached
    assert rl.weekly_loss_reduction(-2.0, -3.0, 0.5) == 1.0   # within
    assert rl.weekly_loss_reduction(1.0, -3.0, 0.5) == 1.0    # profitable
    print("[test] weekly loss reduction OK")


def test_consecutive_loss_logic():
    assert rl.count_leading_losses([-1, -2, -3, 1, -1]) == 3   # stops at the win
    assert rl.count_leading_losses([1, -1, -1]) == 0           # most recent is a win
    assert rl.count_leading_losses([-1, 0, -1]) == 3           # 0 counts as a loss
    assert rl.consecutive_loss_tripped(5, 5) is True
    assert rl.consecutive_loss_tripped(4, 5) is False
    print("[test] consecutive-loss logic OK")


def _run_all():
    tests = [
        test_sector_blocks_overconcentration,
        test_sector_allows_when_under_cap,
        test_sector_unknown_symbol_allowed,
        test_sector_no_positions_allowed,
        test_single_position_cap,
        test_weekly_loss_reduction,
        test_consecutive_loss_logic,
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
