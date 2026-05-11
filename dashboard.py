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
            SELECT symbol, strategy_type,
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
                SELECT symbol,
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

tab_account, tab_positions, tab_tradelog, tab_discovery, tab_analytics = st.tabs([
    "💰 Account",
    "📊 Positions",
    "📋 Trade Log",
    "🔬 Discovery",
    "📈 Analytics",
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
                    f"Approve via: `UPDATE discovery_results SET status='approved' WHERE id=<id>;`"
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
                use_container_width=True,
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
            st.dataframe(styled_disc, use_container_width=True, hide_index=True)

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
