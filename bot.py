import os
import time
import asyncio
import threading
from datetime import datetime, timedelta
import pytz
import pandas as pd
from flask import Flask, jsonify

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import CryptoDataStream

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.smb_strategy import SMBStrategy
from strategies.swing_strategy import SwingStrategy
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
        self.daily_pnl = 0.0
        self.start_of_day_equity = 0.0
        self.last_pnl_reset_date = datetime.now(pytz.timezone('America/New_York')).date()
        self.trading_halted_for_day = False
        self.active_signals = {}

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
                if current_daily_pnl < 0:
                    current_daily_loss_percent = (abs(current_daily_pnl) / self.start_of_day_equity) * 100
                    if current_daily_loss_percent >= Config.MAX_DAILY_LOSS_PERCENT:
                        self.trading_halted_for_day = True
                        print(f"CRITICAL: Max daily loss of {Config.MAX_DAILY_LOSS_PERCENT}% hit! Trading halted for the day.")
                
                self.daily_pnl = current_daily_pnl
                return True
            return False
        except Exception as e:
            print(f"Error checking account status: {e}")
            return False

    async def _process_symbol(self, symbol, strategies, is_crypto, risk_percent, stop_loss_percent, current_price=None):
        if self.trading_halted_for_day:
            return

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
                signal_key = f"{symbol}-{strategy.name}-{signal['signal']}"
                
                # Check if signal is active and within cooldown period (e.g., 1 hour)
                if signal_key in self.active_signals:
                    last_signal_time = self.active_signals[signal_key]
                    if datetime.now(pytz.utc) - last_signal_time < timedelta(hours=1):
                        continue
                    else:
                        # Cooldown expired, remove from active signals
                        del self.active_signals[signal_key]

                print(f"Signal generated: {signal}")
                strategy.execute_trade(
                    signal, 
                    self.trading_client, 
                    risk_percent,
                    stop_loss_percent,
                    Config.TAKE_PROFIT_PERCENT,
                    Config.MAX_BUYING_POWER_UTILIZATION_PERCENT
                )
                # Record the time the signal was generated
                self.active_signals[signal_key] = datetime.now(pytz.utc)

    async def _on_crypto_trade(self, trade):
        await self._process_symbol(
            trade.symbol, 
            self.scalp_strategies, 
            is_crypto=True, 
            risk_percent=Config.EQUITY_RISK_PER_TRADE_PERCENT, 
            stop_loss_percent=Config.CRYPTO_SCALP_STOP_LOSS_PERCENT,
            current_price=trade.price
        )

    async def scalp_loop(self):
        print(f"🚀 Starting Crypto Scalping Bot for {Config.SCALP_SYMBOLS} (Websocket)...")
        while True:
            try:
                # Re-initialize CryptoDataStream on each attempt to ensure a fresh connection
                # This helps to avoid connection limit issues by ensuring a clean state.
                self.crypto_stream = CryptoDataStream(
                    api_key=Config.ALPACA_API_KEY,
                    secret_key=Config.ALPACA_SECRET_KEY
                )
                self.crypto_stream.subscribe_trades(self._on_crypto_trade, *Config.SCALP_SYMBOLS)
                await self.crypto_stream._run_forever()
            except Exception as e:
                print(f"WebSocket connection error: {e}. Retrying in 60 seconds...")
                await asyncio.sleep(60) # Wait for 60 seconds before retrying

    async def swing_loop(self):
        print(f"📈 Starting Stock Swing Bot for {Config.SWING_SYMBOLS} (Polling)...")
        while True:
            await self._check_account_status()
            for symbol in Config.SWING_SYMBOLS:
                await self._process_symbol(
                    symbol, 
                    self.swing_strategies, 
                    is_crypto=False, 
                    risk_percent=Config.SWING_EQUITY_RISK_PERCENT, 
                    stop_loss_percent=Config.STOP_LOSS_PERCENT
                )
            await asyncio.sleep(86400)

    async def start_dual_engine(self):
        if not await self._check_account_status():
            return
        self.add_scalp_strategy(SMBStrategy("SMB Late Scalp", ema_window=9, rr_ratio=3))
        self.add_swing_strategy(SwingStrategy("Swing Trader", ema_short=50, ema_long=200))
        await asyncio.gather(self.scalp_loop(), self.swing_loop())

if __name__ == "__main__":
    start_health_server(port=8501)
    bot = TradingBot()
    asyncio.run(bot.start_dual_engine())
