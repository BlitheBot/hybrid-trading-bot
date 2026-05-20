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
  x -= Math.floor(x);
  y -= Math.floor(y);
  const u = fade(x), v = fade(y);
  const a = p[X] + Y, b = p[X + 1] + Y;
  return lerp(v,
    lerp(u, grad(p[a],     x,     y,     z), grad(p[b],     x - 1, y,     z)),
    lerp(u, grad(p[a + 1], x,     y - 1, z), grad(p[b + 1], x - 1, y - 1, z))
  );
}

// ── Canvas setup ──────────────────────────────────────────────────────────────
const canvas = document.getElementById('dot-grid');
const ctx = canvas.getContext('2d');
const DOT_SPACING = 28;
let W, H, dots = [], t = 0;
let mouseX = -9999, mouseY = -9999;

function initDots() {
  W = canvas.width = window.innerWidth;
  H = canvas.height = window.innerHeight;
  dots = [];
  for (let x = 0; x <= W; x += DOT_SPACING) {
    for (let y = 0; y <= H; y += DOT_SPACING) {
      dots.push({
        x,
        y,
        phase: Math.random() * Math.PI * 2,
        speed: 0.6 + Math.random() * 0.8,
        noiseOx: Math.random() * 100,
        noiseOy: Math.random() * 100,
      });
    }
  }
}

function drawFrame() {
  ctx.clearRect(0, 0, W, H);
  t += 0.008;

  for (const dot of dots) {
    const nx = (dot.x / W) * 3 + dot.noiseOx;
    const ny = (dot.y / H) * 3 + dot.noiseOy;
    const n1 = noise(nx + t * dot.speed, ny, 0) * 0.5 + 0.5;
    const n2 = noise(nx * 2.1, ny * 2.1 + t * 0.4, 0.5) * 0.5 + 0.5;
    const combined = n1 * 0.7 + n2 * 0.3;
    const threshold = 0.52;
    const above = Math.max(0, (combined - threshold) / (1 - threshold));
    const dx = dot.x - mouseX;
    const dy = dot.y - mouseY;
    const distToMouse = Math.sqrt(dx * dx + dy * dy);
    const mouseBoost = Math.max(0, 1 - distToMouse / 100) * 0.4;
    const final = Math.min(1, above + mouseBoost);
    const glowAlpha = 0.07 + final * 0.28;
    const radius = 1.1 + final * 0.7;
    ctx.beginPath();
    ctx.arc(dot.x, dot.y, radius, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(20,184,166,${glowAlpha.toFixed(3)})`;
    ctx.fill();
  }

  requestAnimationFrame(drawFrame);
}

window.addEventListener('mousemove', e => {
  mouseX = e.clientX;
  mouseY = e.clientY;
});

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(initDots, 100);
});

initDots();
requestAnimationFrame(drawFrame);

// ── SPA routing ───────────────────────────────────────────────────────────────
const pageContent = document.getElementById('page-content');

// Snapshot home HTML before any scroll animations add .visible classes
const homeHTML = pageContent.innerHTML;

function setupPage(page) {
  if (page === 'home') {
    // Hero animations
    const heroText = document.querySelector('.hero-text');
    if (heroText) requestAnimationFrame(() => heroText.classList.add('visible'));
    const heroTerminal = document.querySelector('.hero-terminal');
    if (heroTerminal) setTimeout(() => heroTerminal.classList.add('visible'), 600);

    // Re-observe scroll elements
    document.querySelectorAll('.animate-in').forEach(el => scrollObserver.observe(el));

    // Gate chain stagger (home page .gate-row elements)
    const gateList = document.querySelector('.gate-list');
    if (gateList) homeGateObserver.observe(gateList);
  } else {
    // Scroll animate-in for SPA pages
    document.querySelectorAll('.animate-in').forEach(el => scrollObserver.observe(el));

    if (page === 'strategy') {
      // Stagger .sgate-item elements
      const sgateList = document.querySelector('.strategy-gate-list');
      if (sgateList) {
        const sgateObs = new IntersectionObserver((entries) => {
          entries.forEach(entry => {
            if (entry.isIntersecting) {
              entry.target.querySelectorAll('.sgate-item').forEach((item, i) => {
                setTimeout(() => item.classList.add('visible'), i * 55);
              });
              sgateObs.unobserve(entry.target);
            }
          });
        }, { threshold: 0.05 });
        sgateObs.observe(sgateList);
      }
    }
  }
}

function navigateTo(page, push) {
  // Update active nav link
  document.querySelectorAll('.nav-center a').forEach(a => {
    a.classList.toggle('nav-active', (a.dataset.page || 'home') === page);
  });
  // Also toggle logo active state
  const logo = document.querySelector('.nav-logo');
  if (logo) logo.classList.toggle('nav-active', page === 'home');

  // Fade out
  pageContent.style.opacity = '0';

  setTimeout(() => {
    // Swap content
    pageContent.innerHTML = page === 'home' ? homeHTML : (PAGES[page] || homeHTML);
    window.scrollTo(0, 0);

    if (push) {
      const url = page === 'home' ? '/' : `/${page}`;
      history.pushState({ page }, '', url);
    }

    // Fade in (double rAF ensures transition fires after browser processes new DOM)
    requestAnimationFrame(() => requestAnimationFrame(() => {
      pageContent.style.opacity = '1';
      setupPage(page);
    }));
  }, 200);
}

// Nav click handler
document.querySelectorAll('[data-page]').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    const page = el.dataset.page;
    navigateTo(page, true);
  });
});

// Back/forward
window.addEventListener('popstate', e => {
  const page = (e.state && e.state.page) || 'home';
  navigateTo(page, false);
});

// ── Scroll animations ─────────────────────────────────────────────────────────
const scrollObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
      scrollObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.1 });

// ── Gate chain stagger (home page) ────────────────────────────────────────────
const homeGateObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.querySelectorAll('.gate-row').forEach((row, i) => {
        setTimeout(() => row.classList.add('visible'), i * 80);
      });
      homeGateObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.1 });

// ── Initial page setup ────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  // Determine page from URL path
  const path = window.location.pathname.replace(/^\//, '').replace(/\/$/, '');
  const validPages = ['strategy', 'performance', 'research', 'dashboard'];
  const initialPage = validPages.includes(path) ? path : 'home';

  if (initialPage !== 'home') {
    navigateTo(initialPage, false);
  } else {
    setupPage('home');
  }
});
