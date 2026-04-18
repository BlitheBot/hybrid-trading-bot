from .base_strategy import BaseStrategy
from .sma_crossover import SMACrossoverStrategy
from .rsi_strategy import RSIStrategy
from .mean_reversion import MeanReversionStrategy
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce

class HybridStrategy(BaseStrategy):
    def __init__(self, name, min_confidence=0.6, min_votes=2):
        super().__init__(name)
        self.sma_strategy = SMACrossoverStrategy("SMA Crossover")
        self.rsi_strategy = RSIStrategy("RSI Strategy")
        self.mr_strategy = MeanReversionStrategy("Mean Reversion")
        self.min_confidence = min_confidence
        self.min_votes = min_votes

    def generate_signals(self, market_data):
        sma_signal = self.sma_strategy.generate_signals(market_data)
        rsi_signal = self.rsi_strategy.generate_signals(market_data)
        mr_signal = self.mr_strategy.generate_signals(market_data)

        buy_votes = 0
        sell_votes = 0
        total_confidence = 0.0
        entry_price = market_data["close"].iloc[-1]
        symbol = market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN"

        signals = []
        if sma_signal and sma_signal["signal"] == "buy" and sma_signal["confidence"] >= self.min_confidence:
            buy_votes += 1
            total_confidence += sma_signal["confidence"]
            signals.append(sma_signal)
        elif sma_signal and sma_signal["signal"] == "sell" and sma_signal["confidence"] >= self.min_confidence:
            sell_votes += 1
            total_confidence += sma_signal["confidence"]
            signals.append(sma_signal)

        if rsi_signal and rsi_signal["signal"] == "buy" and rsi_signal["confidence"] >= self.min_confidence:
            buy_votes += 1
            total_confidence += rsi_signal["confidence"]
            signals.append(rsi_signal)
        elif rsi_signal and rsi_signal["signal"] == "sell" and rsi_signal["confidence"] >= self.min_confidence:
            sell_votes += 1
            total_confidence += rsi_signal["confidence"]
            signals.append(rsi_signal)

        if mr_signal and mr_signal["signal"] == "buy" and mr_signal["confidence"] >= self.min_confidence:
            buy_votes += 1
            total_confidence += mr_signal["confidence"]
            signals.append(mr_signal)
        elif mr_signal and mr_signal["signal"] == "sell" and mr_signal["confidence"] >= self.min_confidence:
            sell_votes += 1
            total_confidence += mr_signal["confidence"]
            signals.append(mr_signal)

        if buy_votes >= self.min_votes:
            return {
                "symbol": symbol,
                "signal": "buy",
                "confidence": total_confidence / buy_votes if buy_votes > 0 else 0.0,
                "entry_price": entry_price
            }
        elif sell_votes >= self.min_votes:
            return {
                "symbol": symbol,
                "signal": "sell",
                "confidence": total_confidence / sell_votes if sell_votes > 0 else 0.0,
                "entry_price": entry_price
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
