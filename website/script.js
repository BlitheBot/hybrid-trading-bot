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
  '<span class="t-bracket">[Startup]</span> <span class="t-log">Initializing 19 async loops...</span>',
  '<span class="t-bracket">[Startup]</span> <span class="t-log">DB connected · Alpaca connected · FRED fetched</span>',
  '<span class="t-bracket">[SWING]</span>   Evaluating <span class="t-ticker">COST</span> <span class="t-log">ema_short=20 ema_long=100 rsi=42.1</span>',
  '<span class="t-bracket">[SWING]</span>   <span class="t-ticker">COST</span> <span class="t-log">Kalman noise_ratio=0.28 H=0.71</span> — <span class="t-buy">signals align</span>',
  '<span class="t-bracket">[GATE]</span>    GATE-01 halt=false — <span class="t-profit">PASS</span>',
  '<span class="t-bracket">[GATE]</span>    GATE-02 heat=8.2% &lt; 15% — <span class="t-profit">PASS</span>',
  '<span class="t-bracket">[GATE]</span>    GATE-03 corr_guard ρ=0.54 — <span class="t-profit">PASS</span>',
  '<span class="t-bracket">[GATE]</span>    GATE-07 LLM verdict=proceed conviction=0.81',
  '<span class="t-bracket">[ORDER]</span>   <span class="t-buy">↑ BUY</span> 12 shares <span class="t-ticker">COST</span> @ $95.40 · SL=$93.49 · TP=$101.57',
  '<span class="t-bracket">[SWING]</span>   Evaluating <span class="t-ticker">JPM</span> <span class="t-log">H=0.58</span>',
  '<span class="t-bracket">[GATE]</span>    GATE-06 Hurst H=0.58 &lt; 0.60 — <span class="t-sell">BLOCK</span> regime not trending',
  '<span class="t-bracket">[EXIT]</span>    <span class="t-ticker">SPY</span> TP hit @ $519.20 · entry=$510.00',
  '<span class="t-bracket">[P&amp;L]</span>    <span class="t-ticker">SPY</span> +<span class="t-profit">$184.00</span> (+1.80%) · hold=3d',
  '<span class="t-bracket">[EDGAR]</span>   Form 4: <span class="t-ticker">TSLA</span> insider buy $2.1M · score=14 → <span class="t-buy">AUTO-TRADE</span>',
  '<span class="t-bracket">[Health]</span>  equity=$98,806 · positions=2 · daily_pnl=+0.8%',
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
      end: '+=250%',
      scrub: 0.6,
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

  // Roadmap: SVG line draws down, milestones fade in
  const tlSvg = document.getElementById('timeline-svg');
  if (tlSvg) {
    gsap.fromTo(tlSvg,
      { clipPath: 'inset(0 0 100% 0)' },
      {
        clipPath: 'inset(0 0 0% 0)',
        ease: 'none',
        scrollTrigger: {
          trigger: '#roadmap-timeline',
          start: 'top 72%',
          end: 'bottom 65%',
          scrub: 0.4,
        },
      }
    );
  }

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

// ── SPA routing ───────────────────────────────────────────────────────────────
const pageContent = document.getElementById('page-content');
const homeHTML = pageContent.innerHTML;

function placeholderPage(page) {
  return `
    <section style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:6rem 2.5rem;position:relative;z-index:10;">
      <div style="text-align:center;">
        <div style="font-family:var(--font-mono);font-size:9px;color:rgba(20,184,166,0.3);letter-spacing:0.1em;margin-bottom:1rem;">${page.toUpperCase()}</div>
        <h1 style="font-family:var(--font-mono);font-size:clamp(2rem,5vw,3.5rem);font-weight:300;color:var(--h1-primary);margin-bottom:1rem;letter-spacing:-0.02em;">${page.charAt(0).toUpperCase() + page.slice(1)}</h1>
        <p style="font-size:0.82rem;color:var(--log-text);max-width:380px;margin:0 auto 2.5rem;line-height:1.7;">This section is coming soon. Check back as paper trading data accumulates.</p>
        <a href="#" data-page="home" class="btn-primary" style="text-decoration:none;">← BACK HOME</a>
      </div>
    </section>`;
}

function wireNavClicks() {
  document.querySelectorAll('[data-page]').forEach(el => {
    el.addEventListener('click', e => {
      e.preventDefault();
      navigateTo(el.dataset.page, true);
    });
  });
}

function navigateTo(page, push) {
  document.querySelectorAll('[data-page]').forEach(el => {
    el.classList.toggle('nav-active', el.dataset.page === page);
  });

  pageContent.style.opacity = '0';

  setTimeout(() => {
    if (page === 'home') {
      pageContent.innerHTML = homeHTML;
    } else {
      pageContent.innerHTML = placeholderPage(page);
    }

    window.scrollTo(0, 0);
    if (push) history.pushState({ page }, '', page === 'home' ? '/' : `/${page}`);

    wireNavClicks();

    requestAnimationFrame(() => requestAnimationFrame(() => {
      pageContent.style.opacity = '1';
      if (page === 'home') setupAnimations();
    }));
  }, 200);
}

window.addEventListener('popstate', e => {
  navigateTo((e.state && e.state.page) || 'home', false);
});

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  gsap.registerPlugin(ScrollTrigger);

  resizeCanvases(); // sizes canvases, draws blueprint, inits dots
  rafLoop();        // starts CRT + dots animation

  wireNavClicks();
  setupAnimations();
});
