import traceback
import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
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
        # Q=5e-3 / R=0.1 calibrated for 1-minute intraday bars — reacts faster than
        # the daily Q=1e-3 setting to keep up with intraday crypto price action.
        self._kalman = KalmanTrendSignal(process_variance=5e-3, measurement_variance=0.1)
        # AnchoredVWAPSignal: entry filter for 1-minute intraday crypto data.
        # distance_threshold_pct=0.15: loosened from 0.3 — intraday crypto moves
        #   away from VWAP by smaller percentages than daily data.
        # volume_ratio_threshold=1.1: loosened from 1.2 — 10% above-average is
        #   sufficient confirmation at 1-minute resolution.
        self._avwap = AnchoredVWAPSignal(
            window=20,
            anchor="rolling",
            distance_threshold_pct=0.15,
            volume_ratio_threshold=1.1,
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
        df.sort_index(inplace=True)

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
        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL

        # 1. SAFETY CHECK: position guard (buy = avoid doubling up; sell = must have position)
        if side == OrderSide.SELL:
            try:
                trading_client.get_open_position(symbol)
            except Exception:
                print(f"[DEBUG] SMB sell skipped for {symbol} — no open position.")
                return
        elif self.is_already_in_position(symbol, trading_client):
            return
        entry_price, stop_price, target_price = signal["entry_price"], signal["stop_price"], signal["target_price"]

        account = trading_client.get_account()
        if not account:
            return
            
        # 2. SAFETY LOCK: Calculate Quantity
        # Kelly pre-computed in _process_symbol when sufficient history exists;
        # falls back to risk-based sizing when below MIN_SAMPLE_SIZE.
        kelly_qty = signal.get('kelly_qty')
        _adv_cap = signal.get('adv_cap_shares')
        if kelly_qty and kelly_qty > 0:
            max_cash = float(account.buying_power) * (max_buying_power_utilization_percent / 100)
            qty = min(kelly_qty, int(max_cash / entry_price) if entry_price > 0 else kelly_qty)
            if _adv_cap and qty > _adv_cap:
                adv_raw = int(_adv_cap / 0.01)
                print(
                    f"[Sizing] {symbol} ADV cap applied — requested {qty} shares, "
                    f"capped to {_adv_cap} (1% of ADV={adv_raw})"
                )
                qty = _adv_cap
        else:
            qty = self.calculate_safe_quantity(
                symbol, entry_price, stop_price, account,
                equity_risk_percent, max_buying_power_utilization_percent,
                adv_cap_shares=_adv_cap,
            )

        if qty <= 0:
            print(f"[SMB] {symbol}: qty=0 — skipped (price={entry_price:.4f})")
            return

        import time as _time
        _base = getattr(getattr(trading_client, '_base_url', None), 'host', None) \
                or getattr(trading_client, '_base_url', 'unknown')
        print(f"[ORDER] Submitting SMB order → {symbol} {qty} {side.value} | endpoint={_base}")
        try:
            # Step 1: plain market entry — Alpaca does not support BRACKET on market orders
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC,
            ))
            print(f"[ORDER] Alpaca response: id={order.id} status={order.status.value if order else '?'} symbol={getattr(order,'symbol','?')} qty={getattr(order,'qty','?')}")

            # Step 2: poll for fill (max 30 s)
            fill_price = None
            for _i in range(30):
                _time.sleep(1)
                _checked = trading_client.get_order_by_id(order.id)
                if getattr(_checked, 'status', None) and _checked.status.value == 'filled':
                    fill_price = float(_checked.filled_avg_price or entry_price)
                    break
            if fill_price is None:
                print(f"[ORDER] {symbol}: fill not confirmed in 30s — using signal entry price for OCO")
                fill_price = float(entry_price)

            # Recompute stop/target from actual fill price
            if side == OrderSide.BUY:
                actual_stop   = round(fill_price * (1 - stop_loss_percent / 100), 4)
                actual_target = round(fill_price * (1 + take_profit_percent / 100), 4)
                oco_side = OrderSide.SELL
            else:
                actual_stop   = round(fill_price * (1 + stop_loss_percent / 100), 4)
                actual_target = round(fill_price * (1 - take_profit_percent / 100), 4)
                oco_side = OrderSide.BUY

            print(
                f"✅ SMB {'LONG' if side == OrderSide.BUY else 'SHORT'} entered: "
                f"{symbol} qty={qty} fill={fill_price:.4f} stop={actual_stop} target={actual_target}"
            )

            # Step 3: GTC OCO protection
            _oco_error = None
            try:
                oco = trading_client.submit_order(LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=oco_side,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.OCO,
                    limit_price=actual_target,
                    take_profit=TakeProfitRequest(limit_price=actual_target),
                    stop_loss=StopLossRequest(stop_price=actual_stop),
                ))
                print(
                    f"[ORDER] {symbol}: OCO protection placed — "
                    f"id={str(oco.id)[:12]} target={actual_target} stop={actual_stop}"
                )
            except Exception as _oco_e:
                print(f"[ORDER] {symbol}: OCO FAILED — {_oco_e}\n{traceback.format_exc()}")
                _oco_error = str(_oco_e)
            return (qty, _oco_error)
        except Exception:
            print(f"❌ SMB Order Failed for {symbol}:\n{traceback.format_exc()}")
