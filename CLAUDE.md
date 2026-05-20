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
        │     (only blocks if EARNINGS_FILTER_ENABLED=False and earnings today)
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

Four tables, created automatically on startup by `_ensure_signal_outcomes_table()` in `bot.py`
and by `_ensure_tables()` in `discovery_engine_v2.py`.

**Discovery Engine v1 results** were written to Railway PostgreSQL on 2026-05-10: 1215 rows across
5 symbols (JPM 0, SPY 9, COST 125, BRK.B 24, PG 0 validated of 243 combos each).

### `signal_outcomes` — Live trade log (primary ML training data)

```sql
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(10),
    signal_type   VARCHAR(20),      -- 'swing_long', 'swing_bb', 'discovery_ema_trend', 'scalp_long'
    entry_time    TIMESTAMP,
    exit_time     TIMESTAMP,        -- NULL until position closes
    entry_price   FLOAT,
    exit_price    FLOAT,            -- NULL until position closes
    pnl_pct       FLOAT,            -- NULL until position closes
    hold_bars     INTEGER,          -- days held (seconds / 86400)
    ema_short     INTEGER,
    ema_long      INTEGER,
    rsi_at_entry  FLOAT,
    macd_at_entry FLOAT,
    market_regime VARCHAR(20),      -- 'bull', 'bear', 'neutral' (SPY vs EMA200)
    exit_reason   VARCHAR(30),      -- 'stop', 'target', 'manual'
    discovered_at TIMESTAMP DEFAULT NOW()
);
```

Entry is logged after `execute_trade()` succeeds. Exit is backfilled by `_exit_monitor_loop`
every 10 minutes (7-day lookback window).

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
    degradation       FLOAT,        -- train_sharpe - test_sharpe; lower is better
    p_value           FLOAT,        -- scipy t-test on walk-forward test_cagr values
    total_test_trades INTEGER,
    status            VARCHAR(20),  -- 'validated' or 'rejected'
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

Auto-reset: when the rolling net pnl_pct recovers above the threshold, the row is deleted and
the strategy resumes. Checked on every buy signal via `_check_strategy_circuit_breaker()`.

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

Approval managed via the dashboard Discovery tab. `RegimeAdapter` reads `approved` rows.

---

## Statistical Signal Layer

These modules produce features consumed by strategies and gate checks — they do not trade directly.

| Module | Method | Output |
|---|---|---|
| `KalmanTrendSignal` | 1D scalar Kalman filter; optional wavelet (db4) pre-denoising via PyWavelets | trend, slope, noise_ratio, signal ∈ {−1,0,+1} |
| `HurstSignal` | R/S rescaled-range analysis; rolling 100-bar window; OLS log(RS) ~ log(lag) | H exponent; regime: trending/random_walk/mean_reverting |
| `AnchoredVWAPSignal` | Rolling N-bar, weekly, or monthly-anchored VWAP; distance_pct + volume_ratio gate | distance_pct, volume_ratio, signal ∈ {−1,0,+1} |
| `HalfLifeSignal` | OLS on Δp ~ α + β·p_lag; HL = −ln2/ln(1+β); requires β ∈ (−1, 0) | halflife (bars), is_mean_reverting, OU theta, suggested_holding_period, confidence |
| `KellySizer` | f* = (p·b−q)/b; half-Kelly with 10% hard cap; 90-day DB lookback; ≥20 trade min | dollars, shares, kelly_f, half_kelly_f, win_rate, payoff_ratio, sample_size, note |
| `CorrelationGuard` | Pearson ρ on 60-day closing prices; 30-min in-memory cache; GICS sector map | allowed (bool), reason, correlation_map, avg_correlation |
| `ShortInterestSignal` | FINRA CNMSshvol: ShortVolume/TotalVolume; 12-hour cache; last 8 calendar days tried | short_interest_pct, squeeze_score, signal ∈ {−1,0,+1}, note |

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

**Debate flow:** 3 LLM calls per swing buy signal. Bull and Bear prompts run in parallel via
`asyncio.gather`, each with `plugins=[{"id":"web","max_results":1}]` for live news search.
Synthesis call returns `{"verdict":"proceed"|"skip"|"reduce_size","conviction":0.0-1.0,"reasoning":"..."}`.
`reduce_size` sets `strategy.debate_size_multiplier = 0.5`. Citation URLs surface in #trading-decisions.

**Daily call limit:** `CLAUDE_DAILY_CALL_LIMIT = 100` — once exceeded, news NLP falls back to
keyword scoring automatically.

---

## Per-Symbol Swing Strategy Parameters

Determined by running the Discovery Engine v1 (243-combo grid, 24-month train / 3-month test
walk-forward, scipy t-test p < 0.05). Run with `python -m discovery.discovery_engine`.

| Symbol | ema_short | ema_long | rsi_period | rsi_entry_low | rsi_entry_high | Validated | Notes |
|---|---|---|---|---|---|---|---|
| COST | 20 | 100 | 10 | 35 | 65 | 125/243 | Best test Sharpe 0.87; short EMA crossover dominates |
| BRK.B | 50 (default) | 200 (default) | 21 | 40 | 65 | 24/243 | RSI21 + wide upper band required |
| SPY | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 9/243 | Defaults already optimal |
| V | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 0/243 | No validated edge; monitoring only |
| JPM | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 0/243 | No validated edge; flagged in swing_loop logs |
| PG | 50 (default) | 200 (default) | 14 (default) | 40 (default) | 60 (default) | 0/243 | No validated edge; flagged in swing_loop logs |

AMZN and V were run as replacement candidates for JPM/PG — both returned 0 validated combos,
so JPM and PG were kept.

---

## Full Config Reference (`config.py`)

```python
# Alpaca
ALPACA_API_KEY          # from env — paper keys start 'PK', live start 'AK'
ALPACA_SECRET_KEY       # from env
ALPACA_BASE_URL         # from env — paper: https://paper-api.alpaca.markets
PAPER_TRADING = True    # set False for live trading — DO NOT CHANGE without review
FINNHUB_API_KEY         # from env — real-time overlay + fundamentals

# Crypto Scalp
SCALP_ENABLED = False   # set True to re-enable WebSocket crypto scalping
SCALP_SYMBOLS = ["BTC/USD", "ETH/USD"]
CRYPTO_SCALP_STOP_LOSS_PERCENT = 4.0

# Slack (from env)
SLACK_ALERTS_WEBHOOK      # #trading-alerts — errors, critical, trade fills
SLACK_DECISIONS_WEBHOOK   # #trading-decisions — all signals, debate results, skips
SLACK_PERFORMANCE_WEBHOOK # #trading-performance — weekly reports
SLACK_HEALTH_WEBHOOK      # #trading-health — daily health
SLACK_SIGNING_SECRET      # from env — HMAC-SHA256 verification for slash commands

# LLM provider routing
LLM_PROVIDER = "anthropic"              # or 'kimi' / 'openai_compatible'
ANTHROPIC_API_KEY                       # from env — Anthropic Claude (discovery debate)
OPENROUTER_API_KEY                      # from env — DeepSeek Flash for bull/bear debate + news NLP
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_COMPATIBLE_API_KEY               # from env — Kimi / Moonshot fallback
OPENAI_COMPATIBLE_BASE_URL = "https://api.moonshot.cn/v1"
OPENAI_COMPATIBLE_MODEL = "moonshot-v1-8k"
CLAUDE_DAILY_CALL_LIMIT = 100           # fall back to keyword scoring once exceeded
NEWS_CLAUDE_SCORING_ENABLED = True

# Risk Management
EQUITY_RISK_PER_TRADE_PERCENT = 2.0
STOP_LOSS_PERCENT = 2.0
TAKE_PROFIT_PERCENT = 6.0
MAX_BUYING_POWER_UTILIZATION_PERCENT = 10.0
SWING_MIN_RR_RATIO = 2.0
SWING_EQUITY_RISK_PERCENT = 1.0
SWING_SYMBOLS = ["JPM", "SPY", "COST", "BRK.B", "PG", "V"]

# Graduated Daily Loss Limits
DAILY_LOSS_REDUCTION_1_PERCENT = 2.0   # risk_multiplier = 0.75
DAILY_LOSS_REDUCTION_2_PERCENT = 3.5   # risk_multiplier = 0.50
MAX_DAILY_LOSS_PERCENT = 5.0           # all new trading halted for day

# VIX / volatility gates
VIX_SPIKE_THRESHOLD = 35               # size × 0.25
VIX_EXTREME_THRESHOLD = 40             # block trade entirely + critical alert

# Bear market / regime
BEAR_MARKET_SIZE_REDUCTION = 0.5       # × position size when SPY < EMA200

# Earnings filter
EARNINGS_FILTER_ENABLED = True         # reduce size to 25% when earnings within 48h

# Candlestick confirmation
CANDLESTICK_CONFIRMATION_ENABLED = True  # −20% conviction when no bullish pattern last 3 bars

# Portfolio heat cap
PORTFOLIO_HEAT_CAP = 0.15              # max aggregate open-position risk as % of equity

# Performance Brain
PERFORMANCE_SCALING_ENABLED = True     # adjust position size based on last 20-trade win rate
POSITION_SIZE_FLOOR = 0.1              # floor: no trade below 10% of SWING_EQUITY_RISK_PERCENT

# Trailing Stop
TRAILING_STOP_ACTIVATION_PCT = 0.03
TRAILING_STOP_TRAIL_PCT = 0.015

# Cooldowns & Rate Limits
SYMBOL_COOLDOWN_MINUTES = 120
MIN_PRICE_MOVEMENT_PCT = 0.0015
NEWS_DEDUP_HOURS = 2
NEWS_BATCH_SIZE = 50
SEC_EDGAR_COOLDOWN_HOURS = 4
SEC_EDGAR_RATE_LIMIT_SLEEP = 0.15
MARKET_REGIME_CACHE_SECONDS = 900      # 15-min SPY/EMA-200 regime cache TTL
TRAILING_STOP_MONITOR_INTERVAL = 60

# News / Benzinga
NEWS_SIGNAL_ALERT_THRESHOLD = 7
NEWS_SIGNAL_AUTO_TRADE_THRESHOLD = 13

# Truth Social (disabled)
TRUTH_SOCIAL_ENABLED = False
TRUTH_SOCIAL_ALERT_THRESHOLD = 7
TRUTH_SOCIAL_AUTO_TRADE_THRESHOLD = 13
TRUTH_SOCIAL_STOP_LOSS = 2.0
TRUTH_SOCIAL_TAKE_PROFIT = 8.0
TRUTH_SOCIAL_POSITION_SIZE_MULTIPLIER = 0.50

# SEC EDGAR Insider Trades
SEC_EDGAR_ALERT_THRESHOLD = 6
SEC_EDGAR_AUTO_TRADE_THRESHOLD = 13    # only $1M+ insider buys reach this (strength=14)
SEC_EDGAR_MIN_BUY_VALUE = 100_000
SEC_EDGAR_MIN_SELL_VALUE = 500_000

# FRED Macro Indicators
FRED_ENABLED = True                    # free public CSV endpoints; no API key required

# Congressional Trading (Quiver Quantitative)
QUIVER_API_KEY                         # from env; loop self-disables on 401/403
CONGRESSIONAL_ENABLED = False          # re-enable by setting QUIVER_API_KEY in Railway
CONGRESSIONAL_ALERT_THRESHOLD = 6
CONGRESSIONAL_AUTO_TRADE_THRESHOLD = 13  # max achievable ~11.2 → effectively alert-only

# Reddit Momentum
REDDIT_ENABLED = True
REDDIT_ALERT_THRESHOLD = 5.0
REDDIT_MIN_MENTIONS = 3
REDDIT_POLL_INTERVAL = 1800            # 30 min
REDDIT_AUTO_TRADE_THRESHOLD = 999      # alert-only stub

# Grok X/Twitter crypto sentiment
GROK_API_KEY                           # from env (console.x.ai)
GROK_ENABLED = True
GROK_ALERT_THRESHOLD = 7               # score ≥ 7 (bullish) or ≤ 3 (bearish) fires alert

# Webull retail crowding (disabled — endpoint returns 417)
WEBULL_ENABLED = False
WEBULL_ALERT_THRESHOLD = 5.0

# Debug / verbose flags
SWING_VERBOSE_LOGGING = True
BULL_BEAR_DEBATE_ENABLED = True        # DeepSeek Flash via OpenRouter with web search
DISCOVERY_DEBATE_ENABLED = True        # Discovery Engine only — Claude reviews each validated strategy
SLACK_VERBOSE = False                  # False = critical/trade alerts only

# Prometheus metrics
PROMETHEUS_ENABLED = True              # expose /metrics on port 8502

# Optional integrations (from env)
SENTRY_DSN                             # Sentry project DSN; omit to disable
PAGERDUTY_ROUTING_KEY                  # PagerDuty events API; omit to disable
NOTION_API_KEY                         # Notion trade journal; omit to disable
NOTION_DATABASE_ID

# Discovery Engine
BACKTEST_START_DATE = "2019-01-01"
BACKTEST_END_DATE = "2024-12-31"
WALK_FORWARD_TRAIN_MONTHS = 24
WALK_FORWARD_TEST_MONTHS = 3
DISCOVERY_SYMBOLS = ["JPM", "SPY", "COST", "BRK.B", "PG"]
DISCOVERY_MIN_TRADES = 10
DISCOVERY_P_VALUE_THRESHOLD = 0.05
DATABASE_URL                           # from env — PostgreSQL connection string
```

---

## Railway Environment Variables

Set in Railway → Project → Variables (or local `.env` for development):

| Variable | Required | Notes |
|---|---|---|
| `ALPACA_API_KEY` | Yes | Paper keys start `PK`; live keys start `AK` |
| `ALPACA_SECRET_KEY` | Yes | |
| `ALPACA_BASE_URL` | Yes | `https://paper-api.alpaca.markets` for paper; `https://api.alpaca.markets` for live |
| `ANTHROPIC_API_KEY` | Yes | Used for discovery debate; bot degrades gracefully if missing |
| `FINNHUB_API_KEY` | Yes | Free tier sufficient; fundamentals gate always passes if missing |
| `SLACK_ALERTS_WEBHOOK` | Yes | Incoming webhook URL for #trading-alerts |
| `SLACK_DECISIONS_WEBHOOK` | Yes | Incoming webhook URL for #trading-decisions |
| `SLACK_PERFORMANCE_WEBHOOK` | Yes | Incoming webhook URL for #trading-performance |
| `SLACK_HEALTH_WEBHOOK` | Yes | Incoming webhook URL for #trading-health |
| `SLACK_SIGNING_SECRET` | Recommended | HMAC-SHA256 verification for slash-command requests |
| `OPENROUTER_API_KEY` | Recommended | DeepSeek Flash for bull/bear debate + news NLP; falls back to Anthropic if missing |
| `DATABASE_URL` | Optional | PostgreSQL `postgresql://user:pass@host/db`; without it all DB calls silently no-op |
| `GROK_API_KEY` | Optional | xAI API key (console.x.ai); `grok_loop` skips if missing |
| `QUIVER_API_KEY` | Optional | Quiver Quantitative free-tier key; congressional + short-interest loops self-disable on 401 |
| `SENTRY_DSN` | Optional | Sentry project DSN; omit to disable error monitoring |
| `NOTION_API_KEY` | Optional | Notion trade journal integration |
| `NOTION_DATABASE_ID` | Optional | Notion database ID for trade journal |
| `PAGERDUTY_ROUTING_KEY` | Optional | PagerDuty Events API v2; omit to disable phone escalation |
| `HEALTH_PORT` | Optional | Flask/Prometheus port; defaults to 8502 |
| `LLM_PROVIDER` | Optional | `anthropic` (default), `kimi`, or `openai_compatible` |
| `OPENAI_COMPATIBLE_API_KEY` | Optional | Moonshot/Kimi API key when `LLM_PROVIDER=kimi` |

---

## Port Architecture

| Port | Service | Notes |
|---|---|---|
| 8501 | Streamlit dashboard | Railway public domain routes here |
| 8502 | Flask server | `/health` JSON · `/metrics` Prometheus text · `/slack/commands` POST handler. Port configurable via `HEALTH_PORT` env var |

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

The `/metrics` endpoint (Prometheus text/plain) exposes:
```
bot_uptime_seconds        gauge
bot_equity_usd            gauge
bot_open_positions        gauge
bot_daily_pnl_pct         gauge
bot_vix_level             gauge   (from FRED MACRO_SNAPSHOT)
bot_websocket_connected   gauge
bot_signals_fired_total   counter
```

---

## Operator Interface (Slack Slash Commands)

All commands POST to `:{HEALTH_PORT}/slack/commands`. Requests are HMAC-SHA256 verified against
`SLACK_SIGNING_SECRET`; unsigned requests accepted in development (no secret configured).
Railway setup: add a second public domain pointing to `HEALTH_PORT`, then register that URL as
the Slack app's Request URL.

| Command | Effect |
|---|---|
| `/status` | Returns equity, open positions, daily P&L, VIX, market regime, and pause state |
| `/buy SYMBOL SHARES` | Submits a market buy order; blocked if paused or daily loss limit hit |
| `/sell SYMBOL` | Closes the full open position for that symbol at market |
| `/pause` | Sets `_bot_paused=True`; all `_process_symbol` paths skip immediately |
| `/resume` | Clears `_bot_paused`; trading resumes from the next loop tick |
| `/help` | Lists all commands |

---

## Swing Trade Signal Flow

```
swing_loop() [daily 10:30 AM EST]
  └─ for each symbol in SWING_SYMBOLS:
       └─ _process_symbol(symbol, [SwingStrategy, BollingerMeanReversionStrategy],
                          pre_execute_hook=_swing_pre_trade_hook)
            ├─ get_historical_bars(365 days daily)  ← Alpaca IEX + Finnhub overlay
            ├─ strategy.generate_signals(df)
            │    SwingStrategy:
            │      EMA_short > EMA_long (bullish trend)
            │      MACD crossed above signal line (momentum confirmation)
            │      rsi_entry_low ≤ RSI ≤ rsi_entry_high
            │      reward/risk ≥ SWING_MIN_RR_RATIO (2.0) → else signal='hold'
            │    BollingerMeanReversionStrategy:
            │      HalfLifeSignal gate (OLS OU; β ∈ (−1, 0); HL ∈ [1, 30])
            │      close crosses below BB_lower AND RSI < rsi_entry (30)
            │      R/R ≥ 2.0; TP = BB_middle; SL = STOP_LOSS_PERCENT
            │
            ├─ [if signal='buy'] Gate Chain ① through ⑩ in _process_symbol
            │    (heat cap, correlation guard, FINRA veto, then pre_execute_hook)
            │
            ├─ [if signal='buy'] _swing_pre_trade_hook(symbol, signal, strategy)
            │    ├─ _check_upcoming_earnings() [EARNINGS_FILTER_ENABLED]
            │    │    └─ within 48h → earnings_override_multiplier=0.25 (NOT a block)
            │    ├─ _check_fundamentals(symbol)   ← Finnhub /stock/metric + /calendar/earnings
            │    │    ├─ Negative P/E → BLOCK
            │    │    ├─ EPS decline > 20% YoY → BLOCK
            │    │    └─ (earnings block only if EARNINGS_FILTER_ENABLED=False)
            │    └─ _debate_trade(symbol, signal, strategy)  ← 3 LLM calls via llm_client
            │         ├─ asyncio.gather(bull_call, bear_call)  ← parallel + web search
            │         └─ synthesis_call → JSON verdict → proceed / skip / reduce_size
            │              └─ SKIP → notify_trade_skipped → abort
            │
            ├─ Gate Chain ⑪–⑮ (circuit breaker, VIX gates, ADX, candlestick)
            │
            ├─ KellySizer.get_position_size() × all multipliers
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

### Crypto Scalp Symbols (scalp_loop disabled)

| Symbol | Notes |
|---|---|
| BTC/USD | Primary crypto scalp via WebSocket |
| ETH/USD | Secondary; only traded when RSI stronger than BTC |

---

## Architecture Notes

### Signal module imports
- `KalmanTrendSignal`, `HurstSignal`, and `KellySizer` are imported inside `strategies/swing_strategy.py`, not directly in `bot.py`. `AnchoredVWAPSignal` is imported inside `strategies/smb_strategy.py`. `bot.py` imports `CorrelationGuard` and `ShortInterestSignal` directly because it calls them inline in the gate chain. The signal modules attached to strategy objects (`_kalman`, `_hurst`, `_kelly`, `_avwap`) are accessed only through those strategy instances.

### Async safety
- All `trading_client.*` SDK calls are wrapped in `asyncio.to_thread()` — no blocking calls on the event loop
- `_open_trade_ids` is protected by `self._trade_ids_lock = asyncio.Lock()` — all reads and writes acquire the lock
- `_update_loss_cache()` is `async def` — all callers use `await`
- Bull/bear debate runs both LLM calls in parallel via `asyncio.gather()`
- All gate-chain DB and HTTP checks are wrapped in `asyncio.to_thread()`

### Database (SQLAlchemy)
- `bot.py` uses `create_engine(url, pool_pre_ping=True)` stored as `self._db_engine`
- `dashboard.py` uses `@st.cache_resource` engine via `_get_engine()`
- Both use `engine.begin()` for writes (auto-commit on context exit) and `text()` with named `:params`
- Raw psycopg2 was fully removed from both files

### Signal cooldown key
- Key is `f"{symbol}-{strategy.name}"` (NOT including signal direction) — prevents buy/sell having separate cooldown windows on the same symbol+strategy
- General active-signal cooldown is 1 hour; stop-loss triggered cooldown is `SYMBOL_COOLDOWN_MINUTES` (120 min)

### FRED macro conviction multiplier
- `get_conviction_multiplier()` in `strategies/fred_strategy.py` reads the module-level `MACRO_SNAPSHOT["vix"]`
- Returns 0.7 when VIX > 30, else 1.0; returns 1.0 safely if FRED data hasn't loaded yet (startup window before first fetch)
- Applied in `news_loop` and `sec_edgar_loop` only, after `sig["auto_trade"]` is True, before symbol cooldown check
- Does NOT modify the strength value shown in Slack — only gates the auto-trade execution path
- At 0.7× a news/EDGAR signal needs raw strength ~18.6 to cross the threshold of 13 — effectively suppresses all auto-trades when VIX > 30

### Kelly sizer
- `KellySizer` instances are attached to each strategy at startup (`strategy._kelly`)
- `update_capital()` is called from `_check_account_status()` each account-status poll to keep `base_capital` current
- Falls back to 2% default (`DEFAULT_KELLY_FRACTION`) when fewer than 20 closed trades exist for that `signal_type`
- Results are cached for 60 minutes; `invalidate_cache()` forces a fresh DB read

### Portfolio heat cap
- Calculated as `∑(|market_value| × stop_loss_percent) / equity`
- Checked before correlation guard in `_process_symbol`; fires a critical Slack alert if triggered
- Threshold: `PORTFOLIO_HEAT_CAP = 0.15` (15% of equity at risk across all open positions)

### Performance Brain
- `PERFORMANCE_SCALING_ENABLED = True` — adjusts position size based on last 20-trade win rate
- Floor enforced by `POSITION_SIZE_FLOOR = 0.1` so no trade ever falls below 10% of base risk

### Sector alert cooldown
- `_sector_alert_cooldown` dict prevents repeated sector-concentration Slack alerts within 4 hours
- Sector map in `_SECTOR_MAP` (module-level) covers only the 6 SWING_SYMBOLS; news/EDGAR loops do not yet use it

### Confluence tracking
- `_record_daily_signal(symbol, source)` tracks which sources fired on a given ticker each day
- When ≥ 2 distinct sources fire the same day, a confluence alert fires to #trading-alerts (deduped via `_confluence_alerted`)
- Resets daily at midnight EST

### Market regime
- Primary: SPY vs EMA200 (15-min cache) — returns `'bull'` / `'bear'` / `'neutral'`
- Secondary: SPY ADX(14) (4-hour cache) — returns `'trending'` / `'choppy'` / `'neutral'`
- Both cached to avoid redundant Alpaca calls across loop ticks

---

## Known Issues and Tech Debt

1. **`bot.py` is ~2764 lines** — monolithic. The DB methods, regime check, fundamentals gate, debate, gate chain, and signal-stack tracking could be extracted into modules, but low priority until the next major feature phase.

2. **V, JPM, PG have no validated edge** — they remain in `SWING_SYMBOLS` to accumulate live `signal_outcomes` data. Consider removing after 6 months if they don't signal (or are consistently blocked by the fundamentals gate).

3. **Alpaca 15-minute delay** — free IEX feed has 15-min delay for stocks. Mitigated by Finnhub real-time price overlay in `get_historical_bars()`. Crypto is unaffected (real-time WebSocket).

4. **`_seen_accessions` in `SECEdgarStrategy` is in-memory** — on Railway restart, it re-processes the last 40 filings. The `SEC_EDGAR_COOLDOWN_HOURS` (4h) per ticker prevents duplicate signals, but ~80 duplicate HTTP requests happen on each cold start. Acceptable for 30-min polling.

5. **Truth Social loop is dead code** — `truth_social_loop` exists in `bot.py` and is included in `asyncio.gather()`, but `TRUTH_SOCIAL_ENABLED=False` causes it to return immediately. No performance impact.

6. **No structured logging** — all output is `print()`. No log levels, no rotation, no file output. Hard to filter signal vs. noise in Railway logs.

7. **Exit monitor symbol-only matching** — `_exit_monitor_loop` matches closed sell orders to `_open_trade_ids` by symbol. If two sell orders for the same symbol fill in one 10-min window (shouldn't happen in practice), only the first match is logged.

8. **`backtester.py`** — early prototype. Do not modify without understanding it first.

9. **Congressional trading loop is disabled** — `CONGRESSIONAL_ENABLED=False`. Free data sources unavailable (House Stock Watcher S3 went private in 2024). To re-enable: add `QUIVER_API_KEY` to Railway env vars (Quiver Quantitative, $30/mo at quiverquant.com), set `CONGRESSIONAL_ENABLED=True` in `config.py`, and restore the `Authorization: Token` header in `_fetch_trades()`. The `_COMMITTEE_MEMBERS` set reflects the 119th Congress and will need updating every two years.

10. **Webull loop is disabled** — `WEBULL_ENABLED=False`; endpoint returns HTTP 417. Disabled until a working data source is found.

---

## Dependencies (`requirements.txt`)

```
alpaca-py          # Alpaca trading + data SDK
pandas             # DataFrames
pandas-ta          # Technical indicators (EMA, MACD, RSI, BBands, ADX)
numpy
scipy              # t-test for discovery engine validation
pytz
requests
anthropic          # Claude API (discovery debate fallback)
openai             # OpenAI-compatible client (OpenRouter + Kimi/Moonshot)
python-dotenv      # .env loading
flask              # /health + /metrics + /slack/commands endpoint (port 8502)
streamlit          # Dashboard (port 8501)
plotly             # Charts in dashboard
matplotlib
seaborn
psycopg2-binary    # PostgreSQL driver (needed by SQLAlchemy for postgresql:// URLs)
sqlalchemy         # ORM/connection layer for bot.py and dashboard.py
pyarrow            # Parquet cache for discovery engine
sentry-sdk         # Error monitoring (optional — omit SENTRY_DSN to disable)
notion-client      # Notion trade journal (optional — omit NOTION_API_KEY to disable)
PyWavelets         # Optional wavelet pre-denoising for KalmanTrendSignal
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

# Discovery Engine v1 (243-combo grid, ~7 min per symbol)
python -m discovery.discovery_engine

# Discovery Engine v2 (multi-strategy, multiprocessing)
python -m discovery.discovery_engine_v2

# Syntax check before committing
python -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('.').rglob('*.py') if '.git' not in str(p) and 'discovery/data' not in str(p)]"
```

The bot requires all Railway env vars in a local `.env` file. `config.py` calls `load_dotenv()`
at module load time — this is intentional and critical. Do not move it.
