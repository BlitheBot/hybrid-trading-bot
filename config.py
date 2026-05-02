import os

class Config:
    # Alpaca API credentials
    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
    PAPER_TRADING = True  # Set to False for live trading

    # Slack Webhooks
    SLACK_ALERTS_WEBHOOK = os.getenv("SLACK_ALERTS_WEBHOOK")
    SLACK_DECISIONS_WEBHOOK = os.getenv("SLACK_DECISIONS_WEBHOOK")
    SLACK_PERFORMANCE_WEBHOOK = os.getenv("SLACK_PERFORMANCE_WEBHOOK")
    SLACK_HEALTH_WEBHOOK = os.getenv("SLACK_HEALTH_WEBHOOK")

    # Risk Management Parameters (General)
    EQUITY_RISK_PER_TRADE_PERCENT = 2.0  # Percentage of total equity to risk per trade
    STOP_LOSS_PERCENT = 2.0              # Percentage drop from entry price to trigger stop-loss
    TAKE_PROFIT_PERCENT = 6.0            # Percentage gain from entry price to trigger take-profit
    MAX_BUYING_POWER_UTILIZATION_PERCENT = 10.0 # Max percentage of buying power to use for a single trade

    # New: Daily Loss Limit
    MAX_DAILY_LOSS_PERCENT = 3.0 # If hit, stop all new trading for the rest of the day

    # Scalping Bot Parameters (Crypto)
    SCALP_SYMBOLS = ["BTC/USD", "ETH/USD"]
    CRYPTO_SCALP_STOP_LOSS_PERCENT = 4.0 # Wider stop losses for crypto scalp trades (4-8%)

    # Swing Bot Parameters (Stocks)
    SWING_SYMBOLS = ["MSFT", "AAPL", "NVDA", "AMZN", "SPY", "QQQ"]
    SWING_EQUITY_RISK_PERCENT = 1.0 # Smaller position sizes for swing trades

    # Trading parameters (will be dynamically calculated based on risk management)
    # TRADE_AMOUNT = 100  # No longer a fixed amount, calculated dynamically

    # Add more configuration parameters as needed
