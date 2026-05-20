// ── SPA page content ──────────────────────────────────────────────────────────
// Each key is the innerHTML swapped into #page-content on navigation.

const PAGES = {

// ─────────────────────────────────────────────────────────────────────────────
strategy: `
<div class="spa-page">

  <!-- Gate chain header -->
  <div class="spa-section">
    <div class="section-inner animate-in">
      <div class="section-tag">The filter chain</div>
      <h2>15 gates. Every trade. No exceptions.</h2>
      <p class="page-subtitle">Before any order reaches Alpaca, it passes through 15 independent
      filters in sequence — cheapest checks first, most expensive last. A single failure blocks
      the trade.</p>
    </div>
  </div>

  <!-- Gate chain list -->
  <div class="spa-section spa-section--flush-top">
    <div class="section-inner">
      <div class="strategy-gate-list">

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 01</span>
            <span class="sgate-name">Daily halt check</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Checks if trading has been manually halted for the day via
          Slack /pause command or automatic risk trigger.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 02</span>
            <span class="sgate-name">Hold signal filter</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Strategy's generate_signals() returned hold — no trade setup
          detected in current bar data.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 03</span>
            <span class="sgate-name">One position per symbol</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Alpaca confirms an open position already exists for this symbol.
          No pyramiding.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 04</span>
            <span class="sgate-name">Portfolio heat cap</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Total portfolio risk exposure across all open positions exceeds
          the maximum allowed heat (15% of equity). New positions blocked until heat drops.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 05</span>
            <span class="sgate-name">Correlation guard</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Incoming symbol is highly correlated (&gt;0.70) with 2+ existing
          open positions. Prevents doubling up on the same macro bet.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 06</span>
            <span class="sgate-name">Short interest signal</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">FINRA short volume ratio &gt;65% with price falling &gt;1% —
          heavily shorted stock under active selling pressure. Vetoed.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 07</span>
            <span class="sgate-name">Pre-execute hook</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Fundamental check + LLM bull/bear debate via OpenRouter.
          3 AI calls analyze the trade from opposing perspectives before a verdict is reached.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 08</span>
            <span class="sgate-name">Circuit breaker</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">This strategy has lost &gt;5% of capital in the last 10 days.
          Auto-paused until the drawdown window clears.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 09</span>
            <span class="sgate-name">Confluence check</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Sector alert cooldown active — too many recent trades in
          the same sector. Prevents sector concentration.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 10</span>
            <span class="sgate-name">Signal type determination</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Final signal classification assigned: swing_long, scalp_long,
          swing_bb, etc. Required for Kelly sizing and DB logging.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 11</span>
            <span class="sgate-name">Performance brain</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Position size multiplier applied based on recent strategy
          performance. Underperforming strategies size down automatically.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 12</span>
            <span class="sgate-name">Kelly sizing</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Half-Kelly position size calculated from historical win rate
          and payoff ratio pulled from signal_outcomes. Hard cap at 10% per trade.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 13</span>
            <span class="sgate-name">Buying power check</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Final Alpaca account buying power confirmed before submission.
          Prevents orders that would exceed available capital.</p>
        </div>

        <div class="sgate-item">
          <div class="sgate-header">
            <span class="gate-num">GATE 14</span>
            <span class="sgate-name">Order submission</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Market or limit order submitted to Alpaca paper/live API as a
          bracket order with take-profit and stop-loss legs attached.</p>
        </div>

        <div class="sgate-item sgate-last">
          <div class="sgate-header">
            <span class="gate-num">GATE 15</span>
            <span class="sgate-name">Trade logging</span>
            <span class="gate-check">✓</span>
          </div>
          <p class="sgate-desc">Entry logged to PostgreSQL signal_outcomes table with all
          signal metadata for performance analysis and Kelly recalibration.</p>
        </div>

      </div>
    </div>
  </div>

  <!-- Strategy classes -->
  <div class="spa-section">
    <div class="section-inner animate-in">
      <div class="section-tag">Strategy classes</div>
      <h2>Three strategies, one unified engine</h2>
    </div>
    <div class="section-inner">
      <div class="strategy-detail-grid animate-in">

        <div class="strategy-detail-card">
          <h3>SwingStrategy</h3>
          <p class="detail-desc">Multi-day momentum trades on large-cap US equities. Enters
          when a short-term trend is emerging with confirmation from multiple independent signals.</p>
          <div class="detail-rows">
            <div class="detail-row"><span class="detail-label">Symbols</span><span class="detail-value">JPM, SPY, COST, BRK.B, PG, V</span></div>
            <div class="detail-row"><span class="detail-label">Exit</span><span class="detail-value">ATR-based trailing stop · 3:1 take profit</span></div>
            <div class="detail-row"><span class="detail-label">Regime</span><span class="detail-value">Trending markets only (Hurst H &gt; 0.6)</span></div>
          </div>
          <div class="signal-stack-label">Signal stack (in order)</div>
          <ol class="signal-stack">
            <li><span class="sig-num">1.</span><span class="sig-text">EMA crossover — short MA crosses above long MA (trend structure)</span></li>
            <li><span class="sig-num">2.</span><span class="sig-text">MACD crossover — momentum confirmation above signal line</span></li>
            <li><span class="sig-num">3.</span><span class="sig-text">RSI gate — entry not overbought or oversold</span></li>
            <li><span class="sig-num">4.</span><span class="sig-text">Kalman noise gate — noise_ratio &lt; 0.4 (movement is signal, not noise)</span></li>
            <li><span class="sig-num">5.</span><span class="sig-text">Hurst regime gate — H &gt; 0.6 (market demonstrates statistical persistence)</span></li>
          </ol>
        </div>

        <div class="strategy-detail-card">
          <h3>SMBStrategy</h3>
          <p class="detail-desc">Short-duration mean reversion trades anchored to VWAP. Fades
          moves away from institutional fair value when confirmation signals align.</p>
          <div class="detail-rows">
            <div class="detail-row"><span class="detail-label">Exit</span><span class="detail-value">Mean reversion to VWAP or half-life holding period</span></div>
            <div class="detail-row"><span class="detail-label">Regime</span><span class="detail-value">Intraday mean reversion</span></div>
            <div class="detail-row"><span class="detail-label">Style</span><span class="detail-value">Short-duration fade trades</span></div>
          </div>
          <div class="signal-stack-label">Signal stack (in order)</div>
          <ol class="signal-stack">
            <li><span class="sig-num">1.</span><span class="sig-text">Kalman/VWAP crossover — Kalman trend crosses ta.vwap() with k_signal == ±1</span></li>
            <li><span class="sig-num">2.</span><span class="sig-text">AnchoredVWAP confirmation — price ≥ 0.3% from rolling VWAP AND volume ≥ 1.2× average</span></li>
          </ol>
        </div>

        <div class="strategy-detail-card">
          <h3>Discovery-Optimized</h3>
          <p class="detail-desc">Parameters auto-updated weekly by the Strategy Discovery Engine.
          Walk-forward validated across bull, bear, and high-volatility regimes before deployment.</p>
          <div class="detail-rows">
            <div class="detail-row"><span class="detail-label">Symbols</span><span class="detail-value">JPM, SPY, COST, BRK.B, PG</span></div>
            <div class="detail-row"><span class="detail-label">Updates</span><span class="detail-value">Every Friday at 4:30 PM EST</span></div>
            <div class="detail-row"><span class="detail-label">Regime</span><span class="detail-value">Adaptive — best type per current regime</span></div>
          </div>
          <div class="signal-stack-label">How it works</div>
          <ol class="signal-stack">
            <li><span class="sig-num">1.</span><span class="sig-text">discovery_engine_v2 runs 1,215+ parameter combinations per symbol</span></li>
            <li><span class="sig-num">2.</span><span class="sig-text">Walk-forward validation tags bull_sharpe, bear_sharpe, high_vol_sharpe separately</span></li>
            <li><span class="sig-num">3.</span><span class="sig-text">Best performer per regime is approved and deployed via regime_adapter.py</span></li>
          </ol>
        </div>

      </div>
    </div>
  </div>

  <!-- Signal processing layer -->
  <div class="spa-section">
    <div class="section-inner animate-in">
      <div class="section-tag">Signal intelligence</div>
      <h2>Adaptive signals that evolve with the market</h2>
    </div>
    <div class="section-inner">
      <div class="signal-proc-grid animate-in">

        <div class="sig-proc-card">
          <div class="prob-tag">The problem</div>
          <h3>Kalman Filter</h3>
          <p class="prob-text">Static moving averages treat every price equally. A 20-day MA weights
          yesterday's price the same as one from 3 weeks ago.</p>
          <p class="what-text">Maintains a confidence estimate about the true underlying price.
          When uncertainty is high it trusts new data more. When confident it barely moves. The result
          is a trend line that adapts its responsiveness in real time.</p>
          <div class="key-param">
            <span class="key-param-label">Key gate</span>
            noise_ratio &lt; 0.4 required before SwingStrategy fires
          </div>
          <div class="sig-outputs">
            <span>trend</span><span>slope</span><span>noise_ratio</span><span>signal ∈ {−1, 0, +1}</span>
          </div>
        </div>

        <div class="sig-proc-card">
          <div class="prob-tag">The problem</div>
          <h3>Hurst Exponent</h3>
          <p class="prob-text">Momentum strategies lose money in mean-reverting markets. Mean reversion
          strategies lose in trending markets. How do you know which regime you're in?</p>
          <p class="what-text">R/S analysis on the last 60 bars produces a single number H.
          H &gt; 0.6 = trending (run momentum). H &lt; 0.4 = mean-reverting (run reversion).
          H ≈ 0.5 = random walk (stay flat).</p>
          <div class="key-param">
            <span class="key-param-label">Key gate</span>
            SwingStrategy only fires when H &gt; 0.6 — never fights the regime
          </div>
        </div>

        <div class="sig-proc-card">
          <div class="prob-tag">The problem</div>
          <h3>Half-Life of Mean Reversion</h3>
          <p class="prob-text">How long should a mean reversion trade be held? Too short and you
          exit before reversion completes. Too long and you give back gains.</p>
          <p class="what-text">OLS regression on the lagged price series estimates how many bars
          it takes for 50% of a deviation to revert. This becomes the suggested holding period
          for SMBStrategy exits. Only fires when β ∈ (−1, 0) — confirming the series is
          actually mean-reverting.</p>
        </div>

        <div class="sig-proc-card">
          <div class="prob-tag">The problem</div>
          <h3>Correlation Guard</h3>
          <p class="prob-text">Running 6 SwingStrategy instances simultaneously means all 6 could
          trigger in the same direction during a broad rally — concentrating 60% of capital
          into the same macro bet.</p>
          <p class="what-text">Checks Pearson correlation between the incoming symbol and all
          open positions over 60 days before execution. Blocks if average correlation &gt; 0.70
          or if 2+ positions are already highly correlated. Also tracks sector concentration —
          blocks if 3+ positions are in the same GICS sector.</p>
        </div>

      </div>
    </div>
  </div>

  <footer class="spa-footer">
    <div class="section-inner footer-inner">
      <span class="footer-logo">BLITHEBOT</span>
      <span class="footer-note">Paper trading · Summer 2026 live launch</span>
    </div>
  </footer>

</div>
`,

// ─────────────────────────────────────────────────────────────────────────────
research: `
<div class="spa-page">

  <div class="spa-section">
    <div class="section-inner animate-in">
      <div class="section-tag">Quantitative methods</div>
      <h2>The quantitative foundation</h2>
      <p class="page-subtitle">Written for someone with a finance or math background who wants to
      understand why these methods work — not just what they do.</p>
    </div>
  </div>

  <div class="spa-section">
    <div class="section-inner">

      <div class="research-section animate-in">
        <div class="research-num">01</div>
        <h3>Why adaptive signals beat static indicators</h3>
        <p>Most retail algorithms use fixed-parameter indicators — a 20-day moving average, a
        14-period RSI, a standard MACD. These work in certain regimes and fail in others. The
        problem is that markets are non-stationary: the statistical properties of price series
        change over time.</p>
        <p>A trending market has autocorrelated returns. A mean-reverting market has
        anti-autocorrelated returns. A random-walk market has neither. Applying a momentum
        indicator to a mean-reverting market — or vice versa — is not just suboptimal, it
        actively loses money because the signal fires in the wrong direction.</p>
        <p>The solution is signal processing methods borrowed from control theory and engineering
        that adapt to current market conditions. The Kalman filter was developed for aerospace
        navigation. The Hurst exponent was developed for hydrology. Both turn out to be
        more effective market tools than any fixed-window indicator, because they
        estimate the current state of the system rather than averaging its past.</p>
      </div>

      <div class="research-section animate-in">
        <div class="research-num">02</div>
        <h3>The Kelly Criterion</h3>
        <p>Kelly sizing answers the question every systematic trader faces: given a known edge,
        how much capital should each trade risk? Bet too little and you under-capitalize the
        edge. Bet too much and variance destroys the account before the edge can compound.</p>
        <div class="formula-box">
          <div class="formula-main">f* = (p · b − q) / b</div>
          <div class="formula-vars">
            <span>p = win rate (fraction of winning trades)</span>
            <span>b = payoff ratio (average win ÷ average loss)</span>
            <span>q = 1 − p (loss probability)</span>
            <span>f* = optimal fraction of capital to risk per trade</span>
          </div>
        </div>
        <p>Full Kelly maximizes long-run geometric growth but produces extreme short-term drawdowns —
        a mathematically optimal but psychologically and practically brutal path. Half-Kelly (f*/2)
        sacrifices approximately 25% of maximum long-run growth in exchange for roughly 75%
        reduction in variance. For a systematic strategy with limited track record, this
        trade-off is clearly correct.</p>
        <p>BlitheBot's implementation requires a minimum of 20 closed trades per strategy type
        before Kelly activates. Below that threshold, a conservative 2% fixed size is used.
        Kelly fraction updates automatically as trade history accumulates in signal_outcomes,
        recalculated every 60 minutes with a 90-day lookback. Hard cap at 10% regardless of
        computed fraction.</p>
      </div>

      <div class="research-section animate-in">
        <div class="research-num">03</div>
        <h3>Walk-Forward Validation vs Backtesting</h3>
        <p>Any parameter set can be optimized to look excellent on historical data. This is the
        fundamental problem with backtesting: given enough parameters and enough time, you can
        always find a combination that fits the past perfectly. The question is whether it
        generalizes to data it has never seen.</p>
        <p>Walk-forward validation solves this by enforcing a strict out-of-sample test at every
        step. Train on window 1, test on window 2 (out of sample). Retrain on windows 1+2,
        test on window 3. Repeat across the full data range. Only parameter sets that
        produce statistically significant positive results across multiple independent out-of-sample
        windows are approved.</p>
        <p>BlitheBot's discovery engine adds a second layer: regime tagging. Each walk-forward
        result is tagged with separate bull_sharpe, bear_sharpe, and high_vol_sharpe values.
        The strategy deployed in a trending market may have entirely different parameters
        from the one deployed in a high-volatility environment. RegimeAdapter reads the
        current SPY regime and selects the appropriate approved parameter set automatically.</p>
      </div>

      <div class="research-section animate-in">
        <div class="research-num">04</div>
        <h3>Genetic Programming for Indicator Discovery</h3>
        <p>Instead of hand-crafting indicators, the genetic engine evolves them. Building blocks
        (price transforms, rolling statistics, crossover operators, logical gates) are combined
        into expression trees and evaluated for predictive power using Information Coefficient —
        the Spearman rank correlation between the indicator value and forward returns.</p>
        <div class="formula-box">
          <div class="formula-main">IC = Spearman ρ(indicator_t, return_{t+1})</div>
          <div class="formula-vars">
            <span>IC &gt; 0.05 consistently across walk-forward folds = graduation threshold</span>
            <span>IC &gt; 0.10 = exceptional (most professional quant strategies target this)</span>
            <span>Most evolved indicators fail — only top survivors are deployed</span>
          </div>
        </div>
        <p>The evolution loop runs a population of 50 expression trees over 20 generations,
        applying mutation and crossover operators each generation. Runs Saturday nights after
        Friday's parquet cache is populated. Graduated indicators are stored in PostgreSQL
        and become candidates for the Discovery Strategy's signal stack.</p>
        <p>The value of genetic programming is that it searches a space of indicators too large
        to enumerate manually. The combination of rolling standard deviation of a wavelet-denoised
        price, lagged by 3 bars, cross-referenced against volume-weighted momentum — this kind
        of composite indicator would take weeks to test by hand. The GP engine evaluates
        thousands of such combinations overnight.</p>
      </div>

      <div class="research-section animate-in">
        <div class="research-num">05</div>
        <h3>Alternative Data Edge</h3>
        <p>Price and volume are available to every participant. The edge in alternative data
        comes from signals that most retail systems never see — not because the data is
        secret, but because parsing and acting on it correctly requires infrastructure most
        retail algos don't have.</p>
        <div class="alt-data-cards">
          <div class="alt-data-card">
            <div class="alt-data-source">SEC EDGAR Form 4</div>
            <p>Corporate insiders — executives, directors, 10%+ shareholders — must report
            open-market transactions within 2 business days. Clusters of insider buying before
            a significant price move is a documented anomaly in the academic literature.
            BlitheBot monitors the Form 4 RSS feed every 30 minutes, scores each filing by
            transaction size and insider seniority, and fires auto-trade signals on
            $1M+ insider buys.</p>
          </div>
          <div class="alt-data-card">
            <div class="alt-data-source">Congressional Trading (Quiver Quantitative)</div>
            <p>Members of Congress have historically generated above-market returns. Whether
            from information advantage, constituent-serving trades, or coincidence, their
            disclosed trades are a tracked signal. BlitheBot scores congressional disclosures
            by transaction size, committee membership (a 1.3× multiplier for relevant
            committee chairs), and recency, with a 7-day staleness cutoff.</p>
          </div>
          <div class="alt-data-card">
            <div class="alt-data-source">FINRA Short Volume Ratio</div>
            <p>Daily short sale volume as a fraction of total volume, from FINRA's CNMSshvol
            files. A ratio above 65% with a declining price signals active institutional
            short selling — a veto on new long positions. The same ratio above 65% with a
            rising price is a squeeze candidate signal, noted as a boost to any existing
            long signals.</p>
          </div>
        </div>
      </div>

    </div>
  </div>

  <footer class="spa-footer">
    <div class="section-inner footer-inner">
      <span class="footer-logo">BLITHEBOT</span>
      <span class="footer-note">Paper trading · Summer 2026 live launch</span>
    </div>
  </footer>

</div>
`,

// ─────────────────────────────────────────────────────────────────────────────
performance: `
<div class="spa-page">

  <div class="spa-section">
    <div class="section-inner animate-in">
      <div class="section-tag">Track record</div>
      <h2>Performance</h2>
      <p class="page-subtitle">Paper trading since May 2026. Live launch target: Summer 2026.</p>
    </div>
    <div class="section-inner animate-in">
      <div class="perf-notice">
        <strong>Paper trading mode.</strong> Real capital has not been deployed. Stats below
        reflect paper trading results and will be updated as history accumulates.
        Live performance data will replace these placeholders at launch.
      </div>

      <div class="perf-stats-grid">
        <div class="perf-stat-card">
          <span class="perf-stat-label">Total Trades</span>
          <span class="perf-stat-value">—</span>
          <span class="perf-stat-note">accumulating</span>
        </div>
        <div class="perf-stat-card">
          <span class="perf-stat-label">Win Rate</span>
          <span class="perf-stat-value">—</span>
          <span class="perf-stat-note">accumulating</span>
        </div>
        <div class="perf-stat-card">
          <span class="perf-stat-label">Sharpe Ratio</span>
          <span class="perf-stat-value">—</span>
          <span class="perf-stat-note">accumulating</span>
        </div>
        <div class="perf-stat-card">
          <span class="perf-stat-label">Max Drawdown</span>
          <span class="perf-stat-value">—</span>
          <span class="perf-stat-note">accumulating</span>
        </div>
      </div>
    </div>
  </div>

  <div class="spa-section">
    <div class="section-inner animate-in">
      <div class="section-tag">Coming soon</div>
      <h2>Metrics that will be tracked</h2>
      <p class="page-subtitle">Once sufficient trade history accumulates, these views will be
      populated from the live signal_outcomes PostgreSQL table.</p>
      <div class="future-metrics-grid">
        <div class="future-metric">
          <div class="future-metric-title">Equity curve</div>
          <p>Cumulative P&amp;L over time, plotted as an equity curve against SPY benchmark.</p>
        </div>
        <div class="future-metric">
          <div class="future-metric-title">Win rate by strategy</div>
          <p>Separate win rates for SwingStrategy, SMBStrategy, and Discovery-Optimized trades.</p>
        </div>
        <div class="future-metric">
          <div class="future-metric-title">Win rate by regime</div>
          <p>Performance segmented by market regime: trending vs mean-reverting vs high-volatility.</p>
        </div>
        <div class="future-metric">
          <div class="future-metric-title">Kelly fraction evolution</div>
          <p>How position sizing adapts as historical win rate and payoff ratio accumulate per strategy.</p>
        </div>
        <div class="future-metric">
          <div class="future-metric-title">Gate block breakdown</div>
          <p>Which gate blocks the most trades. Useful for diagnosing over-filtering or under-filtering.</p>
        </div>
        <div class="future-metric">
          <div class="future-metric-title">Top symbols</div>
          <p>Best and worst performing symbols by risk-adjusted return across the full history.</p>
        </div>
      </div>
      <p class="perf-timeline-note">Check back in 4–6 weeks for meaningful paper trading data.
      Live performance data will be added at launch.</p>
    </div>
  </div>

  <footer class="spa-footer">
    <div class="section-inner footer-inner">
      <span class="footer-logo">BLITHEBOT</span>
      <span class="footer-note">Paper trading · Summer 2026 live launch</span>
    </div>
  </footer>

</div>
`,

// ─────────────────────────────────────────────────────────────────────────────
dashboard: `
<div class="spa-page">

  <div class="spa-section">
    <div class="section-inner animate-in">
      <div class="section-tag">Live monitoring</div>
      <h2>Trading Dashboard</h2>
      <p class="page-subtitle">Real-time monitoring of all active strategies, positions, and
      signal intelligence. The mockup below reflects the live Streamlit dashboard layout.</p>
    </div>
  </div>

  <div class="spa-section spa-section--flush-top">
    <div class="section-inner animate-in">

      <!-- Row 1: Account + Strategy Health -->
      <div class="dashboard-grid">

        <!-- Account overview -->
        <div class="dash-panel">
          <div class="dash-panel-title">Account Overview</div>
          <div class="account-stats">
            <div class="account-stat">
              <span class="account-label">Equity</span>
              <span class="account-value">$98,806.38</span>
            </div>
            <div class="account-stat">
              <span class="account-label">Buying Power</span>
              <span class="account-value">$197,612.76</span>
            </div>
            <div class="account-stat">
              <span class="account-label">Mode</span>
              <span class="status-chip chip-paper">PAPER TRADING</span>
            </div>
            <div class="account-stat">
              <span class="account-label">Status</span>
              <span class="status-chip chip-active">ACTIVE</span>
            </div>
            <div class="account-stat">
              <span class="account-label">Open Positions</span>
              <span class="account-value">2</span>
            </div>
          </div>
        </div>

        <!-- Strategy health -->
        <div class="dash-panel">
          <div class="dash-panel-title">Strategy Health</div>
          <table class="dash-table">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Status</th>
                <th>H Value</th>
                <th>Kalman</th>
                <th>Kelly f</th>
                <th>7d Win</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td class="td-sym">COST Swing</td>
                <td class="td-active">✓ Active</td>
                <td class="td-mono">0.71</td>
                <td class="td-mono">0.28</td>
                <td class="td-mono">4.9%</td>
                <td class="td-mono td-green">62%</td>
              </tr>
              <tr>
                <td class="td-sym">JPM Swing</td>
                <td class="td-active">✓ Active</td>
                <td class="td-mono">0.68</td>
                <td class="td-mono">0.31</td>
                <td class="td-mono">3.8%</td>
                <td class="td-mono td-green">58%</td>
              </tr>
              <tr>
                <td class="td-sym">SMB Late</td>
                <td class="td-active">✓ Active</td>
                <td class="td-mono td-muted">—</td>
                <td class="td-mono">0.22</td>
                <td class="td-mono">2.0%</td>
                <td class="td-mono td-muted">—</td>
              </tr>
              <tr>
                <td class="td-sym">SPY Swing</td>
                <td class="td-active">✓ Active</td>
                <td class="td-mono">0.74</td>
                <td class="td-mono">0.19</td>
                <td class="td-mono">5.2%</td>
                <td class="td-mono td-green">65%</td>
              </tr>
            </tbody>
          </table>
        </div>

      </div>

      <!-- Row 2: Signals + Correlation -->
      <div class="dashboard-grid" style="margin-top:1.5rem">

        <!-- Recent signals -->
        <div class="dash-panel">
          <div class="dash-panel-title">Recent Signals</div>
          <div class="signal-rows">
            <div class="signal-row sig-green">
              <span class="sig-sym">COST</span>
              <span class="sig-strat">Swing</span>
              <span class="sig-action sig-buy-text">↑ BUY</span>
              <span class="sig-price">$95.40</span>
              <span class="sig-meta">noise=0.28 H=0.71 Kelly=4.9%</span>
              <span class="sig-status">Executed</span>
            </div>
            <div class="signal-row sig-green">
              <span class="sig-sym">SPY</span>
              <span class="sig-strat">Swing</span>
              <span class="sig-action sig-buy-text">↑ BUY</span>
              <span class="sig-price">$518.20</span>
              <span class="sig-meta">noise=0.19 H=0.74 Kelly=5.2%</span>
              <span class="sig-status">Executed</span>
            </div>
            <div class="signal-row sig-red">
              <span class="sig-sym">JPM</span>
              <span class="sig-strat">Swing</span>
              <span class="sig-action sig-blocked-text">BUY blocked</span>
              <span class="sig-price"></span>
              <span class="sig-meta">Circuit breaker active</span>
              <span class="sig-status sig-status-blocked">Blocked</span>
            </div>
            <div class="signal-row sig-yellow">
              <span class="sig-sym">PG</span>
              <span class="sig-strat">Swing</span>
              <span class="sig-action sig-blocked-text">BUY blocked</span>
              <span class="sig-price"></span>
              <span class="sig-meta">Correlation guard: 2 correlated positions</span>
              <span class="sig-status sig-status-warn">Blocked</span>
            </div>
            <div class="signal-row sig-green">
              <span class="sig-sym">COST</span>
              <span class="sig-strat">SMB</span>
              <span class="sig-action sig-sell-text">↓ SELL</span>
              <span class="sig-price">$98.80</span>
              <span class="sig-meta">VWAP dist=0.4%</span>
              <span class="sig-status">Executed</span>
            </div>
          </div>
        </div>

        <!-- Correlation matrix -->
        <div class="dash-panel">
          <div class="dash-panel-title">60-Day Correlation Matrix</div>
          <div class="corr-wrapper">
            <div class="corr-matrix">
              <div class="corr-corner"></div>
              <div class="corr-head">JPM</div>
              <div class="corr-head">SPY</div>
              <div class="corr-head">COST</div>
              <div class="corr-head">BRK.B</div>
              <div class="corr-head">PG</div>
              <div class="corr-head">V</div>

              <div class="corr-head">JPM</div>
              <div class="corr-cell corr-diag">1.00</div>
              <div class="corr-cell corr-amber">0.71</div>
              <div class="corr-cell corr-amber">0.54</div>
              <div class="corr-cell corr-low">0.48</div>
              <div class="corr-cell corr-low">0.42</div>
              <div class="corr-cell corr-rose">0.78</div>

              <div class="corr-head">SPY</div>
              <div class="corr-cell corr-amber">0.71</div>
              <div class="corr-cell corr-diag">1.00</div>
              <div class="corr-cell corr-rose">0.82</div>
              <div class="corr-cell corr-amber">0.61</div>
              <div class="corr-cell corr-low">0.39</div>
              <div class="corr-cell corr-amber">0.68</div>

              <div class="corr-head">COST</div>
              <div class="corr-cell corr-amber">0.54</div>
              <div class="corr-cell corr-rose">0.82</div>
              <div class="corr-cell corr-diag">1.00</div>
              <div class="corr-cell corr-amber">0.52</div>
              <div class="corr-cell corr-low">0.35</div>
              <div class="corr-cell corr-amber">0.61</div>

              <div class="corr-head">BRK.B</div>
              <div class="corr-cell corr-low">0.48</div>
              <div class="corr-cell corr-amber">0.61</div>
              <div class="corr-cell corr-amber">0.52</div>
              <div class="corr-cell corr-diag">1.00</div>
              <div class="corr-cell corr-low">0.44</div>
              <div class="corr-cell corr-amber">0.55</div>

              <div class="corr-head">PG</div>
              <div class="corr-cell corr-low">0.42</div>
              <div class="corr-cell corr-low">0.39</div>
              <div class="corr-cell corr-low">0.35</div>
              <div class="corr-cell corr-low">0.44</div>
              <div class="corr-cell corr-diag">1.00</div>
              <div class="corr-cell corr-low">0.38</div>

              <div class="corr-head">V</div>
              <div class="corr-cell corr-rose">0.78</div>
              <div class="corr-cell corr-amber">0.68</div>
              <div class="corr-cell corr-amber">0.61</div>
              <div class="corr-cell corr-amber">0.55</div>
              <div class="corr-cell corr-low">0.38</div>
              <div class="corr-cell corr-diag">1.00</div>
            </div>
          </div>
          <div class="corr-legend">
            <span class="corr-legend-item"><span class="corr-swatch corr-rose"></span>&gt;0.75 high</span>
            <span class="corr-legend-item"><span class="corr-swatch corr-amber"></span>&gt;0.50 medium</span>
            <span class="corr-legend-item"><span class="corr-swatch corr-low"></span>low</span>
          </div>
        </div>

      </div>

      <!-- CTA -->
      <div class="dash-cta animate-in">
        <a href="#" class="btn-primary">View Live Dashboard →</a>
        <p class="dash-cta-note">Live dashboard available when Railway service is active</p>
      </div>

    </div>
  </div>

  <footer class="spa-footer">
    <div class="section-inner footer-inner">
      <span class="footer-logo">BLITHEBOT</span>
      <span class="footer-note">Paper trading · Summer 2026 live launch</span>
    </div>
  </footer>

</div>
`

}; // end PAGES
