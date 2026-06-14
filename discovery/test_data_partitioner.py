"""
Unit tests for the out-of-sample integrity wall (Task 2).

Verifies:
- 70/15/15 split produces disjoint, contiguous, correctly-ordered regions,
- validation/holdout access raises PartitionViolation until unlocked,
- unlocking permits access; holdout unlock requires a reason,
- get_non_holdout excludes the holdout region exactly,
- boundaries reflect the real first/last timestamps.

Run:  python -m discovery.test_data_partitioner
"""
import numpy as np
import pandas as pd

from discovery.data_partitioner import DataPartitioner, PartitionViolation


def _make_df(n: int = 1000) -> pd.DataFrame:
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    close = 100.0 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


def test_split_sizes_and_disjoint():
    df = _make_df(1000)
    p = DataPartitioner(df, "TEST")
    train = p.get_training()
    p.unlock_validation()
    val = p.get_validation()
    p.unlock_holdout("test")
    hold = p.get_holdout()
    assert len(train) == 700 and len(val) == 150 and len(hold) == 150
    # Contiguous + disjoint: concatenation reconstructs the original.
    rebuilt = pd.concat([train, val, hold])
    assert len(rebuilt) == len(df)
    assert rebuilt.index.equals(df.index)
    print("[test] 70/15/15 disjoint contiguous split OK")


def test_validation_locked_by_default():
    p = DataPartitioner(_make_df(), "TEST")
    try:
        p.get_validation()
    except PartitionViolation:
        print("[test] validation locked by default OK")
        return
    raise AssertionError("validation access should have raised PartitionViolation")


def test_holdout_locked_by_default():
    p = DataPartitioner(_make_df(), "TEST")
    try:
        p.get_holdout()
    except PartitionViolation:
        print("[test] holdout locked by default OK")
        return
    raise AssertionError("holdout access should have raised PartitionViolation")


def test_partition_violation_is_value_error():
    """Optimization code catching ValueError also catches PartitionViolation."""
    assert issubclass(PartitionViolation, ValueError)
    print("[test] PartitionViolation is a ValueError OK")


def test_holdout_requires_reason():
    p = DataPartitioner(_make_df(), "TEST")
    try:
        p.unlock_holdout("")
    except PartitionViolation:
        print("[test] holdout unlock requires reason OK")
        return
    raise AssertionError("empty-reason holdout unlock should raise")


def test_non_holdout_excludes_holdout():
    df = _make_df(1000)
    p = DataPartitioner(df, "TEST")
    nh = p.get_non_holdout()
    assert len(nh) == 850
    p.unlock_holdout("verify")
    hold = p.get_holdout()
    # No overlap between non-holdout and holdout indices.
    assert not nh.index.intersection(hold.index).size
    print("[test] non-holdout excludes holdout OK")


def test_boundaries():
    df = _make_df(1000)
    p = DataPartitioner(df, "TEST")
    b = p.boundaries
    assert b["train_start"] == "2019-01-01"
    assert b["holdout_end"] == df.index[-1].strftime("%Y-%m-%d")
    assert b["n_bars"] == 1000
    print("[test] boundaries reflect real timestamps OK")


def _run_all():
    tests = [
        test_split_sizes_and_disjoint,
        test_validation_locked_by_default,
        test_holdout_locked_by_default,
        test_partition_violation_is_value_error,
        test_holdout_requires_reason,
        test_non_holdout_excludes_holdout,
        test_boundaries,
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
