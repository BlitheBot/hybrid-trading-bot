# Hybrid Trading Bot — Claude Code Session Rules

## Operating Rules

1. **Read this entire file** before touching any code file — the architecture notes and known issues section prevent repeat mistakes.
2. **Never auto-deploy strategies** — all strategy logic changes require explicit user confirmation before Railway deployment.
3. **Always run syntax check** before committing:
   ```
   python -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('.').rglob('*.py') if '.git' not in str(p) and 'discovery/data' not in str(p)]"
   ```
4. **Always commit and push** after completing code changes.

---

# Hybrid Trading Bot — System Reference

This file gives a Claude Code session instant context on the full system.

---

## What This Bot Does

A Python asyncio trading bot running 24/7 on Railway. It runs 9 concurrent loops covering crypto scalping (WebSocket), stock swing trading (daily), news sentiment (Benzinga via Alpaca), political sentiment (Truth Social — currently disabled), insider trade signals (SEC EDGAR Form 4), and housekeeping (trailing stops, DB exit logging, Slack health reports). All trade decisions go to Slack. High-conviction signals auto-trade via Alpaca. All completed trades are logged to PostgreSQL via SQLAlchemy for ML training data.

---

## File Inventory

### Core Runtime

| File | Purpose |
|---|---|
| `bot.py` | Main `TradingBot` class — 9 async loops, all trade execution, SQLAlchemy DB logging, market regime check, bull/bear debate, fundamentals gate |
| `config.py` | All config constants as class attributes; reads `.env` via `load_dotenv()` before class definition (critical — class attributes are evaluated at import time) |
| `notifications.py` | Slack webhook functions for each channel: alerts, decisions, health, performance |
| `utils.py` | `get_historical_bars()` (Alpaca + Finnhub real-time overlay), `get_spy_data()`, `get_finnhub_price()` |
| `dashboard.py` | 5-tab Streamlit dashboard — Account, Positions, Trade Log (signal_outcomes via SQLAlchemy), Discovery (strategy_results), Analytics (P&L chart + win rate) |
| `requirements.txt` | All dependencies (see Dependencies section) |

### Strategies (Active)

| File | Used By | Purpose |
|---|---|---|
| `strategies/base_strategy.py` | All strategies | Abstract base: `generate_signals()`, `execute_trade()`, `calculate_safe_quantity()`, `is_already_in_position()` |
| `strategies/swing_strategy.py` | `swing_loop` | EMA crossover + MACD + RSI swing signals with configurable per-symbol params; R/R ratio gate |
| `strategies/smb_strategy.py` | `scalp_loop` | Crypto scalp; EMA9 vs VWAP crossover, ATR-based stops, 3:1 R/R; BTC/USD + ETH/USD. Null check uses `iloc[-2:]` not full DataFrame (early rows have NaN from rolling indicators) |
| `strategies/news_strategy.py` | `news_loop` | Benzinga news via Alpaca News API; Claude NLP scoring with keyword fallback; dynamic sleep; 429 retry (10s/20s/30s) per batch |
| `strategies/truth_social_strategy.py` | `truth_social_loop` | Disabled — `TRUTH_SOCIAL_ENABLED=False`; scan_once() returns [] immediately. Re-enable when Quiver Quantitative API is integrated |
| `strategies/sec_edgar_strategy.py` | `sec_edgar_loop` | SEC EDGAR Form 4 insider trades; ElementTree XML parsing; strength-tiered scoring; exponential 429 backoff (30s/60s/120s) |
| `strategies/fred_strategy.py` | `fred_loop` | FRED macro indicators via free public CSV endpoints; module-level `MACRO_SNAPSHOT` dict + `get_conviction_multiplier()` function readable by any loop; no API key required |
| `strategies/congressional_trading_strategy.py` | `congressional_trading_loop` | Quiver Quantitative congressional trades; scores buys by amount ($50k/$250k tiers) + committee membership (1.3×) + recency ≤7 days (1.2×); informational sell signals at half strength; 4-hour per-ticker cooldown; self-disables on 401/403 |

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

## 12 Concurrent Loops (`asyncio.gather` in `start_dual_engine`)

| # | Method | Interval | What It Does |
|---|---|---|---|
| 1 | `scalp_loop` | WebSocket (continuous) | Crypto scalp on BTC/USD + ETH/USD via CryptoDataStream; evaluates whichever has stronger RSI momentum; sets `websocket_connected` in `_health_state` |
| 2 | `swing_loop` | Daily 10:30 AM EST | Evaluates all 6 SWING_SYMBOLS with per-symbol SwingStrategy instances; gates via fundamentals check + Claude bull/bear debate (bull+bear run in parallel via `asyncio.gather`) |
| 3 | `news_loop` | 60s–15min (dynamic) | Scans all S&P 500 tickers for Benzinga headlines; Claude NLP scores each; alerts to Slack, auto-trades if strength ≥ threshold; sets `last_news_scan_utc` in `_health_state` |
| 4 | `truth_social_loop` | — | **Disabled** (`TRUTH_SOCIAL_ENABLED=False`); returns immediately on startup |
| 5 | `sec_edgar_loop` | 30 min | Polls EDGAR Form 4 RSS; parses XML for open-market transactions; alerts on buys ≥ $100k and sells ≥ $500k; auto-trades on $1M+ buys; sets `last_edgar_scan_utc` in `_health_state` |
| 6 | `health_report_loop` | Daily 9 AM EST | Sends uptime, equity, buying power, daily P&L to #trading-health |
| 7 | `performance_report_loop` | Weekly Sun 6 PM EST | Sends weekly equity + active positions to #trading-performance |
| 8 | `trailing_stop_monitor_loop` | Every `TRAILING_STOP_MONITOR_INTERVAL` (60s) | Upgrades static stop-loss orders to trailing stops once unrealized gain ≥ `TRAILING_STOP_ACTIVATION_PCT` (3%) |
| 9 | `_exit_monitor_loop` | Every 10 min | Queries Alpaca for closed sell orders (7-day lookback, limit 200); updates `signal_outcomes` with exit price, P&L%, and exit reason (stop/target/manual). Protected by `_trade_ids_lock` |
| 10 | `market_open_notification_loop` | Daily 9:30 AM EST (Mon–Fri) | Sends morning briefing to #trading-alerts: equity, market regime, swing watchlist, reminder that swing evaluation fires at 10:30 AM EST |
| 11 | `fred_loop` | Daily 7 PM EST + startup fetch | Fetches 5 FRED macro indicators (FF rate, VIX, 10Y, unemployment, CPI YoY); updates `MACRO_SNAPSHOT`; VIX > 30 → 0.7× conviction multiplier in news + EDGAR loops; VIX > 40 → one-time `#trading-alerts` critical alert; Sunday 7 PM → weekly summary to `#trading-health` |
| 12 | `congressional_trading_loop` | — | **Disabled** (`CONGRESSIONAL_ENABLED=False`); free data sources unavailable; re-enable by setting `QUIVER_API_KEY` in Railway and flipping flag |

---

## Database Schema (PostgreSQL)

Both tables are created by `_ensure_signal_outcomes_table()` in `bot.py` on startup (via SQLAlchemy), and by `_ensure_tables()` in `discovery_engine.py` when the discovery engine runs.

**Discovery Engine results** were written to Railway PostgreSQL on 2026-05-10: 1215 rows across 5 symbols (JPM 0, SPY 9, COST 125, BRK.B 24, PG 0 validated of 243 combos each).

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

Entry is logged after `execute_trade()` succeeds. Exit is backfilled by `_exit_monitor_loop` every 10 minutes (7-day lookback window).

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

# Cooldowns & Rate Limits
SYMBOL_COOLDOWN_MINUTES = 120         # block re-entry after a stop-loss on same symbol
MIN_PRICE_MOVEMENT_PCT = 0.0015       # ignore crypto ticks smaller than 0.15% move
NEWS_DEDUP_HOURS = 2                  # per-ticker cooldown and news lookback window
NEWS_BATCH_SIZE = 50                  # symbols per Alpaca News API request (10 batches for 500 tickers)
SEC_EDGAR_COOLDOWN_HOURS = 4          # per-ticker cooldown after an EDGAR signal fires
SEC_EDGAR_RATE_LIMIT_SLEEP = 0.15     # seconds between EDGAR HTTP requests
MARKET_REGIME_CACHE_SECONDS = 900     # 15-min SPY/EMA-200 regime cache TTL
TRAILING_STOP_MONITOR_INTERVAL = 60   # seconds between trailing-stop checks

# News / Benzinga
NEWS_SIGNAL_ALERT_THRESHOLD = 7       # strength ≥ 7 → Slack alert
NEWS_SIGNAL_AUTO_TRADE_THRESHOLD = 13 # strength ≥ 13 → auto-trade

# Truth Social
TRUTH_SOCIAL_ENABLED = False          # disabled; re-enable with Quiver Quantitative API
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

# FRED Macro Indicators
FRED_ENABLED = True            # free public CSV endpoints; no API key required

# Congressional Trading (Quiver Quantitative)
QUIVER_API_KEY              # from env — free tier key from quiverquant.com; loop self-disables on 401/403
CONGRESSIONAL_ENABLED = True
CONGRESSIONAL_ALERT_THRESHOLD = 6      # all S&P 500 buys above min amount sent to Slack
CONGRESSIONAL_AUTO_TRADE_THRESHOLD = 13  # max achievable strength ~11.2 → effectively alert-only

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
| `QUIVER_API_KEY` | Optional | Quiver Quantitative free-tier API key; without it, congressional loop logs one 401 warning and exits permanently |

---

## Port Architecture

| Port | Service | Notes |
|---|---|---|
| 8501 | Streamlit dashboard | Railway public domain routes here |
| 8502 | Flask `/health` endpoint | Internal only; polled by dashboard sidebar |

The `/health` endpoint returns:
```json
{
  "status": "running",
  "uptime_seconds": 3600.0,
  "started_at": "2026-05-12T09:30:00+00:00",
  "db_connected": true,
  "alpaca_connected": true,
  "last_news_scan": "2026-05-12T10:32:00+00:00",
  "last_edgar_scan": "2026-05-12T10:00:00+00:00",
  "websocket_connected": true
}
```

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
            │    └─ _debate_trade(symbol, signal, strategy)   ← 2+1 Claude calls
            │         ├─ asyncio.gather(bull_call, bear_call)  ← parallel
            │         └─ verdict_call(bull, bear) → BUY or SKIP
            │              └─ SKIP → notify_trade_skipped → abort
            │
            ├─ strategy.execute_trade(signal, trading_client, ...)
            │    └─ MarketOrderRequest with TakeProfitRequest + StopLossRequest (bracket order)
            │
            └─ _log_trade_entry(symbol, ...)   ← INSERT into signal_outcomes via SQLAlchemy
                 └─ async with _trade_ids_lock:
                      _open_trade_ids[symbol] = (row_id, entry_price, entry_time)

_exit_monitor_loop() [every 10 min, concurrent]
  └─ snapshot = dict(_open_trade_ids)   ← locked read
  └─ get_orders(CLOSED, last 7 days, limit=200)
       └─ for each filled sell order matching snapshot:
            └─ async with _trade_ids_lock: pop(sym)
            └─ _update_trade_exit(row_id, exit_price, exit_reason, pnl_pct)  ← SQLAlchemy
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

## Architecture Notes

### Async safety
- All `trading_client.*` SDK calls are wrapped in `asyncio.to_thread()` — no blocking calls on the event loop
- `_open_trade_ids` is protected by `self._trade_ids_lock = asyncio.Lock()` — all reads and writes acquire the lock
- `_update_loss_cache()` is `async def` — all callers use `await`
- Bull/bear debate runs both Claude calls in parallel via `asyncio.gather()`

### Database (SQLAlchemy)
- `bot.py` uses `create_engine(url, pool_pre_ping=True)` stored as `self._db_engine`
- `dashboard.py` uses `@st.cache_resource` engine via `_get_engine()`
- Both use `engine.begin()` for writes (auto-commit on context exit) and `text()` with named `:params`
- Raw psycopg2 was fully removed from both files

### Signal cooldown key
- Key is `f"{symbol}-{strategy.name}"` (NOT including signal direction) — prevents buy/sell having separate cooldown windows on the same symbol+strategy

### FRED macro conviction multiplier
- `get_conviction_multiplier()` in `strategies/fred_strategy.py` reads the module-level `MACRO_SNAPSHOT["vix"]`
- Returns 0.7 when VIX > 30, else 1.0; returns 1.0 safely if FRED data hasn't loaded yet (startup window before first fetch)
- Applied in `news_loop` and `sec_edgar_loop` only, after `sig["auto_trade"]` is True, before symbol cooldown check
- Does NOT modify the strength value shown in Slack — only gates the auto-trade execution path
- At 0.7× a signal needs raw strength ~18.6 to cross the threshold of 13 — effectively suppresses all auto-trades when VIX > 30

---

## Known Issues and Tech Debt

1. **`bot.py` is ~1100 lines** — monolithic. The DB methods, regime check, fundamentals gate, and debate could be extracted into `trading/risk.py` or similar, but this is low priority until the bot needs significant new features.

2. **V, JPM, PG have no validated edge** — they remain in `SWING_SYMBOLS` to accumulate live `signal_outcomes` data. Consider removing after 6 months if they don't signal (or are consistently blocked by fundamentals gate).

3. **Alpaca 15-minute delay** — free IEX feed has 15-min delay for stocks. Mitigated by Finnhub real-time price overlay in `get_historical_bars()`. Crypto is unaffected (real-time WebSocket).

4. **`_seen_accessions` in `SECEdgarStrategy` is in-memory** — on Railway restart, it re-processes the last 40 filings. The `SEC_EDGAR_COOLDOWN_HOURS` (4h) per ticker prevents duplicate signals, but ~80 duplicate HTTP requests happen on each cold start. Acceptable for 30-min polling.

5. **Truth Social loop is dead code** — `truth_social_loop` exists in `bot.py` and is included in `asyncio.gather()`, but `TRUTH_SOCIAL_ENABLED=False` causes it to return immediately. No performance impact, but the loop registration and import are still present. Will be cleaned up when Quiver API integration is ready.

6. **No structured logging** — all output is `print()`. No log levels, no rotation, no file output. Hard to filter signal vs. noise in Railway logs.

7. **Exit monitor symbol-only matching** — `_exit_monitor_loop` matches closed sell orders to `_open_trade_ids` by symbol. If two sell orders for the same symbol fill in one 10-min window (shouldn't happen in practice), only the first match is logged.

8. **`backtester.py`** — early prototype. Do not modify without understanding it first.

9. **Congressional trading loop is disabled** — `CONGRESSIONAL_ENABLED=False` because the House Stock Watcher S3 bucket (`house-stock-watcher-data.s3-us-east-2.amazonaws.com`) went private in 2024 and no other free unauthenticated JSON source exists. The full strategy code is in place. To re-enable: add `QUIVER_API_KEY` to Railway env vars (Quiver Quantitative, $30/mo at quiverquant.com), set `CONGRESSIONAL_ENABLED=True` in `config.py`, and restore the `Authorization: Token` header in `_fetch_trades()`. The `_COMMITTEE_MEMBERS` set reflects the 119th Congress and will need updating every two years.

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
flask              # /health endpoint (port 8502)
streamlit          # Dashboard (port 8501)
plotly             # Charts in dashboard
matplotlib
seaborn
psycopg2-binary    # PostgreSQL driver (still needed by SQLAlchemy for pg:// URLs)
sqlalchemy         # ORM/connection layer for bot.py and dashboard.py
pyarrow            # Parquet cache for discovery engine
```

---

## Running Locally

```bash
# Bot only
python bot.py

# Dashboard only
streamlit run dashboard.py

# Both (with restart supervision)
python run_all.py

# Discovery Engine (full 243-combo run, ~7 min)
python -m discovery.discovery_engine
```

The bot requires all Railway env vars in a local `.env` file. `config.py` calls `load_dotenv()` at module load time — this is intentional and critical. Do not move it.
