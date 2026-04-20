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
        if market_data is None or len(market_data) < self.long_window:
            return None

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
            confidence = (last_short - last_long) / last_long
        elif last_short < last_long and prev_short >= prev_long: # Death Cross
            signal = "sell"
            confidence = (last_long - last_short) / last_long
        
        if signal:
            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": min(confidence * 10, 1.0),
                "entry_price": current_price
            }
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        if signal is None or signal["signal"] == "hold":
            return

        symbol = signal["symbol"]
        
        # 1. SAFETY CHECK: Are we already in this position?
        if self.is_already_in_position(symbol, trading_client):
            # print(f"Skipping {symbol} - already in position or order pending.")
            return

        entry_price = signal["entry_price"]
        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL

        account = trading_client.get_account()
        if not account:
            return

        # 2. Calculate Stop/Target
        if side == OrderSide.BUY:
            stop_price = entry_price * (1 - (stop_loss_percent / 100))
            take_profit_price = entry_price * (1 + (take_profit_percent / 100))
        else:
            stop_price = entry_price * (1 + (stop_loss_percent / 100))
            take_profit_price = entry_price * (1 - (take_profit_percent / 100))

        # 3. SAFETY LOCK: Calculate Safe Quantity
        qty = self.calculate_safe_quantity(
            symbol, entry_price, stop_price, account, 
            equity_risk_percent, max_buying_power_utilization_percent
        )

        if qty <= 0:
            return

        # 4. Place Order
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price)
        )
        
        try:
            trading_client.submit_order(order_data=order_data)
            print(f"✅ Order Placed: {side} {symbol} (Qty: {qty}) at {entry_price}. SL: {stop_price}, TP: {take_profit_price}")
        except Exception as e:
            print(f"❌ Order Failed for {symbol}: {e}")
