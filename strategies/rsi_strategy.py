import pandas as pd
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy

class RSIStrategy(BaseStrategy):
    def __init__(self, name, window=14, overbought=70, oversold=30):
        super().__init__(name)
        self.window = window
        self.overbought = overbought
        self.oversold = oversold

    def calculate_rsi(self, data):
        delta = data["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.window).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def generate_signals(self, market_data):
        if market_data is None or len(market_data) < self.window + 1:
            return None

        market_data["RSI"] = self.calculate_rsi(market_data)
        last_rsi = market_data["RSI"].iloc[-1]
        prev_rsi = market_data["RSI"].iloc[-2]
        current_price = market_data["close"].iloc[-1]

        signal = None
        confidence = 0.0

        if last_rsi < self.oversold and prev_rsi >= self.oversold:
            signal = "buy"
            confidence = (self.oversold - last_rsi) / self.oversold # Confidence based on how oversold
        elif last_rsi > self.overbought and prev_rsi <= self.overbought:
            signal = "sell"
            confidence = (last_rsi - self.overbought) / (100 - self.overbought) # Confidence based on how overbought
        
        if signal:
            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": min(confidence * 2, 1.0), # Scale confidence to max 1.0
                "entry_price": current_price
            }
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent):
        if signal is None or signal["signal"] == "hold":
            return

        symbol = signal["symbol"]
        entry_price = signal["entry_price"]
        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL

        # Get account details to calculate position size
        account = trading_client.get_account()
        if not account:
            print("Could not retrieve account details.")
            return

        current_equity = float(account.equity)
        risk_amount = current_equity * (equity_risk_percent / 100)

        # Calculate stop loss and take profit prices
        if side == OrderSide.BUY:
            stop_price = entry_price * (1 - (stop_loss_percent / 100))
            take_profit_price = entry_price * (1 + (take_profit_percent / 100))
            # Calculate quantity based on risk amount and stop loss
            price_diff_to_stop = entry_price - stop_price
            if price_diff_to_stop <= 0: # Avoid division by zero or negative risk
                print(f"Invalid stop loss price for {symbol}. Cannot calculate quantity.")
                return
            qty = int(risk_amount / price_diff_to_stop)
        else: # Sell (short)
            stop_price = entry_price * (1 + (stop_loss_percent / 100))
            take_profit_price = entry_price * (1 - (take_profit_percent / 100))
            # Calculate quantity based on risk amount and stop loss
            price_diff_to_stop = stop_price - entry_price
            if price_diff_to_stop <= 0: # Avoid division by zero or negative risk
                print(f"Invalid stop loss price for {symbol}. Cannot calculate quantity.")
                return
            qty = int(risk_amount / price_diff_to_stop)

        if qty <= 0:
            print(f"Calculated quantity for {symbol} is zero or negative. Not placing order.")
            return

        # Place a bracket order (market order with stop loss and take profit)
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC, # Good \'Til Canceled
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price)
        )
        
        try:
            order = trading_client.submit_order(order_data=order_data)
            print(f"Successfully placed {side} bracket order for {symbol} (Qty: {qty}) at {entry_price}. SL: {stop_price}, TP: {take_profit_price}. Order ID: {order.id}")
        except Exception as e:
            print(f"Failed to place {side} bracket order for {symbol}: {e}")
