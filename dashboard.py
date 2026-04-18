import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta
import os
from config import Config

# Set Page Config
st.set_page_config(page_title="Hybrid Bot Dashboard", layout="wide")

# Authentication
API_KEY = Config.ALPACA_API_KEY
SECRET_KEY = Config.ALPACA_SECRET_KEY
PAPER = Config.PAPER_TRADING

if not API_KEY or not SECRET_KEY:
    st.error("🔑 API Keys Missing! Please add ALPACA_API_KEY and ALPACA_SECRET_KEY to your Railway Variables.")
    st.stop()

# Initialize Clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

st.title("🚀 Hybrid Trading Bot Dashboard")
st.sidebar.header("Account Settings")
st.sidebar.write(f"Account: {'Paper' if PAPER else 'Live'}")

# --- 1. Account Metrics ---
st.subheader("💰 Account Overview")
try:
    account = trading_client.get_account()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Equity", f"${float(account.equity):,.2f}")
    col2.metric("Buying Power", f"${float(account.buying_power):,.2f}")
    col3.metric("Cash", f"${float(account.cash):,.2f}")
    col4.metric("Status", account.status)
except Exception as e:
    st.error(f"Error fetching account data: {e}")

# --- 2. Open Positions ---
st.subheader("📈 Current Positions")
try:
    positions = trading_client.get_all_positions()
    if positions:
        pos_data = []
        for p in positions:
            pos_data.append({
                "Symbol": p.symbol,
                "Qty": p.qty,
                "Entry Price": f"${float(p.avg_entry_price):,.2f}",
                "Current Price": f"${float(p.current_price):,.2f}",
                "P&L (%)": f"{float(p.unrealized_plpc)*100:.2f}%",
                "Market Value": f"${float(p.market_value):,.2f}"
            })
        st.table(pd.DataFrame(pos_data))
    else:
        st.info("No open positions at the moment.")
except Exception as e:
    st.error(f"Error fetching positions: {e}")

# --- 3. Recent Orders ---
st.subheader("📜 Recent Activity")
try:
    orders = trading_client.get_orders(filter=None)
    if orders:
        order_list = []
        for o in orders[:10]: # Last 10 orders
            order_list.append({
                "Created At": o.created_at.strftime("%Y-%m-%d %H:%M"),
                "Symbol": o.symbol,
                "Side": o.side,
                "Status": o.status,
                "Qty": o.qty,
                "Type": o.order_type
            })
        st.dataframe(pd.DataFrame(order_list))
    else:
        st.info("No recent orders found.")
except Exception as e:
    st.error(f"Error fetching orders: {e}")

st.sidebar.markdown("---")
st.sidebar.write("Bot is running 24/7 on Railway")
if st.sidebar.button("Refresh Data"):
    st.rerun()
