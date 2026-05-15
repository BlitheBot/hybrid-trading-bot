import os
from dotenv import load_dotenv
load_dotenv()

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
    SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")  # for slash-command request verification

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
    SCALP_ENABLED = False  # set True to re-enable WebSocket crypto scalping
    SCALP_SYMBOLS = ["BTC/USD", "ETH/USD"]
    CRYPTO_SCALP_STOP_LOSS_PERCENT = 4.0 # Wider stop losses for crypto scalp trades (4-8%)

    # Swing Bot Parameters (Stocks)
    SWING_SYMBOLS = ["JPM", "SPY", "COST", "BRK.B", "PG", "V"]
    SWING_EQUITY_RISK_PERCENT = 1.0 # Smaller position sizes for swing trades

    # Anthropic API credentials
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

    # LLM provider — 'anthropic' (default) or 'kimi' / 'openai_compatible'
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
    OPENAI_COMPATIBLE_API_KEY = os.getenv("OPENAI_COMPATIBLE_API_KEY")
    OPENAI_COMPATIBLE_BASE_URL = os.getenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.moonshot.cn/v1")
    OPENAI_COMPATIBLE_MODEL = os.getenv("OPENAI_COMPATIBLE_MODEL", "moonshot-v1-8k")

    # Quiver Quantitative API (congressional trading)
    QUIVER_API_KEY = os.getenv("QUIVER_API_KEY")

    # Polling intervals and rate limits
    NEWS_DEDUP_HOURS = 2                   # dedup / cooldown window per ticker
    NEWS_BATCH_SIZE = 50                   # symbols per Alpaca News API request
    SEC_EDGAR_COOLDOWN_HOURS = 4           # per-ticker cooldown after EDGAR signal
    SEC_EDGAR_RATE_LIMIT_SLEEP = 0.15      # seconds between EDGAR HTTP requests
    MARKET_REGIME_CACHE_SECONDS = 900      # 15-min SPY/EMA-200 regime cache TTL
    TRAILING_STOP_MONITOR_INTERVAL = 60    # seconds between trailing-stop checks

    # Sentiment & News Parameters
    NEWS_SIGNAL_ALERT_THRESHOLD = 7
    NEWS_SIGNAL_AUTO_TRADE_THRESHOLD = 13
    CLAUDE_DAILY_CALL_LIMIT = 100        # fall back to keyword scoring once exceeded
    NEWS_CLAUDE_SCORING_ENABLED = False  # set True to re-enable Claude NLP (costs API credits)
    TRUTH_SOCIAL_ENABLED = False  # Disabled: Truth Social blocks automated access; re-enable with Quiver API
    CONGRESSIONAL_ENABLED = False  # Disabled: free data sources unavailable; re-enable with QUIVER_API_KEY ($30/mo at quiverquant.com)
    FRED_ENABLED = True            # Free public FRED CSV endpoints — no API key required
    EARNINGS_FILTER_ENABLED = True # Reduce swing position size to 25% when earnings within 48h; skip if earnings today/tomorrow
    VIX_SPIKE_THRESHOLD = 35      # VIX above this → reduce position size to 25%
    VIX_EXTREME_THRESHOLD = 40    # VIX above this → block trade entirely, alert #trading-alerts
    BEAR_MARKET_SIZE_REDUCTION = 0.5  # Multiply swing position size by this when SPY < 200 EMA
    CANDLESTICK_CONFIRMATION_ENABLED = True  # Reduce swing conviction 20% when no bullish candlestick pattern on last 3 bars
    SENTRY_DSN = os.getenv("SENTRY_DSN")     # Sentry project DSN; omit to disable error monitoring

    # Reddit Momentum Signal Parameters
    REDDIT_ENABLED = True
    REDDIT_ALERT_THRESHOLD = 5.0       # combined mention score to send Slack alert
    REDDIT_MIN_MENTIONS = 3            # minimum post mentions across subreddits
    REDDIT_POLL_INTERVAL = 1800        # seconds between polls (30 min)
    REDDIT_AUTO_TRADE_THRESHOLD = 999  # alert-only stub; lower to enable auto-trade

    # Prometheus metrics endpoint
    PROMETHEUS_ENABLED = True          # expose /metrics on port 8502 for Grafana scraping
    CONGRESSIONAL_ALERT_THRESHOLD = 6     # any S&P 500 buy above min amount → Slack alert
    CONGRESSIONAL_AUTO_TRADE_THRESHOLD = 13  # effectively unreachable (max ~11.2) — alert-only
    TRUTH_SOCIAL_ALERT_THRESHOLD = 7
    TRUTH_SOCIAL_AUTO_TRADE_THRESHOLD = 13
    
    TRUTH_SOCIAL_STOP_LOSS = 2.0  # 2% stop loss
    TRUTH_SOCIAL_TAKE_PROFIT = 8.0 # 8% take profit
    TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER = 0.50 # 50% of normal size

    # SEC EDGAR Insider Trading Signal Parameters
    SEC_EDGAR_ALERT_THRESHOLD = 6        # minimum strength to send Slack alert
    SEC_EDGAR_AUTO_TRADE_THRESHOLD = 13  # $1M+ insider buys only (strength=14)
    SEC_EDGAR_MIN_BUY_VALUE = 100_000    # ignore buys below $100k
    SEC_EDGAR_MIN_SELL_VALUE = 500_000   # ignore sells below $500k

    # PagerDuty phone alerts (optional — omit PAGERDUTY_ROUTING_KEY to disable)
    PAGERDUTY_ROUTING_KEY = os.getenv("PAGERDUTY_ROUTING_KEY")

    # Notion trade journal (optional — omit either key to disable)
    NOTION_API_KEY      = os.getenv("NOTION_API_KEY")
    NOTION_DATABASE_ID  = os.getenv("NOTION_DATABASE_ID")

    # Grok (xAI) X/Twitter crypto sentiment (alert-only)
    GROK_API_KEY         = os.getenv("GROK_API_KEY")
    GROK_ENABLED         = True
    GROK_ALERT_THRESHOLD = 7   # score ≥ 7 (bullish) or ≤ 3 (bearish) fires alert

    # Webull contrarian retail-crowding signal (alert-only)
    WEBULL_ENABLED         = True
    WEBULL_ALERT_THRESHOLD = 5.0  # minimum intraday gain % to flag as crowded

    # Diagnostic / verbose logging flags
    SWING_VERBOSE_LOGGING = True   # log EMA/RSI/MACD values + exact hold reason each evaluation
    BULL_BEAR_DEBATE_ENABLED = False     # set True to re-enable Claude debate gate (costs API credits)
    DISCOVERY_DEBATE_ENABLED = False     # reserved for future discovery engine Claude integration

    # Performance Brain
    PERFORMANCE_SCALING_ENABLED = True  # adjust position size based on last 20-trade win rate
    POSITION_SIZE_FLOOR = 0.1           # floor: no trade below 10% of SWING_EQUITY_RISK_PERCENT

    # Portfolio heat cap
    PORTFOLIO_HEAT_CAP = 0.15   # max aggregate open-position risk as % of equity

    # Backtester / Strategy Discovery Engine
    BACKTEST_START_DATE = "2019-01-01"
    BACKTEST_END_DATE = "2024-12-31"
    WALK_FORWARD_TRAIN_MONTHS = 24
    WALK_FORWARD_TEST_MONTHS = 3

    # Discovery Engine
    DISCOVERY_SYMBOLS = ["JPM", "SPY", "COST", "BRK.B", "PG"]
    DISCOVERY_MIN_TRADES = 10
    DISCOVERY_P_VALUE_THRESHOLD = 0.05
    DATABASE_URL = os.getenv("DATABASE_URL")
