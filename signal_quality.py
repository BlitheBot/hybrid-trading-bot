"""
Enhanced signal quality scoring (Task 5).

Turns the disparate evidence available at trade time into a single composite
quality score in [0, 10] and a position-size multiplier in [0.5x, 1.5x].

Five components, each scored 0-10 and combined by weight:

    technical 30% | sentiment 20% | regime 20% | insider 20% | volume 10%

Design notes
------------
* All component scorers are pure and bounded so they're trivially unit-tested.
* Missing evidence maps to a NEUTRAL 5.0 rather than 0, so the gate penalizes
  *known-bad* alignment but does not blanket-block trades just because an optional
  feed (e.g. a historical insider series) is unavailable. The spec's "0 or 10"
  binary applies when the regime/insider signal is actually known.
* ``size_multiplier`` is linear: score 5.0 -> 0.5x, score 10.0 -> 1.5x.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

NEUTRAL = 5.0

WEIGHTS = {
    "technical": 0.30,
    "sentiment": 0.20,
    "regime": 0.20,
    "insider": 0.20,
    "volume": 0.10,
}

# When an evolved indicator (Task 7 GP discovery) is available for the symbol, it
# joins the composite as a sixth component. composite_score normalizes by the sum of
# weights, so we just add the evolved weight rather than re-deriving the others.
EVOLVED_WEIGHT = 0.10
WEIGHTS_WITH_EVOLVED = {**WEIGHTS, "evolved": EVOLVED_WEIGHT}


def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def score_technical(
    rsi: float | None,
    macd_histogram: float | None,
    ema_short_val: float | None = None,
    ema_long_val: float | None = None,
    direction: str = "long",
) -> float:
    """0-10 technical strength: how far RSI/MACD/EMA exceed their thresholds.

    Each available sub-signal contributes a bounded, monotonic score; the result
    is their mean (NEUTRAL when nothing is available). ``direction`` flips the sign
    so a short is scored on bearish strength.

    ``macd_histogram`` must be the pre-computed MACD histogram (line minus signal
    line), not the raw MACD line value. At a fresh crossover the histogram is near
    zero (NEUTRAL score) and grows as momentum builds — this gives correct
    directionality regardless of raw MACD line magnitude or stock price.
    """
    sign = 1.0 if direction == "long" else -1.0
    subs: list[float] = []

    if rsi is not None:
        # long: RSI 30->0, 50->5, 70->10 ; short mirrored.
        subs.append(_clamp((rsi - 30.0) / 4.0 if sign > 0 else (70.0 - rsi) / 4.0))

    if macd_histogram is not None:
        favorable = macd_histogram * sign
        # Smoothly map the (sign-adjusted) MACD histogram to 0-10 around NEUTRAL.
        subs.append(_clamp(NEUTRAL + 5.0 * math.tanh(favorable * 5.0)))

    if ema_short_val is not None and ema_long_val:
        diff = (ema_short_val - ema_long_val) / ema_long_val
        signed = diff * sign
        # +2% favorable spread -> +5 (=> 10); -2% -> 0.
        subs.append(_clamp(NEUTRAL + signed * 250.0))

    return sum(subs) / len(subs) if subs else NEUTRAL


def score_sentiment(grok_score: float | None, direction: str = "long") -> float:
    """0-10 sentiment alignment from the Grok 0-10 bullishness score."""
    if grok_score is None:
        return NEUTRAL
    g = _clamp(float(grok_score))
    return g if direction == "long" else _clamp(10.0 - g)


def score_regime(validated_for_regime: bool | None) -> float:
    """0/10 regime alignment (NEUTRAL when no validation data exists)."""
    if validated_for_regime is None:
        return NEUTRAL
    return 10.0 if validated_for_regime else 0.0


def score_insider(insider_aligned: bool | None) -> float:
    """0/10 insider-flow alignment (NEUTRAL when no insider data is available)."""
    if insider_aligned is None:
        return NEUTRAL
    return 10.0 if insider_aligned else 0.0


def score_volume(current_volume: float | None, adv: float | None) -> float:
    """0-10 volume confirmation: ratio of current volume to ADV (1x->5, 2x->10)."""
    if not current_volume or not adv or adv <= 0:
        return NEUTRAL
    return _clamp((current_volume / adv) * 5.0)


def score_evolved(evolved_score: float | None) -> float:
    """0-10 evolved-indicator alignment (NEUTRAL when no graduated indicator exists)."""
    if evolved_score is None:
        return NEUTRAL
    return _clamp(float(evolved_score))


@dataclass
class SignalQuality:
    composite: float
    components: dict
    size_multiplier: float
    passes: bool


def composite_score(components: dict, weights: dict | None = None) -> float:
    """Weighted-average composite in [0, 10]."""
    w = weights or WEIGHTS
    total_w = sum(w.values())
    if total_w == 0:
        return 0.0
    return sum(components.get(k, NEUTRAL) * wt for k, wt in w.items()) / total_w


def size_multiplier(score: float) -> float:
    """Linear size scaler: 5.0 -> 0.5x, 10.0 -> 1.5x, clamped to [0.5, 1.5]."""
    return max(0.5, min(1.5, 0.5 + (score - 5.0) * 0.2))


def evaluate(
    *,
    rsi: float | None = None,
    macd_histogram: float | None = None,
    ema_short_val: float | None = None,
    ema_long_val: float | None = None,
    grok_score: float | None = None,
    validated_for_regime: bool | None = None,
    insider_aligned: bool | None = None,
    current_volume: float | None = None,
    adv: float | None = None,
    evolved_score: float | None = None,
    direction: str = "long",
    min_score: float = 5.0,
) -> SignalQuality:
    """Compute the full composite quality assessment for one trade decision.

    When ``evolved_score`` is provided (a graduated GP indicator was available for
    the symbol, Task 7), it joins the composite as a sixth weighted component;
    otherwise the original five-component weighting is used unchanged.
    """
    components = {
        "technical": score_technical(rsi, macd_histogram, ema_short_val, ema_long_val, direction),
        "sentiment": score_sentiment(grok_score, direction),
        "regime": score_regime(validated_for_regime),
        "insider": score_insider(insider_aligned),
        "volume": score_volume(current_volume, adv),
    }
    if evolved_score is not None:
        components["evolved"] = score_evolved(evolved_score)
        comp = composite_score(components, WEIGHTS_WITH_EVOLVED)
    else:
        comp = composite_score(components)
    return SignalQuality(
        composite=comp,
        components={k: round(v, 1) for k, v in components.items()},
        size_multiplier=size_multiplier(comp),
        passes=comp >= min_score,
    )
