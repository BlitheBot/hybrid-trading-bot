import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy
from utils import get_spy_data

import pandas_ta as ta

class SMBStrategy(BaseStrategy):
    def __init__(self, name, ema_window=9, rr_ratio=3):
        super().__init__(name)
        self.ema_window = ema_window
        self.rr_ratio = rr_ratio

    def calculate_relative_strength(self, stock_data, spy_data):
        if stock_data is None or spy_data is None or len(stock_data) < 2 or len(spy_data) < 2:
            return 0.0
        common_dates = stock_data.index.intersection(spy_data.index)
        if common_dates.empty:
            return 0.0
        stock_close = stock_data["close"].loc[common_dates]
        spy_close = spy_data["close"].loc[common_dates]
        stock_performance = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
        spy_performance = (spy_close.iloc[-1] / spy_close.iloc[0]) - 1
        return stock_performance - spy_performance

    def generate_signals(self, market_data, stock_data_client):
        if market_data is None or len(market_data) < self.ema_window + 14:
            return None
        df = market_data.copy()
        
        # Replace manual calculations with pandas-ta
        df["EMA_9"] = ta.ema(df["close"], length=self.ema_window)
        df["VWAP"] = ta.vwap(high=df["high"], low=df["low"], close=df["close"], volume=df["volume"])
        df["ATR"] = ta.atr(high=df["high"], low=df["low"], close=df["close"], length=14)
        
        # Only check the last 2 rows — early rows naturally have NaN from rolling calculations
        if df[["EMA_9", "VWAP", "ATR"]].iloc[-2:].isnull().any().any():
            return None
            
        symbol = str(df["symbol"].iloc[-1]) if "symbol" in df.columns else "UNKNOWN"
        is_crypto = "/" in symbol or symbol in ["BTCUSD", "ETHUSD"]
        if not is_crypto:
            spy_data = get_spy_data(stock_data_client, days_back=len(df))
            relative_strength = self.calculate_relative_strength(df, spy_data)
            if relative_strength is None or relative_strength <= 0:
                return None
                
        curr_ema, prev_ema = df["EMA_9"].iloc[-1], df["EMA_9"].iloc[-2]
        curr_vwap, prev_vwap = df["VWAP"].iloc[-1], df["VWAP"].iloc[-2]
        current_price = df["close"].iloc[-1]
        atr = df["ATR"].iloc[-1]
        
        signal = None
        if curr_ema > curr_vwap and prev_ema <= prev_vwap and curr_ema > prev_ema:
            signal = "buy"
        elif curr_ema < curr_vwap and prev_ema >= prev_vwap and curr_ema < prev_ema:
            signal = "sell"
            
        if signal and not np.isnan(atr):
            if signal == "buy":
                distance = atr * 1.5 # Use 1.5x ATR for stop distance
                stop_price = current_price - distance
                target_price = current_price + (distance * self.rr_ratio)
            else:
                distance = atr * 1.5
                stop_price = current_price + distance
                target_price = current_price - (distance * self.rr_ratio)
            return {
                "symbol": symbol, "signal": signal, "confidence": 0.8,
                "entry_price": current_price, "stop_price": stop_price, "target_price": target_price,
                "reasoning": f"VWAP Crossover. ATR(14): {atr:.4f}"
            }
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        if not signal:
            return
        symbol = signal["symbol"]
        
        # 1. SAFETY CHECK: Are we already in this position?
        if self.is_already_in_position(symbol, trading_client):
            return

        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL
        entry_price, stop_price, target_price = signal["entry_price"], signal["stop_price"], signal["target_price"]

        account = trading_client.get_account()
        if not account:
            return
            
        # 2. SAFETY LOCK: Calculate Safe Quantity
        qty = self.calculate_safe_quantity(
            symbol, entry_price, stop_price, account, 
            equity_risk_percent, max_buying_power_utilization_percent
        )
        
        if qty <= 0:
            return

        order_data = MarketOrderRequest(
            symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC,
            take_profit=TakeProfitRequest(limit_price=target_price),
            stop_loss=StopLossRequest(stop_price=stop_price)
        )
        
        try:
            trading_client.submit_order(order_data=order_data)
            print(f"✅ SMB Order Placed: {side} {symbol} (Qty: {qty}) @ {entry_price}. Target: {target_price}, Stop: {stop_price}")
        except Exception as e:
            print(f"❌ SMB Order Failed for {symbol}: {e}")
