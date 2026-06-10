import asyncio
import json
import traceback
import pandas as pd
import numpy as np
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from sqlalchemy import text as sql_text
from .base_strategy import BaseStrategy
from .kalman_signal import KalmanTrendSignal
from .hurst_signal import HurstSignal
from .kelly_sizer import KellySizer
from config import Config
from llm_client import call_llm_with_model, LLMError, MODEL_GEMINI_FLASH

import pandas_ta as ta

class SwingStrategy(BaseStrategy):
    def __init__(self, name, ema_short=50, ema_long=200, macd_fast=12, macd_slow=26, macd_signal=9,
                 rsi_period=14, rsi_entry_low=40, rsi_entry_high=60,
                 db_engine=None, base_capital: float = 0.0,
                 drawdown_threshold_pct: float = 10.0, drawdown_window_days: int = 14,
                 min_bars: int = None):
        super().__init__(name)
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.rsi_period = rsi_period
        self.min_bars = min_bars  # overrides the formula-based minimum when set
        self.rsi_entry_low = rsi_entry_low
        self.rsi_entry_high = rsi_entry_high
        self.drawdown_threshold_pct = drawdown_threshold_pct
        self.drawdown_window_days = drawdown_window_days
        self._db_engine = db_engine
        self._kelly = KellySizer(db_engine=db_engine, base_capital=base_capital) if db_engine else None
        # Kalman noise gate — suppresses entries when noise_ratio >= 0.4 (40% of price
        # movement is unexplained noise). Q/R tuned for daily equity bars.
        self._kalman = KalmanTrendSignal(
            process_variance=1e-3,
            measurement_variance=0.1,
            signal_noise_threshold=0.4,
        )
        # Hurst regime gate — only trades when H > 0.6 (statistically trending market).
        # 60-bar warmup: first 60 rows default to H=0.5 (random walk → gate blocks).
        self._hurst = HurstSignal(rolling_window=60)

    # ── Gemini 2.5 Flash bull/bear debate gate ───────────────────────────────

    def _ensure_debate_log_table(self) -> None:
        with self._db_engine.begin() as conn:
            conn.execute(sql_text("""
                CREATE TABLE IF NOT EXISTS debate_log (
                    id             SERIAL PRIMARY KEY,
                    ticker         VARCHAR(10),
                    bull_argument  TEXT,
                    bear_argument  TEXT,
                    trade_approved BOOLEAN,
                    created_at     TIMESTAMPTZ DEFAULT NOW()
                )
            """))

    def _log_debate_sync(self, ticker: str, bull_argument: str,
                         bear_argument: str, trade_approved: bool) -> None:
        """Write one debate record to debate_log. Sync — called via asyncio.to_thread."""
        if not self._db_engine:
            return
        try:
            self._ensure_debate_log_table()
            with self._db_engine.begin() as conn:
                conn.execute(sql_text("""
                    INSERT INTO debate_log
                        (ticker, bull_argument, bear_argument, trade_approved, created_at)
                    VALUES (:ticker, :bull, :bear, :approved, NOW())
                """), {
                    "ticker":   ticker,
                    "bull":     bull_argument,
                    "bear":     bear_argument,
                    "approved": trade_approved,
                })
        except Exception as e:
            print(f"[SwingDebate] debate_log write failed for {ticker}: {e}")

    async def run_debate(self, symbol: str, signal: dict) -> tuple[bool, str]:
        """
        Sequential Gemini 2.5 Flash (thinking enabled) debate gate.
        Direction-aware: for LONG signals the bear must raise > 2 objections to block;
        for SHORT signals the bull override must raise > 2 objections to block.
        """
        _thinking = {"thinking": {"type": "enabled", "budget_tokens": 5000}}
        is_short = signal.get('signal', '') == 'sell'
        crossover_desc = (
            f"EMA{self.ema_short}/{self.ema_long} bearish configuration"
            if is_short else
            f"EMA{self.ema_short}/{self.ema_long} bullish crossover"
        )
        context = (
            f"Signal direction: {'SHORT SALE' if is_short else 'LONG BUY'} | "
            f"Symbol: {symbol} | "
            f"Price: ${float(signal.get('entry_price', 0)):.2f} | "
            f"RSI({self.rsi_period}): {signal.get('rsi_at_entry', 'N/A')} | "
            f"MACD: {signal.get('macd_at_entry', 'N/A')} | "
            f"{crossover_desc} | "
            f"Kalman noise_ratio: {signal.get('noise_ratio', 'N/A')} | "
            f"Hurst H: {signal.get('hurst', 'N/A')} | "
            f"Signal detail: {signal.get('reasoning', '')}"
        )

        primary_text = ""
        rebuttal_text = ""
        approved = True

        if is_short:
            # ── Short case (bear supports the signal) ────────────────────────
            short_text = "Short case unavailable (LLM error)"
            try:
                short_resp = await call_llm_with_model(
                    MODEL_GEMINI_FLASH,
                    (
                        f"You are a bearish equity analyst evaluating a SHORT SALE signal on {symbol}. "
                        f"A technical sell signal has fired. Make the strongest possible case that "
                        f"this bearish signal is correct and the stock should be shorted now. "
                        f"Be specific — cite at least 3 concrete reasons from the technical data.\n\nData: {context}"
                    ),
                    max_tokens=600,
                    extra_body=_thinking,
                )
                short_text = short_resp.text
            except LLMError as e:
                print(f"[SwingDebate] {symbol} short case call failed: {e} — using placeholder")

            # ── Override call (bull must raise 2+ concrete fundamental/macro reasons to block) ──
            override_text = "Override case unavailable (LLM error)"
            override_objections: list[str] = []
            override_summary = "Override analysis unavailable"
            try:
                override_resp = await call_llm_with_model(
                    MODEL_GEMINI_FLASH,
                    (
                        f"A technical SELL signal has fired for {symbol} with the following bearish "
                        f"evidence: {context}. You are a bullish analyst. Make a compelling case for "
                        f"why this bearish signal should be IGNORED and the stock will rise. "
                        f"Provide only concrete fundamental or macro reasons — vague optimism does not count. "
                        f"Rebut the short case where you can.\n\n"
                        f"Short case to rebut:\n{short_text[:500]}\n\n"
                        "Respond with JSON only: "
                        '{"objections": ["<concrete fundamental/macro reason 1>", "<reason 2>", ...], "summary": "<one sentence>"}'
                    ),
                    response_format={"type": "json_object"},
                    max_tokens=600,
                    extra_body=_thinking,
                )
                override_text = override_resp.text
                parsed = json.loads(override_resp.text)
                override_objections = [str(o) for o in parsed.get("objections", []) if o]
                override_summary = parsed.get("summary", override_resp.text[:200])
            except LLMError as e:
                print(f"[SwingDebate] {symbol} override call failed: {e} — defaulting to 0 objections")
            except Exception as e:
                print(f"[SwingDebate] {symbol} override JSON parse failed: {e} — defaulting to 0 objections")

            # Short proceeds unless bull raises 2+ concrete fundamental/macro override reasons
            approved = len(override_objections) < 2
            n = len(override_objections)
            objection_lines = "\n".join(f"  {i+1}. {o}" for i, o in enumerate(override_objections))
            summary = (
                f"*Short case:* {short_text[:400]}\n"
                f"*Bull override ({n} reason{'s' if n != 1 else ''}):* {override_summary}"
            )
            if not approved:
                summary += f"\n*Override reasons that blocked the short:*\n{objection_lines}"
            summary += f"\n*Verdict:* {'✅ SHORT PROCEEDS (bull override failed)' if approved else '🚫 SHORT BLOCKED (bull raised 2+ concrete override reasons)'}"
            if approved:
                print(f"[Debate] SHORT {symbol} — bull override FAILED ({n} reason(s)) — short proceeds")
            else:
                print(f"[Debate] SHORT {symbol} — bull override SUCCEEDED ({n} reason(s)) — short blocked")
            primary_text, rebuttal_text = short_text, override_text

        else:
            # ── Bull call ────────────────────────────────────────────────────
            bull_text = "Bull case unavailable (LLM error)"
            try:
                bull_resp = await call_llm_with_model(
                    MODEL_GEMINI_FLASH,
                    (
                        f"You are a bullish equity analyst. Make the strongest possible case FOR buying "
                        f"{symbol} right now. Be specific — cite at least 3 concrete reasons from "
                        f"the technical data provided.\n\nData: {context}"
                    ),
                    max_tokens=600,
                    extra_body=_thinking,
                )
                bull_text = bull_resp.text
            except LLMError as e:
                print(f"[SwingDebate] {symbol} bull call failed: {e} — using placeholder")

            # ── Bear call (sequential — explicitly rebutting bull) ────────────
            bear_text = "Bear case unavailable (LLM error)"
            bear_objections: list[str] = []
            bear_summary = "Bear analysis unavailable"
            try:
                bear_resp = await call_llm_with_model(
                    MODEL_GEMINI_FLASH,
                    (
                        f"You are a bearish equity analyst. Identify concrete risks AGAINST buying "
                        f"{symbol} right now. Rebut the bull case where you can.\n\n"
                        f"Data: {context}\n\nBull case to rebut:\n{bull_text[:500]}\n\n"
                        "Respond with JSON only: "
                        '{"objections": ["<risk 1>", "<risk 2>", ...], "summary": "<one sentence>"}'
                    ),
                    response_format={"type": "json_object"},
                    max_tokens=600,
                    extra_body=_thinking,
                )
                bear_text = bear_resp.text
                parsed = json.loads(bear_resp.text)
                bear_objections = [str(o) for o in parsed.get("objections", []) if o]
                bear_summary = parsed.get("summary", bear_resp.text[:200])
            except LLMError as e:
                print(f"[SwingDebate] {symbol} bear call failed: {e} — defaulting to 0 objections")
            except Exception as e:
                print(f"[SwingDebate] {symbol} bear JSON parse failed: {e} — defaulting to 0 objections")

            approved = len(bear_objections) <= 2
            n = len(bear_objections)
            objection_lines = "\n".join(f"  {i+1}. {o}" for i, o in enumerate(bear_objections))
            summary = (
                f"*Bull:* {bull_text[:400]}\n"
                f"*Bear ({n} objection{'s' if n != 1 else ''}):* {bear_summary}"
            )
            if not approved:
                summary += f"\n*Objections that blocked the trade:*\n{objection_lines}"
            summary += f"\n*Verdict:* {'✅ APPROVED' if approved else '🚫 BLOCKED (>2 bear objections)'}"
            print(f"[SwingDebate] {symbol}: {n} bear objection(s) → {'APPROVED' if approved else 'BLOCKED'}")
            primary_text, rebuttal_text = bull_text, bear_text

        if self._db_engine:
            await asyncio.to_thread(
                self._log_debate_sync, symbol, primary_text, rebuttal_text, approved
            )

        return approved, summary

    # ── Candlestick confirmation ─────────────────────────────────────────────

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
        try:
            _required = self.min_bars if self.min_bars is not None else (
                self.ema_long + self.macd_slow + self.macd_signal + self.rsi_period
            )
            _actual   = 0 if market_data is None else len(market_data)
            if market_data is None or _actual < _required:
                print(f"[SwingVerbose] {self.name}: insufficient data ({_actual} bars, need {_required})")
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

            last_ema_short   = df['EMA_short'].iloc[-1]
            last_ema_long    = df['EMA_long'].iloc[-1]
            last_macd        = df['MACD'].iloc[-1]
            last_macd_signal = df['MACD_Signal'].iloc[-1]
            last_rsi         = df['RSI'].iloc[-1]
            current_price    = df['close'].iloc[-1]

            if Config.SWING_VERBOSE_LOGGING:
                print(
                    f"[SwingVerbose] {self.name}: price={current_price:.2f} | "
                    f"EMA{self.ema_short}={last_ema_short:.2f} EMA{self.ema_long}={last_ema_long:.2f} | "
                    f"MACD={last_macd:.4f} MACDsig={last_macd_signal:.4f} | "
                    f"RSI({self.rsi_period})={last_rsi:.2f} (gate [{self.rsi_entry_low},{self.rsi_entry_high}])"
                )

            # Adaptive signal gates — compute once per evaluation
            k = self._kalman.compute_latest(df['close'])
            h = self._hurst.compute_latest(df['close'])
            noise_ok = k["noise_ratio"] < 0.4
            hurst_ok = h["regime_code"] == 1  # H > 0.6 → statistically trending

            # Decompose entry conditions for clean logging
            ema_ok  = last_ema_short > last_ema_long
            macd_ok = (last_macd > last_macd_signal and
                       df['MACD'].iloc[-2] <= df['MACD_Signal'].iloc[-2])
            rsi_ok  = self.rsi_entry_low <= last_rsi <= self.rsi_entry_high

            signal     = None
            confidence = 0.0

            # Entry: EMA crossover + MACD + RSI + Kalman noise gate + Hurst regime gate
            if ema_ok and macd_ok and rsi_ok and noise_ok and hurst_ok:
                signal = "buy"
                confidence = 0.7
                reasoning = (
                    f"EMA{self.ema_short}({last_ema_short:.2f}) > EMA{self.ema_long}({last_ema_long:.2f}), "
                    f"MACD Cross Up, RSI({last_rsi:.2f}) in [{self.rsi_entry_low},{self.rsi_entry_high}], "
                    f"Kalman noise={k['noise_ratio']:.2f}, Hurst H={h['hurst']:.3f}"
                )
            elif ema_ok and macd_ok and rsi_ok and noise_ok and not hurst_ok:
                if Config.SWING_VERBOSE_LOGGING:
                    print(
                        f"[SwingVerbose] {self.name}: BUY suppressed by Hurst regime gate "
                        f"(H={h['hurst']:.3f} regime={h['regime']} — not trending)"
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
                stop_loss_price   = current_price * (1 - (Config.STOP_LOSS_PERCENT / 100))
                take_profit_price = current_price * (1 + (Config.TAKE_PROFIT_PERCENT / 100))

                # Enforce 1:2 R/R Check
                risk   = current_price - stop_loss_price
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
                    "prev_close": float(df["close"].iloc[-2]),
                    "stop_price": stop_loss_price,
                    "target_price": take_profit_price,
                    "rsi_at_entry": float(last_rsi),
                    "macd_at_entry": float(last_macd),
                    "noise_ratio": round(float(k["noise_ratio"]), 3),
                    "hurst": round(float(h["hurst"]), 3),
                    "reasoning": reasoning
                }
            elif signal == "sell":
                return {
                    "symbol": market_data["symbol"].iloc[-1] if "symbol" in market_data.columns else "UNKNOWN",
                    "signal": signal,
                    "confidence": confidence,
                    "entry_price": current_price,
                    "reasoning": reasoning
                }
            return None
        except Exception:
            print(f"[SwingVerbose] {self.name}: generate_signals() raised an exception:\n{traceback.format_exc()}")
            return None

    def execute_trade(self, signal, trading_client, equity_risk_percent, stop_loss_percent, take_profit_percent, max_buying_power_utilization_percent):
        if not signal:
            return

        symbol = signal["symbol"]
        
        # Handle Exit Signals
        if signal["signal"] == "sell":
            try:
                trading_client.get_open_position(symbol)
            except Exception:
                print(f"[DEBUG] Swing sell skipped for {symbol} — no open position.")
                return
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

        # 2. SAFETY LOCK: Calculate Quantity
        # Kelly pre-computed in _process_symbol when sufficient history exists;
        # falls back to risk-based sizing when below MIN_SAMPLE_SIZE.
        kelly_qty = signal.get('kelly_qty')
        if kelly_qty and kelly_qty > 0:
            max_cash = float(account.buying_power) * (max_buying_power_utilization_percent / 100)
            qty = min(kelly_qty, int(max_cash / entry_price) if entry_price > 0 else kelly_qty)
        else:
            qty = self.calculate_safe_quantity(
                symbol, entry_price, signal["stop_price"], account,
                equity_risk_percent, max_buying_power_utilization_percent
            )

        if qty <= 0:
            print(
                f"[Swing] {symbol}: qty=0 — skipped "
                f"(price={entry_price:.2f} equity={float(account.equity):.0f} "
                f"risk_pct={equity_risk_percent:.3f}% bp={float(account.buying_power):.0f})"
            )
            return

        # 3. Place Order
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(signal["target_price"], 2)),
            stop_loss=StopLossRequest(stop_price=round(signal["stop_price"], 2)),
        )
        
        _base = getattr(getattr(trading_client, '_base_url', None), 'host', None) \
                or getattr(trading_client, '_base_url', 'unknown')
        print(f"[ORDER] Submitting Swing order → {symbol} {qty} {side.value} | endpoint={_base}")
        try:
            order = trading_client.submit_order(order_data=order_data)
            sl_price = signal["stop_price"]
            tp_price = signal["target_price"]
            order_id = str(order.id)[:8] if order and order.id else "unknown"
            print(f"[ORDER] Alpaca response: id={order.id} status={order.status.value if order else '?'} symbol={getattr(order,'symbol','?')} qty={getattr(order,'qty','?')}")
            print(f"✅ Swing Order Placed: {side} {symbol} (Qty: {qty}) @ {entry_price}. SL: {sl_price}, TP: {tp_price} | order_id={order_id} status={order.status.value if order else '?'}")
            return qty
        except Exception:
            print(f"❌ Swing Order Failed for {symbol}:\n{traceback.format_exc()}")
