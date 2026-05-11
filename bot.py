import os
import time
import asyncio
import threading
from datetime import datetime, timedelta
import pytz
import pandas as pd
from flask import Flask, jsonify
import notifications
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

import anthropic
import requests as _requests

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import CryptoDataStream

from sqlalchemy import create_engine, text as sql_text

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.smb_strategy import SMBStrategy
from strategies.swing_strategy import SwingStrategy
from strategies.news_strategy import NewsStrategy, _get_scan_sleep_seconds
from strategies.truth_social_strategy import TruthSocialStrategy
from strategies.sec_edgar_strategy import SECEdgarStrategy
from strategies.congressional_trading_strategy import CongressionalTradingStrategy
from utils import get_historical_bars, get_finnhub_price

# ── Flask Health Endpoint ────────────────────────────────────────────
_health_app = Flask(__name__)
_bot_start_time = datetime.now(pytz.utc)

# Updated by bot loops so the /health endpoint reflects live state
_health_state: dict = {
    "db_connected": False,
    "alpaca_connected": False,
    "last_news_scan_utc": None,
    "last_edgar_scan_utc": None,
    "websocket_connected": False,
}

@_health_app.route("/health", methods=["GET"])
def health_check():
    uptime_seconds = (datetime.now(pytz.utc) - _bot_start_time).total_seconds()
    return jsonify({
        "status": "running",
        "uptime_seconds": round(uptime_seconds, 2),
        "started_at": _bot_start_time.isoformat(),
        "db_connected": _health_state["db_connected"],
        "alpaca_connected": _health_state["alpaca_connected"],
        "last_news_scan": _health_state["last_news_scan_utc"],
        "last_edgar_scan": _health_state["last_edgar_scan_utc"],
        "websocket_connected": _health_state["websocket_connected"],
    }), 200

def start_health_server(port=8502):
    """Run the Flask health server in a daemon thread so it never blocks the bot."""
    thread = threading.Thread(
        target=lambda: _health_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True
    )
    thread.start()
    print(f"🩺 Health endpoint running on http://0.0.0.0:{port}/health")


class TradingBot:
    def __init__(self):
        print("DEBUG: Initializing TradingBot...")
        
        # Determine Base URL
        base_url = "https://paper-api.alpaca.markets" if Config.PAPER_TRADING else "https://api.alpaca.markets"
        print(f"DEBUG: Using Base URL: {base_url}")

        # Explicitly passing None for oauth_token to ensure no conflict
        self.trading_client = TradingClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY, 
            paper=Config.PAPER_TRADING,
            url_override=base_url
        )
        self.stock_data_client = StockHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY
        )
        self.crypto_data_client = CryptoHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY
        )
        
        # FIX: CryptoDataStream does not take a 'paper' argument in some SDK versions.
        # It determines the environment from the keys or uses a default.
        self.crypto_stream = CryptoDataStream(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY
        )
        self.scalp_strategies = []
        self.swing_strategies = []
        self.swing_symbol_strategies: dict[str, SwingStrategy] = {}
        self._open_trade_ids: dict = {}       # symbol → (row_id, entry_price, entry_time)
        self._trade_ids_lock = asyncio.Lock() # guards all _open_trade_ids mutations
        self._db_engine = self._init_db_engine()
        self._regime_cache = None             # (regime_str, timestamp)
        self._claude = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.daily_pnl = 0.0
        self.start_of_day_equity = 0.0
        self.last_pnl_reset_date = datetime.now(pytz.timezone('America/New_York')).date()
        self.trading_halted_for_day = False
        self.risk_multiplier = 1.0
        self.active_signals = {}
        self.last_loss_times = {}
        self.last_evaluated_price = {}

    def add_scalp_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.scalp_strategies.append(strategy)

    def add_swing_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.swing_strategies.append(strategy)

    async def _check_account_status(self):
        print("DEBUG: Fetching account details from Alpaca...")
        try:
            account = await asyncio.to_thread(self.trading_client.get_account)
            if account:
                print(f"Account Status: {account.status}, Equity: ${float(account.equity):,.2f}, Buying Power: ${float(account.buying_power):,.2f}")
                
                current_date = datetime.now(pytz.timezone('America/New_York')).date()
                if current_date != self.last_pnl_reset_date:
                    self.daily_pnl = 0.0
                    self.start_of_day_equity = float(account.equity)
                    self.last_pnl_reset_date = current_date
                    self.trading_halted_for_day = False
                    print(f"DEBUG: Daily PnL reset for {current_date}. Starting equity: ${self.start_of_day_equity:,.2f}")
                
                if self.start_of_day_equity == 0.0:
                    self.start_of_day_equity = float(account.equity)

                current_daily_pnl = float(account.equity) - self.start_of_day_equity
                self.risk_multiplier = 1.0
                if current_daily_pnl < 0:
                    current_daily_loss_percent = (abs(current_daily_pnl) / self.start_of_day_equity) * 100
                    if current_daily_loss_percent >= Config.MAX_DAILY_LOSS_PERCENT:
                        if not self.trading_halted_for_day:
                            self.trading_halted_for_day = True
                            msg = f"CRITICAL: Max daily loss of {Config.MAX_DAILY_LOSS_PERCENT}% hit! Trading halted for the day."
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg, level="CRITICAL"))
                    elif current_daily_loss_percent >= Config.DAILY_LOSS_REDUCTION_2_PERCENT:
                        self.risk_multiplier = 0.50
                    elif current_daily_loss_percent >= Config.DAILY_LOSS_REDUCTION_1_PERCENT:
                        self.risk_multiplier = 0.75
                
                self.daily_pnl = current_daily_pnl
                _health_state["alpaca_connected"] = True
                return True
            return False
        except Exception as e:
            _health_state["alpaca_connected"] = False
            msg = f"Error checking account status: {e}"
            print(msg)
            asyncio.create_task(notifications.notify_alert(msg))
            return False

    async def _update_loss_cache(self):
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                limit=50,
                after=datetime.now(pytz.utc) - timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES)
            )
            orders = await asyncio.to_thread(self.trading_client.get_orders, req)
            for order in orders:
                if order.status.value == "filled" and (order.order_type.value == "stop" or order.order_type.value == "trailing_stop"):
                    self.last_loss_times[order.symbol] = order.filled_at
        except Exception as e:
            print(f"Failed to update loss cache: {e}")

    # ── Database helpers (SQLAlchemy) ─────────────────────────────────────────

    def _init_db_engine(self):
        url = Config.DATABASE_URL
        if not url:
            return None
        try:
            engine = create_engine(url, pool_pre_ping=True)
            return engine
        except Exception as e:
            print(f"[DB] Engine creation failed: {e}")
            return None

    def _ensure_signal_outcomes_table(self):
        if not self._db_engine:
            return
        try:
            with self._db_engine.begin() as conn:
                conn.execute(sql_text("""
                    CREATE TABLE IF NOT EXISTS signal_outcomes (
                        id            SERIAL PRIMARY KEY,
                        symbol        VARCHAR(10),
                        signal_type   VARCHAR(20),
                        entry_time    TIMESTAMP,
                        exit_time     TIMESTAMP,
                        entry_price   FLOAT,
                        exit_price    FLOAT,
                        pnl_pct       FLOAT,
                        hold_bars     INTEGER,
                        ema_short     INTEGER,
                        ema_long      INTEGER,
                        rsi_at_entry  FLOAT,
                        macd_at_entry FLOAT,
                        market_regime VARCHAR(20),
                        exit_reason   VARCHAR(30),
                        discovered_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                count = conn.execute(sql_text("SELECT COUNT(*) FROM signal_outcomes")).scalar()
            _health_state["db_connected"] = True
            print(f"[DB] signal_outcomes table verified — {count} existing rows")
        except Exception as e:
            _health_state["db_connected"] = False
            print(f"[DB] Table setup failed: {e}")

    def _log_trade_entry(self, symbol: str, signal_type: str, entry_price: float,
                          ema_short: int, ema_long: int, rsi_at_entry: float,
                          macd_at_entry: float, regime: str, entry_time) -> int | None:
        if not self._db_engine:
            return None
        try:
            with self._db_engine.begin() as conn:
                result = conn.execute(sql_text("""
                    INSERT INTO signal_outcomes
                        (symbol, signal_type, entry_time, entry_price, ema_short, ema_long,
                         rsi_at_entry, macd_at_entry, market_regime)
                    VALUES (:symbol, :signal_type, :entry_time, :entry_price, :ema_short, :ema_long,
                            :rsi_at_entry, :macd_at_entry, :market_regime)
                    RETURNING id
                """), {
                    "symbol": symbol, "signal_type": signal_type, "entry_time": entry_time,
                    "entry_price": float(entry_price), "ema_short": int(ema_short),
                    "ema_long": int(ema_long), "rsi_at_entry": float(rsi_at_entry),
                    "macd_at_entry": float(macd_at_entry), "market_regime": regime,
                })
                row_id = result.fetchone()[0]
            print(f"[DB] Logged {signal_type} entry for {symbol} (row={row_id})")
            return row_id
        except Exception as e:
            print(f"[DB] Entry log failed for {symbol}: {e}")
            return None

    def _update_trade_exit(self, row_id: int, exit_price: float, exit_reason: str,
                            exit_time, hold_bars: int, pnl_pct: float):
        if not self._db_engine:
            return
        try:
            with self._db_engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE signal_outcomes
                    SET exit_time=:exit_time, exit_price=:exit_price, pnl_pct=:pnl_pct,
                        hold_bars=:hold_bars, exit_reason=:exit_reason
                    WHERE id=:id
                """), {
                    "exit_time": exit_time, "exit_price": float(exit_price),
                    "pnl_pct": float(pnl_pct), "hold_bars": int(hold_bars),
                    "exit_reason": exit_reason, "id": row_id,
                })
            print(f"[DB] Exit logged row={row_id}: {exit_reason} @ {exit_price:.2f} ({pnl_pct:+.2f}%)")
        except Exception as e:
            print(f"[DB] Exit update failed row={row_id}: {e}")

    # ── Market regime (Task 1) ────────────────────────────────────────────────

    async def _get_market_regime(self) -> str:
        if self._regime_cache is not None:
            regime, ts = self._regime_cache
            if time.time() - ts < Config.MARKET_REGIME_CACHE_SECONDS:
                return regime
        try:
            bars = await asyncio.to_thread(
                get_historical_bars, "SPY", TimeFrame.Day, 210, self.stock_data_client, False
            )
            if bars is not None and len(bars) >= 200:
                spy_close  = float(bars['close'].iloc[-1])
                spy_ema200 = float(bars['close'].ewm(span=200, adjust=False).mean().iloc[-1])
                regime = 'bull' if spy_close > spy_ema200 else 'bear'
            else:
                regime = 'neutral'
        except Exception as e:
            print(f"[MarketRegime] Failed: {e}")
            regime = 'neutral'
        self._regime_cache = (regime, time.time())
        return regime

    # ── Fundamentals check (Task 3) ───────────────────────────────────────────

    async def _check_fundamentals(self, symbol: str) -> tuple[bool, str | None]:
        try:
            api_key = Config.FINNHUB_API_KEY
            if not api_key:
                return True, None

            base = "https://finnhub.io/api/v1"
            today = datetime.now(pytz.timezone("America/New_York")).date()

            def _fetch_metrics():
                return _requests.get(
                    f"{base}/stock/metric",
                    params={"symbol": symbol, "metric": "all", "token": api_key},
                    timeout=10,
                ).json()

            metrics = await asyncio.to_thread(_fetch_metrics)
            m = metrics.get("metric", {})

            pe = m.get("peBasicExclExtraTTM")
            if pe is not None and float(pe) < 0:
                return False, f"Negative P/E ({float(pe):.1f}) — company not profitable"

            eps_list = metrics.get("series", {}).get("annual", {}).get("eps", [])
            if len(eps_list) >= 2:
                recent = eps_list[-1].get("v") or 0
                prior  = eps_list[-2].get("v") or 1
                if prior != 0:
                    growth_pct = (recent - prior) / abs(prior) * 100
                    if growth_pct < -20:
                        return False, f"EPS declined {growth_pct:.1f}% YoY"

            def _fetch_calendar():
                return _requests.get(
                    f"{base}/calendar/earnings",
                    params={
                        "from":   str(today),
                        "to":     str(today + timedelta(days=2)),
                        "symbol": symbol,
                        "token":  api_key,
                    },
                    timeout=10,
                ).json()

            cal    = await asyncio.to_thread(_fetch_calendar)
            events = cal.get("earningsCalendar", [])
            if events:
                report_date = events[0].get("date", "within 48h")
                return False, f"Earnings report {report_date} — avoid pre-earnings volatility"

            return True, None

        except Exception as e:
            print(f"[Fundamentals] {symbol} check failed ({e}) — proceeding without")
            return True, None

    # ── Bull/Bear debate (Task 2) ─────────────────────────────────────────────

    async def _debate_trade(self, symbol: str, signal: dict, strategy) -> tuple[bool, str]:
        try:
            shared_data = (
                f"Symbol: {symbol}  Price: ${signal.get('entry_price', 0):.2f}  "
                f"RSI({getattr(strategy, 'rsi_period', 14)}): {signal.get('rsi_at_entry', 'N/A')}  "
                f"MACD: {signal.get('macd_at_entry', 'N/A')}  "
                f"EMA{getattr(strategy, 'ema_short', 50)} crossed above EMA{getattr(strategy, 'ema_long', 200)}.  "
                f"Signal detail: {signal.get('reasoning', '')}"
            )

            def _call(prompt):
                return self._claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=150,
                    messages=[{"role": "user", "content": prompt}],
                ).content[0].text.strip()

            bull, bear = await asyncio.gather(
                asyncio.to_thread(_call,
                    f"You are a bullish stock analyst. Make the strongest case FOR buying {symbol} right now. "
                    f"Data: {shared_data}  Respond in 2 sentences only."
                ),
                asyncio.to_thread(_call,
                    f"You are a bearish stock analyst. Make the strongest case AGAINST buying {symbol} right now. "
                    f"Data: {shared_data}  Respond in 2 sentences only."
                ),
            )
            decision = await asyncio.to_thread(_call,
                f"Bull case: {bull}\nBear case: {bear}\n"
                f"Should we buy {symbol} right now? "
                f"Start your response with BUY or SKIP, then give one sentence reason."
            )

            proceed = decision.upper().startswith("BUY")
            summary = f"*Bull:* {bull}\n*Bear:* {bear}\n*Decision:* {decision}"
            return proceed, summary

        except Exception as e:
            print(f"[Debate] {symbol} failed: {e}")
            return True, "debate unavailable"

    # ── Pre-trade hook: fundamentals → debate (Tasks 2 & 3) ──────────────────

    async def _swing_pre_trade_hook(self, symbol: str, signal: dict, strategy) -> tuple[bool, str]:
        # Task 3 — Fundamentals check
        proceed, reason = await self._check_fundamentals(symbol)
        if not proceed:
            print(f"[Fundamentals] Blocking {symbol}: {reason}")
            asyncio.create_task(notifications.notify_trade_skipped(symbol, "Fundamentals", reason))
            return False, f"Fundamentals: {reason}"

        # Task 2 — Bull/Bear debate
        proceed, debate_summary = await self._debate_trade(symbol, signal, strategy)
        action_label = "BUY" if proceed else "SKIP"
        asyncio.create_task(notifications.notify_trade_decision(
            symbol, "Bull/Bear Debate",
            {"signal": "buy" if proceed else "hold",
             "reasoning": f"[{action_label}] {debate_summary}",
             "confidence": 0.0},
        ))

        if not proceed:
            return False, f"Debate SKIP — {debate_summary}"

        return True, debate_summary

    async def _process_symbol(self, symbol, strategies, is_crypto, risk_percent, stop_loss_percent,
                              current_price=None, pre_execute_hook=None):
        if self.trading_halted_for_day:
            return

        await self._update_loss_cache()
        if symbol in self.last_loss_times:
            if datetime.now(pytz.utc) - self.last_loss_times[symbol] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                return # Blocked by cooldown

        client = self.crypto_data_client if is_crypto else self.stock_data_client
        data = get_historical_bars(symbol, TimeFrame.Day, 365, client, is_crypto=is_crypto)
        
        if data is None:
            return

        # Ensure 'symbol' column exists in the DataFrame
        if 'symbol' not in data.columns:
            data['symbol'] = symbol

        if current_price is not None:
            current_bar = pd.DataFrame([{
                'timestamp': datetime.now(pytz.utc),
                'open': current_price,
                'high': current_price,
                'low': current_price,
                'close': current_price,
                'volume': 0,
                'vwap': current_price,
                'symbol': symbol  # Add symbol to the current bar as well
            }])
            data = pd.concat([data, current_bar], ignore_index=True)

        for strategy in strategies:
            print(f"Running strategy: {strategy.name} for {symbol}")
            if isinstance(strategy, SMBStrategy):
                signal = strategy.generate_signals(data, self.stock_data_client)
            else:
                signal = strategy.generate_signals(data)
            
            if signal:
                if self.trading_halted_for_day:
                    asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Daily loss limit hit"))
                    continue
                    
                if signal['signal'] == "hold":
                    asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Signal was hold (insufficient RR ratio or bear case stronger)"))
                    continue

                if signal['signal'] == "buy":
                    try:
                        await asyncio.to_thread(self.trading_client.get_open_position, symbol)
                        asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "One position per symbol limit"))
                        continue
                    except Exception as e:
                        err = str(e).lower()
                        if "position" not in err and "not found" not in err and "404" not in err:
                            print(f"[ProcessSymbol] Unexpected error checking position for {symbol}: {e}")
                            continue  # Don't trade on unexpected API errors

                    # Pre-execute hook: fundamentals check + bull/bear debate (swing only)
                    if pre_execute_hook:
                        hook_proceed, hook_reason = await pre_execute_hook(symbol, signal, strategy)
                        if not hook_proceed:
                            asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, hook_reason))
                            continue

                signal_key = f"{symbol}-{strategy.name}"

                # Check if signal is active and within cooldown period (1 hour, per symbol+strategy)
                if signal_key in self.active_signals:
                    last_signal_time = self.active_signals[signal_key]
                    if datetime.now(pytz.utc) - last_signal_time < timedelta(hours=1):
                        asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "Symbol on cooldown"))
                        continue
                    else:
                        # Cooldown expired, remove from active signals
                        del self.active_signals[signal_key]

                print(f"Signal generated: {signal}")
                asyncio.create_task(notifications.notify_trade_decision(symbol, strategy.name, signal))

                entry_time = datetime.now(pytz.utc)
                try:
                    scaled_risk_percent = risk_percent * self.risk_multiplier
                    strategy.execute_trade(
                        signal,
                        self.trading_client,
                        scaled_risk_percent,
                        stop_loss_percent,
                        Config.TAKE_PROFIT_PERCENT,
                        Config.MAX_BUYING_POWER_UTILIZATION_PERCENT
                    )

                    # Task 1 — log entry to signal_outcomes after successful execute_trade
                    if signal['signal'] == 'buy':
                        signal_type = 'swing_long' if isinstance(strategy, SwingStrategy) else 'scalp_long'
                        regime = await self._get_market_regime()
                        row_id = await asyncio.to_thread(
                            self._log_trade_entry,
                            symbol, signal_type, float(signal.get('entry_price', 0)),
                            getattr(strategy, 'ema_short', 50), getattr(strategy, 'ema_long', 200),
                            float(signal.get('rsi_at_entry', 0)), float(signal.get('macd_at_entry', 0)),
                            regime, entry_time,
                        )
                        if row_id:
                            async with self._trade_ids_lock:
                                self._open_trade_ids[symbol] = (row_id, float(signal.get('entry_price', 0)), entry_time)

                except Exception as e:
                    msg = f"Error executing trade for {symbol}: {e}"
                    print(msg)
                    asyncio.create_task(notifications.notify_alert(msg))

                # Record the time the signal was generated
                self.active_signals[signal_key] = datetime.now(pytz.utc)

    async def _get_stronger_momentum_crypto(self):
        now = datetime.now(pytz.utc)
        if hasattr(self, '_momentum_winner_cache') and hasattr(self, '_momentum_winner_time') and (now - self._momentum_winner_time).total_seconds() < 300:
            return self._momentum_winner_cache
            
        import pandas_ta as ta
        from utils import get_historical_bars
        from alpaca.data.timeframe import TimeFrame
        
        rsi_scores = {}
        for sym in Config.SCALP_SYMBOLS:
            df = get_historical_bars(sym, TimeFrame.Hour, 7, self.crypto_data_client, is_crypto=True)
            if df is not None and len(df) > 14:
                df['RSI'] = ta.rsi(df['close'], length=14)
                rsi_scores[sym] = df['RSI'].iloc[-1]
                
        if len(rsi_scores) == 2:
            winner = max(rsi_scores, key=rsi_scores.get)
            self._momentum_winner_cache = winner
            self._momentum_winner_time = now
            return winner
        return None

    async def _on_crypto_trade(self, trade):
        symbol = trade.symbol
        price = trade.price
        
        if symbol in self.last_evaluated_price:
            last_price = self.last_evaluated_price[symbol]
            if abs(price - last_price) / last_price < Config.MIN_PRICE_MOVEMENT_PCT:
                return # Not enough movement
        self.last_evaluated_price[symbol] = price
        
        winner = await self._get_stronger_momentum_crypto()
        if winner and symbol != winner:
            return # Skip if this symbol doesn't have the strongest momentum
            
        await self._process_symbol(
            symbol, 
            self.scalp_strategies, 
            is_crypto=True, 
            risk_percent=Config.EQUITY_RISK_PER_TRADE_PERCENT, 
            stop_loss_percent=Config.CRYPTO_SCALP_STOP_LOSS_PERCENT,
            current_price=price
        )

    async def scalp_loop(self):
        print(f"🚀 Starting Crypto Scalping Bot for {Config.SCALP_SYMBOLS} (Websocket)...")
        retry_delay = 5
        consecutive_failures = 0
        while True:
            print(f"WebSocket retry in {retry_delay}s...")
            await asyncio.sleep(retry_delay)

            connect_time = time.time()
            try:
                self.crypto_stream = CryptoDataStream(
                    api_key=Config.ALPACA_API_KEY,
                    secret_key=Config.ALPACA_SECRET_KEY
                )
                self.crypto_stream.subscribe_trades(self._on_crypto_trade, *Config.SCALP_SYMBOLS)
                # _connect() makes a single connection attempt and returns when it drops.
                # _run_forever() has an internal retry loop that bypasses our backoff — avoid it.
                _health_state["websocket_connected"] = True
                await self.crypto_stream._connect()
                _health_state["websocket_connected"] = False
                print("WebSocket stream closed cleanly.")
            except Exception as e:
                _health_state["websocket_connected"] = False
                msg = f"WebSocket error: {e}"
                print(msg)
                asyncio.create_task(notifications.notify_alert(f"{msg} Retrying in {retry_delay}s..."))

            if time.time() - connect_time > 60:
                retry_delay = 5
                consecutive_failures = 0
                print(f"WebSocket was stable for >60s. Backoff reset to 5s.")
            else:
                retry_delay = min(retry_delay * 2, 60)
                consecutive_failures += 1
                if consecutive_failures >= 10:
                    asyncio.create_task(notifications.notify_alert(
                        "Crypto websocket has failed 10 consecutive times — possible Alpaca outage"
                    ))
                    consecutive_failures = 0

    async def trailing_stop_monitor_loop(self):
        print("🛡️ Starting Trailing Stop Monitor Loop...")
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        while True:
            await asyncio.sleep(Config.TRAILING_STOP_MONITOR_INTERVAL)
            try:
                positions = await asyncio.to_thread(self.trading_client.get_all_positions)
                for pos in positions:
                    unrealized_pct = float(pos.unrealized_plpc)
                    if unrealized_pct >= Config.TRAILING_STOP_ACTIVATION_PCT:
                        req = GetOrdersRequest(
                            status=QueryOrderStatus.OPEN,
                            symbols=[pos.symbol]
                        )
                        orders = await asyncio.to_thread(self.trading_client.get_orders, req)
                        for order in orders:
                            if order.order_type.value == "stop":
                                msg = f"Activating Trailing Stop for {pos.symbol} at {unrealized_pct*100:.2f}% profit!"
                                print(msg)
                                asyncio.create_task(notifications.notify_alert(msg, level="INFO"))
                                await asyncio.to_thread(self.trading_client.cancel_order_by_id, order.id)
                                new_sl = TrailingStopOrderRequest(
                                    symbol=pos.symbol,
                                    qty=abs(float(pos.qty)),
                                    side=OrderSide.SELL if pos.side == "long" else OrderSide.BUY,
                                    time_in_force=TimeInForce.GTC,
                                    trail_percent=Config.TRAILING_STOP_TRAIL_PCT * 100
                                )
                                await asyncio.to_thread(self.trading_client.submit_order, new_sl)
            except Exception as e:
                print(f"[TrailingStop] Error: {e}")

    # ── Task 1 — exit monitor: updates signal_outcomes when positions close ───

    async def _exit_monitor_loop(self):
        print("[DB] Exit monitor loop started (10-min polling)")
        while True:
            await asyncio.sleep(600)
            async with self._trade_ids_lock:
                open_ids_snapshot = dict(self._open_trade_ids)
            if not open_ids_snapshot:
                continue
            try:
                req = GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED,
                    limit=200,
                    after=datetime.now(pytz.utc) - timedelta(days=7),
                )
                orders = await asyncio.to_thread(self.trading_client.get_orders, req)
                for order in orders:
                    sym = order.symbol
                    if sym not in open_ids_snapshot:
                        continue
                    if order.status.value != 'filled':
                        continue
                    if not hasattr(order, 'side') or order.side.value != 'sell':
                        continue

                    async with self._trade_ids_lock:
                        if sym not in self._open_trade_ids:
                            continue  # Already processed by a concurrent iteration
                        row_id, entry_price, entry_time = self._open_trade_ids.pop(sym)

                    if row_id is None:
                        continue

                    exit_price = float(order.filled_avg_price) if order.filled_avg_price else 0.0
                    exit_time  = order.filled_at or datetime.now(pytz.utc)
                    pnl_pct    = (exit_price - entry_price) / entry_price * 100 if entry_price else 0.0

                    order_type = order.order_type.value if hasattr(order, 'order_type') else 'unknown'
                    if order_type in ('stop', 'trailing_stop'):
                        exit_reason = 'stop'
                    elif order_type == 'limit':
                        exit_reason = 'target'
                    else:
                        exit_reason = 'manual'

                    hold_days = int((exit_time - entry_time).total_seconds() / 86400) if entry_time else 0
                    await asyncio.to_thread(
                        self._update_trade_exit,
                        row_id, exit_price, exit_reason, exit_time, hold_days, pnl_pct,
                    )
            except Exception as e:
                print(f"[DB] Exit monitor error: {e}")

    async def swing_loop(self):
        print(f"📈 Starting Stock Swing Bot for {Config.SWING_SYMBOLS} (10:30 AM EST Polling)...")
        # Symbols with no statistically validated edge — evaluated but flagged in logs
        _no_edge = {"JPM", "PG"}
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            target = now.replace(hour=10, minute=30, second=0, microsecond=0)

            # If it's past 10:30 AM, move to tomorrow.
            if now >= target:
                target += timedelta(days=1)
            # Skip weekends
            while target.weekday() > 4: # 5=Sat, 6=Sun
                target += timedelta(days=1)

            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)

            await self._check_account_status()
            print(f"📈 Swing evaluation starting at {datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %I:%M:%S %p')} EST")
            for symbol in Config.SWING_SYMBOLS:
                if symbol in _no_edge:
                    print(f"[Swing] {symbol}: no statistically validated edge (p>0.05 across all 243 discovery combos) — monitoring only")
                strategy = self.swing_symbol_strategies.get(symbol)
                if strategy is None:
                    print(f"[Swing] {symbol}: no strategy configured, skipping")
                    continue
                print(f"Evaluating {symbol} for swing signals [{strategy.name}]")
                await self._process_symbol(
                    symbol,
                    [strategy],
                    is_crypto=False,
                    risk_percent=Config.SWING_EQUITY_RISK_PERCENT,
                    stop_loss_percent=Config.STOP_LOSS_PERCENT,
                    pre_execute_hook=self._swing_pre_trade_hook,
                )
            print(f"📈 Swing evaluation complete for {len(Config.SWING_SYMBOLS)} symbols.")

    async def health_report_loop(self):
        print("🏥 Starting Daily Health Report Loop (9:00 AM EST)...")
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            
            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)
            
            await self._check_account_status()
            account = await asyncio.to_thread(self.trading_client.get_account)
            if account:
                uptime_seconds = (datetime.now(pytz.utc) - _bot_start_time).total_seconds()
                uptime_str = str(timedelta(seconds=int(uptime_seconds)))
                equity = float(account.equity)
                buying_power = float(account.buying_power)
                asyncio.create_task(notifications.notify_daily_health(uptime_str, equity, buying_power, self.daily_pnl))

    async def performance_report_loop(self):
        print("📊 Starting Weekly Performance Report Loop (Sunday 6:00 PM EST)...")
        while True:
            now = datetime.now(pytz.timezone('America/New_York'))
            days_ahead = 6 - now.weekday() # Sunday is 6
            target = now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
            if now >= target:
                target += timedelta(days=7)
                
            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)
            
            account = await asyncio.to_thread(self.trading_client.get_account)
            if account:
                equity = float(account.equity)
                try:
                    positions = await asyncio.to_thread(self.trading_client.get_all_positions)
                    active_positions_count = len(positions)
                except Exception:
                    active_positions_count = 0

                asyncio.create_task(notifications.notify_weekly_performance(equity, active_positions_count, self.daily_pnl))

    async def news_loop(self):
        """Continuously polls Benzinga news via Alpaca and routes signals to Slack / trade execution."""
        print("📰 Starting Benzinga News Sentiment Loop...")
        strategy = NewsStrategy()
        while True:
            try:
                if not self.trading_halted_for_day:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        ticker  = sig["ticker"]
                        strength = sig["strength"]
                        action  = sig["action"]

                        # Always alert Slack about the signal
                        asyncio.create_task(notifications.notify_news_signal(
                            ticker, sig["headline"], sig["sentiment"], strength, action
                        ))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (news)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (news)"))
                            continue
                        except Exception:
                            pass  # No open position — proceed

                        # Execute using swing risk parameters
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = Config.SWING_EQUITY_RISK_PERCENT * self.risk_multiplier
                            risk_dollars = equity * (scaled_risk / 100.0)

                            latest = await asyncio.to_thread(
                                self.stock_data_client.get_stock_latest_trade,
                                StockLatestTradeRequest(symbol_or_symbols=ticker)
                            )
                            entry_price = float(latest[ticker].price)
                            stop_distance = entry_price * (Config.STOP_LOSS_PERCENT / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            side = OrderSide.BUY if action == "buy" else OrderSide.SELL
                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY)
                            )

                            asyncio.create_task(notifications.notify_news_trade(
                                ticker, sig["headline"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-news-{action}"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[NewsLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[NewsLoop] Unexpected error: {e}")
            finally:
                _health_state["last_news_scan_utc"] = datetime.now(pytz.utc).isoformat()
                sleep_seconds = _get_scan_sleep_seconds()
                print(f"📰 News scan complete — {strategy._last_articles_scanned} headlines analyzed, "
                      f"{len(signals)} signals above threshold, next scan in {sleep_seconds}s")
                await asyncio.sleep(sleep_seconds)

    async def truth_social_loop(self):
        """Polls Trump's Truth Social feed. Disabled until Quiver Quantitative integration is wired up."""
        if not Config.TRUTH_SOCIAL_ENABLED:
            print("🇺🇸 Truth Social loop disabled (TRUTH_SOCIAL_ENABLED=False) — exiting loop.")
            return
        print("🇺🇸 Starting Truth Social Sentiment Loop (60s polling)...")
        strategy = TruthSocialStrategy()
        while True:
            try:
                if not self.trading_halted_for_day:
                    signals = await strategy.scan_once(trading_client=self.trading_client)
                    for sig in signals:
                        ticker  = sig["ticker"]
                        strength = sig["strength"]
                        action  = sig["action"]

                        # Always alert Slack about the signal
                        asyncio.create_task(notifications.notify_truth_social_signal(
                            sig["post_text"], [ticker], sig["sentiment"], strength, action
                        ))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (TS)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (TS)"))
                            continue
                        except Exception:
                            pass

                        # Execute using Truth Social risk overrides (50% size, 2% SL, 8% TP)
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = (
                                Config.SWING_EQUITY_RISK_PERCENT
                                * self.risk_multiplier
                                * Config.TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER
                            )
                            risk_dollars = equity * (scaled_risk / 100.0)

                            entry_price = sig.get("current_price", 0.0)
                            if entry_price <= 0:
                                latest = await asyncio.to_thread(
                                    self.stock_data_client.get_stock_latest_trade,
                                    StockLatestTradeRequest(symbol_or_symbols=ticker)
                                )
                                entry_price = float(latest[ticker].price)

                            stop_distance = entry_price * (Config.TRUTH_SOCIAL_STOP_LOSS / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            side = OrderSide.BUY if action == "buy" else OrderSide.SELL
                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY)
                            )

                            asyncio.create_task(notifications.notify_truth_social_trade(
                                ticker, sig["post_text"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-ts-{action}"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[TruthSocialLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[TruthSocialLoop] Unexpected error: {e}")
            finally:
                await asyncio.sleep(60)

    async def sec_edgar_loop(self):
        """Polls SEC EDGAR Form 4 insider trade filings every 30 minutes."""
        print("📋 Starting SEC EDGAR Insider Trade Loop (30-min polling)...")
        strategy = SECEdgarStrategy()
        while True:
            signals: list[dict] = []
            try:
                if not self.trading_halted_for_day:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        ticker   = sig["ticker"]
                        strength = sig["strength"]
                        action   = sig["action"]

                        # Always send to #trading-decisions with 📋 emoji
                        asyncio.create_task(notifications.notify_edgar_signal(
                            ticker, sig["headline"], sig["sentiment"], strength, action
                        ))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (EDGAR)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (EDGAR)"))
                            continue
                        except Exception:
                            pass

                        # Execute using swing risk parameters (buys only)
                        if action != "buy":
                            continue
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = Config.SWING_EQUITY_RISK_PERCENT * self.risk_multiplier
                            risk_dollars = equity * (scaled_risk / 100.0)

                            latest = await asyncio.to_thread(
                                self.stock_data_client.get_stock_latest_trade,
                                StockLatestTradeRequest(symbol_or_symbols=ticker)
                            )
                            entry_price = float(latest[ticker].price)
                            stop_distance = entry_price * (Config.STOP_LOSS_PERCENT / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                            )
                            asyncio.create_task(notifications.notify_news_trade(
                                ticker, sig["headline"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-edgar-buy"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[EDGARLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[EDGARLoop] Unexpected error: {e}")
            finally:
                _health_state["last_edgar_scan_utc"] = datetime.now(pytz.utc).isoformat()
                print(f"📋 EDGAR scan complete — {len(signals)} insider signals above threshold, next scan in 30 min")
                await asyncio.sleep(1800)

    async def _validate_swing_symbols(self):
        """Fetch latest trade for each SWING_SYMBOLS entry to catch config typos at startup."""
        print("Validating swing symbols...")
        for symbol in Config.SWING_SYMBOLS:
            try:
                self.stock_data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
                print(f"  {symbol} OK")
            except Exception as e:
                msg = f"WARNING: Symbol {symbol} failed validation — check config ({e})"
                print(msg)
                asyncio.create_task(notifications.notify_alert(msg))

    async def congressional_trading_loop(self):
        """Polls Quiver Quantitative for congressional trades every 60 minutes."""
        if not Config.CONGRESSIONAL_ENABLED:
            print("🏛️ Congressional trading loop disabled (CONGRESSIONAL_ENABLED=False) — exiting.")
            return
        print("🏛️ Starting Congressional Trading Loop (60-min polling)...")
        strategy = CongressionalTradingStrategy()
        while True:
            signals: list[dict] = []
            try:
                if not self.trading_halted_for_day:
                    signals = await strategy.scan_once()
                    for sig in signals:
                        ticker   = sig["ticker"]
                        strength = sig["strength"]
                        action   = sig["action"]

                        asyncio.create_task(notifications.notify_congressional_signal(
                            ticker, sig["headline"], sig["representative"],
                            sig["party"], sig["chamber"], sig["amount_range"],
                            sig["transaction"], strength, action,
                            informational=sig["informational"],
                        ))

                        if not sig["auto_trade"]:
                            continue

                        # Guard: symbol cooldown
                        await self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (Congress)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            await asyncio.to_thread(self.trading_client.get_open_position, ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (Congress)"))
                            continue
                        except Exception:
                            pass

                        # Execute using swing risk parameters (buys only; sells are informational)
                        if action != "buy":
                            continue
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = await asyncio.to_thread(self.trading_client.get_account)
                            equity = float(account.equity)
                            scaled_risk = Config.SWING_EQUITY_RISK_PERCENT * self.risk_multiplier
                            risk_dollars = equity * (scaled_risk / 100.0)

                            latest = await asyncio.to_thread(
                                self.stock_data_client.get_stock_latest_trade,
                                StockLatestTradeRequest(symbol_or_symbols=ticker)
                            )
                            entry_price = float(latest[ticker].price)
                            stop_distance = entry_price * (Config.STOP_LOSS_PERCENT / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            await asyncio.to_thread(
                                self.trading_client.submit_order,
                                MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                            )
                            asyncio.create_task(notifications.notify_news_trade(
                                ticker, sig["headline"], action, entry_price, qty
                            ))
                            self.active_signals[f"{ticker}-congress-buy"] = datetime.now(pytz.utc)

                        except Exception as e:
                            msg = f"[CongressLoop] Trade execution error for {ticker}: {e}"
                            print(msg)
                            asyncio.create_task(notifications.notify_alert(msg))

            except Exception as e:
                print(f"[CongressLoop] Unexpected error: {e}")
            finally:
                if strategy._disabled:
                    print("[CongressLoop] Disabled after auth failure — exiting loop permanently.")
                    return
                buy_count  = sum(1 for s in signals if not s.get("informational"))
                sell_count = sum(1 for s in signals if s.get("informational"))
                print(f"🏛️ Congressional scan complete — {buy_count} buy signals, {sell_count} informational sell signals, next scan in 60 min")
                await asyncio.sleep(3600)

    async def market_open_notification_loop(self):
        """Sends a morning briefing to #trading-alerts at 9:30 AM EST, weekdays only."""
        print("🔔 Starting Market Open Notification Loop (9:30 AM EST, Mon-Fri)...")
        est = pytz.timezone('America/New_York')
        while True:
            now = datetime.now(est)
            target = now.replace(hour=9, minute=30, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            # Advance past weekend days
            while target.weekday() >= 5:
                target += timedelta(days=1)

            await asyncio.sleep((target - now).total_seconds())

            # Double-check we landed on a weekday (clock skew guard)
            if datetime.now(est).weekday() >= 5:
                continue

            try:
                account = await asyncio.to_thread(self.trading_client.get_account)
                equity = float(account.equity) if account else 0.0
            except Exception:
                equity = 0.0

            regime = await self._get_market_regime()
            watchlist = ", ".join(Config.SWING_SYMBOLS)
            asyncio.create_task(notifications.notify_market_open(equity, watchlist, regime))

    async def start_dual_engine(self):
        print("🚀 Hybrid Trading Bot starting...")

        if not await self._check_account_status():
            return

        await self._validate_swing_symbols()

        try:
            account = await asyncio.to_thread(self.trading_client.get_account)
            equity = float(account.equity)
            pnl = self.daily_pnl
            pnl_sign = "+" if pnl >= 0 else ""
            startup_msg = (
                f"🚀 Hybrid Trading Bot started\n"
                f"Equity: ${equity:,.2f}  |  "
                f"Opening equity: ${self.start_of_day_equity:,.2f}  |  "
                f"Daily P&L: {pnl_sign}${pnl:,.2f}\n"
                f"Swing watchlist: {', '.join(Config.SWING_SYMBOLS)}"
            )
        except Exception:
            startup_msg = "🚀 Hybrid Trading Bot has successfully started and connected to Slack!"

        print(startup_msg)
        asyncio.create_task(notifications.notify_alert(startup_msg, level="INFO"))

        # Ensure signal_outcomes table exists (creates if missing on Railway PostgreSQL)
        await asyncio.to_thread(self._ensure_signal_outcomes_table)

        self.add_scalp_strategy(SMBStrategy("SMB Late Scalp", ema_window=9, rr_ratio=3))

        # Per-symbol swing strategies — parameters from Discovery Engine walk-forward validation
        self.swing_symbol_strategies = {
            # 125/243 combos validated, best test Sharpe 0.87 — short EMA crossover dominates
            "COST":  SwingStrategy("COST Swing",  ema_short=20, ema_long=100, rsi_period=10, rsi_entry_low=35, rsi_entry_high=65),
            # 24/243 combos validated, best test Sharpe 0.90 — RSI21 + wide upper band required
            "BRK.B": SwingStrategy("BRK.B Swing", rsi_period=21, rsi_entry_low=40, rsi_entry_high=65),
            # 9/243 combos validated — EMA50/200 with RSI upper=60 already matches defaults
            "SPY":   SwingStrategy("SPY Swing"),
            # 0/243 combos validated — defaults until further data
            "V":     SwingStrategy("V Swing"),
            # 0/243 combos validated — monitoring only (see swing_loop warning)
            "JPM":   SwingStrategy("JPM Swing"),
            "PG":    SwingStrategy("PG Swing"),
        }
        await asyncio.gather(
            self.scalp_loop(),
            self.swing_loop(),
            self.news_loop(),
            self.truth_social_loop(),
            self.sec_edgar_loop(),
            self.congressional_trading_loop(),
            self.health_report_loop(),
            self.performance_report_loop(),
            self.trailing_stop_monitor_loop(),
            self._exit_monitor_loop(),
            self.market_open_notification_loop(),
        )

if __name__ == "__main__":
    start_health_server(port=8502)
    bot = TradingBot()
    asyncio.run(bot.start_dual_engine())

