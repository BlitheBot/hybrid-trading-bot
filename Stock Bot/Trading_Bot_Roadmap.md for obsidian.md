# 🤖 Hybrid Trading Bot — Master Roadmap
> **Start Date:** April 22, 2026 | **Goal:** Financial freedom in 10-15 years | **Target:** Mid 6-figures annually

---

## 📊 Quick Status Dashboard

| Item | Status |
|------|--------|
| Bot deployed on Railway | ✅ Live |
| GitHub repo connected | ✅ BlitheBot/hybrid-trading-bot |
| Alpaca paper trading | ✅ Active — $99,887 equity |
| Slack webhook | ✅ Set up and tested |
| Uptime Robot monitor | ✅ Running |
| Antigravity installed | ✅ Ready |
| Claude Code installed | ✅ Ready |
| Git installed | ✅ Ready |
| Repo cloned locally | ✅ C:\Users\mjshi\hybrid-trading-bot |
| Claude Pro subscription | ⏳ Subscribe at claude.ai/upgrade |
| Anthropic API key | ⏳ Add to Railway variables |

---

## 🏗️ Full System Architecture

```
Strategy Discovery Engine (finds new edges autonomously)
                    ↓
        Market Regime Bot (trending or choppy?)
                    ↓ (only when conditions favorable)
┌─────────────────────────────────────────────────────┐
│                  Scanner Layer                       │
│  Scanner 1 (20 stocks) ─┐                           │
│  Scanner 2 (20 stocks) ─┤                           │
│  Scanner 3 (20 stocks) ─┼→ Signal Queue (Redis)     │
│  Scanner 4 (20 stocks) ─┤                           │
│  Scanner 5 (20 stocks) ─┘                           │
│                                                      │
│  News Bot (Benzinga, dynamic intervals) ─┐           │
│  Truth Social Bot (60 sec)              ─┼→ Queue   │
│  Reddit Momentum Bot                    ─┘           │
└─────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────┐
│                   Head Bot                           │
│  • Weighs + stacks signals from all sources         │
│  • Correlation check — highest conviction only      │
│  • Portfolio heat check (max 15% at risk)           │
│  • Time of day multiplier                           │
│  • Conviction score 0-10                            │
│  • Score 0-4 → ignore                              │
│  • Score 5-7 → Slack alert, you decide             │
│  • Score 8-10 → auto trade                         │
│  • Trump post 13+ → immediate, no chart needed      │
└─────────────────────────────────────────────────────┘
                    ↓                    ↓
           Trading Bot              Slack Alerts
           (Alpaca execution)       (your phone)
                    ↓
┌─────────────────────────────────────────────────────┐
│              Risk Management Layer                   │
│  • One position per symbol (all strategies)         │
│  • 5% daily loss limit (graduated response)        │
│  • Portfolio heat cap 15%                           │
│  • Symbol cooldown 2hrs after loss                  │
│  • VIX >35 → reduce size 75%                       │
│  • SPY below 200 DMA 20 days → bear mode           │
│  • Friday 3:45pm → close thin crypto positions     │
│  • Gap check at 9:30am market open                 │
└─────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────┐
│             Performance Brain                        │
│  • Win rate per strategy/symbol/time/regime         │
│  • Average winner vs loser size tracked             │
│  • EV = (win rate × avg win) - (loss rate × avg loss)|
│  • Day of week performance tracking                 │
│  • Auto-adjust position size on hot/cold strategies │
│  • Weekly Slack performance report every Sunday     │
└─────────────────────────────────────────────────────┘
```

---

## 🗺️ Development Phases

### ✅ Phase 0 — Foundation (Complete)
- [x] Bot built by Manus and deployed on Railway
- [x] Connected to Alpaca paper trading
- [x] GitHub repo at BlitheBot/hybrid-trading-bot
- [x] Basic SMB scalp + SMA crossover strategies
- [x] Slack workspace created, webhook configured
- [x] Uptime Robot external monitoring active

---

### 🔄 Phase 1 — Hybrid System + Sentiment (April 22 → May 22, 2026)

#### Bug Fixes (Do First)
- [ ] Fix float/string TypeError in `smb_strategy.py` line 44
- [ ] Fix `execute_trade` signature mismatch across strategies (5 vs 6 params)
- [ ] Fix f-string syntax error in `swing_strategy.py` line 134
- [ ] Fix `mean_reversion.py` mutating shared DataFrame
- [ ] Fix incomplete `requirements.txt` (add requests, numpy, pytz, pandas-ta)
- [ ] Add Flask health endpoint for Uptime Robot (port 8501)

#### Trading Bot Upgrades
- [ ] One trade per signal — one position per symbol enforced
- [ ] Crypto scalp → Alpaca `CryptoDataStream` websocket (BTC/USD, ETH/USD)
- [ ] Stock swing strategy → 50/200 EMA + MACD + RSI, daily polling at 10:30am EST
- [ ] Both running concurrently via asyncio
- [ ] Minimum 1:2 risk/reward gate before any swing entry
- [ ] Trailing stop activates at 3% profit (locks in at 1.5% below price)
- [ ] Symbol cooldown 2 hours after any losing trade
- [ ] ATR-based volatility position sizing
- [ ] Minimum price movement threshold 0.15% for crypto scalp signals
- [ ] Friday 3:45pm crypto position check — close thin positions before weekend
- [ ] Gap protection check at 9:30am for all open swing positions
- [ ] Volume confirmation on swing entries (1.5x 20-day average)

#### Sentiment Layer
- [ ] Benzinga news — dynamic intervals (60s market open, 10min midday, 15min after hours)
- [ ] Truth Social RSS — 60 second polling, 60 second wait before entry
- [ ] Truth Social — tight 2% stop, 8% take profit, 50% position size
- [ ] Truth Social — skip if price already reversed to pre-post baseline
- [ ] Claude API scoring for all sentiment signals
- [ ] Claude API fallback to keyword scoring if API unavailable
- [ ] News deduplication — same ticker within 2 hours counts once
- [ ] News source weighting — Bloomberg/Reuters/WSJ = 1.5x, unknown = 0.7x

#### Risk Management
- [ ] Graduated daily loss response (2% → -25% size, 3.5% → -50%, 5% → shutdown)
- [ ] 5% max daily loss limit across all strategies
- [ ] Portfolio heat cap — never more than 15% of account at risk simultaneously

#### Notifications
- [ ] Daily 8am health report to Slack (uptime, trades, equity, loss used, component status)
- [ ] Trade executed alerts with full details
- [ ] Confluence detected alerts
- [ ] Daily 4pm market close digest
- [ ] Bot error alerts
- [ ] Truth Social trade alerts (🇺🇸🚀 emoji — distinguishable from chart trades)
- [ ] News trade alerts (📰🚀 emoji)

#### Data Layer
- [ ] Switch to Pandas DataFrames via Alpaca `.df` property
- [ ] Replace manual indicators with `pandas-ta` one-liners
- [ ] Add to requirements.txt: pandas, numpy, pandas-ta, scipy, matplotlib, seaborn
- [ ] Finnhub demoted to backup price feed + fundamentals only

#### Discovery Engine (Start in Parallel)
- [ ] Pull 5-10 years historical data from Alpaca
- [ ] Set up PostgreSQL on Railway (free tier)
- [ ] Automated backtester testing indicator combinations
- [ ] Walk-forward testing on out-of-sample data only
- [ ] SciPy p-value testing — only strategies with p < 0.05 considered
- [ ] Human approval gate — Discovery Engine NEVER auto-deploys
- [ ] Slack report when new strategy validated, you approve before deployment

**🎯 May 22 Goal:** Bot running 24/7 without crashing, Slack alerts firing, paper trades executing, Discovery Engine running in background

---

### 📅 Phase 2 — Strategy Discovery Engine (May → June 2026)
- [ ] Discovery Engine finding first validated strategies
- [ ] Walk-forward backtested, statistically significant (p < 0.05)
- [ ] First AI-discovered strategy plugged into trading bot
- [ ] New strategies start at 25% position size for first 50 trades
- [ ] Scale to full size only if live results match backtest within 15%
- [ ] Strategy changelog maintained in PostgreSQL
- [ ] Matplotlib equity curve charts generated
- [ ] Weekly Slack report with Seaborn correlation heatmaps
- [ ] EV tracking per strategy (positive EV required to stay active)

---

### 🤖 Phase 3 — Multi-Agent Expansion (June → August 2026)
- [ ] 5 scanner bots, 20 stocks each = 100 symbols total
- [ ] Dynamic symbol selection — top 100 S&P 500 by daily volume, refreshed weekly
- [ ] Minimum 5M average daily volume filter
- [ ] Redis message queue on Railway (~$5/mo)
- [ ] Head bot coordinating all signals
- [ ] Signal stacking — news + chart on same ticker within 30min = combined score
- [ ] Time of day multiplier (9:30-10:30am = 1.2x, 12-2pm = 0.7x, etc.)
- [ ] Sector sentiment detection (3+ same sector signals = sector hot flag)
- [ ] Market Regime Bot — ADX on SPY/QQQ (trending vs choppy)
- [ ] Earnings calendar filter — reduce size 75% within 48hrs of earnings
- [ ] VIX spike protection — VIX >35 → reduce all sizes 75%
- [ ] SPY 200 DMA bear market mode
- [ ] Grafana + Prometheus operations dashboard
- [ ] Sentry error monitoring integrated
- [ ] TA-Lib candlestick pattern recognition
- [ ] Reddit momentum bot as additional signal source

---

### 🧠 Phase 4 — Performance Brain + Go-Live (July → August 2026)
- [ ] Performance Brain tracking all metrics
- [ ] Win rate per strategy, symbol, time of day, market regime
- [ ] Average winner vs loser size tracked separately
- [ ] Day of week performance tracking
- [ ] Auto position size adjustment on hot/cold strategies
- [ ] PagerDuty phone alerts for critical failures
- [ ] Slack slash commands (/buy /sell /status /pause /resume)
- [ ] Notion API automated trade journal
- [ ] **Evaluate go-live criteria (see below)**
- [ ] Go live with $1,000-2,000 starting capital if criteria met

#### ✅ Go-Live Criteria (ALL must be true)
- [ ] Positive returns in 3 of 4 paper trading months
- [ ] Win rate consistently above 50%
- [ ] Max drawdown below 15% in any single month
- [ ] Bot running 30+ consecutive days without crashing
- [ ] Slack alerts firing accurately for every trade
- [ ] Discovery Engine validated 3+ new strategies
- [ ] Starting capital is money 100% comfortable losing entirely

---

### 📈 Phase 5 — Scale + Compound (September 2026+)
- [ ] Reinvest all profits back into trading account
- [ ] Add $500+/month from job income
- [ ] Discovery Engine continuously finding new edges
- [ ] scikit-learn ML patterns in strategy discovery
- [ ] Grok API for X/Twitter crypto sentiment (when available)
- [ ] Consider IBKR migration at $50k+ capital
- [ ] Alpaca Options strategies explored
- [ ] Webull sentiment as contrarian signal

---

### 💰 Phase 6 — Prop Firm Funding + Mid 6 Figures (2027-2031)

#### Step 1 — Build Verified Track Record (Month 12-18)
- [ ] Trade live 12-18 months with documented results
- [ ] Track: monthly return, max drawdown, win rate, Sharpe ratio
- [ ] Target: 5-10% monthly, drawdown <10%, win rate >50%
- [ ] Alpaca verified performance reports as proof

#### Step 2 — Prop Firm Evaluation (Year 2)
- [ ] Target firms: Apex Trader Funding, Topstep, The Funded Trader
- [ ] Tune bot for firm-specific rules before paying evaluation fee
- [ ] Pass evaluation → $150k-400k funded account
- [ ] Keep 80-90% of all profits

#### Step 3 — Private Investors (Year 2-3)
- [ ] 18 month track record → approach private investors
- [ ] Structure as profit sharing (you keep 20-30%)
- [ ] $200-500k managed capital at 30% return

#### Step 4 — Signal Subscriptions (Year 2+)
- [ ] Sell trade signals at $50-100/month per subscriber
- [ ] 100 subscribers = $5-10k/month additional income
- [ ] Runs from existing Slack notification system

#### Combined Income Target (Year 4-6)
| Source | Monthly | Annual |
|--------|---------|--------|
| Personal account ($100k @ 30%) | $2,500 | $30,000 |
| Prop firm ($300k @ 30%) | $6,750 | $81,000 |
| Private investors ($300k, 25% fee) | $1,875 | $22,500 |
| Signal subscriptions (100 @ $75) | $7,500 | $90,000 |
| **TOTAL** | **$18,625** | **$223,500** |

---

## 💻 Technology Stack

### Core Infrastructure
| Service | Purpose | Cost | Status |
|---------|---------|------|--------|
| Alpaca | Primary broker + data (.df for DataFrames) | $0 | ✅ Connected |
| Railway | Cloud hosting for all bots | $0-5/mo | ✅ Deployed |
| GitHub | Code storage + version control | $0 | ✅ Connected |
| Slack | All alerts and reports | $0 | ✅ Webhook ready |
| Antigravity | Primary AI coding tool | Free | ✅ Installed |
| Claude Code | Precision edits + large refactors | $20/mo | ✅ Installed |
| Alpaca App | View positions on phone | $0 | ⏳ Download |
| PostgreSQL (Railway) | Strategy DB + trade logs | $0 | ⏳ Phase 2 |
| Redis (Railway) | Scanner → Head bot queue | ~$5/mo | ⏳ Phase 3 |
| IBKR | Upgrade at $50k+ capital | $0 trading | ⏳ Phase 5+ |

### Monitoring & Reliability
| Service | Purpose | Cost | Status |
|---------|---------|------|--------|
| Uptime Robot | External ping every 5min | Free | ✅ Running |
| Sentry | Professional error monitoring | Free | ⏳ Phase 1 |
| PagerDuty | Phone call for critical failures | Free | ⏳ Before go-live |
| Grafana + Prometheus | Real time ops dashboard | Free | ⏳ Phase 3 |

### Data & Sentiment Sources
| Service | Purpose | Cost | Status |
|---------|---------|------|--------|
| Anthropic API | Claude scoring sentiment | ~$5/mo | ⏳ Phase 1 |
| Finnhub | Backup price + fundamentals ONLY | $0 | ✅ Integrated |
| Benzinga (via Alpaca) | Financial news — already free | $0 | ⏳ Phase 1 |
| SEC EDGAR API | Insider + hedge fund trades | Free | ⏳ Phase 2 |
| Unusual Whales | Options flow data | Free tier | ⏳ Phase 2 |
| Quiver Quantitative | Congressional trading data | Free tier | ⏳ Phase 2 |
| FRED API | Fed economic data + macro | Free | ⏳ Phase 3 |
| Polygon.io | Supplementary data + options flow | Free tier | ⏳ Phase 3 |
| Grok API (xAI) | X/Twitter crypto sentiment | TBD | ⏳ Phase 3 |
| Webull Sentiment | Retail long/short ratio (contrarian) | Free | ⏳ Phase 3 |

### Python Libraries
| Library | Purpose | Cost | Status |
|---------|---------|------|--------|
| Pandas | Core data manipulation | Free | ⏳ Phase 1 |
| NumPy | Fast vectorized math | Free | ⏳ Phase 1 |
| pandas-ta | All indicators in one line | Free | ⏳ Phase 1 |
| SciPy | Statistical validation (p-value) | Free | ⏳ Phase 2 |
| Matplotlib | Equity curve charts | Free | ⏳ Phase 2 |
| Seaborn | Correlation heatmaps + reports | Free | ⏳ Phase 3 |
| TA-Lib | Candlestick pattern recognition | Free | ⏳ Phase 3 |
| scikit-learn | ML pattern recognition | Free | ⏳ Phase 4+ |

---

## 💸 Monthly Cost by Phase

| Phase | When | Monthly Cost |
|-------|------|-------------|
| Now | Today | $0 (Antigravity is free) |
| Phase 1 | Next 2 weeks | $5-20 (Anthropic API only) |
| Phase 2 | Month 2-3 | $5-20 (same) |
| Phase 3 | Month 4-6 | $15-30 (add Redis + more API) |
| Full system | Month 6+ | $30-45/mo total |

> **Key:** Anthropic API is your only variable cost. Monitor at console.anthropic.com. Set $30/month budget alert.

---

## 🚨 Known Issues & Fix Plan

### Big Holes

#### ❌ No Proven Edge Yet — MOSTLY FIXABLE
- Require minimum 200 trades before trusting win rate
- Discovery Engine uses strict walk-forward backtesting only
- SciPy p-value < 0.05 required for any strategy deployment
- Paper trade 8-12 weeks minimum before conclusions

#### ❌ No Daily Health Report — FULLY FIXABLE (Build First)
- 8am EST automated Slack: uptime, trades, equity, loss %, component status
- Alert if no trade in 48hrs during market hours
- Alert if equity drops 2%+ in single day
- **This is the FIRST thing to build**

#### ❌ Discovery Engine Can Deploy Bad Strategies — FULLY FIXABLE
- Discovery Engine NEVER auto-deploys
- Slack report for manual review and approval
- New strategies start at 25% size for first 50 trades
- Scale to full size only if within 15% of backtested metrics

#### ⚠️ Black Swan Correlation Risk — ACCEPT + MANAGE
- No system is immune to black swan events — this is the nature of markets
- Never risk more than you can afford to lose entirely
- Keep emergency fund completely separate from trading capital
- VIX >35 → reduce all sizes 75%
- SPY drops 3%+ in one day → pause all new entries

#### ❌ Tax and Legal — FULLY MANAGEABLE
- Every trade is a taxable event — use TurboTax or TaxAct
- Set aside 25-30% of profits for taxes in separate account
- Do NOT manage others' money without financial/legal advice first

---

## 📋 Recovery Plan
> **Write these rules before going live. Never override them in the moment.**

| Trigger | Action |
|---------|--------|
| Down 5% in a week | Reduce all position sizes 30%, review logs |
| Down 10% in a month | Reduce 50%, pause Discovery Engine deployments |
| Down 20% in a month | Pause bot entirely, full Claude Code review |
| Down 30% from peak | Shut down live trading, return to paper |
| 3 consecutive losing months | Pause, rebuild strategy set from scratch |
| No health report for 24hrs | Fix health report before anything else |
| Single trade loses 10%+ | Emergency shutdown — position sizing bug |

---

## 📈 Capital Growth Projection (30% annual + $500/mo deposits)

| Year | Capital | 30% Return | Monthly Income |
|------|---------|-----------|---------------|
| Start | $1,500 | — | $0 |
| Year 1 | $7,950 | $450 | $199 |
| Year 2 | $16,335 | $2,385 | $408 |
| Year 3 | $27,236 | $4,901 | $681 |
| Year 5 | $59,829 | $12,422 | $1,496 |
| Year 7 | $107,000 | $23,450 | $2,675 |
| Year 10 | $230,000+ | $52,000 | $5,750+ |

> Does NOT include prop firm income, investor fees, or signal subscriptions — those are on top.

---

## ⚡ The 4 Rules

> **1. PATIENCE** — Let the system run long enough to get real data. A bad week is not a failed system.

> **2. DISCIPLINE** — Don't override the bot emotionally. Change rules based on data, not feelings.

> **3. ITERATION** — Every loss is data. Every underperforming strategy is information. Improve constantly.

> **4. HONESTY** — Be willing to admit when something isn't working and change it. Sunk cost thinking kills accounts.

---

## 📅 30-Day Sprint (April 22 → May 22, 2026)

### Week 1 (April 22-29) — Fix, Deploy, Confirm
- [ ] Let Antigravity fix the 4 bugs it identified
- [ ] Confirm Railway deploys successfully after bug fixes
- [ ] Check Slack for bot started alert
- [ ] Paste big refactor prompt into Antigravity (break into 3-4 sessions)
- [ ] Add ANTHROPIC_API_KEY to Railway variables
- [ ] Start Discovery Engine build in Antigravity

### Week 2 (April 29 → May 6) — Stabilize
- [ ] Monitor Slack daily — trade alerts firing?
- [ ] Check Alpaca dashboard for paper trade history
- [ ] Confirm news sentiment and Truth Social monitoring active
- [ ] Check Anthropic API usage in console (cost tracking)

### Week 3 (May 6-13) — Refine
- [ ] Review 2 weeks of paper trading data
- [ ] Discovery Engine — first backtests completing?
- [ ] Tune signal thresholds if too noisy
- [ ] Note which symbols generating best signals

### Week 4 (May 13-22) — Polish
- [ ] First AI-discovered strategy integrated
- [ ] Full month paper trading review
- [ ] May 22 checkpoint — did you hit all goals?
- [ ] Plan Phase 3 multi-agent expansion

### 🎯 The One Metric That Matters This Month
**STABILITY** — Is the bot running every day without crashing, executing trades, and sending accurate Slack alerts? If yes on May 22 — you are ahead of schedule.

### Daily 5-Minute Morning Habit
- [ ] Check Railway logs — any errors?
- [ ] Check Slack #trading — trades fired?
- [ ] Check Alpaca app — open positions, P&L
- [ ] Note anything unusual

---

## 🛠️ Key Prompts Saved

### Big Refactor Prompt (Paste into Antigravity — Break into 3-4 Sessions)

**Session 1 — Bug Fixes:**
> Fix the four bugs identified: (1) execute_trade signature mismatch across strategies — 5 vs 6 params, fix root cause in bot.py. (2) f-string syntax error in swing_strategy.py line 134. (3) mean_reversion.py mutating shared DataFrame — work on a copy. (4) incomplete requirements.txt — add requests, numpy, pytz, pandas-ta, scipy, matplotlib, seaborn. Also add a Flask health endpoint on port 8501 returning JSON status for Uptime Robot.

**Session 2 — Hybrid System:**
> Refactor into hybrid two-part system. Part 1: crypto scalp using CryptoDataStream websocket for BTC/USD and ETH/USD, keep SMBStrategy. Part 2: swing strategy using 50 EMA/200 EMA + MACD + RSI, polling at 10:30am EST daily on MSFT, AAPL, NVDA, AMZN, SPY, QQQ. Run both concurrently via asyncio. Add one-position-per-symbol rule. Add graduated daily loss limit (2% → -25% size, 3.5% → -50%, 5% → shutdown). Add symbol cooldown 2 hours after any loss. Add trailing stop activating at 3% profit. Use pandas-ta for all indicator calculations. Replace all manual indicator math.

**Session 3 — Sentiment Layer:**
> Add Benzinga news strategy polling every 60 seconds at market open, 10 minutes midday, 15 minutes after hours. Claude API scores each headline — score × confidence / 10. Below 7 ignore, 7-12 Slack alert, 13+ check chart confirmation then trade. Add Truth Social RSS monitoring every 60 seconds. Wait 60 seconds after post detected, check if price still 1%+ above baseline, enter with 2% stop and 8% take profit at 50% position size. Skip if already reversed. Add all Slack notifications with emoji differentiation. Add SLACK_WEBHOOK_URL and ANTHROPIC_API_KEY from environment variables.

**Session 4 — Discovery Engine:**
> Build Strategy Discovery Engine as separate Railway service. Pull 5-10 years daily bars from Alpaca for top 20 S&P 500 symbols by volume. Automated backtester tests indicator combinations. Walk-forward testing only — train on old data, validate on new. SciPy p-value testing — only p < 0.05 passes. Store validated strategies in PostgreSQL. Send Slack report for human approval before any deployment. New strategies deploy at 25% position size for first 50 trades.

### Flask Health Endpoint
```python
from flask import Flask, jsonify
import threading, time

app = Flask(__name__)
start_time = time.time()

@app.route('/health')
def health():
    return jsonify({
        "status": "running",
        "uptime_seconds": int(time.time() - start_time),
        "paper_trading": True
    })

def run_health_server():
    app.run(host='0.0.0.0', port=8501)

threading.Thread(target=run_health_server, daemon=True).start()
```

---

*Last updated: April 30, 2026 — Update this file whenever the plan changes*
