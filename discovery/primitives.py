"""
Building blocks for the Indicator Discovery genetic engine.

Arity encoding in PRIMITIVE_REGISTRY:
  0 = leaf    callable(bars_df: pd.DataFrame) -> pd.Series
  1 = unary   callable(pd.Series)             -> pd.Series
  2 = binary  callable(pd.Series, pd.Series)  -> pd.Series

TimeOps are pre-bound to N in [5, 10, 14, 20] and stored as
rolling_mean_5, rolling_mean_10, etc.
"""
import numpy as np
import pandas as pd


# ── Safe math helpers ──────────────────────────────────────────────────────────

def _safe_log(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    return np.log(s.where(s > 0))

def _safe_sqrt(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    return np.sqrt(s.where(s >= 0))

def _safe_divide(a: pd.Series, b: pd.Series) -> pd.Series:
    with np.errstate(divide="ignore", invalid="ignore"):
        return a / b.where(b.abs() > 1e-9)


# ── Existing-signal wrappers (treated as leaves — produce Series from bars_df) ─

def _kalman_slope(bars_df: pd.DataFrame) -> pd.Series:
    try:
        from strategies.kalman_signal import KalmanTrendSignal
        return KalmanTrendSignal().compute(bars_df["close"])["slope"]
    except Exception:
        return pd.Series(np.nan, index=bars_df.index)

def _kalman_noise_ratio(bars_df: pd.DataFrame) -> pd.Series:
    try:
        from strategies.kalman_signal import KalmanTrendSignal
        return KalmanTrendSignal().compute(bars_df["close"])["noise_ratio"]
    except Exception:
        return pd.Series(np.nan, index=bars_df.index)

def _hurst_exponent(bars_df: pd.DataFrame) -> pd.Series:
    try:
        from strategies.hurst_signal import HurstSignal
        return HurstSignal().compute(bars_df["close"])["hurst"]
    except Exception:
        return pd.Series(np.nan, index=bars_df.index)

def _vwap_distance(bars_df: pd.DataFrame) -> pd.Series:
    try:
        from strategies.vwap_signal import AnchoredVWAPSignal
        bars_df = bars_df.sort_index()
        return AnchoredVWAPSignal().compute(bars_df)["distance_pct"]
    except Exception:
        return pd.Series(np.nan, index=bars_df.index)


# ── Build registries ───────────────────────────────────────────────────────────

_LEAVES: dict[str, callable] = {
    "close":              lambda df: df["close"],
    "high":               lambda df: df["high"],
    "low":                lambda df: df["low"],
    "volume":             lambda df: df["volume"].astype(float),
    "open":               lambda df: df["open"],
    "returns":            lambda df: df["close"].pct_change(),
    "log_return":         lambda df: _safe_log(df["close"]) - _safe_log(df["close"].shift(1)),
    "kalman_slope":       _kalman_slope,
    "kalman_noise_ratio": _kalman_noise_ratio,
    "hurst_exponent":     _hurst_exponent,
    "vwap_distance":      _vwap_distance,
}

_UNARY: dict[str, callable] = {
    "log":  _safe_log,
    "sqrt": _safe_sqrt,
    "abs":  lambda s: s.abs(),
    "sign": lambda s: np.sign(s),
}

_N_VALUES = [5, 10, 14, 20]
for _n in _N_VALUES:
    _UNARY[f"rolling_mean_{_n}"] = (lambda n: lambda s: s.rolling(n).mean())(_n)
    _UNARY[f"rolling_std_{_n}"]  = (lambda n: lambda s: s.rolling(n).std())(_n)
    _UNARY[f"lag_{_n}"]          = (lambda n: lambda s: s.shift(n))(_n)
    _UNARY[f"diff_{_n}"]         = (lambda n: lambda s: s.diff(n))(_n)
    _UNARY[f"rolling_max_{_n}"]  = (lambda n: lambda s: s.rolling(n).max())(_n)
    _UNARY[f"rolling_min_{_n}"]  = (lambda n: lambda s: s.rolling(n).min())(_n)
    # momentum(n): n-bar percent change (Task 7 primitive set).
    _UNARY[f"momentum_{_n}"]     = (lambda n: lambda s: s.pct_change(n))(_n)

_BINARY: dict[str, callable] = {
    "add":      lambda a, b: a + b,        # difference(a,b) == subtract; ratio(a,b) == divide
    "subtract": lambda a, b: a - b,
    "multiply": lambda a, b: a * b,
    "divide":   _safe_divide,
    "maximum":  lambda a, b: np.maximum(a, b),
    "minimum":  lambda a, b: np.minimum(a, b),
}

# Unified registry: name → (arity, callable)
PRIMITIVE_REGISTRY: dict[str, tuple[int, callable]] = {}
for _name, _fn in _LEAVES.items():
    PRIMITIVE_REGISTRY[_name] = (0, _fn)
for _name, _fn in _UNARY.items():
    PRIMITIVE_REGISTRY[_name] = (1, _fn)
for _name, _fn in _BINARY.items():
    PRIMITIVE_REGISTRY[_name] = (2, _fn)

LEAF_NAMES   = list(_LEAVES.keys())
UNARY_NAMES  = list(_UNARY.keys())
BINARY_NAMES = list(_BINARY.keys())
