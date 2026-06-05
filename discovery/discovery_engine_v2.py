"""
Discovery Engine v2 — extensible multi-strategy walk-forward backtester.

Usage:
    python -m discovery.discovery_engine_v2

Architecture:
- Auto-discovers all DiscoveryStrategy subclasses in discovery/strategies/
- Fetches top-100 S&P 500 symbols by 30-day avg volume
- Runs all combos via multiprocessing.Pool (min(4, cpu_count()) workers)
- Walk-forward: 24-month train / 3-month test, anchored windows per symbol
- Validation: p<0.05 t-test, ≥30 trades, ≥60% positive windows, degradation<0.5
- Regime tagging: bull/bear/high_vol Sharpe computed from full-dataset trade returns
- Correlation filter: keeps highest-Sharpe from pairs with >0.8 signal correlation
- Incremental: skips (symbol, strategy_type, params) combos already 'approved' in DB
- Writes results to discovery_results (JSONB parameters, pending_approval status)
"""
import csv
import io
import json
import math
import multiprocessing
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytz
import requests
import scipy.stats as stats
from sqlalchemy import create_engine, text as sql_text

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import Config
from discovery.strategies import load_all_strategies
from discovery.strategies.base import DiscoveryStrategy
from discovery.symbol_universe import get_discovery_candidates

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

_CACHE_MAX_AGE_HOURS = 24
_COMMISSION          = 0.005    # per share
_SLIPPAGE_BPS        = 2.0
_BARS_PER_MONTH      = 21
_TRAIN_MONTHS        = 24
_TEST_MONTHS         = 3
_MIN_TRADES          = 30
_P_THRESHOLD         = 0.05
_MIN_POSITIVE_WINDOW_RATE = 0.60
_MAX_DEGRADATION     = 0.5
_CORR_THRESHOLD      = 0.80
_MIN_REGIME_TRADES   = 5


# ── Parameter serialization ───────────────────────────────────────────────────

def _params_to_json(params: dict) -> str:
    def _prep(v):
        return list(v) if isinstance(v, tuple) else v
    return json.dumps({k: _prep(v) for k, v in params.items()}, sort_keys=True)


def _params_from_json(params_json: str, param_grid: dict) -> dict:
    raw = json.loads(params_json)
    result = {}
    for k, v in raw.items():
        if (k in param_grid and isinstance(v, list)
                and param_grid[k] and isinstance(param_grid[k][0], tuple)):
            result[k] = tuple(v)
        else:
            result[k] = v
    return result


# ── Database helpers ──────────────────────────────────────────────────────────

def _ensure_table(db_url: str):
    engine = create_engine(db_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS discovery_results (
                id              SERIAL PRIMARY KEY,
                symbol          VARCHAR(10),
                strategy_type   VARCHAR(50),
                parameters      JSONB,
                train_sharpe    FLOAT,
                test_sharpe     FLOAT,
                degradation     FLOAT,
                p_value         FLOAT,
                total_trades    INTEGER,
                win_rate        FLOAT,
                bull_sharpe     FLOAT,
                bear_sharpe     FLOAT,
                high_vol_sharpe FLOAT,
                best_regime     VARCHAR(20),
                status          VARCHAR(20) DEFAULT 'pending_approval',
                discovered_at   TIMESTAMP DEFAULT NOW(),
                UNIQUE (symbol, strategy_type, parameters)
            )
        """))
    engine.dispose()


def _load_approved_combos(db_url: str, symbol: str, strategy_type: str) -> set[str]:
    """Returns set of JSON-serialized param strings already approved in DB."""
    if not db_url:
        return set()
    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT parameters::text FROM discovery_results
                WHERE symbol = :sym AND strategy_type = :st AND status = 'approved'
            """), {"sym": symbol, "st": strategy_type}).fetchall()
        engine.dispose()
        return {row[0] for row in rows}
    except Exception:
        return set()


def _upsert_result(db_url: str, result: dict):
    if not db_url:
        return
    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(sql_text("""
                INSERT INTO discovery_results (
                    symbol, strategy_type, parameters,
                    train_sharpe, test_sharpe, degradation, p_value,
                    total_trades, win_rate,
                    bull_sharpe, bear_sharpe, high_vol_sharpe,
                    best_regime, status
                ) VALUES (
                    :symbol, :strategy_type, :parameters::jsonb,
                    :train_sharpe, :test_sharpe, :degradation, :p_value,
                    :total_trades, :win_rate,
                    :bull_sharpe, :bear_sharpe, :high_vol_sharpe,
                    :best_regime, :status
                )
                ON CONFLICT (symbol, strategy_type, parameters) DO UPDATE SET
                    train_sharpe    = EXCLUDED.train_sharpe,
                    test_sharpe     = EXCLUDED.test_sharpe,
                    degradation     = EXCLUDED.degradation,
                    p_value         = EXCLUDED.p_value,
                    total_trades    = EXCLUDED.total_trades,
                    win_rate        = EXCLUDED.win_rate,
                    bull_sharpe     = EXCLUDED.bull_sharpe,
                    bear_sharpe     = EXCLUDED.bear_sharpe,
                    high_vol_sharpe = EXCLUDED.high_vol_sharpe,
                    best_regime     = EXCLUDED.best_regime,
                    discovered_at   = NOW()
            """), result)
        engine.dispose()
    except Exception as e:
        print(f"[v2] DB upsert failed for {result.get('symbol')}: {e}")


# ── Bar data helpers ──────────────────────────────────────────────────────────

def _load_bars(symbol: str, data_client: StockHistoricalDataClient) -> pd.DataFrame:
    cache_path = DATA_DIR / f"{symbol}.parquet"
    if cache_path.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if age < timedelta(hours=_CACHE_MAX_AGE_HOURS):
            import pyarrow.parquet as pq
            return pq.read_table(str(cache_path)).to_pandas()

    start = datetime.strptime(Config.BACKTEST_START_DATE, "%Y-%m-%d").replace(tzinfo=pytz.utc)
    end   = datetime.strptime(Config.BACKTEST_END_DATE,   "%Y-%m-%d").replace(tzinfo=pytz.utc)
    req   = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
    bars  = data_client.get_stock_bars(req)
    df    = bars.df

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


def _load_bars_from_cache(symbol: str, data_dir: Path) -> pd.DataFrame:
    cache_path = data_dir / f"{symbol}.parquet"
    if not cache_path.exists():
        return pd.DataFrame()
    try:
        import pyarrow.parquet as pq
        return pq.read_table(str(cache_path)).to_pandas()
    except Exception:
        return pd.DataFrame()


# ── Walk-forward windows ──────────────────────────────────────────────────────

def _compute_windows(n_bars: int) -> list[tuple[int, int, int]]:
    """
    Returns list of (train_start, train_end, test_end) bar-index tuples.
    All strategy types for a symbol use the same windows (anchored comparison).
    """
    train_n = _TRAIN_MONTHS * _BARS_PER_MONTH
    test_n  = _TEST_MONTHS  * _BARS_PER_MONTH
    windows = []
    start = 0
    while start + train_n + test_n <= n_bars:
        windows.append((start, start + train_n, start + train_n + test_n))
        start += test_n
    return windows


# ── Generic simulation engine ─────────────────────────────────────────────────

def _simulate_generic(
    ind_df: pd.DataFrame,
    strategy: DiscoveryStrategy,
    params: dict,
    skip_bars: int = 0,
    initial_capital: float = 10_000.0,
) -> dict:
    """
    O(n) bar-by-bar simulation using precomputed indicators.
    Calls strategy.generate_signals() once on the full slice, then iterates bars.
    If strategy.exit_signal() returns non-None, uses it as an additional exit.
    If strategy.use_atr_stops, reads ind_df["atr"] for per-bar stop/target sizing.
    """
    signals   = strategy.generate_signals(ind_df, params)
    exit_sigs = strategy.exit_signal(ind_df, params)

    use_atr   = getattr(strategy, "use_atr_stops", False)
    atr_stop  = getattr(strategy, "atr_stop_mult", 1.5)
    atr_tp    = getattr(strategy, "atr_tp_mult",   4.5)

    closes    = ind_df["close"].values
    highs     = ind_df["high"].values
    lows      = ind_df["low"].values
    timestamps = ind_df.index
    sig_arr   = signals.values
    exit_arr  = exit_sigs.values if exit_sigs is not None else None
    atr_arr   = ind_df["atr"].values if (use_atr and "atr" in ind_df.columns) else None

    sl_pct = Config.STOP_LOSS_PERCENT / 100.0
    tp_pct = Config.TAKE_PROFIT_PERCENT / 100.0
    slip   = _SLIPPAGE_BPS / 10_000.0

    equity       = initial_capital
    equity_curve = []
    trades       = []

    in_position  = False
    entry_price  = stop_price = target_price = 0.0
    entry_bar    = 0
    entry_time   = None
    shares       = 0.0

    start_i = max(1, skip_bars)

    for i in range(start_i, len(ind_df)):
        equity_curve.append(equity)

        if np.isnan(closes[i]):
            continue

        if in_position:
            exit_price  = None
            exit_reason = None

            if exit_arr is not None and not np.isnan(exit_arr[i]) and bool(exit_arr[i]):
                exit_price  = closes[i] * (1.0 - slip)
                exit_reason = "exit_signal"
            elif lows[i] <= stop_price:
                exit_price  = stop_price * (1.0 - slip)
                exit_reason = "stop"
            elif highs[i] >= target_price:
                exit_price  = target_price * (1.0 - slip)
                exit_reason = "target"

            if exit_price is not None:
                net_pnl = (exit_price - entry_price) * shares - _COMMISSION * shares * 2
                equity += net_pnl
                pnl_pct = (exit_price - entry_price) / entry_price * 100.0
                trades.append({
                    "pnl_pct":    pnl_pct,
                    "net_pnl":    net_pnl,
                    "hold_bars":  i - entry_bar,
                    "exit_reason": exit_reason,
                    "entry_time":  entry_time,
                })
                in_position = False

        else:
            if not np.isnan(sig_arr[i]) and bool(sig_arr[i]):
                ep = closes[i] * (1.0 + slip)

                if use_atr and atr_arr is not None and not np.isnan(atr_arr[i]) and atr_arr[i] > 0:
                    sl = ep - atr_stop * atr_arr[i]
                    tp = ep + atr_tp  * atr_arr[i]
                else:
                    sl = ep * (1.0 - sl_pct)
                    tp = ep * (1.0 + tp_pct)

                risk   = ep - sl
                reward = tp - ep
                if risk <= 0 or (reward / risk) < Config.SWING_MIN_RR_RATIO:
                    continue

                risk_amount = equity * (2.0 / 100.0)
                qty = math.floor(risk_amount / risk)
                if qty <= 0:
                    continue

                equity      -= _COMMISSION * qty
                in_position  = True
                entry_price  = ep
                stop_price   = sl
                target_price = tp
                entry_bar    = i
                entry_time   = timestamps[i]
                shares       = qty

    if in_position:
        ep      = closes[-1]
        net_pnl = (ep - entry_price) * shares - _COMMISSION * shares
        equity += net_pnl
        trades.append({
            "pnl_pct":    (ep - entry_price) / entry_price * 100.0,
            "net_pnl":    net_pnl,
            "hold_bars":  len(ind_df) - 1 - entry_bar,
            "exit_reason": "end_of_data",
            "entry_time":  entry_time,
        })

    equity_curve.append(equity)
    return {"equity_curve": equity_curve, "trades": trades}


# ── Metrics ───────────────────────────────────────────────────────────────────

def _compute_metrics(equity_curve: list, trades: list) -> dict:
    if len(equity_curve) < 2 or not trades:
        return {"sharpe": 0.0, "win_rate": 0.0, "num_trades": 0}
    eq      = np.array(equity_curve, dtype=float)
    returns = np.diff(eq) / np.where(eq[:-1] != 0, eq[:-1], 1e-9)
    sharpe  = float((returns.mean() / returns.std()) * np.sqrt(252)) if returns.std() > 0 else 0.0
    wins    = sum(1 for t in trades if t["pnl_pct"] > 0)
    return {
        "sharpe":     round(sharpe, 4),
        "win_rate":   round(wins / len(trades), 4),
        "num_trades": len(trades),
    }


def _trade_sharpe(trades: list) -> float | None:
    """Annualized Sharpe from trade P&L — used for regime sub-period analysis."""
    if len(trades) < _MIN_REGIME_TRADES:
        return None
    returns = np.array([t["pnl_pct"] for t in trades])
    if returns.std() == 0:
        return None
    avg_hold = max(np.mean([t.get("hold_bars", 1) for t in trades]), 1)
    ann      = np.sqrt(252 / avg_hold)
    return round(float(returns.mean() / returns.std() * ann), 4)


# ── Walk-forward ──────────────────────────────────────────────────────────────

def _walk_forward(
    ind_df: pd.DataFrame,
    strategy: DiscoveryStrategy,
    params: dict,
    windows: list[tuple[int, int, int]],
) -> pd.DataFrame:
    rows = []
    for train_start, train_end, test_end in windows:
        train_n = train_end - train_start
        train_sl = ind_df.iloc[train_start:train_end]
        combined = ind_df.iloc[train_start:test_end]

        train_sim = _simulate_generic(train_sl, strategy, params)
        test_sim  = _simulate_generic(combined, strategy, params, skip_bars=train_n)

        tm = _compute_metrics(train_sim["equity_curve"], train_sim["trades"])
        sm = _compute_metrics(test_sim["equity_curve"], test_sim["trades"])
        rows.append({
            "train_sharpe":  tm["sharpe"],
            "test_sharpe":   sm["sharpe"],
            "test_trades":   sm["num_trades"],
            "test_win_rate": sm["win_rate"],
        })
    return pd.DataFrame(rows)


# ── Statistical validation ────────────────────────────────────────────────────

def _validate(wf_df: pd.DataFrame) -> tuple[bool, float, dict]:
    if wf_df.empty or len(wf_df) < 2:
        return False, 1.0, {}

    total_trades = int(wf_df["test_trades"].sum())
    if total_trades < _MIN_TRADES:
        return False, 1.0, {}

    sharpe_vals = wf_df["test_sharpe"].values
    if sharpe_vals.mean() <= 0:
        return False, 1.0, {}

    t_stat, p_value = stats.ttest_1samp(sharpe_vals, 0)
    if p_value >= _P_THRESHOLD or t_stat <= 0:
        return False, float(p_value), {}

    positive_rate = float((wf_df["test_sharpe"] > 0).sum()) / len(wf_df)
    if positive_rate < _MIN_POSITIVE_WINDOW_RATE:
        return False, float(p_value), {}

    train_sharpe = float(wf_df["train_sharpe"].mean())
    test_sharpe  = float(wf_df["test_sharpe"].mean())
    degradation  = train_sharpe - test_sharpe

    if degradation >= _MAX_DEGRADATION:
        return False, float(p_value), {}

    return True, float(p_value), {
        "train_sharpe": train_sharpe,
        "test_sharpe":  test_sharpe,
        "degradation":  degradation,
        "total_trades": total_trades,
        "win_rate":     float(wf_df["test_win_rate"].mean()),
    }


# ── Regime tagging ────────────────────────────────────────────────────────────

def _regime_sharpes(
    ind_df: pd.DataFrame,
    strategy: DiscoveryStrategy,
    params: dict,
    spy_mask: dict[str, str],
    vix_mask: set[str],
) -> tuple[float | None, float | None, float | None]:
    """
    Runs a single full-dataset simulation and splits trades by entry regime.
    This avoids bar-continuity issues from filtering the bars DataFrame.
    Note: trades entered at regime boundaries use the regime of their entry bar.
    """
    sim    = _simulate_generic(ind_df, strategy, params)
    trades = sim["trades"]

    def _date_str(t):
        ts = t.get("entry_time")
        if ts is None:
            return None
        return ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]

    bull_trades     = [t for t in trades if spy_mask.get(_date_str(t)) == "bull"]
    bear_trades     = [t for t in trades if spy_mask.get(_date_str(t)) == "bear"]
    high_vol_trades = [t for t in trades if (_date_str(t) or "") in vix_mask]

    return _trade_sharpe(bull_trades), _trade_sharpe(bear_trades), _trade_sharpe(high_vol_trades)


def _best_regime(bull: float | None, bear: float | None, high_vol: float | None) -> str:
    candidates = {k: v for k, v in [("bull", bull), ("bear", bear), ("high_vol", high_vol)] if v is not None}
    return max(candidates, key=lambda k: candidates[k]) if candidates else "all"


# ── Correlation filter ────────────────────────────────────────────────────────

def _filter_correlated(
    bars: pd.DataFrame,
    strategy: DiscoveryStrategy,
    validated: list[dict],
) -> list[dict]:
    """Within a strategy family, drop lower-Sharpe combos with >0.8 signal correlation."""
    if len(validated) <= 1:
        return validated

    sigs: list[np.ndarray] = []
    for r in validated:
        try:
            p   = _params_from_json(r["parameters"], strategy.param_grid)
            ind = strategy.compute_indicators(bars, p)
            s   = strategy.generate_signals(ind, p).fillna(False).astype(float).values
        except Exception:
            s = np.zeros(len(bars))
        sigs.append(s)

    keep = [True] * len(validated)
    for i in range(len(validated)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(validated)):
            if not keep[j]:
                continue
            if sigs[i].sum() < 5 or sigs[j].sum() < 5:
                continue
            corr = float(np.corrcoef(sigs[i], sigs[j])[0, 1])
            if corr > _CORR_THRESHOLD:
                if validated[i]["test_sharpe"] >= validated[j]["test_sharpe"]:
                    keep[j] = False
                else:
                    keep[i] = False
                    break

    removed = keep.count(False)
    if removed:
        print(f"[v2] Correlation filter removed {removed} redundant {strategy.strategy_type} combos")
    return [v for v, k in zip(validated, keep) if k]


# ── Worker function (module-level for multiprocessing pickle) ─────────────────

def _run_symbol(args: dict) -> list[dict]:
    """
    Processes all discovered strategy families × all combos for one symbol.
    Called by Pool.imap_unordered — must be a module-level function.
    """
    symbol   = args["symbol"]
    db_url   = args["db_url"]
    data_dir = Path(args["data_dir"])
    spy_mask = args["spy_mask"]
    vix_mask = args["vix_mask"]

    bars = _load_bars_from_cache(symbol, data_dir)
    if bars.empty or len(bars) < 252:
        print(f"[v2:{symbol}] Insufficient data ({len(bars)} bars), skipping")
        return []

    windows = _compute_windows(len(bars))
    if not windows:
        print(f"[v2:{symbol}] Not enough bars for walk-forward windows")
        return []

    strategy_classes = load_all_strategies()
    all_validated    = []

    for strategy_cls in strategy_classes:
        strategy = strategy_cls()
        approved = _load_approved_combos(db_url, symbol, strategy.strategy_type)
        combos   = strategy.get_combos()
        validated: list[dict] = []

        for params in combos:
            params_json = _params_to_json(params)
            if params_json in approved:
                continue

            try:
                ind_df = strategy.compute_indicators(bars, params)
                wf_df  = _walk_forward(ind_df, strategy, params, windows)
                is_valid, p_value, metrics = _validate(wf_df)

                if not is_valid:
                    continue

                bull_s, bear_s, hvol_s = _regime_sharpes(
                    ind_df, strategy, params, spy_mask, vix_mask
                )

                result = {
                    "symbol":         symbol,
                    "strategy_type":  strategy.strategy_type,
                    "parameters":     params_json,
                    "train_sharpe":   round(metrics["train_sharpe"], 4),
                    "test_sharpe":    round(metrics["test_sharpe"],  4),
                    "degradation":    round(metrics["degradation"],  4),
                    "p_value":        round(p_value, 6),
                    "total_trades":   metrics["total_trades"],
                    "win_rate":       round(metrics["win_rate"], 4),
                    "bull_sharpe":    bull_s,
                    "bear_sharpe":    bear_s,
                    "high_vol_sharpe": hvol_s,
                    "best_regime":    _best_regime(bull_s, bear_s, hvol_s),
                    "status":         "pending_approval",
                }
                validated.append(result)
                print(
                    f"[v2:{symbol}] VALIDATED {strategy.strategy_type} "
                    f"test_sharpe={metrics['test_sharpe']:.2f} "
                    f"p={p_value:.4f} trades={metrics['total_trades']}"
                )

            except Exception as e:
                print(f"[v2:{symbol}/{strategy.strategy_type}] Error: {e}")

        validated = _filter_correlated(bars, strategy, validated)
        all_validated.extend(validated)

    return all_validated


# ── Claude debate gate (main-process, sync wrapper around async call_llm) ─────

def _debate_strategy_sync(result: dict) -> bool:
    """
    Asks DeepSeek Pro to review a statistically validated strategy and return APPROVE/REJECT.
    Runs synchronously in the main process (safe — no event loop is active here).
    Fails open: returns True on any error so an LLM outage never blocks results.
    Only called when Config.DISCOVERY_DEBATE_ENABLED=True.
    """
    import asyncio
    from llm_client import call_llm_with_model, get_llm_cost_estimate, LLMError, MODEL_PRO

    params_dict = json.loads(result["parameters"]) if isinstance(result["parameters"], str) else result["parameters"]
    params_str  = ", ".join(f"{k}={v}" for k, v in params_dict.items())
    bull_s = f"{result['bull_sharpe']:.2f}"  if result.get("bull_sharpe")  is not None else "N/A"
    bear_s = f"{result['bear_sharpe']:.2f}"  if result.get("bear_sharpe")  is not None else "N/A"

    prompt = (
        "You are a quantitative strategy validator. Review this backtested trading strategy "
        "and decide if it shows genuine edge or looks like over-fitting / data-mining.\n\n"
        f"Symbol:          {result['symbol']}\n"
        f"Strategy type:   {result['strategy_type']}\n"
        f"Parameters:      {params_str}\n"
        f"Test Sharpe:     {result.get('test_sharpe', 0):.2f}\n"
        f"Train Sharpe:    {result.get('train_sharpe', 0):.2f}\n"
        f"Degradation:     {result.get('degradation', 0):.2f}  (train − test; lower is better)\n"
        f"Win Rate:        {result.get('win_rate', 0)*100:.0f}%\n"
        f"Total Trades:    {result.get('total_trades', 0)}\n"
        f"Best Regime:     {result.get('best_regime', 'N/A')}\n"
        f"Bull Sharpe:     {bull_s}\n"
        f"Bear Sharpe:     {bear_s}\n\n"
        "Does this strategy show genuine edge worth live-testing? "
        "Write a detailed analysis (3-4 sentences), then end with APPROVE or REJECT."
    )
    try:
        resp = asyncio.run(call_llm_with_model(MODEL_PRO, prompt, max_tokens=2000))
        approved = "APPROVE" in resp.text.upper().split()[-1] or resp.text.upper().endswith("APPROVE")
        # Also accept if APPROVE appears and REJECT does not (simpler check)
        text_upper = resp.text.upper()
        if "APPROVE" in text_upper and "REJECT" not in text_upper:
            approved = True
        elif "REJECT" in text_upper and "APPROVE" not in text_upper:
            approved = False
        verdict  = "APPROVE" if approved else "REJECT"
        print(f"[v2] Debate {verdict}: {result['symbol']} {result['strategy_type']} — {resp.text[:120]}")
        return approved
    except LLMError as e:
        print(f"[v2] Debate LLMError for {result['symbol']} {result['strategy_type']}: {e} — defaulting APPROVE")
        return True  # fail open
    except Exception as e:
        print(f"[v2] Debate error for {result['symbol']} {result['strategy_type']}: {e} — defaulting APPROVE")
        return True  # fail open


# ── Discovery Engine ──────────────────────────────────────────────────────────

class DiscoveryEngineV2:
    def __init__(self):
        self._data_client = StockHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
        )
        self._db_url = Config.DATABASE_URL

    def _slack(self, msg: str):
        webhook = Config.SLACK_DECISIONS_WEBHOOK
        if not webhook:
            return
        try:
            requests.post(webhook, json={"text": msg}, timeout=10)
        except Exception as e:
            print(f"[v2] Slack error: {e}")

    def _fetch_vix_mask(self) -> set[str]:
        """Returns set of date strings where daily VIX > 20."""
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
        req = urllib.request.Request(url, headers={"User-Agent": "HybridTradingBot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8")
            reader = csv.reader(io.StringIO(text))
            next(reader, None)
            result: set[str] = set()
            for row in reader:
                if len(row) < 2 or row[1].strip() == ".":
                    continue
                try:
                    if float(row[1]) > 20:
                        result.add(row[0].strip())
                except ValueError:
                    continue
            print(f"[v2] VIX mask: {len(result)} high-vol days (VIX>20)")
            return result
        except Exception as e:
            print(f"[v2] VIX fetch failed: {e}")
            return set()

    def _fetch_spy_mask(self) -> dict[str, str]:
        """Returns {date_str: 'bull'|'bear'} from SPY EMA200."""
        try:
            spy = _load_bars("SPY", self._data_client)
            if spy.empty:
                return {}
            ema200 = ta.ema(spy["close"], length=200)
            if ema200 is None:
                return {}
            mask: dict[str, str] = {}
            for date, close, ema in zip(spy.index, spy["close"], ema200):
                if pd.isna(ema):
                    continue
                d = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
                mask[d] = "bull" if close > ema else "bear"
            print(f"[v2] SPY regime mask: {sum(1 for v in mask.values() if v=='bull')} bull / "
                  f"{sum(1 for v in mask.values() if v=='bear')} bear days")
            return mask
        except Exception as e:
            print(f"[v2] SPY mask fetch failed: {e}")
            return {}

    def _prefetch_bars(self, symbols: list[str]):
        """Fetches all bar data to parquet cache before spawning workers."""
        print(f"[v2] Pre-fetching bars for {len(symbols)} symbols...")
        for i, sym in enumerate(symbols, 1):
            try:
                bars = _load_bars(sym, self._data_client)
                if bars.empty:
                    print(f"[v2] {sym}: no data")
                elif i % 10 == 0 or i == len(symbols):
                    print(f"[v2] Bar cache: {i}/{len(symbols)} symbols ready")
            except Exception as e:
                print(f"[v2] {sym} bar fetch error: {e}")

    def _upload_image(self, file_path: Path) -> str | None:
        """POSTs a PNG to 0x0.st and returns the public URL, or None on failure."""
        try:
            with open(file_path, "rb") as f:
                resp = requests.post("https://0x0.st", files={"file": f}, timeout=30)
            resp.raise_for_status()
            url = resp.text.strip()
            return url if url.startswith("http") else None
        except Exception as e:
            print(f"[v2] Image upload failed: {e}")
            return None

    def _slack_image(self, image_url: str, title: str):
        """Posts a Slack image block to #trading-decisions."""
        webhook = Config.SLACK_DECISIONS_WEBHOOK
        if not webhook:
            return
        payload = {
            "blocks": [
                {
                    "type": "image",
                    "image_url": image_url,
                    "alt_text": title,
                    "title": {"type": "plain_text", "text": title},
                }
            ]
        }
        try:
            requests.post(webhook, json=payload, timeout=10)
        except Exception as e:
            print(f"[v2] Slack image post failed: {e}")

    def _generate_chart(self, result: dict, equity_curve: list, charts_dir: Path) -> Path | None:
        """
        Generates a dark-theme equity curve + drawdown chart for a single validated strategy.
        Returns the saved file path, or None on failure.
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            eq = np.array(equity_curve, dtype=float)
            if len(eq) < 2 or eq[0] == 0:
                return None

            eq_norm = eq / eq[0] * 100.0

            fig, (ax_eq, ax_dd) = plt.subplots(
                2, 1, figsize=(12, 7),
                gridspec_kw={"height_ratios": [3, 1]},
                facecolor="#0d1117",
            )

            for ax in (ax_eq, ax_dd):
                ax.set_facecolor("#0d1117")
                ax.tick_params(colors="#8b949e")
                for spine in ax.spines.values():
                    spine.set_edgecolor("#30363d")

            x = range(len(eq_norm))
            ax_eq.plot(x, eq_norm, color="#00c851", linewidth=1.5, zorder=2)
            ax_eq.axhline(100, color="#8b949e", linewidth=0.5, linestyle="--", alpha=0.5)
            ax_eq.fill_between(
                x, 100, eq_norm,
                where=(eq_norm >= 100),
                color="#00c851", alpha=0.12, zorder=1,
            )
            ax_eq.set_ylabel("Equity (indexed to 100)", color="#8b949e", fontsize=9)

            running_max = np.maximum.accumulate(eq_norm)
            drawdown = np.where(running_max > 0, (eq_norm - running_max) / running_max * 100.0, 0.0)
            ax_dd.fill_between(x, drawdown, 0, color="#ff4444", alpha=0.5)
            ax_dd.set_ylabel("Drawdown %", color="#8b949e", fontsize=9)
            ax_dd.set_ylim(top=0)
            ax_dd.tick_params(colors="#8b949e")

            params_dict = json.loads(result["parameters"]) if isinstance(result["parameters"], str) else result["parameters"]
            params_short = ", ".join(f"{k}={v}" for k, v in list(params_dict.items())[:4])
            title = (
                f"{result['symbol']} — {result['strategy_type'].replace('_', ' ').title()}\n"
                f"Test Sharpe: {result.get('test_sharpe', 0):.2f}  |  "
                f"Win Rate: {result.get('win_rate', 0)*100:.0f}%  |  "
                f"{result.get('total_trades', 0)} trades  |  {params_short}"
            )
            ax_eq.set_title(title, color="#e6edf3", fontsize=10, pad=10)

            plt.tight_layout(pad=1.5)
            charts_dir.mkdir(parents=True, exist_ok=True)
            safe_sym  = result["symbol"].replace("/", "_")
            fname     = charts_dir / f"{safe_sym}_{result['strategy_type']}_equity.png"
            plt.savefig(fname, dpi=100, bbox_inches="tight", facecolor="#0d1117")
            plt.close(fig)
            return fname
        except Exception as e:
            print(f"[v2] Chart generation failed for {result.get('symbol')}: {e}")
            return None

    def _send_report(self, all_results: list[dict], symbols: list[str], elapsed_s: float):
        strategy_classes = load_all_strategies()
        combos_per_symbol = sum(len(sc().get_combos()) for sc in strategy_classes)
        total_combos = combos_per_symbol * len(symbols)
        validated    = len(all_results)
        h, rem = divmod(int(elapsed_s), 3600)
        m = rem // 60

        top5 = sorted(all_results, key=lambda r: r.get("test_sharpe", 0), reverse=True)[:5]

        lines = [
            ":microscope: *Discovery Engine Weekly Report*",
            f"Symbols tested: {len(symbols)} | Combos tested: {total_combos:,} | Validated: {validated}",
            f"Runtime: {h}h {m}m",
            "",
            "*Top 5 new findings:*",
        ]

        for i, r in enumerate(top5, 1):
            params_dict = json.loads(r["parameters"]) if isinstance(r["parameters"], str) else r["parameters"]
            params_str  = ", ".join(f"{k}={v}" for k, v in params_dict.items())
            deg         = r.get("degradation", 0) or 0
            deg_emoji   = ":white_check_mark:" if deg <= 0 else (":warning:" if deg < 0.3 else ":red_circle:")
            deg_sign    = "+" if deg >= 0 else ""
            bull_s  = f"{r['bull_sharpe']:.2f}"  if r.get("bull_sharpe")  is not None else "N/A"
            bear_s  = f"{r['bear_sharpe']:.2f}"  if r.get("bear_sharpe")  is not None else "N/A"
            lines.extend([
                f"{i}. *{r['symbol']}* — {r['strategy_type'].replace('_', ' ').title()}",
                f"   Test Sharpe: {r.get('test_sharpe', 0):.2f} | "
                f"Win Rate: {r.get('win_rate', 0)*100:.0f}% | {r.get('total_trades', 0)} trades",
                f"   Best regime: {r.get('best_regime', 'N/A')} | Bull: {bull_s} | Bear: {bear_s}",
                f"   Params: {params_str}",
                f"   Degradation: {deg_sign}{deg:.2f} {deg_emoji}",
                "",
            ])

        pending = len([r for r in all_results if r.get("status") == "pending_approval"])
        lines.append(f"*Strategies awaiting approval:* {pending}")
        lines.append("Review at: hybrid-trading-bot-production.up.railway.app")

        fi_brief = self._feature_importance_brief()
        if fi_brief:
            lines.append(f"\n*ML Feature Importance:* {fi_brief}")

        self._slack("\n".join(lines))

        # Generate and post equity curve charts for top 5 findings
        charts_dir = DATA_DIR / "charts"
        strategy_map = {sc().strategy_type: sc for sc in load_all_strategies()}
        for r in top5:
            try:
                bars = _load_bars_from_cache(r["symbol"], DATA_DIR)
                if bars.empty:
                    continue
                strategy_cls = strategy_map.get(r["strategy_type"])
                if strategy_cls is None:
                    continue
                strategy  = strategy_cls()
                params    = _params_from_json(r["parameters"], strategy.param_grid)
                ind_df    = strategy.compute_indicators(bars, params)
                sim       = _simulate_generic(ind_df, strategy, params)
                eq_curve  = sim["equity_curve"]
                chart_path = self._generate_chart(r, eq_curve, charts_dir)
                if chart_path is None:
                    continue
                url = self._upload_image(chart_path)
                if url:
                    sym_label = r["symbol"]
                    strat_label = r["strategy_type"].replace("_", " ").title()
                    self._slack_image(url, f"{sym_label} — {strat_label} equity curve")
                    print(f"[v2] Chart posted for {sym_label} {strat_label}: {url}")
                else:
                    print(f"[v2] Chart upload failed for {r['symbol']} — skipping image post")
            except Exception as e:
                print(f"[v2] Chart pipeline error for {r.get('symbol')}: {e}")

    def _feature_importance_brief(self) -> str:
        """
        Trains a RandomForestClassifier on closed signal_outcomes rows.
        Returns a human-readable importance line for the Slack brief,
        or "" if there is insufficient data or no DB connection.
        """
        if not self._db_url:
            return ""
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError:
            print("[v2] scikit-learn not installed — feature importance skipped")
            return ""
        try:
            engine = create_engine(self._db_url, pool_pre_ping=True)
            with engine.connect() as conn:
                df = pd.read_sql_query(
                    sql_text("""
                        SELECT rsi_at_entry,
                               macd_at_entry,
                               ema_short,
                               ema_long,
                               market_regime,
                               EXTRACT(HOUR FROM entry_time AT TIME ZONE 'America/New_York')::int
                                   AS hour_of_day,
                               EXTRACT(DOW  FROM entry_time AT TIME ZONE 'America/New_York')::int
                                   AS day_of_week,
                               pnl_pct
                        FROM signal_outcomes
                        WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                    """),
                    conn,
                )
            engine.dispose()

            if len(df) < 30:
                print(f"[v2] Feature importance: {len(df)} closed trades — need ≥30, skipping")
                return ""

            regime_map = {"bull": 1, "bear": -1, "neutral": 0}
            df["market_regime_enc"] = df["market_regime"].map(regime_map).fillna(0).astype(int)

            feature_cols   = ["rsi_at_entry", "macd_at_entry", "ema_short", "ema_long",
                               "market_regime_enc", "hour_of_day", "day_of_week"]
            feature_labels = ["RSI at entry", "MACD at entry", "EMA short", "EMA long",
                               "Market regime", "Hour of day", "Day of week"]

            X = df[feature_cols].apply(lambda col: col.fillna(col.median())).values
            y = (df["pnl_pct"] > 0).astype(int).values

            if y.sum() < 5 or (len(y) - y.sum()) < 5:
                return ""

            clf = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=1)
            clf.fit(X, y)

            sorted_idx = np.argsort(clf.feature_importances_)[::-1]
            top3 = [(feature_labels[i], clf.feature_importances_[i]) for i in sorted_idx[:3]]
            parts = [f"{name} ({imp * 100:.0f}%)" for name, imp in top3]
            brief = f"Key predictors ({len(df)} trades): {', '.join(parts)}"
            print(f"[v2] {brief}")
            return brief
        except Exception as e:
            print(f"[v2] Feature importance failed: {e}")
            return ""

    def run(self):
        strategy_classes    = load_all_strategies()
        combos_per_symbol   = sum(len(sc().get_combos()) for sc in strategy_classes)
        _sym_engine         = create_engine(self._db_url, pool_pre_ping=True) if self._db_url else None
        symbols             = get_discovery_candidates(_sym_engine, top_n=100) if _sym_engine else []

        print(
            f"[v2] Starting: {len(symbols)} symbols × {combos_per_symbol} combos/symbol "
            f"= {len(symbols) * combos_per_symbol:,} total backtests"
        )
        self._slack(
            f":mag: Discovery Engine v2 started — "
            f"{len(symbols)} symbols × {combos_per_symbol} combos = "
            f"{len(symbols) * combos_per_symbol:,} backtests. Runtime ~2h."
        )

        if self._db_url:
            _ensure_table(self._db_url)
            print("[v2] PostgreSQL table ready")
        else:
            print("[v2] No DATABASE_URL — results will not be persisted")

        spy_mask = self._fetch_spy_mask()
        vix_mask = self._fetch_vix_mask()
        self._prefetch_bars(symbols)

        symbol_args = [
            {
                "symbol":   sym,
                "db_url":   self._db_url,
                "data_dir": str(DATA_DIR),
                "spy_mask": spy_mask,
                "vix_mask": vix_mask,
            }
            for sym in symbols
        ]

        n_workers    = min(4, multiprocessing.cpu_count())
        all_results: list[dict] = []
        symbols_done = 0
        start_time   = time.time()
        last_progress_slack = start_time

        print(f"[v2] Spawning {n_workers} workers via multiprocessing.Pool")

        with multiprocessing.Pool(processes=n_workers) as pool:
            for results in pool.imap_unordered(_run_symbol, symbol_args):
                symbols_done += 1

                for r in results:
                    if Config.DISCOVERY_DEBATE_ENABLED and r.get("status") == "pending_approval":
                        if not _debate_strategy_sync(r):
                            r["status"] = "rejected_by_debate"
                    _upsert_result(self._db_url, r)
                    if r.get("status") != "rejected_by_debate":
                        all_results.append(r)

                elapsed_min = (time.time() - start_time) / 60
                print(
                    f"[v2] {symbols_done}/{len(symbols)} symbols done | "
                    f"{len(all_results)} validated | {elapsed_min:.0f}m elapsed"
                )

                if time.time() - last_progress_slack >= 3600:
                    self._slack(
                        f":mag: Discovery running — {symbols_done}/{len(symbols)} symbols | "
                        f"{len(all_results)} validated | {elapsed_min:.0f}m elapsed"
                    )
                    last_progress_slack = time.time()

        elapsed_total = time.time() - start_time
        print(
            f"\n[v2] Complete — {len(all_results)} strategies validated across "
            f"{symbols_done} symbols in {elapsed_total/60:.1f}m"
        )
        self._send_report(all_results, symbols, elapsed_total)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for Windows executables
    DiscoveryEngineV2().run()
