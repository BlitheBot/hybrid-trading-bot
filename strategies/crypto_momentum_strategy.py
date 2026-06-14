"""
Crypto momentum scalp (Task 6).

A simpler, higher-frequency companion to the SMB late-scalp strategy. SMB's
Kalman/AnchoredVWAP gate fires rarely even on 1-minute bars; this strategy uses a
plain 9/21 EMA crossover so it generates signals more often, while still being
risk-controlled (ATR stop/target, volume confirmation, per-symbol cooldown).

Entry (BTC/USD, ETH/USD; 1-minute bars):
    * long  when EMA9 crosses **above** EMA21
    * short when EMA9 crosses **below** EMA21
Confirmation:
    * current bar volume > ``vol_mult`` (1.2x) × average volume of the last 20 bars
Risk:
    * stop  = 1.5 × ATR(14) from entry, target = 3.0 × ATR(14)  => R/R = 2.0
Throttles:
    * minimum 0.1% price move since the last signal for the symbol (anti-churn)
    * 15-minute cooldown between signals per symbol

Execution mirrors the fixed SMB two-step submission: a plain market entry, poll for
the fill, then a GTC OCO (take-profit + stop-loss) sized off the actual fill price.
"""
import time as _time
import traceback
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytz
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

from .base_strategy import BaseStrategy
from config import Config


class CryptoMomentumStrategy(BaseStrategy):
    def __init__(self, name, ema_fast=None, ema_slow=None, rr_ratio=2.0):
        super().__init__(name)
        self.ema_fast = ema_fast or Config.CRYPTO_MOMENTUM_EMA_FAST
        self.ema_slow = ema_slow or Config.CRYPTO_MOMENTUM_EMA_SLOW
        self.rr_ratio = rr_ratio
        # Per-symbol throttle state: {symbol: {"time": dt, "price": float}}
        self._last_signal: dict[str, dict] = {}

    def _throttled(self, symbol: str, price: float) -> bool:
        """True if a signal for ``symbol`` is suppressed by cooldown or min-move."""
        last = self._last_signal.get(symbol)
        if not last:
            return False
        now = datetime.now(pytz.utc)
        if now - last["time"] < timedelta(minutes=Config.CRYPTO_MOMENTUM_COOLDOWN_MINUTES):
            print(f"[CryptoMom] {symbol}: cooldown active — signal suppressed")
            return True
        if last["price"] > 0:
            move = abs(price - last["price"]) / last["price"]
            if move < Config.CRYPTO_MOMENTUM_MIN_MOVE_PCT:
                print(f"[CryptoMom] {symbol}: move {move:.4%} < "
                      f"{Config.CRYPTO_MOMENTUM_MIN_MOVE_PCT:.2%} min — signal suppressed")
                return True
        return False

    def generate_signals(self, market_data, stock_data_client=None):
        if market_data is None or len(market_data) < max(self.ema_slow + 2, 30):
            return None
        df = market_data.copy()
        df.sort_index(inplace=True)

        ema_f = ta.ema(df["close"], length=self.ema_fast)
        ema_s = ta.ema(df["close"], length=self.ema_slow)
        atr = ta.atr(high=df["high"], low=df["low"], close=df["close"], length=14)
        if ema_f is None or ema_s is None or atr is None:
            return None
        if pd.isna(ema_f.iloc[-2:]).any() or pd.isna(ema_s.iloc[-2:]).any() or pd.isna(atr.iloc[-1]):
            return None

        symbol = str(df["symbol"].iloc[-1]) if "symbol" in df.columns else "UNKNOWN"
        curr_f, prev_f = float(ema_f.iloc[-1]), float(ema_f.iloc[-2])
        curr_s, prev_s = float(ema_s.iloc[-1]), float(ema_s.iloc[-2])
        price = float(df["close"].iloc[-1])
        atr_val = float(atr.iloc[-1])

        signal = None
        if prev_f <= prev_s and curr_f > curr_s:
            signal = "buy"
        elif prev_f >= prev_s and curr_f < curr_s:
            signal = "sell"
        if signal is None:
            return None

        # Volume confirmation: current bar > vol_mult × avg of last 20 bars.
        vol = df["volume"]
        avg_vol = float(vol.iloc[-21:-1].mean()) if len(vol) >= 21 else float(vol.mean())
        cur_vol = float(vol.iloc[-1])
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0.0
        if vol_ratio < Config.CRYPTO_MOMENTUM_VOL_MULT:
            print(f"[CryptoMom] {symbol}: {signal.upper()} suppressed — volume "
                  f"{vol_ratio:.2f}x < {Config.CRYPTO_MOMENTUM_VOL_MULT}x")
            return None

        if self._throttled(symbol, price):
            return None
        if atr_val <= 0:
            return None

        stop_dist = atr_val * Config.CRYPTO_MOMENTUM_ATR_STOP_MULT
        target_dist = atr_val * Config.CRYPTO_MOMENTUM_ATR_TARGET_MULT
        if signal == "buy":
            stop_price = price - stop_dist
            target_price = price + target_dist
        else:
            stop_price = price + stop_dist
            target_price = price - target_dist

        # Confidence rises with volume surge and EMA spread (used to break ties vs SMB).
        spread = abs(curr_f - curr_s) / price if price > 0 else 0.0
        confidence = float(min(0.95, 0.6 + 0.15 * (vol_ratio - Config.CRYPTO_MOMENTUM_VOL_MULT) + 50.0 * spread))

        # Record throttle state at signal time (covers debate/gate rejections too —
        # anti-churn should count the signal regardless of downstream execution).
        self._last_signal[symbol] = {"time": datetime.now(pytz.utc), "price": price}

        return {
            "symbol": symbol, "signal": signal, "confidence": round(confidence, 3),
            "entry_price": price, "stop_price": stop_price, "target_price": target_price,
            "reasoning": (
                f"EMA{self.ema_fast}/{self.ema_slow} {'bull' if signal == 'buy' else 'bear'} cross + "
                f"vol {vol_ratio:.2f}x. ATR(14)={atr_val:.4f} stop=1.5×ATR target=3×ATR (R/R 2.0)"
            ),
        }

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent,
                      take_profit_percent, max_buying_power_utilization_percent):
        if not signal:
            return
        symbol = signal["symbol"]
        side = OrderSide.BUY if signal["signal"] == "buy" else OrderSide.SELL

        if side == OrderSide.SELL:
            try:
                trading_client.get_open_position(symbol)
            except Exception:
                print(f"[CryptoMom] sell skipped for {symbol} — no open position.")
                return
        elif self.is_already_in_position(symbol, trading_client):
            return

        entry_price = signal["entry_price"]
        stop_price = signal["stop_price"]
        account = trading_client.get_account()
        if not account:
            return

        _adv_cap = signal.get("adv_cap_shares")
        kelly_qty = signal.get("kelly_qty")
        if kelly_qty and kelly_qty > 0:
            max_cash = float(account.buying_power) * (max_buying_power_utilization_percent / 100)
            qty = min(kelly_qty, int(max_cash / entry_price) if entry_price > 0 else kelly_qty)
        else:
            qty = self.calculate_safe_quantity(
                symbol, entry_price, stop_price, account,
                equity_risk_percent, max_buying_power_utilization_percent,
                adv_cap_shares=_adv_cap,
            )
        if qty <= 0:
            print(f"[CryptoMom] {symbol}: qty=0 — skipped (price={entry_price:.4f})")
            return

        print(f"[ORDER] Submitting CryptoMom order → {symbol} {qty} {side.value}")
        try:
            # Step 1: plain market entry (Alpaca rejects BRACKET on market orders).
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC,
            ))
            print(f"[ORDER] Alpaca response: id={order.id} "
                  f"status={order.status.value if order else '?'}")

            # Step 2: poll for fill (max 30 s).
            fill_price = None
            for _i in range(30):
                _time.sleep(1)
                _checked = trading_client.get_order_by_id(order.id)
                if getattr(_checked, "status", None) and _checked.status.value == "filled":
                    fill_price = float(_checked.filled_avg_price or entry_price)
                    break
            if fill_price is None:
                print(f"[ORDER] {symbol}: fill not confirmed in 30s — using signal entry for OCO")
                fill_price = float(entry_price)

            if side == OrderSide.BUY:
                actual_stop = round(fill_price * (1 - stop_loss_percent / 100), 4)
                actual_target = round(fill_price * (1 + take_profit_percent / 100), 4)
                oco_side = OrderSide.SELL
            else:
                actual_stop = round(fill_price * (1 + stop_loss_percent / 100), 4)
                actual_target = round(fill_price * (1 - take_profit_percent / 100), 4)
                oco_side = OrderSide.BUY

            print(f"✅ CryptoMom {'LONG' if side == OrderSide.BUY else 'SHORT'} entered: "
                  f"{symbol} qty={qty} fill={fill_price:.4f} stop={actual_stop} target={actual_target}")

            # Step 3: GTC OCO protection.
            try:
                oco = trading_client.submit_order(LimitOrderRequest(
                    symbol=symbol, qty=qty, side=oco_side,
                    time_in_force=TimeInForce.GTC, order_class=OrderClass.OCO,
                    limit_price=actual_target,
                    take_profit=TakeProfitRequest(limit_price=actual_target),
                    stop_loss=StopLossRequest(stop_price=actual_stop),
                ))
                print(f"[ORDER] {symbol}: OCO protection placed — id={str(oco.id)[:12]} "
                      f"target={actual_target} stop={actual_stop}")
            except Exception as _oco_e:
                print(f"[ORDER] {symbol}: OCO FAILED — {_oco_e}\n{traceback.format_exc()}")
            return qty
        except Exception:
            print(f"❌ CryptoMom Order Failed for {symbol}:\n{traceback.format_exc()}")
