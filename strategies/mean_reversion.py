import pandas as pd
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy

class MeanReversionStrategy(BaseStrategy):
    def __init__(self, name, window=20, std_dev=2):
        super().__init__(name)
        self.window = window
        self.std_dev = std_dev

    def generate_signals(self, market_data):
        if market_data is None or len(market_data) < self.window:
            return None

        # Calculate Bollinger Bands
        market_data["rolling_mean"] = market_data["close"].rolling(window=self.window).mean()
        market_data["rolling_std"] = market_data["close"].rolling(window=self.window).std()
        market_data["upper_band"] = market_data["rolling_mean"] + (market_data["rolling_std"] * self.std_dev)
        market_data["lower_band"] = market_data["rolling_mean"] - (market_data["rolling_std"] * self.std_dev)

        last_price = market_data["close"].iloc[-1]
        current_price = market_data["close"].iloc[-1]

        signal = None
        confidence = 0.0

        if last_price < market_data["lower_band"].iloc[-1]:
            signal = "buy"
            confidence = (market_data["lower_band"].iloc[-1] - last_price) / market_data["lower_band"].iloc[-1] # Confidence based on how far below lower band
        elif last_price > market_data["upper_band"].iloc[-1]:
            signal = "sell"
            confidence = (last_price - market_data["upper_band"].iloc[-1]) / market_data["upper_band"].iloc[-1] # Confidence based on how far above upper band
        
        if signal:
            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": min(confidence * 5, 1.0), # Scale confidence to max 1.0
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
