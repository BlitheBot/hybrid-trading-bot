"""
Strategy decay monitoring — detects when validated strategies stop working in
live trading and throttles or disables them before they cause serious damage.

A strategy can pass rigorous backtest validation (permutation framework + regime
gating) and still decay live: the edge it exploited erodes, the regime shifts, or
the market adapts. This module continuously compares *live* performance (from
``signal_outcomes``) against the *backtested* baseline (from
``validated_strategies``) and applies a four-tier automated response.

Keying note: ``signal_outcomes`` records ``signal_type`` (e.g. 'swing_long',
'discovery_ema_trend', 'swing_bb', 'scalp_long') + ``symbol`` but not a free-form
strategy name, so decay status is keyed by ``(strategy_name=signal_type, symbol)``
— the finest granularity available in the live trade log.

Sharpe comparability: backtested Sharpe is an annualized per-bar figure; the live
Sharpe is computed per-trade and annualized by the *observed* trade frequency so
the decay ratio (live / backtested) is on a comparable scale. The CRITICAL tier
is driven by a *negative* live Sharpe, which is scale-robust (annualization is a
positive factor, so the sign is invariant).

Response tiers:
    HEALTHY   ratio >= 0.8            → 1.0x, no action
    DEGRADED  0.5 <= ratio < 0.8      → 0.5x, Slack warning
    DECAYING  ratio < 0.5 (>=30 sigs) → 0.25x + re-validation request, urgent Slack
    CRITICAL  live Sharpe < 0         → disable + cancel positions + re-validate + PagerDuty

All DB writes are sync (testable, callable via asyncio.to_thread). Side effects
that need the event loop / trading client (Slack, PagerDuty, position cancel) are
returned to the caller as descriptors rather than performed here.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime

import numpy as np
import pytz
from sqlalchemy import create_engine, text as sql_text

from config import Config

_TRADING_DAYS = 252

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
DECAYING = "DECAYING"
CRITICAL = "CRITICAL"


# ──────────────────────────────────────────────────────────────────────────────
# Pure classification (no DB — unit tested directly)
# ──────────────────────────────────────────────────────────────────────────────

def classify_decay(
    live_sharpe: float,
    backtested_sharpe: float | None,
    n_signals: int,
    consecutive_below: int = 0,
    live_sharpe_recent: float | None = None,
) -> dict:
    """
    Classify decay severity from live vs backtested Sharpe.

    Returns: {decay_ratio, status, position_multiplier,
              consecutive_below_threshold, is_decaying, is_critical}.

    CRITICAL (negative recent live Sharpe) overrides the ratio bands. When there
    is no positive backtested baseline the ratio is undefined and the strategy is
    treated as HEALTHY unless it is critically losing — we never penalize a
    strategy we cannot fairly compare.
    """
    recent = live_sharpe if live_sharpe_recent is None else live_sharpe_recent
    is_critical = (recent < 0) and (n_signals >= Config.DECAY_CRITICAL_MIN_SIGNALS)

    decay_ratio = None
    if backtested_sharpe and backtested_sharpe > 0:
        decay_ratio = live_sharpe / backtested_sharpe

    if is_critical:
        status, mult, is_decaying = CRITICAL, 0.0, True
    elif decay_ratio is None:
        status, mult, is_decaying = HEALTHY, 1.0, False
    elif decay_ratio < Config.DECAY_DEGRADED_RATIO and n_signals >= Config.DECAY_MIN_SIGNALS:
        status, mult, is_decaying = DECAYING, Config.DECAY_DECAYING_MULT, True
    elif decay_ratio < Config.DECAY_HEALTHY_RATIO:
        status, mult, is_decaying = DEGRADED, Config.DECAY_DEGRADED_MULT, False
    else:
        status, mult, is_decaying = HEALTHY, 1.0, False

    return {
        "decay_ratio": decay_ratio,
        "status": status,
        "position_multiplier": mult,
        "consecutive_below_threshold": consecutive_below,
        "is_decaying": is_decaying,
        "is_critical": is_critical,
    }


# Cap annualized Sharpe magnitude — near-identical returns (e.g. every trade
# hitting the same take-profit %) drive std → ~0 and the raw ratio explodes.
# Capping keeps the decay ratio sane while preserving sign and "very high/low".
_SHARPE_CAP = 50.0


def _annualized_sharpe(pnl_fractions: np.ndarray, span_years: float) -> float:
    """Per-trade Sharpe annualized by observed trade frequency, capped at ±50."""
    r = np.asarray(pnl_fractions, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return 0.0
    mean = r.mean()
    std = r.std()
    if std < 1e-9:
        # Degenerate (near-identical returns): preserve sign, avoid div-by-zero blowup.
        if abs(mean) < 1e-12:
            return 0.0
        return _SHARPE_CAP if mean > 0 else -_SHARPE_CAP
    trades_per_year = (r.size / span_years) if span_years and span_years > 0 else _TRADING_DAYS
    trades_per_year = max(1.0, min(trades_per_year, _TRADING_DAYS))
    value = mean / std * np.sqrt(trades_per_year)
    return float(np.clip(value, -_SHARPE_CAP, _SHARPE_CAP))


def _profit_factor(pnl_fractions: np.ndarray) -> float:
    r = np.asarray(pnl_fractions, dtype=float)
    gains = r[r > 0].sum()
    losses = abs(r[r < 0].sum())
    if losses == 0:
        return float(gains * 1e6) if gains > 0 else 0.0
    return float(gains / losses)


def _trailing_loss_streak(pnl_chrono: np.ndarray) -> int:
    """Count of most-recent consecutive losing trades (pnl <= 0)."""
    streak = 0
    for v in reversed(list(pnl_chrono)):
        if v <= 0:
            streak += 1
        else:
            break
    return streak


# ──────────────────────────────────────────────────────────────────────────────
# DB schema
# ──────────────────────────────────────────────────────────────────────────────

def ensure_decay_tables(engine) -> None:
    """Create the decay-status and re-validation-queue tables if missing."""
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS strategy_decay_status (
                strategy_name             VARCHAR(50),
                symbol                    VARCHAR(10),
                decay_ratio               FLOAT,
                status                    VARCHAR(20),
                position_multiplier       FLOAT DEFAULT 1.0,
                consecutive_signals_below INTEGER DEFAULT 0,
                last_checked              TIMESTAMP DEFAULT NOW(),
                re_validation_requested   BOOLEAN DEFAULT FALSE,
                disabled                  BOOLEAN DEFAULT FALSE,
                PRIMARY KEY (strategy_name, symbol)
            )
        """))
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS revalidation_queue (
                id                SERIAL PRIMARY KEY,
                strategy_name     VARCHAR(50),
                symbol            VARCHAR(10),
                reason            VARCHAR(20),
                discovery_version VARCHAR(10) DEFAULT 'v2',
                requested_at      TIMESTAMP DEFAULT NOW(),
                status            VARCHAR(20) DEFAULT 'pending'
            )
        """))
        # discovery_version disambiguates which engine owns a request (v1 grid
        # search vs v2 regime-aware). Added via ALTER for existing databases.
        conn.execute(sql_text(
            "ALTER TABLE revalidation_queue ADD COLUMN IF NOT EXISTS "
            "discovery_version VARCHAR(10) DEFAULT 'v2'"
        ))


# ──────────────────────────────────────────────────────────────────────────────
# Re-validation queue helpers (also used by the Discovery Engine)
# ──────────────────────────────────────────────────────────────────────────────

def request_revalidation(engine, strategy_name: str, symbol: str, reason: str = "decay",
                         discovery_version: str = "v2") -> None:
    """
    Enqueue a re-validation request (deduped against existing pending rows).

    ``discovery_version`` records which engine owns the request — defaults to 'v2',
    the live regime-aware pipeline, which re-validates the full universe on its
    weekly Friday run. The v1 grid-search engine only processes 'v1' requests.
    """
    if engine is None:
        return
    try:
        ensure_decay_tables(engine)
        with engine.begin() as conn:
            existing = conn.execute(sql_text("""
                SELECT 1 FROM revalidation_queue
                WHERE strategy_name=:s AND symbol=:sym AND status='pending' LIMIT 1
            """), {"s": strategy_name, "sym": symbol}).fetchone()
            if existing:
                return
            conn.execute(sql_text("""
                INSERT INTO revalidation_queue (strategy_name, symbol, reason, discovery_version, status)
                VALUES (:s, :sym, :reason, :ver, 'pending')
            """), {"s": strategy_name, "sym": symbol, "reason": reason, "ver": discovery_version})
            conn.execute(sql_text("""
                UPDATE strategy_decay_status SET re_validation_requested=TRUE
                WHERE strategy_name=:s AND symbol=:sym
            """), {"s": strategy_name, "sym": symbol})
        print(f"[Decay] Re-validation requested for {symbol} {strategy_name} "
              f"— reason: {reason}, engine: {discovery_version}")
    except Exception:
        print(f"[Decay] request_revalidation failed:\n{traceback.format_exc()}")


def fetch_pending_revalidations(engine, discovery_version: str | None = None) -> list[dict]:
    """
    Pending re-validation requests. When ``discovery_version`` is given, only
    requests owned by that engine are returned — so the v1 engine never picks up
    v2-owned requests (and vice versa).
    """
    if engine is None:
        return []
    try:
        ensure_decay_tables(engine)
        query = """
            SELECT id, strategy_name, symbol, reason, discovery_version
            FROM revalidation_queue WHERE status='pending'
        """
        params = {}
        if discovery_version is not None:
            query += " AND discovery_version=:ver"
            params["ver"] = discovery_version
        query += " ORDER BY requested_at ASC"
        with engine.connect() as conn:
            rows = conn.execute(sql_text(query), params).mappings().fetchall()
        return [dict(r) for r in rows]
    except Exception:
        print(f"[Decay] fetch_pending_revalidations failed:\n{traceback.format_exc()}")
        return []


def mark_revalidation(engine, request_id: int, status: str) -> None:
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(sql_text(
                "UPDATE revalidation_queue SET status=:st WHERE id=:id"
            ), {"st": status, "id": request_id})
    except Exception:
        print(f"[Decay] mark_revalidation failed:\n{traceback.format_exc()}")


def reset_decay_status(engine, strategy_name: str, symbol: str) -> None:
    """Reset a strategy to HEALTHY after a successful re-validation."""
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE strategy_decay_status
                SET status='HEALTHY', position_multiplier=1.0, disabled=FALSE,
                    re_validation_requested=FALSE, consecutive_signals_below=0,
                    last_checked=NOW()
                WHERE strategy_name=:s AND symbol=:sym
            """), {"s": strategy_name, "sym": symbol})
        print(f"[Decay] Reset {symbol} {strategy_name} to HEALTHY after re-validation")
    except Exception:
        print(f"[Decay] reset_decay_status failed:\n{traceback.format_exc()}")


# ──────────────────────────────────────────────────────────────────────────────
# Monitor
# ──────────────────────────────────────────────────────────────────────────────

class StrategyDecayMonitor:
    def __init__(self, engine=None):
        self._engine = engine or self._build_engine()
        self._all_status_cache: tuple[list[dict], float] | None = None
        if self._engine is not None:
            try:
                ensure_decay_tables(self._engine)
            except Exception:
                print(f"[Decay] ensure_decay_tables failed:\n{traceback.format_exc()}")

    @staticmethod
    def _build_engine():
        url = Config.DATABASE_URL
        if not url:
            return None
        try:
            return create_engine(url, pool_pre_ping=True)
        except Exception:
            print(f"[Decay] engine creation failed:\n{traceback.format_exc()}")
            return None

    # ── Live performance ───────────────────────────────────────────────────────

    def calculate_live_performance(self, strategy_name: str, symbol: str,
                                   lookback_signals: int = None) -> dict | None:
        """
        Live Sharpe / profit factor / win rate from the last N closed signals for
        this (signal_type, symbol). Returns None when fewer than DECAY_MIN_SIGNALS
        closed signals exist (insufficient sample — never act on thin data).
        """
        if self._engine is None:
            return None
        lookback = lookback_signals or Config.DECAY_LOOKBACK_SIGNALS
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql_text("""
                    SELECT pnl_pct, exit_time
                    FROM signal_outcomes
                    WHERE symbol=:sym AND signal_type=:st
                      AND exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                    ORDER BY exit_time DESC
                    LIMIT :n
                """), {"sym": symbol, "st": strategy_name, "n": lookback}).mappings().fetchall()
        except Exception:
            print(f"[Decay] live perf query failed for {symbol}/{strategy_name}:\n{traceback.format_exc()}")
            return None

        if len(rows) < Config.DECAY_MIN_SIGNALS:
            return None

        # Chronological order (oldest → newest).
        rows = list(reversed(rows))
        pnl = np.array([float(r["pnl_pct"]) / 100.0 for r in rows], dtype=float)
        exits = [r["exit_time"] for r in rows if r["exit_time"] is not None]

        span_years = 0.0
        if len(exits) >= 2:
            try:
                span_days = (exits[-1] - exits[0]).days
                span_years = max(span_days, 1) / 365.25
            except Exception:
                span_years = 0.0

        sharpe = _annualized_sharpe(pnl, span_years)
        # Recent-window Sharpe for the critical (negative-Sharpe) check.
        recent_n = Config.DECAY_CRITICAL_MIN_SIGNALS
        recent_pnl = pnl[-recent_n:] if pnl.size >= recent_n else pnl
        recent_span = span_years * (recent_pnl.size / pnl.size) if pnl.size else span_years
        sharpe_recent = _annualized_sharpe(recent_pnl, recent_span)

        win_rate = float((pnl > 0).mean())
        pf = _profit_factor(pnl)
        streak = _trailing_loss_streak(pnl)

        print(
            f"[Decay] {symbol} {strategy_name} — {len(rows)} signals analyzed | "
            f"live Sharpe={sharpe:.2f} win_rate={win_rate:.1%}"
        )
        return {
            "n_signals": len(rows),
            "sharpe": sharpe,
            "sharpe_recent": sharpe_recent,
            "profit_factor": pf,
            "win_rate": win_rate,
            "consecutive_below": streak,
        }

    # ── Backtested baseline ────────────────────────────────────────────────────

    def calculate_backtested_performance(self, strategy_name: str, symbol: str,
                                         current_regime: str | None = None) -> dict | None:
        """
        Backtested Sharpe / profit factor from validated_strategies for this symbol.
        Prefers the current regime's Sharpe (from regime_sharpes JSONB) when given,
        else falls back to the overall insample_score. Returns None with no baseline.
        """
        if self._engine is None:
            return None
        try:
            with self._engine.connect() as conn:
                row = conn.execute(sql_text("""
                    SELECT insample_score, walkforward_score, regime_sharpes
                    FROM validated_strategies
                    WHERE symbol=:sym
                    ORDER BY validated_at DESC NULLS LAST
                    LIMIT 1
                """), {"sym": symbol}).mappings().fetchone()
        except Exception as e:
            # validated_strategies may not exist until the first discovery run —
            # that's an expected transient, so keep it quiet (no baseline → None).
            if "validated_strategies" in str(e) and "exist" in str(e).lower():
                print(f"[Decay] no backtested baseline for {symbol} (validated_strategies not ready)")
            else:
                print(f"[Decay] backtested query failed for {symbol}: {e}")
            return None

        if row is None:
            return None

        sharpe = None
        pf = None
        regime_sharpes = row.get("regime_sharpes")
        if current_regime and regime_sharpes:
            try:
                rs = regime_sharpes if isinstance(regime_sharpes, dict) else __import__("json").loads(regime_sharpes)
                reg = rs.get(current_regime)
                if reg:
                    sharpe = reg.get("sharpe")
                    pf = reg.get("profit_factor")
            except Exception:
                print(f"[Decay] regime_sharpes parse failed for {symbol}:\n{traceback.format_exc()}")

        if sharpe is None:
            sharpe = row.get("insample_score")
        if sharpe is None:
            return None
        return {"sharpe": float(sharpe), "profit_factor": float(pf) if pf is not None else None}

    # ── Decay ratio ────────────────────────────────────────────────────────────

    def calculate_decay_ratio(self, live_performance: dict, backtested_performance: dict | None) -> dict:
        bt_sharpe = backtested_performance.get("sharpe") if backtested_performance else None
        return classify_decay(
            live_sharpe=live_performance["sharpe"],
            backtested_sharpe=bt_sharpe,
            n_signals=live_performance["n_signals"],
            consecutive_below=live_performance.get("consecutive_below", 0),
            live_sharpe_recent=live_performance.get("sharpe_recent"),
        )

    # ── Scan all strategies ────────────────────────────────────────────────────

    def get_decay_status_all_strategies(self, current_regime: str | None = None,
                                        use_cache: bool = True) -> list[dict]:
        """
        Decay check across every (signal_type, symbol) with >= DECAY_MIN_SIGNALS
        closed signals. Returns a list ranked by severity (CRITICAL first, then by
        ascending decay ratio). Cached for DECAY_CACHE_SECONDS.
        """
        if use_cache and self._all_status_cache is not None:
            data, ts = self._all_status_cache
            if time.time() - ts < Config.DECAY_CACHE_SECONDS:
                return data
        if self._engine is None:
            return []

        try:
            with self._engine.connect() as conn:
                combos = conn.execute(sql_text("""
                    SELECT symbol, signal_type, COUNT(*) AS n
                    FROM signal_outcomes
                    WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                      AND signal_type IS NOT NULL
                    GROUP BY symbol, signal_type
                    HAVING COUNT(*) >= :min_n
                """), {"min_n": Config.DECAY_MIN_SIGNALS}).mappings().fetchall()
        except Exception:
            print(f"[Decay] combo scan failed:\n{traceback.format_exc()}")
            return []

        results = []
        for c in combos:
            symbol, strategy_name = c["symbol"], c["signal_type"]
            try:
                live = self.calculate_live_performance(strategy_name, symbol)
                if live is None:
                    continue
                backtested = self.calculate_backtested_performance(strategy_name, symbol, current_regime)
                decay = self.calculate_decay_ratio(live, backtested)
                results.append({
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "live_sharpe": round(live["sharpe"], 4),
                    "backtested_sharpe": round(backtested["sharpe"], 4) if backtested else None,
                    "win_rate": round(live["win_rate"], 4),
                    "n_signals": live["n_signals"],
                    **decay,
                })
            except Exception:
                print(f"[Decay] status calc failed for {symbol}/{strategy_name}:\n{traceback.format_exc()}")

        severity = {CRITICAL: 0, DECAYING: 1, DEGRADED: 2, HEALTHY: 3}
        results.sort(key=lambda r: (severity.get(r["status"], 9),
                                    r["decay_ratio"] if r["decay_ratio"] is not None else 9))
        self._all_status_cache = (results, time.time())
        return results

    # ── Apply response (DB writes here; side effects returned to caller) ─────────

    def apply_decay_response(self, strategy_name: str, symbol: str, decay_status: dict) -> dict:
        """
        Persist decay status + multiplier and (for DECAYING/CRITICAL) enqueue a
        re-validation request. Returns an action descriptor for the async caller:
        {status, position_multiplier, disabled, notify:(msg, level)|None,
         cancel_positions:bool}.
        """
        status = decay_status["status"]
        mult = decay_status["position_multiplier"]
        ratio = decay_status.get("decay_ratio")
        ratio_str = f"{ratio:.2f}" if ratio is not None else "n/a"
        disabled = status == CRITICAL
        notify = None
        cancel_positions = False
        want_revalidation = status in (DECAYING, CRITICAL)

        if status == HEALTHY:
            print(f"[Decay] {symbol} {strategy_name} HEALTHY (ratio={ratio_str}) — no action")
        elif status == DEGRADED:
            notify = (f"⚠️ Decay DEGRADED: {symbol} {strategy_name} ratio={ratio_str} "
                      f"— position size reduced to {mult}x", "WARNING")
        elif status == DECAYING:
            notify = (f"🔻 Decay DECAYING: {symbol} {strategy_name} ratio={ratio_str} "
                      f"— size {mult}x and flagged for re-validation", "WARNING")
        elif status == CRITICAL:
            notify = (f"🚨 Decay CRITICAL: {symbol} {strategy_name} live Sharpe negative "
                      f"— strategy DISABLED, open positions will be closed, re-validation triggered",
                      "CRITICAL")
            cancel_positions = True

        if self._engine is not None:
            try:
                with self._engine.begin() as conn:
                    conn.execute(sql_text("""
                        INSERT INTO strategy_decay_status (
                            strategy_name, symbol, decay_ratio, status, position_multiplier,
                            consecutive_signals_below, last_checked, re_validation_requested, disabled
                        ) VALUES (:s, :sym, :ratio, :status, :mult, :streak, NOW(), :revalq, :disabled)
                        ON CONFLICT (strategy_name, symbol) DO UPDATE SET
                            decay_ratio=EXCLUDED.decay_ratio,
                            status=EXCLUDED.status,
                            position_multiplier=EXCLUDED.position_multiplier,
                            consecutive_signals_below=EXCLUDED.consecutive_signals_below,
                            last_checked=NOW(),
                            re_validation_requested=
                                strategy_decay_status.re_validation_requested OR EXCLUDED.re_validation_requested,
                            disabled=EXCLUDED.disabled
                    """), {
                        "s": strategy_name, "sym": symbol, "ratio": ratio, "status": status,
                        "mult": mult, "streak": decay_status.get("consecutive_below_threshold", 0),
                        "revalq": want_revalidation, "disabled": disabled,
                    })
            except Exception:
                print(f"[Decay] status write failed for {symbol}/{strategy_name}:\n{traceback.format_exc()}")

        if want_revalidation:
            request_revalidation(self._engine, strategy_name, symbol,
                                 reason="critical" if status == CRITICAL else "decay")

        return {
            "status": status,
            "position_multiplier": mult,
            "disabled": disabled,
            "notify": notify,
            "cancel_positions": cancel_positions,
        }

    # ── Cache for live gating (read by bot _process_symbol) ─────────────────────

    def load_status_map(self) -> dict:
        """{(strategy_name, symbol): {disabled, position_multiplier, status}} for fast gating."""
        if self._engine is None:
            return {}
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql_text("""
                    SELECT strategy_name, symbol, position_multiplier, disabled, status
                    FROM strategy_decay_status
                """)).mappings().fetchall()
            return {
                (r["strategy_name"], r["symbol"]): {
                    "disabled": bool(r["disabled"]),
                    "position_multiplier": float(r["position_multiplier"] if r["position_multiplier"] is not None else 1.0),
                    "status": r["status"],
                }
                for r in rows
            }
        except Exception:
            print(f"[Decay] load_status_map failed:\n{traceback.format_exc()}")
            return {}


def summarize_decay_status(engine) -> dict:
    """Weekly digest summary: tier counts + re-validation queue depth."""
    out = {"counts": {HEALTHY: 0, DEGRADED: 0, DECAYING: 0, CRITICAL: 0},
           "pending_revalidations": 0, "disabled": []}
    if engine is None:
        return out
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text(
                "SELECT status, symbol, strategy_name, disabled FROM strategy_decay_status"
            )).mappings().fetchall()
            pending = conn.execute(sql_text(
                "SELECT COUNT(*) FROM revalidation_queue WHERE status='pending'"
            )).scalar()
        for r in rows:
            out["counts"][r["status"]] = out["counts"].get(r["status"], 0) + 1
            if r["disabled"]:
                out["disabled"].append(f"{r['symbol']}/{r['strategy_name']}")
        out["pending_revalidations"] = int(pending or 0)
    except Exception:
        print(f"[Decay] summarize_decay_status failed:\n{traceback.format_exc()}")
    return out
