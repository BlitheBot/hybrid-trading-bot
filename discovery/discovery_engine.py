import asyncio
import itertools
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytz
import requests
import scipy.stats as stats

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import Config
from discovery.permutation_framework import (
    SwingPositionStrategy,
    validate_strategy_edge_regime_aware,
)
from discovery.strategies.mean_reversion_strategy import MeanReversionPositionStrategy
from discovery.strategies.volume_breakout_strategy import VolumeBreakoutPositionStrategy
from discovery.strategies.insider_flow_strategy import InsiderFlowPositionStrategy
from discovery.strategies.smc_strategy import SMCPositionStrategy
from discovery.strategies.pead_strategy import PEADPositionStrategy
from discovery.strategies.short_interest_momentum_strategy import (
    ShortInterestMomentumPositionStrategy,
)
from discovery.strategies.sector_rotation_strategy import SectorRotationPositionStrategy

# Multi-factor families run in addition to the swing momentum family (family 1).
# Families gated off via config are filtered out in _active_extra_families().
_EXTRA_FAMILIES = [
    MeanReversionPositionStrategy,
    VolumeBreakoutPositionStrategy,
    InsiderFlowPositionStrategy,
    SMCPositionStrategy,
    PEADPositionStrategy,
    ShortInterestMomentumPositionStrategy,
    SectorRotationPositionStrategy,
]


def _active_extra_families() -> list:
    """The extra families enabled by config flags (overnight-build families gate individually)."""
    fams = [
        MeanReversionPositionStrategy,
        VolumeBreakoutPositionStrategy,
        InsiderFlowPositionStrategy,
        SMCPositionStrategy,
    ]
    if getattr(Config, "DISCOVERY_PEAD_ENABLED", True):
        fams.append(PEADPositionStrategy)
    if getattr(Config, "DISCOVERY_SHORT_MOMENTUM_ENABLED", True):
        fams.append(ShortInterestMomentumPositionStrategy)
    if getattr(Config, "DISCOVERY_SECTOR_ROTATION_ENABLED", True):
        fams.append(SectorRotationPositionStrategy)
    return fams
from discovery.regime_classifier import CHOPPY, classify_regime, realized_vol_proxy
from discovery.data_partitioner import DataPartitioner, PartitionViolation
from discovery.decay_monitor import (
    fetch_pending_revalidations,
    mark_revalidation,
    reset_decay_status,
)

PARAM_GRID = {
    "ema_short":     [20, 30, 50],
    "ema_long":      [100, 150, 200],
    "rsi_period":    [10, 14, 21],
    "rsi_entry_low": [35, 40, 45],
    "rsi_entry_high":[55, 60, 65],
}

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

_CACHE_MAX_AGE_HOURS = 24
_COMMISSION = 0.005   # per share
_SLIPPAGE_BPS = 2.0


# ── Database helpers ──────────────────────────────────────────────────────────

def _get_db_conn():
    import psycopg2
    url = Config.DATABASE_URL
    if not url:
        return None
    return psycopg2.connect(url)


def _ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS strategy_results (
                id               SERIAL PRIMARY KEY,
                symbol           VARCHAR(10),
                ema_short        INTEGER,
                ema_long         INTEGER,
                rsi_period       INTEGER,
                rsi_entry_low    FLOAT,
                rsi_entry_high   FLOAT,
                train_sharpe     FLOAT,
                test_sharpe      FLOAT,
                degradation      FLOAT,
                p_value          FLOAT,
                total_test_trades INTEGER,
                status           VARCHAR(20),
                permutation_tested BOOLEAN DEFAULT FALSE,
                discovered_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE (symbol, ema_short, ema_long, rsi_period, rsi_entry_low, rsi_entry_high)
            )
        """)
        # Backfill the column for databases created before the permutation gate.
        cur.execute(
            "ALTER TABLE strategy_results "
            "ADD COLUMN IF NOT EXISTS permutation_tested BOOLEAN DEFAULT FALSE"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                id            SERIAL PRIMARY KEY,
                symbol        VARCHAR(10),
                signal_type   VARCHAR(20),
                entry_time    TIMESTAMP,
                exit_time     TIMESTAMP,
                entry_price   FLOAT,
                exit_price    FLOAT,
                pnl_pct       FLOAT,
                hold_bars     INTEGER,
                ema_short     INTEGER,
                ema_long      INTEGER,
                rsi_at_entry  FLOAT,
                macd_at_entry FLOAT,
                market_regime VARCHAR(20),
                exit_reason   VARCHAR(30),
                discovered_at TIMESTAMP DEFAULT NOW()
            )
        """)
    conn.commit()


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_bars(symbol: str, data_client: StockHistoricalDataClient) -> pd.DataFrame:
    cache_path = DATA_DIR / f"{symbol}.parquet"

    if cache_path.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if age < timedelta(hours=_CACHE_MAX_AGE_HOURS):
            import pyarrow.parquet as pq
            return pq.read_table(str(cache_path)).to_pandas()

    start = datetime.strptime(Config.BACKTEST_START_DATE, "%Y-%m-%d").replace(tzinfo=pytz.utc)
    end   = datetime.strptime(Config.BACKTEST_END_DATE,   "%Y-%m-%d").replace(tzinfo=pytz.utc)

    req  = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
    bars = data_client.get_stock_bars(req)
    df   = bars.df

    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df.columns = [c.lower() for c in df.columns]

    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.Table.from_pandas(df), str(cache_path))
    return df


# ── Indicator computation (once per param combo) ──────────────────────────────

def _compute_indicators(bars: pd.DataFrame, params: dict) -> pd.DataFrame:
    df = bars.copy()
    df["EMA_short"] = ta.ema(df["close"], length=params["ema_short"])
    df["EMA_long"]  = ta.ema(df["close"], length=params["ema_long"])

    macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd_df is not None and not macd_df.empty:
        df["MACD"]        = macd_df.iloc[:, 0]
        df["MACD_Signal"] = macd_df.iloc[:, 2]
    else:
        df["MACD"] = df["MACD_Signal"] = np.nan

    df["RSI"] = ta.rsi(df["close"], length=params["rsi_period"])
    return df


# ── O(n) bar-by-bar simulation ────────────────────────────────────────────────

def _simulate(
    ind_df: pd.DataFrame,
    params: dict,
    skip_bars: int = 0,
    initial_capital: float = 10_000.0,
    symbol: str = "",
    db_conn=None,
) -> dict:
    rsi_entry_low  = params["rsi_entry_low"]
    rsi_entry_high = params["rsi_entry_high"]
    ema_short      = params["ema_short"]
    ema_long       = params["ema_long"]

    closes     = ind_df["close"].values
    highs      = ind_df["high"].values
    lows       = ind_df["low"].values
    timestamps = ind_df.index
    ema_s      = ind_df["EMA_short"].values
    ema_l      = ind_df["EMA_long"].values
    macd       = ind_df["MACD"].values
    macd_sig   = ind_df["MACD_Signal"].values
    rsi        = ind_df["RSI"].values

    slip = _SLIPPAGE_BPS / 10_000.0

    equity = initial_capital
    equity_curve = []
    trades = []

    in_position  = False
    entry_price  = stop_price = target_price = 0.0
    entry_bar    = 0
    entry_time   = None
    rsi_at_entry = macd_at_entry = shares = 0.0

    start_i = max(1, skip_bars)

    for i in range(start_i, len(ind_df)):
        equity_curve.append(equity)

        if any(np.isnan(v) for v in (ema_s[i], ema_l[i], macd[i], macd_sig[i], rsi[i])):
            continue

        if in_position:
            exit_price = None
            exit_reason = None

            if lows[i] <= stop_price:
                exit_price  = stop_price * (1.0 - slip)
                exit_reason = "stop"
            elif highs[i] >= target_price:
                exit_price  = target_price * (1.0 - slip)
                exit_reason = "target"

            if exit_price is not None:
                gross_pnl = (exit_price - entry_price) * shares
                cost      = _COMMISSION * shares * 2
                net_pnl   = gross_pnl - cost
                equity   += net_pnl
                pnl_pct   = (exit_price - entry_price) / entry_price * 100.0
                hold_bars = i - entry_bar

                trades.append({
                    "entry_price":  entry_price,
                    "exit_price":   exit_price,
                    "pnl_pct":      pnl_pct,
                    "net_pnl":      net_pnl,
                    "hold_bars":    hold_bars,
                    "exit_reason":  exit_reason,
                    "entry_time":   entry_time,
                    "exit_time":    timestamps[i],
                    "rsi_at_entry": rsi_at_entry,
                    "macd_at_entry":macd_at_entry,
                })

                if db_conn is not None:
                    regime = "bull" if ema_s[i] > ema_l[i] else "bear"
                    _insert_outcome(db_conn, symbol, entry_time, timestamps[i],
                                    entry_price, exit_price, pnl_pct, hold_bars,
                                    ema_short, ema_long, rsi_at_entry, macd_at_entry,
                                    regime, exit_reason)

                in_position = False

        else:
            if (ema_s[i] > ema_l[i]
                    and macd[i] > macd_sig[i] and macd[i - 1] <= macd_sig[i - 1]
                    and rsi_entry_low <= rsi[i] <= rsi_entry_high):

                ep     = closes[i] * (1.0 + slip)
                sl_pct = Config.STOP_LOSS_PERCENT / 100.0
                tp_pct = Config.TAKE_PROFIT_PERCENT / 100.0
                sl     = ep * (1.0 - sl_pct)
                tp     = ep * (1.0 + tp_pct)

                risk   = ep - sl
                reward = tp - ep
                if risk <= 0 or (reward / risk) < Config.SWING_MIN_RR_RATIO:
                    continue

                risk_amount = equity * (2.0 / 100.0)
                qty = math.floor(risk_amount / risk)
                if qty <= 0:
                    continue

                equity     -= _COMMISSION * qty
                in_position = True
                entry_price = ep
                stop_price  = sl
                target_price = tp
                entry_bar   = i
                entry_time  = timestamps[i]
                rsi_at_entry  = float(rsi[i])
                macd_at_entry = float(macd[i])
                shares = qty

    # Close open position at last bar
    if in_position:
        ep = closes[-1]
        gross_pnl = (ep - entry_price) * shares
        cost      = _COMMISSION * shares
        net_pnl   = gross_pnl - cost
        equity   += net_pnl
        pnl_pct   = (ep - entry_price) / entry_price * 100.0
        trades.append({
            "entry_price":  entry_price,
            "exit_price":   ep,
            "pnl_pct":      pnl_pct,
            "net_pnl":      net_pnl,
            "hold_bars":    len(ind_df) - 1 - entry_bar,
            "exit_reason":  "end_of_data",
            "entry_time":   entry_time,
            "exit_time":    timestamps[-1],
            "rsi_at_entry": rsi_at_entry,
            "macd_at_entry":macd_at_entry,
        })

    equity_curve.append(equity)

    return {"equity_curve": equity_curve, "trades": trades, "final_equity": equity}


def _insert_outcome(conn, symbol, entry_time, exit_time, entry_price, exit_price,
                    pnl_pct, hold_bars, ema_short, ema_long, rsi_at_entry,
                    macd_at_entry, regime, exit_reason):
    try:
        def _ts(t):
            return t.isoformat() if hasattr(t, "isoformat") else str(t)

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_outcomes (
                    symbol, signal_type, entry_time, exit_time,
                    entry_price, exit_price, pnl_pct, hold_bars,
                    ema_short, ema_long, rsi_at_entry, macd_at_entry,
                    market_regime, exit_reason
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                symbol, "swing_long", _ts(entry_time), _ts(exit_time),
                float(entry_price), float(exit_price), float(pnl_pct), int(hold_bars),
                int(ema_short), int(ema_long), float(rsi_at_entry), float(macd_at_entry),
                regime, exit_reason,
            ))
        conn.commit()
    except Exception as e:
        print(f"[DiscoveryEngine] signal_outcomes insert failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# ── Metrics ───────────────────────────────────────────────────────────────────

def _compute_metrics(equity_curve: list, trades: list) -> dict:
    empty = {"sharpe": 0.0, "max_dd": 0.0, "cagr_pct": 0.0,
             "win_rate": 0.0, "profit_factor": 0.0, "num_trades": 0}

    if len(equity_curve) < 2 or not trades:
        return empty

    eq      = np.array(equity_curve, dtype=float)
    returns = np.diff(eq) / np.where(eq[:-1] != 0, eq[:-1], 1e-9)

    sharpe = float((returns.mean() / returns.std()) * np.sqrt(252)) if returns.std() > 0 else 0.0

    peak   = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / np.where(peak != 0, peak, 1e-9)).min())

    years    = len(equity_curve) / 252
    cagr_pct = ((eq[-1] / eq[0]) ** (1.0 / years) - 1) * 100 if years > 0 and eq[0] > 0 else 0.0

    wins   = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate     = len(wins) / len(trades)
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss   = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    return {
        "sharpe":        round(sharpe, 4),
        "max_dd":        round(max_dd, 4),
        "cagr_pct":      round(float(cagr_pct), 4),
        "win_rate":      round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "num_trades":    len(trades),
    }


# ── Walk-forward ──────────────────────────────────────────────────────────────

def _walk_forward(
    symbol: str,
    bars: pd.DataFrame,
    params: dict,
    train_months: int,
    test_months: int,
    db_conn=None,
) -> pd.DataFrame:
    ind_df = _compute_indicators(bars, params)

    bars_per_month  = 21
    train_n         = train_months * bars_per_month
    test_n          = test_months  * bars_per_month

    rows      = []
    start_idx = 0
    window    = 0

    while True:
        train_end = start_idx + train_n
        test_end  = train_end + test_n

        if test_end > len(ind_df):
            break

        train_slice    = ind_df.iloc[start_idx:train_end]
        combined_slice = ind_df.iloc[start_idx:test_end]

        train_sim     = _simulate(train_slice, params, skip_bars=0, symbol=symbol)
        train_metrics = _compute_metrics(train_sim["equity_curve"], train_sim["trades"])

        # Test period: combined slice with train as warmup; DB logging only for test trades
        test_sim     = _simulate(combined_slice, params, skip_bars=train_n,
                                 symbol=symbol, db_conn=db_conn)
        test_metrics = _compute_metrics(test_sim["equity_curve"], test_sim["trades"])

        rows.append({
            "window":       window,
            "train_sharpe": train_metrics["sharpe"],
            "test_sharpe":  test_metrics["sharpe"],
            "test_cagr_pct":test_metrics["cagr_pct"],
            "test_trades":  test_metrics["num_trades"],
        })

        start_idx += test_n
        window    += 1

    return pd.DataFrame(rows)


# ── Statistical validation ────────────────────────────────────────────────────

def _validate(wf_df: pd.DataFrame, min_trades: int, p_threshold: float) -> tuple[bool, float]:
    if wf_df.empty or len(wf_df) < 2:
        return False, 1.0

    if wf_df["test_trades"].sum() < min_trades:
        return False, 1.0

    cagr_vals = wf_df["test_cagr_pct"].values
    if cagr_vals.mean() <= 0:
        return False, 1.0

    t_stat, p_value = stats.ttest_1samp(cagr_vals, 0)
    is_valid = (p_value < p_threshold) and (t_stat > 0)
    return is_valid, float(p_value)


# ── Discovery Engine ──────────────────────────────────────────────────────────

class DiscoveryEngine:
    """
    Grid-searches SwingStrategy parameter combinations via walk-forward validation.
    Persists results to PostgreSQL and sends a Slack summary on completion.
    """

    def __init__(self):
        self._data_client = StockHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
        )

    def _load_symbols(self) -> list[str]:
        """Pull top-250 tickers by volume from active_tickers, falling back to DISCOVERY_SYMBOLS."""
        import traceback as _tb
        try:
            from sqlalchemy import create_engine, text as _sql_text
            url = Config.DATABASE_URL
            if not url:
                raise ValueError("DATABASE_URL not set")
            engine = create_engine(url)
            with engine.connect() as conn:
                rows = conn.execute(_sql_text(
                    "SELECT ticker FROM active_tickers ORDER BY rank ASC LIMIT 250"
                )).mappings().fetchall()
            symbols = [r["ticker"] for r in rows] if rows else []
            if not symbols:
                raise ValueError("active_tickers is empty")
            print(f"[DiscoveryEngine] Symbol universe: {len(symbols)} tickers from active_tickers")
            return symbols
        except Exception as e:
            print(
                f"[DiscoveryEngine] active_tickers fetch failed — falling back to "
                f"DISCOVERY_SYMBOLS: {e}\n{_tb.format_exc()}"
            )
            return list(Config.DISCOVERY_SYMBOLS)

    def _slack(self, message: str):
        webhook = Config.SLACK_DECISIONS_WEBHOOK
        if not webhook:
            return
        try:
            requests.post(webhook, json={"text": message}, timeout=10)
        except Exception as e:
            print(f"[DiscoveryEngine] Slack error: {e}")

    def _process_revalidation_queue(self, spy_regime_df) -> None:
        """
        Process pending decay-driven re-validation requests before the full scan.
        Each request re-runs the regime-aware permutation gate for its symbol; on
        a promotion the strategy's decay status is reset to HEALTHY. Sync — call
        via asyncio.to_thread. Fail-open per request (one failure never blocks the
        rest of the queue or the weekly scan).
        """
        from sqlalchemy import create_engine
        import traceback as _tb
        if not Config.DATABASE_URL:
            return
        engine = None
        try:
            engine = create_engine(Config.DATABASE_URL, pool_pre_ping=True)
            # Only v1-owned requests — v2 (regime-aware) re-validates naturally on
            # its own weekly run, so it never queues work for this v1 engine.
            pending = fetch_pending_revalidations(engine, discovery_version="v1")
            if not pending:
                return
            print(f"[Discovery] Processing {len(pending)} pending v1 re-validation request(s) before weekly scan")
            for req in pending:
                sym, sname, rid = req["symbol"], req["strategy_name"], req["id"]
                mark_revalidation(engine, rid, "running")
                try:
                    bars = _load_bars(sym, self._data_client)
                    if bars is None or bars.empty:
                        print(f"[Discovery] Re-validation {sym}: no bars — marking failed")
                        mark_revalidation(engine, rid, "failed")
                        continue
                    # Same holdout wall (Task 2) so re-validation never sees holdout.
                    try:
                        part = DataPartitioner(bars, sym)
                        part.log_boundaries()
                        bars = part.get_non_holdout()
                    except (ValueError, PartitionViolation) as e:
                        print(f"[Partition] {sym}: partitioning failed ({e}) — using full series")
                    regime_series = self._regime_series_for(spy_regime_df, bars)
                    result = validate_strategy_edge_regime_aware(
                        SwingPositionStrategy, {}, sym, bars, regime_series
                    )
                    mark_revalidation(engine, rid, "complete")
                    if result.get("promoted"):
                        reset_decay_status(engine, sname, sym)
                        print(f"[Discovery] Re-validation {sym} {sname} — re-promoted, decay reset")
                    else:
                        print(f"[Discovery] Re-validation {sym} {sname} — still no edge ({result.get('reason')})")
                except Exception:
                    print(f"[Discovery] Re-validation {sym} raised:\n{_tb.format_exc()}")
                    mark_revalidation(engine, rid, "failed")
        except Exception:
            print(f"[Discovery] Re-validation queue processing failed:\n{_tb.format_exc()}")
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass

    def _regime_series_for(self, spy_regime_df, symbol_bars):
        """
        Align SPY regime labels to a symbol's bar index (forward-filled by date).
        Returns a list of regime strings the same length as symbol_bars, defaulting
        to CHOPPY where no SPY label is available (fail-safe).
        """
        n = len(symbol_bars)
        if spy_regime_df is None or "regime" not in getattr(spy_regime_df, "columns", []):
            return [CHOPPY] * n
        try:
            aligned = spy_regime_df["regime"].reindex(symbol_bars.index, method="ffill")
            aligned = aligned.fillna(CHOPPY)
            return aligned.tolist()
        except Exception as e:
            print(f"[DiscoveryEngine] regime alignment failed — defaulting to CHOPPY: {e}")
            return [CHOPPY] * n

    @staticmethod
    def _best_net_sharpe(edge_result) -> float | None:
        """Net-of-cost Sharpe of a regime-aware validation result's best regime."""
        if not edge_result or not edge_result.get("best_regime"):
            return None
        rs = edge_result.get("regime_scores", {}).get(edge_result["best_regime"], {})
        return rs.get("net_sharpe", rs.get("sharpe"))

    async def _run_extra_families(self, symbol, bars, spy_regime_df, swing_edge_result) -> int:
        """Validate the non-swing discovery families for one symbol.

        Each family runs the full regime-aware permutation + cost gate and writes
        its own row(s) to validated_strategies. Returns the number of families
        promoted (for the engine's running validated total). Logs the best family
        per symbol across all four (swing included) by net-of-cost Sharpe.
        """
        regime_series = self._regime_series_for(spy_regime_df, bars)
        # candidates: name -> net Sharpe of best regime (only promoted families)
        candidates: dict[str, float] = {}
        swing_net = self._best_net_sharpe(swing_edge_result)
        if swing_edge_result and swing_edge_result.get("promoted") and swing_net is not None:
            candidates[SwingPositionStrategy.name] = swing_net

        promoted_count = 0
        for fam in _active_extra_families():
            try:
                # Event-driven families (PEAD, etc.) expose an `enrich` hook that
                # attaches their per-bar data column. Extra columns survive the MCPT
                # permutation (it copies the frame and only rewrites OHLC), so the
                # enriched frame can be passed straight through. Fail-open: on any
                # enrich error fall back to the raw bars (family → all-flat).
                fam_bars = bars
                enrich = getattr(fam, "enrich", None)
                if enrich is not None:
                    try:
                        fam_bars = enrich(bars, symbol)
                        if fam_bars is None or len(fam_bars) != len(bars):
                            fam_bars = bars
                    except Exception:
                        import traceback as _tb
                        print(f"[Discovery] {symbol} family={fam.name} enrich raised:\n{_tb.format_exc()}")
                        fam_bars = bars
                res = await asyncio.to_thread(
                    validate_strategy_edge_regime_aware, fam, {}, symbol, fam_bars, regime_series
                )
                if res.get("promoted"):
                    promoted_count += 1
                    net = self._best_net_sharpe(res)
                    if net is not None:
                        candidates[fam.name] = net
                    print(
                        f"[Discovery] {symbol} family={fam.name} — PROMOTED "
                        f"valid={res.get('valid_regimes')} best={res.get('best_regime')}"
                    )
                else:
                    print(f"[Discovery] {symbol} family={fam.name} — no edge ({res.get('reason')})")
            except Exception:
                import traceback as _tb
                print(f"[Discovery] {symbol} family={fam.name} raised:\n{_tb.format_exc()}")

        if candidates:
            best_name = max(candidates, key=candidates.get)
            print(
                f"[Discovery] {symbol}: best family across {len(candidates)} promoted = "
                f"{best_name} (net Sharpe={candidates[best_name]:.2f})"
            )
        else:
            print(f"[Discovery] {symbol}: no family promoted across all {1 + len(_active_extra_families())} families")
        return promoted_count

    def _upsert_result(self, conn, symbol, params, wf_df, p_value, status,
                       permutation_tested=False):
        if wf_df.empty:
            train_sharpe = test_sharpe = 0.0
            total_test_trades = 0
        else:
            train_sharpe      = float(wf_df["train_sharpe"].mean())
            test_sharpe       = float(wf_df["test_sharpe"].mean())
            total_test_trades = int(wf_df["test_trades"].sum())

        degradation = train_sharpe - test_sharpe

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO strategy_results (
                        symbol, ema_short, ema_long, rsi_period, rsi_entry_low, rsi_entry_high,
                        train_sharpe, test_sharpe, degradation, p_value, total_test_trades,
                        status, permutation_tested
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, ema_short, ema_long, rsi_period, rsi_entry_low, rsi_entry_high)
                    DO UPDATE SET
                        train_sharpe      = EXCLUDED.train_sharpe,
                        test_sharpe       = EXCLUDED.test_sharpe,
                        degradation       = EXCLUDED.degradation,
                        p_value           = EXCLUDED.p_value,
                        total_test_trades = EXCLUDED.total_test_trades,
                        status            = EXCLUDED.status,
                        permutation_tested = EXCLUDED.permutation_tested,
                        discovered_at     = NOW()
                """, (
                    symbol,
                    int(params["ema_short"]), int(params["ema_long"]),
                    int(params["rsi_period"]), float(params["rsi_entry_low"]),
                    float(params["rsi_entry_high"]),
                    train_sharpe, test_sharpe, degradation,
                    p_value, total_test_trades, status, bool(permutation_tested),
                ))
            conn.commit()
        except Exception as e:
            print(f"[DiscoveryEngine] DB upsert failed: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

    async def run(self):
        # Pull top-250 symbols by volume from active_tickers so Discovery Engine
        # and the swing screener always work on the same universe.
        symbols = self._load_symbols()
        all_combos = list(itertools.product(
            PARAM_GRID["ema_short"],
            PARAM_GRID["ema_long"],
            PARAM_GRID["rsi_period"],
            PARAM_GRID["rsi_entry_low"],
            PARAM_GRID["rsi_entry_high"],
        ))
        # Filter invalid combos upfront
        all_combos = [c for c in all_combos if c[0] < c[1] and c[3] < c[4]]
        total_combos = len(all_combos)

        print(f"[DiscoveryEngine] Starting: {len(symbols)} symbols x {total_combos} combos = {len(symbols) * total_combos} runs")
        self._slack(f":mag: Strategy Discovery Engine started — {len(symbols)} symbols x {total_combos} param combos")

        db_conn = None
        try:
            db_conn = _get_db_conn()
        except Exception as e:
            print(f"[DiscoveryEngine] DB connection failed: {e}")

        if db_conn:
            _ensure_tables(db_conn)
            print("[DiscoveryEngine] PostgreSQL connected, tables verified")
        else:
            print("[DiscoveryEngine] No DATABASE_URL — skipping DB persistence")

        # SPY-based regime labels for the whole backtest window. VIX history is
        # approximated by SPY realized volatility (no per-bar VIX feed available);
        # live gating uses the true FRED VIX. Computed once and reindexed per symbol.
        spy_regime_df = None
        try:
            spy_bars = await asyncio.to_thread(_load_bars, "SPY", self._data_client)
            if spy_bars is not None and not spy_bars.empty:
                vix_proxy = realized_vol_proxy(spy_bars)
                spy_regime_df = classify_regime(spy_bars, vix_proxy)
                print(f"[DiscoveryEngine] SPY regime labels computed over {len(spy_regime_df)} bars")
        except Exception as e:
            import traceback as _tb
            print(f"[DiscoveryEngine] SPY regime computation failed — regime gate disabled: {e}\n{_tb.format_exc()}")

        # Decay-driven re-validation requests are validated first, before the scan.
        try:
            await asyncio.to_thread(self._process_revalidation_queue, spy_regime_df)
        except Exception as e:
            import traceback as _tb
            print(f"[DiscoveryEngine] re-validation queue step failed (non-fatal): {e}\n{_tb.format_exc()}")

        validated_total = 0
        processed_total = 0

        for symbol in symbols:
            print(f"\n[DiscoveryEngine] {symbol}: loading bars...")
            try:
                bars = await asyncio.to_thread(_load_bars, symbol, self._data_client)
            except Exception as e:
                print(f"[DiscoveryEngine] {symbol}: bar load failed — {e}")
                continue

            if bars.empty:
                print(f"[DiscoveryEngine] {symbol}: no data, skipping")
                continue

            # Out-of-sample integrity wall (Task 2): reserve the final 15% holdout —
            # optimization and validation run only on the train+val (non-holdout)
            # region. Boundaries are logged + persisted before any optimization.
            try:
                partitioner = DataPartitioner(bars, symbol)
                partitioner.log_boundaries()
                if db_conn:
                    partitioner.persist(db_conn)
                bars = partitioner.get_non_holdout()
            except (ValueError, PartitionViolation) as e:
                print(f"[Partition] {symbol}: partitioning failed ({e}) — using full series")

            print(f"[DiscoveryEngine] {symbol}: {len(bars)} bars (holdout reserved). Running {total_combos} combos...")
            symbol_validated = 0
            ttest_passers: list[dict] = []   # combos that clear the SciPy t-test gate

            for combo in all_combos:
                ema_short, ema_long, rsi_period, rsi_entry_low, rsi_entry_high = combo
                params = {
                    "ema_short":      ema_short,
                    "ema_long":       ema_long,
                    "rsi_period":     rsi_period,
                    "rsi_entry_low":  rsi_entry_low,
                    "rsi_entry_high": rsi_entry_high,
                }

                try:
                    wf_df = await asyncio.to_thread(
                        _walk_forward,
                        symbol, bars, params,
                        Config.WALK_FORWARD_TRAIN_MONTHS,
                        Config.WALK_FORWARD_TEST_MONTHS,
                        db_conn,
                    )

                    is_valid, p_value = _validate(
                        wf_df,
                        Config.DISCOVERY_MIN_TRADES,
                        Config.DISCOVERY_P_VALUE_THRESHOLD,
                    )

                    processed_total += 1

                    if is_valid:
                        # Defer the upsert until the permutation gate has run for
                        # this symbol, so the row carries the final status.
                        ttest_passers.append({"params": params, "wf_df": wf_df, "p_value": p_value})
                        ts  = wf_df["test_sharpe"].mean()
                        trs = wf_df["train_sharpe"].mean()
                        print(
                            f"[DiscoveryEngine] T-TEST PASS {symbol} "
                            f"EMA{ema_short}/{ema_long} RSI{rsi_period}[{rsi_entry_low}-{rsi_entry_high}] "
                            f"train={trs:.2f} test={ts:.2f} p={p_value:.4f}"
                        )
                    elif db_conn:
                        self._upsert_result(
                            db_conn, symbol, params, wf_df, p_value,
                            status="rejected", permutation_tested=False,
                        )

                except Exception as e:
                    print(f"[DiscoveryEngine] {symbol} {params}: error — {e}")

            # ── Second mandatory gate: permutation framework (Masters MCPT) ──────
            # The MCPT re-optimizes the whole grid on each permuted path, so it
            # tests the strategy *family* on this symbol — run it once and apply
            # the verdict to every t-test passer.
            n_ttest = len(ttest_passers)
            n_permutation = 0
            n_promoted = 0
            swing_edge_result = None

            if n_ttest == 0:
                edge_promoted = False
                perm_was_run = False
            elif not Config.PERMUTATION_ENABLED:
                # Permutation gate disabled — fall back to t-test-only promotion.
                edge_promoted = True
                perm_was_run = False
                print(f"[DiscoveryEngine] {symbol}: permutation gate disabled — promoting on t-test only")
            else:
                print(
                    f"[DiscoveryEngine] {symbol}: {n_ttest} combos passed t-test — "
                    f"running permutation framework (IS={Config.PERMUTATION_INSAMPLE_ITERS}, "
                    f"WF={Config.PERMUTATION_WALKFORWARD_ITERS} iters)"
                )
                perm_was_run = True
                try:
                    regime_series = self._regime_series_for(spy_regime_df, bars)
                    edge_result = await asyncio.to_thread(
                        validate_strategy_edge_regime_aware,
                        SwingPositionStrategy, ttest_passers[0]["params"], symbol, bars,
                        regime_series,
                    )
                    swing_edge_result = edge_result
                    edge_promoted = bool(edge_result.get("promoted"))
                    if edge_promoted:
                        print(
                            f"[DiscoveryEngine] {symbol}: regime validation — "
                            f"valid for {edge_result.get('valid_regimes')} "
                            f"best={edge_result.get('best_regime')}"
                        )
                except Exception as e:
                    import traceback as _tb
                    print(f"[DiscoveryEngine] {symbol}: permutation gate raised — {e}\n{_tb.format_exc()}")
                    edge_promoted = False

            if n_ttest > 0:
                final_status = "validated" if edge_promoted else (
                    "rejected_permutation" if perm_was_run else "rejected"
                )
                if edge_promoted:
                    n_permutation = n_ttest if perm_was_run else 0
                    n_promoted = n_ttest
                for entry in ttest_passers:
                    symbol_validated += 1 if edge_promoted else 0
                    if edge_promoted:
                        validated_total += 1
                    if db_conn:
                        self._upsert_result(
                            db_conn, symbol, entry["params"], entry["wf_df"], entry["p_value"],
                            status=final_status, permutation_tested=perm_was_run,
                        )

            print(
                f"[Discovery] {symbol}: {total_combos} combos tested → "
                f"{n_ttest} passed t-test → {n_permutation} passed permutation → "
                f"{n_promoted} promoted"
            )

            # ── Multi-factor families (Tasks 3 & 9): mean-reversion, volume-breakout,
            #    insider-flow, and SMC run alongside the swing momentum family. Best
            #    net-of-cost Sharpe across all five wins deployment for this symbol.
            if Config.DISCOVERY_MULTI_FAMILY_ENABLED and Config.PERMUTATION_ENABLED:
                try:
                    n_fam = await self._run_extra_families(
                        symbol, bars, spy_regime_df, swing_edge_result
                    )
                    validated_total += n_fam
                except Exception:
                    import traceback as _tb
                    print(f"[Discovery] {symbol}: multi-factor families raised:\n{_tb.format_exc()}")

        if db_conn:
            db_conn.close()

        # Correlation-aware portfolio construction (Task 4): after all strategies
        # are validated, select the diversified subset to deploy and persist it.
        if Config.PORTFOLIO_OPTIMIZER_ENABLED:
            try:
                from discovery.portfolio_optimizer import PortfolioOptimizer
                await asyncio.to_thread(PortfolioOptimizer().optimize)
            except Exception as e:
                import traceback as _tb
                print(f"[Portfolio] construction step failed (non-fatal): {e}\n{_tb.format_exc()}")

        summary = (
            f":white_check_mark: Discovery Engine complete — "
            f"{processed_total} combos processed, {validated_total} validated across {len(symbols)} symbols."
        )
        print(f"\n[DiscoveryEngine] {summary}")
        self._slack(summary)


if __name__ == "__main__":
    asyncio.run(DiscoveryEngine().run())
