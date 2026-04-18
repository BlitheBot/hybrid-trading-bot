import time
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame

from config import Config
from strategies.base_strategy import BaseStrategy
from strategies.sma_crossover import SMACrossoverStrategy
from utils import get_historical_bars

class TradingBot:
    def __init__(self):
        self.trading_client = TradingClient(api_key=Config.ALPACA_API_KEY, secret_key=Config.ALPACA_SECRET_KEY, paper=Config.PAPER_TRADING)
        self.stock_data_client = StockHistoricalDataClient(api_key=Config.ALPACA_API_KEY, secret_key=Config.ALPACA_SECRET_KEY)
        self.strategies = []

    def add_strategy(self, strategy: BaseStrategy):
        if not isinstance(strategy, BaseStrategy):
            raise TypeError("Strategy must inherit from BaseStrategy")
        self.strategies.append(strategy)

    def run_once(self, symbol, qty):
        """
        Runs the bot for a single symbol and quantity.
        """
        # 1. Fetch historical data
        data = get_historical_bars(symbol, TimeFrame.Day, 365, self.stock_data_client)
        
        # 2. Run each strategy
        for strategy in self.strategies:
            print(f"Running strategy: {strategy.name} for {symbol}")
            signal = strategy.generate_signals(data)
            
            if signal:
                print(f"Signal generated: {signal}")
                strategy.execute_trade(signal, symbol, qty, self.trading_client)
            else:
                print(f"No signal for {symbol}")

    def start(self, symbol, qty, interval_seconds=3600):
        """
        Starts the bot loop.
        """
        print(f"Trading bot started for {symbol}...")
        try:
            while True:
                self.run_once(symbol, qty)
                print(f"Waiting for {interval_seconds} seconds...")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("Bot stopped by user.")

if __name__ == "__main__":
    if not Config.ALPACA_API_KEY or not Config.ALPACA_SECRET_KEY:
        print("CRITICAL ERROR: ALPACA_API_KEY or ALPACA_SECRET_KEY is missing!")
        print("Please add them to the 'Variables' tab in your Railway project.")
    else:
        bot = TradingBot()
        # Add SMA Crossover strategy
        bot.add_strategy(SMACrossoverStrategy("SMA Crossover", short_window=20, long_window=50))
        
        # Start the bot (example: trade 1 share of AAPL every hour)
        # Note: In a real scenario, you'd use a more frequent interval or WebSockets
        # bot.start("AAPL", 1, interval_seconds=3600)
        
        # For demonstration, just run once
        bot.run_once("AAPL", 1)
