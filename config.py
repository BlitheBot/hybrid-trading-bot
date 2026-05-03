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

    # Graduated Daily Loss Limits
    DAILY_LOSS_REDUCTION_1_PERCENT = 2.0  # Reduce new position sizes by 25%
    DAILY_LOSS_REDUCTION_2_PERCENT = 3.5  # Reduce new position sizes by 50%
    MAX_DAILY_LOSS_PERCENT = 5.0          # Stop all new trading for the day
    
    # Advanced Swing & Risk Parameters
    SWING_MIN_RR_RATIO = 2.0
    TRAILING_STOP_ACTIVATION_PCT = 0.03
    TRAILING_STOP_TRAIL_PCT = 0.015
    SYMBOL_COOLDOWN_MINUTES = 120
    MIN_PRICE_MOVEMENT_PCT = 0.0015

    # Scalping Bot Parameters (Crypto)
    SCALP_SYMBOLS = ["BTC/USD", "ETH/USD"]
    CRYPTO_SCALP_STOP_LOSS_PERCENT = 4.0 # Wider stop losses for crypto scalp trades (4-8%)

    # Swing Bot Parameters (Stocks)
    SWING_SYMBOLS = ["MSFT", "AAPL", "NVDA", "AMZN", "SPY", "QQQ"]
    SWING_EQUITY_RISK_PERCENT = 1.0 # Smaller position sizes for swing trades

    # Trading parameters (will be dynamically calculated based on risk management)
    # TRADE_AMOUNT = 100  # No longer a fixed amount, calculated dynamically

    # Add more configuration parameters as needed
