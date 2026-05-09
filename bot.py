import os
import time
import asyncio
import threading
from datetime import datetime, timedelta
import pytz
import pandas as pd
from flask import Flask, jsonify
import notifications
import time
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import CryptoDataStream

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.smb_strategy import SMBStrategy
from strategies.swing_strategy import SwingStrategy
from strategies.news_strategy import NewsStrategy, _get_scan_sleep_seconds
from strategies.truth_social_strategy import TruthSocialStrategy
from utils import get_historical_bars, get_finnhub_price

# ── Flask Health Endpoint (Bug 5) ───────────────────────────────────
_health_app = Flask(__name__)
_bot_start_time = datetime.now(pytz.utc)

@_health_app.route("/health", methods=["GET"])
def health_check():
    uptime_seconds = (datetime.now(pytz.utc) - _bot_start_time).total_seconds()
    return jsonify({
        "status": "running",
        "uptime_seconds": round(uptime_seconds, 2),
        "started_at": _bot_start_time.isoformat()
    }), 200

def start_health_server(port=8501):
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
            account = self.trading_client.get_account()
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
                return True
            return False
        except Exception as e:
            msg = f"Error checking account status: {e}"
            print(msg)
            asyncio.create_task(notifications.notify_alert(msg))
            return False

    def _update_loss_cache(self):
        try:
            # Check recently filled orders to see if any were stop losses
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                limit=50,
                after=datetime.now(pytz.utc) - timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES)
            )
            orders = self.trading_client.get_orders(req)
            for order in orders:
                # If a 'stop' order was filled, it's a loss exit
                if order.status.value == "filled" and (order.order_type.value == "stop" or order.order_type.value == "trailing_stop"):
                    self.last_loss_times[order.symbol] = order.filled_at
        except Exception as e:
            print(f"Failed to update loss cache: {e}")

    async def _process_symbol(self, symbol, strategies, is_crypto, risk_percent, stop_loss_percent, current_price=None):
        if self.trading_halted_for_day:
            return

        self._update_loss_cache()
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
                        self.trading_client.get_open_position(symbol)
                        asyncio.create_task(notifications.notify_trade_skipped(symbol, strategy.name, "One position per symbol limit"))
                        continue
                    except:
                        pass # No open position, proceed

                signal_key = f"{symbol}-{strategy.name}-{signal['signal']}"
                
                # Check if signal is active and within cooldown period (e.g., 1 hour)
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
                await self.crypto_stream._connect()
                print("WebSocket stream closed cleanly.")
            except Exception as e:
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
            await asyncio.sleep(60) # check every minute
            try:
                positions = self.trading_client.get_all_positions()
                for pos in positions:
                    unrealized_pct = float(pos.unrealized_plpc)
                    if unrealized_pct >= Config.TRAILING_STOP_ACTIVATION_PCT:
                        # Find open stop loss order for this symbol and replace it
                        req = GetOrdersRequest(
                            status=QueryOrderStatus.OPEN,
                            symbols=[pos.symbol]
                        )
                        orders = self.trading_client.get_orders(req)
                        for order in orders:
                            if order.order_type.value == "stop": # static stop loss found
                                msg = f"Activating Trailing Stop for {pos.symbol} at {unrealized_pct*100:.2f}% profit!"
                                print(msg)
                                asyncio.create_task(notifications.notify_alert(msg, level="INFO"))
                                
                                self.trading_client.cancel_order_by_id(order.id)
                                new_sl = TrailingStopOrderRequest(
                                    symbol=pos.symbol,
                                    qty=abs(float(pos.qty)),
                                    side=OrderSide.SELL if pos.side == "long" else OrderSide.BUY,
                                    time_in_force=TimeInForce.GTC,
                                    trail_percent=Config.TRAILING_STOP_TRAIL_PCT * 100
                                )
                                self.trading_client.submit_order(new_sl)
            except Exception as e:
                pass

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
                    stop_loss_percent=Config.STOP_LOSS_PERCENT
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
            account = self.trading_client.get_account()
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
            
            account = self.trading_client.get_account()
            if account:
                equity = float(account.equity)
                try:
                    positions = self.trading_client.get_all_positions()
                    active_positions_count = len(positions)
                except:
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
                        self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (news)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            self.trading_client.get_open_position(ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (news)"))
                            continue
                        except Exception:
                            pass  # No open position — proceed

                        # Execute using swing risk parameters
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = self.trading_client.get_account()
                            equity = float(account.equity)
                            scaled_risk = Config.SWING_EQUITY_RISK_PERCENT * self.risk_multiplier
                            risk_dollars = equity * (scaled_risk / 100.0)

                            latest = self.stock_data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
                            entry_price = float(latest[ticker].price)
                            stop_distance = entry_price * (Config.STOP_LOSS_PERCENT / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            side = OrderSide.BUY if action == "buy" else OrderSide.SELL
                            self.trading_client.submit_order(MarketOrderRequest(
                                symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY
                            ))

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
                sleep_seconds = _get_scan_sleep_seconds()
                print(f"📰 News scan complete — {strategy._last_articles_scanned} headlines analyzed, "
                      f"{len(signals)} signals above threshold, next scan in {sleep_seconds}s")
                await asyncio.sleep(sleep_seconds)

    async def truth_social_loop(self):
        """Continuously polls Trump's Truth Social RSS feed and routes signals to Slack / trade execution."""
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
                        self._update_loss_cache()
                        if ticker in self.last_loss_times:
                            if datetime.now(pytz.utc) - self.last_loss_times[ticker] < timedelta(minutes=Config.SYMBOL_COOLDOWN_MINUTES):
                                asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "Symbol on cooldown (TS)"))
                                continue

                        # Guard: one position per symbol
                        try:
                            self.trading_client.get_open_position(ticker)
                            asyncio.create_task(notifications.notify_trade_skipped(ticker, strategy.name, "One position per symbol limit (TS)"))
                            continue
                        except Exception:
                            pass

                        # Execute using Truth Social risk overrides (50% size, 2% SL, 8% TP)
                        try:
                            from alpaca.trading.requests import MarketOrderRequest
                            from alpaca.trading.enums import OrderSide, TimeInForce

                            account = self.trading_client.get_account()
                            equity = float(account.equity)
                            scaled_risk = (
                                Config.SWING_EQUITY_RISK_PERCENT
                                * self.risk_multiplier
                                * Config.TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER
                            )
                            risk_dollars = equity * (scaled_risk / 100.0)

                            entry_price = sig.get("current_price", 0.0)
                            if entry_price <= 0:
                                latest = self.stock_data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
                                entry_price = float(latest[ticker].price)

                            stop_distance = entry_price * (Config.TRUTH_SOCIAL_STOP_LOSS / 100.0)
                            qty = max(1, int(risk_dollars / stop_distance))

                            max_dollars = float(account.buying_power) * (Config.MAX_BUYING_POWER_UTILIZATION_PERCENT / 100.0)
                            qty = min(qty, max(1, int(max_dollars / entry_price)))

                            side = OrderSide.BUY if action == "buy" else OrderSide.SELL
                            self.trading_client.submit_order(MarketOrderRequest(
                                symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY
                            ))

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

    async def start_dual_engine(self):
        print("🚀 Hybrid Trading Bot starting...")

        if not await self._check_account_status():
            return

        await self._validate_swing_symbols()

        try:
            account = self.trading_client.get_account()
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
            self.health_report_loop(),
            self.performance_report_loop(),
            self.trailing_stop_monitor_loop()
        )

if __name__ == "__main__":
    start_health_server(port=8501)
    bot = TradingBot()
    asyncio.run(bot.start_dual_engine())

