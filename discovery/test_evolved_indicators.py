"""
Unit tests for the extended genetic-programming indicator discovery (Task 7).

All offline (no DB, no network). Verifies:
- the new primitives (momentum, log_return, maximum, minimum) exist and evaluate,
- ExpressionNode JSON round-trips and reports node_count,
- FitnessEvaluator.graduation_check passes a genuinely predictive indicator and
  rejects noise (IC > 0.05 AND p < 0.01 on the validation slice),
- GeneticEngine tournament selection prefers the fitter candidate,
- signal_quality folds an evolved component into the composite,
- evolved_features.get_evolved_score yields a direction-aware 0-10 score.

Run:  python -m discovery.test_evolved_indicators
"""
import time

import numpy as np
import pandas as pd

import signal_quality
from discovery.expression_tree import ExpressionNode
from discovery.fitness_evaluator import FitnessEvaluator
from discovery.genetic_engine import GeneticEngine
from discovery.primitives import PRIMITIVE_REGISTRY


def _trending_df(n=300, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    return pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": rng.integers(1e6, 5e6, n).astype(float),
    })


def test_new_primitives_present_and_evaluate():
    for name in ("log_return", "momentum_10", "maximum", "minimum"):
        assert name in PRIMITIVE_REGISTRY, name
    df = _trending_df(60)
    lr = ExpressionNode("log_return", [], 0).evaluate(df)
    mo = ExpressionNode("momentum_10", [ExpressionNode("close", [], 0)], 1).evaluate(df)
    mx = ExpressionNode("maximum",
                        [ExpressionNode("high", [], 0), ExpressionNode("low", [], 0)], 1).evaluate(df)
    assert lr.notna().sum() > 0 and mo.notna().sum() > 0 and mx.notna().sum() > 0
    # maximum(high, low) == high for every bar.
    assert (mx.dropna() == df["high"].reindex(mx.dropna().index)).all()
    print("[test] new primitives evaluate OK")


def test_tree_json_round_trip():
    tree = ExpressionNode(
        "subtract",
        [ExpressionNode("rolling_mean_5", [ExpressionNode("close", [], 0)], 1),
         ExpressionNode("close", [], 0)],
        2,
    )
    d = tree.to_dict()
    rebuilt = ExpressionNode.from_dict(d)
    assert rebuilt.to_string() == tree.to_string()
    assert tree.node_count() == 4  # subtract, rolling_mean_5, close, close
    df = _trending_df(60)
    a = tree.evaluate(df).dropna().to_numpy()
    b = rebuilt.evaluate(df).dropna().to_numpy()
    assert np.allclose(a, b)
    print("[test] ExpressionNode JSON round-trip OK")


def test_graduation_check_passes_predictive_rejects_noise():
    fe = FitnessEvaluator(forward_period=1, ic_threshold=0.05, val_pvalue_threshold=0.01)
    n = 200
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 1.0, n))
    df = pd.DataFrame({"close": close, "high": close, "low": close,
                       "open": close, "volume": np.ones(n)})
    # Indicator value at t = the (slightly noised) 1-bar forward return at t -> high IC.
    fwd_ret = df["close"].pct_change(1).shift(-1)
    df["signal_feat"] = (fwd_ret + rng.normal(0, 1e-4, n)).fillna(0.0)

    class _LeafFeat(ExpressionNode):
        def __init__(self):
            super().__init__("close", [], 0)
        def evaluate(self, bars_df):
            return bars_df["signal_feat"]

    grad = fe.graduation_check(_LeafFeat(), df)
    assert grad["graduated"] is True, grad
    # Pure noise indicator should not graduate.
    df["noise"] = rng.normal(0, 1, n)

    class _LeafNoise(ExpressionNode):
        def __init__(self):
            super().__init__("close", [], 0)
        def evaluate(self, bars_df):
            return bars_df["noise"]

    assert fe.graduation_check(_LeafNoise(), df)["graduated"] is False
    print("[test] graduation_check predictive/noise OK")


def test_tournament_selection_prefers_fitter():
    eng = GeneticEngine(population_size=10, tournament_size=10)
    import random
    rng = random.Random(0)
    trees = [ExpressionNode("close", [], 0) for _ in range(5)]
    scored = [(trees[i], {"mean_ic": ic}) for i, ic in enumerate([0.1, 0.2, 0.9, 0.0, 0.3])]
    # tournament_size covers whole pool -> always the max (idx 2).
    winner = eng._tournament_select(scored, rng)
    assert winner is trees[2]
    print("[test] tournament selection prefers fitter OK")


def test_signal_quality_folds_evolved_component():
    base = signal_quality.evaluate(rsi=50, direction="long")
    high = signal_quality.evaluate(rsi=50, evolved_score=10.0, direction="long")
    low = signal_quality.evaluate(rsi=50, evolved_score=0.0, direction="long")
    assert "evolved" in high.components and "evolved" not in base.components
    assert high.composite > base.composite > low.composite
    print("[test] signal_quality evolved component OK")


def test_evolved_score_direction_aware():
    import discovery.evolved_features as ef
    df = _trending_df(120)
    tree = ExpressionNode("close", [], 0)  # latest close is near the top of its range
    ef._TREE_CACHE["FAKE"] = (tree, 0.5, time.time())  # positive IC -> high value bullish
    long_score = ef.get_evolved_score("FAKE", df, "long", db_engine=object())
    short_score = ef.get_evolved_score("FAKE", df, "short", db_engine=object())
    assert long_score is not None and short_score is not None
    assert abs((long_score + short_score) - 10.0) < 1e-6  # mirror around 5
    assert long_score > 5.0  # uptrend, latest close high -> bullish confirmation
    print("[test] evolved_features direction-aware score OK")


def _run_all():
    tests = [
        test_new_primitives_present_and_evaluate,
        test_tree_json_round_trip,
        test_graduation_check_passes_predictive_rejects_noise,
        test_tournament_selection_prefers_fitter,
        test_signal_quality_folds_evolved_component,
        test_evolved_score_direction_aware,
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
