"""
Unit tests for the enhanced signal quality scorer (Task 5).

Run:  python -m pytest test_signal_quality.py   (or)   python test_signal_quality.py
"""
import signal_quality as sq


def test_all_scores_bounded():
    for rsi in (10, 30, 50, 70, 90):
        for d in ("long", "short"):
            s = sq.score_technical(rsi, 0.1, 0.0, 105, 100, d)
            assert 0.0 <= s <= 10.0
    print("[test] technical bounded OK")


def test_technical_directionality():
    """Bullish setup scores higher long than short, and vice-versa."""
    long_s = sq.score_technical(65, 0.5, 0.0, 105, 100, "long")
    short_s = sq.score_technical(65, 0.5, 0.0, 105, 100, "short")
    assert long_s > short_s
    print("[test] technical directional OK")


def test_sentiment_alignment():
    assert sq.score_sentiment(9, "long") == 9.0
    assert sq.score_sentiment(9, "short") == 1.0
    assert sq.score_sentiment(None) == sq.NEUTRAL
    print("[test] sentiment alignment OK")


def test_regime_and_insider_binary():
    assert sq.score_regime(True) == 10.0
    assert sq.score_regime(False) == 0.0
    assert sq.score_regime(None) == sq.NEUTRAL
    assert sq.score_insider(True) == 10.0
    assert sq.score_insider(False) == 0.0
    assert sq.score_insider(None) == sq.NEUTRAL
    print("[test] regime/insider binary + neutral OK")


def test_volume_ratio():
    assert sq.score_volume(100, 100) == 5.0   # 1x ADV
    assert sq.score_volume(200, 100) == 10.0  # 2x ADV (clamped at 10)
    assert sq.score_volume(50, 100) == 2.5    # 0.5x ADV
    assert sq.score_volume(None, 100) == sq.NEUTRAL
    print("[test] volume ratio OK")


def test_composite_weights_sum():
    assert abs(sum(sq.WEIGHTS.values()) - 1.0) < 1e-9
    # All-10 components -> composite 10; all-0 -> 0.
    assert abs(sq.composite_score({k: 10.0 for k in sq.WEIGHTS}) - 10.0) < 1e-9
    assert abs(sq.composite_score({k: 0.0 for k in sq.WEIGHTS}) - 0.0) < 1e-9
    print("[test] composite weights OK")


def test_size_multiplier_linear():
    assert sq.size_multiplier(5.0) == 0.5
    assert sq.size_multiplier(10.0) == 1.5
    assert abs(sq.size_multiplier(7.5) - 1.0) < 1e-9
    assert sq.size_multiplier(0.0) == 0.5    # clamped
    assert sq.size_multiplier(12.0) == 1.5   # clamped
    print("[test] size multiplier linear + clamped OK")


def test_evaluate_gate():
    # Strong setup passes; weak setup fails the 5.0 gate.
    strong = sq.evaluate(rsi=68, macd=0.6, ema_short_val=104, ema_long_val=100,
                         grok_score=9, validated_for_regime=True, insider_aligned=True,
                         current_volume=180, adv=100, direction="long", min_score=5.0)
    assert strong.passes and strong.composite > 5.0
    weak = sq.evaluate(rsi=32, macd=-0.5, ema_short_val=98, ema_long_val=100,
                       grok_score=1, validated_for_regime=False, insider_aligned=False,
                       current_volume=10, adv=100, direction="long", min_score=5.0)
    assert not weak.passes and weak.composite < 5.0
    print(f"[test] evaluate gate (strong={strong.composite:.1f} weak={weak.composite:.1f}) OK")


def _run_all():
    tests = [
        test_all_scores_bounded,
        test_technical_directionality,
        test_sentiment_alignment,
        test_regime_and_insider_binary,
        test_volume_ratio,
        test_composite_weights_sum,
        test_size_multiplier_linear,
        test_evaluate_gate,
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
