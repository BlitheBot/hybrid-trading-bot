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

A Python asyncio trading bot running 24/7 on Railway with 22 concurrent loops. The swing screener runs every 5 minutes during market hours (9:30 AM–4:00 PM EDT, Mon–Fri) across up to 250 symbols pulled by volume from the `active_tickers` PostgreSQL table (6 priority symbols — JPM, SPY, COST, BRK.B, PG, V — always included). Per-symbol 4-hour cooldown is set the moment a signal enters the protection stack (debate gate), not on trade execution — this prevents the same symbol from being debated repeatedly in one session. Short selling is enabled (`SHORT_SELLING_ENABLED=True`): a SELL signal with no open long executes a short sale with ATR-based stop/target, full debate + fundamentals gate, and 1:2 minimum R/R. SHORT debate gate: bull must raise **4+ concrete fundamental/macro reasons** to block the trade (LONG path remains at 2+ bear objections). **Active short exit**: every swing cycle checks open shorts for thesis reversal (RSI < 55 AND MACD crosses above signal) — if both true, cancels OCO and covers at market immediately. The Discovery Engine uses the same 250-symbol universe from `active_tickers`. All positions use EMA/MACD/RSI + Kalman/Hurst/VWAP signal gates, Kelly sizing, and a 15-gate risk chain. Alternative data loops scan Benzinga news, SEC EDGAR Form 4 filings, FRED macro indicators, Reddit, and X/Twitter sentiment. All decisions post to Slack. Completed trades log to PostgreSQL via SQLAlchemy.

**Signal conditions (swing_strategy.py):**
- LONG: EMA50 > EMA200 AND MACD above signal within last 3 bars AND RSI in [35, 65] AND Kalman noise < 0.4 AND Hurst H ≥ 0.55
- SHORT: at least 2 of 3 — RSI > 70, MACD fresh bearish crossover, EMA50 < EMA200
- Crypto scalp (smb_strategy.py): uses 1-minute bars (390 bars = ~1 session), Kalman Q=5e-3 (intraday), AnchoredVWAP gate at 0.15% distance / 1.1× volume

---

## Strategy / Signal Files

| File | Purpose |
|---|---|
| `bot.py` | Main TradingBot class — 21 async loops, full gate chain, all trade execution |
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
| `discovery/discovery_engine.py` | v1 — 243-combo EMA/RSI grid; scipy t-test → permutation gate; writes `strategy_results` |
| `discovery/permutation_framework.py` | Masters 4-step MCPT validation; position-vector backtest + bar permutation; **regime-aware** per-regime MCPT; writes `validated_strategies` |
| `discovery/regime_classifier.py` | 4-regime classifier (BULL_TREND/BEAR_TREND/HIGH_VOL/CHOPPY); `classify_regime()`, `get_current_regime()` (4h cache); SPY-only fallback when VIX missing |
| `discovery/decay_monitor.py` | `StrategyDecayMonitor` — live vs backtested Sharpe decay detection; 4 response tiers; writes `strategy_decay_status` + `revalidation_queue` |
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
- `strategy_results` — Discovery Engine v1 walk-forward results; `permutation_tested` BOOLEAN marks combos that ran the MCPT gate (status `validated` = passed both gates, `rejected_permutation` = passed t-test but failed MCPT)
- `strategy_circuit_breakers` — per-strategy drawdown pauses; auto-resets on recovery
- `discovery_results` — Discovery Engine v2 multi-strategy JSONB results; approval via dashboard
- `validated_strategies` — strategies that cleared the permutation framework; stores IS/WF p-values, scores, params, and **per-regime validity** (`valid_bull_trend`, `valid_bear_trend`, `valid_high_vol`, `valid_choppy`, `best_regime`, `regime_sharpes` JSONB); authoritative "genuine edge" record consulted by the live regime gate
- `signal_outcomes.regime_class` — 4-regime label captured at signal time (added via `ALTER TABLE IF NOT EXISTS`); powers the weekly regime performance breakdown
- `signal_outcomes.decay_multiplier` — decay-monitor position multiplier applied to each trade (audit trail)
- `strategy_decay_status` — per `(signal_type, symbol)` decay state: `decay_ratio`, `status`, `position_multiplier`, `consecutive_signals_below`, `re_validation_requested`, `disabled`
- `revalidation_queue` — decay/manual re-validation requests (`status` pending/running/complete/failed); `discovery_version` ('v1'/'v2', default 'v2') marks which engine owns each request. v1 grid-search engine processes only `discovery_version='v1'`; v2 (regime-aware, live) re-validates the full universe on its weekly Friday run rather than draining this queue

---

## Strategy Validation Pipeline (Discovery Engine v1)

Every parameter combo passes through **two mandatory gates** before being marked `validated`:

1. **SciPy t-test gate** (`_validate`) — walk-forward test-period CAGR significantly > 0 (p < `DISCOVERY_P_VALUE_THRESHOLD`, default 0.05) with ≥ `DISCOVERY_MIN_TRADES` trades.
2. **Permutation framework gate** (`permutation_framework.validate_strategy_edge`) — Timothy Masters 4-step Monte Carlo Permutation Test. Runs **once per symbol** (it re-optimizes the whole grid on each permuted path, so it tests the strategy *family*, not one combo) and the verdict applies to all that symbol's t-test passers:
   - **Step 1 — Position-vector backtest**: posture vector S ∈ {+1, −1, 0}; close-to-close log returns; strategy returns = S_t × R_{t+1}; scored by `calculate_objective_score` (Sharpe or Profit Factor, on the return vector — never a trade list).
   - **Step 2 — Bar permutation** (`get_permutation`): single shuffle index applied to candle gaps + intra-bar moves, preserving return moments and final close while destroying path memory. `start_index` keeps a training prefix intact.
   - **Step 3 — In-sample MCPT** (1000 iters): p = count(PF_perm ≥ PF_real)/N; p > 0.01 ⇒ data-mining bias ⇒ discard.
   - **Step 4 — Walk-forward MCPT** (200 iters, training period preserved): p > 0.01 ⇒ out-of-sample selection luck ⇒ reject.
   - **Step 5 — Fail-fast gateway**: 80/20 hard wall; in-sample runs first and short-circuits on failure; only both-pass writes to `validated_strategies`.
   - Histograms of permuted-score distributions saved to `discovery/reports/` (gitignored). Iterations parallelized via multiprocessing (seed = base_seed + worker_id), with a serial fallback.
   - Config: `PERMUTATION_ENABLED`, `PERMUTATION_P_THRESHOLD`, `PERMUTATION_INSAMPLE_ITERS`, `PERMUTATION_WALKFORWARD_ITERS`, `PERMUTATION_OBJECTIVE`, `PERMUTATION_WORKERS`.
   - Unit tests: `discovery/test_permutation_framework.py` (moment preservation, final-close invariance, training-period preservation, objective directionality).

Per-symbol summary log line: `[Discovery] {symbol}: {n_combos} combos tested → {n_ttest} passed t-test → {n_permutation} passed permutation → {n_promoted} promoted`.

---

## Regime-Aware Validation & Live Gating

**4 market regimes** (`discovery/regime_classifier.py`), evaluated in priority order:
- **HIGH_VOL** — VIX > 30 (overrides trend)
- **BULL_TREND** — SPY EMA50 > EMA200 AND VIX < 20 AND SPY 20-day return > +2%
- **BEAR_TREND** — SPY EMA50 < EMA200 AND VIX > 25 AND SPY 20-day return < −2%
- **CHOPPY** — everything else

VIX comes from the FRED-sourced `MACRO_SNAPSHOT`; if VIX is missing the classifier falls back to SPY-only rules (HIGH_VOL disabled, VIX sub-conditions dropped). Historical backtest tagging approximates per-bar VIX with SPY realized volatility (`realized_vol_proxy`) since no per-bar VIX feed exists.

**Discovery (regime-aware MCPT):** the permutation gate runs **independently within each regime's bars** (`validate_strategy_edge_regime_aware`). A regime needs ≥ `REGIME_MIN_BARS` (50) bars to be scored; MCPT iterations scale by regime bar share (floor 200). A strategy can be validated for some regimes and not others — `validated_strategies` stores a `valid_*` flag per regime plus `best_regime` (highest Sharpe) and `regime_sharpes` JSONB.

**Live gating (`bot.py` swing loop):** `_get_current_regime_class()` computes the current regime once per cycle (cached 4h). Before evaluating each symbol, `_regime_gate_ok()` looks up its `validated_strategies` flags and **only proceeds if the strategy is validated for the current regime**. **Fail-open**: no DB / no validation row / any error → trade proceeds with a warning log. Toggle via `REGIME_GATING_ENABLED` (default on). Log line: `[Regime] {symbol} strategy validated for {valid_regimes} — current regime {current} — PROCEED|SKIP`.

**Regime performance tracking:** `signal_outcomes.regime_class` records the regime at signal time; the Sunday Performance Brain digest adds a per-regime win-rate / avg-P&L breakdown to compare live vs backtested edge and detect decay.

Config: `REGIME_HIGH_VOL_VIX`, `REGIME_BULL_VIX_MAX`, `REGIME_BEAR_VIX_MIN`, `REGIME_BULL_RETURN_PCT`, `REGIME_BEAR_RETURN_PCT`, `REGIME_CACHE_SECONDS`, `REGIME_MIN_BARS`, `REGIME_GATING_ENABLED`.

---

## Strategy Decay Monitoring (Loop 22)

Detects validated strategies that stop working live and throttles/disables them before serious damage (`discovery/decay_monitor.py`, `StrategyDecayMonitor`). Keyed by `(signal_type, symbol)` — the granularity `signal_outcomes` records.

- **Live performance**: live Sharpe / profit factor / win rate from the last 30 closed signals (`DECAY_LOOKBACK_SIGNALS`). **Minimum 30 closed signals (`DECAY_MIN_SIGNALS`) before any action** — never penalize thin data. Live Sharpe is per-trade, annualized by observed trade frequency (so it's comparable to the backtested annualized Sharpe) and capped at ±50 to survive degenerate near-identical-return data.
- **Backtested baseline**: from `validated_strategies` (regime-specific Sharpe for the current regime, else overall). No baseline → not penalized unless live Sharpe is negative.
- **Decay ratio** = live Sharpe / backtested Sharpe.

**Response tiers** (`apply_decay_response`): 

| Status | Trigger | Action |
|---|---|---|
| HEALTHY | ratio ≥ 0.8 | 1.0× — no action |
| DEGRADED | 0.5 ≤ ratio < 0.8 | 0.5× size, Slack warning |
| DECAYING | ratio < 0.5 (≥30 signals) | 0.25× size + re-validation request, urgent Slack |
| CRITICAL | negative live Sharpe (≥15 recent signals) | disable + cancel/close positions + re-validation + PagerDuty (via `notify_alert` level CRITICAL) |

CRITICAL is scale-robust (negative-Sharpe sign survives annualization) and overrides the ratio bands. `decay_monitor_loop` runs every 6h (`DECAY_LOOP_INTERVAL_SECONDS`), writes `strategy_decay_status`, and refreshes a gating cache. In `_process_symbol`: `disabled=True` skips the symbol; `position_multiplier < 1.0` stacks onto the multiplier chain with a 0.1× floor (`DECAY_MULTIPLIER_FLOOR`); the applied multiplier is logged to `signal_outcomes.decay_multiplier`. Fail-open throughout (any error logs a traceback and trading continues). Re-validation requests are processed first by the Discovery Engine `run()`, which resets decay status to HEALTHY on a successful re-promotion. Dashboard tab "🩺 Decay" shows color-coded status with Re-validate / Disable / Reset overrides; the Sunday digest adds a tier-count + queue-depth summary.

Config: `DECAY_MONITOR_ENABLED`, `DECAY_MIN_SIGNALS`, `DECAY_CRITICAL_MIN_SIGNALS`, `DECAY_LOOKBACK_SIGNALS`, `DECAY_HEALTHY_RATIO`, `DECAY_DEGRADED_RATIO`, `DECAY_DEGRADED_MULT`, `DECAY_DECAYING_MULT`, `DECAY_MULTIPLIER_FLOOR`, `DECAY_LOOP_INTERVAL_SECONDS`, `DECAY_CACHE_SECONDS`.

---

## Critical Environment Variables

**Required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `SLACK_ALERTS_WEBHOOK`, `SLACK_DECISIONS_WEBHOOK`, `SLACK_PERFORMANCE_WEBHOOK`, `SLACK_HEALTH_WEBHOOK`, `DATABASE_URL`

**Recommended:** `OPENROUTER_API_KEY` (DeepSeek Flash for debate + news NLP), `SLACK_SIGNING_SECRET`

**Optional:** `GROK_API_KEY`, `QUIVER_API_KEY`, `SENTRY_DSN`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `PAGERDUTY_ROUTING_KEY`, `HEALTH_PORT` (default 8502), `LLM_PROVIDER`, `OPENAI_COMPATIBLE_API_KEY`

---

## 22 Async Loops

| # | Method | Status |
|---|---|---|
| 1 | `scalp_loop` | Active (`SCALP_ENABLED=True`) |
| 2 | `swing_loop` | Active |
| 3 | `prioritizer_loop` | Active |
| 4 | `news_loop` | Active |
| 5 | `truth_social_loop` | **Disabled** (`TRUTH_SOCIAL_ENABLED=False`) |
| 6 | `sec_edgar_loop` | Active |
| 7 | `fred_loop` | Active |
| 8 | `congressional_trading_loop` | **Disabled** (`CONGRESSIONAL_ENABLED=False`) |
| 9 | `health_report_loop` | Active |
| 10 | `performance_report_loop` | Active |
| 11 | `trailing_stop_monitor_loop` | Active |
| 12 | `_exit_monitor_loop` | Active |
| 13 | `market_open_notification_loop` | Active |
| 14 | `discovery_loop` | Active |
| 15 | `reddit_loop` | Active |
| 16 | `symbol_universe_loop` | Active |
| 17 | `market_close_digest_loop` | Active |
| 18 | `grok_loop` | Active |
| 19 | `webull_loop` | **Disabled** (417 error) |
| 20 | `indicator_discovery_loop` | Active |
| 21 | `grok_sentiment_loop` | Active (requires `XAI_API_KEY`) |
| 22 | `decay_monitor_loop` | Active (`DECAY_MONITOR_ENABLED`, 6h cadence) |

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
10. Bull/bear debate (SHORT: bull needs 4+ concrete reasons to block; LONG: bear needs 2+)
11. Strategy circuit breaker
12. VIX extreme gate (>40)
13. VIX spike gate (>35)
14. ADX regime filter
15. Candlestick confirmation
