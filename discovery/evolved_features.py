"""
Evolved-indicator features for the live signal-quality composite (Task 7).

The Indicator Discovery genetic engine graduates novel indicator trees into the
``discovered_indicators`` table (with their JSON structure + out-of-sample IC). This
module loads the best graduated tree for a symbol, evaluates it on the symbol's
recent bars, and turns the latest value into a directional 0-10 alignment score that
``signal_quality.evaluate`` can fold in as an extra component.

Fail-open everywhere: no DB / no graduated indicator / any evaluation error →
returns ``None`` so the composite simply omits the evolved component (NEUTRAL).
Results are cached per symbol for a short TTL to avoid a DB hit + tree eval on
every swing tick.
"""
from __future__ import annotations

import json
import time
import traceback

import numpy as np

# Per-symbol cache: symbol -> (best_tree_or_None, val_ic, loaded_at).
_TREE_CACHE: dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 6 * 3600  # graduated set only changes on the weekly GP run


def _load_best_tree(symbol: str, db_engine, regime: str = "any"):
    """Return (ExpressionNode | None, val_ic) for the highest-IC graduated indicator."""
    now = time.time()
    cached = _TREE_CACHE.get(symbol)
    if cached is not None and now - cached[2] < _CACHE_TTL_SECONDS:
        return cached[0], cached[1]

    tree = None
    val_ic = 0.0
    try:
        from discovery.indicator_library import IndicatorLibrary
        from discovery.expression_tree import ExpressionNode
        rows = IndicatorLibrary(db_engine).get_graduated(symbol, regime)
        for row in rows:  # already ordered by mean_ic DESC
            tj = row.get("tree_json")
            if not tj:
                continue
            try:
                tree = ExpressionNode.from_dict(json.loads(tj))
                val_ic = float(row.get("val_ic") or row.get("mean_ic") or 0.0)
                break
            except Exception:
                print(f"[Evolved] {symbol}: tree_json parse failed:\n{traceback.format_exc()}")
                continue
    except Exception:
        print(f"[Evolved] {symbol}: graduated load failed:\n{traceback.format_exc()}")

    _TREE_CACHE[symbol] = (tree, val_ic, now)
    return tree, val_ic


def get_evolved_score(symbol: str, bars_df, direction: str, db_engine, regime: str = "any"):
    """
    0-10 directional alignment of the symbol's best evolved indicator, or ``None``
    when unavailable. 10 == strongly confirms ``direction`` ('long'/'short').

    The latest indicator value is percentile-ranked within its own recent history;
    the indicator's validation-IC sign orients "high value == bullish".
    """
    if db_engine is None or bars_df is None or len(bars_df) < 30:
        return None
    try:
        tree, val_ic = _load_best_tree(symbol, db_engine, regime)
        if tree is None:
            return None
        series = tree.evaluate(bars_df).dropna()
        if len(series) < 20:
            return None
        latest = float(series.iloc[-1])
        if not np.isfinite(latest):
            return None
        # Percentile of the latest value within its own history (0..1).
        pct = float((series < latest).mean())
        # Orient: IC >= 0 means a high indicator value is bullish.
        bull_strength = pct if val_ic >= 0 else (1.0 - pct)
        score10 = bull_strength * 10.0
        if direction == "short":
            score10 = 10.0 - score10
        return max(0.0, min(10.0, score10))
    except Exception:
        print(f"[Evolved] {symbol}: scoring failed:\n{traceback.format_exc()}")
        return None
