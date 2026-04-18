import os

class Config:
    # Alpaca API credentials
    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
    PAPER_TRADING = True  # Set to False for live trading

    # Risk Management Parameters
    EQUITY_RISK_PER_TRADE_PERCENT = 2.0  # Percentage of total equity to risk per trade
    STOP_LOSS_PERCENT = 2.0              # Percentage drop from entry price to trigger stop-loss
    TAKE_PROFIT_PERCENT = 6.0            # Percentage gain from entry price to trigger take-profit

    # Trading parameters (will be dynamically calculated based on risk management)
    # TRADE_AMOUNT = 100  # No longer a fixed amount, calculated dynamically

    # Add more configuration parameters as needed
