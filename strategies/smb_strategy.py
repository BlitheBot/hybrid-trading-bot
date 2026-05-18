import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from .base_strategy import BaseStrategy
from .kalman_signal import KalmanTrendSignal
from .vwap_signal import AnchoredVWAPSignal
from .kelly_sizer import KellySizer
from utils import get_spy_data
from config import Config

import pandas_ta as ta

class SMBStrategy(BaseStrategy):
    def __init__(self, name, ema_window=9, rr_ratio=3, db_engine=None, base_capital: float = 0.0,
                 drawdown_threshold_pct: float = 10.0, drawdown_window_days: int = 14):
        super().__init__(name)
        self.ema_window = ema_window  # kept for API compatibility; no longer used in signal generation
        self.rr_ratio = rr_ratio
        self.drawdown_threshold_pct = drawdown_threshold_pct
        self.drawdown_window_days = drawdown_window_days
        self._kelly = KellySizer(db_engine=db_engine, base_capital=base_capital) if db_engine else None
        # Kalman replaces EMA-9 as the trend line for VWAP crossover detection.
        # Q=1e-3 / R=0.1 are calibrated for daily bars (current use).
        # If crypto scalp is ever re-enabled on intraday data, raise Q to ~5e-3
        # so the filter reacts faster to the shorter bar duration.
        self._kalman = KalmanTrendSignal(process_variance=1e-3, measurement_variance=0.1)
        # AnchoredVWAPSignal: entry filter requiring price to be on the correct
        # side of VWAP with above-average volume before executing a crossover signal.
        # distance_threshold_pct=0.3: tighter than the 0.5 equity default — crypto
        #   sits near VWAP longer during consolidation so 0.3% separates real
        #   directional moves from indecision at the crossover point.
        # volume_ratio_threshold=1.2: 20% above-average volume as a floor for
        #   institutional participation confirmation.
        self._avwap = AnchoredVWAPSignal(
            window=20,
            anchor="rolling",
            distance_threshold_pct=0.3,
            volume_ratio_threshold=1.2,
        )

    def calculate_relative_strength(self, stock_data, spy_data):
        if stock_data is None or spy_data is None or len(stock_data) < 2 or len(spy_data) < 2:
            return 0.0
        common_dates = stock_data.index.intersection(spy_data.index)
        if common_dates.empty:
            return 0.0
        stock_close = stock_data["close"].loc[common_dates]
        spy_close = spy_data["close"].loc[common_dates]
        stock_performance = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
        spy_performance = (spy_close.iloc[-1] / spy_close.iloc[0]) - 1
        return stock_performance - spy_performance

    def generate_signals(self, market_data, stock_data_client):
        # 30-bar minimum: Kalman noise_ratio needs 20-bar warmup; VWAP+ATR need ~14 bars
        if market_data is None or len(market_data) < 30:
            return None
        df = market_data.copy()

        df["VWAP"] = ta.vwap(high=df["high"], low=df["low"], close=df["close"], volume=df["volume"])
        df["ATR"] = ta.atr(high=df["high"], low=df["low"], close=df["close"], length=14)

        # Only check the last 2 rows — early rows naturally have NaN from rolling calculations
        if df[["VWAP", "ATR"]].iloc[-2:].isnull().any().any():
            return None

        symbol = str(df["symbol"].iloc[-1]) if "symbol" in df.columns else "UNKNOWN"
        is_crypto = "/" in symbol or symbol in ["BTCUSD", "ETHUSD"]
        if not is_crypto:
            spy_data = get_spy_data(stock_data_client, days_back=len(df))
            relative_strength = self.calculate_relative_strength(df, spy_data)
            if relative_strength is None or relative_strength <= 0:
                return None

        # Kalman trend line replaces EMA-9 for VWAP crossover detection.
        # noise_ratio gate baked into k_signal: it's 0 (flat) when the market is too choppy.
        k_df = self._kalman.compute(df["close"])
        curr_trend  = k_df["trend"].iloc[-1]
        prev_trend  = k_df["trend"].iloc[-2]
        k_signal    = k_df["signal"].iloc[-1]   # +1 rising+clean / -1 falling+clean / 0 noisy
        noise_ratio = k_df["noise_ratio"].iloc[-1]

        curr_vwap, prev_vwap = df["VWAP"].iloc[-1], df["VWAP"].iloc[-2]
        current_price = df["close"].iloc[-1]
        atr = df["ATR"].iloc[-1]

        vwap_latest = self._avwap.compute_latest(df)

        raw_signal = None
        if curr_trend > curr_vwap and prev_trend <= prev_vwap and k_signal == 1:
            raw_signal = "buy"
        elif curr_trend < curr_vwap and prev_trend >= prev_vwap and k_signal == -1:
            raw_signal = "sell"

        # AnchoredVWAP gate: signal +1/-1 requires price >= 0.3% from VWAP
        # AND volume_ratio >= 1.2x — confirms institutional activity in the
        # same direction as the Kalman/VWAP crossover.
        signal = None
        if raw_signal == "buy" and vwap_latest["signal"] == 1:
            signal = "buy"
        elif raw_signal == "sell" and vwap_latest["signal"] == -1:
            signal = "sell"
        elif raw_signal is not None and Config.SWING_VERBOSE_LOGGING:
            print(
                f"[SMBVerbose] {self.name}: {raw_signal.upper()} suppressed by VWAP "
                f"confirmation gate (distance_pct={vwap_latest['distance_pct']:.2f}% "
                f"volume_ratio={vwap_latest['volume_ratio']:.2f}x — "
                f"need ±0.3% and >=1.2x)"
            )

        if signal and not np.isnan(atr):
            if signal == "buy":
                distance = atr * 1.5
                stop_price = current_price - distance
                target_price = current_price + (distance * self.rr_ratio)
            else:
                distance = atr * 1.5
                stop_price = current_price + distance
                target_price = current_price - (distance * self.rr_ratio)
            return {
                "symbol": symbol, "signal": signal, "confidence": 0.8,
                "entry_price": current_price, "stop_price": stop_price, "target_price": target_price,
                "noise_ratio": round(float(noise_ratio), 3),
                "distance_pct": round(float(vwap_latest["distance_pct"]), 2),
                "reasoning": (
                    f"Kalman/VWAP Crossover + AVWAP confirm. "
                    f"noise_ratio={noise_ratio:.2f} "
                    f"dist={vwap_latest['distance_pct']:.2f}% "
                    f"vol={vwap_latest['volume_ratio']:.2f}x "
                    f"ATR(14): {atr:.4f}"
                ),
            }
        return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        if not signal:
            return
        symbol = signal["symbol"]
        
        # 1. SAFETY CHECK: Are we already in this position?
        if self.is_already_in_position(symbol, trading_client):
            return

        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL
        entry_price, stop_price, target_price = signal["entry_price"], signal["stop_price"], signal["target_price"]

        account = trading_client.get_account()
        if not account:
            return
            
        # 2. SAFETY LOCK: Calculate Quantity
        # Kelly pre-computed in _process_symbol when sufficient history exists;
        # falls back to risk-based sizing when below MIN_SAMPLE_SIZE.
        kelly_qty = signal.get('kelly_qty')
        if kelly_qty and kelly_qty > 0:
            max_cash = float(account.buying_power) * (max_buying_power_utilization_percent / 100)
            qty = min(kelly_qty, int(max_cash / entry_price) if entry_price > 0 else kelly_qty)
        else:
            qty = self.calculate_safe_quantity(
                symbol, entry_price, stop_price, account,
                equity_risk_percent, max_buying_power_utilization_percent
            )

        if qty <= 0:
            return

        order_data = MarketOrderRequest(
            symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC,
            take_profit=TakeProfitRequest(limit_price=target_price),
            stop_loss=StopLossRequest(stop_price=stop_price)
        )
        
        try:
            order = trading_client.submit_order(order_data=order_data)
            order_id = str(order.id)[:8] if order and order.id else "unknown"
            print(f"✅ SMB Order Placed: {side} {symbol} (Qty: {qty}) @ {entry_price}. Target: {target_price}, Stop: {stop_price} | order_id={order_id} status={order.status.value if order else '?'}")
        except Exception as e:
            print(f"❌ SMB Order Failed for {symbol}: {e}")
