import pandas as pd

class Backtester:
    def __init__(self, strategy, initial_capital=10000):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions = 0
        self.equity_curve = []

    def run(self, data):
        """
        Runs the backtest on historical data.
        :param data: A pandas DataFrame with OHLCV data.
        """
        for i in range(len(data)):
            # Create a window of data up to the current point
            current_data = data.iloc[:i+1].copy()
            signal = self.strategy.generate_signals(current_data)
            
            price = data['close'].iloc[i]
            
            if signal:
                if signal['signal'] == 'buy' and self.capital >= price:
                    # Buy one unit
                    self.positions += 1
                    self.capital -= price
                    print(f"Backtest: Bought at {price}")
                elif signal['signal'] == 'sell' and self.positions > 0:
                    # Sell one unit
                    self.positions -= 1
                    self.capital += price
                    print(f"Backtest: Sold at {price}")
            
            # Record total equity (cash + position value)
            total_equity = self.capital + (self.positions * price)
            self.equity_curve.append(total_equity)

        final_equity = self.capital + (self.positions * data['close'].iloc[-1])
        return final_equity, self.equity_curve

if __name__ == "__main__":
    # Example usage
    from strategies.sma_crossover import SMACrossoverStrategy
    import numpy as np
    
    # Generate dummy data
    dates = pd.date_range('2023-01-01', periods=300)
    prices = 100 + np.cumsum(np.random.randn(300))
    data = pd.DataFrame({'close': prices}, index=dates)
    
    strategy = SMACrossoverStrategy("SMA Crossover", short_window=10, long_window=30)
    backtester = Backtester(strategy)
    final_val, curve = backtester.run(data)
    
    print(f"Initial Capital: 10000")
    print(f"Final Equity: {final_val:.2f}")
    print(f"Return: {((final_val - 10000) / 10000) * 100:.2f}%")
