# Automated Trading Bot

This project provides a modular and extensible framework for building automated trading bots using Python and the Alpaca API. It supports paper trading for testing strategies without financial risk and includes several common algorithmic trading strategies.

## Features

*   **Modular Architecture**: Easily add new trading strategies and data sources.
*   **Alpaca API Integration**: Connects to Alpaca for trading (stocks and crypto) and market data.
*   **Paper Trading Support**: Test strategies in a simulated environment.
*   **Multiple Strategies**: Includes implementations for:
    *   **SMA Crossover**: Generates signals based on Simple Moving Average crossovers.
    *   **RSI Strategy**: Identifies overbought/oversold conditions using the Relative Strength Index.
    *   **Mean Reversion**: Trades based on the assumption that prices will revert to their historical average.
*   **Backtesting Module**: Evaluate strategy performance using historical data.
*   **Configurable**: Easy setup of API keys and trading parameters.

## Installation

1.  **Clone the repository**:

    ```bash
    git clone https://github.com/your-username/automated-trading-bot.git
    cd automated-trading-bot
    ```

2.  **Create a virtual environment** (recommended):

    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3.  **Install dependencies**:

    ```bash
    pip install -r requirements.txt
    ```

    *(Note: You will need to create a `requirements.txt` file with `alpaca-py` and `pandas`)*

## Configuration

1.  **Alpaca API Keys**: Sign up for an Alpaca account (https://alpaca.markets/) and obtain your API Key and Secret Key. For testing, it's highly recommended to use paper trading keys.

2.  **Set Environment Variables**: It's best practice to set your API keys as environment variables to avoid hardcoding them in your code.

    ```bash
    export ALPACA_API_KEY="YOUR_ALPACA_API_KEY"
    export ALPACA_SECRET_KEY="YOUR_ALPACA_SECRET_KEY"
    ```

    Alternatively, you can directly edit `config.py` (not recommended for production).

3.  **Adjust `config.py`**: Open `config.py` to adjust other parameters:

    ```python
    # config.py
    class Config:
        ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
        ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
        PAPER_TRADING = True  # Set to False for live trading
        TRADE_AMOUNT = 100    # Amount to trade per signal (e.g., in USD)
    ```

## Strategies

All trading strategies inherit from `strategies/base_strategy.py`. To add a new strategy:

1.  Create a new Python file in the `strategies/` directory (e.g., `my_new_strategy.py`).
2.  Implement a class that inherits from `BaseStrategy`.
3.  Override the `generate_signals` and `execute_trade` methods with your strategy logic.
4.  Add your strategy to the `TradingBot` instance in `bot.py`:

    ```python
    from strategies.my_new_strategy import MyNewStrategy
    bot.add_strategy(MyNewStrategy("My New Strategy"))
    ```

## Usage

### Running the Bot

To run the bot, execute `bot.py`. The `if __name__ == "__main__":` block in `bot.py` contains an example of how to instantiate the bot, add strategies, and run it.

```bash
python3 bot.py
```

By default, the example in `bot.py` will run the SMA Crossover strategy once for AAPL. You can modify this to run continuously or with different symbols and strategies.

### Backtesting

To backtest a strategy, you can use the `backtester.py` module. The `if __name__ == "__main__":` block in `backtester.py` provides an example of how to use it with dummy data.

```bash
python3 backtester.py
```

You will need to provide historical data to the backtester. In a real scenario, you would fetch this data using the `StockHistoricalDataClient` or `CryptoHistoricalDataClient` as shown in `utils.py`.

## Disclaimer

Automated trading involves significant risks, including the potential loss of capital. Past performance is not indicative of future results. This bot is provided for educational and informational purposes only and should not be considered financial advice. Always test thoroughly in a paper trading environment before deploying any strategy with real money.
