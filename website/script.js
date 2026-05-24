// ── Perlin noise ──────────────────────────────────────────────────────────────
function fade(t) { return t * t * t * (t * (t * 6 - 15) + 10); }
function lerp(t, a, b) { return a + t * (b - a); }
function grad(hash, x, y, z) {
  const h = hash & 15;
  const u = h < 8 ? x : y, v = h < 4 ? y : (h === 12 || h === 14 ? x : z);
  return ((h & 1) ? -u : u) + ((h & 2) ? -v : v);
}
const perm = Array.from({ length: 256 }, (_, i) => i).sort(() => Math.random() - 0.5);
const p = [...perm, ...perm];

function noise(x, y, z) {
  const X = Math.floor(x) & 255;
  const Y = Math.floor(y) & 255;
  x -= Math.floor(x); y -= Math.floor(y);
  const u = fade(x), v = fade(y);
  const a = p[X] + Y, b = p[X + 1] + Y;
  return lerp(v,
    lerp(u, grad(p[a],     x,     y,     z), grad(p[b],     x - 1, y,     z)),
    lerp(u, grad(p[a + 1], x,     y - 1, z), grad(p[b + 1], x - 1, y - 1, z))
  );
}

// ── Canvas references ─────────────────────────────────────────────────────────
const canvasGrid = document.getElementById('canvas-grid');
const canvasCrt  = document.getElementById('canvas-crt');
const canvasDots = document.getElementById('canvas-dots');
const ctxGrid = canvasGrid.getContext('2d');
const ctxCrt  = canvasCrt.getContext('2d');
const ctxDots = canvasDots.getContext('2d');

let W, H;

function resizeCanvases() {
  W = window.innerWidth;
  H = window.innerHeight;
  canvasGrid.width = canvasCrt.width = canvasDots.width = W;
  canvasGrid.height = canvasCrt.height = canvasDots.height = H;
  drawBlueprint();
  initDots();
}

// ── Layer 1: Blueprint grid (draws once, redraws on resize) ───────────────────
function drawBlueprint() {
  ctxGrid.clearRect(0, 0, W, H);
  const MINOR = 24, MAJOR = 120;

  // Minor grid lines
  ctxGrid.strokeStyle = 'rgba(20,184,166,0.04)';
  ctxGrid.lineWidth = 0.5;
  for (let x = 0; x <= W; x += MINOR) {
    ctxGrid.beginPath(); ctxGrid.moveTo(x, 0); ctxGrid.lineTo(x, H); ctxGrid.stroke();
  }
  for (let y = 0; y <= H; y += MINOR) {
    ctxGrid.beginPath(); ctxGrid.moveTo(0, y); ctxGrid.lineTo(W, y); ctxGrid.stroke();
  }

  // Major grid lines
  ctxGrid.strokeStyle = 'rgba(20,184,166,0.09)';
  for (let x = 0; x <= W; x += MAJOR) {
    ctxGrid.beginPath(); ctxGrid.moveTo(x, 0); ctxGrid.lineTo(x, H); ctxGrid.stroke();
  }
  for (let y = 0; y <= H; y += MAJOR) {
    ctxGrid.beginPath(); ctxGrid.moveTo(0, y); ctxGrid.lineTo(W, y); ctxGrid.stroke();
  }

  // Cross markers at major intersections
  ctxGrid.strokeStyle = 'rgba(20,184,166,0.15)';
  for (let x = 0; x <= W; x += MAJOR) {
    for (let y = 0; y <= H; y += MAJOR) {
      ctxGrid.beginPath(); ctxGrid.moveTo(x - 4, y); ctxGrid.lineTo(x + 4, y); ctxGrid.stroke();
      ctxGrid.beginPath(); ctxGrid.moveTo(x, y - 4); ctxGrid.lineTo(x, y + 4); ctxGrid.stroke();
    }
  }

  // Coordinate labels
  ctxGrid.fillStyle = 'rgba(20,184,166,0.08)';
  ctxGrid.font = '7px "JetBrains Mono", monospace';
  for (let x = MAJOR; x < W; x += MAJOR) {
    for (let y = MAJOR; y < H; y += MAJOR) {
      ctxGrid.fillText(`${x},${y}`, x + 6, y - 3);
    }
  }

  // Double border frame
  ctxGrid.strokeStyle = 'rgba(20,184,166,0.08)';
  ctxGrid.lineWidth = 0.5;
  ctxGrid.strokeRect(16, 16, W - 32, H - 32);
  ctxGrid.strokeStyle = 'rgba(20,184,166,0.04)';
  ctxGrid.strokeRect(20, 20, W - 40, H - 40);

  drawTitleBlock();
}

function drawTitleBlock() {
  const bw = 260, bh = 50;
  const bx = W - bw - 24, by = H - bh - 24;
  const colWidths = [80, 50, 40, 50, 40];

  ctxGrid.strokeStyle = 'rgba(20,184,166,0.12)';
  ctxGrid.lineWidth = 0.5;
  ctxGrid.strokeRect(bx, by, bw, bh);

  // Vertical cell dividers
  let cx = bx;
  for (let i = 0; i < colWidths.length - 1; i++) {
    cx += colWidths[i];
    ctxGrid.beginPath(); ctxGrid.moveTo(cx, by); ctxGrid.lineTo(cx, by + bh); ctxGrid.stroke();
  }
  // Horizontal mid-divider
  ctxGrid.beginPath();
  ctxGrid.moveTo(bx, by + bh / 2);
  ctxGrid.lineTo(bx + bw, by + bh / 2);
  ctxGrid.stroke();

  // Top-row labels
  ctxGrid.fillStyle = 'rgba(20,184,166,0.12)';
  ctxGrid.font = '6px "JetBrains Mono", monospace';
  const labels = ['TITLE', 'DOC NO', 'REV', 'DATE', 'STATUS'];
  cx = bx;
  for (let i = 0; i < labels.length; i++) {
    ctxGrid.fillText(labels[i], cx + 4, by + 10);
    cx += colWidths[i];
  }

  // Bottom-row values
  ctxGrid.fillStyle = 'rgba(20,184,166,0.28)';
  ctxGrid.font = '7px "JetBrains Mono", monospace';
  const vals = ['BLITHEBOT', 'BB-001', 'A', '2026', 'PAPER'];
  cx = bx;
  for (let i = 0; i < vals.length; i++) {
    ctxGrid.fillText(vals[i], cx + 4, by + bh / 2 + 14);
    cx += colWidths[i];
  }
}

// ── Layer 2: CRT phosphor effects ─────────────────────────────────────────────
let crtMouseX = W / 2 || 760, crtMouseY = H / 2 || 400;
let crtTargetX = crtMouseX, crtTargetY = crtMouseY;
let crtBeamY = 0;

function drawCRT() {
  ctxCrt.clearRect(0, 0, W, H);

  // Lag phosphor glow toward mouse
  crtMouseX += (crtTargetX - crtMouseX) * 0.04;
  crtMouseY += (crtTargetY - crtMouseY) * 0.04;

  const grd = ctxCrt.createRadialGradient(
    crtMouseX, crtMouseY, 0,
    crtMouseX, crtMouseY, Math.max(W, H) * 0.7
  );
  grd.addColorStop(0, 'rgba(20,184,166,0.05)');
  grd.addColorStop(1, 'rgba(20,184,166,0)');
  ctxCrt.fillStyle = grd;
  ctxCrt.fillRect(0, 0, W, H);

  // CRT beam sweep (top-to-bottom, 8 s period)
  crtBeamY += H / (8 * 60);
  if (crtBeamY > H) crtBeamY = 0;
  const beamGrd = ctxCrt.createLinearGradient(0, crtBeamY - 20, 0, crtBeamY + 20);
  beamGrd.addColorStop(0,   'rgba(20,184,166,0)');
  beamGrd.addColorStop(0.5, 'rgba(20,184,166,0.03)');
  beamGrd.addColorStop(1,   'rgba(20,184,166,0)');
  ctxCrt.fillStyle = beamGrd;
  ctxCrt.fillRect(0, crtBeamY - 20, W, 40);

  // Pixel noise: 60 random pixels
  for (let i = 0; i < 60; i++) {
    ctxCrt.fillStyle = `rgba(20,184,166,${(Math.random() * 0.04).toFixed(3)})`;
    ctxCrt.fillRect(Math.random() * W, Math.random() * H, 1, 1);
  }
}

// ── Layer 3: Perlin dot grid ──────────────────────────────────────────────────
const DOT_SPACING = 28;
let dots = [];
let dotsMouseX = -9999, dotsMouseY = -9999;

function initDots() {
  dots = [];
  for (let x = 0; x <= W; x += DOT_SPACING) {
    for (let y = 0; y <= H; y += DOT_SPACING) {
      dots.push({
        x, y,
        speed:   0.6 + Math.random() * 0.8,
        noiseOx: Math.random() * 100,
        noiseOy: Math.random() * 100,
      });
    }
  }
}

function drawDots(t) {
  ctxDots.clearRect(0, 0, W, H);
  for (const dot of dots) {
    const nx = (dot.x / W) * 3 + dot.noiseOx;
    const ny = (dot.y / H) * 3 + dot.noiseOy;
    const n1 = noise(nx + t * dot.speed, ny, 0) * 0.5 + 0.5;
    const n2 = noise(nx * 2.1, ny * 2.1 + t * 0.4, 0.5) * 0.5 + 0.5;
    const combined = n1 * 0.7 + n2 * 0.3;
    const above = Math.max(0, (combined - 0.52) / (1 - 0.52));
    const dx = dot.x - dotsMouseX, dy = dot.y - dotsMouseY;
    const mouseBoost = Math.max(0, 1 - Math.sqrt(dx * dx + dy * dy) / 100) * 0.4;
    const final = Math.min(1, above + mouseBoost);
    const alpha = 0.05 + final * 0.17;
    const radius = 1.0 + final * 0.7;
    ctxDots.beginPath();
    ctxDots.arc(dot.x, dot.y, radius, 0, Math.PI * 2);
    ctxDots.fillStyle = `rgba(20,184,166,${alpha.toFixed(3)})`;
    ctxDots.fill();
  }
}

// ── Combined RAF loop ─────────────────────────────────────────────────────────
let rafT = 0;
function rafLoop() {
  rafT += 0.008;
  drawCRT();
  drawDots(rafT);
  requestAnimationFrame(rafLoop);
}

// ── Mouse + resize ────────────────────────────────────────────────────────────
window.addEventListener('mousemove', e => {
  crtTargetX = e.clientX;
  crtTargetY = e.clientY;
  dotsMouseX = e.clientX;
  dotsMouseY = e.clientY;
});

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(resizeCanvases, 120);
});

// ── Terminal lines data ───────────────────────────────────────────────────────
const TERMINAL_LINES = [
  '<span class="t-br">[Startup]</span> <span class="t-val">Market: OPEN 09:30 EST</span>',
  '<span class="t-br">[Startup]</span> <span class="t-val">HurstRegime: H=0.74 trending</span>',
  '<span class="t-br">[Startup]</span> <span class="t-val">KalmanSignal: Q=0.001 noise=0.31</span>',
  '<span class="t-br">[Startup]</span> <span class="t-val">CorrelationGuard: max_corr=0.70</span>',
  '<span class="t-br">[Startup]</span> <span class="t-val">ShortInterest: threshold=65%</span>',
  '<span class="t-div">──────────────────────────────────────</span>',
  '<span class="t-br">[SWING]</span>  <span class="t-sym">COST</span>  <span class="t-val">15 gates passed</span>',
  '<span class="t-br">[ORDER]</span>  <span class="t-buy">↑ BUY</span>  <span class="t-val">12 shares @ $95.40</span>',
  '<span class="t-br">[KELLY]</span>  <span class="t-val">win=61% payoff=1.8x  f=4.9%</span>',
  '<span class="t-div">──────────────────────────────────────</span>',
  '<span class="t-br">[SWING]</span>  <span class="t-sym">JPM</span>  <span class="t-val">15 gates passed</span>',
  '<span class="t-br">[ORDER]</span>  <span class="t-buy">↑ BUY</span>  <span class="t-val">8 shares @ $212.30</span>',
  '<span class="t-br">[EXIT]</span>   <span class="t-sell">↓ SELL</span>  <span class="t-val">12 shares @ $98.80</span>',
  '<span class="t-br">[P&amp;L]</span>   <span class="t-val">Kelly=4.9%</span>  <span class="t-buy">+$40.80</span>',
  '<span class="t-br">[EXIT]</span>   <span class="t-sell">↓ SELL</span>  <span class="t-val">8 shares @ $209.10</span>',
  '<span class="t-br">[P&amp;L]</span>   <span class="t-val">Kelly=3.8%</span>  <span class="t-sell">-$25.60</span>',
  '<span class="t-div">──────────────────────────────────────</span>',
  '<span class="t-cursor">_</span>',
];

function buildTerminal() {
  const container = document.getElementById('terminal-lines');
  if (!container) return;
  container.innerHTML = '';
  TERMINAL_LINES.forEach(html => {
    const span = document.createElement('span');
    span.className = 'terminal-line';
    span.innerHTML = html;
    container.appendChild(span);
  });
}

// ── GSAP animations ───────────────────────────────────────────────────────────
function setupAnimations() {
  // Kill any existing ScrollTriggers (called again after SPA nav back to home)
  if (typeof ScrollTrigger !== 'undefined') {
    ScrollTrigger.getAll().forEach(t => t.kill());
  }

  // Hero headline stagger
  gsap.to('.hero-line', {
    opacity: 1, y: 0,
    duration: 0.7,
    ease: 'power2.out',
    stagger: 0.08,
    delay: 0.2,
  });
  gsap.to('.hero-sub',     { opacity: 1, duration: 0.5, delay: 0.6, ease: 'power2.out' });
  gsap.to('.hero-actions', { opacity: 1, duration: 0.5, delay: 0.75, ease: 'power2.out' });

  // Scroll indicator fade on scroll
  window.addEventListener('scroll', () => {
    const ind = document.getElementById('scroll-indicator');
    if (ind) ind.style.opacity = window.scrollY > 80 ? '0' : '1';
  }, { passive: true });

  // Stat counters
  document.querySelectorAll('.stat-number').forEach(el => {
    const target = parseInt(el.dataset.target, 10);
    const obj = { val: 0 };
    gsap.to(obj, {
      val: target,
      duration: 1.5,
      ease: 'power2.out',
      snap: { val: 1 },
      onUpdate() { el.textContent = Math.round(obj.val).toLocaleString(); },
      scrollTrigger: { trigger: el, start: 'top 88%', once: true },
    });
  });

  // Edge section — pin + scrub lines + cards in
  const isMobile = window.innerWidth <= 768;
  const edgeTl = gsap.timeline({
    scrollTrigger: {
      trigger: '.edge-section',
      start: 'top top',
      end: '+=60%',
      scrub: 0.2,
      pin: !isMobile,
    },
  });
  edgeTl
    .to('.edge-line--1', { opacity: 1, y: 0, duration: 1 }, 0)
    .to('.edge-line--2', { opacity: 1, y: 0, duration: 1 }, 0.25)
    .to('.edge-line--3', { opacity: 1, y: 0, duration: 1 }, 0.5)
    .to('[data-edge-card="1"]', { opacity: 1, y: 0, duration: 0.8 }, 0.1)
    .to('[data-edge-card="2"]', { opacity: 1, y: 0, duration: 0.8 }, 0.3)
    .to('[data-edge-card="3"]', { opacity: 1, y: 0, duration: 0.8 }, 0.5)
    .to('[data-edge-card="4"]', { opacity: 1, y: 0, duration: 0.8 }, 0.7)
    .to('[data-edge-card="5"]', { opacity: 1, y: 0, duration: 0.8 }, 0.9);

  // Gate chain rows stagger in from left
  gsap.to('.gate-row', {
    opacity: 1, x: 0,
    duration: 0.4,
    ease: 'power2.out',
    stagger: 0.06,
    scrollTrigger: { trigger: '.gate-table', start: 'top 82%', once: true },
  });

  // Build terminal + type lines in
  buildTerminal();
  gsap.to('.terminal-line', {
    opacity: 1,
    duration: 0.12,
    stagger: 0.09,
    ease: 'none',
    scrollTrigger: { trigger: '.terminal-section', start: 'top 72%', once: true },
  });

  // Strategy panels slide up
  gsap.to('.strat-panel', {
    opacity: 1, y: 0,
    duration: 0.6,
    ease: 'power2.out',
    stagger: 0.12,
    scrollTrigger: { trigger: '.strategy-section', start: 'top 82%', once: true },
  });

  gsap.to('.milestone', {
    opacity: 1, y: 0,
    duration: 0.55,
    ease: 'power2.out',
    stagger: 0.14,
    scrollTrigger: { trigger: '#roadmap-timeline', start: 'top 78%', once: true },
  });

  // About lines
  gsap.to('.about-line', {
    opacity: 1, y: 0,
    duration: 0.7,
    ease: 'power2.out',
    stagger: 0.1,
    scrollTrigger: { trigger: '.about-section', start: 'top 78%', once: true },
  });

  gsap.to('.about-text', {
    opacity: 1, y: 0,
    duration: 0.55,
    ease: 'power2.out',
    stagger: 0.12,
    scrollTrigger: { trigger: '.about-right', start: 'top 82%', once: true },
  });
}

// ── Page builders (removed — pages now loaded via fetch) ──────────────────────
function buildStrategyPage() {
  return `<div class="spa-page">
    <div class="spa-hero spa-fade">
      <div class="spa-hero-dim">§ 01.00</div>
      <div class="spa-hero-accent">Strategy Architecture</div>
      <p style="font-size:0.82rem;color:var(--log-text);margin-top:1rem;max-width:580px;line-height:1.75;">19 concurrent asyncio loops. 15-gate conviction chain. Walk-forward validated parameters.</p>
    </div>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 01.01 — SYSTEM SPEC</div>
      <div class="spec-two-col" style="margin-top:2rem;">
        <div class="spec-rows">
          <div class="spec-row"><span class="spec-label">RUNTIME</span><span class="spec-val">Python asyncio · Railway 24/7</span></div>
          <div class="spec-row"><span class="spec-label">ACTIVE LOOPS</span><span class="spec-val">14 / 19 (5 disabled)</span></div>
          <div class="spec-row"><span class="spec-label">DATA STORE</span><span class="spec-val">PostgreSQL via SQLAlchemy</span></div>
          <div class="spec-row"><span class="spec-label">EXECUTION</span><span class="spec-val">Alpaca Markets (paper)</span></div>
          <div class="spec-row"><span class="spec-label">GATE CHAIN</span><span class="spec-val">15 gates per buy signal</span></div>
          <div class="spec-row"><span class="spec-label">LLM LAYER</span><span class="spec-val">DeepSeek Flash via OpenRouter</span></div>
          <div class="spec-row"><span class="spec-label">POSITION SIZE</span><span class="spec-val">Half-Kelly capped at 10%</span></div>
          <div class="spec-row"><span class="spec-label">MAX DAILY LOSS</span><span class="spec-val">5.0% — halts all new trades</span></div>
          <div class="spec-row"><span class="spec-label">HEAT CAP</span><span class="spec-val">15% aggregate open risk</span></div>
          <div class="spec-row"><span class="spec-label">VIX EXTREME</span><span class="spec-val">&gt; 40 blocks all trades</span></div>
        </div>
        <div class="mini-terminal">
          <div class="mini-terminal-hdr">SYSTEM LOG · 09:30:04 EST</div>
          <div class="mini-terminal-body">[Startup] DB connected · Alpaca ready<br>[Startup] FRED VIX=18.4 · regime=BULL<br>[SWING]  10:30 window open<br>[SWING]  Evaluating COST ema=20/100<br>[GATE-05] corr ρ=0.54 PASS<br>[GATE-06] FINRA ratio=0.51 PASS<br>[GATE-07] LLM verdict=proceed cv=0.81<br>[ORDER]  ↑ BUY 12 COST @ $95.40<br>[Health] equity=$98,806 pos=2</div>
        </div>
      </div>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 01.02 — PER-SYMBOL PARAMETERS</div>
      <p style="font-size:0.78rem;color:var(--log-text);margin:1rem 0 2rem;max-width:620px;">Parameters from Discovery Engine v1: 243-combo grid, 24-month train / 3-month test walk-forward, scipy t-test p &lt; 0.05 required.</p>
      <div class="param-cards">
        <div class="param-card"><div class="param-card-label">COST — 125/243 VALIDATED</div><div class="param-card-val">EMA 20/100 · RSI 10 · band 35–65<br>Best Sharpe 0.87 · Priority symbol</div></div>
        <div class="param-card"><div class="param-card-label">BRK.B — 24/243 VALIDATED</div><div class="param-card-val">EMA 50/200 · RSI 21 · band 40–65<br>Best Sharpe 0.61 · Low volatility</div></div>
        <div class="param-card"><div class="param-card-label">SPY — 9/243 VALIDATED</div><div class="param-card-val">EMA 50/200 · RSI 14 · band 40–60<br>Best Sharpe 0.52 · Regime hedge</div></div>
        <div class="param-card"><div class="param-card-label">V / JPM / PG — 0/243</div><div class="param-card-val">EMA 50/200 · RSI 14 · defaults<br>No validated edge · data collection</div></div>
      </div>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 01.03 — GATE CHAIN (15 GATES)</div>
      <p style="font-size:0.78rem;color:var(--log-text);margin:1rem 0 2rem;max-width:620px;">All 15 gates run sequentially per buy signal. First failure discards the trade.</p>
      <div class="gate-detail">
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-01</div><div class="gd-gate-name">Trading Halt</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>Max daily loss ≥ 5% halts all new trades for the session</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-02</div><div class="gd-gate-name">Bot Pause</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>Slack /pause command sets _bot_paused; all signals skip immediately</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-03</div><div class="gd-gate-name">Cooldown</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>120-min cooldown per symbol+strategy after stop-loss trigger</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-04</div><div class="gd-gate-name">Position Check</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>Skip if already holding this symbol (live Alpaca check)</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-05</div><div class="gd-gate-name">Heat Cap</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>∑(|market_value| × SL%) / equity ≥ 15% → block + critical Slack alert</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-06</div><div class="gd-gate-name">Correlation Guard</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>Pearson ρ &gt; 0.75 on 60-day closes vs open positions; max 2 per sector</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-07</div><div class="gd-gate-name">Short Interest</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>FINRA short ratio ≥ 65% vetoes buy; squeeze note if price uptick</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-08</div><div class="gd-gate-name">Fundamentals</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>Negative P/E or EPS decline &gt; 20% YoY via Finnhub</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-09</div><div class="gd-gate-name">Earnings Filter</div><div class="gd-gate-type">SIZE × 0.25</div></div><div class="gd-right"><p>Earnings within 48h → position size reduced to 25% (not a hard block)</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-10</div><div class="gd-gate-name">LLM Debate</div><div class="gd-gate-type">SOFT GATE</div></div><div class="gd-right"><p>3 calls: parallel bull+bear with web search → synthesis: proceed / skip / reduce_size</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-11</div><div class="gd-gate-name">Circuit Breaker</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>Rolling pnl_pct ≤ −threshold%; auto-resets when drawdown recovers</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-12</div><div class="gd-gate-name">VIX Extreme</div><div class="gd-gate-type">HARD BLOCK</div></div><div class="gd-right"><p>VIX &gt; 40 blocks entirely + critical Slack alert</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-13</div><div class="gd-gate-name">VIX Spike</div><div class="gd-gate-type">SIZE × 0.25</div></div><div class="gd-right"><p>VIX &gt; 35 proceeds at 25% size</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-14</div><div class="gd-gate-name">ADX Regime</div><div class="gd-gate-type">LOG ONLY</div></div><div class="gd-right"><p>SPY ADX(14) &lt; 20 (choppy) logs caution only; 4-hour cache</p></div></div>
        <div class="gd-row"><div class="gd-left"><div class="gd-gate-id">GATE-15</div><div class="gd-gate-name">Candlestick</div><div class="gd-gate-type">CONVICTION −20%</div></div><div class="gd-right"><p>No bullish pattern in last 3 bars → conviction multiplier × 0.8</p></div></div>
        <div class="gd-row gd-execute"><div class="gd-left"><div class="gd-gate-id" style="color:var(--teal);">EXECUTE</div><div class="gd-gate-name" style="color:#fff;">KellySizer → bracket order → DB log</div></div><div class="gd-right"><p style="color:var(--teal);">risk_pct × vix_mult × earnings_mult × debate_mult × perf_mult</p></div></div>
      </div>
    </section>
    <footer class="spa-footer"><span class="spa-footer-logo">BLITHEBOT</span><span class="spa-footer-doc">BB-001 · REV A · STRATEGY</span></footer>
  </div>`;
}

function buildResearchPage() {
  return `<div class="spa-page">
    <div class="spa-hero spa-fade">
      <div class="spa-hero-dim">§ 02.00</div>
      <div class="spa-hero-accent">Signal Research</div>
      <p style="font-size:0.82rem;color:var(--log-text);margin-top:1rem;max-width:580px;line-height:1.75;">Walk-forward validated discovery. Statistical signal layer. Open data sources.</p>
    </div>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 02.01 — WALK-FORWARD VALIDATION</div>
      <p style="font-size:0.78rem;color:var(--log-text);margin:1rem 0 1.5rem;max-width:620px;">Discovery Engine v1 runs a 243-combination grid search on 6-year daily bars. Each combination validated with 24-month train / 3-month test walk-forward requiring p &lt; 0.05 on scipy t-test of test CAGRs.</p>
      <div class="wf-diagram">
        <span class="wf-train">█████████████████ TRAIN 24 mo</span><br>
        <span class="wf-test">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;████ TEST 3 mo → scipy t-test p &lt; 0.05</span><br>
        <span class="wf-train">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;█████████████████ TRAIN 24 mo</span><br>
        <span class="wf-test">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;████ TEST 3 mo → validate</span><br>
        <span class="wf-result">→ VALIDATED if test_sharpe &gt; 0 and p &lt; 0.05 across folds</span>
      </div>
      <table class="spec-table">
        <thead><tr><th>SYMBOL</th><th>COMBOS</th><th>VALIDATED</th><th>BEST SHARPE</th><th>RUN DATE</th></tr></thead>
        <tbody>
          <tr><td class="td-teal">COST</td><td>243</td><td class="td-teal">125</td><td>0.87</td><td>2026-05-10</td></tr>
          <tr><td>BRK.B</td><td>243</td><td>24</td><td>0.61</td><td>2026-05-10</td></tr>
          <tr><td>SPY</td><td>243</td><td>9</td><td>0.52</td><td>2026-05-10</td></tr>
          <tr><td class="td-muted">JPM</td><td>243</td><td class="td-warn">0</td><td>—</td><td>2026-05-10</td></tr>
          <tr><td class="td-muted">PG</td><td>243</td><td class="td-warn">0</td><td>—</td><td>2026-05-10</td></tr>
        </tbody>
      </table>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 02.02 — STATISTICAL SIGNAL LAYER</div>
      <div class="formula-block"><strong>Kalman Filter (1D scalar)</strong><br>x̂ₖ = x̂ₖ₋₁ + K·(zₖ − x̂ₖ₋₁) &nbsp; K = P⁻·(P⁻ + R)⁻¹<br><span class="f-note">Optional wavelet (db4) pre-denoising · outputs: trend, slope, noise_ratio, signal ∈ {−1,0,+1}</span></div>
      <div class="formula-block"><strong>Hurst Exponent (R/S Analysis)</strong><br>H = log(R/S) / log(n) &nbsp; H &gt; 0.6 trending · H &lt; 0.4 mean-reverting<br><span class="f-note">100-bar rolling window · OLS log(RS) ~ log(lag) · regime gate for swing entries</span></div>
      <div class="formula-block"><strong>Half-Life (Ornstein-Uhlenbeck)</strong><br>Δpₜ = α + β·pₜ₋₁ + εₜ &nbsp; HL = −ln(2) / ln(1 + β)<br><span class="f-note">Gates Bollinger mean reversion entries · valid HL ∈ [1, 30] bars · β ∈ (−1, 0) required</span></div>
      <div class="formula-block"><strong>Half-Kelly Position Sizing</strong><br>f* = (p·b − q) / b &nbsp; size = (f*/2) · equity · risk%<br><span class="f-note">90-day lookback from signal_outcomes · min 20 closed trades · fallback: 2% default</span></div>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 02.03 — DATA SOURCES</div>
      <div class="src-cards">
        <div class="src-card"><div class="src-card-tag">PRICE</div><div class="src-card-title">Alpaca IEX + Finnhub</div><div class="src-card-meta">15-min delay mitigated by Finnhub real-time overlay · 1-year daily bars for signals</div></div>
        <div class="src-card"><div class="src-card-tag">NEWS</div><div class="src-card-title">Benzinga via Alpaca</div><div class="src-card-meta">S&amp;P 500 headlines · LLM NLP scoring (1–15) · auto-trade threshold 13</div></div>
        <div class="src-card"><div class="src-card-tag">MACRO</div><div class="src-card-title">FRED Public CSV</div><div class="src-card-meta">FF rate, VIX, 10Y yield, unemployment, CPI YoY · no API key · daily 7 PM EST</div></div>
        <div class="src-card"><div class="src-card-tag">INSIDER</div><div class="src-card-title">SEC EDGAR Form 4</div><div class="src-card-meta">Open-market transactions · ElementTree XML · buys ≥ $1M → auto-trade (score=14)</div></div>
        <div class="src-card"><div class="src-card-tag">SENTIMENT</div><div class="src-card-title">xAI Grok + X/Twitter</div><div class="src-card-meta">Live X search via grok-3-mini · BTC/ETH sentiment 0–10 · alert at ≥7 or ≤3</div></div>
        <div class="src-card"><div class="src-card-tag">SHORT</div><div class="src-card-title">FINRA CNMSshvol</div><div class="src-card-meta">ShortVolume / TotalVolume daily · ≥65% vetoes swing buy · squeeze detection on uptick</div></div>
      </div>
    </section>
    <footer class="spa-footer"><span class="spa-footer-logo">BLITHEBOT</span><span class="spa-footer-doc">BB-001 · REV A · RESEARCH</span></footer>
  </div>`;
}

function buildPerformancePage() {
  return `<div class="spa-page">
    <div class="spa-hero spa-fade">
      <div class="spa-hero-dim">§ 03.00</div>
      <div class="spa-hero-accent">Performance</div>
      <p style="font-size:0.82rem;color:var(--log-text);margin-top:1rem;max-width:580px;line-height:1.75;">Paper trading · data accumulating since May 2026 · live account equity tracking</p>
    </div>
    <section class="spa-section spa-fade">
      <div class="perf-notice-block">
        <div class="perf-notice-title">PAPER TRADING MODE</div>
        <p>The bot is operating on Alpaca's simulated exchange. No real capital at risk. Performance data reflects simulated fills and may differ from live execution due to slippage, liquidity, and market impact. Metrics update as signal_outcomes accumulates closed trades.</p>
      </div>
      <div class="perf-stats-grid">
        <div class="perf-stat-card"><div class="perf-stat-ref">§ PS-01</div><div class="perf-stat-value">$100k</div><div class="perf-stat-label">STARTING EQUITY</div></div>
        <div class="perf-stat-card"><div class="perf-stat-ref">§ PS-02</div><div class="perf-stat-value">2</div><div class="perf-stat-label">OPEN POSITIONS</div><div class="perf-stat-note">COST · SPY</div></div>
        <div class="perf-stat-card"><div class="perf-stat-ref">§ PS-03</div><div class="perf-stat-value">15</div><div class="perf-stat-label">GATE CHAIN DEPTH</div><div class="perf-stat-note">per buy signal</div></div>
        <div class="perf-stat-card"><div class="perf-stat-ref">§ PS-04</div><div class="perf-stat-value">19</div><div class="perf-stat-label">ACTIVE LOOPS</div><div class="perf-stat-note">railway 24/7</div></div>
      </div>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 03.01 — GATE PASS RATES (OBSERVED)</div>
      <div class="metric-grid">
        <div class="metric-card"><div class="metric-card-label">HEAT CAP BLOCKS</div><div class="metric-card-title">~12%</div><p>Portfolio aggregate risk exceeds 15% cap</p></div>
        <div class="metric-card"><div class="metric-card-label">CORR GUARD BLOCKS</div><div class="metric-card-title">~8%</div><p>Pearson ρ &gt; 0.75 with an open position</p></div>
        <div class="metric-card"><div class="metric-card-label">LLM SKIP RATE</div><div class="metric-card-title">~31%</div><p>Bull/bear debate verdict = skip</p></div>
        <div class="metric-card"><div class="metric-card-label">SIZE REDUCTIONS</div><div class="metric-card-title">~19%</div><p>Debate verdict = reduce_size (× 0.5)</p></div>
        <div class="metric-card"><div class="metric-card-label">EXECUTION RATE</div><div class="metric-card-title">~30%</div><p>Of raw signals that pass all 15 gates</p></div>
        <div class="metric-card"><div class="metric-card-label">KELLY STATUS</div><div class="metric-card-title">DEFAULT</div><p>Min 20 closed trades required · using 2% fallback</p></div>
      </div>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 03.02 — SIGNAL LOG SAMPLE</div>
      <div class="perf-timeline">
        <div class="perf-tl-header">DATE &nbsp;·&nbsp; SYMBOL &nbsp;·&nbsp; TYPE &nbsp;·&nbsp; ENTRY &nbsp;·&nbsp; STATUS &nbsp;·&nbsp; P&amp;L</div>
        <div class="perf-tl-row"><span class="perf-tl-date active">2026-05-19</span><span class="perf-tl-badge">COST</span><span class="perf-tl-text">swing_long · entry $910.40 · <em>open</em></span></div>
        <div class="perf-tl-row"><span class="perf-tl-date active">2026-05-15</span><span class="perf-tl-badge">SPY</span><span class="perf-tl-text">swing_long · entry $523.10 · <em>open</em></span></div>
        <div class="perf-tl-row"><span class="perf-tl-date">—</span><span class="perf-tl-text" style="color:var(--muted);">Closed trade data accumulating · check back as exits are logged by _exit_monitor_loop</span></div>
      </div>
      <div class="formula-block" style="margin-top:2rem;"><strong>Current sizing: DEFAULT FALLBACK</strong><br>risk = 2.0% × vix_mult × earnings_mult × debate_mult × perf_mult<br><span class="f-note">Kelly activates once 20+ closed trades accumulate per signal type · floor: 10% of base risk</span></div>
    </section>
    <footer class="spa-footer"><span class="spa-footer-logo">BLITHEBOT</span><span class="spa-footer-doc">BB-001 · REV A · PERFORMANCE</span></footer>
  </div>`;
}

function buildDashboardPage() {
  return `<div class="spa-page">
    <div class="spa-hero spa-fade">
      <div class="spa-hero-dim">§ 04.00</div>
      <div class="spa-hero-accent">Live Dashboard</div>
      <p style="font-size:0.82rem;color:var(--log-text);margin-top:1rem;max-width:580px;line-height:1.75;">Real-time account state · signal feed · correlation matrix</p>
    </div>
    <section class="spa-section spa-fade">
      <div class="dash-notice">READ-ONLY PREVIEW — Live data reflects the paper trading account. Interactive Streamlit dashboard (trade log, discovery approval, analytics) requires Railway deployment access on port 8501.</div>
      <div class="section-tag-mono" style="margin-top:2rem;">§ 04.01 — ACCOUNT STATE</div>
      <div class="dash-account-grid" style="margin-top:1rem;">
        <div class="dash-account-card"><div class="dac-ref">EQUITY</div><div class="dac-value">$98,806</div><div class="dac-label">paper account</div></div>
        <div class="dash-account-card"><div class="dac-ref">DAILY P&amp;L</div><div class="dac-value" style="color:var(--teal);">+$184</div><div class="dac-label">+0.19%</div></div>
        <div class="dash-account-card"><div class="dac-ref">OPEN POSITIONS</div><div class="dac-value">2</div><div class="dac-label">COST · SPY</div></div>
        <div class="dash-account-card"><div class="dac-ref">VIX / REGIME</div><div class="dac-value">18.4</div><div class="dac-label" style="color:var(--teal);">BULL · SPY &gt; EMA-200</div></div>
      </div>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 04.02 — SIGNAL FEED</div>
      <div class="signal-feed" style="margin-top:1rem;">
        <div class="sf-header"><span>TIME</span><span>SYM</span><span>SOURCE</span><span>STR</span><span>GATE</span><span>ACTION</span></div>
        <div class="sf-row sf-green"><span>10:31:04</span><span class="sf-sym">COST</span><span class="sf-strat">swing_long</span><span>—</span><span>PASS</span><span class="sf-action">BUY 12 @ $910</span></div>
        <div class="sf-row sf-red"><span>10:31:05</span><span class="sf-sym">JPM</span><span class="sf-strat">swing_long</span><span>—</span><span>H=0.58</span><span class="sf-action">SKIP</span></div>
        <div class="sf-row sf-yellow"><span>09:58:12</span><span class="sf-sym">TSLA</span><span class="sf-strat">news_nlp</span><span>8/15</span><span>ALERT</span><span class="sf-action">SLACK</span></div>
        <div class="sf-row sf-green"><span>08:14:33</span><span class="sf-sym">TSLA</span><span class="sf-strat">sec_edgar</span><span>14/15</span><span>PASS</span><span class="sf-action">BUY 3 @ $182</span></div>
        <div class="sf-row sf-yellow"><span>07:30:01</span><span class="sf-sym">BTC</span><span class="sf-strat">grok_x</span><span>8/10</span><span>ALERT</span><span class="sf-action">SLACK</span></div>
        <div class="sf-row sf-green"><span>07:02:55</span><span class="sf-sym">SPY</span><span class="sf-strat">swing_long</span><span>—</span><span>PASS</span><span class="sf-action">BUY 4 @ $523</span></div>
      </div>
    </section>
    <section class="spa-section spa-fade">
      <div class="section-tag-mono">§ 04.03 — CORRELATION MATRIX</div>
      <p style="font-size:0.78rem;color:var(--log-text);margin:0.75rem 0 1rem;max-width:600px;">Pearson ρ on 60-day closing prices · refreshed every 30 min · ρ &gt; 0.75 blocks new trade if correlated position is open</p>
      <div class="corr-grid">
        <div></div>
        <div class="cg-head">SPY</div><div class="cg-head">COST</div><div class="cg-head">BRK.B</div><div class="cg-head">V</div><div class="cg-head">JPM</div><div class="cg-head">PG</div>
        <div class="cg-row-label">SPY</div><div class="cg-self cg-cell">1.00</div><div class="cg-watch cg-cell">0.68</div><div class="cg-watch cg-cell">0.71</div><div class="cg-blocked cg-cell">0.82</div><div class="cg-watch cg-cell">0.73</div><div class="cg-ok cg-cell">0.55</div>
        <div class="cg-row-label">COST</div><div class="cg-watch cg-cell">0.68</div><div class="cg-self cg-cell">1.00</div><div class="cg-ok cg-cell">0.48</div><div class="cg-ok cg-cell">0.52</div><div class="cg-ok cg-cell">0.44</div><div class="cg-ok cg-cell">0.39</div>
        <div class="cg-row-label">BRK.B</div><div class="cg-watch cg-cell">0.71</div><div class="cg-ok cg-cell">0.48</div><div class="cg-self cg-cell">1.00</div><div class="cg-ok cg-cell">0.61</div><div class="cg-watch cg-cell">0.74</div><div class="cg-ok cg-cell">0.58</div>
        <div class="cg-row-label">V</div><div class="cg-blocked cg-cell">0.82</div><div class="cg-ok cg-cell">0.52</div><div class="cg-ok cg-cell">0.61</div><div class="cg-self cg-cell">1.00</div><div class="cg-watch cg-cell">0.69</div><div class="cg-ok cg-cell">0.47</div>
        <div class="cg-row-label">JPM</div><div class="cg-watch cg-cell">0.73</div><div class="cg-ok cg-cell">0.44</div><div class="cg-watch cg-cell">0.74</div><div class="cg-watch cg-cell">0.69</div><div class="cg-self cg-cell">1.00</div><div class="cg-ok cg-cell">0.51</div>
        <div class="cg-row-label">PG</div><div class="cg-ok cg-cell">0.55</div><div class="cg-ok cg-cell">0.39</div><div class="cg-ok cg-cell">0.58</div><div class="cg-ok cg-cell">0.47</div><div class="cg-ok cg-cell">0.51</div><div class="cg-self cg-cell">1.00</div>
      </div>
      <div class="corr-legend">
        <span><span class="cs cs-blocked"></span>BLOCKED ρ &gt; 0.75</span>
        <span><span class="cs cs-watch"></span>WATCH 0.60–0.75</span>
        <span><span class="cs cs-ok"></span>OK &lt; 0.60</span>
      </div>
    </section>
    <section class="spa-section spa-fade">
      <div class="dash-cta">
        <div style="font-family:var(--font-mono);font-size:0.95rem;color:var(--h1-primary);margin-bottom:0.75rem;">Interactive Dashboard</div>
        <p style="font-size:0.78rem;color:var(--log-text);max-width:480px;margin:0 auto 1rem;line-height:1.7;">Full Streamlit dashboard: Trade Log, Discovery approval workflow, Analytics (equity curve, win rate by signal type). Runs at Railway deployment port 8501.</p>
        <div class="dash-cta-note">Dashboard access restricted to operator account. Railway deployment URL is not public.</div>
      </div>
    </section>
    <footer class="spa-footer"><span class="spa-footer-logo">BLITHEBOT</span><span class="spa-footer-doc">BB-001 · REV A · DASHBOARD</span></footer>
  </div>`;
}

// ── SPA routing ───────────────────────────────────────────────────────────────
const pageContent = document.getElementById('page-content');

// Single delegated listener — never duplicated on re-navigation
document.addEventListener('click', e => {
  const el = e.target.closest('[data-page]');
  if (!el) return;
  e.preventDefault();
  navigateTo(el.dataset.page, true);
});

function navigateTo(page, push) {
  document.querySelectorAll('[data-page]').forEach(el => {
    el.classList.toggle('nav-active', el.dataset.page === page);
  });

  const filename = page === 'home' ? 'index.html' : `${page}.html`;

  fetch(filename)
    .then(r => r.text())
    .then(html => {
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');
      const body = doc.body.innerHTML;

      pageContent.style.opacity = '0';

      setTimeout(() => {
        if (typeof ScrollTrigger !== 'undefined') {
          ScrollTrigger.getAll().forEach(t => t.kill());
        }

        pageContent.innerHTML = body;
        window.scrollTo(0, 0);

        if (push) history.pushState({ page }, '', page === 'home' ? '/' : `/${page}`);

        requestAnimationFrame(() => requestAnimationFrame(() => {
          pageContent.style.opacity = '1';
          if (page === 'home') {
            setupAnimations();
          } else {
            gsap.to('.spa-fade', {
              opacity: 1, y: 0,
              duration: 0.6,
              ease: 'power2.out',
              stagger: 0.1,
              delay: 0.05,
            });
          }
        }));
      }, 200);
    });
}

window.addEventListener('popstate', e => {
  navigateTo((e.state && e.state.page) || 'home', false);
});

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  gsap.registerPlugin(ScrollTrigger);

  resizeCanvases(); // sizes canvases, draws blueprint, inits dots
  rafLoop();        // starts CRT + dots animation

  setupAnimations(); // buildTerminal() is called inside here
});
