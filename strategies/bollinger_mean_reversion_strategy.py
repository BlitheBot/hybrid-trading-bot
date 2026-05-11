import pandas as pd
import numpy as np
import pandas_ta as ta
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from .base_strategy import BaseStrategy
from config import Config


class BollingerMeanReversionStrategy(BaseStrategy):
    """
    Bollinger Band lower-break + RSI oversold entry; middle band cross or RSI exit.
    Mirrors the bb_mean_reversion DiscoveryStrategy logic for live execution.

    Entry:  close crosses below lower band AND RSI < rsi_entry
    Exit:   close crosses above middle band OR RSI > rsi_exit
    """

    def __init__(self, name: str, bb_period: int = 20, bb_std: float = 2.0,
                 rsi_period: int = 14, rsi_entry: int = 30, rsi_exit: int = 65):
        super().__init__(name)
        self.bb_period  = bb_period
        self.bb_std     = bb_std
        self.rsi_period = rsi_period
        self.rsi_entry  = rsi_entry
        self.rsi_exit   = rsi_exit
        # Expose ema_short/ema_long as 0 so _log_trade_entry doesn't crash on getattr
        self.ema_short  = 0
        self.ema_long   = 0

    def generate_signals(self, market_data: pd.DataFrame):
        min_bars = self.bb_period + self.rsi_period + 5
        if market_data is None or len(market_data) < min_bars:
            return None

        df = market_data.copy()

        bb = ta.bbands(df["close"], length=self.bb_period, std=self.bb_std)
        if bb is None or bb.empty:
            return None

        df["bb_lower"]  = bb.iloc[:, 0]
        df["bb_middle"] = bb.iloc[:, 1]
        df["rsi"]       = ta.rsi(df["close"], length=self.rsi_period)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        if any(pd.isna([last["bb_lower"], last["bb_middle"], last["rsi"],
                        prev["bb_lower"], prev["rsi"]])):
            return None

        current_price = float(last["close"])
        signal        = None
        confidence    = 0.0
        reasoning     = ""

        # Entry: close crosses below lower band this bar AND RSI oversold
        crossed_below = (float(last["close"]) < float(last["bb_lower"]) and
                         float(prev["close"]) >= float(prev["bb_lower"]))
        if crossed_below and float(last["rsi"]) < self.rsi_entry:
            signal    = "buy"
            confidence = 0.7
            reasoning = (
                f"BB Mean Reversion: Price({current_price:.2f}) crossed below "
                f"BB_lower({last['bb_lower']:.2f}), "
                f"RSI({last['rsi']:.1f}) < {self.rsi_entry}"
            )

        # Exit: close crosses above middle band OR RSI overbought
        elif (float(last["close"]) >= float(last["bb_middle"]) and
              float(prev["close"]) < float(prev["bb_middle"])) or float(last["rsi"]) > self.rsi_exit:
            signal    = "sell"
            confidence = 0.9
            reasoning = (
                f"BB Mean Reversion Exit: Price({current_price:.2f}) "
                f"≥ BB_mid({last['bb_middle']:.2f}) "
                f"OR RSI({last['rsi']:.1f}) > {self.rsi_exit}"
            )

        symbol = (market_data["symbol"].iloc[-1]
                  if "symbol" in market_data.columns else "UNKNOWN")

        if signal == "buy":
            stop_loss_price = current_price * (1 - Config.STOP_LOSS_PERCENT / 100)
            # Target: mean reversion to middle band; fall back to config TP if unreachable
            take_profit_price = float(last["bb_middle"])
            if take_profit_price <= current_price * 1.001:
                take_profit_price = current_price * (1 + Config.TAKE_PROFIT_PERCENT / 100)

            risk   = current_price - stop_loss_price
            reward = take_profit_price - current_price
            if risk > 0 and (reward / risk) < Config.SWING_MIN_RR_RATIO:
                return {
                    "symbol":    symbol,
                    "signal":    "hold",
                    "confidence": 0.0,
                    "reasoning": (
                        f"BB MeanRev: Insufficient R/R "
                        f"{reward/risk:.2f} < {Config.SWING_MIN_RR_RATIO}"
                    ),
                }

            return {
                "symbol":               symbol,
                "signal":               "buy",
                "confidence":           confidence,
                "confidence_multiplier": 1.0,
                "entry_price":          current_price,
                "stop_price":           round(stop_loss_price, 4),
                "target_price":         round(take_profit_price, 4),
                "rsi_at_entry":         float(last["rsi"]),
                "macd_at_entry":        0.0,
                "reasoning":            reasoning,
            }

        elif signal == "sell":
            return {
                "symbol":     symbol,
                "signal":     "sell",
                "confidence": confidence,
                "reasoning":  reasoning,
            }

        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent,
                      stop_loss_percent, take_profit_percent,
                      max_buying_power_utilization_percent):
        if not signal:
            return

        symbol = signal["symbol"]

        if signal["signal"] == "sell":
            try:
                trading_client.close_position(symbol)
                print(f"✅ BB MeanRev Position Closed for {symbol}")
            except Exception as e:
                print(f"❌ Failed to close position for {symbol}: {e}")
            return

        if self.is_already_in_position(symbol, trading_client):
            print(f"Skipping {symbol} — already in position or order pending.")
            return

        entry_price = signal["entry_price"]
        account     = trading_client.get_account()
        if not account:
            print("Could not retrieve account for BB MeanRev trade.")
            return

        qty = self.calculate_safe_quantity(
            symbol, entry_price, signal["stop_price"], account,
            equity_risk_percent, max_buying_power_utilization_percent
        )
        if qty <= 0:
            print(f"BB MeanRev: calculated qty=0 for {symbol} — skipping.")
            return

        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            take_profit=TakeProfitRequest(limit_price=signal["target_price"]),
            stop_loss=StopLossRequest(stop_price=signal["stop_price"]),
        )
        try:
            trading_client.submit_order(order_data=order_data)
            print(
                f"✅ BB MeanRev Order Placed: BUY {symbol} (Qty: {qty}) "
                f"@ {entry_price:.2f}  SL: {signal['stop_price']:.2f}  "
                f"TP: {signal['target_price']:.2f}"
            )
        except Exception as e:
            print(f"❌ BB MeanRev Order Failed for {symbol}: {e}")
