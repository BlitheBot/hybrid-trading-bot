import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy

class SMBStrategy(BaseStrategy):
    """
    Implements SMB Capital inspired strategies:
    1. Fashionably Late Scalp (9 EMA crossing VWAP)
    2. Relative Strength Filter (Stock vs SPY)
    """
    def __init__(self, name, ema_window=9, rr_ratio=3):
        super().__init__(name)
        self.ema_window = ema_window
        self.rr_ratio = rr_ratio

    def calculate_vwap(self, df):
        """Calculates VWAP for the given dataframe."""
        v = df['volume'].values
        tp = (df['low'] + df['high'] + df['close']).values / 3
        return pd.Series((tp * v).cumsum() / v.cumsum(), index=df.index)

    def generate_signals(self, market_data):
        """
        Generates signals based on the 'Fashionably Late Scalp' setup.
        """
        if market_data is None or len(market_data) < 20:
            return None

        df = market_data.copy()
        
        # 1. Calculate Indicators
        df['EMA_9'] = df['close'].rolling(window=self.ema_window).mean()
        df['VWAP'] = self.calculate_vwap(df)
        
        # 2. Check for "Fashionably Late Scalp" Setup
        # Logic: 9 EMA crosses VWAP
        curr_ema = df['EMA_9'].iloc[-1]
        prev_ema = df['EMA_9'].iloc[-2]
        curr_vwap = df['VWAP'].iloc[-1]
        prev_vwap = df['VWAP'].iloc[-2]
        
        current_price = df['close'].iloc[-1]
        lod = df['low'].min() # Low of Day
        
        signal = None
        confidence = 0.0
        
        # LONG SETUP: Upsloping 9 EMA crosses above VWAP
        if curr_ema > curr_vwap and prev_ema <= prev_vwap:
            # Check if EMA is upsloping
            if curr_ema > prev_ema:
                signal = "buy"
                confidence = 0.8
                
        # SHORT SETUP: Downsloping 9 EMA crosses below VWAP
        elif curr_ema < curr_vwap and prev_ema >= prev_vwap:
            # Check if EMA is downsloping
            if curr_ema < prev_ema:
                signal = "sell"
                confidence = 0.8
                
        if signal:
            # Calculate SMB-style Stop and Target
            # Distance from Cross to Low/High of Day
            if signal == "buy":
                distance = current_price - lod
                stop_price = current_price - (distance / 3) # Tight SMB stop
                target_price = current_price + (distance) # 1:3 RR relative to stop
            else:
                hod = df['high'].max()
                distance = hod - current_price
                stop_price = current_price + (distance / 3)
                target_price = current_price - (distance)
                
            return {
                "symbol": df["symbol"].iloc[-1] if "symbol" in df.columns else "UNKNOWN",
                "signal": signal,
                "confidence": confidence,
                "entry_price": current_price,
                "stop_price": stop_price,
                "target_price": target_price
            }
            
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent):
        """
        Executes trade using SMB-specific stop and target levels.
        """
        if not signal:
            return

        symbol = signal["symbol"]
        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL
        entry_price = signal["entry_price"]
        stop_price = signal["stop_price"]
        target_price = signal["target_price"]

        # Calculate Quantity based on 2% risk
        account = trading_client.get_account()
        risk_amount = float(account.equity) * (equity_risk_percent / 100)
        
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return
            
        qty = int(risk_amount / risk_per_share)
        
        if qty <= 0:
            return

        # Place Bracket Order
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC,
            take_profit=TakeProfitRequest(limit_price=target_price),
            stop_loss=StopLossRequest(stop_price=stop_price)
        )
        
        try:
            trading_client.submit_order(order_data=order_data)
            print(f"✅ SMB Order Placed: {side} {symbol} @ {entry_price}. Target: {target_price}, Stop: {stop_price}")
        except Exception as e:
            print(f"❌ SMB Order Failed: {e}")
