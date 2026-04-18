import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy
from utils import get_spy_data # Import the SPY data function

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
        # Ensure volume is not zero to avoid division by zero
        df_filtered = df[df["volume"] > 0]
        if df_filtered.empty:
            return pd.Series(np.nan, index=df.index)

        v = df_filtered["volume"].values
        tp = (df_filtered["low"] + df_filtered["high"] + df_filtered["close"]).values / 3
        vwap_series = pd.Series((tp * v).cumsum() / v.cumsum(), index=df_filtered.index)
        return vwap_series.reindex(df.index, method='ffill') # Fill NaN for original index

    def calculate_relative_strength(self, stock_data, spy_data):
        """
        Calculates the relative strength of a stock against SPY.
        Returns a positive value if the stock is stronger, negative if weaker.
        """
        if stock_data is None or spy_data is None or len(stock_data) < 2 or len(spy_data) < 2:
            return 0.0

        # Ensure both dataframes are aligned by date
        # We need to handle potential timezone differences or missing dates
        common_dates = stock_data.index.intersection(spy_data.index)
        if common_dates.empty:
            return 0.0

        stock_close = stock_data["close"].loc[common_dates]
        spy_close = spy_data["close"].loc[common_dates]

        if len(stock_close) < 2 or len(spy_close) < 2:
            return 0.0

        # Calculate percentage change from the start of the common period
        stock_performance = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
        spy_performance = (spy_close.iloc[-1] / spy_close.iloc[0]) - 1

        return stock_performance - spy_performance

    def generate_signals(self, market_data, stock_data_client):
        """
        Generates signals based on the 'Fashionably Late Scalp' setup,
        filtered by Relative Strength against SPY.
        """
        if market_data is None or len(market_data) < self.ema_window + 1:
            return None

        df = market_data.copy()
        
        # 1. Calculate Indicators
        df["EMA_9"] = df["close"].ewm(span=self.ema_window, adjust=False).mean()
        df["VWAP"] = self.calculate_vwap(df)
        
        # Ensure we have enough data for calculations
        if df["EMA_9"].isnull().any() or df["VWAP"].isnull().any():
            return None

        # 2. Relative Strength Filter (only for stocks)
        symbol = df["symbol"].iloc[-1] if "symbol" in df.columns else "UNKNOWN"
        is_crypto = "/" in symbol or symbol in ["BTCUSD", "ETHUSD"]

        if not is_crypto:
            spy_data = get_spy_data(stock_data_client, days_back=len(df))
            relative_strength = self.calculate_relative_strength(df, spy_data)

            # Only consider long signals if RS > 0, short signals if RS < 0
            if relative_strength is None:
                print(f"Could not calculate Relative Strength for {symbol}")
                return None
            
            # SMB Rule: Only trade with the trend of Relative Strength
            if relative_strength <= 0: # Stock is weaker or equal to SPY
                # print(f"Skipping {symbol} due to weak Relative Strength ({relative_strength:.2f})")
                return None

        # 3. Check for "Fashionably Late Scalp" Setup
        curr_ema = df["EMA_9"].iloc[-1]
        prev_ema = df["EMA_9"].iloc[-2]
        curr_vwap = df["VWAP"].iloc[-1]
        prev_vwap = df["VWAP"].iloc[-2]
        
        current_price = df["close"].iloc[-1]
        lod = df["low"].min() # Low of Day
        hod = df["high"].max() # High of Day
        
        signal = None
        confidence = 0.0
        
        # LONG SETUP: Upsloping 9 EMA crosses above VWAP
        if curr_ema > curr_vwap and prev_ema <= prev_vwap:
            if curr_ema > prev_ema: # Ensure EMA is upsloping
                signal = "buy"
                confidence = 0.8
                
        # SHORT SETUP: Downsloping 9 EMA crosses below VWAP
        elif curr_ema < curr_vwap and prev_ema >= prev_vwap:
            if curr_ema < prev_ema: # Ensure EMA is downsloping
                signal = "sell"
                confidence = 0.8
                
        if signal:
            # Calculate SMB-style Stop and Target
            # Distance from Cross to Low/High of Day
            if signal == "buy":
                distance = current_price - lod
                stop_price = current_price - (distance / self.rr_ratio) # 1/3 of distance for 1:3 RR
                target_price = current_price + distance # 1:3 RR relative to stop
            else: # Short
                distance = hod - current_price
                stop_price = current_price + (distance / self.rr_ratio)
                target_price = current_price - distance
                
            return {
                "symbol": symbol,
                "signal": signal,
                "confidence": confidence,
                "entry_price": current_price,
                "stop_price": stop_price,
                "target_price": target_price
            }
            
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
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
        max_cash_for_trade = float(account.buying_power) * (max_buying_power_utilization_percent / 100)
        
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            print(f"Calculated risk per share for {symbol} is zero or negative. Cannot place order.")
            return
            
        # Calculate quantity based on risk amount
        qty_from_risk = int(risk_amount / risk_per_share)
        
        # Calculate max quantity based on buying power
        qty_from_buying_power = int(max_cash_for_trade / entry_price) if entry_price > 0 else 0
        
        # Use the minimum of the two to ensure we don't exceed buying power
        qty = min(qty_from_risk, qty_from_buying_power)
        
        if qty <= 0:
            print(f"Calculated quantity for {symbol} is zero or negative. Not placing order.")
            return

        # Place a bracket order
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
            print(f"❌ SMB Order Failed for {symbol}: {e}")
