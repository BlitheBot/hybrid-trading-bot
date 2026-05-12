import csv
import io
import os
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

# ── Tabs ───────────────────────────────────────────────────────────────────────

tab_account, tab_positions, tab_tradelog, tab_discovery, tab_analytics, tab_perf = st.tabs([
    "💰 Account",
    "📊 Positions",
    "📋 Trade Log",
    "🔬 Discovery",
    "📈 Analytics",
    "🧠 Performance",
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
