import os
import time

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.timeframe import TimeFrame

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from strategies.smb_strategy import SMBStrategy
from utils import get_historical_bars, get_finnhub_price

class TradingBot:
    def __init__(self):
        # Explicitly passing None for oauth_token to ensure no conflict
        self.trading_client = TradingClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY, 
            paper=Config.PAPER_TRADING,
            oauth_token=None
        )
        self.stock_data_client = StockHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY,
            oauth_token=None
        )
        self.crypto_data_client = CryptoHistoricalDataClient(
            api_key=Config.ALPACA_API_KEY, 
            secret_key=Config.ALPACA_SECRET_KEY,
            oauth_token=None
        )
        self.strategies = []

    def add_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.strategies.append(strategy)

    def _check_account_status(self):
        """
        Checks the Alpaca account status and logs details.
        Returns True if account is active and has buying power, False otherwise.
        """
        try:
            account = self.trading_client.get_account()
            if account:
                print(f"DEBUG: Account object retrieved: {account}")
                print(f"Account Status: {account.status}, Equity: {float(account.equity):.2f}, Buying Power: {float(account.buying_power):.2f}")
                if account.status != 'ACTIVE':
                    print(f"Account is not ACTIVE. Current status: {account.status}")
                    return False
                if float(account.buying_power) <= 0:
                    print(f"Insufficient buying power. Current: {account.buying_power}")
                    return False
                return True
            else:
                print("Failed to retrieve account details from Alpaca: Account object is None.")
                return False
        except Exception as e:
            print(f"Error checking account status: {e}")
            return False

    def run_once(self, symbol):
        """
        Runs the bot for a single symbol.
        """
        # Determine if it's crypto or stock
        is_crypto = "/" in symbol or symbol in ["BTCUSD", "ETHUSD"]
        client = self.crypto_data_client if is_crypto else self.stock_data_client
        
        # 1. Fetch historical data (Integrated with Finnhub in utils.py)
        data = get_historical_bars(symbol, TimeFrame.Day, 365, client, is_crypto=is_crypto)
        
        if data is None:
            print(f"Could not fetch data for {symbol}")
            return

        # 2. Run each strategy
        for strategy in self.strategies:
            print(f"Running strategy: {strategy.name} for {symbol}")
            # Pass the stock_data_client for Relative Strength calculation in SMBStrategy
            if isinstance(strategy, SMBStrategy):
                signal = strategy.generate_signals(data, self.stock_data_client)
            else:
                signal = strategy.generate_signals(data)
            
            if signal:
                print(f"Signal generated: {signal}")
                strategy.execute_trade(
                    signal, 
                    self.trading_client, 
                    Config.EQUITY_RISK_PER_TRADE_PERCENT,
                    Config.STOP_LOSS_PERCENT,
                    Config.TAKE_PROFIT_PERCENT,
                    Config.MAX_BUYING_POWER_UTILIZATION_PERCENT
                )
            else:
                print(f"No signal for {symbol}")

    def start(self, watchlist, interval_seconds=60):
        """
        Starts the bot loop for a list of symbols.
        """
        print(f"🚀 Trading bot started for {watchlist}...")
        
        # Check account status before starting the main loop
        if not self._check_account_status():
            print("Bot cannot start due to account issues. Please check logs.")
            return

        try:
            while True:
                for symbol in watchlist:
                    self.run_once(symbol)
                print(f"💤 Waiting for {interval_seconds} seconds...")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("🛑 Bot stopped by user.")

if __name__ == "__main__":
    if not Config.ALPACA_API_KEY or not Config.ALPACA_SECRET_KEY:
        print("CRITICAL ERROR: ALPACA_API_KEY or ALPACA_SECRET_KEY is missing!")
    else:
        bot = TradingBot()
        
        # --- SMB CAPITAL STRATEGIES ---
        # 1. SMB Fashionably Late Scalp (9 EMA / VWAP)
        bot.add_strategy(SMBStrategy("SMB Late Scalp", ema_window=9, rr_ratio=3))
        
        # 2. Traditional SMA Crossover (as backup)
        bot.add_strategy(SMACrossoverStrategy("SMA Crossover", short_window=20, long_window=50))
        
        # Expanded Watchlist: High Volume Stocks + Corrected Crypto Symbols
        watchlist = [
            "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "AMZN", "GOOGL", "META", # High Volume Stocks
            "BTC/USD", "ETH/USD" # Crypto
        ]
        bot.start(watchlist, interval_seconds=60)
