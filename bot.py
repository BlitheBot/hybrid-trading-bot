import os
import time
import asyncio
from datetime import datetime, timedelta
import pytz # Import pytz for timezone awareness

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import CryptoDataStream # Import CryptoDataStream

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.smb_strategy import SMBStrategy
from strategies.swing_strategy import SwingStrategy
from utils import get_historical_bars, get_finnhub_price

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
        
        # Initialize CryptoDataStream for real-time scalping
        self.crypto_stream = CryptoDataStream(
            api_key=Config.ALPACA_API_KEY,
            secret_key=Config.ALPACA_SECRET_KEY,
            paper=Config.PAPER_TRADING
        )
        self.scalp_strategies = []
        self.swing_strategies = []
        self.daily_pnl = 0.0
        self.start_of_day_equity = 0.0
        self.last_pnl_reset_date = datetime.now(pytz.timezone('America/New_York')).date() # Use timezone for consistent daily reset
        self.trading_halted_for_day = False
        self.active_signals = {} # To track signals and prevent over-trading

    def add_scalp_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.scalp_strategies.append(strategy)

    def add_swing_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.swing_strategies.append(strategy)

    async def _check_account_status(self):
        """
        Checks the Alpaca account status and logs details.
        Returns True if account is active and has buying power, False otherwise.
        """
        print("DEBUG: Fetching account details from Alpaca...")
        try:
            account = self.trading_client.get_account()
            if account:
                print(f"DEBUG: Raw account data: {account}")
                print(f"Account Status: {account.status}, Equity: ${float(account.equity):,.2f}, Buying Power: ${float(account.buying_power):,.2f}")
                if account.status != 'ACTIVE':
                    print(f"Account is not ACTIVE. Current status: {account.status}")
                    return False
                
                # Initialize daily PnL tracking
                current_date = datetime.now(pytz.timezone('America/New_York')).date() # Use timezone for consistent daily reset
                if current_date != self.last_pnl_reset_date:
                    self.daily_pnl = 0.0
                    self.start_of_day_equity = float(account.equity)
                    self.last_pnl_reset_date = current_date
                    self.trading_halted_for_day = False # Reset halt status for new day
                    print(f"DEBUG: Daily PnL reset for {current_date}. Starting equity: ${self.start_of_day_equity:,.2f}")
                
                # Update daily PnL (simple for now, will be refined with actual trade PnL)
                # For now, we'll use a simplified PnL calculation based on current equity vs start of day equity
                if self.start_of_day_equity == 0.0: # First run of the day
                    self.start_of_day_equity = float(account.equity)

                current_daily_pnl = float(account.equity) - self.start_of_day_equity
                if current_daily_pnl < 0:
                    current_daily_loss_percent = (abs(current_daily_pnl) / self.start_of_day_equity) * 100
                    if current_daily_loss_percent >= Config.MAX_DAILY_LOSS_PERCENT:
                        self.trading_halted_for_day = True
                        print(f"CRITICAL: Max daily loss of {Config.MAX_DAILY_LOSS_PERCENT}% hit! Trading halted for the day.")
                
                self.daily_pnl = current_daily_pnl # Update for dashboard

                return True
            else:
                print("Failed to retrieve account details from Alpaca: Account object is None.")
                return False
        except Exception as e:
            print(f"Error checking account status: {e}")
            return False

    async def _process_symbol(self, symbol, strategies, is_crypto, risk_percent, stop_loss_percent, interval_seconds=None, current_price=None):
        if self.trading_halted_for_day:
            print(f"Trading halted for the day due to max daily loss. Skipping {symbol}.")
            return

        client = self.crypto_data_client if is_crypto else self.stock_data_client
        
        # 1. Fetch historical data (Integrated with Finnhub in utils.py)
        # For scalping, we might only need the latest bar from websocket, but strategies need historical context
        data = get_historical_bars(symbol, TimeFrame.Day, 365, client, is_crypto=is_crypto)
        
        if data is None:
            print(f"Could not fetch data for {symbol}")
            return

        # If current_price is provided (from websocket), append it to data for strategy calculation
        if current_price is not None:
            # Create a dummy DataFrame for the current price to append
            current_bar = pd.DataFrame([{
                'timestamp': datetime.now(pytz.utc), # Use UTC for consistency
                'open': current_price,
                'high': current_price,
                'low': current_price,
                'close': current_price,
                'volume': 0,
                'trade_count': 0,
                'vwap': current_price
            }])
            data = pd.concat([data, current_bar], ignore_index=True)

        # 2. Run each strategy
        for strategy in strategies:
            print(f"Running strategy: {strategy.name} for {symbol}")
            # Pass the stock_data_client for Relative Strength calculation in SMBStrategy
            if isinstance(strategy, SMBStrategy):
                signal = strategy.generate_signals(data, self.stock_data_client)
            else:
                signal = strategy.generate_signals(data)
            
            if signal:
                # Implement Signal Cooldown: only act once per signal until position is closed
                signal_key = f"{symbol}-{strategy.name}-{signal["signal"]}"
                if signal_key in self.active_signals:
                    print(f"Signal for {symbol} already active for {strategy.name}. Skipping.")
                    continue

                print(f"Signal generated: {signal}")
                strategy.execute_trade(
                    signal, 
                    self.trading_client, 
                    risk_percent,
                    stop_loss_percent,
                    Config.TAKE_PROFIT_PERCENT,
                    Config.MAX_BUYING_POWER_UTILIZATION_PERCENT
                )
                # Mark signal as active
                self.active_signals[signal_key] = True
            else:
                print(f"No signal for {symbol}")

    async def _on_crypto_trade(self, trade):
        symbol = trade.symbol
        current_price = trade.price
        print(f"DEBUG: Crypto Trade received for {symbol}: {current_price}")

        # Only process if symbol is in scalp watchlist
        if symbol in Config.SCALP_SYMBOLS:
            await self._process_symbol(
                symbol, 
                self.scalp_strategies, 
                is_crypto=True, 
                risk_percent=Config.EQUITY_RISK_PER_TRADE_PERCENT, 
                stop_loss_percent=Config.CRYPTO_SCALP_STOP_LOSS_PERCENT,
                current_price=current_price # Pass real-time price
            )

    async def scalp_loop(self):
        print(f"🚀 Starting Crypto Scalping Bot for {Config.SCALP_SYMBOLS} (Websocket)...")
        
        # Subscribe to crypto trades
        self.crypto_stream.subscribe_trades(self._on_crypto_trade, *Config.SCALP_SYMBOLS)
        
        # Start the websocket stream in a separate task
        # The stream runs indefinitely, so we need a way to keep the main loop alive
        await self.crypto_stream._run_forever() # This will block, so it needs to be in gather

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
            print(f"💤 Swing Bot waiting for 86400 seconds (daily poll)...")
            await asyncio.sleep(86400) # Poll once per day

    async def start_dual_engine(self):
        print("DEBUG: Script started...")
        if not Config.ALPACA_API_KEY or not Config.ALPACA_SECRET_KEY:
            print("CRITICAL ERROR: ALPACA_API_KEY or ALPACA_SECRET_KEY is missing!")
            return
        
        # Initial account check
        if not await self._check_account_status():
            print("Bot cannot start due to initial account issues. Please check logs.")
            return

        # Add strategies
        self.add_scalp_strategy(SMBStrategy("SMB Late Scalp", ema_window=9, rr_ratio=3))
        # self.add_scalp_strategy(SMACrossoverStrategy("SMA Crossover Scalp", short_window=20, long_window=50)) # Optional

        self.add_swing_strategy(SwingStrategy("Swing Trader", ema_short=50, ema_long=200))
        # self.add_swing_strategy(SMACrossoverStrategy("SMA Crossover Swing", short_window=20, long_window=50)) # Optional

        # Run both loops concurrently
        await asyncio.gather(
            self.scalp_loop(),
            self.swing_loop()
        )

if __name__ == "__main__":
    bot = TradingBot()
    asyncio.run(bot.start_dual_engine())
