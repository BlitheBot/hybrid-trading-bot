import csv
import io
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import pytz
import requests as _requests
import streamlit as st
from sqlalchemy import create_engine, text

# Remove conflicting environment tokens before importing Alpaca
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient

from config import Config

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Hybrid Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS theme — dark trading terminal ─────────────────────────────────────────

st.markdown("""
<style>
/* ── Base ──────────────────────────────────────── */
.stApp { background-color: #0d1117; color: #e6edf3; }
.main .block-container {
    background-color: #0d1117;
    padding-top: 1.25rem;
    max-width: 100%;
}

/* ── Sidebar ────────────────────────────────────── */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] > div:first-child {
    background-color: #0d1117 !important;
    border-right: 1px solid #30363d;
}

/* ── Headers / text ─────────────────────────────── */
h1, h2, h3, h4 { color: #e6edf3 !important; font-weight: 700 !important; }
p, li, label, .stMarkdown p { color: #c9d1d9; }
.stCaption { color: #484f58 !important; font-size: 11px !important; }

/* ── Metric cards (custom HTML) ─────────────────── */
.metric-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 14px 16px 16px 16px;
    margin-bottom: 8px;
    min-height: 78px;
}
.card-label {
    font-size: 10px;
    font-weight: 700;
    color: #484f58;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    margin-bottom: 8px;
}
.card-value {
    font-size: 20px;
    font-weight: 700;
    color: #e6edf3;
    font-variant-numeric: tabular-nums;
    line-height: 1.25;
    letter-spacing: -0.01em;
}
.card-value.positive { color: #00c851; }
.card-value.negative { color: #ff4444; }
.card-value.neutral  { color: #7d8590; }

/* ── Status dots ────────────────────────────────── */
.status-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
    font-size: 13px;
    color: #c9d1d9;
    line-height: 1.5;
}
.status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
}
.dot-green  { background: #00c851; box-shadow: 0 0 5px #00c85155; }
.dot-red    { background: #ff4444; }
.dot-yellow { background: #f0a500; }
.status-detail { color: #484f58; font-size: 11px; }

/* ── Sidebar section labels ─────────────────────── */
.sidebar-section {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: #484f58;
    margin: 18px 0 8px 0;
    padding-top: 14px;
    border-top: 1px solid #21262d;
}

/* ── FRED macro rows ─────────────────────────────── */
.macro-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 5px 0;
    border-bottom: 1px solid #21262d44;
}
.macro-label { color: #7d8590; font-size: 12px; }
.macro-value {
    font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
    font-size: 13px;
    font-weight: 600;
}
.macro-ok      { color: #00c851; }
.macro-warn    { color: #f0a500; }
.macro-danger  { color: #ff4444; }
.macro-neutral { color: #c9d1d9; }

/* ── Price ticker rows ──────────────────────────── */
.price-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 5px 0;
    border-bottom: 1px solid #21262d;
}
.price-sym { color: #7d8590; font-size: 12px; font-weight: 600; }
.price-val {
    font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
    font-size: 13px;
    color: #e6edf3;
    font-weight: 500;
}

/* ── Tabs ───────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {
    background: transparent;
    border-bottom: 1px solid #30363d;
    gap: 0;
}
.stTabs [data-baseweb="tab"] {
    background: transparent;
    color: #7d8590;
    font-size: 13px;
    font-weight: 500;
    padding: 8px 16px;
    border-bottom: 2px solid transparent;
}
.stTabs [data-baseweb="tab"]:hover { color: #c9d1d9; }
.stTabs [aria-selected="true"] {
    color: #00c851 !important;
    border-bottom: 2px solid #00c851 !important;
    background: transparent !important;
}
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.25rem; }

/* ── Buttons ────────────────────────────────────── */
.stButton > button, .stDownloadButton > button {
    background: #21262d;
    border: 1px solid #30363d;
    color: #c9d1d9;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    transition: border-color 0.15s, color 0.15s;
}
.stButton > button:hover, .stDownloadButton > button:hover {
    background: #161b22;
    border-color: #00c851;
    color: #00c851;
}

/* ── DataFrames ─────────────────────────────────── */
.stDataFrame { border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
.ag-root-wrapper { background: #161b22 !important; border: none !important; }
.ag-header { background: #161b22 !important; border-bottom: 1px solid #30363d !important; }
.ag-header-cell-label {
    color: #7d8590 !important;
    font-size: 11px !important;
    font-weight: 700 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.05em !important;
}
.ag-row { background: #0d1117 !important; color: #c9d1d9 !important; border-color: #21262d !important; }
.ag-row-even { background: #161b22 !important; }
.ag-row:hover { background: #1c2128 !important; }
.ag-cell { border: none !important; }

/* ── Dividers ───────────────────────────────────── */
hr { border-color: #30363d !important; opacity: 1 !important; }

/* ── Selectbox / multiselect / date ─────────────── */
.stMultiSelect [data-baseweb="select"] > div,
.stDateInput > div > div,
.stSelectbox > div > div {
    background-color: #161b22 !important;
    border-color: #30363d !important;
    color: #e6edf3 !important;
}

/* ── Toggle ─────────────────────────────────────── */
.stCheckbox label, [data-testid="stCheckbox"] label { color: #c9d1d9 !important; }

/* ── Alerts ─────────────────────────────────────── */
[data-testid="stAlert"] { background: #161b22 !important; border-color: #30363d !important; }

/* ── Scrollbar ──────────────────────────────────── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #0d1117; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }

/* ── Mobile — danger (primary) button ───────────── */
[data-testid="baseButton-primary"] {
    background: #3a1a1a !important;
    border-color: #ff4444 !important;
    color: #ff4444 !important;
    font-weight: 700 !important;
    font-size: 15px !important;
}
[data-testid="baseButton-primary"]:hover {
    background: #ff444422 !important;
    border-color: #ff6666 !important;
    color: #ff6666 !important;
}
</style>
""", unsafe_allow_html=True)

# ── Pure helpers ───────────────────────────────────────────────────────────────

def _metric_card(label: str, value: str, value_class: str = "") -> str:
    return (
        f'<div class="metric-card">'
        f'<div class="card-label">{label}</div>'
        f'<div class="card-value {value_class}">{value}</div>'
        f'</div>'
    )

def _status_row(label: str, ok: bool | None, detail: str = "") -> str:
    dot = "dot-green" if ok else ("dot-yellow" if ok is None else "dot-red")
    detail_html = f'<span class="status-detail">{detail}</span>' if detail else ""
    return (
        f'<div class="status-row">'
        f'<div class="status-dot {dot}"></div>'
        f'<span>{label}</span>{detail_html}'
        f'</div>'
    )

def _minutes_ago(iso_str) -> int | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.utc)
        return max(0, int((datetime.now(pytz.utc) - dt).total_seconds() / 60))
    except Exception:
        return None

# ── Alpaca clients ─────────────────────────────────────────────────────────────

@st.cache_resource
def _get_trading_client():
    if not Config.ALPACA_API_KEY or not Config.ALPACA_SECRET_KEY:
        return None
    base_url = (
        "https://paper-api.alpaca.markets"
        if Config.PAPER_TRADING
        else "https://api.alpaca.markets"
    )
    return TradingClient(
        api_key=Config.ALPACA_API_KEY,
        secret_key=Config.ALPACA_SECRET_KEY,
        paper=Config.PAPER_TRADING,
        url_override=base_url,
    )


@st.cache_resource
def _get_stock_data_client():
    if not Config.ALPACA_API_KEY or not Config.ALPACA_SECRET_KEY:
        return None
    return StockHistoricalDataClient(
        api_key=Config.ALPACA_API_KEY,
        secret_key=Config.ALPACA_SECRET_KEY,
    )


@st.cache_resource
def _get_crypto_data_client():
    if not Config.ALPACA_API_KEY or not Config.ALPACA_SECRET_KEY:
        return None
    return CryptoHistoricalDataClient(
        api_key=Config.ALPACA_API_KEY,
        secret_key=Config.ALPACA_SECRET_KEY,
    )


trading_client     = _get_trading_client()
stock_data_client  = _get_stock_data_client()
crypto_data_client = _get_crypto_data_client()

if not trading_client:
    st.error("🔑 ALPACA_API_KEY / ALPACA_SECRET_KEY missing from environment.")
    st.stop()

# ── DB engine ──────────────────────────────────────────────────────────────────

@st.cache_resource
def _get_engine():
    url = Config.DATABASE_URL
    if not url:
        return None
    try:
        return create_engine(url)
    except Exception as e:
        st.warning(f"[DB] Engine creation failed: {e}")
        return None


def _db_available() -> bool:
    return Config.DATABASE_URL is not None

# ── Styling helpers ────────────────────────────────────────────────────────────

def _pnl_color(val):
    try:
        v = float(val)
        if v > 0:
            return "color: #00c851"
        if v < 0:
            return "color: #ff4444"
    except (TypeError, ValueError):
        pass
    return ""


def _status_color(val):
    return "color: #00c851" if val == "validated" else "color: #ff4444"

# ── Data fetchers — PRESERVED EXACTLY ──────────────────────────────────────────

@st.cache_data(ttl=60)
def fetch_account():
    try:
        return trading_client.get_account()
    except Exception as e:
        st.error(f"Account fetch failed: {e}")
        return None


@st.cache_data(ttl=60)
def fetch_positions():
    try:
        return trading_client.get_all_positions() or []
    except Exception:
        return []


@st.cache_data(ttl=60)
def fetch_signal_outcomes(start_dt: datetime, end_dt: datetime, symbols: tuple):
    engine = _get_engine()
    if engine is None:
        return None
    try:
        query_str = """
            SELECT id, symbol, signal_type,
                   entry_time AT TIME ZONE 'America/New_York' AS entry_time,
                   exit_time  AT TIME ZONE 'America/New_York' AS exit_time,
                   entry_price, exit_price, pnl_pct, hold_bars,
                   ema_short, ema_long, rsi_at_entry, macd_at_entry,
                   market_regime, exit_reason
            FROM signal_outcomes
            WHERE entry_time >= :start_dt AND entry_time <= :end_dt
        """
        params: dict = {"start_dt": start_dt, "end_dt": end_dt}
        if symbols:
            placeholders = ", ".join(f":sym_{i}" for i in range(len(symbols)))
            query_str += f" AND symbol IN ({placeholders})"
            for i, sym in enumerate(symbols):
                params[f"sym_{i}"] = sym
        query_str += " ORDER BY entry_time DESC"
        with engine.connect() as conn:
            return pd.read_sql(text(query_str), conn, params=params)
    except Exception as e:
        st.error(f"Trade log query failed: {e}")
        return None


@st.cache_data(ttl=300)
def fetch_strategy_results(validated_only: bool):
    """
    Queries discovery_results (v2 engine) with fallback to legacy strategy_results.
    validated_only filters to status IN ('approved', 'pending_approval') for v2
    or status = 'validated' for the legacy table.
    """
    engine = _get_engine()
    if engine is None:
        return None
    try:
        # Try v2 table first
        where = "WHERE status IN ('approved', 'pending_approval')" if validated_only else ""
        query_str = f"""
            SELECT id, symbol, strategy_type,
                   parameters::text                  AS parameters,
                   ROUND(train_sharpe::numeric, 3)   AS train_sharpe,
                   ROUND(test_sharpe::numeric,  3)   AS test_sharpe,
                   ROUND(degradation::numeric,  3)   AS degradation,
                   ROUND(p_value::numeric,      4)   AS p_value,
                   total_trades, win_rate,
                   best_regime, status,
                   discovered_at::date               AS date
            FROM discovery_results
            {where}
            ORDER BY test_sharpe DESC NULLS LAST
        """
        with engine.connect() as conn:
            return pd.read_sql(text(query_str), conn)
    except Exception:
        # Fallback to v1 strategy_results if discovery_results doesn't exist yet
        try:
            where_v1 = "WHERE status = 'validated'" if validated_only else ""
            query_v1 = f"""
                SELECT id, symbol,
                       'ema_trend'                       AS strategy_type,
                       NULL::text                        AS parameters,
                       ROUND(train_sharpe::numeric, 3)   AS train_sharpe,
                       ROUND(test_sharpe::numeric,  3)   AS test_sharpe,
                       ROUND(degradation::numeric,  3)   AS degradation,
                       ROUND(p_value::numeric,      4)   AS p_value,
                       total_test_trades                 AS total_trades,
                       NULL::float                       AS win_rate,
                       NULL::text                        AS best_regime,
                       status,
                       discovered_at::date               AS date
                FROM strategy_results
                {where_v1}
                ORDER BY test_sharpe DESC NULLS LAST
            """
            with engine.connect() as conn:
                return pd.read_sql(text(query_v1), conn)
        except Exception as e:
            st.error(f"Discovery query failed: {e}")
            return None


def _approve_strategy(row_id: int) -> bool:
    """UPDATE discovery_results SET status='approved' WHERE id=row_id. Not cached."""
    engine = _get_engine()
    if engine is None:
        return False
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE discovery_results SET status='approved' WHERE id=:id"),
                {"id": row_id},
            )
        return True
    except Exception as e:
        st.error(f"Approval failed: {e}")
        return False


@st.cache_data(ttl=120)
def fetch_decay_status() -> "pd.DataFrame | None":
    """All rows from strategy_decay_status, ranked by severity."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            df = pd.read_sql(text("""
                SELECT strategy_name, symbol, decay_ratio, status, position_multiplier,
                       consecutive_signals_below, re_validation_requested, disabled, last_checked
                FROM strategy_decay_status
            """), conn)
        if df.empty:
            return df
        severity = {"CRITICAL": 0, "DECAYING": 1, "DEGRADED": 2, "HEALTHY": 3}
        df["_sev"] = df["status"].map(lambda s: severity.get(s, 9))
        df = df.sort_values(["_sev", "decay_ratio"], na_position="last").drop(columns=["_sev"])
        return df
    except Exception as e:
        st.error(f"Decay status query failed: {e}")
        return None


def _decay_action(strategy_name: str, symbol: str, action: str) -> bool:
    """Manual override: 're-validate', 'disable', or 'reset'. Not cached."""
    engine = _get_engine()
    if engine is None:
        return False
    try:
        with engine.begin() as conn:
            if action == "disable":
                conn.execute(text("""
                    UPDATE strategy_decay_status
                    SET disabled=TRUE, status='CRITICAL', position_multiplier=0.0, last_checked=NOW()
                    WHERE strategy_name=:s AND symbol=:sym
                """), {"s": strategy_name, "sym": symbol})
            elif action == "reset":
                conn.execute(text("""
                    UPDATE strategy_decay_status
                    SET disabled=FALSE, status='HEALTHY', position_multiplier=1.0,
                        re_validation_requested=FALSE, consecutive_signals_below=0, last_checked=NOW()
                    WHERE strategy_name=:s AND symbol=:sym
                """), {"s": strategy_name, "sym": symbol})
            elif action == "re-validate":
                conn.execute(text("""
                    INSERT INTO revalidation_queue (strategy_name, symbol, reason, status)
                    VALUES (:s, :sym, 'manual', 'pending')
                """), {"s": strategy_name, "sym": symbol})
                conn.execute(text("""
                    UPDATE strategy_decay_status SET re_validation_requested=TRUE
                    WHERE strategy_name=:s AND symbol=:sym
                """), {"s": strategy_name, "sym": symbol})
        return True
    except Exception as e:
        st.error(f"Decay action '{action}' failed: {e}")
        return False


@st.cache_data(ttl=300)
def fetch_golive_readiness() -> dict:
    """Computes all 5 go-live criteria from signal_outcomes and discovery tables."""
    engine = _get_engine()
    result: dict = {
        "monthly_positive": None,
        "months_total": None,
        "win_rate": None,
        "total_trades": 0,
        "max_drawdown_pct": None,
        "validated_strategies": None,
    }
    if engine is None:
        return result
    try:
        with engine.connect() as conn:
            # Win rate
            row = conn.execute(text("""
                SELECT COUNT(*) AS total,
                       ROUND(100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::numeric
                           / NULLIF(COUNT(*), 0), 1) AS win_rate_pct
                FROM signal_outcomes
                WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
            """)).mappings().fetchone()
            if row and row["total"]:
                result["total_trades"] = int(row["total"])
                result["win_rate"] = float(row["win_rate_pct"]) / 100.0 if row["win_rate_pct"] else 0.0

            # Monthly P&L — last 4 calendar months, count positive months
            monthly_rows = conn.execute(text("""
                SELECT DATE_TRUNC('month', exit_time AT TIME ZONE 'America/New_York') AS month,
                       SUM(pnl_pct) AS monthly_pnl
                FROM signal_outcomes
                WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                  AND exit_time >= NOW() - INTERVAL '4 months'
                GROUP BY 1
                ORDER BY 1 DESC
                LIMIT 4
            """)).mappings().fetchall()
            if monthly_rows:
                result["months_total"] = len(monthly_rows)
                result["monthly_positive"] = sum(
                    1 for r in monthly_rows if float(r["monthly_pnl"]) > 0
                )

            # Max drawdown from running cumulative P&L curve
            trade_rows = conn.execute(text("""
                SELECT pnl_pct FROM signal_outcomes
                WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
                ORDER BY exit_time ASC
            """)).mappings().fetchall()
            if trade_rows:
                peak = 0.0
                cum = 0.0
                max_dd = 0.0
                for r in trade_rows:
                    cum += float(r["pnl_pct"])
                    if cum > peak:
                        peak = cum
                    dd = peak - cum
                    if dd > max_dd:
                        max_dd = dd
                result["max_drawdown_pct"] = max_dd

            # Validated strategies count — v2 first, then v1 fallback
            try:
                r2 = conn.execute(text("""
                    SELECT COUNT(*) AS cnt FROM discovery_results
                    WHERE status IN ('approved', 'pending_approval')
                """)).mappings().fetchone()
                result["validated_strategies"] = int(r2["cnt"]) if r2 else 0
            except Exception:
                try:
                    r1 = conn.execute(text("""
                        SELECT COUNT(*) AS cnt FROM strategy_results
                        WHERE status = 'validated'
                    """)).mappings().fetchone()
                    result["validated_strategies"] = int(r1["cnt"]) if r1 else 0
                except Exception:
                    result["validated_strategies"] = 0
    except Exception:
        pass
    return result


@st.cache_data(ttl=300)
def fetch_strategy_ev():
    """
    Returns EV = (win_rate × avg_win_pct) − (loss_rate × avg_loss_pct)
    per signal_type for strategies with ≥ 20 closed trades.
    """
    engine = _get_engine()
    if engine is None:
        return None
    try:
        query_str = """
            SELECT signal_type,
                   COUNT(*) AS trades,
                   ROUND(
                       100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::numeric / COUNT(*),
                       1
                   ) AS win_rate_pct,
                   ROUND(AVG(CASE WHEN pnl_pct > 0  THEN pnl_pct      ELSE NULL END)::numeric, 2)
                       AS avg_win_pct,
                   ROUND(AVG(CASE WHEN pnl_pct <= 0 THEN ABS(pnl_pct) ELSE NULL END)::numeric, 2)
                       AS avg_loss_pct
            FROM signal_outcomes
            WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
            GROUP BY signal_type
            HAVING COUNT(*) >= 20
            ORDER BY signal_type
        """
        with engine.connect() as conn:
            df = pd.read_sql(text(query_str), conn)
        if df.empty:
            return df
        wr = df["win_rate_pct"] / 100.0
        lr = 1.0 - wr
        df["ev_pct"] = ((wr * df["avg_win_pct"]) - (lr * df["avg_loss_pct"])).round(3)
        return df
    except Exception as e:
        st.error(f"EV query failed: {e}")
        return None


@st.cache_data(ttl=60)
def fetch_daily_pnl():
    engine = _get_engine()
    if engine is None:
        return None
    try:
        query_str = """
            SELECT DATE(exit_time AT TIME ZONE 'America/New_York') AS trade_date,
                   COUNT(*)        AS trades,
                   SUM(pnl_pct)    AS daily_pnl_sum
            FROM signal_outcomes
            WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """
        with engine.connect() as conn:
            return pd.read_sql(text(query_str), conn)
    except Exception as e:
        st.error(f"Daily P&L query failed: {e}")
        return None


@st.cache_data(ttl=60)
def fetch_win_rate():
    engine = _get_engine()
    if engine is None:
        return None
    try:
        query_str = """
            SELECT symbol,
                   COUNT(*)  AS total_trades,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(
                       100.0 * SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*),
                       1
                   ) AS win_rate_pct,
                   ROUND(AVG(pnl_pct)::numeric, 2) AS avg_pnl_pct
            FROM signal_outcomes
            WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
            GROUP BY symbol
            ORDER BY total_trades DESC
        """
        with engine.connect() as conn:
            return pd.read_sql(text(query_str), conn)
    except Exception as e:
        st.error(f"Win rate query failed: {e}")
        return None

@st.cache_data(ttl=300)
def fetch_performance_by_dimension(dimension: str) -> pd.DataFrame | None:
    """
    Per-dimension win rate, avg win/loss, and EV from closed signal_outcomes.
    dimension: 'signal_type' | 'market_regime' | 'exit_reason' | 'day_of_week' | 'hour_of_day'
    Returns columns: dim_value, trade_count, win_rate, avg_win_pct, avg_loss_pct, ev
    """
    _DIM_SQL = {
        "signal_type":   "signal_type",
        "market_regime": "market_regime",
        "exit_reason":   "exit_reason",
        "day_of_week":   "EXTRACT(DOW  FROM entry_time AT TIME ZONE 'America/New_York')::int",
        "hour_of_day":   "EXTRACT(HOUR FROM entry_time AT TIME ZONE 'America/New_York')::int",
    }
    dim_expr = _DIM_SQL.get(dimension)
    if not dim_expr:
        return None
    engine = _get_engine()
    if engine is None:
        return None
    try:
        query_str = f"""
            SELECT {dim_expr} AS dim_value,
                   COUNT(*) AS trade_count,
                   ROUND(100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::numeric
                       / NULLIF(COUNT(*), 0), 1) AS win_rate,
                   ROUND(AVG(CASE WHEN pnl_pct > 0  THEN pnl_pct      ELSE NULL END)::numeric, 2)
                       AS avg_win_pct,
                   ROUND(AVG(CASE WHEN pnl_pct <= 0 THEN ABS(pnl_pct) ELSE NULL END)::numeric, 2)
                       AS avg_loss_pct
            FROM signal_outcomes
            WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
            GROUP BY 1
            HAVING COUNT(*) >= 3
            ORDER BY 1
        """
        with engine.connect() as conn:
            df = pd.read_sql(text(query_str), conn)
        if df.empty:
            return df
        wr = df["win_rate"] / 100.0
        lr = 1.0 - wr
        df["ev"] = ((wr * df["avg_win_pct"]) - (lr * df["avg_loss_pct"])).round(3)
        return df
    except Exception as e:
        st.error(f"Performance dimension query failed: {e}")
        return None


@st.cache_data(ttl=300)
def fetch_win_rate_by_regime() -> pd.DataFrame | None:
    """Win rate pivoted by signal_type (rows) × market_regime (columns) for grouped bar chart."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        query_str = """
            SELECT signal_type, market_regime,
                   COUNT(*) AS trade_count,
                   ROUND(100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::numeric
                       / NULLIF(COUNT(*), 0), 1) AS win_rate
            FROM signal_outcomes
            WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
              AND market_regime IS NOT NULL
            GROUP BY signal_type, market_regime
            HAVING COUNT(*) >= 3
        """
        with engine.connect() as conn:
            df = pd.read_sql(text(query_str), conn)
        if df.empty:
            return df
        return df.pivot_table(
            index="signal_type", columns="market_regime",
            values="win_rate", fill_value=None,
        )
    except Exception as e:
        st.error(f"Regime win rate query failed: {e}")
        return None


@st.cache_data(ttl=300)
def fetch_best_time_windows(min_trades: int = 5, top_n: int = 5) -> pd.DataFrame | None:
    """Top day×hour windows ranked by EV — tells you when each dollar of risk pays the most."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        query_str = """
            SELECT EXTRACT(DOW  FROM entry_time AT TIME ZONE 'America/New_York')::int AS dow,
                   EXTRACT(HOUR FROM entry_time AT TIME ZONE 'America/New_York')::int AS hour,
                   COUNT(*) AS trade_count,
                   ROUND(100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::numeric / COUNT(*), 1)
                       AS win_rate,
                   ROUND(AVG(CASE WHEN pnl_pct > 0  THEN pnl_pct      ELSE NULL END)::numeric, 2)
                       AS avg_win_pct,
                   ROUND(AVG(CASE WHEN pnl_pct <= 0 THEN ABS(pnl_pct) ELSE NULL END)::numeric, 2)
                       AS avg_loss_pct
            FROM signal_outcomes
            WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
              AND EXTRACT(DOW  FROM entry_time AT TIME ZONE 'America/New_York') BETWEEN 1 AND 5
              AND EXTRACT(HOUR FROM entry_time AT TIME ZONE 'America/New_York') BETWEEN 9 AND 16
            GROUP BY 1, 2
            HAVING COUNT(*) >= :min_trades
        """
        with engine.connect() as conn:
            df = pd.read_sql(text(query_str), conn, params={"min_trades": min_trades})
        if df.empty:
            return df
        wr = df["win_rate"] / 100.0
        lr = 1.0 - wr
        df["ev"] = ((wr * df["avg_win_pct"]) - (lr * df["avg_loss_pct"])).round(3)
        _DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri"}
        df["window"] = df.apply(
            lambda r: f"{_DAY_NAMES.get(int(r['dow']), '?')} {int(r['hour']):02d}:00 EST",
            axis=1,
        )
        top = df.nlargest(top_n, "ev")[
            ["window", "trade_count", "win_rate", "avg_win_pct", "avg_loss_pct", "ev"]
        ]
        return top.reset_index(drop=True)
    except Exception as e:
        st.error(f"Best time windows query failed: {e}")
        return None


@st.cache_data(ttl=60)
def fetch_recent_signals(n: int = 20) -> "pd.DataFrame | None":
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            return pd.read_sql(
                text("""
                    SELECT symbol, signal_type,
                           entry_time AT TIME ZONE 'America/New_York' AS entry_time,
                           entry_price, market_regime, exit_reason, pnl_pct
                    FROM signal_outcomes
                    ORDER BY entry_time DESC
                    LIMIT :n
                """),
                conn,
                params={"n": n},
            )
    except Exception as e:
        st.error(f"Recent signals query failed: {e}")
        return None


@st.cache_data(ttl=300)
def _fetch_bars_60d(symbol: str) -> "pd.DataFrame | None":
    if not stock_data_client:
        return None
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end   = datetime.now(pytz.utc) - timedelta(minutes=20)
    start = end - timedelta(days=90)
    try:
        req  = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                                start=start, end=end, feed="iex")
        bars = stock_data_client.get_stock_bars(req)
        if not hasattr(bars, "df") or bars.df is None or bars.df.empty:
            return None
        df = bars.df.copy()
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index(level=0, drop=True)
        df.columns = [c.lower() for c in df.columns]
        return df.tail(60)
    except Exception:
        return None


@st.cache_data(ttl=300)
def fetch_live_signal_state() -> dict:
    try:
        from strategies.kalman_signal import KalmanTrendSignal
        from strategies.hurst_signal import HurstSignal
        from strategies.vwap_signal import AnchoredVWAPSignal
        _have_mods = True
    except ImportError:
        _have_mods = False

    result: dict = {}
    for sym in Config.SWING_SYMBOLS:
        bars  = _fetch_bars_60d(sym)
        entry: dict = {}
        if bars is None or bars.empty or not _have_mods:
            result[sym] = entry
            continue
        try:
            k = KalmanTrendSignal().compute(bars["close"])
            entry["kalman_slope"]       = round(float(k["slope"].iloc[-1]), 4)
            entry["kalman_noise_ratio"] = round(float(k["noise_ratio"].iloc[-1]), 4)
        except Exception:
            entry["kalman_slope"] = entry["kalman_noise_ratio"] = None
        try:
            h = HurstSignal().compute(bars["close"])
            entry["hurst_h"]      = round(float(h["hurst"].iloc[-1]), 3)
            entry["hurst_regime"] = str(h["regime"].iloc[-1])
        except Exception:
            entry["hurst_h"] = entry["hurst_regime"] = None
        try:
            bars.sort_index(inplace=True)
            v = AnchoredVWAPSignal().compute(bars)
            entry["vwap_dist_pct"] = round(float(v["distance_pct"].iloc[-1]), 3)
        except Exception:
            entry["vwap_dist_pct"] = None
        result[sym] = entry
    return result


@st.cache_data(ttl=120)
def fetch_gate_chain_health() -> "pd.DataFrame | None":
    engine = _get_engine()
    live   = fetch_live_signal_state()
    rows: list[dict] = []
    for sym in Config.SWING_SYMBOLS:
        row: dict = {"Symbol": sym}

        cb_active, cb_since = False, ""
        if engine:
            try:
                with engine.connect() as conn:
                    cb = conn.execute(text("""
                        SELECT reason, tripped_at
                        FROM strategy_circuit_breakers
                        WHERE strategy_name ILIKE :pat
                        ORDER BY tripped_at DESC LIMIT 1
                    """), {"pat": f"%{sym}%"}).mappings().fetchone()
                if cb:
                    cb_active = True
                    cb_since  = str(cb["tripped_at"])[:16]
            except Exception:
                pass

        row["CB"]       = "ACTIVE" if cb_active else "CLEAR"
        row["CB Since"] = cb_since

        s = live.get(sym, {})
        row["Hurst Regime"] = s.get("hurst_regime", "—")
        row["Hurst H"]      = s.get("hurst_h")
        row["Kalman Noise"] = s.get("kalman_noise_ratio")
        row["VWAP Dist%"]   = s.get("vwap_dist_pct")
        row["Half-Kelly f"] = None
        row["Trades 7d"]    = 0
        row["Win Rate 7d"]  = "—"

        if engine:
            try:
                with engine.connect() as conn:
                    kr = conn.execute(text("""
                        SELECT COUNT(*) AS n,
                               COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)::float
                                   / NULLIF(COUNT(*), 0) AS wr,
                               AVG(CASE WHEN pnl_pct > 0  THEN pnl_pct      END) AS avg_win,
                               AVG(CASE WHEN pnl_pct <= 0 THEN ABS(pnl_pct) END) AS avg_loss
                        FROM signal_outcomes
                        WHERE symbol = :sym
                          AND exit_time IS NOT NULL
                          AND pnl_pct IS NOT NULL
                    """), {"sym": sym}).mappings().fetchone()
                if (kr and kr["n"] and int(kr["n"]) >= 10
                        and kr["avg_win"] and kr["avg_loss"]):
                    wr = float(kr["wr"])
                    b  = float(kr["avg_win"]) / float(kr["avg_loss"])
                    row["Half-Kelly f"] = round(max(0.0, wr - (1.0 - wr) / b) / 2, 4)
            except Exception:
                pass
            try:
                with engine.connect() as conn:
                    r7 = conn.execute(text("""
                        SELECT COUNT(*) AS n,
                               ROUND(100.0 * COUNT(CASE WHEN pnl_pct > 0 THEN 1 END)
                                   / NULLIF(COUNT(*), 0), 1) AS wr
                        FROM signal_outcomes
                        WHERE symbol = :sym
                          AND exit_time IS NOT NULL
                          AND pnl_pct IS NOT NULL
                          AND exit_time >= NOW() - INTERVAL '7 days'
                    """), {"sym": sym}).mappings().fetchone()
                row["Trades 7d"]   = int(r7["n"]) if r7 and r7["n"] else 0
                row["Win Rate 7d"] = (
                    f"{float(r7['wr']):.1f}%" if r7 and r7["wr"] else "—"
                )
            except Exception:
                pass

        rows.append(row)
    return pd.DataFrame(rows) if rows else None


@st.cache_data(ttl=300)
def fetch_swing_correlation() -> "pd.DataFrame | None":
    closes: dict = {}
    for sym in Config.SWING_SYMBOLS:
        bars = _fetch_bars_60d(sym)
        if bars is not None and not bars.empty and "close" in bars.columns:
            closes[sym] = bars["close"].reset_index(drop=True)
    if len(closes) < 2:
        return None
    return pd.DataFrame(closes).corr(method="pearson")


# ── Sidebar data fetchers ──────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def _bot_health():
    try:
        r = _requests.get("http://localhost:8502/health", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


@st.cache_data(ttl=30)
def _fetch_prices() -> dict:
    prices: dict = {}

    # Stock prices — bulk request for all SWING_SYMBOLS
    if stock_data_client:
        try:
            resp = stock_data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=list(Config.SWING_SYMBOLS))
            )
            for sym in Config.SWING_SYMBOLS:
                if sym in resp:
                    prices[sym] = float(resp[sym].price)
        except Exception:
            pass

    # Crypto prices — per-symbol fallback to handle SDK version differences
    if crypto_data_client:
        for sym in ("BTC/USD", "ETH/USD"):
            try:
                from alpaca.data.requests import CryptoLatestTradeRequest
                resp = crypto_data_client.get_crypto_latest_trade(
                    CryptoLatestTradeRequest(symbol_or_symbols=sym)
                )
                if sym in resp:
                    prices[sym] = float(resp[sym].price)
            except Exception:
                pass

    return prices


@st.cache_data(ttl=3600)
def _fetch_fred_sidebar() -> dict:
    """Fetch VIX, Fed Funds Rate, and 10Y Treasury from FRED public CSV endpoints."""
    series = {
        "vix":        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
        "fed_funds":  "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS",
        "treasury":   "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
    }
    headers = {"User-Agent": "HybridTradingBot/1.0 contact@hybridtradingbot.com"}
    result: dict = {}
    for key, url in series.items():
        try:
            r = _requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                reader = csv.reader(io.StringIO(r.text))
                next(reader, None)   # skip DATE,VALUE header
                vals = [
                    float(row[1].strip())
                    for row in reader
                    if len(row) >= 2 and row[1].strip() not in (".", "")
                ]
                result[key] = vals[-1] if vals else None
            else:
                result[key] = None
        except Exception:
            result[key] = None
    return result

# ── Mobile view helpers ────────────────────────────────────────────────────────

def _close_all_positions_action():
    """Cancel all open orders then submit market close for every open position."""
    try:
        trading_client.cancel_orders()
    except Exception as _coe:
        st.error(f"Cancel orders failed: {_coe}")

    try:
        _open = trading_client.get_all_positions()
    except Exception as _pe:
        st.error(f"Could not fetch positions: {_pe}")
        return

    if not _open:
        st.info("No open positions to close.")
        return

    _errors = []
    _closed = 0
    for _pos in _open:
        try:
            trading_client.close_position(_pos.symbol)
            _closed += 1
        except Exception as _ce:
            _errors.append(f"{_pos.symbol}: {_ce}")

    if _errors:
        st.error("Some positions failed to close: " + " | ".join(_errors))
    else:
        st.success(f"Market-close submitted for {_closed} position(s).")


def _render_mobile_view():
    """Single-column mobile dashboard with auto-refresh every 30 seconds."""
    _now_m = datetime.now(pytz.timezone("America/New_York"))

    # ── Equity ─────────────────────────────────────────────────────────────────
    _acct = fetch_account()
    _equity = float(_acct.equity) if _acct else 0.0
    st.markdown(
        f'<div style="text-align:center;padding:24px 0 8px 0;">'
        f'  <div style="font-size:11px;color:#484f58;text-transform:uppercase;'
        f'       letter-spacing:0.12em;font-weight:700;margin-bottom:8px;">Account Equity</div>'
        f'  <div style="font-size:48px;font-weight:800;color:#e6edf3;'
        f'       font-variant-numeric:tabular-nums;line-height:1.1;">${_equity:,.2f}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Today's P&L ────────────────────────────────────────────────────────────
    _daily_df = fetch_daily_pnl()
    _today_str = _now_m.strftime("%Y-%m-%d")
    _today_pnl = 0.0
    if _daily_df is not None and not _daily_df.empty:
        _today_rows = _daily_df[_daily_df["trade_date"].astype(str) == _today_str]
        if not _today_rows.empty:
            _today_pnl = float(_today_rows["daily_pnl_sum"].iloc[0])

    _today_color = "#00c851" if _today_pnl >= 0 else "#ff4444"
    _today_sign  = "+" if _today_pnl >= 0 else ""
    st.markdown(
        f'<div style="text-align:center;padding:4px 0 8px 0;">'
        f'  <div style="font-size:11px;color:#484f58;text-transform:uppercase;'
        f'       letter-spacing:0.12em;font-weight:700;margin-bottom:6px;">Today\'s P&L (closed trades)</div>'
        f'  <div style="font-size:32px;font-weight:700;color:{_today_color};">'
        f'    {_today_sign}{_today_pnl:.2f}%'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Total P&L since inception ───────────────────────────────────────────────
    _total_pnl = 0.0
    if _daily_df is not None and not _daily_df.empty:
        _total_pnl = float(_daily_df["daily_pnl_sum"].sum())

    _total_color = "#00c851" if _total_pnl >= 0 else "#ff4444"
    _total_sign  = "+" if _total_pnl >= 0 else ""
    st.markdown(
        f'<div style="text-align:center;padding:4px 0 24px 0;">'
        f'  <div style="font-size:11px;color:#484f58;text-transform:uppercase;'
        f'       letter-spacing:0.12em;font-weight:700;margin-bottom:6px;">Total P&L since inception</div>'
        f'  <div style="font-size:24px;font-weight:700;color:{_total_color};">'
        f'    {_total_sign}{_total_pnl:.2f}%'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.divider()

    # ── Open Positions ──────────────────────────────────────────────────────────
    st.markdown(
        '<div style="font-size:13px;font-weight:700;color:#484f58;'
        'text-transform:uppercase;letter-spacing:0.12em;margin-bottom:12px;">'
        'Open Positions</div>',
        unsafe_allow_html=True,
    )

    _positions = fetch_positions()

    # Try to find stop prices from open orders for stop-distance display
    _stop_prices: dict = {}
    try:
        _open_orders = trading_client.get_orders()
        for _o in (_open_orders or []):
            _otype = str(getattr(_o, 'order_type', '')).lower()
            if _otype in ("stop", "stop_limit"):
                _sp = getattr(_o, 'stop_price', None)
                if _sp:
                    _stop_prices[str(getattr(_o, 'symbol', ''))] = float(_sp)
    except Exception:
        pass

    if not _positions:
        st.markdown(
            '<div style="color:#484f58;text-align:center;padding:20px 0;">No open positions.</div>',
            unsafe_allow_html=True,
        )
    else:
        for _pos in _positions:
            _sym     = str(_pos.symbol)
            _side    = str(getattr(_pos, 'side', '')).upper()
            _unpl    = float(getattr(_pos, 'unrealized_pl',   0) or 0)
            _unplpc  = float(getattr(_pos, 'unrealized_plpc', 0) or 0) * 100
            _cprice  = float(getattr(_pos, 'current_price',   0) or 0)

            _stop_px = _stop_prices.get(_sym)
            if _stop_px and _cprice > 0:
                _stop_dist = abs(_cprice - _stop_px) / _cprice * 100
                _stop_str  = f"{_stop_dist:.1f}% to stop"
            else:
                _stop_str = "— to stop"

            _pl_color   = "#00c851" if _unpl >= 0 else "#ff4444"
            _pl_sign    = "+" if _unpl >= 0 else ""
            _side_color = "#00c851" if _side == "LONG" else "#ff4444"

            st.markdown(
                f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;'
                f'padding:16px;margin-bottom:10px;">'
                f'  <div style="display:flex;justify-content:space-between;align-items:center;">'
                f'    <span style="font-size:20px;font-weight:700;color:#e6edf3;">{_sym}</span>'
                f'    <span style="font-size:12px;font-weight:700;color:{_side_color};'
                f'          background:{_side_color}1a;border:1px solid {_side_color}44;'
                f'          padding:3px 10px;border-radius:10px;">{_side}</span>'
                f'  </div>'
                f'  <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center;">'
                f'    <span style="font-size:24px;font-weight:700;color:{_pl_color};">'
                f'      {_pl_sign}${_unpl:,.2f}'
                f'    </span>'
                f'    <span style="font-size:13px;color:#7d8590;">{_stop_str}</span>'
                f'  </div>'
                f'  <div style="margin-top:4px;">'
                f'    <span style="font-size:14px;color:{_pl_color};">{_pl_sign}{_unplpc:.2f}%</span>'
                f'  </div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Close All Positions ─────────────────────────────────────────────────────
    if 'mobile_close_all_pending' not in st.session_state:
        st.session_state['mobile_close_all_pending'] = False

    if not st.session_state['mobile_close_all_pending']:
        if st.button(
            "CLOSE ALL POSITIONS",
            key="mobile_close_all_btn",
            use_container_width=True,
            type="primary",
        ):
            st.session_state['mobile_close_all_pending'] = True
            st.rerun()
    else:
        st.warning("This will cancel all open orders and market-close every position. Are you sure?")
        _col_yes, _col_no = st.columns(2)
        with _col_yes:
            if st.button("YES, CLOSE ALL", key="mobile_confirm_yes", use_container_width=True, type="primary"):
                _close_all_positions_action()
                st.session_state['mobile_close_all_pending'] = False
                st.cache_data.clear()
                st.rerun()
        with _col_no:
            if st.button("Cancel", key="mobile_confirm_no", use_container_width=True):
                st.session_state['mobile_close_all_pending'] = False
                st.rerun()

    st.divider()

    # ── Last updated + auto-refresh ─────────────────────────────────────────────
    st.markdown(
        f'<p style="color:#484f58;font-size:12px;text-align:center;">'
        f'Last updated: {_now_m.strftime("%H:%M:%S EST")} · Auto-refreshing every 30s'
        f'</p>',
        unsafe_allow_html=True,
    )
    time.sleep(30)
    st.cache_data.clear()
    st.rerun()


# ── Sidebar ────────────────────────────────────────────────────────────────────

now_est = datetime.now(pytz.timezone("America/New_York"))

# Header
mode_color = "#f0a500" if Config.PAPER_TRADING else "#ff4444"
mode_label = "PAPER TRADING" if Config.PAPER_TRADING else "LIVE TRADING"
st.sidebar.markdown(f"""
<div style="padding:10px 0 4px 0;">
  <div style="font-size:16px;font-weight:800;color:#e6edf3;letter-spacing:0.04em;">
    📈 HYBRID TRADING BOT
  </div>
  <div style="margin-top:8px;">
    <span style="background:{mode_color}1a;color:{mode_color};
                 border:1px solid {mode_color}44;padding:3px 10px;
                 border-radius:10px;font-size:10px;font-weight:700;
                 letter-spacing:0.10em;">{mode_label}</span>
  </div>
</div>
""", unsafe_allow_html=True)

# Bot status
st.sidebar.markdown('<div class="sidebar-section">BOT STATUS</div>', unsafe_allow_html=True)
_health = _bot_health()
if _health:
    uptime_s  = int(_health.get("uptime_seconds", 0))
    h, rem    = divmod(uptime_s, 3600)
    m, s_     = divmod(rem, 60)
    news_min  = _minutes_ago(_health.get("last_news_scan"))
    edgar_min = _minutes_ago(_health.get("last_edgar_scan"))
    ws_ok     = bool(_health.get("websocket_connected", False))
    db_ok     = bool(_health.get("db_connected", False))
    alp_ok    = bool(_health.get("alpaca_connected", False))
    news_ok   = news_min is not None and news_min < 20
    edgar_ok  = edgar_min is not None and edgar_min < 45
    st.sidebar.markdown(
        _status_row(f"Online — {h}h {m}m {s_}s", True)
        + _status_row("DB Connected",       db_ok)
        + _status_row("Alpaca Connected",   alp_ok)
        + _status_row("WebSocket Live",     ws_ok)
        + _status_row("News",  news_ok,  f"{news_min}m ago"  if news_min  is not None else "—")
        + _status_row("EDGAR", edgar_ok, f"{edgar_min}m ago" if edgar_min is not None else "—"),
        unsafe_allow_html=True,
    )
else:
    st.sidebar.markdown(_status_row("Bot Offline / Unreachable", False), unsafe_allow_html=True)

# FRED macro snapshot
st.sidebar.markdown('<div class="sidebar-section">MACRO SNAPSHOT</div>', unsafe_allow_html=True)
fred = _fetch_fred_sidebar()
vix  = fred.get("vix")
ff   = fred.get("fed_funds")
t10  = fred.get("treasury")

def _vix_class(v):
    if v is None: return "macro-neutral"
    if v > 30:    return "macro-danger"
    if v > 20:    return "macro-warn"
    return "macro-ok"

def _t10_class(v):
    if v is None: return "macro-neutral"
    if v > 5.0:   return "macro-danger"
    if v > 4.5:   return "macro-warn"
    return "macro-neutral"

macro_rows = [
    ("VIX",        vix, _vix_class(vix),  f"{vix:.1f}"  if vix is not None else "N/A"),
    ("Fed Funds",  ff,  "macro-neutral",  f"{ff:.2f}%"  if ff  is not None else "N/A"),
    ("10Y Yield",  t10, _t10_class(t10),  f"{t10:.2f}%" if t10 is not None else "N/A"),
]
macro_html = "".join(
    f'<div class="macro-row">'
    f'<span class="macro-label">{lbl}</span>'
    f'<span class="macro-value {cls}">{disp}</span>'
    f'</div>'
    for lbl, _, cls, disp in macro_rows
)
st.sidebar.markdown(macro_html, unsafe_allow_html=True)

# Live price ticker
st.sidebar.markdown('<div class="sidebar-section">LIVE PRICES</div>', unsafe_allow_html=True)
prices = _fetch_prices()
ticker_syms = list(Config.SWING_SYMBOLS) + ["BTC/USD", "ETH/USD"]
price_html = ""
for sym in ticker_syms:
    price = prices.get(sym)
    if price is None:
        price_str = "N/A"
    elif price >= 10_000:
        price_str = f"${price:,.0f}"
    elif price >= 100:
        price_str = f"${price:,.2f}"
    else:
        price_str = f"${price:.4f}"
    price_html += (
        f'<div class="price-row">'
        f'<span class="price-sym">{sym}</span>'
        f'<span class="price-val">{price_str}</span>'
        f'</div>'
    )
st.sidebar.markdown(price_html, unsafe_allow_html=True)

# Refresh + timestamp
st.sidebar.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)
if st.sidebar.button("⟳  Refresh Data", key="sidebar_refresh"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption(f"Prices 30s · FRED 1h · Refreshed {now_est.strftime('%H:%M:%S EST')}")

st.sidebar.markdown('<div class="sidebar-section">VIEW</div>', unsafe_allow_html=True)
_mobile_view = st.sidebar.checkbox("📱 Mobile View", key="mobile_view_toggle", value=False)

# ── Mobile layout (skips desktop tabs when active) ─────────────────────────────

if _mobile_view:
    _render_mobile_view()
    st.stop()

# ── Tabs ───────────────────────────────────────────────────────────────────────

tab_account, tab_positions, tab_tradelog, tab_discovery, tab_analytics, tab_perf, tab_intel, tab_decay = st.tabs([
    "💰 Account",
    "📊 Positions",
    "📋 Trade Log",
    "🔬 Discovery",
    "📈 Analytics",
    "🧠 Performance",
    "📡 Signal Intel",
    "🩺 Decay",
])

# ── Tab 1: Account ─────────────────────────────────────────────────────────────

with tab_account:
    st.header("Account Overview")
    account = fetch_account()

    if not account:
        st.error("Could not retrieve account from Alpaca.")
    else:
        equity       = float(account.equity)
        buying_power = float(account.buying_power)
        cash         = float(account.cash)
        status_str   = str(account.status).upper()
        mode_str     = "Paper" if Config.PAPER_TRADING else "Live"
        status_class = "positive" if status_str == "ACTIVE" else "neutral"

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            st.markdown(_metric_card("Equity",       f"${equity:,.2f}"),         unsafe_allow_html=True)
        with c2:
            st.markdown(_metric_card("Buying Power", f"${buying_power:,.2f}"),    unsafe_allow_html=True)
        with c3:
            st.markdown(_metric_card("Cash",         f"${cash:,.2f}"),            unsafe_allow_html=True)
        with c4:
            st.markdown(_metric_card("Mode",         mode_str),                   unsafe_allow_html=True)
        with c5:
            st.markdown(_metric_card("Status",       status_str, status_class),   unsafe_allow_html=True)

        positions = fetch_positions()
        if positions:
            total_mv   = sum(float(p.market_value)   for p in positions)
            total_unrl = sum(float(p.unrealized_pl)  for p in positions)
            pnl_class  = "positive" if total_unrl >= 0 else "negative"
            pnl_sign   = "+" if total_unrl >= 0 else ""

            st.markdown("---")
            d1, d2, d3 = st.columns(3)
            with d1:
                st.markdown(_metric_card("Open Positions",   str(len(positions))),                          unsafe_allow_html=True)
            with d2:
                st.markdown(_metric_card("Total Market Value", f"${total_mv:,.2f}"),                        unsafe_allow_html=True)
            with d3:
                st.markdown(_metric_card("Unrealized P&L", f"{pnl_sign}${total_unrl:,.2f}", pnl_class),    unsafe_allow_html=True)

        st.markdown("---")
        st.markdown(
            '<p style="color:#484f58;font-size:10px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:0.10em;margin-bottom:12px;">'
            'Risk Configuration</p>',
            unsafe_allow_html=True,
        )
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            st.markdown(_metric_card("Equity Risk / Trade", f"{Config.SWING_EQUITY_RISK_PERCENT}%"), unsafe_allow_html=True)
        with r2:
            st.markdown(_metric_card("Stop Loss",           f"{Config.STOP_LOSS_PERCENT}%"),         unsafe_allow_html=True)
        with r3:
            st.markdown(_metric_card("Take Profit",         f"{Config.TAKE_PROFIT_PERCENT}%"),       unsafe_allow_html=True)
        with r4:
            st.markdown(_metric_card("Max Daily Loss",      f"{Config.MAX_DAILY_LOSS_PERCENT}%"),    unsafe_allow_html=True)

        # ── Go-Live Readiness ────────────────────────────────────────────────
        st.markdown("---")
        st.subheader("Go-Live Readiness")

        readiness = fetch_golive_readiness()
        health = _bot_health()
        uptime_days = health.get("uptime_seconds", 0) / 86400 if health else None

        _criteria: list[tuple[str, bool, str]] = []

        # 1. Positive returns ≥3 of last 4 paper months
        mp = readiness.get("monthly_positive")
        mt = readiness.get("months_total") or 0
        if mp is not None:
            _criteria.append((
                "Positive returns ≥3 of last 4 paper months",
                mp >= 3,
                f"{mp}/{mt} months positive",
            ))
        else:
            _criteria.append(("Positive returns ≥3 of last 4 paper months", False, "Insufficient trade data"))

        # 2. Win rate >50%
        wr = readiness.get("win_rate")
        n_trades = readiness.get("total_trades", 0)
        if wr is not None and n_trades > 0:
            _criteria.append(("Win rate >50%", wr > 0.5, f"{wr:.1%} across {n_trades} trades"))
        else:
            _criteria.append(("Win rate >50%", False, "No closed trades yet"))

        # 3. Max drawdown <15%
        dd = readiness.get("max_drawdown_pct")
        if dd is not None:
            _criteria.append((
                "Max drawdown <15% (cumulative P&L)",
                dd < 15.0,
                f"{dd:.1f} P&L points cumulative drawdown",
            ))
        else:
            _criteria.append(("Max drawdown <15% (cumulative P&L)", False, "No trade data"))

        # 4. Bot uptime ≥30 consecutive days
        if uptime_days is not None:
            _criteria.append((
                "Bot uptime ≥30 consecutive days",
                uptime_days >= 30,
                f"{uptime_days:.1f} days since last restart",
            ))
        else:
            _criteria.append(("Bot uptime ≥30 consecutive days", False, "Health endpoint unreachable"))

        # 5. Discovery Engine ≥3 validated strategies
        vs = readiness.get("validated_strategies")
        if vs is not None:
            _criteria.append((
                "Discovery Engine: ≥3 validated strategies",
                vs >= 3,
                f"{vs} strategy combos validated",
            ))
        else:
            _criteria.append(("Discovery Engine: ≥3 validated strategies", False, "No discovery data"))

        _score = sum(1 for _, m, _ in _criteria if m)
        _score_color = "#00c851" if _score == 5 else ("#f0a500" if _score >= 3 else "#ff4444")
        _score_label = (
            "Ready for live trading"
            if _score == 5 else ("Getting close" if _score >= 3 else "Not yet ready")
        )

        st.markdown(
            f'<div class="metric-card" style="margin-bottom:16px;">'
            f'<div class="card-label">Overall Readiness</div>'
            f'<div class="card-value" style="color:{_score_color};font-size:28px;">{_score}/5</div>'
            f'<div style="font-size:11px;color:#484f58;margin-top:4px;">{_score_label}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        for _label, _met, _detail in _criteria:
            _dot = "dot-green" if _met else "dot-red"
            st.markdown(
                f'<div class="status-row">'
                f'<span class="status-dot {_dot}"></span>'
                f'<span>{_label}</span>'
                f'<span class="status-detail">&nbsp;—&nbsp;{_detail}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

# ── Tab 2: Positions ───────────────────────────────────────────────────────────

with tab_positions:
    st.header("Open Positions")
    positions = fetch_positions()

    if not positions:
        st.info("No open positions right now.")
    else:
        rows = []
        for p in positions:
            unrl_pct = float(p.unrealized_plpc) * 100
            rows.append({
                "Symbol":           p.symbol,
                "Side":             str(p.side).upper(),
                "Qty":              float(p.qty),
                "Entry Price":      float(p.avg_entry_price),
                "Current Price":    float(p.current_price),
                "Unrealized P&L %": round(unrl_pct, 2),
                "Unrealized P&L $": round(float(p.unrealized_pl), 2),
                "Market Value":     round(float(p.market_value), 2),
            })

        df_pos = pd.DataFrame(rows)
        totals = {
            "Symbol": "TOTAL", "Side": "", "Qty": "",
            "Entry Price": "", "Current Price": "",
            "Unrealized P&L %": round(df_pos["Unrealized P&L %"].sum(), 2),
            "Unrealized P&L $": round(df_pos["Unrealized P&L $"].sum(), 2),
            "Market Value":     round(df_pos["Market Value"].sum(), 2),
        }
        df_display = pd.concat([df_pos, pd.DataFrame([totals])], ignore_index=True)
        styled = (
            df_display.style
            .map(_pnl_color, subset=["Unrealized P&L %", "Unrealized P&L $"])
            .format({
                "Entry Price":      "${:.2f}",
                "Current Price":    "${:.2f}",
                "Market Value":     "${:,.2f}",
                "Unrealized P&L $": "${:+,.2f}",
                "Unrealized P&L %": "{:+.2f}%",
            }, na_rep="")
        )
        st.dataframe(styled, width="stretch", hide_index=True)

# ── Tab 3: Trade Log ───────────────────────────────────────────────────────────

with tab_tradelog:
    st.header("Trade Log")

    if not _db_available():
        st.info(
            "DATABASE_URL is not configured. "
            "Add a PostgreSQL database in Railway to see trade history."
        )
    else:
        f1, f2, f3 = st.columns([2, 2, 1])
        with f1:
            date_input = st.date_input(
                "Date range",
                value=(now_est.date() - timedelta(days=30), now_est.date()),
                key="tl_dates",
            )
        with f2:
            sym_filter = st.multiselect(
                "Symbols (blank = all)",
                options=sorted(Config.SWING_SYMBOLS + list(Config.SCALP_SYMBOLS)),
                key="tl_symbols",
            )
        with f3:
            open_only = st.checkbox("Open only", value=False, key="tl_open")

        if isinstance(date_input, (list, tuple)) and len(date_input) == 2:
            start_d, end_d = date_input
        else:
            start_d = end_d = date_input if not isinstance(date_input, (list, tuple)) else date_input[0]

        start_dt = datetime.combine(start_d, datetime.min.time())
        end_dt   = datetime.combine(end_d,   datetime.max.time())

        df = fetch_signal_outcomes(start_dt, end_dt, tuple(sym_filter))

        if df is None:
            st.warning("Could not load trade data.")
        elif df.empty:
            st.info("No trades found for the selected filters.")
        else:
            if open_only:
                df = df[df["exit_time"].isna()].copy()

            df.insert(0, "Status", df["exit_time"].apply(
                lambda x: "OPEN" if pd.isna(x) else "CLOSED"
            ))

            show_cols = [
                "Status", "symbol", "signal_type",
                "entry_time", "exit_time",
                "entry_price", "exit_price", "pnl_pct",
                "exit_reason", "market_regime", "hold_bars",
            ]
            df_show = df[show_cols].copy()
            df_show["entry_time"] = pd.to_datetime(df_show["entry_time"]).dt.strftime("%Y-%m-%d %H:%M")
            df_show["exit_time"]  = pd.to_datetime(df_show["exit_time"]).dt.strftime("%Y-%m-%d %H:%M")

            st.markdown(f"**{len(df)} trades** matched")
            styled_tl = (
                df_show.style
                .map(_pnl_color, subset=["pnl_pct"])
                .format({
                    "entry_price": "${:.2f}",
                    "exit_price":  "${:.2f}",
                    "pnl_pct":     "{:+.2f}%",
                }, na_rep="—")
            )
            st.dataframe(styled_tl, width="stretch", hide_index=True)

            csv_bytes = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇ Download CSV",
                data=csv_bytes,
                file_name=f"signal_outcomes_{start_d}_{end_d}.csv",
                mime="text/csv",
            )

# ── Tab 4: Discovery Results ───────────────────────────────────────────────────

# Strategy families (for funnel/leaderboard/coverage panels — Task 8).
_DISCOVERY_FAMILY_NAMES = [
    "swing_ema_macd_rsi", "mean_reversion_bb_rsi", "volume_breakout_obv",
    "insider_flow_form4", "smc_order_block_fvg", "pead_earnings_drift",
    "short_interest_momentum", "sector_rotation",
]
_REGIME_FLAG_COLS = {
    "BULL_TREND": "valid_bull_trend",
    "BEAR_TREND": "valid_bear_trend",
    "HIGH_VOL": "valid_high_vol",
    "CHOPPY": "valid_choppy",
}


def _next_discovery_run(now_et: datetime) -> datetime:
    """Next Friday 16:30 ET (the weekly Discovery Engine run)."""
    target_hour, target_min = 16, 30
    days_ahead = (4 - now_et.weekday()) % 7  # Friday == weekday 4
    candidate = now_et.replace(hour=target_hour, minute=target_min, second=0, microsecond=0) \
        + timedelta(days=days_ahead)
    if candidate <= now_et:
        candidate += timedelta(days=7)
    return candidate


@st.cache_data(ttl=300)
def fetch_validated_strategies() -> "pd.DataFrame | None":
    """All rows from validated_strategies (Task 8 leaderboard/regime/coverage)."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            return pd.read_sql(text("""
                SELECT symbol, strategy_name, best_regime,
                       valid_bull_trend, valid_bear_trend, valid_high_vol, valid_choppy,
                       gross_sharpe_before_costs, net_sharpe_after_costs, validated_at
                FROM validated_strategies
            """), conn)
    except Exception as e:
        print(f"[Dashboard] fetch_validated_strategies failed: {e}")
        return None


@st.cache_data(ttl=300)
def fetch_v1_funnel() -> "pd.DataFrame | None":
    """v1 swing-family funnel counts from strategy_results (per symbol)."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            return pd.read_sql(text("""
                SELECT
                    COUNT(*)                                              AS combos_tested,
                    COUNT(*) FILTER (WHERE status IN ('validated','rejected_permutation'))
                                                                         AS ttest_passers,
                    COUNT(*) FILTER (WHERE permutation_tested AND status='validated')
                                                                         AS permutation_passers,
                    COUNT(*) FILTER (WHERE status='validated')           AS promoted
                FROM strategy_results
            """), conn)
    except Exception as e:
        print(f"[Dashboard] fetch_v1_funnel failed: {e}")
        return None


@st.cache_data(ttl=300)
def fetch_discovered_indicators() -> "pd.DataFrame | None":
    """Graduated evolved indicators (Task 7) for the Task 8 dashboard table."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            return pd.read_sql(text("""
                SELECT symbol, formula, regime, mean_ic, std_ic, val_ic, val_pvalue,
                       discovered_at
                FROM discovered_indicators
                WHERE status = 'graduated'
                ORDER BY mean_ic DESC
                LIMIT 200
            """), conn)
    except Exception as e:
        print(f"[Dashboard] fetch_discovered_indicators failed: {e}")
        return None


@st.cache_data(ttl=120)
def fetch_revalidation_queue() -> "pd.DataFrame | None":
    """Pending re-validation requests with age (Task 8)."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            return pd.read_sql(text("""
                SELECT symbol, strategy_name, reason, discovery_version,
                       requested_at, status
                FROM revalidation_queue
                WHERE status = 'pending'
                ORDER BY requested_at ASC
            """), conn)
    except Exception as e:
        print(f"[Dashboard] fetch_revalidation_queue failed: {e}")
        return None


with tab_discovery:
    st.header("Strategy Discovery Results")

    if not _db_available():
        st.info(
            "DATABASE_URL is not configured. "
            "Run `python -m discovery.discovery_engine_v2` and connect PostgreSQL to see results."
        )
    else:
        validated_only = st.toggle("Validated / pending approval only", value=True, key="disc_toggle")
        df_disc = fetch_strategy_results(validated_only=validated_only)

        if df_disc is None:
            st.warning("Could not load discovery data.")
        elif df_disc.empty:
            st.info(
                "No results yet. Run:\n```\npython -m discovery.discovery_engine_v2\n```"
            )
        else:
            # Approval status banner
            pending_count = int((df_disc["status"] == "pending_approval").sum()) if "status" in df_disc.columns else 0
            approved_count = int((df_disc["status"] == "approved").sum()) if "status" in df_disc.columns else 0
            if pending_count > 0:
                st.warning(
                    f":hourglass_flowing_sand: **{pending_count} strategies awaiting approval.** "
                    f"Review the candidates below and click Approve to activate."
                )
            if approved_count > 0:
                st.success(f":white_check_mark: {approved_count} strategies approved and active in swing_loop.")

            st.subheader("Summary by Symbol")
            agg_cols: dict = {
                "total":          ("status",     "count"),
                "best_sharpe":    ("test_sharpe", "max"),
                "avg_sharpe":     ("test_sharpe", "mean"),
            }
            if "strategy_type" in df_disc.columns:
                summary_groups = df_disc.groupby("symbol").agg(**agg_cols).reset_index()
            else:
                summary_groups = df_disc.groupby("symbol").agg(**agg_cols).reset_index()

            st.dataframe(
                summary_groups.style.format({
                    "best_sharpe": "{:.3f}",
                    "avg_sharpe":  "{:.3f}",
                }),
                width="stretch",
                hide_index=True,
            )

            st.subheader("All Results — Ranked by Test Sharpe")
            display_cols = [c for c in [
                "symbol", "strategy_type", "best_regime", "status",
                "test_sharpe", "train_sharpe", "degradation", "win_rate",
                "total_trades", "p_value", "date",
            ] if c in df_disc.columns]

            fmt = {
                "train_sharpe": "{:.3f}",
                "test_sharpe":  "{:.3f}",
                "degradation":  "{:.3f}",
                "win_rate":     "{:.1%}",
                "p_value":      "{:.4f}",
            }
            styled_disc = (
                df_disc[display_cols].style
                .map(_status_color, subset=["status"])
                .map(_pnl_color,    subset=["test_sharpe", "train_sharpe"])
                .format({k: v for k, v in fmt.items() if k in display_cols}, na_rep="—")
            )
            st.dataframe(styled_disc, width="stretch", hide_index=True)

            # ── Approve pending strategies ──────────────────────────────────
            pending_df = (
                df_disc[df_disc["status"] == "pending_approval"]
                if "status" in df_disc.columns else pd.DataFrame()
            )
            if not pending_df.empty and "id" in pending_df.columns:
                st.markdown("---")
                st.subheader("Approve Pending Strategies")
                st.caption(
                    "Each candidate passed walk-forward validation. "
                    "Approve to activate it in the swing loop."
                )
                hdr = st.columns([2, 2, 1.5, 1.5, 1.5, 1.2])
                for label, col in zip(
                    ["Symbol", "Strategy Type", "Test Sharpe", "Win Rate", "Best Regime", ""],
                    hdr,
                ):
                    col.markdown(f"**{label}**")

                for _, prow in pending_df.iterrows():
                    row_id = int(prow["id"])
                    cols = st.columns([2, 2, 1.5, 1.5, 1.5, 1.2])
                    cols[0].write(prow.get("symbol", "—"))
                    cols[1].write(prow.get("strategy_type", "—") or "—")
                    ts = prow.get("test_sharpe")
                    cols[2].write(f"{ts:.3f}" if pd.notna(ts) else "—")
                    wr = prow.get("win_rate")
                    cols[3].write(f"{float(wr):.1%}" if pd.notna(wr) else "—")
                    cols[4].write(prow.get("best_regime", "—") or "—")
                    if cols[5].button("Approve", key=f"approve_{row_id}"):
                        if _approve_strategy(row_id):
                            fetch_strategy_results.clear()
                            st.toast(
                                f"✅ {prow.get('symbol', '')} {prow.get('strategy_type', '')} approved!",
                                icon="✅",
                            )
                            st.rerun()

        # ══ Discovery Engine analytics (Task 8) ══════════════════════════════
        st.markdown("---")
        st.subheader("🔬 Discovery Engine Analytics")

        # ── Next run countdown ───────────────────────────────────────────────
        _now_et = datetime.now(pytz.timezone("America/New_York"))
        _next_run = _next_discovery_run(_now_et)
        _delta = _next_run - _now_et
        _d, _rem = divmod(int(_delta.total_seconds()), 86400)
        _h, _rem = divmod(_rem, 3600)
        _m, _ = divmod(_rem, 60)
        cdn1, cdn2 = st.columns([1, 2])
        cdn1.metric("Next Discovery Run", f"{_d}d {_h}h {_m}m")
        cdn2.caption(
            f"Scheduled **{_next_run.strftime('%a %Y-%m-%d %H:%M %Z')}** "
            f"(weekly Friday 4:30 PM ET run). The v2 engine re-validates the full "
            f"universe on this run."
        )

        df_val = fetch_validated_strategies()

        # ── Validation funnel (v1 swing family) ──────────────────────────────
        st.markdown("#### Validation Funnel")
        df_funnel = fetch_v1_funnel()
        if df_funnel is not None and not df_funnel.empty and int(df_funnel.iloc[0]["combos_tested"]) > 0:
            r = df_funnel.iloc[0]
            f1, f2, f3, f4 = st.columns(4)
            f1.metric("Combos tested", f"{int(r['combos_tested']):,}")
            f2.metric("t-test passers", f"{int(r['ttest_passers']):,}")
            f3.metric("Permutation passers", f"{int(r['permutation_passers']):,}")
            f4.metric("Promoted", f"{int(r['promoted']):,}")
            st.caption(
                "Funnel reflects the v1 grid-search swing family (`strategy_results`). "
                "Per-family earlier-stage counts for the position-vector families aren't "
                "persisted — their promotions appear in the leaderboard below "
                "(`validated_strategies`)."
            )
        else:
            st.info("No `strategy_results` rows yet — run the v1 Discovery Engine to populate the funnel.")

        # ── Strategy family leaderboard ──────────────────────────────────────
        st.markdown("#### Strategy Family Leaderboard")
        if df_val is not None and not df_val.empty:
            lb = (
                df_val.groupby("strategy_name")
                .agg(validated=("symbol", "count"),
                     avg_net_sharpe=("net_sharpe_after_costs", "mean"),
                     best_net_sharpe=("net_sharpe_after_costs", "max"))
                .reset_index()
                .sort_values("validated", ascending=False)
            )
            st.dataframe(
                lb.style.format({
                    "avg_net_sharpe": "{:.3f}", "best_net_sharpe": "{:.3f}",
                }, na_rep="—"),
                width="stretch", hide_index=True,
            )
        else:
            st.info("No validated strategies yet (`validated_strategies` empty).")

        # ── Regime breakdown ─────────────────────────────────────────────────
        st.markdown("#### Validated Strategies by Regime")
        if df_val is not None and not df_val.empty:
            reg_counts = {
                regime: int(df_val[col].fillna(False).astype(bool).sum())
                for regime, col in _REGIME_FLAG_COLS.items() if col in df_val.columns
            }
            if reg_counts:
                rc = st.columns(len(reg_counts))
                for (regime, cnt), col in zip(reg_counts.items(), rc):
                    col.metric(regime, cnt)
        else:
            st.caption("—")

        # ── Symbol coverage heatmap (symbol × family, net Sharpe) ────────────
        st.markdown("#### Symbol Coverage")
        if df_val is not None and not df_val.empty:
            pivot = df_val.pivot_table(
                index="symbol", columns="strategy_name",
                values="net_sharpe_after_costs", aggfunc="max",
            )
            # Ensure all families show as columns so gaps (unvalidated) are visible.
            for fam in _DISCOVERY_FAMILY_NAMES:
                if fam not in pivot.columns:
                    pivot[fam] = float("nan")
            pivot = pivot[[c for c in _DISCOVERY_FAMILY_NAMES if c in pivot.columns]]
            heat = go.Figure(data=go.Heatmap(
                z=pivot.values, x=list(pivot.columns), y=list(pivot.index),
                colorscale="RdYlGn", zmid=0,
                colorbar=dict(title="net Sharpe"),
                hovertemplate="%{y} / %{x}<br>net Sharpe=%{z:.3f}<extra></extra>",
            ))
            heat.update_layout(
                height=max(220, 26 * len(pivot.index) + 80),
                margin=dict(l=0, r=0, t=10, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#7d8590", size=11),
                xaxis=dict(tickangle=-30),
            )
            st.plotly_chart(heat, use_container_width=True)
            st.caption(
                f"{pivot.index.nunique()} symbols have ≥1 validated strategy. "
                "Blank cells = that family is not validated for the symbol."
            )
        else:
            st.caption("No coverage data.")

        # ── Discovered (evolved) indicators ──────────────────────────────────
        st.markdown("#### Discovered Indicators (Evolved GP)")
        df_ind = fetch_discovered_indicators()
        if df_ind is not None and not df_ind.empty:
            st.dataframe(
                df_ind.style.format({
                    "mean_ic": "{:.3f}", "std_ic": "{:.3f}",
                    "val_ic": "{:.3f}", "val_pvalue": "{:.4f}",
                }, na_rep="—"),
                width="stretch", hide_index=True,
            )
            st.caption(f"{len(df_ind)} graduated indicators across "
                       f"{df_ind['symbol'].nunique()} symbols.")
        else:
            st.info("No graduated indicators yet — run the Indicator Discovery scheduler.")

        # ── Re-validation queue ──────────────────────────────────────────────
        st.markdown("#### Re-validation Queue")
        df_q = fetch_revalidation_queue()
        if df_q is not None and not df_q.empty:
            df_q = df_q.copy()
            _now_utc = pd.Timestamp.now(tz="UTC")
            _req = pd.to_datetime(df_q["requested_at"], utc=True, errors="coerce")
            df_q["age"] = (_now_utc - _req).apply(
                lambda d: "—" if pd.isna(d) else f"{int(d.total_seconds() // 3600)}h"
            )
            st.dataframe(
                df_q[["symbol", "strategy_name", "reason", "discovery_version", "age", "status"]],
                width="stretch", hide_index=True,
            )
            st.caption(f"{len(df_q)} pending request(s).")
        else:
            st.success("Re-validation queue is empty.")

# ── Tab 5: Analytics ───────────────────────────────────────────────────────────

_CHART_LAYOUT = dict(
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#7d8590", size=12),
    xaxis=dict(
        gridcolor="#21262d", linecolor="#30363d",
        tickfont=dict(color="#7d8590"),
        title_font=dict(color="#7d8590"),
    ),
    yaxis=dict(
        gridcolor="#21262d", linecolor="#30363d",
        tickfont=dict(color="#7d8590"),
        title_font=dict(color="#7d8590"),
    ),
    hovermode="x unified",
    margin=dict(l=0, r=0, t=10, b=0),
    showlegend=False,
)

with tab_analytics:
    st.header("Analytics")

    if not _db_available():
        st.info(
            "DATABASE_URL is not configured. "
            "Connect PostgreSQL to see P&L charts and win rate breakdown."
        )
    else:
        # ── Cumulative P&L ─────────────────────────────────────────────────────
        st.subheader("Cumulative P&L Over Time")
        df_pnl = fetch_daily_pnl()

        if df_pnl is not None and not df_pnl.empty:
            df_pnl = df_pnl.sort_values("trade_date")
            df_pnl["cumulative_pnl"] = df_pnl["daily_pnl_sum"].cumsum()

            positive   = df_pnl["cumulative_pnl"].iloc[-1] >= 0
            line_color = "#00c851" if positive else "#ff4444"
            fill_color = "rgba(0,200,81,0.10)" if positive else "rgba(255,68,68,0.10)"

            fig_pnl = go.Figure()
            fig_pnl.add_trace(go.Scatter(
                x=df_pnl["trade_date"],
                y=df_pnl["cumulative_pnl"],
                mode="lines+markers",
                name="Cumulative P&L %",
                line=dict(color=line_color, width=2),
                fill="tozeroy",
                fillcolor=fill_color,
                marker=dict(size=4, color=line_color),
                hovertemplate="%{x}<br>Cumulative: %{y:+.2f}%<extra></extra>",
            ))
            fig_pnl.add_hline(y=0, line_dash="dash", line_color="#30363d")
            fig_pnl.update_layout(height=400, **_CHART_LAYOUT)
            fig_pnl.update_layout(
                xaxis_title="Date",
                yaxis_title="Cumulative P&L (%)",
            )
            st.plotly_chart(fig_pnl, width="stretch")
            st.caption(
                f"Sum of pnl_pct across {int(df_pnl['trades'].sum())} closed trades "
                f"({len(df_pnl)} trading days). "
                f"Not dollar-weighted — equal weight per trade."
            )
        else:
            st.info("No closed trades yet — P&L chart will populate once positions close.")

        st.markdown("---")

        # ── Win rate by symbol ─────────────────────────────────────────────────
        st.subheader("Win Rate by Symbol")
        df_wr = fetch_win_rate()

        if df_wr is not None and not df_wr.empty:
            colors = ["#00c851" if w >= 50 else "#ff4444" for w in df_wr["win_rate_pct"]]
            fig_wr = go.Figure()
            fig_wr.add_trace(go.Bar(
                x=df_wr["symbol"],
                y=df_wr["win_rate_pct"],
                marker_color=colors,
                text=[
                    f"{w:.0f}%<br>{n} trades"
                    for w, n in zip(df_wr["win_rate_pct"], df_wr["total_trades"])
                ],
                textposition="outside",
                textfont=dict(color="#7d8590", size=11),
                hovertemplate="%{x}<br>Win rate: %{y:.1f}%<extra></extra>",
            ))
            fig_wr.add_hline(
                y=50,
                line_dash="dash",
                line_color="#30363d",
                annotation_text="50% breakeven",
                annotation_position="right",
                annotation_font_color="#484f58",
            )
            fig_wr.update_layout(height=380, yaxis_range=[0, 115], **_CHART_LAYOUT)
            fig_wr.update_layout(xaxis_title="Symbol", yaxis_title="Win Rate (%)")
            st.plotly_chart(fig_wr, width="stretch")

            styled_wr = (
                df_wr.style
                .map(_pnl_color, subset=["avg_pnl_pct"])
                .format({
                    "win_rate_pct": "{:.1f}%",
                    "avg_pnl_pct":  "{:+.2f}%",
                })
            )
            st.dataframe(styled_wr, width="stretch", hide_index=True)
        else:
            st.info("No closed trades yet — win rate will populate once positions close.")

        st.markdown("---")

        # ── Expected Value by Strategy ─────────────────────────────────────────
        st.subheader("Expected Value by Strategy")
        st.caption(
            "EV = (win_rate × avg_win%) − (loss_rate × avg_loss%). "
            "Positive EV = edge exists. Negative EV = strategy losing money on average. "
            "Requires ≥ 20 closed trades."
        )
        df_ev = fetch_strategy_ev()

        if df_ev is None:
            st.warning("Could not load EV data.")
        elif df_ev.empty:
            st.info("Not enough closed trades yet (≥ 20 required per strategy).")
        else:
            def _ev_color(val):
                if not isinstance(val, (int, float)):
                    return ""
                return "color: #00c851" if val > 0 else "color: #ff4444"

            styled_ev = (
                df_ev.style
                .map(_ev_color, subset=["ev_pct"])
                .format({
                    "win_rate_pct": "{:.1f}%",
                    "avg_win_pct":  "{:+.2f}%",
                    "avg_loss_pct": "{:.2f}%",
                    "ev_pct":       "{:+.3f}%",
                }, na_rep="—")
            )
            st.dataframe(styled_ev, width="stretch", hide_index=True)

# ── Tab 6: Performance Brain ───────────────────────────────────────────────────

_REGIME_COLORS = {"bull": "#00c851", "bear": "#ff4444", "neutral": "#f0a500"}
_DOW_NAMES     = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri"}

with tab_perf:
    st.header("Performance Brain")
    st.caption(
        "Derived from `signal_outcomes`. Win rate thresholds drive live position-size scaling: "
        "WR >60% → +20%, WR <40% → −30%, floor at 10% of normal. "
        "Requires ≥ 3 closed trades per group."
    )

    if not _db_available():
        st.info(
            "DATABASE_URL is not configured. "
            "Connect PostgreSQL to see performance analytics."
        )
    else:
        # ── Win rate by signal type ─────────────────────────────────────────────
        st.subheader("Win Rate by Signal Type")
        df_st = fetch_performance_by_dimension("signal_type")

        if df_st is not None and not df_st.empty:
            _colors_st = ["#00c851" if w >= 50 else "#ff4444" for w in df_st["win_rate"]]
            fig_st = go.Figure()
            fig_st.add_trace(go.Bar(
                x=df_st["win_rate"],
                y=df_st["dim_value"].astype(str),
                orientation="h",
                marker_color=_colors_st,
                text=[
                    f"{w:.0f}%  ({n} trades)"
                    for w, n in zip(df_st["win_rate"], df_st["trade_count"])
                ],
                textposition="outside",
                textfont=dict(color="#7d8590", size=11),
                hovertemplate="%{y}<br>Win rate: %{x:.1f}%<extra></extra>",
            ))
            fig_st.add_vline(
                x=50, line_dash="dash", line_color="#30363d",
                annotation_text="50%", annotation_position="top right",
                annotation_font_color="#484f58",
            )
            _layout_no_hover = {k: v for k, v in _CHART_LAYOUT.items() if k != "hovermode"}
            fig_st.update_layout(
                height=max(280, 70 * len(df_st)),
                xaxis_range=[0, 125],
                hovermode="y unified",
                **_layout_no_hover,
            )
            fig_st.update_layout(xaxis_title="Win Rate (%)", yaxis_title="")
            st.plotly_chart(fig_st, width="stretch")

            styled_st = (
                df_st.style
                .map(_pnl_color, subset=["ev"])
                .format({
                    "win_rate":    "{:.1f}%",
                    "avg_win_pct": "{:+.2f}%",
                    "avg_loss_pct": "{:.2f}%",
                    "ev":          "{:+.3f}%",
                }, na_rep="—")
            )
            st.dataframe(styled_st, width="stretch", hide_index=True)
        else:
            st.info("No closed trades yet — signal type breakdown will populate once positions close.")

        st.markdown("---")

        # ── Win rate by market regime ───────────────────────────────────────────
        st.subheader("Win Rate by Market Regime")
        df_regime = fetch_win_rate_by_regime()

        if df_regime is not None and not df_regime.empty:
            fig_reg = go.Figure()
            for regime_col in df_regime.columns:
                col_vals = df_regime[regime_col]
                if col_vals.dropna().empty:
                    continue
                fig_reg.add_trace(go.Bar(
                    name=str(regime_col).capitalize(),
                    x=df_regime.index.astype(str),
                    y=col_vals,
                    marker_color=_REGIME_COLORS.get(str(regime_col), "#7d8590"),
                    hovertemplate=f"{str(regime_col).capitalize()}: %{{y:.1f}}%<extra></extra>",
                ))
            fig_reg.add_hline(y=50, line_dash="dash", line_color="#30363d")
            _layout_no_legend = {k: v for k, v in _CHART_LAYOUT.items() if k != "showlegend"}
            fig_reg.update_layout(
                height=380, barmode="group", showlegend=True,
                legend=dict(font=dict(color="#7d8590")),
                **_layout_no_legend,
            )
            fig_reg.update_layout(
                xaxis_title="Strategy", yaxis_title="Win Rate (%)", yaxis_range=[0, 120],
            )
            st.plotly_chart(fig_reg, width="stretch")
        else:
            st.info("No regime data yet — win rate by regime will populate as trades close.")

        st.markdown("---")

        # ── Win rate by day of week ─────────────────────────────────────────────
        st.subheader("Win Rate by Day of Week")
        df_dow = fetch_performance_by_dimension("day_of_week")

        if df_dow is not None and not df_dow.empty:
            df_dow = df_dow.copy()
            df_dow["dim_value"] = pd.to_numeric(df_dow["dim_value"], errors="coerce")
            df_dow = df_dow[df_dow["dim_value"].between(1, 5)].sort_values("dim_value")
            df_dow["day_name"] = df_dow["dim_value"].map(_DOW_NAMES)

            _colors_dow = ["#00c851" if w >= 50 else "#ff4444" for w in df_dow["win_rate"]]
            fig_dow = go.Figure()
            fig_dow.add_trace(go.Bar(
                x=df_dow["day_name"],
                y=df_dow["win_rate"],
                marker_color=_colors_dow,
                text=[f"{w:.0f}%<br>{n} trades" for w, n in
                      zip(df_dow["win_rate"], df_dow["trade_count"])],
                textposition="outside",
                textfont=dict(color="#7d8590", size=11),
                hovertemplate="%{x}: %{y:.1f}% win rate<extra></extra>",
            ))
            fig_dow.add_hline(y=50, line_dash="dash", line_color="#30363d")
            fig_dow.update_layout(height=320, yaxis_range=[0, 125], **_CHART_LAYOUT)
            fig_dow.update_layout(xaxis_title="Day of Week", yaxis_title="Win Rate (%)")
            st.plotly_chart(fig_dow, width="stretch")
        else:
            st.info("No day-of-week data yet.")

        st.markdown("---")

        # ── Win rate by hour of day ─────────────────────────────────────────────
        st.subheader("Win Rate by Entry Hour (EST)")
        df_hour = fetch_performance_by_dimension("hour_of_day")

        if df_hour is not None and not df_hour.empty:
            df_hour = df_hour.copy()
            df_hour["dim_value"] = pd.to_numeric(df_hour["dim_value"], errors="coerce")
            df_hour = df_hour[df_hour["dim_value"].between(9, 16)].sort_values("dim_value")
            df_hour["hour_label"] = df_hour["dim_value"].apply(lambda h: f"{int(h):02d}:00")

            _colors_hr = ["#00c851" if w >= 50 else "#ff4444" for w in df_hour["win_rate"]]
            fig_hr = go.Figure()
            fig_hr.add_trace(go.Bar(
                x=df_hour["hour_label"],
                y=df_hour["win_rate"],
                marker_color=_colors_hr,
                text=[f"{w:.0f}%<br>{n} trades" for w, n in
                      zip(df_hour["win_rate"], df_hour["trade_count"])],
                textposition="outside",
                textfont=dict(color="#7d8590", size=11),
                hovertemplate="%{x}: %{y:.1f}% win rate<extra></extra>",
            ))
            fig_hr.add_hline(y=50, line_dash="dash", line_color="#30363d")
            fig_hr.update_layout(height=320, yaxis_range=[0, 125], **_CHART_LAYOUT)
            fig_hr.update_layout(xaxis_title="Entry Hour (EST)", yaxis_title="Win Rate (%)")
            st.plotly_chart(fig_hr, width="stretch")
        else:
            st.info("No hour-of-day data yet.")

        st.markdown("---")

        # ── Best time windows ───────────────────────────────────────────────────
        st.subheader("Best Time Windows")
        st.caption(
            "Top 5 (weekday × entry hour) windows ranked by EV "
            f"— requires ≥ 5 trades per window."
        )
        df_tw = fetch_best_time_windows(min_trades=5, top_n=5)

        if df_tw is None:
            st.warning("Could not load time-window data.")
        elif df_tw.empty:
            st.info("Not enough data yet — need ≥ 5 trades per (day × hour) window.")
        else:
            def _ev_color_tw(val):
                if not isinstance(val, (int, float)):
                    return ""
                return "color: #00c851" if val > 0 else "color: #ff4444"

            styled_tw = (
                df_tw.style
                .map(_ev_color_tw, subset=["ev"])
                .format({
                    "win_rate":     "{:.1f}%",
                    "avg_win_pct":  "{:+.2f}%",
                    "avg_loss_pct": "{:.2f}%",
                    "ev":           "{:+.3f}%",
                }, na_rep="—")
            )
            st.dataframe(styled_tw, width="stretch", hide_index=True)

        st.markdown("---")

        # ── Average winner vs loser size ────────────────────────────────────────
        st.subheader("Average Winner vs Loser Size")
        st.caption(
            "Avg win % vs avg loss % per strategy — ratio > 1 means wins are larger than losses. "
            "Requires ≥ 3 closed trades."
        )
        df_wl = fetch_performance_by_dimension("signal_type")

        if df_wl is not None and not df_wl.empty:
            df_wl = df_wl.copy()
            df_wl = df_wl[df_wl["avg_win_pct"].notna() & df_wl["avg_loss_pct"].notna()]

            if not df_wl.empty:
                fig_wl = go.Figure()
                fig_wl.add_trace(go.Bar(
                    name="Avg Win",
                    x=df_wl["dim_value"].astype(str),
                    y=df_wl["avg_win_pct"],
                    marker_color="#00c851",
                    hovertemplate="%{x}<br>Avg Win: +%{y:.2f}%<extra></extra>",
                ))
                fig_wl.add_trace(go.Bar(
                    name="Avg Loss",
                    x=df_wl["dim_value"].astype(str),
                    y=df_wl["avg_loss_pct"],
                    marker_color="#ff4444",
                    hovertemplate="%{x}<br>Avg Loss: -%{y:.2f}%<extra></extra>",
                ))
                _layout_wl = {k: v for k, v in _CHART_LAYOUT.items() if k != "showlegend"}
                fig_wl.update_layout(
                    height=max(300, 70 * len(df_wl)),
                    barmode="group",
                    showlegend=True,
                    legend=dict(font=dict(color="#7d8590")),
                    **_layout_wl,
                )
                fig_wl.update_layout(xaxis_title="Strategy", yaxis_title="Return (%)")
                st.plotly_chart(fig_wl, width="stretch")

                df_wl["ratio"] = (
                    df_wl["avg_win_pct"] / df_wl["avg_loss_pct"].replace(0, float("nan"))
                ).round(2)

                def _ratio_color(val):
                    if not isinstance(val, (int, float)):
                        return ""
                    return "color: #00c851" if val > 1.0 else "color: #ff4444"

                styled_wl = (
                    df_wl[["dim_value", "trade_count", "avg_win_pct", "avg_loss_pct", "ratio"]]
                    .rename(columns={"dim_value": "strategy"})
                    .style
                    .map(_ratio_color, subset=["ratio"])
                    .format({
                        "avg_win_pct":  "{:+.2f}%",
                        "avg_loss_pct": "{:.2f}%",
                        "ratio":        "{:.2f}x",
                    }, na_rep="—")
                )
                st.dataframe(styled_wl, width="stretch", hide_index=True)
            else:
                st.info("No win/loss data yet — needs at least one winning and one losing trade.")
        else:
            st.info("No closed trade data yet.")

# ── Tab 7: Signal Intel ────────────────────────────────────────────────────────

with tab_intel:
    st.header("Signal Intelligence")

    # ── Section 1: Recent Trade Signals ──────────────────────────────────────

    st.subheader("Recent Trade Signals")
    df_sig = fetch_recent_signals(20)

    if df_sig is not None and not df_sig.empty:
        live_state = fetch_live_signal_state()

        df_sig = df_sig.copy()
        df_sig["Kalman Slope"]  = df_sig["symbol"].map(
            lambda s: live_state.get(s, {}).get("kalman_slope")
        )
        df_sig["Hurst H"]       = df_sig["symbol"].map(
            lambda s: live_state.get(s, {}).get("hurst_h")
        )
        df_sig["VWAP Dist%"]    = df_sig["symbol"].map(
            lambda s: live_state.get(s, {}).get("vwap_dist_pct")
        )
        df_sig["Status"] = df_sig["pnl_pct"].apply(
            lambda v: "OPEN" if pd.isna(v) else ("WIN" if float(v) > 0 else "LOSS")
        )

        def _sig_row_style(row):
            status = row.get("Status", "")
            if status == "WIN":
                bg = "background-color: #1a3a1a"
            elif status == "LOSS":
                bg = "background-color: #3a1a1a"
            elif status == "OPEN":
                bg = "background-color: #2a2a1a"
            else:
                bg = ""
            return [bg] * len(row)

        cols_show = [
            "symbol", "signal_type", "entry_time", "entry_price",
            "market_regime", "Kalman Slope", "Hurst H", "VWAP Dist%",
            "pnl_pct", "exit_reason", "Status",
        ]
        df_display = df_sig[[c for c in cols_show if c in df_sig.columns]]
        styled_sig = (
            df_display.style
            .apply(_sig_row_style, axis=1)
            .format({"entry_price": "{:.2f}", "pnl_pct": "{:+.2f}%",
                     "Kalman Slope": "{:.4f}", "Hurst H": "{:.3f}",
                     "VWAP Dist%": "{:+.3f}%"}, na_rep="—")
        )
        st.dataframe(styled_sig, width="stretch", hide_index=True)
        st.caption(
            "Kalman/Hurst/VWAP values reflect current live state (60-day bars), "
            "not at-signal-time values — those are not stored in the DB."
        )
    else:
        st.info("No trade signals in signal_outcomes yet.")

    st.divider()

    # ── Section 2: Gate Chain Health ─────────────────────────────────────────

    st.subheader("Gate Chain Health")
    df_gate = fetch_gate_chain_health()

    if df_gate is not None and not df_gate.empty:
        def _cb_color(val):
            if val == "ACTIVE":
                return "color: #ff4444; font-weight: bold"
            if val == "CLEAR":
                return "color: #00c851"
            return ""

        def _hurst_regime_color(val):
            v = str(val).lower()
            if "trending" in v:
                return "color: #00c851"
            if "mean" in v or "revert" in v:
                return "color: #7d8590"
            return ""

        styled_gate = (
            df_gate.style
            .map(_cb_color, subset=["CB"])
            .map(_hurst_regime_color, subset=["Hurst Regime"])
            .format({
                "Hurst H":      "{:.3f}",
                "Kalman Noise": "{:.4f}",
                "VWAP Dist%":   "{:+.3f}%",
                "Half-Kelly f": "{:.4f}",
            }, na_rep="—")
        )
        st.dataframe(styled_gate, width="stretch", hide_index=True)
        st.caption(
            "Half-Kelly f requires >= 10 closed trades per symbol to compute. "
            "CB = strategy_circuit_breakers table. Live indicators from 60-day daily bars."
        )
    else:
        st.info("Gate chain health unavailable — check DB connection.")

    st.divider()

    # ── Section 3: Correlation Matrix ─────────────────────────────────────────

    st.subheader("Swing Symbol Correlation (60-day)")
    df_corr = fetch_swing_correlation()

    if df_corr is not None and not df_corr.empty:
        def _corr_cell_style(val):
            try:
                r = float(val)
            except (TypeError, ValueError):
                return ""
            if abs(r) >= 0.999:
                return "background-color: #1a1a2e; color: #7d8590"
            if r >= 0.75:
                return "background-color: #3a1a1a; color: #ff4444"
            if r >= 0.50:
                return "background-color: #3a2a1a; color: #ffbb33"
            if r >= 0.25:
                return "background-color: #1e2a1e; color: #aaaaaa"
            return "background-color: #1a1a1a; color: #555555"

        styled_corr = (
            df_corr.style
            .map(_corr_cell_style)
            .format("{:.2f}")
        )
        st.dataframe(styled_corr, width="stretch")
        st.markdown(
            '<span style="color:#ff4444">&#9632;</span> r &gt;= 0.75 — CorrelationGuard blocks trade &nbsp;|&nbsp; '
            '<span style="color:#ffbb33">&#9632;</span> r &gt;= 0.50 — elevated correlation &nbsp;|&nbsp; '
            '<span style="color:#aaaaaa">&#9632;</span> r &gt;= 0.25 — moderate &nbsp;|&nbsp; '
            '<span style="color:#555555">&#9632;</span> low / diagonal',
            unsafe_allow_html=True,
        )
    else:
        st.info("Correlation data unavailable — needs Alpaca bars for at least 2 symbols.")


# ── Tab 8: Strategy Decay ──────────────────────────────────────────────────────

with tab_decay:
    st.header("🩺 Strategy Decay Monitor")
    if not _db_available():
        st.warning("DATABASE_URL is not configured — decay status unavailable.")
    else:
        df_decay = fetch_decay_status()
        if df_decay is None or df_decay.empty:
            st.info(
                "No decay status yet. The decay monitor activates once a strategy/symbol "
                "has at least 30 closed signals in signal_outcomes."
            )
        else:
            counts = df_decay["status"].value_counts().to_dict()
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("🟢 Healthy",  counts.get("HEALTHY", 0))
            c2.metric("🟡 Degraded", counts.get("DEGRADED", 0))
            c3.metric("🟠 Decaying", counts.get("DECAYING", 0))
            c4.metric("🔴 Critical", counts.get("CRITICAL", 0))

            _decay_row_colors = {
                "CRITICAL": "background-color: #5c1a1a",
                "DECAYING": "background-color: #5c3a1a",
                "DEGRADED": "background-color: #5c551a",
                "HEALTHY":  "background-color: #1a5c2a",
            }

            def _decay_row_style(row):
                return [_decay_row_colors.get(row["status"], "")] * len(row)

            show_cols = [
                "symbol", "strategy_name", "decay_ratio", "status", "position_multiplier",
                "consecutive_signals_below", "re_validation_requested", "disabled", "last_checked",
            ]
            show_cols = [c for c in show_cols if c in df_decay.columns]
            styled_decay = (
                df_decay[show_cols].style
                .apply(_decay_row_style, axis=1)
                .format({"decay_ratio": "{:.2f}", "position_multiplier": "{:.2f}"}, na_rep="—")
            )
            st.dataframe(styled_decay, width="stretch", hide_index=True)
            st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # ── Manual override controls ────────────────────────────────────────
            st.markdown("---")
            st.subheader("Manual Override")
            _opts = [f"{r.symbol} / {r.strategy_name}" for r in df_decay.itertuples()]
            sel = st.selectbox("Select strategy/symbol", _opts, key="decay_sel")
            if sel:
                sel_symbol, sel_strategy = [s.strip() for s in sel.split("/", 1)]
                b1, b2, b3 = st.columns(3)
                if b1.button("🔄 Re-validate now", key="decay_reval"):
                    if _decay_action(sel_strategy, sel_symbol, "re-validate"):
                        st.success(f"Re-validation queued for {sel_symbol} {sel_strategy}")
                        st.cache_data.clear()
                if b2.button("🛑 Disable strategy", key="decay_disable"):
                    if _decay_action(sel_strategy, sel_symbol, "disable"):
                        st.success(f"Disabled {sel_symbol} {sel_strategy}")
                        st.cache_data.clear()
                if b3.button("✅ Reset to healthy", key="decay_reset"):
                    if _decay_action(sel_strategy, sel_symbol, "reset"):
                        st.success(f"Reset {sel_symbol} {sel_strategy} to HEALTHY")
                        st.cache_data.clear()

            st.caption(
                "Tiers: HEALTHY ≥0.8 (1.0×) · DEGRADED 0.5–0.8 (0.5×) · "
                "DECAYING <0.5 (0.25× + re-validation) · CRITICAL negative live Sharpe "
                "(disabled + positions closed + PagerDuty)."
            )
