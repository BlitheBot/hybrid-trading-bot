import os
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import pytz
import requests as _requests
import streamlit as st

# Remove conflicting environment tokens before importing Alpaca
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

from alpaca.trading.client import TradingClient

from config import Config

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Hybrid Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.title("📈 Hybrid Trading Bot")
mode_label = "🟡 Paper Trading" if Config.PAPER_TRADING else "🔴 Live Trading"
st.sidebar.markdown(f"**Mode:** {mode_label}")
st.sidebar.markdown("---")

if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

now_est = datetime.now(pytz.timezone("America/New_York"))
st.sidebar.caption(f"Data cached 60s. Refreshed {now_est.strftime('%I:%M:%S %p EST')}")

st.sidebar.markdown("---")

# Bot health — pings Flask /health on port 8502 (internal, same Railway instance)
@st.cache_data(ttl=30)
def _bot_health():
    try:
        r = _requests.get("http://localhost:8502/health", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

_health = _bot_health()
if _health:
    uptime_s = int(_health.get("uptime_seconds", 0))
    h, rem = divmod(uptime_s, 3600)
    m, s   = divmod(rem, 60)
    st.sidebar.success(f"🟢 Bot online — {h}h {m}m {s}s uptime")
else:
    st.sidebar.error("🔴 Bot offline or unreachable")

st.sidebar.markdown("---")
st.sidebar.markdown("**Swing Watchlist**")
for sym in Config.SWING_SYMBOLS:
    st.sidebar.markdown(f"  - {sym}")
st.sidebar.markdown("**Crypto Scalp**")
for sym in Config.SCALP_SYMBOLS:
    st.sidebar.markdown(f"  - {sym}")

# ── Alpaca client ──────────────────────────────────────────────────────────────

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


trading_client = _get_trading_client()
if not trading_client:
    st.error("🔑 ALPACA_API_KEY / ALPACA_SECRET_KEY missing from environment.")
    st.stop()

# ── DB helper (new connection per query — avoids stale cached connections) ─────

def _db_conn():
    url = Config.DATABASE_URL
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        st.warning(f"[DB] Connection failed: {e}")
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

# ── Data fetchers ──────────────────────────────────────────────────────────────

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
    conn = _db_conn()
    if conn is None:
        return None
    try:
        query = """
            SELECT id, symbol, signal_type,
                   entry_time AT TIME ZONE 'America/New_York' AS entry_time,
                   exit_time  AT TIME ZONE 'America/New_York' AS exit_time,
                   entry_price, exit_price, pnl_pct, hold_bars,
                   ema_short, ema_long, rsi_at_entry, macd_at_entry,
                   market_regime, exit_reason
            FROM signal_outcomes
            WHERE entry_time >= %s AND entry_time <= %s
        """
        params: list = [start_dt, end_dt]
        if symbols:
            query += " AND symbol = ANY(%s)"
            params.append(list(symbols))
        query += " ORDER BY entry_time DESC"
        return pd.read_sql(query, conn, params=params)
    except Exception as e:
        st.error(f"Trade log query failed: {e}")
        return None
    finally:
        conn.close()


@st.cache_data(ttl=300)
def fetch_strategy_results(validated_only: bool):
    conn = _db_conn()
    if conn is None:
        return None
    try:
        where = "WHERE status = 'validated'" if validated_only else ""
        query = f"""
            SELECT symbol, ema_short, ema_long, rsi_period,
                   rsi_entry_low, rsi_entry_high,
                   ROUND(train_sharpe::numeric, 3)  AS train_sharpe,
                   ROUND(test_sharpe::numeric,  3)  AS test_sharpe,
                   ROUND(degradation::numeric,  3)  AS degradation,
                   ROUND(p_value::numeric,      4)  AS p_value,
                   total_test_trades, status,
                   discovered_at::date              AS date
            FROM strategy_results
            {where}
            ORDER BY test_sharpe DESC NULLS LAST
        """
        return pd.read_sql(query, conn)
    except Exception as e:
        st.error(f"Discovery query failed: {e}")
        return None
    finally:
        conn.close()


@st.cache_data(ttl=60)
def fetch_daily_pnl():
    conn = _db_conn()
    if conn is None:
        return None
    try:
        query = """
            SELECT DATE(exit_time AT TIME ZONE 'America/New_York') AS trade_date,
                   COUNT(*)        AS trades,
                   SUM(pnl_pct)    AS daily_pnl_sum
            FROM signal_outcomes
            WHERE exit_time IS NOT NULL AND pnl_pct IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """
        return pd.read_sql(query, conn)
    except Exception as e:
        st.error(f"Daily P&L query failed: {e}")
        return None
    finally:
        conn.close()


@st.cache_data(ttl=60)
def fetch_win_rate():
    conn = _db_conn()
    if conn is None:
        return None
    try:
        query = """
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
        return pd.read_sql(query, conn)
    except Exception as e:
        st.error(f"Win rate query failed: {e}")
        return None
    finally:
        conn.close()

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

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Equity",       f"${equity:,.2f}")
        c2.metric("Buying Power", f"${buying_power:,.2f}")
        c3.metric("Cash",         f"${cash:,.2f}")
        c4.metric("Mode",         "Paper" if Config.PAPER_TRADING else "Live")
        c5.metric("Status",       str(account.status).upper())

        positions = fetch_positions()
        if positions:
            total_mv   = sum(float(p.market_value) for p in positions)
            total_unrl = sum(float(p.unrealized_pl) for p in positions)
            st.markdown("---")
            d1, d2, d3 = st.columns(3)
            d1.metric("Open Positions",     len(positions))
            d2.metric("Total Market Value", f"${total_mv:,.2f}")
            d3.metric(
                "Total Unrealized P&L",
                f"${total_unrl:+,.2f}",
                delta=f"${total_unrl:+,.2f}",
                delta_color="normal",
            )

        st.markdown("---")
        st.markdown("#### Risk Config")
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Equity Risk / Trade", f"{Config.SWING_EQUITY_RISK_PERCENT}%")
        r2.metric("Stop Loss",           f"{Config.STOP_LOSS_PERCENT}%")
        r3.metric("Take Profit",         f"{Config.TAKE_PROFIT_PERCENT}%")
        r4.metric("Max Daily Loss",      f"{Config.MAX_DAILY_LOSS_PERCENT}%")

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

        # Totals row
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
            .applymap(_pnl_color, subset=["Unrealized P&L %", "Unrealized P&L $"])
            .format({
                "Entry Price":      "${:.2f}",
                "Current Price":    "${:.2f}",
                "Market Value":     "${:,.2f}",
                "Unrealized P&L $": "${:+,.2f}",
                "Unrealized P&L %": "{:+.2f}%",
            }, na_rep="")
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

# ── Tab 3: Trade Log ───────────────────────────────────────────────────────────

with tab_tradelog:
    st.header("Trade Log")

    if not _db_available():
        st.info(
            "DATABASE_URL is not configured. "
            "Add a PostgreSQL database in Railway to see trade history."
        )
    else:
        # Filters
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

        # Guard against single-date selection
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
                .applymap(_pnl_color, subset=["pnl_pct"])
                .format({
                    "entry_price": "${:.2f}",
                    "exit_price":  "${:.2f}",
                    "pnl_pct":     "{:+.2f}%",
                }, na_rep="—")
            )
            st.dataframe(styled_tl, use_container_width=True, hide_index=True)

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
            "Run `python -m discovery.discovery_engine` and connect PostgreSQL to see results."
        )
    else:
        validated_only = st.toggle("Validated only", value=True, key="disc_toggle")
        df_disc = fetch_strategy_results(validated_only=validated_only)

        if df_disc is None:
            st.warning("Could not load discovery data.")
        elif df_disc.empty:
            st.info(
                "No results yet. Run:\n```\npython -m discovery.discovery_engine\n```"
            )
        else:
            # Per-symbol summary
            st.subheader("Summary by Symbol")
            summary = (
                df_disc.groupby("symbol")
                .agg(
                    validated=("status", lambda x: (x == "validated").sum()),
                    total=("status", "count"),
                    best_test_sharpe=("test_sharpe", "max"),
                    avg_test_sharpe=("test_sharpe", "mean"),
                )
                .reset_index()
            )
            summary["validated %"] = (summary["validated"] / summary["total"] * 100).round(1)
            st.dataframe(
                summary.style.format({
                    "best_test_sharpe": "{:.3f}",
                    "avg_test_sharpe":  "{:.3f}",
                    "validated %":      "{:.1f}%",
                }),
                use_container_width=True,
                hide_index=True,
            )

            st.subheader("All Combos — Ranked by Test Sharpe")
            styled_disc = (
                df_disc.style
                .applymap(_status_color, subset=["status"])
                .applymap(_pnl_color,    subset=["test_sharpe", "train_sharpe"])
                .format({
                    "train_sharpe": "{:.3f}",
                    "test_sharpe":  "{:.3f}",
                    "degradation":  "{:.3f}",
                    "p_value":      "{:.4f}",
                }, na_rep="—")
            )
            st.dataframe(styled_disc, use_container_width=True, hide_index=True)

# ── Tab 5: Analytics ───────────────────────────────────────────────────────────

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

            positive = df_pnl["cumulative_pnl"].iloc[-1] >= 0
            line_color = "#00c851" if positive else "#ff4444"
            fill_color = "rgba(0,200,81,0.12)" if positive else "rgba(255,68,68,0.12)"

            fig_pnl = go.Figure()
            fig_pnl.add_trace(go.Scatter(
                x=df_pnl["trade_date"],
                y=df_pnl["cumulative_pnl"],
                mode="lines+markers",
                name="Cumulative P&L %",
                line=dict(color=line_color, width=2),
                fill="tozeroy",
                fillcolor=fill_color,
                hovertemplate="%{x}<br>Cumulative: %{y:+.2f}%<extra></extra>",
            ))
            fig_pnl.add_hline(y=0, line_dash="dash", line_color="rgba(128,128,128,0.4)")
            fig_pnl.update_layout(
                xaxis_title="Date",
                yaxis_title="Cumulative P&L (%)",
                hovermode="x unified",
                height=400,
                margin=dict(l=0, r=0, t=10, b=0),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_pnl, use_container_width=True)
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
            colors = [
                "#00c851" if w >= 50 else "#ff4444"
                for w in df_wr["win_rate_pct"]
            ]
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
                hovertemplate="%{x}<br>Win rate: %{y:.1f}%<extra></extra>",
            ))
            fig_wr.add_hline(
                y=50,
                line_dash="dash",
                line_color="rgba(128,128,128,0.5)",
                annotation_text="50% breakeven",
                annotation_position="right",
            )
            fig_wr.update_layout(
                xaxis_title="Symbol",
                yaxis_title="Win Rate (%)",
                yaxis_range=[0, 115],
                height=380,
                margin=dict(l=0, r=0, t=10, b=0),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                showlegend=False,
            )
            st.plotly_chart(fig_wr, use_container_width=True)

            # Detail table beneath the chart
            styled_wr = (
                df_wr.style
                .applymap(_pnl_color, subset=["avg_pnl_pct"])
                .format({
                    "win_rate_pct": "{:.1f}%",
                    "avg_pnl_pct":  "{:+.2f}%",
                })
            )
            st.dataframe(styled_wr, use_container_width=True, hide_index=True)
        else:
            st.info("No closed trades yet — win rate will populate once positions close.")
