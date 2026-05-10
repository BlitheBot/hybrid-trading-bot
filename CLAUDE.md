# Hybrid Trading Bot — System Reference

This file gives a Claude Code session instant context on the full system. Read this before touching anything.

---

## What This Bot Does

A Python asyncio trading bot running 24/7 on Railway. It runs 9 concurrent loops covering crypto scalping (WebSocket), stock swing trading (daily), news sentiment (Benzinga via Alpaca), political sentiment (Truth Social), insider trade signals (SEC EDGAR Form 4), and housekeeping (trailing stops, DB exit logging, Slack health reports). All trade decisions go to Slack. High-conviction signals auto-trade via Alpaca. All completed trades are logged to PostgreSQL for ML training data.

---

## File Inventory

### Core Runtime

| File | Purpose |
|---|---|
| `bot.py` | Main `TradingBot` class — 9 async loops, all trade execution, DB logging, market regime check, bull/bear debate, fundamentals gate |
| `config.py` | All config constants as class attributes; reads `.env` via `load_dotenv()` before class definition (critical — class attributes are evaluated at import time) |
| `notifications.py` | Slack webhook functions for each channel: alerts, decisions, health, performance |
| `utils.py` | `get_historical_bars()` (Alpaca + Finnhub real-time overlay), `get_spy_data()`, `get_finnhub_price()` |
| `dashboard.py` | Streamlit dashboard — account metrics, open positions, recent orders, signal_outcomes, discovery results, analytics |
| `requirements.txt` | All dependencies (see Dependencies section) |

### Strategies (Active)

| File | Used By | Purpose |
|---|---|---|
| `strategies/base_strategy.py` | All strategies | Abstract base: `generate_signals()`, `execute_trade()`, `calculate_safe_quantity()`, `is_already_in_position()` |
| `strategies/swing_strategy.py` | `swing_loop` | EMA crossover + MACD + RSI swing signals with configurable per-symbol params; R/R ratio gate |
| `strategies/smb_strategy.py` | `scalp_loop` | Late-day crypto scalp; EMA9, relative strength vs SPY, 3:1 R/R; used for BTC/USD + ETH/USD |
| `strategies/news_strategy.py` | `news_loop` | Benzinga news via Alpaca News API; Claude NLP scoring with keyword fallback; dynamic sleep interval |
| `strategies/truth_social_strategy.py` | `truth_social_loop` | Trump Truth Social RSS; Claude NLP for ticker extraction + sentiment |
| `strategies/sec_edgar_strategy.py` | `sec_edgar_loop` | SEC EDGAR Form 4 insider trades; ElementTree XML parsing; strength-tiered scoring |

### Strategies (Legacy — in repo, not wired into bot)

`strategies/sma_crossover.py`, `strategies/hybrid_strategy.py`, `strategies/mean_reversion.py`, `strategies/rsi_strategy.py` — early prototypes, not used in production.

### Discovery Engine

| File | Purpose |
|---|---|
| `discovery/__init__.py` | Empty — marks directory as Python package |
| `discovery/discovery_engine.py` | Walk-forward backtester; 243-combo grid search over EMA/RSI params; scipy t-test validation; writes `strategy_results` + `signal_outcomes` to PostgreSQL; parquet cache in `discovery/data/` (24h TTL, gitignored) |

### Data

| File | Purpose |
|---|---|
| `data/sp500_tickers.py` | `SP500_TICKERS` list used by news, truth social, and EDGAR strategies to filter signals |

---

## 9 Concurrent Loops (`asyncio.gather` in `start_dual_engine`)

| # | Method | Interval | What It Does |
|---|---|---|---|
| 1 | `scalp_loop` | WebSocket (continuous) | Crypto scalp on BTC/USD + ETH/USD via CryptoDataStream; evaluates whichever has stronger RSI momentum |
| 2 | `swing_loop` | Daily 10:30 AM EST | Evaluates all 6 SWING_SYMBOLS with per-symbol SwingStrategy instances; gates via fundamentals check + Claude bull/bear debate |
| 3 | `news_loop` | 60s–15min (dynamic) | Scans all S&P 500 tickers for Benzinga headlines; Claude NLP scores each; alerts to Slack, auto-trades if strength ≥ threshold |
| 4 | `truth_social_loop` | 60s | Polls Trump's Truth Social RSS; Claude extracts tickers + sentiment; 50% position sizing, wider TP |
| 5 | `sec_edgar_loop` | 30 min | Polls EDGAR Form 4 RSS; parses XML for open-market transactions; alerts on buys ≥ $100k and sells ≥ $500k; auto-trades on $1M+ buys |
| 6 | `health_report_loop` | Daily 9 AM EST | Sends uptime, equity, buying power, daily P&L to #trading-health |
| 7 | `performance_report_loop` | Weekly Sun 6 PM EST | Sends weekly equity + active positions to #trading-performance |
| 8 | `trailing_stop_monitor_loop` | Every 60s | Upgrades static stop-loss orders to trailing stops once unrealized gain ≥ `TRAILING_STOP_ACTIVATION_PCT` (3%) |
| 9 | `_exit_monitor_loop` | Every 10 min | Queries Alpaca for closed sell orders; updates `signal_outcomes` with exit price, P&L%, and exit reason (stop/target/manual) |

---

## Database Schema (PostgreSQL)

Both tables are created by `_ensure_signal_outcomes_table()` in `bot.py` on startup, and by `_ensure_tables()` in `discovery_engine.py` when the discovery engine runs.

### `signal_outcomes` — Live trade log (primary ML training data)

```sql
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(10),
    signal_type   VARCHAR(20),      -- 'swing_long', 'scalp_long'
    entry_time    TIMESTAMP,
    exit_time     TIMESTAMP,        -- NULL until position closes
    entry_price   FLOAT,
    exit_price    FLOAT,            -- NULL until position closes
    pnl_pct       FLOAT,            -- NULL until position closes
    hold_bars     INTEGER,          -- days held (approximated from seconds)
    ema_short     INTEGER,
    ema_long      INTEGER,
    rsi_at_entry  FLOAT,
    macd_at_entry FLOAT,
    market_regime VARCHAR(20),      -- 'bull', 'bear', 'neutral' (SPY vs EMA200)
    exit_reason   VARCHAR(30),      -- 'stop', 'target', 'manual'
    discovered_at TIMESTAMP DEFAULT NOW()
);
```

Entry is logged after `execute_trade()` succeeds. Exit is backfilled by `_exit_monitor_loop` every 10 minutes.

### `strategy_results` — Discovery Engine walk-forward results

```sql
CREATE TABLE IF NOT EXISTS strategy_results (
    id                SERIAL PRIMARY KEY,
    symbol            VARCHAR(10),
    ema_short         INTEGER,
    ema_long          INTEGER,
    rsi_period        INTEGER,
    rsi_entry_low     FLOAT,
    rsi_entry_high    FLOAT,
    train_sharpe      FLOAT,
    test_sharpe       FLOAT,
    degradation       FLOAT,        -- train_sharpe - test_sharpe; lower is better
    p_value           FLOAT,        -- scipy t-test on walk-forward test_cagr values
    total_test_trades INTEGER,
    status            VARCHAR(20),  -- 'validated' or 'rejected'
    discovered_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE (symbol, ema_short, ema_long, rsi_period, rsi_entry_low, rsi_entry_high)
);
```

---

## Per-Symbol Swing Strategy Parameters

Determined by running the Discovery Engine (243-combo grid, 24-month train / 3-month test walk-forward, scipy t-test p < 0.05). Run with `python -m discovery.discovery_engine`.

| Symbol | ema_short | ema_long | rsi_period | rsi_entry_low | rsi_entry_high | Validated | Notes |
|---|---|---|---|---|---|---|---|
| COST | 20 | 100 | 10 | 35 | 65 | 125/243 | Best test Sharpe 0.87; short EMA crossover dominates |
| BRK.B | 50 (default) | 200 (default) | 21 | 40 | 65 | 24/243 | RSI21 + wide upper band required |
| SPY | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 9/243 | Defaults already optimal |
| V | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 0/243 | No validated edge; monitoring only |
| JPM | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 0/243 | No validated edge; flagged in swing_loop logs |
| PG | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 0/243 | No validated edge; flagged in swing_loop logs |

AMZN and V were run as replacement candidates for JPM/PG — both returned 0 validated combos, so JPM and PG were kept.

---

## Full Config Reference (`config.py`)

```python
# Alpaca
ALPACA_API_KEY          # from env
ALPACA_SECRET_KEY       # from env
PAPER_TRADING = True    # set False for live trading — DO NOT CHANGE without review

# Slack (from env)
SLACK_ALERTS_WEBHOOK      # #trading-alerts — errors, critical, trade fills
SLACK_DECISIONS_WEBHOOK   # #trading-decisions — all signals, debate results, skips
SLACK_PERFORMANCE_WEBHOOK # #trading-performance — weekly reports
SLACK_HEALTH_WEBHOOK      # #trading-health — daily health

# Risk Management
EQUITY_RISK_PER_TRADE_PERCENT = 2.0   # % of equity to risk per scalp trade
STOP_LOSS_PERCENT = 2.0               # % drop from entry → stop
TAKE_PROFIT_PERCENT = 6.0             # % gain from entry → take profit
MAX_BUYING_POWER_UTILIZATION_PERCENT = 10.0  # max % of buying power per single trade

# Graduated Daily Loss Limits
DAILY_LOSS_REDUCTION_1_PERCENT = 2.0  # hit 2% daily loss → risk_multiplier = 0.75
DAILY_LOSS_REDUCTION_2_PERCENT = 3.5  # hit 3.5% daily loss → risk_multiplier = 0.50
MAX_DAILY_LOSS_PERCENT = 5.0          # hit 5% daily loss → all new trading halted for day

# Swing Specific
SWING_MIN_RR_RATIO = 2.0              # minimum reward:risk to enter a swing trade
SWING_EQUITY_RISK_PERCENT = 1.0       # smaller position sizing for swings (vs scalps)
SWING_SYMBOLS = ["JPM", "SPY", "COST", "BRK.B", "PG", "V"]

# Crypto Scalp
SCALP_SYMBOLS = ["BTC/USD", "ETH/USD"]
CRYPTO_SCALP_STOP_LOSS_PERCENT = 4.0  # wider stop for crypto volatility

# Trailing Stop
TRAILING_STOP_ACTIVATION_PCT = 0.03   # activate trailing stop at 3% unrealized gain
TRAILING_STOP_TRAIL_PCT = 0.015       # trail at 1.5% below high-water mark

# Cooldowns
SYMBOL_COOLDOWN_MINUTES = 120         # block re-entry after a stop-loss on same symbol
MIN_PRICE_MOVEMENT_PCT = 0.0015       # ignore crypto ticks smaller than 0.15% move

# News / Benzinga
NEWS_SIGNAL_ALERT_THRESHOLD = 7       # strength ≥ 7 → Slack alert
NEWS_SIGNAL_AUTO_TRADE_THRESHOLD = 13 # strength ≥ 13 → auto-trade

# Truth Social
TRUTH_SOCIAL_ALERT_THRESHOLD = 7
TRUTH_SOCIAL_AUTO_TRADE_THRESHOLD = 13
TRUTH_SOCIAL_STOP_LOSS = 2.0          # 2% SL (tighter — political news fades fast)
TRUTH_SOCIAL_TAKE_PROFIT = 8.0        # 8% TP (wider — catch initial spike)
TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER = 0.50  # 50% of normal size (higher uncertainty)

# SEC EDGAR Insider Trades
SEC_EDGAR_ALERT_THRESHOLD = 6         # strength ≥ 6 → Slack alert
SEC_EDGAR_AUTO_TRADE_THRESHOLD = 13   # only $1M+ insider buys reach this (strength=14)
SEC_EDGAR_MIN_BUY_VALUE = 100_000     # ignore buys below $100k
SEC_EDGAR_MIN_SELL_VALUE = 500_000    # ignore sells below $500k

# Anthropic / Claude
ANTHROPIC_API_KEY   # from env — used for bull/bear debate and news NLP

# Finnhub
FINNHUB_API_KEY     # from env — used for real-time stock price overlay and fundamentals check

# Discovery Engine
BACKTEST_START_DATE = "2019-01-01"
BACKTEST_END_DATE = "2024-12-31"
WALK_FORWARD_TRAIN_MONTHS = 24
WALK_FORWARD_TEST_MONTHS = 3
DISCOVERY_SYMBOLS = ["JPM", "SPY", "COST", "BRK.B", "PG"]
DISCOVERY_MIN_TRADES = 10             # reject combos with fewer than 10 test trades
DISCOVERY_P_VALUE_THRESHOLD = 0.05    # scipy t-test significance threshold
DATABASE_URL   # from env — PostgreSQL connection string
```

---

## Railway Environment Variables (Required)

Set these in Railway → Project → Variables:

| Variable | Required | Notes |
|---|---|---|
| `ALPACA_API_KEY` | Yes | Alpaca paper or live API key |
| `ALPACA_SECRET_KEY` | Yes | Alpaca secret key |
| `ANTHROPIC_API_KEY` | Yes | Claude API key — bot degrades gracefully if missing but debate/NLP features disabled |
| `FINNHUB_API_KEY` | Yes | Free tier is sufficient; bot continues without it but fundamentals gate always passes |
| `SLACK_ALERTS_WEBHOOK` | Yes | Incoming webhook URL for #trading-alerts |
| `SLACK_DECISIONS_WEBHOOK` | Yes | Incoming webhook URL for #trading-decisions |
| `SLACK_PERFORMANCE_WEBHOOK` | Yes | Incoming webhook URL for #trading-performance |
| `SLACK_HEALTH_WEBHOOK` | Yes | Incoming webhook URL for #trading-health |
| `DATABASE_URL` | Optional | PostgreSQL URL (e.g. `postgresql://user:pass@host/db`); without it, all DB calls silently no-op and `signal_outcomes` logging is disabled |

---

## Swing Trade Signal Flow

```
swing_loop() [daily 10:30 AM EST]
  └─ for each symbol in SWING_SYMBOLS:
       └─ _process_symbol(symbol, [strategy], pre_execute_hook=_swing_pre_trade_hook)
            ├─ get_historical_bars(365 days daily)  ← Alpaca IEX + Finnhub overlay
            ├─ SwingStrategy.generate_signals(df)
            │    ├─ EMA_short > EMA_long (bullish trend)
            │    ├─ MACD crossed above signal line (momentum confirmation)
            │    ├─ rsi_entry_low ≤ RSI ≤ rsi_entry_high (not overbought/oversold)
            │    └─ reward/risk ≥ SWING_MIN_RR_RATIO (2.0) → else signal='hold'
            │
            ├─ [if signal='buy'] _swing_pre_trade_hook(symbol, signal, strategy)
            │    ├─ _check_fundamentals(symbol)   ← Finnhub /stock/metric + /calendar/earnings
            │    │    ├─ Negative P/E → BLOCK
            │    │    ├─ EPS decline > 20% YoY → BLOCK
            │    │    └─ Earnings within 48h → BLOCK
            │    │
            │    └─ _debate_trade(symbol, signal, strategy)   ← 3 Claude calls
            │         ├─ Call 1: Bull case (2 sentences)
            │         ├─ Call 2: Bear case (2 sentences)
            │         └─ Call 3: BUY or SKIP verdict
            │              └─ SKIP → notify_trade_skipped → abort
            │
            ├─ strategy.execute_trade(signal, trading_client, ...)
            │    └─ MarketOrderRequest with TakeProfitRequest + StopLossRequest (bracket order)
            │
            └─ _log_trade_entry(symbol, ...)   ← INSERT into signal_outcomes
                 └─ stores (row_id, entry_price, entry_time) in _open_trade_ids[symbol]

_exit_monitor_loop() [every 10 min, concurrent]
  └─ get_orders(CLOSED, last 24h)
       └─ for each filled sell order matching _open_trade_ids:
            └─ _update_trade_exit(row_id, exit_price, exit_reason, pnl_pct)
```

---

## Current Watchlist and Rationale

### Swing Symbols (`SWING_SYMBOLS`)

| Symbol | Status | Rationale |
|---|---|---|
| COST | Priority — 125/243 validated | Costco: defensive growth, consistent momentum, discovery found strong short-EMA crossover edge |
| BRK.B | Active — 24/243 validated | Berkshire: low volatility, long hold periods suit RSI21 wider bands |
| SPY | Active — 9/243 validated | Broad market; acts as portfolio hedge and regime indicator |
| V | Monitoring — 0/243 validated | Visa: high-quality business but no discovered edge yet; included for future data collection |
| JPM | Monitoring — 0/243, no edge | JPMorgan: flagged in logs with "no statistically validated edge" warning |
| PG | Monitoring — 0/243, no edge | Procter & Gamble: same flag; low volatility reduces signal opportunities |

### Crypto Scalp Symbols

| Symbol | Notes |
|---|---|
| BTC/USD | Primary crypto scalp via WebSocket |
| ETH/USD | Secondary; only traded when RSI stronger than BTC |

---

## Known Issues and Tech Debt

1. **`news_strategy.py` uses `claude-opus-4-5`** — this model ID may be outdated. Current IDs: `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5-20251001`. Consider updating to `claude-sonnet-4-6` to reduce cost.

2. **`bot.py` is ~1050 lines** — monolithic. The DB methods, regime check, fundamentals gate, and debate could be extracted into `trading/risk.py` or similar, but this is low priority until the bot needs significant new features.

3. **V, JPM, PG have no validated edge** — they remain in `SWING_SYMBOLS` to accumulate live `signal_outcomes` data. Consider removing after 6 months if they don't signal (or are consistently blocked by fundamentals gate).

4. **Alpaca 15-minute delay** — free IEX feed has 15-min delay for stocks. Mitigated by Finnhub real-time price overlay in `get_historical_bars()`. Crypto is unaffected (real-time WebSocket).

5. **`_seen_accessions` in `SECEdgarStrategy` is in-memory** — on Railway restart, it re-processes the last 40 filings. The 4-hour cooldown per ticker prevents duplicate signals but ~80 duplicate HTTP requests will happen on each cold start. Acceptable for 30-min polling.

6. **`dashboard.py` PostgreSQL integration** — the existing dashboard connects to Alpaca only. The DB views (signal_outcomes, strategy_results) are a planned enhancement.

7. **`strategies/sma_crossover.py` is imported at top of `bot.py`** — it's unused but still imported. Can be removed.

8. **`backtester.py`, `run_all.py`** — purpose unclear; these may be early prototypes. Do not modify without understanding them first.

---

## Dependencies (`requirements.txt`)

```
alpaca-py          # Alpaca trading + data SDK
pandas             # DataFrames
pandas-ta          # Technical indicators (EMA, MACD, RSI)
numpy
scipy              # t-test for discovery engine validation
pytz
requests
anthropic          # Claude API (NLP scoring, bull/bear debate)
python-dotenv      # .env loading
flask              # /health endpoint (port 8501)
streamlit          # Dashboard
plotly             # Charts in dashboard
matplotlib
seaborn
psycopg2-binary    # PostgreSQL driver
pyarrow            # Parquet cache for discovery engine
```

---

## Running Locally

```bash
# Bot
python bot.py

# Dashboard
streamlit run dashboard.py

# Discovery Engine (full 243-combo run, ~7 min)
python -m discovery.discovery_engine

# Discovery Engine (quick test — edit PARAM_GRID in discovery_engine.py first)
python -m discovery.discovery_engine
```

The bot requires all Railway env vars in a local `.env` file. `config.py` calls `load_dotenv()` at module load time — this is intentional and critical. Do not move it.
