import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    # Alpaca API credentials
    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
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
    MIN_DOLLAR_VOLUME = 10_000_000  # skip trades where avg daily dollar volume < $10M
    TRAILING_STOP_ACTIVATION_PCT = 0.03
    TRAILING_STOP_TRAIL_PCT = 0.015
    SYMBOL_COOLDOWN_MINUTES = 120
    MIN_PRICE_MOVEMENT_PCT = 0.0015

    # Scalping Bot Parameters (Crypto)
    SCALP_ENABLED = True  # set True to re-enable WebSocket crypto scalping
    SCALP_SYMBOLS = ["BTC/USD", "ETH/USD"]
    CRYPTO_SCALP_STOP_LOSS_PERCENT = 4.0 # Wider stop losses for crypto scalp trades (4-8%)

    # Crypto Momentum Strategy (Task 6) — simpler/more-frequent EMA crossover scalp
    # that runs alongside the SMB late scalp; best signal (higher confidence) wins.
    CRYPTO_MOMENTUM_ENABLED = os.getenv("CRYPTO_MOMENTUM_ENABLED", "true").lower() != "false"
    CRYPTO_MOMENTUM_EMA_FAST = int(os.getenv("CRYPTO_MOMENTUM_EMA_FAST", "9"))
    CRYPTO_MOMENTUM_EMA_SLOW = int(os.getenv("CRYPTO_MOMENTUM_EMA_SLOW", "21"))
    CRYPTO_MOMENTUM_VOL_MULT = float(os.getenv("CRYPTO_MOMENTUM_VOL_MULT", "1.2"))  # vol > 1.2x avg of last 20 bars
    CRYPTO_MOMENTUM_ATR_STOP_MULT = float(os.getenv("CRYPTO_MOMENTUM_ATR_STOP_MULT", "1.5"))
    CRYPTO_MOMENTUM_ATR_TARGET_MULT = float(os.getenv("CRYPTO_MOMENTUM_ATR_TARGET_MULT", "3.0"))  # R/R = 2.0
    CRYPTO_MOMENTUM_COOLDOWN_MINUTES = int(os.getenv("CRYPTO_MOMENTUM_COOLDOWN_MINUTES", "15"))
    CRYPTO_MOMENTUM_MIN_MOVE_PCT = float(os.getenv("CRYPTO_MOMENTUM_MIN_MOVE_PCT", "0.001"))  # 0.1% min move since last signal

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

    # OpenRouter (used by call_llm_with_model for task-specific model routing)
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

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
    NEWS_CLAUDE_SCORING_ENABLED = True   # DeepSeek Flash free tier via OpenRouter
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
    # xAI API key for Grok stock sentiment scorer (grok-3-mini-fast)
    XAI_API_KEY          = os.getenv("XAI_API_KEY")
    GROK_ENABLED         = True
    GROK_ALERT_THRESHOLD = 7   # score ≥ 7 (bullish) or ≤ 3 (bearish) fires alert
    GROK_STRATEGY_INTERVAL_MINUTES  = int(os.getenv("GROK_STRATEGY_INTERVAL_MINUTES",  "120"))
    GROK_SENTIMENT_INTERVAL_MINUTES = int(os.getenv("GROK_SENTIMENT_INTERVAL_MINUTES", "120"))

    # Webull contrarian retail-crowding signal (alert-only)
    WEBULL_ENABLED         = False  # endpoint returns 417; disabled until a working source is found
    WEBULL_ALERT_THRESHOLD = 5.0  # minimum intraday gain % to flag as crowded

    # Short selling
    SHORT_SELLING_ENABLED = os.getenv("SHORT_SELLING_ENABLED", "true").lower() != "false"

    # Diagnostic / verbose logging flags
    SWING_VERBOSE_LOGGING = True   # log EMA/RSI/MACD values + exact hold reason each evaluation
    BULL_BEAR_DEBATE_ENABLED = True      # DeepSeek Flash via OpenRouter with web search
    DISCOVERY_DEBATE_ENABLED = True      # Discovery Engine only — Claude reviews each validated strategy
    SLACK_VERBOSE = False                # False = critical/trade alerts only; True = all signals fire

    # Enhanced signal quality scoring (Task 5)
    # Composite 0-10 score from technical/sentiment/regime/insider/volume components.
    SIGNAL_QUALITY_ENABLED = os.getenv("SIGNAL_QUALITY_ENABLED", "true").lower() != "false"  # compute + log + store always
    SIGNAL_QUALITY_GATING_ENABLED = os.getenv("SIGNAL_QUALITY_GATING_ENABLED", "false").lower() != "false"  # block trades below min + scale size; off by default until MACD calibration verified in live logs
    SIGNAL_QUALITY_MIN_SCORE = float(os.getenv("SIGNAL_QUALITY_MIN_SCORE", "5.0"))  # minimum composite score to trade

    # Performance Brain
    PERFORMANCE_SCALING_ENABLED = True  # adjust position size based on last 20-trade win rate
    POSITION_SIZE_FLOOR = 0.1           # floor: no trade below 10% of SWING_EQUITY_RISK_PERCENT

    # Portfolio heat cap
    PORTFOLIO_HEAT_CAP = 0.15   # max aggregate open-position risk as % of equity

    # Risk Management Upgrade (Task 8) — all env-var configurable
    MAX_SECTOR_CONCENTRATION_PCT = float(os.getenv("MAX_SECTOR_CONCENTRATION_PCT", "30.0"))  # max % of exposure in one GICS sector
    MAX_SINGLE_POSITION_PCT = float(os.getenv("MAX_SINGLE_POSITION_PCT", "5.0"))             # max single position as % of equity at entry
    WEEKLY_LOSS_LIMIT_PCT = float(os.getenv("WEEKLY_LOSS_LIMIT_PCT", "-3.0"))                # weekly P&L below this → size reduction
    WEEKLY_LOSS_SIZE_REDUCTION = float(os.getenv("WEEKLY_LOSS_SIZE_REDUCTION", "0.5"))       # multiplier applied for rest of week
    CONSECUTIVE_LOSS_LIMIT = int(os.getenv("CONSECUTIVE_LOSS_LIMIT", "5"))                   # consecutive losers → pause entries
    CONSECUTIVE_LOSS_PAUSE_HOURS = float(os.getenv("CONSECUTIVE_LOSS_PAUSE_HOURS", "2.0"))   # pause duration on tripping
    RISK_STATE_CACHE_SECONDS = int(os.getenv("RISK_STATE_CACHE_SECONDS", "300"))             # risk-state recompute TTL

    # Backtester / Strategy Discovery Engine
    BACKTEST_START_DATE = "2019-01-01"
    BACKTEST_END_DATE = "2024-12-31"
    WALK_FORWARD_TRAIN_MONTHS = 24
    WALK_FORWARD_TEST_MONTHS = 3

    # Discovery Engine
    DISCOVERY_SYMBOLS = ["JPM", "SPY", "COST", "BRK.B", "PG"]
    DISCOVERY_MIN_TRADES = 10
    DISCOVERY_P_VALUE_THRESHOLD = 0.05
    # Multi-factor discovery families (Task 3): run mean-reversion, volume-breakout
    # and insider-flow families alongside the EMA/MACD/RSI momentum family.
    DISCOVERY_MULTI_FAMILY_ENABLED = os.getenv("DISCOVERY_MULTI_FAMILY_ENABLED", "true").lower() != "false"

    # Correlation-aware portfolio construction (Task 4)
    PORTFOLIO_OPTIMIZER_ENABLED = os.getenv("PORTFOLIO_OPTIMIZER_ENABLED", "true").lower() != "false"
    PORTFOLIO_MAX_CORRELATION = float(os.getenv("PORTFOLIO_MAX_CORRELATION", "0.7"))  # add only if corr w/ all selected < this
    PORTFOLIO_MAX_SIZE = int(os.getenv("PORTFOLIO_MAX_SIZE", "20"))                   # max strategy/symbol combos
    PORTFOLIO_MIN_SHARPE = float(os.getenv("PORTFOLIO_MIN_SHARPE", "0.5"))           # min combined portfolio Sharpe to deploy
    PORTFOLIO_MIN_OVERLAP = int(os.getenv("PORTFOLIO_MIN_OVERLAP", "10"))            # min overlapping daily obs to trust a correlation
    # When on, the swing screener only evaluates symbols in the current optimal portfolio.
    PORTFOLIO_GATING_ENABLED = os.getenv("PORTFOLIO_GATING_ENABLED", "true").lower() != "false"
    DATABASE_URL = os.getenv("DATABASE_URL")

    # Permutation Validation Framework (Timothy Masters 4-step MCPT)
    # Mandatory second gate after the SciPy t-test before a strategy is recorded
    # as fully validated. Eliminates data-mining bias and out-of-sample selection luck.
    PERMUTATION_ENABLED = os.getenv("PERMUTATION_ENABLED", "true").lower() != "false"
    PERMUTATION_P_THRESHOLD = 0.01          # quasi p-value gate for both IS and WF tests
    PERMUTATION_INSAMPLE_ITERS = 1000       # Monte Carlo iterations for in-sample test
    PERMUTATION_WALKFORWARD_ITERS = 200     # Monte Carlo iterations for walk-forward test
    PERMUTATION_OBJECTIVE = "profit_factor" # objective function: "profit_factor" or "sharpe"
    PERMUTATION_WORKERS = 0                 # multiprocessing workers; 0 = cpu_count() - 1
    PERMUTATION_MOMENT_TOLERANCE = 0.01     # 1% tolerance for moment-preservation validation

    # Transaction cost model (Task 1) — applied inside the permutation backtester
    # so strategies are only validated if they survive realistic costs.
    COST_MODELING_ENABLED = os.getenv("COST_MODELING_ENABLED", "true").lower() != "false"
    COST_LIQUID_DOLLAR_VOLUME = float(os.getenv("COST_LIQUID_DOLLAR_VOLUME", "100000000"))  # >$100M avg daily $vol = liquid
    COST_SPREAD_LIQUID_PCT = float(os.getenv("COST_SPREAD_LIQUID_PCT", "0.0005"))    # 0.05% per side, liquid
    COST_SPREAD_ILLIQUID_PCT = float(os.getenv("COST_SPREAD_ILLIQUID_PCT", "0.0010"))  # 0.10% per side, $10M-$100M
    COST_IMPACT_SMALL_PCT = float(os.getenv("COST_IMPACT_SMALL_PCT", "0.0010"))   # order < 0.1% ADV
    COST_IMPACT_MEDIUM_PCT = float(os.getenv("COST_IMPACT_MEDIUM_PCT", "0.0025"))  # 0.1%-0.5% ADV
    COST_IMPACT_LARGE_PCT = float(os.getenv("COST_IMPACT_LARGE_PCT", "0.0050"))   # > 0.5% ADV
    COST_ADV_FRACTION = float(os.getenv("COST_ADV_FRACTION", "0.0005"))  # assumed order size as fraction of ADV (default <0.1%)
    COST_BORROW_EASY_ANNUAL = float(os.getenv("COST_BORROW_EASY_ANNUAL", "0.0050"))   # 0.50% annualized easy-to-borrow
    COST_BORROW_HARD_ANNUAL = float(os.getenv("COST_BORROW_HARD_ANNUAL", "0.0200"))   # 2.00% annualized hard-to-borrow
    COST_HARD_TO_BORROW = os.getenv("COST_HARD_TO_BORROW", "false").lower() == "true"  # treat shorts as hard-to-borrow
    COST_MIN_NET_SHARPE = float(os.getenv("COST_MIN_NET_SHARPE", "0.0"))  # validated strategies must exceed this net-of-cost Sharpe

    # Regime classifier (4 market regimes) + live regime gating
    REGIME_HIGH_VOL_VIX = 30.0      # VIX > this => HIGH_VOL (overrides trend)
    REGIME_BULL_VIX_MAX = 20.0      # BULL_TREND requires VIX < this
    REGIME_BEAR_VIX_MIN = 25.0      # BEAR_TREND requires VIX > this
    REGIME_BULL_RETURN_PCT = 0.02   # BULL_TREND requires SPY 20-day return > +2%
    REGIME_BEAR_RETURN_PCT = -0.02  # BEAR_TREND requires SPY 20-day return < -2%
    REGIME_CACHE_SECONDS = 14400    # live regime cache TTL (4 hours)
    REGIME_MIN_BARS = 50            # min bars in a regime to score / validate it
    REGIME_GATING_ENABLED = os.getenv("REGIME_GATING_ENABLED", "true").lower() != "false"

    # Strategy decay monitoring — detects validated strategies that stop working live
    DECAY_MONITOR_ENABLED = os.getenv("DECAY_MONITOR_ENABLED", "true").lower() != "false"
    DECAY_MIN_SIGNALS = 30          # min closed signals before any decay action (never penalize thin data)
    DECAY_CRITICAL_MIN_SIGNALS = 15 # min recent signals for the negative-Sharpe critical check
    DECAY_LOOKBACK_SIGNALS = 30     # window of recent closed signals analyzed
    DECAY_HEALTHY_RATIO = 0.8       # ratio >= this => HEALTHY (1.0x)
    DECAY_DEGRADED_RATIO = 0.5      # ratio in [0.5, 0.8) => DEGRADED (0.5x)
    DECAY_DEGRADED_MULT = 0.5       # DEGRADED position multiplier
    DECAY_DECAYING_MULT = 0.25      # DECAYING position multiplier
    DECAY_MULTIPLIER_FLOOR = 0.1    # floor applied when stacking decay multiplier
    DECAY_LOOP_INTERVAL_SECONDS = 21600   # decay monitor loop cadence (6 hours)
    DECAY_CACHE_SECONDS = 3600      # get_decay_status_all_strategies cache TTL (1 hour)
