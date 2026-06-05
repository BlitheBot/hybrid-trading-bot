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

A Python asyncio trading bot running 24/7 on Railway. It runs 19 concurrent loops covering crypto
scalping (WebSocket), equity swing trading (daily), Bollinger mean reversion, news NLP (Benzinga),
Reddit momentum (WSB/r/stocks), X/Twitter crypto sentiment (Grok), insider trade signals (SEC
EDGAR Form 4), congressional trade disclosures (Quiver), macro indicators (FRED), short interest
(FINRA), and housekeeping (trailing stops, DB exit logging, Slack health reports, overnight
discovery). All trade decisions go to Slack. High-conviction signals auto-trade via Alpaca. All
completed trades are logged to PostgreSQL via SQLAlchemy for ML training data. An overnight
discovery engine (walk-forward grid search + genetic programming) continuously searches for new
validated strategy parameters.

---

## File Inventory

### Core Runtime

| File | Purpose |
|---|---|
| `bot.py` | Main `TradingBot` class — 19 async loops, all trade execution, full gate chain, SQLAlchemy DB logging, market regime check, bull/bear debate, fundamentals gate, portfolio heat cap, correlation guard, short interest signal, circuit breakers |
| `config.py` | All config constants as class attributes; reads `.env` via `load_dotenv()` before class definition (critical — class attributes are evaluated at import time) |
| `llm_client.py` | Unified LLM abstraction: `call_llm()`, `call_llm_with_model()`, `LLMError`, `MODEL_FLASH`; routes to Anthropic, OpenRouter, or OpenAI-compatible provider based on `LLM_PROVIDER` env var |
| `notifications.py` | Slack webhook functions for each channel: alerts, decisions, health, performance |
| `notion_journal.py` | Optional Notion trade journal integration; no-ops if `NOTION_API_KEY` is unset |
| `utils.py` | `get_historical_bars()` (Alpaca + Finnhub real-time overlay), `get_spy_data()`, `get_finnhub_price()` |
| `dashboard.py` | 5-tab Streamlit dashboard — Account, Positions, Trade Log (`signal_outcomes` via SQLAlchemy), Discovery (`strategy_results`), Analytics (P&L chart + win rate) |
| `requirements.txt` | All dependencies (see Dependencies section) |

### Strategies (Active)

| File | Used By | Purpose |
|---|---|---|
| `strategies/base_strategy.py` | All strategies | Abstract base: `generate_signals()`, `execute_trade()`, `calculate_safe_quantity()`, `is_already_in_position()` |
| `strategies/swing_strategy.py` | `swing_loop` | EMA crossover + MACD + RSI swing signals with configurable per-symbol params; R/R ratio gate; `_kelly` sizer attached at startup |
| `strategies/bollinger_mean_reversion_strategy.py` | `swing_loop` | BB lower-break + RSI oversold entry; half-life gate (OLS Ornstein-Uhlenbeck); middle-band cross or RSI exit |
| `strategies/smb_strategy.py` | `scalp_loop` | Crypto scalp; EMA9 vs VWAP crossover, ATR-based stops, 3:1 R/R; BTC/USD + ETH/USD. Null check uses `iloc[-2:]` not full DataFrame (early rows have NaN from rolling indicators) |
| `strategies/news_strategy.py` | `news_loop` | Benzinga news via Alpaca News API; LLM NLP scoring via `llm_client` with keyword fallback; dynamic sleep; 429 retry (10s/20s/30s) per batch |
| `strategies/truth_social_strategy.py` | `truth_social_loop` | Disabled — `TRUTH_SOCIAL_ENABLED=False`; `scan_once()` returns `[]` immediately |
| `strategies/sec_edgar_strategy.py` | `sec_edgar_loop` | SEC EDGAR Form 4 insider trades; ElementTree XML parsing; strength-tiered scoring; exponential 429 backoff (30s/60s/120s) |
| `strategies/fred_strategy.py` | `fred_loop` | FRED macro indicators via free public CSV endpoints; module-level `MACRO_SNAPSHOT` dict + `get_conviction_multiplier()` readable by any loop; no API key required |
| `strategies/congressional_trading_strategy.py` | `congressional_trading_loop` | Quiver Quantitative congressional trades; scores buys by amount ($50k/$250k tiers) + committee membership (1.3×) + recency ≤7 days (1.2×); informational sell signals at half strength; 4-hour per-ticker cooldown; self-disables on 401/403 |
| `strategies/reddit_strategy.py` | `reddit_loop` | Scans r/wallstreetbets + r/stocks hot posts; ticker extraction against SP500 set; 4-hour dedup; alert-only |
| `strategies/grok_strategy.py` | `grok_loop` | xAI grok-3-mini with live X/Twitter search; scores BTC/ETH sentiment 0–10; alert-only; requires `GROK_API_KEY` |
| `strategies/webull_strategy.py` | `webull_loop` | Retail crowding signal stub; currently disabled — endpoint returns 417 |
| `strategies/kalman_signal.py` | `swing_loop`, discovery | 1D scalar Kalman filter; optional wavelet (db4/PyWavelets) pre-denoising; outputs trend, slope, noise_ratio, signal ∈ {−1,0,+1} |
| `strategies/hurst_signal.py` | `swing_loop`, discovery | Rolling Hurst exponent via R/S rescaled-range analysis; regime: trending (H>0.6) / random (0.4–0.6) / mean-reverting (H<0.4) |
| `strategies/vwap_signal.py` | `swing_loop`, discovery | Anchored VWAP (rolling/weekly/monthly); distance_pct from VWAP + volume_ratio gate; signal ∈ {−1,0,+1} |
| `strategies/halflife_signal.py` | `BollingerMeanReversionStrategy` | OLS on Δp ~ α + β·p_lag; HL = −ln2/ln(1+β); gates BB mean reversion entries on [1, 30] bar range |
| `strategies/kelly_sizer.py` | All swing strategies | Half-Kelly position sizing (f* = (p·b−q)/b, capped 10%); pulls win/loss history from `signal_outcomes` via PostgreSQL; 90-day lookback; falls back to 2% default below 20-trade threshold |
| `strategies/correlation_guard.py` | `_process_symbol` gate | Pearson ρ on 60-day closing prices (30-min cache); blocks trade if ρ > 0.75 with any open position; max 2 positions per GICS sector |
| `strategies/short_interest_signal.py` | `_process_symbol` gate | FINRA CNMSshvol daily files; ShortVolume/TotalVolume; ratio ≥ 65% → veto swing buy; ratio ≥ 65% + price uptick → squeeze boost note |

### Strategies (Legacy — in repo, not wired into bot)

`strategies/sma_crossover.py`, `strategies/hybrid_strategy.py`, `strategies/mean_reversion.py`, `strategies/rsi_strategy.py` — early prototypes, not used in production.

### Discovery Engine

| File | Purpose |
|---|---|
| `discovery/__init__.py` | Empty — marks directory as Python package |
| `discovery/discovery_engine.py` | v1 walk-forward backtester; 243-combo grid search over EMA/RSI params; scipy t-test validation; writes `strategy_results` + `signal_outcomes` to PostgreSQL; parquet cache in `discovery/data/` (24h TTL, gitignored) |
| `discovery/discovery_engine_v2.py` | v2 extensible backtester; auto-discovers `DiscoveryStrategy` subclasses; top-100 S&P 500 by 30-day avg volume; `multiprocessing.Pool`; regime Sharpe tagging; correlation filter ρ>0.8; incremental (skips approved combos); writes `discovery_results` JSONB |
| `discovery/regime_adapter.py` | Reads approved `discovery_results` from PostgreSQL; returns best strategy type + params for given symbol + SPY regime (bull/bear/high_vol); drop-in override for live SwingStrategy; falls back to hardcoded defaults |
| `discovery/symbol_universe.py` | `get_top_n()` — fetches top-N S&P 500 symbols by 30-day average volume for the discovery engine |
| `discovery/genetic_engine.py` | Genetic programming engine; 50-pop × 20-gen; crossover + mutation on `ExpressionNode` trees; walk-forward IC fitness; graduates mean_IC > 0.05 |
| `discovery/expression_tree.py` | `ExpressionNode` AST representation for evolved indicator expressions |
| `discovery/fitness_evaluator.py` | `FitnessEvaluator.walk_forward_ic()` — evaluates an expression tree's IC across 4-month walk-forward folds |
| `discovery/indicator_library.py` | `IndicatorLibrary` — primitive set: price transforms, rolling stats, crossover operators, logical gates |
| `discovery/primitives.py` | Low-level numeric primitives used by `IndicatorLibrary` |
| `discovery/discovery_scheduler.py` | Schedules overnight discovery runs |

### Data

| File | Purpose |
|---|---|
| `data/sp500_tickers.py` | `SP500_TICKERS` list used by news, EDGAR, and Reddit strategies to filter signals |

---

## 19 Concurrent Loops (`asyncio.gather` in `start_dual_engine`)

| # | Method | Interval | Status | What It Does |
|---|---|---|---|---|
| 1 | `scalp_loop` | WebSocket (continuous) | **Disabled** (`SCALP_ENABLED=False`) | Crypto scalp on BTC/USD + ETH/USD via CryptoDataStream; EMA9/VWAP crossover; ATR stops; RSI momentum arbitrage |
| 2 | `swing_loop` | Daily 10:30 AM EST | Active | Evaluates all 6 SWING_SYMBOLS with per-symbol SwingStrategy + BollingerMeanReversionStrategy instances; full gate chain; `_swing_pre_trade_hook` |
| 3 | `news_loop` | 60s–15min (dynamic) | Active | Scans all S&P 500 tickers for Benzinga headlines; LLM NLP scores each; alerts to Slack, auto-trades if strength ≥ threshold; sets `last_news_scan_utc` in `_health_state` |
| 4 | `truth_social_loop` | — | **Disabled** (`TRUTH_SOCIAL_ENABLED=False`) | Returns immediately; no-op |
| 5 | `sec_edgar_loop` | 30 min | Active | Polls EDGAR Form 4 RSS; parses XML for open-market transactions; alerts on buys ≥ $100k and sells ≥ $500k; auto-trades on $1M+ buys; sets `last_edgar_scan_utc` |
| 6 | `fred_loop` | Daily 7 PM EST + startup fetch | Active | Fetches 5 FRED macro indicators (FF rate, VIX, 10Y, unemployment, CPI YoY); updates `MACRO_SNAPSHOT`; VIX > 30 → 0.7× conviction multiplier; VIX > 40 → critical alert; Sunday 7 PM → weekly summary |
| 7 | `congressional_trading_loop` | — | **Disabled** (`CONGRESSIONAL_ENABLED=False`) | Requires `QUIVER_API_KEY`; full strategy code in place; self-disables on 401/403 |
| 8 | `health_report_loop` | Daily 9 AM EST | Active | Sends uptime, equity, buying power, daily P&L to #trading-health |
| 9 | `performance_report_loop` | Weekly Sun 6 PM EST | Active | Sends weekly equity + active positions to #trading-performance |
| 10 | `trailing_stop_monitor_loop` | Every 60s | Active | Upgrades static stop-loss orders to trailing stops once unrealized gain ≥ 3% (trails at 1.5%) |
| 11 | `_exit_monitor_loop` | Every 10 min | Active | Queries Alpaca for closed sell orders (7-day lookback, limit 200); backfills `signal_outcomes` with exit price, P&L%, exit reason. Protected by `_trade_ids_lock` |
| 12 | `market_open_notification_loop` | Daily 9:30 AM EST (Mon–Fri) | Active | Morning briefing to #trading-alerts: equity, market regime, swing watchlist |
| 13 | `discovery_loop` | Overnight | Active | Runs v2 walk-forward backtester; writes approved results to PostgreSQL `discovery_results` |
| 14 | `reddit_loop` | 30 min | Active | Scans r/wallstreetbets + r/stocks; S&P 500 ticker extraction; alert-only; 4-hour dedup |
| 15 | `symbol_universe_loop` | Periodic | Active | Refreshes top-100 S&P 500 by 30-day avg volume for discovery engine |
| 16 | `market_close_digest_loop` | Daily close | Active | End-of-day performance digest → Slack |
| 17 | `grok_loop` | 30 min | Active | xAI grok-3-mini + live X/Twitter search; BTC/ETH sentiment 0–10 scale; alert-only |
| 18 | `webull_loop` | — | **Disabled** | Webull retail-crowding endpoint returns 417; disabled until working source found |
| 19 | `indicator_discovery_loop` | Overnight | Active | Genetic programming; evolves novel indicator expressions; graduates IC > 0.05 |

---

## Gate Chain

All 15 gates run in `_process_symbol()` on every buy signal, in this order. The first failure
discards the trade. Blocking DB/HTTP calls are wrapped in `asyncio.to_thread`.

```
Signal generated (buy)
        │
        ├─ ① trading_halted_for_day  ────────────────────────────────► SKIP
        ├─ ② _bot_paused (/pause slash command)  ───────────────────► SKIP
        ├─ ③ symbol+strategy cooldown (120 min post stop-loss)  ────► SKIP
        ├─ ④ already in position (Alpaca position check)  ─────────► SKIP
        ├─ ⑤ portfolio heat cap ≥ 15%  ─────────────────────────────► SKIP (critical alert)
        │     ∑(|market_value| × SL%) / equity ≥ PORTFOLIO_HEAT_CAP
        ├─ ⑥ correlation guard  ─────────────────────────────────────► SKIP if ρ > 0.75
        │     Pearson ρ on 60-day closes vs each open position;
        │     also blocks if ≥ 2 positions in same GICS sector
        ├─ ⑦ FINRA short interest veto  ─────────────────────────────► SKIP if ratio ≥ 65%
        │       └── ratio ≥ 65% + price uptick  ──────────────────────► PROCEED + si_boost note
        ├─ ⑧ fundamentals gate (Finnhub)  ──────────────────────────► BLOCK if P/E < 0
        │     Negative P/E → block; EPS decline > 20% YoY → block    BLOCK if EPS −20%
        ├─ ⑨ earnings filter  ───────────────────────────────────────► PROCEED at 25% size
        │     earnings within 48h → earnings_override_multiplier=0.25
        ├─ ⑩ bull/bear debate (3 LLM calls via llm_client)  ────────► SKIP or PROCEED
        │     parallel bull+bear prompts with web search,             (or 50% size on
        │     synthesis call → JSON verdict: proceed/skip/reduce_size  reduce_size)
        ├─ ⑪ strategy circuit breaker  ─────────────────────────────► SKIP if tripped
        │     rolling net pnl_pct over window_days ≤ −threshold_pct;
        │     auto-resets when drawdown recovers; persisted in strategy_circuit_breakers
        ├─ ⑫ VIX extreme gate (VIX > VIX_EXTREME_THRESHOLD=40)  ────► BLOCK + critical alert
        ├─ ⑬ VIX spike gate (VIX > VIX_SPIKE_THRESHOLD=35)  ───────► PROCEED at 25% size
        ├─ ⑭ ADX regime filter (SPY ADX(14), 4-hour cache)  ────────► logs warning if choppy
        │     ADX > 25 → trending; ADX < 20 → choppy (caution logged)
        └─ ⑮ candlestick confirmation  ──────────────────────────────► −20% conviction if absent
              (CANDLESTICK_CONFIRMATION_ENABLED)

        └─ PROCEED → KellySizer → position sizing multipliers → bracket order → DB log
```

**Position sizing multipliers** (all multiplicative, applied after gate chain):
`base_risk × risk_multiplier × vix_risk_mult × earnings_override × bear_market_factor × debate_size_mult × performance_brain_mult`
Floor: `POSITION_SIZE_FLOOR = 0.1` (10% of base risk percent).

---

## Database Schema (PostgreSQL)

Four tables, created automatically on startup.

### `signal_outcomes` — Live trade log (primary ML training data)

```sql
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(10),
    signal_type   VARCHAR(20),
    entry_time    TIMESTAMP,
    exit_time     TIMESTAMP,
    entry_price   FLOAT,
    exit_price    FLOAT,
    pnl_pct       FLOAT,
    hold_bars     INTEGER,
    ema_short     INTEGER,
    ema_long      INTEGER,
    rsi_at_entry  FLOAT,
    macd_at_entry FLOAT,
    market_regime VARCHAR(20),
    exit_reason   VARCHAR(30),
    discovered_at TIMESTAMP DEFAULT NOW()
);
```

### `strategy_results` — Discovery Engine v1 walk-forward results

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
    degradation       FLOAT,
    p_value           FLOAT,
    total_test_trades INTEGER,
    status            VARCHAR(20),
    discovered_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE (symbol, ema_short, ema_long, rsi_period, rsi_entry_low, rsi_entry_high)
);
```

### `strategy_circuit_breakers` — Per-strategy drawdown pauses

```sql
CREATE TABLE IF NOT EXISTS strategy_circuit_breakers (
    strategy_name  TEXT PRIMARY KEY,
    tripped_at     TIMESTAMP DEFAULT NOW(),
    reason         TEXT
);
```

### `discovery_results` — Discovery Engine v2 multi-strategy results

```sql
CREATE TABLE IF NOT EXISTS discovery_results (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(10),
    strategy_type   TEXT,
    parameters      JSONB,
    test_sharpe     FLOAT,
    bull_sharpe     FLOAT,
    bear_sharpe     FLOAT,
    high_vol_sharpe FLOAT,
    status          TEXT DEFAULT 'pending_approval',
    discovered_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE (symbol, strategy_type, parameters)
);
```

---

## Statistical Signal Layer

| Module | Method | Output |
|---|---|---|
| `KalmanTrendSignal` | 1D scalar Kalman filter; optional wavelet (db4) pre-denoising | trend, slope, noise_ratio, signal ∈ {−1,0,+1} |
| `HurstSignal` | R/S rescaled-range analysis; rolling 100-bar window | H exponent; regime: trending/random_walk/mean_reverting |
| `AnchoredVWAPSignal` | Rolling N-bar, weekly, or monthly-anchored VWAP; distance_pct + volume_ratio gate | distance_pct, volume_ratio, signal ∈ {−1,0,+1} |
| `HalfLifeSignal` | OLS on Δp ~ α + β·p_lag; HL = −ln2/ln(1+β); requires β ∈ (−1, 0) | halflife (bars), is_mean_reverting, OU theta |
| `KellySizer` | f* = (p·b−q)/b; half-Kelly with 10% hard cap; 90-day DB lookback; ≥20 trade min | dollars, shares, kelly_f, half_kelly_f, win_rate, note |
| `CorrelationGuard` | Pearson ρ on 60-day closing prices; 30-min in-memory cache; GICS sector map | allowed (bool), reason, correlation_map, avg_correlation |
| `ShortInterestSignal` | FINRA CNMSshvol: ShortVolume/TotalVolume; 12-hour cache | short_interest_pct, squeeze_score, signal ∈ {−1,0,+1} |

---

## LLM Layer

All LLM calls route through `llm_client.py` — `call_llm()` and `call_llm_with_model()`.

| Use Case | Model | Provider |
|---|---|---|
| Bull/Bear debate — bull + bear calls (parallel) | `MODEL_FLASH` (DeepSeek Flash) + live web search | OpenRouter |
| Bull/Bear debate — synthesis verdict | `MODEL_FLASH` | OpenRouter |
| News NLP scoring | `MODEL_FLASH` or keyword fallback | OpenRouter |
| Discovery strategy review | Claude (`ANTHROPIC_API_KEY`) | Anthropic |
| Grok X/Twitter sentiment | grok-3-mini + live X search | xAI |
| Fallback if OpenRouter unavailable | claude-sonnet / kimi-v1-8k | Anthropic / Moonshot |

**Debate flow:** 3 LLM calls per swing buy signal. Bull and Bear run in parallel via `asyncio.gather`. Synthesis returns `{"verdict":"proceed"|"skip"|"reduce_size","conviction":0.0-1.0,"reasoning":"..."}`. `reduce_size` sets `debate_size_multiplier = 0.5`.

**Daily call limit:** `CLAUDE_DAILY_CALL_LIMIT = 100` — once exceeded, news NLP falls back to keyword scoring.

---

## Per-Symbol Swing Strategy Parameters

| Symbol | ema_short | ema_long | rsi_period | rsi_entry_low | rsi_entry_high | Validated |
|---|---|---|---|---|---|---|
| COST | 20 | 100 | 10 | 35 | 65 | 125/243 |
| BRK.B | 50 | 200 | 21 | 40 | 65 | 24/243 |
| SPY | 50 | 200 | 14 | 40 | 60 | 9/243 |
| V | 50 | 200 | 14 | 40 | 60 | 0/243 |
| JPM | 50 | 200 | 14 | 40 | 60 | 0/243 |
| PG | 50 | 200 | 14 | 40 | 60 | 0/243 |

---

## Critical Environment Variables

**Required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `SLACK_ALERTS_WEBHOOK`, `SLACK_DECISIONS_WEBHOOK`, `SLACK_PERFORMANCE_WEBHOOK`, `SLACK_HEALTH_WEBHOOK`, `DATABASE_URL`

**Recommended:** `OPENROUTER_API_KEY`, `SLACK_SIGNING_SECRET`

**Optional:** `GROK_API_KEY`, `QUIVER_API_KEY`, `SENTRY_DSN`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `PAGERDUTY_ROUTING_KEY`, `HEALTH_PORT` (default 8502), `LLM_PROVIDER`, `OPENAI_COMPATIBLE_API_KEY`

---

## Port Architecture

| Port | Service |
|---|---|
| 8501 | Streamlit dashboard |
| 8502 | Flask — `/health` JSON · `/metrics` Prometheus · `/slack/commands` POST |

---

## Slack Slash Commands

| Command | Effect |
|---|---|
| `/status` | Equity, positions, daily P&L, VIX, regime, pause state |
| `/buy SYMBOL SHARES` | Market buy; blocked if paused or daily loss limit hit |
| `/sell SYMBOL` | Close full open position at market |
| `/pause` | Sets `_bot_paused=True` — all `_process_symbol` paths skip |
| `/resume` | Clears `_bot_paused` |
| `/help` | Lists all commands |

---

## Architecture Notes

- **Signal module imports:** `KalmanTrendSignal`, `HurstSignal`, `KellySizer` imported inside `swing_strategy.py`. `AnchoredVWAPSignal` inside `smb_strategy.py`. `CorrelationGuard` and `ShortInterestSignal` imported directly in `bot.py`.
- **Async safety:** All `trading_client.*` SDK calls wrapped in `asyncio.to_thread()`. `_open_trade_ids` protected by `asyncio.Lock()`. Bull/bear debate runs parallel via `asyncio.gather()`.
- **Database:** `bot.py` uses `create_engine(url, pool_pre_ping=True)`. Both bot and dashboard use `engine.begin()` for writes and `text()` with named `:params`.
- **Signal cooldown key:** `f"{symbol}-{strategy.name}"` — buy/sell share one cooldown window per symbol+strategy.
- **FRED conviction multiplier:** Returns 0.7 when VIX > 30, else 1.0. Applied in `news_loop` and `sec_edgar_loop` only. Does NOT modify the strength value shown in Slack.
- **Kelly sizer:** Falls back to 2% default below 20-trade threshold. Results cached 60 minutes. `update_capital()` called from `_check_account_status()` to keep `base_capital` current.
- **Portfolio heat cap:** `∑(|market_value| × stop_loss_percent) / equity`. Fires critical Slack alert if triggered.
- **Confluence tracking:** `_record_daily_signal(symbol, source)` — when ≥ 2 distinct sources fire same day, confluence alert fires to #trading-alerts. Resets daily at midnight EST.
- **Market regime:** SPY vs EMA200 (15-min cache) → `'bull'`/`'bear'`/`'neutral'`. SPY ADX(14) (4-hour cache) → `'trending'`/`'choppy'`/`'neutral'`.

---

## Known Issues and Tech Debt

1. **`bot.py` is ~2764 lines** — monolithic. Low priority to split until next major phase.
2. **V, JPM, PG have no validated edge** — kept in `SWING_SYMBOLS` to accumulate `signal_outcomes` data. Review after 6 months.
3. **Alpaca 15-minute delay** — free IEX feed; mitigated by Finnhub real-time overlay in `get_historical_bars()`.
4. **`_seen_accessions` in `SECEdgarStrategy` is in-memory** — re-processes last 40 filings on restart. `SEC_EDGAR_COOLDOWN_HOURS` (4h) prevents duplicate signals.
5. **Truth Social loop is dead code** — `TRUTH_SOCIAL_ENABLED=False`; no-op included in `asyncio.gather()`.
6. **No structured logging** — all output is `print()`. No log levels, rotation, or file output.
7. **Exit monitor symbol-only matching** — `_exit_monitor_loop` matches closed sell orders by symbol only.
8. **Congressional trading loop is disabled** — `CONGRESSIONAL_ENABLED=False`. Re-enable by adding `QUIVER_API_KEY` to Railway and setting `CONGRESSIONAL_ENABLED=True`.
9. **Webull loop is disabled** — endpoint returns HTTP 417.

---

## Dependencies

```
alpaca-py, pandas, pandas-ta, numpy, scipy, pytz, requests, anthropic, openai,
python-dotenv, flask, streamlit, plotly, matplotlib, seaborn, psycopg2-binary,
sqlalchemy, pyarrow, sentry-sdk, notion-client, PyWavelets
```

---

## Running Locally

```bash
python bot.py                          # bot only
streamlit run dashboard.py             # dashboard only
python run_all.py                      # both with restart supervision
python -m discovery.discovery_engine   # v1 grid search (~7 min per symbol)
python -m discovery.discovery_engine_v2  # v2 multi-strategy
```

The bot requires all Railway env vars in a local `.env` file. `config.py` calls `load_dotenv()` at module load time — this is intentional and critical.
