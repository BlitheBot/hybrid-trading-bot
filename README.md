# Hybrid Trading Bot

A production algorithmic trading system running 24/7 on Railway. 19 concurrent asyncio loops span
crypto scalping (WebSocket), equity swing trading, news NLP, insider filings, macro indicators,
congressional trades, Reddit/X sentiment, and short interest — all funnel into a multi-stage gate
chain before reaching Alpaca for execution. Every live trade feeds a PostgreSQL training corpus
consumed by an overnight discovery engine.

---

## Architecture

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                  HYBRID TRADING BOT — SYSTEM ARCHITECTURE                   ║
╚══════════════════════════════════════════════════════════════════════════════╝

 ┌────────────────────────────────────────────────────────────────────────────┐
 │                         DATA SOURCES                                       │
 │                                                                            │
 │  Alpaca REST/IEX    Alpaca WebSocket   Finnhub          FRED (free CSV)   │
 │  daily OHLCV bars   crypto tick feed   real-time price  FF rate · VIX     │
 │  (15-min delay)     BTC/USD ETH/USD    + P/E · EPS      10Y · CPI YoY     │
 │                                        + earnings cal.  + unemployment     │
 │                                                                            │
 │  SEC EDGAR          FINRA CNMSshvol    Benzinga/Alpaca   Reddit JSON API  │
 │  Form 4 RSS feed    daily short-vol    News API          WSB · r/stocks   │
 │  insider filings    ratio per ticker   all S&P 500       hot posts        │
 │                                                                            │
 │  xAI Grok API       Quiver Quant       Webull (stub)                      │
 │  X/Twitter search   congressional      retail crowding                    │
 │  BTC · ETH NLP      trade disclosures  (disabled 417)                     │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │                      SIGNAL GENERATION                                     │
 │                                                                            │
 │  TECHNICAL                        ALTERNATIVE DATA                        │
 │  ──────────────────────────────   ──────────────────────────────────────  │
 │  SwingStrategy                    NewsStrategy                            │
 │    EMA cross · MACD · RSI           Benzinga LLM scoring + keyword FB     │
 │    per-symbol optimised params      429 retry · 2h dedup window           │
 │                                                                            │
 │  BollingerMeanReversionStrategy   SECEdgarStrategy                        │
 │    BB lower-break + RSI oversold    Form 4 XML parse · strength tiers     │
 │    half-life gate (OLS/Engel-G.)    buy ≥$100k · sell ≥$500k             │
 │                                     auto-trade $1M+ buys only             │
 │  SMBStrategy (crypto)                                                      │
 │    EMA9 vs VWAP crossover         FREDStrategy                            │
 │    ATR stops · 3:1 R/R            5 macro indicators · VIX conviction     │
 │    RSI momentum arbitrage BTC/ETH   multiplier for news + EDGAR loops     │
 │                                                                            │
 │  KalmanTrendSignal                CongressionalTradingStrategy            │
 │    scalar Kalman filter            Quiver API · committee 1.3×            │
 │    wavelet pre-denoising (opt.)    recency ≤7d 1.2× · 4h cooldown        │
 │                                                                            │
 │  HurstSignal                      RedditStrategy                          │
 │    R/S rescaled-range analysis     WSB + r/stocks hot posts               │
 │    trending H>0.6 / MR H<0.4      ticker extraction · 4h dedup           │
 │                                                                            │
 │  AnchoredVWAPSignal               GrokStrategy                            │
 │    rolling/weekly/monthly anchor   xAI grok-3-mini + live X search        │
 │    distance_pct + vol ratio gate   BTC/ETH sentiment 0-10 · alert-only    │
 │                                                                            │
 │  HalfLifeSignal                   ShortInterestSignal                     │
 │    Ornstein-Uhlenbeck theta → HL   FINRA CNMSshvol · ratio ≥65% = veto   │
 │    suggested holding period        squeeze signal on price change          │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │                         GATE CHAIN                                         │
 │  (evaluated in order; any failure discards the signal)                    │
 │                                                                            │
 │  ①  Daily loss limits      2% → risk×0.75 · 3.5% → risk×0.50             │
 │                             5% → all trading halted for the day           │
 │  ②  /pause kill switch     Slack slash command; instant operator override │
 │  ③  Symbol cooldown        120 min post stop-loss per symbol+strategy     │
 │  ④  Strategy circuit br.   rolling net P&L check vs PostgreSQL history    │
 │                             auto-resets when drawdown recovers            │
 │  ⑤  Portfolio heat cap     ∑(|market_value| × SL%) / equity ≤ 15%        │
 │  ⑥  Correlation guard      Pearson ρ ≤ 0.75 vs open positions            │
 │                             sector concentration: max 2 per GICS sector   │
 │  ⑦  FINRA short int. veto  short_vol_ratio ≥ 65% → skip                  │
 │                             ratio ≥ 65% + price uptick → squeeze boost    │
 │  ⑧  Fundamentals gate      P/E < 0 → block; EPS decline >20% YoY → block │
 │  ⑨  Earnings filter        earnings within 48h → position size × 0.25    │
 │  ⑩  Bull/Bear Debate       3× DeepSeek Flash via OpenRouter (web search) │
 │                             parallel bull+bear → synthesis → JSON verdict  │
 │                             proceed / skip / reduce_size (× 0.5)          │
 │  ⑪  FRED VIX multiplier    VIX>30 → auto-trade threshold ÷ 0.7           │
 │  ⑫  VIX spike gate         VIX>35 → size×0.25; VIX>40 → full block       │
 │  ⑬  ADX regime filter      SPY ADX(14) < 20 (choppy) → swing caution     │
 │  ⑭  Bear market reduction  SPY < EMA200 → position size × 0.50           │
 │  ⑮  Candlestick conf.      no bullish pattern last 3 bars → conv. −20%   │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │                      POSITION SIZING                                       │
 │                                                                            │
 │  KellySizer (half-Kelly, 90-day rolling history, min 20 trades)           │
 │    f* = (p·b − q) / b  →  f_used = min(f*/2, 10%)                        │
 │    Falls back to 2% default below sample threshold                        │
 │                                                                            │
 │  Applied multipliers (all multiplicative):                                │
 │    × risk_multiplier       (daily loss tiering)                           │
 │    × vix_risk_mult         (VIX spike gate)                               │
 │    × earnings_override     (earnings within 48h)                          │
 │    × bear_market_factor    (SPY regime)                                   │
 │    × debate_size_mult      (reduce_size verdict)                          │
 │    × performance_brain     (last 20-trade win rate scaling)               │
 │    floor: 10% of base risk percent                                        │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │                  EXECUTION  (Alpaca REST API)                              │
 │                                                                            │
 │  Bracket orders: market entry + OCA take-profit + stop-loss               │
 │  Paper (PAPER_TRADING=True default) or live via ALPACA_BASE_URL           │
 │  Trailing stop upgrade at +3% unrealized P&L (1.5% trail)                │
 │  All SDK calls wrapped in asyncio.to_thread() — no event-loop blocking    │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                        ┌───────────┴───────────┐
                        ▼                       ▼
 ┌──────────────────────────┐    ┌─────────────────────────────────────────┐
 │  PERSISTENCE             │    │  OBSERVABILITY                          │
 │                          │    │                                         │
 │  PostgreSQL (SQLAlchemy) │    │  Slack (4 webhooks + slash commands)    │
 │  ├ signal_outcomes       │    │  ├ #trading-alerts   errors + fills     │
 │  │  live ML training log │    │  ├ #trading-decisions all signals       │
 │  ├ strategy_results      │    │  ├ #trading-health   daily + weekly     │
 │  │  discovery v1 combos  │    │  └ #trading-performance weekly P&L      │
 │  ├ strategy_circuit_bkrs │    │  Slash: /status /buy /sell              │
 │  │  per-strategy pauses  │    │         /pause /resume /help            │
 │  └ discovery_results     │    │                                         │
 │     v2 JSONB params       │    │  Streamlit dashboard (port 8501)        │
 │                          │    │  Flask /health + /metrics (port 8502)   │
 │  Notion trade journal    │    │  Prometheus metrics → Grafana           │
 │  (optional)              │    │  Sentry error monitoring (optional)     │
 │                          │    │  PagerDuty phone alerts (optional)      │
 └──────────────────────────┘    └─────────────────────────────────────────┘

 ┌────────────────────────────────────────────────────────────────────────────┐
 │                    DISCOVERY ENGINE (overnight)                            │
 │                                                                            │
 │  v1 Walk-Forward          v2 Multi-Strategy        Genetic Engine         │
 │  243 EMA/RSI combos       Auto-discovers           ExpressionNode trees   │
 │  24-month train           DiscoveryStrategy        walk-forward IC eval   │
 │  3-month test windows     subclasses               50-pop · 20-gen        │
 │  scipy t-test p<0.05      top-100 S&P 500          crossover + mutation   │
 │  parquet 24h cache        multiprocessing.Pool     graduates mean_IC>0.05 │
 │                           regime Sharpe tagging    IndicatorLibrary prims │
 │                           correlation filter ρ>0.8                        │
 │  RegimeAdapter: reads approved discovery_results → live strategy params   │
 └────────────────────────────────────────────────────────────────────────────┘
```

---

## Active Loops

All 19 loops run concurrently via `asyncio.gather`. Disabled loops return immediately with no
performance impact.

| # | Loop | Interval | Status | What It Does |
|---|------|----------|--------|--------------|
| 1 | `scalp_loop` | WebSocket | **Disabled** (`SCALP_ENABLED=False`) | Crypto scalp on BTC/USD + ETH/USD; EMA9/VWAP crossover; ATR stops |
| 2 | `swing_loop` | Daily 10:30 AM EST | Active | 6 symbols; EMA cross + MACD + RSI; per-symbol discovery params; full gate chain |
| 3 | `news_loop` | 60s–15min (dynamic) | Active | Benzinga via Alpaca; LLM NLP scoring; all S&P 500 tickers; 429 backoff |
| 4 | `truth_social_loop` | — | **Disabled** | Quiver Quantitative integration not yet configured |
| 5 | `sec_edgar_loop` | 30 min | Active | EDGAR Form 4 RSS; XML parse; buy ≥$100k / sell ≥$500k; auto-trade $1M+ buys |
| 6 | `fred_loop` | Daily 7 PM EST + startup | Active | 5 FRED macro indicators; VIX conviction multiplier; weekly summary Sunday |
| 7 | `congressional_trading_loop` | — | **Disabled** | Requires `QUIVER_API_KEY`; self-disables on 401/403 |
| 8 | `health_report_loop` | Daily 9 AM EST | Active | Equity, buying power, daily P&L → #trading-health |
| 9 | `performance_report_loop` | Weekly Sun 6 PM EST | Active | Equity + positions → #trading-performance |
| 10 | `trailing_stop_monitor_loop` | 60s | Active | Upgrades static stops to trailing stops at +3% unrealized P&L |
| 11 | `_exit_monitor_loop` | 10 min | Active | Backfills exit price, P&L%, and exit reason into `signal_outcomes` |
| 12 | `market_open_notification_loop` | Daily 9:30 AM EST | Active | Morning briefing: equity, regime, watchlist → #trading-alerts |
| 13 | `discovery_loop` | Overnight | Active | Runs v2 walk-forward backtester; writes approved results to PostgreSQL |
| 14 | `reddit_loop` | 30 min | Active | Scans r/wallstreetbets + r/stocks; ticker extraction; alert-only |
| 15 | `symbol_universe_loop` | Periodic | Active | Refreshes top-100 S&P 500 by 30-day avg volume for discovery engine |
| 16 | `market_close_digest_loop` | Daily close | Active | End-of-day performance digest → Slack |
| 17 | `grok_loop` | 30 min | Active | xAI grok-3-mini + live X/Twitter search; BTC/ETH sentiment 0–10 scale |
| 18 | `webull_loop` | — | **Disabled** | Webull retail-crowding endpoint returns 417 |
| 19 | `indicator_discovery_loop` | Overnight | Active | Genetic programming; evolves novel indicator expressions; graduates IC>0.05 |

---

## Strategies

### SwingStrategy — Equity Momentum

Daily bars. Signal requires all three conditions: EMA_short > EMA_long (trend), MACD crossed above
signal line (momentum confirmation), RSI in `[rsi_entry_low, rsi_entry_high]` (neither
overbought nor oversold), and reward/risk ≥ 2.0. Parameters are per-symbol, determined by the
Discovery Engine.

| Symbol | ema_short | ema_long | rsi_period | Validated combos | Notes |
|--------|-----------|----------|------------|-----------------|-------|
| COST | 20 | 100 | 10 | 125/243 | Best Sharpe 0.87; short EMA dominates |
| BRK.B | 50 | 200 | 21 | 24/243 | Wide RSI bands required |
| SPY | 50 | 200 | 14 | 9/243 | Defaults already optimal; regime proxy |
| V | 50 | 200 | 14 | 0/243 | No validated edge; data collection only |
| JPM | 50 | 200 | 14 | 0/243 | No edge; flagged in swing_loop logs |
| PG | 50 | 200 | 14 | 0/243 | No edge; low volatility reduces signals |

### BollingerMeanReversionStrategy

Entry: close crosses below lower Bollinger band AND RSI < 30. Exit: close crosses above middle
band OR RSI > 65. A **half-life gate** (Ornstein-Uhlenbeck OLS regression) runs before every
entry: if the estimated mean-reversion half-life falls outside [1, 30] bars, the signal is
suppressed. Take-profit targets the middle band; stop-loss uses the global `STOP_LOSS_PERCENT`.
Minimum R/R of 2.0 enforced identically to SwingStrategy.

### SMBStrategy — Crypto Scalp (WebSocket)

EMA9 vs VWAP crossover on streaming tick data. ATR-based stop and 3:1 R/R target. When both
BTC/USD and ETH/USD generate simultaneous signals the system picks the one with stronger
RSI momentum. Currently disabled (`SCALP_ENABLED=False`) pending tuning.

---

## Statistical Signal Layer

These modules produce features used by strategies and gates — they do not trade directly.

| Module | Method | Output |
|--------|--------|--------|
| `KalmanTrendSignal` | 1D scalar Kalman filter; optional wavelet (db4) pre-denoising | trend, slope, noise_ratio, signal ∈ {−1,0,+1} |
| `HurstSignal` | Rescaled range (R/S) analysis; rolling 100-bar window | H exponent; regime: trending/random/mean-reverting |
| `AnchoredVWAPSignal` | Rolling, weekly, or monthly-anchored VWAP | distance_pct from VWAP; volume_ratio; signal |
| `HalfLifeSignal` | OLS on Δp ~ α + β·p_lag; HL = −ln2/ln(1+β) | halflife (bars), is_mean_reverting, OU theta, suggested holding period |
| `KellySizer` | f* = (p·b − q)/b; half-Kelly with 10% hard cap | dollars, shares, kelly_f, win_rate, payoff_ratio from PostgreSQL history |
| `CorrelationGuard` | Pearson ρ on 60-day closing prices; 30-min cache | allowed bool; reason; correlation_map; sector block check |
| `ShortInterestSignal` | FINRA CNMSshvol: ShortVolume/TotalVolume | short_interest_pct, squeeze_score, signal ∈ {−1,0,+1} |

---

## LLM Layer

The system routes LLM calls through a unified `llm_client.py` abstraction.

| Use Case | Model | Provider |
|----------|-------|----------|
| Bull/Bear debate (synthesis) | DeepSeek Flash (MODEL_FLASH) | OpenRouter |
| Bull case + Bear case (parallel) | DeepSeek Flash + live web search | OpenRouter |
| News NLP scoring | MODEL_FLASH or keyword fallback | OpenRouter |
| Grok X/Twitter sentiment | grok-3-mini + live X search | xAI |
| Discovery strategy review | Claude (ANTHROPIC_API_KEY) | Anthropic |
| Fallback if OpenRouter unavailable | claude-sonnet / kimi-v1-8k | Anthropic / Moonshot |

**Bull/Bear Debate flow:** Three LLM calls per swing buy signal. Bull and Bear prompts run in
parallel via `asyncio.gather`, each with web search enabled to pull live news. The synthesis
call receives both responses and returns structured JSON:
`{"verdict":"proceed"|"skip"|"reduce_size", "conviction":0.0-1.0, "reasoning":"..."}`.
Source URLs from both calls are surfaced in #trading-decisions.

---

## Gate Chain Detail

Pre-execution checks run in the order below. The first failure discards the trade. All checks run
inside the event loop; blocking DB/HTTP calls are wrapped in `asyncio.to_thread`.

```
Signal generated (buy)
        │
        ├─ ① trading_halted_for_day  ──────────────────────────────► SKIP
        ├─ ② _bot_paused (/pause)  ───────────────────────────────► SKIP
        ├─ ③ symbol+strategy cooldown (120 min post stop-loss)  ──► SKIP
        ├─ ④ already in position  ────────────────────────────────► SKIP
        ├─ ⑤ portfolio heat cap ≥ 15%  ───────────────────────────► SKIP (critical alert)
        ├─ ⑥ correlation guard  ──────────────────────────────────► SKIP if ρ > 0.75
        ├─ ⑦ FINRA short interest veto  ──────────────────────────► SKIP if ratio ≥ 65%
        │       └── ratio ≥ 65% + price uptick  ──────────────────► PROCEED + si_boost note
        ├─ ⑧ fundamentals gate  ─────────────────────────────────► BLOCK if P/E < 0 or EPS −20%
        ├─ ⑨ earnings filter  ───────────────────────────────────► PROCEED at 25% size
        ├─ ⑩ bull/bear debate  ──────────────────────────────────► SKIP or PROCEED (or 50% size)
        ├─ ⑪ strategy circuit breaker  ──────────────────────────► SKIP if rolling drawdown tripped
        ├─ ⑫ VIX extreme gate (VIX > 40)  ───────────────────────► BLOCK + critical alert
        │
        └─ PROCEED → position sizing → bracket order → DB log
```

Position sizing multipliers stack multiplicatively after the gate chain passes:
`risk × daily_loss_mult × vix_risk_mult × earnings_mult × bear_market_mult × debate_size_mult × performance_brain_mult`.

---

## Discovery Engine

Two engines run concurrently on an overnight schedule and write results to PostgreSQL.

### v1 — Grid Search Walk-Forward (`discovery/discovery_engine.py`)

- **Grid:** 243 parameter combinations over EMA_short (9/20/50), EMA_long (50/100/200),
  RSI_period (10/14/21), RSI entry bands (3×3 grid).
- **Walk-forward:** 24-month training / 3-month out-of-sample test windows, anchored and sliding.
- **Validation:** scipy t-test on walk-forward test CAGR values; requires p < 0.05, ≥10 test
  trades, and positive test Sharpe.
- **Storage:** `strategy_results` table with Sharpe, degradation (train − test Sharpe), p-value.
- **Cache:** Alpaca bars cached as parquet files (24h TTL) in `discovery/data/`.

### v2 — Multi-Strategy Extensible Backtester (`discovery/discovery_engine_v2.py`)

- **Strategy discovery:** Auto-imports all `DiscoveryStrategy` subclasses from `discovery/strategies/`.
- **Symbol universe:** Top-100 S&P 500 tickers by 30-day average volume (refreshed by `symbol_universe_loop`).
- **Parallelism:** `multiprocessing.Pool(min(4, cpu_count()))` workers.
- **Validation:** t-test p < 0.05, ≥30 trades, ≥60% positive walk-forward windows, degradation < 0.5.
- **Regime tagging:** Bull/bear/high-vol Sharpe computed from full dataset; stored as JSONB.
- **Correlation filter:** Of pairs with signal correlation > 0.80, only the higher-Sharpe result
  is kept.
- **Incremental:** Skips `(symbol, strategy_type, params)` tuples already `approved` in DB.
- **Storage:** `discovery_results` table (JSONB `parameters`, `pending_approval` status flag).

### Genetic Indicator Engine (`discovery/genetic_engine.py`)

- **Representation:** `ExpressionNode` trees built from a primitive library (price transforms,
  rolling stats, crossover operators, logical gates).
- **Fitness:** Walk-forward Information Coefficient (IC) across 4-month folds.
- **Evolution:** 50-member population, 20 generations, 30% mutation rate, 50% crossover rate,
  max tree depth 4.
- **Graduation:** Candidates with mean IC > 0.05 across folds are persisted and flagged for
  human review.

### RegimeAdapter (`discovery/regime_adapter.py`)

Reads `approved` rows from `discovery_results` for a given symbol and the current SPY regime
(bull/bear/high_vol). Returns the best-performing strategy type and its parameters as a drop-in
override for the live SwingStrategy instance. Falls back gracefully to hardcoded defaults when
no approved results exist.

---

## Database Schema

Four tables, created automatically on startup if they don't exist.

```sql
-- Live trade log — primary ML training corpus
CREATE TABLE signal_outcomes (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(10),
    signal_type   VARCHAR(20),    -- 'swing_long', 'swing_bb', 'discovery_ema_trend', 'scalp_long'
    entry_time    TIMESTAMP,
    exit_time     TIMESTAMP,      -- NULL until position closes
    entry_price   FLOAT,
    exit_price    FLOAT,
    pnl_pct       FLOAT,
    hold_bars     INTEGER,        -- days held (seconds / 86400)
    ema_short     INTEGER,
    ema_long      INTEGER,
    rsi_at_entry  FLOAT,
    macd_at_entry FLOAT,
    market_regime VARCHAR(20),    -- 'bull', 'bear', 'neutral'
    exit_reason   VARCHAR(30),    -- 'stop', 'target', 'manual'
    discovered_at TIMESTAMP DEFAULT NOW()
);

-- Discovery Engine v1: walk-forward backtest results
CREATE TABLE strategy_results (
    id                SERIAL PRIMARY KEY,
    symbol            VARCHAR(10),
    ema_short         INTEGER,
    ema_long          INTEGER,
    rsi_period        INTEGER,
    rsi_entry_low     FLOAT,
    rsi_entry_high    FLOAT,
    train_sharpe      FLOAT,
    test_sharpe       FLOAT,
    degradation       FLOAT,      -- train_sharpe − test_sharpe; lower is better
    p_value           FLOAT,      -- scipy t-test on walk-forward test CAGR
    total_test_trades INTEGER,
    status            VARCHAR(20),-- 'validated' or 'rejected'
    discovered_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE (symbol, ema_short, ema_long, rsi_period, rsi_entry_low, rsi_entry_high)
);

-- Per-strategy circuit breakers (auto-reset when drawdown recovers)
CREATE TABLE strategy_circuit_breakers (
    strategy_name TEXT PRIMARY KEY,
    tripped_at    TIMESTAMP DEFAULT NOW(),
    reason        TEXT
);

-- Discovery Engine v2: multi-strategy results (JSONB params)
-- Created by discovery_engine_v2; approval managed via dashboard
CREATE TABLE discovery_results (
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

Exit records are backfilled by `_exit_monitor_loop` every 10 minutes using a 7-day lookback
window of closed Alpaca sell orders matched by symbol against `_open_trade_ids`.

---

## Infrastructure

| Component | Role | Details |
|-----------|------|---------|
| **Railway** | Cloud hosting | 24/7 uptime; env vars set in Project → Variables |
| **Alpaca** | Brokerage + data | Paper (`paper-api.alpaca.markets`) or live; IEX feed (15-min delay for stocks); real-time WebSocket for crypto |
| **PostgreSQL** | Persistence | SQLAlchemy `create_engine(pool_pre_ping=True)`; 4 tables; all writes via `engine.begin()` auto-commit |
| **Slack** | Operator interface | 4 dedicated channels + slash commands (see below) |
| **Streamlit** (port 8501) | Dashboard | 5 tabs: Account · Positions · Trade Log · Discovery · Analytics (P&L chart + win rate) |
| **Flask + Prometheus** (port 8502) | Observability | `/health` JSON · `/metrics` text/plain · `/slack/commands` POST handler |
| **Finnhub** | Fundamentals + prices | Real-time stock price overlay; P/E, EPS, earnings calendar; free tier sufficient |
| **Sentry** | Error monitoring | Optional; set `SENTRY_DSN`; `traces_sample_rate=0.1` |
| **Notion** | Trade journal | Optional; set `NOTION_API_KEY` + `NOTION_DATABASE_ID` |
| **PagerDuty** | Phone alerts | Optional; set `PAGERDUTY_ROUTING_KEY` for critical circuit escalation |

### Prometheus Metrics Exposed

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

All commands POST to `:{HEALTH_PORT}/slack/commands`. Request signatures are verified via
HMAC-SHA256 against `SLACK_SIGNING_SECRET`; unsigned requests are accepted in development.

| Command | Effect |
|---------|--------|
| `/status` | Returns equity, open positions, daily P&L, VIX, market regime, and pause state |
| `/buy SYMBOL SHARES` | Submits a market buy order; blocked if paused or daily loss limit hit |
| `/sell SYMBOL` | Closes the full open position for that symbol at market |
| `/pause` | Sets `_bot_paused=True`; all `_process_symbol` paths skip immediately |
| `/resume` | Clears `_bot_paused`; trading resumes from the next loop tick |
| `/help` | Lists all commands |

---

## Risk Parameters (Quick Reference)

```python
EQUITY_RISK_PER_TRADE_PERCENT    = 2.0    # % of equity risked per scalp trade
SWING_EQUITY_RISK_PERCENT        = 1.0    # % of equity risked per swing trade
STOP_LOSS_PERCENT                = 2.0    # % drop from entry → stop
TAKE_PROFIT_PERCENT              = 6.0    # % gain from entry → take-profit
MAX_BUYING_POWER_UTILIZATION_PERCENT = 10.0
SWING_MIN_RR_RATIO               = 2.0    # min reward:risk to enter
TRAILING_STOP_ACTIVATION_PCT     = 0.03   # upgrade to trailing stop at +3%
TRAILING_STOP_TRAIL_PCT          = 0.015  # trail 1.5% below high-water mark
MAX_DAILY_LOSS_PERCENT           = 5.0    # halt all trading for the day
PORTFOLIO_HEAT_CAP               = 0.15   # max aggregate open-position risk
VIX_SPIKE_THRESHOLD              = 35     # reduce size to 25%
VIX_EXTREME_THRESHOLD            = 40     # block all new trades
BEAR_MARKET_SIZE_REDUCTION       = 0.5    # × when SPY < EMA200
SYMBOL_COOLDOWN_MINUTES          = 120
```

---

## Environment Variables

Set in Railway → Project → Variables (or a local `.env` file for development).

| Variable | Required | Notes |
|----------|----------|-------|
| `ALPACA_API_KEY` | Yes | Paper keys start `PK`; live keys start `AK` |
| `ALPACA_SECRET_KEY` | Yes | |
| `ALPACA_BASE_URL` | Yes | `https://paper-api.alpaca.markets` or `https://api.alpaca.markets` |
| `ANTHROPIC_API_KEY` | Yes | Used for discovery debate; bot degrades without it |
| `FINNHUB_API_KEY` | Yes | Free tier; fundamentals gate always passes if missing |
| `SLACK_ALERTS_WEBHOOK` | Yes | #trading-alerts |
| `SLACK_DECISIONS_WEBHOOK` | Yes | #trading-decisions |
| `SLACK_PERFORMANCE_WEBHOOK` | Yes | #trading-performance |
| `SLACK_HEALTH_WEBHOOK` | Yes | #trading-health |
| `SLACK_SIGNING_SECRET` | Recommended | Verifies slash-command requests from Slack |
| `OPENROUTER_API_KEY` | Recommended | DeepSeek Flash for bull/bear debate |
| `DATABASE_URL` | Optional | PostgreSQL `postgresql://user:pass@host/db`; without it all DB calls no-op |
| `GROK_API_KEY` | Optional | xAI API key; `grok_loop` skips if missing |
| `QUIVER_API_KEY` | Optional | Congressional trades + short interest; loops self-disable on 401 |
| `SENTRY_DSN` | Optional | Sentry project DSN |
| `NOTION_API_KEY` | Optional | Notion trade journal |
| `NOTION_DATABASE_ID` | Optional | |
| `PAGERDUTY_ROUTING_KEY` | Optional | Phone escalation on critical alerts |
| `HEALTH_PORT` | Optional | Flask port; defaults to 8502 |
| `LLM_PROVIDER` | Optional | `anthropic` (default) or `kimi` / `openai_compatible` |

---

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Bot only
python bot.py

# Dashboard only
streamlit run dashboard.py

# Both with restart supervision
python run_all.py

# Discovery Engine v1 (243-combo grid, ~7 min per symbol)
python -m discovery.discovery_engine

# Discovery Engine v2 (multi-strategy, multiprocessing)
python -m discovery.discovery_engine_v2

# Syntax check before committing
python -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('.').rglob('*.py') if '.git' not in str(p) and 'discovery/data' not in str(p)]"
```

All Railway environment variables must be present in a local `.env` file. `config.py` calls
`load_dotenv()` at module load time before the `Config` class body is evaluated — do not move it.

---

## Ports

| Port | Service |
|------|---------|
| 8501 | Streamlit dashboard (Railway public domain) |
| 8502 | Flask: `/health`, `/metrics`, `/slack/commands` (configurable via `HEALTH_PORT`) |

---

## Disclaimer

This system executes real trades with real capital when `PAPER_TRADING=False`. Automated trading
involves substantial risk of loss. Past discovery-engine results are not indicative of future
performance. All strategy parameters were validated on historical data subject to survivorship
bias and overfitting risk. Run in paper mode and review all signals manually before enabling
live execution.
