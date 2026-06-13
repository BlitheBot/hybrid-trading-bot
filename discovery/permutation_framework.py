"""
Permutation-based strategy validation framework — Timothy Masters 4-step MCPT.

This module eliminates data-mining bias and out-of-sample selection luck by asking
a single question at each stage: *could a strategy optimizer have found an
equally-good result on pure noise that preserves the price series' statistical
properties?* If yes, the discovered edge is an artifact of the search, not a real
signal, and the strategy is discarded.

Pipeline (see ``validate_strategy_edge`` — the single entry point):

    Step 1  Position-Vector Backtester
            Strategy posture is a vector S in {+1 long, -1 short, 0 flat}.
            Returns are continuous close-to-close log returns R_t.
            Strategy returns = S_t * R_{t+1}  (shift +1 bar => no lookahead).
            ``calculate_objective_score`` is the one scoring function used
            everywhere (Sharpe or Profit Factor, computed on the return vector).

    Step 2  Bar Permutation Noise Generator (``get_permutation``)
            Shuffles intra-bar movements and candle gaps with a *single* shuffle
            index, preserving the multiset of close-to-close returns (hence mean,
            std, skew, kurtosis and the final close) while destroying path memory.

    Step 3  In-Sample Monte Carlo Permutation Test
            Optimize on real data (PF_real), then re-optimize on N permuted paths.
            p = count(PF_perm >= PF_real) / N.   p > threshold => data-mining bias.

    Step 4  Walk-Forward Monte Carlo Permutation Test
            Roll train/test on real data (WF_real), then on partially-permuted
            paths (training period kept intact). p > threshold => selection luck.

    Step 5  Fail-Fast Gateway (``validate_strategy_edge``)
            80/20 hard wall. In-sample test first; only on pass run walk-forward;
            only on both passing, persist to the ``validated_strategies`` table.

All statistics use numpy; scipy is used only for skew/kurtosis in moment
validation. Monte Carlo iterations are parallelized with multiprocessing, each
worker seeded as ``base_seed + worker_id`` for reproducible, independent draws.
"""
from __future__ import annotations

import itertools
import os
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
from scipy.stats import kurtosis, skew

from config import Config

# Reports directory — created on import so every code path can write freely.
REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_TRADING_DAYS = 252


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — Position-Vector Backtester
# ──────────────────────────────────────────────────────────────────────────────

def calculate_log_returns(closes: np.ndarray) -> np.ndarray:
    """
    Continuous close-to-close log returns R_t = log(close_t / close_{t-1}).

    Returns an array the same length as ``closes`` with R[0] = 0.0 so that the
    return vector stays index-aligned with the position vector.
    """
    closes = np.asarray(closes, dtype=float)
    out = np.zeros_like(closes)
    if closes.size < 2:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = np.log(closes[1:] / closes[:-1])
    out[~np.isfinite(out)] = 0.0
    return out


def _score_strategy_returns(strategy_returns: np.ndarray, method: str) -> float:
    """Objective score for an already-computed strategy-return vector."""
    sr = np.asarray(strategy_returns, dtype=float)
    sr = sr[np.isfinite(sr)]
    if sr.size == 0:
        return 0.0

    if method == "sharpe":
        sd = sr.std()
        if sd == 0:
            return 0.0
        return float(sr.mean() / sd * np.sqrt(_TRADING_DAYS))

    if method == "profit_factor":
        gross_profit = sr[sr > 0].sum()
        gross_loss = abs(sr[sr < 0].sum())
        if gross_loss == 0:
            # No losing bars: profit factor is undefined/infinite. Return a large
            # finite number so comparisons/plots behave, or 0 if also no profit.
            return float(gross_profit * 1e6) if gross_profit > 0 else 0.0
        return float(gross_profit / gross_loss)

    raise ValueError(f"Unknown objective method: {method!r}")


def calculate_objective_score(
    position_vector: np.ndarray,
    returns: np.ndarray,
    method: str = "sharpe",
    mask: np.ndarray | None = None,
) -> float:
    """
    The single scoring function used everywhere in the framework.

    Applies the +1-bar forward shift (strategy_returns = S_t * R_{t+1}) so the
    position taken on bar t is rewarded with the return realized on bar t+1, then
    scores the resulting return vector directly (never a trade list).

    ``mask`` (optional, aligned to ``position_vector``) restricts scoring to bars
    where the mask is True — used for regime-specific scoring. The position bar t
    determines inclusion (a position held during regime R is scored under R).
    """
    pos = np.asarray(position_vector, dtype=float)
    ret = np.asarray(returns, dtype=float)
    n = min(pos.size, ret.size)
    if n < 2:
        return 0.0
    pos = pos[:n]
    ret = ret[:n]
    # S_t * R_{t+1}: drop the final position (no t+1 return for it).
    strategy_returns = pos[:-1] * ret[1:]
    if mask is not None:
        m = np.asarray(mask, dtype=bool)[:n][:-1]
        strategy_returns = strategy_returns[m]
    return _score_strategy_returns(strategy_returns, method)


# ──────────────────────────────────────────────────────────────────────────────
# Position-vector strategy adapters
# ──────────────────────────────────────────────────────────────────────────────

class SwingPositionStrategy:
    """
    Position-vector adapter for the live EMA/MACD/RSI swing strategy.

    Produces a posture vector in {+1 long, 0 flat} per bar from a deterministic
    function of the indicators on the supplied price path. The exit is a
    trend-flip (EMA_short < EMA_long) rather than an intra-bar stop/target, which
    keeps the backtest bar-granular and permutation-safe (intra-bar stops would
    depend on permuted high/low ordering and bias the test).
    """

    name = "swing_ema_macd_rsi"

    # Same search space the v1 Discovery Engine grid-searches.
    PARAM_GRID = {
        "ema_short": [20, 30, 50],
        "ema_long": [100, 150, 200],
        "rsi_period": [10, 14, 21],
        "rsi_entry_low": [35, 40, 45],
        "rsi_entry_high": [55, 60, 65],
    }

    def param_grid(self) -> list[dict]:
        combos = itertools.product(
            self.PARAM_GRID["ema_short"],
            self.PARAM_GRID["ema_long"],
            self.PARAM_GRID["rsi_period"],
            self.PARAM_GRID["rsi_entry_low"],
            self.PARAM_GRID["rsi_entry_high"],
        )
        grid = []
        for es, el, rp, rl, rh in combos:
            if es < el and rl < rh:
                grid.append({
                    "ema_short": es,
                    "ema_long": el,
                    "rsi_period": rp,
                    "rsi_entry_low": rl,
                    "rsi_entry_high": rh,
                })
        return grid

    def position_vector(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        """Deterministic {+1, 0} posture vector for the given path and params."""
        close = df["close"]
        ema_s = ta.ema(close, length=params["ema_short"])
        ema_l = ta.ema(close, length=params["ema_long"])
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        rsi = ta.rsi(close, length=params["rsi_period"])

        n = len(df)
        pos = np.zeros(n, dtype=float)
        if ema_s is None or ema_l is None or macd_df is None or rsi is None:
            return pos

        ema_s = ema_s.to_numpy()
        ema_l = ema_l.to_numpy()
        macd = macd_df.iloc[:, 0].to_numpy()
        macd_sig = macd_df.iloc[:, 2].to_numpy()
        rsi = rsi.to_numpy()
        rl = params["rsi_entry_low"]
        rh = params["rsi_entry_high"]

        in_pos = False
        for i in range(1, n):
            if any(not np.isfinite(v) for v in (ema_s[i], ema_l[i], macd[i], macd_sig[i], rsi[i], macd_sig[i - 1], macd[i - 1])):
                pos[i] = 1.0 if in_pos else 0.0
                continue
            if not in_pos:
                if (ema_s[i] > ema_l[i]
                        and macd[i] > macd_sig[i] and macd[i - 1] <= macd_sig[i - 1]
                        and rl <= rsi[i] <= rh):
                    in_pos = True
            else:
                if ema_s[i] < ema_l[i]:
                    in_pos = False
            pos[i] = 1.0 if in_pos else 0.0
        return pos


# Registry so multiprocessing workers can resolve a strategy by class without
# pickling instances. Module-level classes pickle cleanly under spawn (Windows).
_STRATEGY_REGISTRY = {SwingPositionStrategy.name: SwingPositionStrategy}


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 — Bar Permutation Noise Generator
# ──────────────────────────────────────────────────────────────────────────────

def _moments(returns: np.ndarray) -> tuple[float, float, float, float]:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return 0.0, 0.0, 0.0, 0.0
    return (
        float(np.mean(r)),
        float(np.std(r)),
        float(skew(r)),
        float(kurtosis(r)),
    )


def _validate_moments(orig_log_close: np.ndarray, perm_log_close: np.ndarray) -> None:
    """Raise ValueError if any return moment drifts beyond the configured tolerance."""
    tol = Config.PERMUTATION_MOMENT_TOLERANCE
    o_mean, o_std, o_skew, o_kurt = _moments(np.diff(orig_log_close))
    p_mean, p_std, p_skew, p_kurt = _moments(np.diff(perm_log_close))

    checks = {
        "mean": (o_mean, p_mean),
        "std": (o_std, p_std),
        "skew": (o_skew, p_skew),
        "kurtosis": (o_kurt, p_kurt),
    }
    for label, (orig, perm) in checks.items():
        # Relative tolerance with a small absolute floor for near-zero moments.
        if not np.isclose(perm, orig, rtol=tol, atol=1e-6):
            raise ValueError(
                f"Permutation moment mismatch on {label}: "
                f"original={orig:.6f} permuted={perm:.6f} (tol={tol:.1%})"
            )


def get_permutation(data, start_index: int = 0, seed: int | None = None):
    """
    Generate a synthetic price path (or list of correlated paths) that preserves
    statistical properties while destroying path memory.

    A single shuffle index is applied simultaneously to the candle gaps and the
    intra-bar (high/low/close-relative-to-open) movements, for every supplied
    asset. Because gap and close-relative-to-open move together, the multiset of
    close-to-close log returns is preserved — so the moments and the final close
    are preserved, and (with ``start_index`` = 0) the first open is untouched.

    ``start_index`` > 0 keeps bars ``[0, start_index]`` intact and only shuffles
    bars after it, for walk-forward permutation where the training period must be
    preserved.

    Pass a list of DataFrames for correlated-asset permutation: the same shuffle
    index is applied to all of them.
    """
    is_list = isinstance(data, (list, tuple))
    dfs = list(data) if is_list else [data]
    if not dfs:
        raise ValueError("get_permutation received empty input")

    n_bars = len(dfs[0])
    for d in dfs:
        if len(d) != n_bars:
            raise ValueError("All assets must have identical length for correlated permutation")
    if start_index < 0:
        raise ValueError(f"start_index must be >= 0, got {start_index}")
    if start_index >= n_bars - 1:
        raise ValueError(
            f"start_index ({start_index}) leaves fewer than 2 bars to permute "
            f"(n_bars={n_bars})"
        )

    rng = np.random.default_rng(seed)
    perm_index = start_index + 1           # first bar that gets shuffled
    perm_n = n_bars - perm_index
    shuffle = rng.permutation(perm_n)      # SINGLE shuffle index for gaps + intra-bar

    results = []
    for d in dfs:
        log_o = np.log(d["open"].to_numpy(dtype=float))
        log_h = np.log(d["high"].to_numpy(dtype=float))
        log_l = np.log(d["low"].to_numpy(dtype=float))
        log_c = np.log(d["close"].to_numpy(dtype=float))

        # Relative decomposition.
        gap = np.zeros_like(log_o)
        gap[1:] = log_o[1:] - log_c[:-1]   # gap_t = log(open_t) - log(close_{t-1})
        rel_h = log_h - log_o
        rel_l = log_l - log_o
        rel_c = log_c - log_o

        # Apply the same shuffle to every relative component over the perm region.
        gap_p = gap[perm_index:][shuffle]
        rel_h_p = rel_h[perm_index:][shuffle]
        rel_l_p = rel_l[perm_index:][shuffle]
        rel_c_p = rel_c[perm_index:][shuffle]

        new_log_o = log_o.copy()
        new_log_h = log_h.copy()
        new_log_l = log_l.copy()
        new_log_c = log_c.copy()

        last_close = log_c[perm_index - 1]
        for k in range(perm_n):
            o = last_close + gap_p[k]
            new_log_o[perm_index + k] = o
            new_log_h[perm_index + k] = o + rel_h_p[k]
            new_log_l[perm_index + k] = o + rel_l_p[k]
            new_log_c[perm_index + k] = o + rel_c_p[k]
            last_close = new_log_c[perm_index + k]

        _validate_moments(log_c, new_log_c)

        out = d.copy()
        out["open"] = np.exp(new_log_o)
        out["high"] = np.exp(new_log_h)
        out["low"] = np.exp(new_log_l)
        out["close"] = np.exp(new_log_c)

        o_mean, o_std, o_skew, o_kurt = _moments(np.diff(log_c))
        p_mean, p_std, p_skew, p_kurt = _moments(np.diff(new_log_c))
        print(
            f"[Permutation] Generated synthetic path — "
            f"mean={p_mean:.4f} std={p_std:.4f} skew={p_skew:.4f} kurt={p_kurt:.4f} "
            f"(original: mean={o_mean:.4f} std={o_std:.4f})"
        )
        results.append(out)

    return results if is_list else results[0]


# ──────────────────────────────────────────────────────────────────────────────
# Optimization over the parameter grid
# ──────────────────────────────────────────────────────────────────────────────

def _optimize_score(strategy, grid: list[dict], df: pd.DataFrame, method: str,
                    mask: np.ndarray | None = None) -> tuple[float, dict]:
    """Best objective score (and params) across the grid for one path.

    ``mask`` (aligned to ``df``) restricts scoring to a regime's bars.
    """
    returns = calculate_log_returns(df["close"].to_numpy())
    best_score = -np.inf
    best_params: dict = {}
    for params in grid:
        try:
            pos = strategy.position_vector(df, params)
        except Exception:
            print(f"[Permutation] position_vector failed for {params}:\n{traceback.format_exc()}")
            continue
        score = calculate_objective_score(pos, returns, method, mask=mask)
        if score > best_score:
            best_score = score
            best_params = params
    if not np.isfinite(best_score):
        best_score = 0.0
    return best_score, best_params


def _walk_forward_score(
    strategy,
    grid: list[dict],
    df: pd.DataFrame,
    train_window: int,
    test_window: int,
    method: str,
    mask: np.ndarray | None = None,
) -> float:
    """
    Rolling walk-forward: optimize on each train window, apply best params to the
    following test window, accumulate test-period strategy returns, then score the
    concatenated out-of-sample return vector once.

    ``mask`` (aligned to ``df``) restricts the out-of-sample scoring to a regime's
    bars. Training-window optimization is restricted to the same regime so the
    selected params are tuned for that regime.
    """
    n = len(df)
    start = 0
    oos_returns: list[np.ndarray] = []
    mask_arr = None if mask is None else np.asarray(mask, dtype=bool)

    while start + train_window + test_window <= n:
        train_mask = None if mask_arr is None else mask_arr[start:start + train_window]
        train = df.iloc[start:start + train_window]
        _, best_params = _optimize_score(strategy, grid, train, method, mask=train_mask)
        if not best_params:
            start += test_window
            continue
        test = df.iloc[start + train_window:start + train_window + test_window]
        try:
            pos = strategy.position_vector(test, best_params)
        except Exception:
            print(f"[Permutation] WF position_vector failed:\n{traceback.format_exc()}")
            start += test_window
            continue
        test_ret = calculate_log_returns(test["close"].to_numpy())
        if pos.size >= 2:
            strat_ret = pos[:-1] * test_ret[1:]
            if mask_arr is not None:
                tm = mask_arr[start + train_window:start + train_window + test_window][:pos.size][:-1]
                strat_ret = strat_ret[tm]
            oos_returns.append(strat_ret)
        start += test_window

    if not oos_returns or sum(r.size for r in oos_returns) == 0:
        return 0.0
    return _score_strategy_returns(np.concatenate(oos_returns), method)


# ──────────────────────────────────────────────────────────────────────────────
# Multiprocessing workers (module-level for spawn picklability)
# ──────────────────────────────────────────────────────────────────────────────

def _insample_worker(payload: tuple) -> float:
    iteration, base_seed, df, grid, method, strat_name, mask = payload
    seed = base_seed + iteration
    try:
        strategy = _STRATEGY_REGISTRY[strat_name]()
        perm = get_permutation(df, start_index=0, seed=seed)
        score, _ = _optimize_score(strategy, grid, perm, method, mask=mask)
        return float(score)
    except Exception:
        print(f"[Permutation] in-sample worker {iteration} failed:\n{traceback.format_exc()}")
        return float("nan")


def _walkforward_worker(payload: tuple) -> float:
    iteration, base_seed, df, grid, method, strat_name, train_window, test_window, mask = payload
    seed = base_seed + iteration
    try:
        strategy = _STRATEGY_REGISTRY[strat_name]()
        perm = get_permutation(df, start_index=train_window, seed=seed)
        score = _walk_forward_score(strategy, grid, perm, train_window, test_window, method, mask=mask)
        return float(score)
    except Exception:
        print(f"[Permutation] walk-forward worker {iteration} failed:\n{traceback.format_exc()}")
        return float("nan")


def _n_workers() -> int:
    configured = int(getattr(Config, "PERMUTATION_WORKERS", 0) or 0)
    if configured > 0:
        return configured
    return max(1, (os.cpu_count() or 2) - 1)


def _run_parallel(worker, payloads: list[tuple]) -> list[float]:
    """Run worker over payloads with a process pool; fall back to serial on failure."""
    n_workers = _n_workers()
    if n_workers <= 1 or len(payloads) <= 1:
        return [worker(p) for p in payloads]

    try:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=n_workers) as pool:
            return list(pool.map(worker, payloads))
    except Exception:
        print(
            f"[Permutation] multiprocessing pool failed — falling back to serial:\n"
            f"{traceback.format_exc()}"
        )
        return [worker(p) for p in payloads]


# ──────────────────────────────────────────────────────────────────────────────
# Histogram reporting
# ──────────────────────────────────────────────────────────────────────────────

def _save_histogram(perm_scores: np.ndarray, real_score: float, title: str, path: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9, 5))
        ax.hist(perm_scores, bins=50, color="#4C78A8", alpha=0.8, label="Permuted scores")
        ax.axvline(real_score, color="#E45756", linestyle="--", linewidth=2,
                   label=f"Real score = {real_score:.4f}")
        ax.set_title(title)
        ax.set_xlabel("Objective score")
        ax.set_ylabel("Frequency")
        ax.legend()
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        print(f"[Permutation] Saved histogram → {path}")
    except Exception:
        print(f"[Permutation] histogram render failed (non-fatal):\n{traceback.format_exc()}")


# ──────────────────────────────────────────────────────────────────────────────
# Step 3 — In-Sample Monte Carlo Permutation Test
# ──────────────────────────────────────────────────────────────────────────────

def run_insample_permutation_test(
    strategy_class,
    params: dict,
    insample_data: pd.DataFrame,
    n_iterations: int = 1000,
    method: str | None = None,
    symbol: str = "",
    base_seed: int = 1_000,
    mask: np.ndarray | None = None,
    regime_label: str = "",
) -> tuple[bool, float, float]:
    """
    Returns (passed, p_value, real_score).

    Optimizes the strategy on real in-sample data for PF_real, then re-optimizes
    on ``n_iterations`` fully-permuted paths. p = count(PF_perm >= PF_real) / N.
    Passes when p <= Config.PERMUTATION_P_THRESHOLD.

    ``mask`` restricts scoring to a regime's bars; ``regime_label`` tags the logs
    and report filename.
    """
    method = method or Config.PERMUTATION_OBJECTIVE
    strategy = strategy_class()
    grid = strategy.param_grid()
    tag = f"{symbol}/{regime_label}" if regime_label else symbol

    real_score, real_params = _optimize_score(strategy, grid, insample_data, method, mask=mask)
    print(
        f"[Permutation] {tag} in-sample baseline {method}={real_score:.4f} "
        f"params={real_params}"
    )

    payloads = [
        (i, base_seed, insample_data, grid, method, strategy.name, mask)
        for i in range(n_iterations)
    ]
    raw = _run_parallel(_insample_worker, payloads)
    perm_scores = np.array([s for s in raw if np.isfinite(s)], dtype=float)

    if perm_scores.size == 0:
        print(f"[Permutation] {tag} in-sample: all permutations failed — treating as FAIL")
        return False, 1.0, real_score

    p_value = float(np.mean(perm_scores >= real_score))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{regime_label}" if regime_label else ""
    _save_histogram(
        perm_scores, real_score,
        title=f"In-Sample MCPT — {tag} ({method}) p={p_value:.4f}",
        path=REPORTS_DIR / f"insample_{symbol}{suffix}_{ts}.png",
    )

    if p_value > Config.PERMUTATION_P_THRESHOLD:
        print(
            f"[Permutation] FAIL in-sample p={p_value:.4f} > {Config.PERMUTATION_P_THRESHOLD} "
            f"({tag}) — strategy discarded (data mining bias detected)"
        )
        return False, p_value, real_score

    print(f"[Permutation] PASS in-sample p={p_value:.4f} ({tag}) — advancing to walk-forward test")
    return True, p_value, real_score


# ──────────────────────────────────────────────────────────────────────────────
# Step 4 — Walk-Forward Monte Carlo Permutation Test
# ──────────────────────────────────────────────────────────────────────────────

def run_walkforward_permutation_test(
    strategy_class,
    params: dict,
    full_data: pd.DataFrame,
    train_window: int,
    test_window: int,
    n_iterations: int = 200,
    method: str | None = None,
    symbol: str = "",
    base_seed: int = 5_000,
    mask: np.ndarray | None = None,
    regime_label: str = "",
) -> tuple[bool, float, float]:
    """
    Returns (passed, p_value, real_score).

    Runs walk-forward on real data for WF_real, then on ``n_iterations`` partially
    permuted paths (training period intact via start_index=train_window).
    p = count(WF_perm >= WF_real) / N. Passes when p <= the configured threshold.

    ``mask`` restricts out-of-sample scoring to a regime's bars; ``regime_label``
    tags logs and the report filename.
    """
    method = method or Config.PERMUTATION_OBJECTIVE
    strategy = strategy_class()
    grid = strategy.param_grid()
    tag = f"{symbol}/{regime_label}" if regime_label else symbol

    real_score = _walk_forward_score(strategy, grid, full_data, train_window, test_window, method, mask=mask)
    print(f"[Permutation] {tag} walk-forward baseline {method}={real_score:.4f}")

    payloads = [
        (i, base_seed, full_data, grid, method, strategy.name, train_window, test_window, mask)
        for i in range(n_iterations)
    ]
    raw = _run_parallel(_walkforward_worker, payloads)
    perm_scores = np.array([s for s in raw if np.isfinite(s)], dtype=float)

    if perm_scores.size == 0:
        print(f"[Permutation] {tag} walk-forward: all permutations failed — treating as FAIL")
        return False, 1.0, real_score

    p_value = float(np.mean(perm_scores >= real_score))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{regime_label}" if regime_label else ""
    _save_histogram(
        perm_scores, real_score,
        title=f"Walk-Forward MCPT — {tag} ({method}) p={p_value:.4f}",
        path=REPORTS_DIR / f"walkforward_{symbol}{suffix}_{ts}.png",
    )

    if p_value > Config.PERMUTATION_P_THRESHOLD:
        print(
            f"[Permutation] FAIL walk-forward p={p_value:.4f} ({tag}) "
            f"— strategy rejected (out-of-sample selection luck)"
        )
        return False, p_value, real_score

    print(f"[Permutation] PASS walk-forward p={p_value:.4f} ({tag}) — strategy has genuine edge")
    return True, p_value, real_score


# ──────────────────────────────────────────────────────────────────────────────
# validated_strategies persistence
# ──────────────────────────────────────────────────────────────────────────────

def _get_db_conn():
    import psycopg2
    url = Config.DATABASE_URL
    if not url:
        return None
    return psycopg2.connect(url)


def _ensure_validated_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS validated_strategies (
                id                 SERIAL PRIMARY KEY,
                symbol             VARCHAR(10),
                strategy_name      VARCHAR(50),
                parameters         JSONB,
                objective_method   VARCHAR(20),
                insample_p         FLOAT,
                walkforward_p      FLOAT,
                insample_score     FLOAT,
                walkforward_score  FLOAT,
                validated_at       TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Regime-aware columns — added for safe deployment to existing databases.
        for col, ddl in (
            ("valid_bull_trend", "BOOLEAN DEFAULT FALSE"),
            ("valid_bear_trend", "BOOLEAN DEFAULT FALSE"),
            ("valid_high_vol", "BOOLEAN DEFAULT FALSE"),
            ("valid_choppy", "BOOLEAN DEFAULT FALSE"),
            ("best_regime", "VARCHAR(20)"),
            ("regime_sharpes", "JSONB"),
        ):
            cur.execute(f"ALTER TABLE validated_strategies ADD COLUMN IF NOT EXISTS {col} {ddl}")
    conn.commit()


def _write_validated(symbol, strategy_name, params, method,
                     insample_p, walkforward_p, insample_score, walkforward_score) -> None:
    import json
    conn = None
    try:
        conn = _get_db_conn()
        if conn is None:
            print("[Permutation] No DATABASE_URL — skipping validated_strategies write")
            return
        _ensure_validated_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO validated_strategies (
                    symbol, strategy_name, parameters, objective_method,
                    insample_p, walkforward_p, insample_score, walkforward_score
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                symbol, strategy_name, json.dumps(params), method,
                float(insample_p), float(walkforward_p),
                float(insample_score), float(walkforward_score),
            ))
        conn.commit()
        print(f"[Permutation] Persisted validated strategy {symbol}/{strategy_name} to DB")
    except Exception:
        print(f"[Permutation] validated_strategies write failed:\n{traceback.format_exc()}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Step 5 — Fail-Fast Gateway
# ──────────────────────────────────────────────────────────────────────────────

def validate_strategy_edge(strategy_class, params: dict, symbol: str, full_data: pd.DataFrame) -> dict:
    """
    Single entry point. Returns a result dict::

        {
          "promoted": bool,
          "insample_p": float | None,
          "walkforward_p": float | None,
          "insample_score": float | None,
          "walkforward_score": float | None,
          "reason": str,
        }

    The first 80% of ``full_data`` is the in-sample optimization set; the last 20%
    is never touched by optimization. The in-sample test runs first and returns
    immediately on failure (the validation set is not consulted). Only if both
    tests pass is the strategy persisted to ``validated_strategies``.
    """
    method = Config.PERMUTATION_OBJECTIVE
    result = {
        "promoted": False,
        "insample_p": None,
        "walkforward_p": None,
        "insample_score": None,
        "walkforward_score": None,
        "reason": "",
    }

    n = len(full_data)
    train_window = Config.WALK_FORWARD_TRAIN_MONTHS * 21
    test_window = Config.WALK_FORWARD_TEST_MONTHS * 21
    min_required = train_window + 2 * test_window

    if n < max(min_required, 60):
        result["reason"] = f"insufficient data ({n} bars < {max(min_required, 60)} required)"
        print(f"[Permutation] {symbol}: {result['reason']} — skipping permutation gate")
        return result

    split = int(n * 0.8)
    insample = full_data.iloc[:split]

    is_passed, is_p, is_score = run_insample_permutation_test(
        strategy_class, params, insample,
        n_iterations=Config.PERMUTATION_INSAMPLE_ITERS,
        method=method, symbol=symbol,
    )
    result["insample_p"] = is_p
    result["insample_score"] = is_score
    if not is_passed:
        result["reason"] = f"in-sample MCPT failed (p={is_p:.4f})"
        return result

    wf_passed, wf_p, wf_score = run_walkforward_permutation_test(
        strategy_class, params, full_data, train_window, test_window,
        n_iterations=Config.PERMUTATION_WALKFORWARD_ITERS,
        method=method, symbol=symbol,
    )
    result["walkforward_p"] = wf_p
    result["walkforward_score"] = wf_score
    if not wf_passed:
        result["reason"] = f"walk-forward MCPT failed (p={wf_p:.4f})"
        return result

    strategy_name = strategy_class().name
    _write_validated(symbol, strategy_name, params, method, is_p, wf_p, is_score, wf_score)
    result["promoted"] = True
    result["reason"] = "passed both gates"
    print(
        f"[Permutation] {symbol} {strategy_name} — IS p={is_p:.4f} WF p={wf_p:.4f} "
        f"— PROMOTED to live trading"
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Regime-aware validation (Steps 2–4)
# ──────────────────────────────────────────────────────────────────────────────

def _scale_regime_iters(base: int, n_regime_bars: int, total_bars: int) -> int:
    """Scale MCPT iterations by a regime's share of bars, floored at 200 (or base
    if base < 200, so reduced test configs stay fast)."""
    floor = min(200, base)
    if total_bars <= 0:
        return base
    scaled = int(base * (n_regime_bars / total_bars))
    return max(floor, min(base, scaled))


def _write_validated_regime(symbol, strategy_name, params, method,
                            regime_scores: dict, valid_flags: dict, best_regime) -> None:
    import json
    conn = None
    try:
        conn = _get_db_conn()
        if conn is None:
            print("[Permutation] No DATABASE_URL — skipping validated_strategies write")
            return
        _ensure_validated_table(conn)
        # Representative IS/WF p-values + scores from the best regime (or any validated).
        ref = regime_scores.get(best_regime) if best_regime else None
        if ref is None:
            ref = next((v for r, v in regime_scores.items() if valid_flags.get(r)), {})
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO validated_strategies (
                    symbol, strategy_name, parameters, objective_method,
                    insample_p, walkforward_p, insample_score, walkforward_score,
                    valid_bull_trend, valid_bear_trend, valid_high_vol, valid_choppy,
                    best_regime, regime_sharpes
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                symbol, strategy_name, json.dumps(params), method,
                ref.get("insample_p"), ref.get("walkforward_p"),
                ref.get("sharpe"), ref.get("walkforward_score"),
                bool(valid_flags.get("BULL_TREND")), bool(valid_flags.get("BEAR_TREND")),
                bool(valid_flags.get("HIGH_VOL")), bool(valid_flags.get("CHOPPY")),
                best_regime, json.dumps(regime_scores),
            ))
        conn.commit()
        print(f"[Permutation] Persisted regime-validated strategy {symbol}/{strategy_name} (best={best_regime})")
    except Exception:
        print(f"[Permutation] validated_strategies regime write failed:\n{traceback.format_exc()}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def validate_strategy_edge_regime_aware(
    strategy_class,
    params: dict,
    symbol: str,
    full_data: pd.DataFrame,
    regime_series,
) -> dict:
    """
    Regime-aware entry point. Validates the strategy *independently within each
    market regime* and persists per-regime validity flags.

    ``regime_series`` must be aligned to ``full_data`` (one regime label per bar).
    A regime is validated only if it has >= Config.REGIME_MIN_BARS bars AND the
    strategy passes both the in-sample and walk-forward MCPT restricted to that
    regime's bars. A strategy can be valid for some regimes and not others.

    Returns::

        {
          "promoted": bool,                 # valid for >= 1 regime
          "regime_scores": {regime: {sharpe, profit_factor, n_bars,
                                     insample_p, walkforward_p, p_value}},
          "valid_regimes": [regime, ...],
          "best_regime": regime | None,     # highest Sharpe among validated
          "reason": str,
        }
    """
    from discovery.regime_classifier import REGIMES

    method = Config.PERMUTATION_OBJECTIVE
    result = {
        "promoted": False,
        "regime_scores": {},
        "valid_regimes": [],
        "best_regime": None,
        "reason": "",
    }

    n = len(full_data)
    train_window = Config.WALK_FORWARD_TRAIN_MONTHS * 21
    test_window = Config.WALK_FORWARD_TEST_MONTHS * 21
    min_required = train_window + 2 * test_window
    if n < max(min_required, 60):
        result["reason"] = f"insufficient data ({n} bars)"
        print(f"[Permutation] {symbol}: {result['reason']} — skipping regime gate")
        return result

    regime_arr = np.asarray(pd.Series(regime_series).to_numpy())
    if regime_arr.shape[0] != n:
        result["reason"] = f"regime_series length {regime_arr.shape[0]} != data length {n}"
        print(f"[Permutation] {symbol}: {result['reason']} — skipping regime gate")
        return result

    split = int(n * 0.8)
    insample = full_data.iloc[:split]
    insample_regimes = regime_arr[:split]

    strategy = strategy_class()
    grid = strategy.param_grid()
    full_returns = calculate_log_returns(full_data["close"].to_numpy())

    regime_scores: dict = {}
    valid_flags: dict = {}

    for regime in REGIMES:
        is_mask = (insample_regimes == regime)
        full_mask = (regime_arr == regime)
        n_is = int(is_mask.sum())
        n_full = int(full_mask.sum())

        # Per-regime metrics (best in-sample config under the configured method).
        best_score, best_params = _optimize_score(strategy, grid, insample, method, mask=is_mask)
        sharpe = pf = 0.0
        if best_params:
            pos_full = strategy.position_vector(full_data, best_params)
            sharpe = calculate_objective_score(pos_full, full_returns, "sharpe", mask=full_mask)
            pf = calculate_objective_score(pos_full, full_returns, "profit_factor", mask=full_mask)

        entry = {
            "sharpe": round(float(sharpe), 4),
            "profit_factor": round(float(pf), 4),
            "n_bars": n_full,
            "insample_p": None,
            "walkforward_p": None,
            "walkforward_score": None,
            "p_value": None,
        }

        if n_is < Config.REGIME_MIN_BARS or n_full < Config.REGIME_MIN_BARS:
            print(
                f"[Permutation] {symbol}/{regime}: insufficient sample "
                f"(IS={n_is}, full={n_full} < {Config.REGIME_MIN_BARS}) — not scored"
            )
            valid_flags[regime] = False
            regime_scores[regime] = entry
            continue

        is_iters = _scale_regime_iters(Config.PERMUTATION_INSAMPLE_ITERS, n_is, split)
        is_passed, is_p, _is_score = run_insample_permutation_test(
            strategy_class, params, insample,
            n_iterations=is_iters, method=method, symbol=symbol,
            mask=is_mask, regime_label=regime,
        )
        entry["insample_p"] = is_p
        entry["p_value"] = is_p

        if not is_passed:
            valid_flags[regime] = False
            regime_scores[regime] = entry
            continue

        wf_iters = _scale_regime_iters(Config.PERMUTATION_WALKFORWARD_ITERS, n_full, n)
        wf_passed, wf_p, wf_score = run_walkforward_permutation_test(
            strategy_class, params, full_data, train_window, test_window,
            n_iterations=wf_iters, method=method, symbol=symbol,
            mask=full_mask, regime_label=regime,
        )
        entry["walkforward_p"] = wf_p
        entry["walkforward_score"] = round(float(wf_score), 4)
        entry["p_value"] = wf_p
        valid_flags[regime] = bool(wf_passed)
        regime_scores[regime] = entry

    valid_regimes = [r for r in REGIMES if valid_flags.get(r)]
    best_regime = None
    if valid_regimes:
        best_regime = max(valid_regimes, key=lambda r: regime_scores[r]["sharpe"])

    result["regime_scores"] = regime_scores
    result["valid_regimes"] = valid_regimes
    result["best_regime"] = best_regime
    result["promoted"] = bool(valid_regimes)

    if valid_regimes:
        _write_validated_regime(
            symbol, strategy.name, params, method,
            regime_scores, valid_flags, best_regime,
        )
        result["reason"] = f"validated for {valid_regimes} (best={best_regime})"
        print(
            f"[Permutation] {symbol} {strategy.name} — regime validation complete — "
            f"valid for {valid_regimes}, best={best_regime}"
        )
    else:
        result["reason"] = "no regime passed MCPT"
        print(f"[Permutation] {symbol} {strategy.name} — no regime passed MCPT — not promoted")

    return result
