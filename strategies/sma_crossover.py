import pandas as pd
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from .base_strategy import BaseStrategy

class SMACrossoverStrategy(BaseStrategy):
    def __init__(self, name, short_window=50, long_window=200):
        super().__init__(name)
        self.short_window = short_window
        self.long_window = long_window

    def generate_signals(self, market_data):
        """
        Generates buy/sell signals based on SMA crossover.
        :param market_data: A pandas DataFrame containing historical bar data for a single symbol.
        :return: A signal dictionary with symbol, signal, confidence, and entry_price.
        """
        if market_data is None or len(market_data) < self.long_window:
            return None

        # Calculate moving averages
        market_data["SMA_short"] = market_data["close"].rolling(window=self.short_window).mean()
        market_data["SMA_long"] = market_data["close"].rolling(window=self.long_window).mean()

        last_short = market_data["SMA_short"].iloc[-1]
        last_long = market_data["SMA_long"].iloc[-1]
        prev_short = market_data["SMA_short"].iloc[-2]
        prev_long = market_data["SMA_long"].iloc[-2]
        current_price = market_data["close"].iloc[-1]

        signal = None
        confidence = 0.0

        if last_short > last_long and prev_short <= prev_long: # Golden Cross
            signal = "buy"
            confidence = (last_short - last_long) / last_long # Confidence based on spread
        elif last_short < last_long and prev_short >= prev_long: # Death Cross
            signal = "sell"
            confidence = (last_long - last_short) / last_long # Confidence based on spread
        
        if signal:
            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": min(confidence * 10, 1.0), # Scale confidence to max 1.0
                "entry_price": current_price
            }
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        """
        Executes a trade based on the generated signal and risk management parameters.
        """
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
        max_cash_for_trade = float(account.buying_power) * (max_buying_power_utilization_percent / 100)

        # Calculate stop loss and take profit prices
        if side == OrderSide.BUY:
            stop_price = entry_price * (1 - (stop_loss_percent / 100))
            take_profit_price = entry_price * (1 + (take_profit_percent / 100))
            # Calculate quantity based on risk amount and stop loss
            price_diff_to_stop = entry_price - stop_price
            if price_diff_to_stop <= 0: # Avoid division by zero or negative risk
                print(f"Invalid stop loss price for {symbol}. Cannot calculate quantity.")
                return
            qty_from_risk = int(risk_amount / price_diff_to_stop)
        else: # Sell (short)
            stop_price = entry_price * (1 + (stop_loss_percent / 100))
            take_profit_price = entry_price * (1 - (take_profit_percent / 100))
            # Calculate quantity based on risk amount and stop loss
            price_diff_to_stop = stop_price - entry_price
            if price_diff_to_stop <= 0: # Avoid division by zero or negative risk
                print(f"Invalid stop loss price for {symbol}. Cannot calculate quantity.")
                return
            qty_from_risk = int(risk_amount / price_diff_to_stop)

        # Calculate max quantity based on buying power
        qty_from_buying_power = int(max_cash_for_trade / entry_price) if entry_price > 0 else 0
        
        # Use the minimum of the two to ensure we don't exceed buying power
        qty = min(qty_from_risk, qty_from_buying_power)

        if qty <= 0:
            print(f"Calculated quantity for {symbol} is zero or negative. Not placing order.")
            return

        # Place a bracket order (market order with stop loss and take profit)
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC, # Good 'Til Canceled
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price)
        )
        
        try:
            order = trading_client.submit_order(order_data=order_data)
            print(f"Successfully placed {side} bracket order for {symbol} (Qty: {qty}) at {entry_price}. SL: {stop_price}, TP: {take_profit_price}. Order ID: {order.id}")
        except Exception as e:
            print(f"Failed to place {side} bracket order for {symbol}: {e}")
