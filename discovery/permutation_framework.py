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
) -> float:
    """
    The single scoring function used everywhere in the framework.

    Applies the +1-bar forward shift (strategy_returns = S_t * R_{t+1}) so the
    position taken on bar t is rewarded with the return realized on bar t+1, then
    scores the resulting return vector directly (never a trade list).
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

def _optimize_score(strategy, grid: list[dict], df: pd.DataFrame, method: str) -> tuple[float, dict]:
    """Best objective score (and params) across the grid for one path."""
    returns = calculate_log_returns(df["close"].to_numpy())
    best_score = -np.inf
    best_params: dict = {}
    for params in grid:
        try:
            pos = strategy.position_vector(df, params)
        except Exception:
            print(f"[Permutation] position_vector failed for {params}:\n{traceback.format_exc()}")
            continue
        score = calculate_objective_score(pos, returns, method)
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
) -> float:
    """
    Rolling walk-forward: optimize on each train window, apply best params to the
    following test window, accumulate test-period strategy returns, then score the
    concatenated out-of-sample return vector once.
    """
    n = len(df)
    start = 0
    oos_returns: list[np.ndarray] = []

    while start + train_window + test_window <= n:
        train = df.iloc[start:start + train_window]
        _, best_params = _optimize_score(strategy, grid, train, method)
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
            oos_returns.append(pos[:-1] * test_ret[1:])
        start += test_window

    if not oos_returns:
        return 0.0
    return _score_strategy_returns(np.concatenate(oos_returns), method)


# ──────────────────────────────────────────────────────────────────────────────
# Multiprocessing workers (module-level for spawn picklability)
# ──────────────────────────────────────────────────────────────────────────────

def _insample_worker(payload: tuple) -> float:
    iteration, base_seed, df, grid, method, strat_name = payload
    seed = base_seed + iteration
    try:
        strategy = _STRATEGY_REGISTRY[strat_name]()
        perm = get_permutation(df, start_index=0, seed=seed)
        score, _ = _optimize_score(strategy, grid, perm, method)
        return float(score)
    except Exception:
        print(f"[Permutation] in-sample worker {iteration} failed:\n{traceback.format_exc()}")
        return float("nan")


def _walkforward_worker(payload: tuple) -> float:
    iteration, base_seed, df, grid, method, strat_name, train_window, test_window = payload
    seed = base_seed + iteration
    try:
        strategy = _STRATEGY_REGISTRY[strat_name]()
        perm = get_permutation(df, start_index=train_window, seed=seed)
        score = _walk_forward_score(strategy, grid, perm, train_window, test_window, method)
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
) -> tuple[bool, float, float]:
    """
    Returns (passed, p_value, real_score).

    Optimizes the strategy on real in-sample data for PF_real, then re-optimizes
    on ``n_iterations`` fully-permuted paths. p = count(PF_perm >= PF_real) / N.
    Passes when p <= Config.PERMUTATION_P_THRESHOLD.
    """
    method = method or Config.PERMUTATION_OBJECTIVE
    strategy = strategy_class()
    grid = strategy.param_grid()

    real_score, real_params = _optimize_score(strategy, grid, insample_data, method)
    print(
        f"[Permutation] {symbol} in-sample baseline {method}={real_score:.4f} "
        f"params={real_params}"
    )

    payloads = [
        (i, base_seed, insample_data, grid, method, strategy.name)
        for i in range(n_iterations)
    ]
    raw = _run_parallel(_insample_worker, payloads)
    perm_scores = np.array([s for s in raw if np.isfinite(s)], dtype=float)

    if perm_scores.size == 0:
        print(f"[Permutation] {symbol} in-sample: all permutations failed — treating as FAIL")
        return False, 1.0, real_score

    p_value = float(np.mean(perm_scores >= real_score))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _save_histogram(
        perm_scores, real_score,
        title=f"In-Sample MCPT — {symbol} ({method}) p={p_value:.4f}",
        path=REPORTS_DIR / f"insample_{symbol}_{ts}.png",
    )

    if p_value > Config.PERMUTATION_P_THRESHOLD:
        print(
            f"[Permutation] FAIL in-sample p={p_value:.4f} > {Config.PERMUTATION_P_THRESHOLD} "
            f"— strategy discarded (data mining bias detected)"
        )
        return False, p_value, real_score

    print(f"[Permutation] PASS in-sample p={p_value:.4f} — advancing to walk-forward test")
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
) -> tuple[bool, float, float]:
    """
    Returns (passed, p_value, real_score).

    Runs walk-forward on real data for WF_real, then on ``n_iterations`` partially
    permuted paths (training period intact via start_index=train_window).
    p = count(WF_perm >= WF_real) / N. Passes when p <= the configured threshold.
    """
    method = method or Config.PERMUTATION_OBJECTIVE
    strategy = strategy_class()
    grid = strategy.param_grid()

    real_score = _walk_forward_score(strategy, grid, full_data, train_window, test_window, method)
    print(f"[Permutation] {symbol} walk-forward baseline {method}={real_score:.4f}")

    payloads = [
        (i, base_seed, full_data, grid, method, strategy.name, train_window, test_window)
        for i in range(n_iterations)
    ]
    raw = _run_parallel(_walkforward_worker, payloads)
    perm_scores = np.array([s for s in raw if np.isfinite(s)], dtype=float)

    if perm_scores.size == 0:
        print(f"[Permutation] {symbol} walk-forward: all permutations failed — treating as FAIL")
        return False, 1.0, real_score

    p_value = float(np.mean(perm_scores >= real_score))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _save_histogram(
        perm_scores, real_score,
        title=f"Walk-Forward MCPT — {symbol} ({method}) p={p_value:.4f}",
        path=REPORTS_DIR / f"walkforward_{symbol}_{ts}.png",
    )

    if p_value > Config.PERMUTATION_P_THRESHOLD:
        print(
            f"[Permutation] FAIL walk-forward p={p_value:.4f} "
            f"— strategy rejected (out-of-sample selection luck)"
        )
        return False, p_value, real_score

    print(f"[Permutation] PASS walk-forward p={p_value:.4f} — strategy has genuine edge")
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
