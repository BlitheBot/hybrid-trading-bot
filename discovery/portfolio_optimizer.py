"""
Correlation-aware portfolio construction (Task 4).

After strategies are validated, pick a diversified subset to actually deploy:

1. Pull candidate strategy/symbol combos + their net-of-cost Sharpe from
   ``validated_strategies``.
2. Build per-combo daily return series from ``signal_outcomes`` (live trade P&L)
   and a pairwise Pearson correlation matrix over overlapping days.
3. Greedy selection: start from the highest-Sharpe combo, add the next-best combo
   only if its correlation with *every* already-selected combo is below
   ``PORTFOLIO_MAX_CORRELATION`` (default 0.7). Cap at ``PORTFOLIO_MAX_SIZE`` (20).
4. Estimate combined (equal-weight) portfolio Sharpe; deploy only if it clears
   ``PORTFOLIO_MIN_SHARPE`` (default 0.5).
5. Persist the chosen portfolio to ``strategy_portfolio`` for the live bot to gate on.

Pure functions (``daily_returns_from_outcomes``, ``correlation_matrix``,
``greedy_select``, ``combined_sharpe``) are DB-free and unit-tested directly.
Log: ``[Portfolio] Optimal portfolio: {n} strategies | combined Sharpe={…} | max pairwise corr={…}``.
"""
from __future__ import annotations

import traceback
from datetime import datetime

import numpy as np
import pandas as pd

from config import Config

_TRADING_DAYS = 252


# ──────────────────────────────────────────────────────────────────────────────
# Pure computation (no DB)
# ──────────────────────────────────────────────────────────────────────────────

def daily_returns_from_outcomes(outcomes: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    """Build a daily fractional-return series per (signal_type, symbol).

    ``outcomes`` needs columns: signal_type, symbol, entry_time, pnl_pct. Multiple
    trades on the same calendar day are summed; the series is indexed by date.
    """
    out: dict[tuple[str, str], pd.Series] = {}
    if outcomes is None or outcomes.empty:
        return out
    df = outcomes.copy()
    df = df.dropna(subset=["entry_time", "pnl_pct"])
    if df.empty:
        return out
    df["date"] = pd.to_datetime(df["entry_time"]).dt.normalize()
    df["ret"] = df["pnl_pct"].astype(float) / 100.0
    for (stype, sym), grp in df.groupby(["signal_type", "symbol"]):
        series = grp.groupby("date")["ret"].sum().sort_index()
        if not series.empty:
            out[(str(stype), str(sym))] = series
    return out


def correlation_matrix(
    returns: dict[tuple, pd.Series],
    keys: list[tuple],
    min_overlap: int,
) -> np.ndarray:
    """Pairwise Pearson correlation over overlapping days.

    Pairs with fewer than ``min_overlap`` overlapping observations are treated as
    uncorrelated (0.0) — we never *block* a candidate on an untrustworthy estimate.
    Diagonal is 1.0.
    """
    k = len(keys)
    corr = np.eye(k, dtype=float)
    for i in range(k):
        si = returns.get(keys[i])
        if si is None:
            continue
        for j in range(i + 1, k):
            sj = returns.get(keys[j])
            if sj is None:
                continue
            joined = pd.concat([si, sj], axis=1, join="inner")
            if len(joined) < min_overlap:
                c = 0.0
            else:
                a, b = joined.iloc[:, 0], joined.iloc[:, 1]
                if a.std(ddof=0) == 0 or b.std(ddof=0) == 0:
                    c = 0.0
                else:
                    c = float(np.corrcoef(a, b)[0, 1])
                    if not np.isfinite(c):
                        c = 0.0
            corr[i, j] = corr[j, i] = c
    return corr


def greedy_select(
    candidates: list[dict],
    corr: np.ndarray,
    max_corr: float,
    max_size: int,
) -> list[dict]:
    """Greedy diversified selection.

    ``candidates`` are dicts with at least ``sharpe`` (already sorted desc by the
    caller, aligned to ``corr`` rows). Returns the chosen candidates in selection
    order, each annotated with ``rank`` and ``max_pairwise_corr`` (vs selected set).
    """
    selected_idx: list[int] = []
    chosen: list[dict] = []
    for i in range(len(candidates)):
        if len(chosen) >= max_size:
            break
        if not selected_idx:
            max_c = 0.0
        else:
            max_c = max(abs(corr[i, j]) for j in selected_idx)
        if max_c < max_corr:
            entry = dict(candidates[i])
            entry["rank"] = len(chosen) + 1
            entry["max_pairwise_corr"] = round(max_c, 4)
            chosen.append(entry)
            selected_idx.append(i)
    return chosen


def combined_sharpe(
    returns: dict[tuple, pd.Series],
    keys: list[tuple],
    fallback_sharpes: list[float] | None = None,
) -> float:
    """Annualized Sharpe of the equal-weight combined daily return series.

    Falls back to the (conservative) average of individual Sharpes when there is
    not enough overlapping return history to form a combined series.
    """
    series = [returns[k] for k in keys if k in returns and not returns[k].empty]
    if len(series) >= 1:
        mat = pd.concat(series, axis=1, join="outer").fillna(0.0)
        combined = mat.mean(axis=1)
        sd = combined.std(ddof=0)
        if sd > 0 and len(combined) >= 2:
            return float(combined.mean() / sd * np.sqrt(_TRADING_DAYS))
    # Fallback: mean of individual Sharpes (a conservative lower bound that ignores
    # the diversification benefit we cannot estimate without return history).
    if fallback_sharpes:
        return float(np.mean(fallback_sharpes))
    return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# DB orchestration
# ──────────────────────────────────────────────────────────────────────────────

def _engine():
    from sqlalchemy import create_engine
    if not Config.DATABASE_URL:
        return None
    return create_engine(Config.DATABASE_URL, pool_pre_ping=True)


def ensure_portfolio_table(engine) -> None:
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS strategy_portfolio (
                id                        SERIAL PRIMARY KEY,
                build_id                  VARCHAR(40),
                strategy_name             VARCHAR(50),
                symbol                    VARCHAR(10),
                rank                      INTEGER,
                sharpe                    FLOAT,
                max_pairwise_corr         FLOAT,
                combined_portfolio_sharpe FLOAT,
                meets_min_sharpe          BOOLEAN DEFAULT FALSE,
                selected_at               TIMESTAMPTZ DEFAULT NOW()
            )
        """))


def _load_candidates(engine) -> list[dict]:
    """Highest-Sharpe validated strategy/symbol combos (best net Sharpe first).

    Uses the most recent ``validated_strategies`` row per (symbol, strategy_name)
    and its ``net_sharpe_after_costs`` (falling back to walkforward_score).
    """
    from sqlalchemy import text as sql_text
    rows = []
    with engine.connect() as conn:
        result = conn.execute(sql_text("""
            SELECT DISTINCT ON (symbol, strategy_name)
                   symbol, strategy_name,
                   net_sharpe_after_costs, walkforward_score
            FROM validated_strategies
            ORDER BY symbol, strategy_name, validated_at DESC NULLS LAST
        """)).mappings().fetchall()
    for r in result:
        sharpe = r.get("net_sharpe_after_costs")
        if sharpe is None:
            sharpe = r.get("walkforward_score")
        if sharpe is None:
            continue
        rows.append({
            "symbol": r["symbol"],
            "strategy_name": r["strategy_name"],
            "sharpe": float(sharpe),
        })
    rows.sort(key=lambda d: d["sharpe"], reverse=True)
    return rows


def _load_outcomes(engine) -> pd.DataFrame:
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        return pd.read_sql(
            sql_text("SELECT signal_type, symbol, entry_time, pnl_pct FROM signal_outcomes "
                     "WHERE pnl_pct IS NOT NULL AND entry_time IS NOT NULL"),
            conn,
        )


def _persist(engine, build_id, chosen, combined, meets_min) -> None:
    from sqlalchemy import text as sql_text
    ensure_portfolio_table(engine)
    with engine.begin() as conn:
        for c in chosen:
            conn.execute(sql_text("""
                INSERT INTO strategy_portfolio (
                    build_id, strategy_name, symbol, rank, sharpe,
                    max_pairwise_corr, combined_portfolio_sharpe, meets_min_sharpe
                ) VALUES (:bid,:name,:sym,:rank,:sharpe,:corr,:combined,:meets)
            """), {
                "bid": build_id, "name": c["strategy_name"], "sym": c["symbol"],
                "rank": c["rank"], "sharpe": c["sharpe"],
                "corr": c["max_pairwise_corr"], "combined": combined, "meets": meets_min,
            })


class PortfolioOptimizer:
    """Builds and persists the correlation-aware optimal portfolio."""

    def __init__(self, engine=None):
        self._engine = engine or _engine()

    def optimize(self) -> dict:
        """Run the full pipeline. Returns a summary dict; fail-open on any error."""
        result = {"selected": [], "combined_sharpe": 0.0, "max_corr": 0.0, "meets_min": False}
        if not Config.PORTFOLIO_OPTIMIZER_ENABLED:
            print("[Portfolio] optimizer disabled — skipping")
            return result
        if self._engine is None:
            print("[Portfolio] no DATABASE_URL — skipping portfolio construction")
            return result
        try:
            candidates = _load_candidates(self._engine)
            if not candidates:
                print("[Portfolio] no validated strategies yet — nothing to select")
                return result
            outcomes = _load_outcomes(self._engine)
            returns = daily_returns_from_outcomes(outcomes)

            keys = [(c["strategy_name"], c["symbol"]) for c in candidates]
            corr = correlation_matrix(returns, keys, Config.PORTFOLIO_MIN_OVERLAP)
            chosen = greedy_select(
                candidates, corr,
                Config.PORTFOLIO_MAX_CORRELATION, Config.PORTFOLIO_MAX_SIZE,
            )
            chosen_keys = [(c["strategy_name"], c["symbol"]) for c in chosen]
            combined = combined_sharpe(
                returns, chosen_keys, fallback_sharpes=[c["sharpe"] for c in chosen]
            )
            max_corr = max((c["max_pairwise_corr"] for c in chosen), default=0.0)
            meets_min = combined >= Config.PORTFOLIO_MIN_SHARPE

            build_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            self._persist_safe(build_id, chosen, combined, meets_min)

            print(
                f"[Portfolio] Optimal portfolio: {len(chosen)} strategies | "
                f"combined Sharpe={combined:.2f} | max pairwise corr={max_corr:.2f}"
            )
            if not meets_min:
                print(
                    f"[Portfolio] WARNING combined Sharpe {combined:.2f} < "
                    f"PORTFOLIO_MIN_SHARPE {Config.PORTFOLIO_MIN_SHARPE} — portfolio below threshold"
                )
            result.update({
                "selected": chosen, "combined_sharpe": combined,
                "max_corr": max_corr, "meets_min": meets_min, "build_id": build_id,
            })
        except Exception:
            print(f"[Portfolio] optimization failed:\n{traceback.format_exc()}")
        return result

    def _persist_safe(self, build_id, chosen, combined, meets_min) -> None:
        try:
            _persist(self._engine, build_id, chosen, combined, meets_min)
            print(f"[Portfolio] persisted build {build_id} ({len(chosen)} combos) to strategy_portfolio")
        except Exception:
            print(f"[Portfolio] persist failed:\n{traceback.format_exc()}")


if __name__ == "__main__":
    PortfolioOptimizer().optimize()
