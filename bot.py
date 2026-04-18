import os
import time

# Hard override to prevent Alpaca from seeing conflicting tokens
os.environ.pop("ALPACA_OAUTH_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from utils import get_historical_bars

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
        self.strategies = []

    def add_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.strategies.append(strategy)

    def run_once(self, symbol):
        """
        Runs the bot for a single symbol.
        """
        # 1. Fetch historical data (Integrated with Finnhub in utils.py)
        data = get_historical_bars(symbol, TimeFrame.Day, 365, self.stock_data_client)
        
        if data is None:
            print(f"Could not fetch data for {symbol}")
            return

        # 2. Run each strategy
        for strategy in self.strategies:
            print(f"Running strategy: {strategy.name} for {symbol}")
            signal = strategy.generate_signals(data)
            
            if signal:
                print(f"Signal generated: {signal}")
                strategy.execute_trade(
                    signal, 
                    self.trading_client, 
                    Config.EQUITY_RISK_PER_TRADE_PERCENT,
                    Config.STOP_LOSS_PERCENT,
                    Config.TAKE_PROFIT_PERCENT
                )
            else:
                print(f"No signal for {symbol}")

    def start(self, watchlist, interval_seconds=60):
        """
        Starts the bot loop for a list of symbols.
        """
        print(f"🚀 Trading bot started for {watchlist}...")
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
        # Add SMA Crossover strategy
        bot.add_strategy(SMACrossoverStrategy("SMA Crossover", short_window=20, long_window=50))
        
        # Start the continuous loop with a watchlist
        watchlist = ["AAPL", "BTC/USD", "ETH/USD", "TSLA", "NVDA"]
        bot.start(watchlist, interval_seconds=60)
