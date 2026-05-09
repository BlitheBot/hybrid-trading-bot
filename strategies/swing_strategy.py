import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy
from config import Config

import pandas_ta as ta

class SwingStrategy(BaseStrategy):
    def __init__(self, name, ema_short=50, ema_long=200, macd_fast=12, macd_slow=26, macd_signal=9,
                 rsi_entry_low=40, rsi_entry_high=60):
        super().__init__(name)
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.rsi_entry_low = rsi_entry_low
        self.rsi_entry_high = rsi_entry_high

    def generate_signals(self, market_data):
        if market_data is None or len(market_data) < self.ema_long + self.macd_slow + self.macd_signal + 14:
            return None

        df = market_data.copy()

        # Calculate EMAs with pandas-ta
        df['EMA_short'] = ta.ema(df['close'], length=self.ema_short)
        df['EMA_long'] = ta.ema(df['close'], length=self.ema_long)

        # Calculate MACD with pandas-ta
        macd_df = ta.macd(df['close'], fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        if macd_df is not None and not macd_df.empty:
            df['MACD'] = macd_df.iloc[:, 0]
            df['MACD_Signal'] = macd_df.iloc[:, 2]
        else:
            df['MACD'] = np.nan
            df['MACD_Signal'] = np.nan

        # Calculate RSI with pandas-ta
        df['RSI'] = ta.rsi(df['close'], length=14)

        # Check only the last row — early rows always have NaN from rolling warmup
        if (pd.isna(df['EMA_short'].iloc[-1]) or pd.isna(df['EMA_long'].iloc[-1]) or
                pd.isna(df['MACD'].iloc[-1]) or pd.isna(df['MACD_Signal'].iloc[-1]) or
                pd.isna(df['RSI'].iloc[-1])):
            return None

        last_ema_short = df['EMA_short'].iloc[-1]
        last_ema_long = df['EMA_long'].iloc[-1]
        last_macd = df['MACD'].iloc[-1]
        last_macd_signal = df['MACD_Signal'].iloc[-1]
        last_rsi = df['RSI'].iloc[-1]
        current_price = df['close'].iloc[-1]

        signal = None
        confidence = 0.0

        # Entry conditions: EMA_short > EMA_long + MACD crossover + RSI in configured range
        if last_ema_short > last_ema_long and \
           last_macd > last_macd_signal and df['MACD'].iloc[-2] <= df['MACD_Signal'].iloc[-2] and \
           self.rsi_entry_low <= last_rsi <= self.rsi_entry_high:
            signal = "buy"
            confidence = 0.7
            reasoning = f"EMA{self.ema_short}({last_ema_short:.2f}) > EMA{self.ema_long}({last_ema_long:.2f}), MACD Cross Up, RSI({last_rsi:.2f}) in [{self.rsi_entry_low},{self.rsi_entry_high}]"

        # Exit conditions (for existing positions)
        # RSI above 70 or MACD reversal
        elif (last_rsi > 70) or \
             (last_macd < last_macd_signal and df['MACD'].iloc[-2] >= df['MACD_Signal'].iloc[-2]):
            signal = "sell"
            confidence = 0.9
            reasoning = f"Exit Condition Met: RSI({last_rsi:.2f}) > 70 OR MACD Reversal Down"

        if signal == "buy":
            stop_loss_price = current_price * (1 - (Config.STOP_LOSS_PERCENT / 100))
            take_profit_price = current_price * (1 + (Config.TAKE_PROFIT_PERCENT / 100))
            
            # Enforce 1:2 R/R Check
            risk = current_price - stop_loss_price
            reward = take_profit_price - current_price
            if risk > 0 and (reward / risk) < Config.SWING_MIN_RR_RATIO:
                signal = "hold"
                reasoning = f"Insufficient RR Ratio: {(reward/risk):.2f} < {Config.SWING_MIN_RR_RATIO}"

            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": confidence,
                "entry_price": current_price,
                "stop_price": stop_loss_price,
                "target_price": take_profit_price,
                "reasoning": reasoning
            }
        elif signal == "sell":
            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": confidence,
                "reasoning": reasoning
            }
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        if not signal:
            return

        symbol = signal["symbol"]
        
        # Handle Exit Signals
        if signal["signal"] == "sell":
            try:
                trading_client.close_position(symbol)
                print(f"✅ Swing Position Closed for {symbol} due to exit signal.")
            except Exception as e:
                print(f"❌ Failed to close position for {symbol}: {e}")
            return
            
        # 1. SAFETY CHECK: Are we already in this position?
        if self.is_already_in_position(symbol, trading_client):
            print(f"Skipping {symbol} - already in position or order pending.")
            return

        entry_price = signal["entry_price"]
        side = OrderSide.BUY

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
            sl_price = signal["stop_price"]
            tp_price = signal["target_price"]
            print(f"✅ Swing Order Placed: {side} {symbol} (Qty: {qty}) @ {entry_price}. SL: {sl_price}, TP: {tp_price}")
        except Exception as e:
            print(f"❌ Swing Order Failed for {symbol}: {e}")
