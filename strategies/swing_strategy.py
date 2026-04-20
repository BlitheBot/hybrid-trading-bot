import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy
from config import Config

class SwingStrategy(BaseStrategy):
    def __init__(self, name, ema_short=50, ema_long=200, macd_fast=12, macd_slow=26, macd_signal=9):
        super().__init__(name)
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal

    def calculate_macd(self, df):
        exp1 = df['close'].ewm(span=self.macd_fast, adjust=False).mean()
        exp2 = df['close'].ewm(span=self.macd_slow, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=self.macd_signal, adjust=False).mean()
        return macd, signal

    def calculate_rsi(self, df, window=14):
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def generate_signals(self, market_data):
        if market_data is None or len(market_data) < self.ema_long + self.macd_slow + self.macd_signal + 14:
            return None

        df = market_data.copy()

        # Calculate EMAs
        df['EMA_short'] = df['close'].ewm(span=self.ema_short, adjust=False).mean()
        df['EMA_long'] = df['close'].ewm(span=self.ema_long, adjust=False).mean()

        # Calculate MACD
        df['MACD'], df['MACD_Signal'] = self.calculate_macd(df)

        # Calculate RSI
        df['RSI'] = self.calculate_rsi(df)

        # Ensure we have enough data for calculations
        if df['EMA_short'].isnull().any() or df['EMA_long'].isnull().any() or \
           df['MACD'].isnull().any() or df['MACD_Signal'].isnull().any() or df['RSI'].isnull().any():
            return None

        last_ema_short = df['EMA_short'].iloc[-1]
        last_ema_long = df['EMA_long'].iloc[-1]
        last_macd = df['MACD'].iloc[-1]
        last_macd_signal = df['MACD_Signal'].iloc[-1]
        last_rsi = df['RSI'].iloc[-1]
        current_price = df['close'].iloc[-1]

        signal = None
        confidence = 0.0

        # Entry conditions: 50 EMA > 200 EMA + MACD crossover + RSI between 40-60
        if last_ema_short > last_ema_long and \
           last_macd > last_macd_signal and df['MACD'].iloc[-2] <= df['MACD_Signal'].iloc[-2] and \
           40 <= last_rsi <= 60:
            signal = "buy"
            confidence = 0.7

        # Exit conditions (for existing positions, not new short signals)
        # RSI above 70 or MACD reversal
        elif (last_rsi > 70) or \
             (last_macd < last_macd_signal and df['MACD'].iloc[-2] >= df['MACD_Signal'].iloc[-2]):
            # This is an exit signal for an existing long position
            # For now, we'll just return None for new trades, and handle exits in bot.py
            pass

        if signal == "buy":
            # Use general risk parameters for swing trades
            stop_loss_price = current_price * (1 - (Config.STOP_LOSS_PERCENT / 100))
            take_profit_price = current_price * (1 + (Config.TAKE_PROFIT_PERCENT / 100))

            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": confidence,
                "entry_price": current_price,
                "stop_price": stop_loss_price,
                "target_price": take_profit_price
            }
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        if not signal:
            return

        symbol = signal["symbol"]
        
        # 1. SAFETY CHECK: Are we already in this position?
        if self.is_already_in_position(symbol, trading_client):
            print(f"Skipping {symbol} - already in position or order pending.")
            return

        entry_price = signal["entry_price"]
        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL

        account = trading_client.get_account()
        if not account:
            print("Could not retrieve account details for trade execution.")
            return

        # 2. SAFETY LOCK: Calculate Safe Quantity
        qty = self.calculate_safe_quantity(
            symbol, entry_price, signal["stop_price"], account, 
            equity_risk_percent, max_buying_power_utilization_percent
        )

        if qty <= 0:
            print(f"Calculated quantity for {symbol} is zero or negative. Not placing order.")
            return

        # 3. Place Order
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC,
            take_profit=TakeProfitRequest(limit_price=signal["target_price"]),
            stop_loss=StopLossRequest(stop_price=signal["stop_price"])
        )
        
        try:
            trading_client.submit_order(order_data=order_data)
            print(f"✅ Swing Order Placed: {side} {symbol} (Qty: {qty}) @ {entry_price}. SL: {signal["stop_price"]}, TP: {signal["target_price"]}")
        except Exception as e:
            print(f"❌ Swing Order Failed for {symbol}: {e}")
