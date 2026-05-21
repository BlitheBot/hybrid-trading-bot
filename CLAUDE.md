# Hybrid Trading Bot — Claude Code Session Rules

## Operating Rules

1. **Read this entire file** before touching any code file.
2. **Never auto-deploy strategies** — all strategy logic changes require explicit user confirmation before Railway deployment.
3. **Always run syntax check** before committing:
   ```
   python -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('.').rglob('*.py') if '.git' not in str(p) and 'discovery/data' not in str(p)]"
   ```
4. **Always commit and push** after completing code changes.

---

## Architecture Overview

A Python asyncio trading bot running 24/7 on Railway with 19 concurrent loops. Equity swing trades run daily at 10:30 AM EST across 6 symbols using EMA/MACD/RSI + Kalman/Hurst/VWAP signal gates, Kelly position sizing, and a 15-gate risk chain including correlation guard, FINRA short interest veto, fundamentals check, and a 3-call Claude bull/bear debate before every entry. Alternative data loops scan Benzinga news, SEC EDGAR Form 4 filings, FRED macro indicators, Reddit, and X/Twitter sentiment. All decisions post to Slack. Completed trades log to PostgreSQL via SQLAlchemy. An overnight Discovery Engine runs walk-forward grid search + genetic programming to find new validated strategy parameters.

---

## Strategy / Signal Files

| File | Purpose |
|---|---|
| `bot.py` | Main TradingBot class — 19 async loops, full gate chain, all trade execution |
| `config.py` | All config constants; reads `.env` via `load_dotenv()` at import time (critical) |
| `llm_client.py` | Unified LLM abstraction — routes to Anthropic, OpenRouter, or Moonshot |
| `notifications.py` | Slack webhook functions for alerts/decisions/health/performance channels |
| `utils.py` | `get_historical_bars()`, `get_spy_data()`, `get_finnhub_price()` |
| `dashboard.py` | 7-tab Streamlit dashboard (port 8501) |
| `strategies/base_strategy.py` | Abstract base: `generate_signals()`, `execute_trade()`, `calculate_safe_quantity()` |
| `strategies/swing_strategy.py` | EMA crossover + MACD + RSI; per-symbol params from Discovery Engine |
| `strategies/bollinger_mean_reversion_strategy.py` | BB lower-break + RSI oversold; half-life OU gate; middle-band exit |
| `strategies/smb_strategy.py` | Crypto scalp — Kalman/VWAP crossover + AnchoredVWAP gate; BTC/ETH |
| `strategies/news_strategy.py` | Benzinga via Alpaca News API; LLM NLP scoring with keyword fallback |
| `strategies/sec_edgar_strategy.py` | SEC EDGAR Form 4 XML parsing; strength-tiered scoring; 429 backoff |
| `strategies/fred_strategy.py` | FRED macro via public CSV; `MACRO_SNAPSHOT` + `get_conviction_multiplier()` |
| `strategies/congressional_trading_strategy.py` | Quiver Quantitative congressional trades; disabled pending API key |
| `strategies/reddit_strategy.py` | Scans WSB + r/stocks; SP500 ticker extraction; alert-only |
| `strategies/grok_strategy.py` | xAI grok-3-mini + live X search; BTC/ETH sentiment 0–10; alert-only |
| `strategies/truth_social_strategy.py` | Disabled — returns [] immediately |
| `strategies/webull_strategy.py` | Disabled — endpoint returns 417 |
| `strategies/kalman_signal.py` | 1D Kalman filter; outputs trend, slope, noise_ratio, signal ∈ {−1,0,+1} |
| `strategies/hurst_signal.py` | Rolling Hurst via R/S analysis; trending/random/mean-reverting regime |
| `strategies/vwap_signal.py` | Anchored VWAP; distance_pct + volume_ratio gate; signal ∈ {−1,0,+1} |
| `strategies/halflife_signal.py` | OLS Ornstein-Uhlenbeck half-life; gates BB mean reversion entries |
| `strategies/kelly_sizer.py` | Half-Kelly sizing capped 10%; pulls from `signal_outcomes`; 20-trade min |
| `strategies/correlation_guard.py` | Pearson ρ on 60-day closes; blocks ρ > 0.75 or same-sector concentration |
| `strategies/short_interest_signal.py` | FINRA CNMSshvol; ratio ≥ 65% → veto buy; uptick → squeeze boost |
| `discovery/discovery_engine.py` | v1 — 243-combo EMA/RSI grid; scipy t-test; writes `strategy_results` |
| `discovery/discovery_engine_v2.py` | v2 — 5 strategy families; top-100 S&P 500; writes `discovery_results` JSONB |
| `discovery/regime_adapter.py` | Returns best approved strategy per symbol + SPY regime |
| `discovery/genetic_engine.py` | Genetic programming; 50-pop × 20-gen; IC fitness; graduates IC > 0.05 |
| `discovery/fitness_evaluator.py` | Walk-forward IC scoring across 4-month folds |
| `discovery/indicator_library.py` | Primitive set for evolved indicator expressions |
| `discovery/symbol_universe.py` | Top-N S&P 500 by 30-day avg volume |
| `data/sp500_tickers.py` | SP500_TICKERS list used by news/EDGAR/Reddit strategies |

---

## Database Tables

- `signal_outcomes` — live trade log; primary ML training data; exit backfilled by `_exit_monitor_loop`
- `strategy_results` — Discovery Engine v1 walk-forward results
- `strategy_circuit_breakers` — per-strategy drawdown pauses; auto-resets on recovery
- `discovery_results` — Discovery Engine v2 multi-strategy JSONB results; approval via dashboard

---

## Critical Environment Variables

**Required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `SLACK_ALERTS_WEBHOOK`, `SLACK_DECISIONS_WEBHOOK`, `SLACK_PERFORMANCE_WEBHOOK`, `SLACK_HEALTH_WEBHOOK`, `DATABASE_URL`

**Recommended:** `OPENROUTER_API_KEY` (DeepSeek Flash for debate + news NLP), `SLACK_SIGNING_SECRET`

**Optional:** `GROK_API_KEY`, `QUIVER_API_KEY`, `SENTRY_DSN`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `PAGERDUTY_ROUTING_KEY`, `HEALTH_PORT` (default 8502), `LLM_PROVIDER`, `OPENAI_COMPATIBLE_API_KEY`

---

## 19 Async Loops

| # | Method | Status |
|---|---|---|
| 1 | `scalp_loop` | **Disabled** (`SCALP_ENABLED=False`) |
| 2 | `swing_loop` | Active |
| 3 | `news_loop` | Active |
| 4 | `truth_social_loop` | **Disabled** (`TRUTH_SOCIAL_ENABLED=False`) |
| 5 | `sec_edgar_loop` | Active |
| 6 | `fred_loop` | Active |
| 7 | `congressional_trading_loop` | **Disabled** (`CONGRESSIONAL_ENABLED=False`) |
| 8 | `health_report_loop` | Active |
| 9 | `performance_report_loop` | Active |
| 10 | `trailing_stop_monitor_loop` | Active |
| 11 | `_exit_monitor_loop` | Active |
| 12 | `market_open_notification_loop` | Active |
| 13 | `discovery_loop` | Active |
| 14 | `reddit_loop` | Active |
| 15 | `symbol_universe_loop` | Active |
| 16 | `market_close_digest_loop` | Active |
| 17 | `grok_loop` | Active |
| 18 | `webull_loop` | **Disabled** (417 error) |
| 19 | `indicator_discovery_loop` | Active |

---

## 15-Gate Chain (`_process_symbol`, in order)

1. `trading_halted_for_day`
2. `_bot_paused`
3. Symbol + strategy cooldown
4. Already in position
5. Portfolio heat cap
6. Correlation guard
7. FINRA short interest veto
8. Fundamentals gate (Finnhub)
9. Earnings filter
10. Bull/bear debate
11. Strategy circuit breaker
12. VIX extreme gate (>40)
13. VIX spike gate (>35)
14. ADX regime filter
15. Candlestick confirmation
