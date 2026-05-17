import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy
from .kalman_signal import KalmanTrendSignal
from config import Config

import pandas_ta as ta

class SwingStrategy(BaseStrategy):
    def __init__(self, name, ema_short=50, ema_long=200, macd_fast=12, macd_slow=26, macd_signal=9,
                 rsi_period=14, rsi_entry_low=40, rsi_entry_high=60):
        super().__init__(name)
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.rsi_period = rsi_period
        self.rsi_entry_low = rsi_entry_low
        self.rsi_entry_high = rsi_entry_high
        # Kalman noise gate — suppresses entries when noise_ratio >= 0.4 (40% of price
        # movement is unexplained noise). Q/R tuned for daily equity bars.
        self._kalman = KalmanTrendSignal(
            process_variance=1e-3,
            measurement_variance=0.1,
            signal_noise_threshold=0.4,
        )

    def _check_candlestick_patterns(self, df: pd.DataFrame) -> tuple:
        """
        Checks the last 3 bars for any of four bullish candlestick patterns using pandas-ta.
        Returns (pattern_name, 1.0) if a bullish pattern is found, (None, 0.8) otherwise.
        Returns (None, 1.0) on any library error so the gate never blocks a valid signal.
        """
        if not Config.CANDLESTICK_CONFIRMATION_ENABLED:
            return None, 1.0
        try:
            if len(df) < 10:
                return None, 1.0

            o, h, l, c = df["open"], df["high"], df["low"], df["close"]

            pattern_checks = [
                ("Hammer",            "hammer"),
                ("Bullish Engulfing", "engulfing"),
                ("Morning Star",      "morningstar"),
                ("Doji Star",         "dojistar"),
            ]

            for label, name in pattern_checks:
                try:
                    result = ta.cdl_pattern(o, h, l, c, name=name)
                    if result is None:
                        continue
                    vals = result.iloc[-3:].values.flatten() if isinstance(result, pd.DataFrame) \
                        else result.iloc[-3:].values
                    if any(v == 100 for v in vals):
                        return label, 1.0
                except Exception:
                    continue  # unknown pattern name or insufficient data — skip

            return None, 0.8
        except Exception:
            return None, 1.0  # library failure — pass silently at full size

    def generate_signals(self, market_data):
        if market_data is None or len(market_data) < self.ema_long + self.macd_slow + self.macd_signal + self.rsi_period:
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
        df['RSI'] = ta.rsi(df['close'], length=self.rsi_period)

        # Check only the last row — early rows always have NaN from rolling warmup
        if (pd.isna(df['EMA_short'].iloc[-1]) or pd.isna(df['EMA_long'].iloc[-1]) or
                pd.isna(df['MACD'].iloc[-1]) or pd.isna(df['MACD_Signal'].iloc[-1]) or
                pd.isna(df['RSI'].iloc[-1])):
            if Config.SWING_VERBOSE_LOGGING:
                print(
                    f"[SwingVerbose] {self.name}: indicators have NaN — insufficient history "
                    f"(need {self.ema_long + self.macd_slow + self.macd_signal + self.rsi_period} bars)"
                )
            return None

        last_ema_short  = df['EMA_short'].iloc[-1]
        last_ema_long   = df['EMA_long'].iloc[-1]
        last_macd       = df['MACD'].iloc[-1]
        last_macd_signal = df['MACD_Signal'].iloc[-1]
        last_rsi        = df['RSI'].iloc[-1]
        current_price   = df['close'].iloc[-1]

        if Config.SWING_VERBOSE_LOGGING:
            print(
                f"[SwingVerbose] {self.name}: price={current_price:.2f} | "
                f"EMA{self.ema_short}={last_ema_short:.2f} EMA{self.ema_long}={last_ema_long:.2f} | "
                f"MACD={last_macd:.4f} MACDsig={last_macd_signal:.4f} | "
                f"RSI({self.rsi_period})={last_rsi:.2f} (gate [{self.rsi_entry_low},{self.rsi_entry_high}])"
            )

        # Kalman noise gate — compute once, gate the entry condition
        k = self._kalman.compute_latest(df['close'])
        noise_ok = k["noise_ratio"] < 0.4

        # Decompose entry conditions for clean logging
        ema_ok   = last_ema_short > last_ema_long
        macd_ok  = (last_macd > last_macd_signal and
                    df['MACD'].iloc[-2] <= df['MACD_Signal'].iloc[-2])
        rsi_ok   = self.rsi_entry_low <= last_rsi <= self.rsi_entry_high

        signal = None
        confidence = 0.0

        # Entry conditions: EMA_short > EMA_long + MACD crossover + RSI in range + Kalman noise gate
        if ema_ok and macd_ok and rsi_ok and noise_ok:
            signal = "buy"
            confidence = 0.7
            reasoning = (
                f"EMA{self.ema_short}({last_ema_short:.2f}) > EMA{self.ema_long}({last_ema_long:.2f}), "
                f"MACD Cross Up, RSI({last_rsi:.2f}) in [{self.rsi_entry_low},{self.rsi_entry_high}], "
                f"Kalman noise={k['noise_ratio']:.2f}"
            )
        elif ema_ok and macd_ok and rsi_ok and not noise_ok:
            if Config.SWING_VERBOSE_LOGGING:
                print(
                    f"[SwingVerbose] {self.name}: BUY suppressed by Kalman noise gate "
                    f"(noise_ratio={k['noise_ratio']:.2f} >= 0.4)"
                )

        # Exit conditions (for existing positions)
        # RSI above 70 or MACD reversal
        elif (last_rsi > 70) or \
             (last_macd < last_macd_signal and df['MACD'].iloc[-2] >= df['MACD_Signal'].iloc[-2]):
            signal = "sell"
            confidence = 0.9
            reasoning = f"Exit Condition Met: RSI({last_rsi:.2f}) > 70 OR MACD Reversal Down"

        elif Config.SWING_VERBOSE_LOGGING and not (ema_ok and macd_ok and rsi_ok):
            # Log which specific entry condition(s) failed
            failed = []
            if not ema_ok:
                failed.append(
                    f"EMA{self.ema_short}({last_ema_short:.2f}) <= EMA{self.ema_long}({last_ema_long:.2f}) — no bullish trend"
                )
            if not macd_ok:
                if last_macd <= last_macd_signal:
                    failed.append(
                        f"MACD({last_macd:.4f}) below signal({last_macd_signal:.4f}) — bearish momentum"
                    )
                else:
                    failed.append(
                        f"MACD no fresh crossover — already above signal prev bar "
                        f"(prev MACD={df['MACD'].iloc[-2]:.4f} sig={df['MACD_Signal'].iloc[-2]:.4f})"
                    )
            if not rsi_ok:
                if last_rsi < self.rsi_entry_low:
                    failed.append(f"RSI({last_rsi:.2f}) < low gate {self.rsi_entry_low} — oversold / not yet recovering")
                else:
                    failed.append(f"RSI({last_rsi:.2f}) > high gate {self.rsi_entry_high} — overbought")
            print(f"[SwingVerbose] {self.name}: HOLD — {' | '.join(failed)}")

        if signal == "buy":
            stop_loss_price = current_price * (1 - (Config.STOP_LOSS_PERCENT / 100))
            take_profit_price = current_price * (1 + (Config.TAKE_PROFIT_PERCENT / 100))

            # Enforce 1:2 R/R Check
            risk = current_price - stop_loss_price
            reward = take_profit_price - current_price
            if risk > 0 and (reward / risk) < Config.SWING_MIN_RR_RATIO:
                signal = "hold"
                reasoning = f"Insufficient RR Ratio: {(reward/risk):.2f} < {Config.SWING_MIN_RR_RATIO}"
                if Config.SWING_VERBOSE_LOGGING:
                    print(
                        f"[SwingVerbose] {self.name}: HOLD — R/R {reward/risk:.2f} < "
                        f"min {Config.SWING_MIN_RR_RATIO} "
                        f"(risk=${risk:.2f} reward=${reward:.2f})"
                    )

            # Candlestick confirmation gate (only for signals that passed R/R)
            confidence_multiplier = 1.0
            if signal == "buy":
                pattern_name, confidence_multiplier = self._check_candlestick_patterns(df)
                if pattern_name:
                    reasoning += f" | Pattern: {pattern_name}"
                elif confidence_multiplier < 1.0:
                    reasoning += " | No candlestick confirmation — confidence -20%"
                if Config.SWING_VERBOSE_LOGGING:
                    pattern_str = pattern_name if pattern_name else "none"
                    print(
                        f"[SwingVerbose] {self.name}: BUY signal confirmed — "
                        f"candlestick={pattern_str} confidence_mult={confidence_multiplier}"
                    )

            return {
                "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                "signal": signal,
                "confidence": confidence * confidence_multiplier,
                "confidence_multiplier": confidence_multiplier,
                "entry_price": current_price,
                "stop_price": stop_loss_price,
                "target_price": take_profit_price,
                "rsi_at_entry": float(last_rsi),
                "macd_at_entry": float(last_macd),
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
