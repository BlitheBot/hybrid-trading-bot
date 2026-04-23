import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy
from utils import get_spy_data

class SMBStrategy(BaseStrategy):
    def __init__(self, name, ema_window=9, rr_ratio=3):
        super().__init__(name)
        self.ema_window = ema_window
        self.rr_ratio = rr_ratio

    def calculate_vwap(self, df):
        df_filtered = df[df["volume"] > 0]
        if df_filtered.empty:
            return pd.Series(np.nan, index=df.index)
        v = df_filtered["volume"].values
        tp = (df_filtered["low"] + df_filtered["high"] + df_filtered["close"]).values / 3
        vwap_series = pd.Series((tp * v).cumsum() / v.cumsum(), index=df_filtered.index)
        return vwap_series.reindex(df.index, method='ffill')

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
        if market_data is None or len(market_data) < self.ema_window + 1:
            return None
        df = market_data.copy()
        df["EMA_9"] = df["close"].ewm(span=self.ema_window, adjust=False).mean()
        df["VWAP"] = self.calculate_vwap(df)
        if df["EMA_9"].isnull().any() or df["VWAP"].isnull().any():
            return None
        symbol = str(df["symbol"].iloc[-1]) if "symbol" in df.columns else "UNKNOWN"NOWN"
        is_crypto = "/" in symbol or symbol in ["BTCUSD", "ETHUSD"]
        if not is_crypto:
            spy_data = get_spy_data(stock_data_client, days_back=len(df))
            relative_strength = self.calculate_relative_strength(df, spy_data)
            if relative_strength is None or relative_strength <= 0:
                return None
        curr_ema, prev_ema = df["EMA_9"].iloc[-1], df["EMA_9"].iloc[-2]
        curr_vwap, prev_vwap = df["VWAP"].iloc[-1], df["VWAP"].iloc[-2]
        current_price = df["close"].iloc[-1]
        lod, hod = df["low"].min(), df["high"].max()
        signal = None
        if curr_ema > curr_vwap and prev_ema <= prev_vwap and curr_ema > prev_ema:
            signal = "buy"
        elif curr_ema < curr_vwap and prev_ema >= prev_vwap and curr_ema < prev_ema:
            signal = "sell"
        if signal:
            if signal == "buy":
                distance = current_price - lod
                stop_price = current_price - (distance / self.rr_ratio)
                target_price = current_price + distance
            else:
                distance = hod - current_price
                stop_price = current_price + (distance / self.rr_ratio)
                target_price = current_price - distance
            return {
                "symbol": symbol, "signal": signal, "confidence": 0.8,
                "entry_price": current_price, "stop_price": stop_price, "target_price": target_price
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
