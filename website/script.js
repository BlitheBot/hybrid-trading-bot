// BlitheBot fetch routing — body-swap SPA

const PAGE_FILES = {
  architecture: 'architecture.html',
  performance:  'performance.html',
  discovery:    'discovery.html',
  dashboard:    'dashboard.html',
};

function navigateTo(page) {
  if (page === 'home') {
    window.location.href = 'index.html';
    return;
  }
  const filename = PAGE_FILES[page];
  if (!filename) return;

  document.body.style.transition = 'opacity 0.2s ease';
  document.body.style.opacity = '0';

  setTimeout(() => {
    fetch(filename)
      .then(r => r.text())
      .then(html => {
        const doc = new DOMParser().parseFromString(html, 'text/html');
        document.body.innerHTML = doc.body.innerHTML;
        document.body.style.opacity = '0';
        history.pushState({ page }, '', filename);
        requestAnimationFrame(() => requestAnimationFrame(() => {
          document.body.style.transition = 'opacity 0.2s ease';
          document.body.style.opacity = '1';
        }));
        attachRouting();
        initReveal();
      });
  }, 200);
}

function attachRouting() {
  document.querySelectorAll('[data-page]').forEach(el => {
    el.addEventListener('click', e => {
      e.preventDefault();
      navigateTo(el.dataset.page);
    });
  });
}

function initReveal() {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        e.target.classList.add('visible');
        observer.unobserve(e.target);
      }
    });
  }, { threshold: 0.12 });

  document.querySelectorAll('.reveal').forEach(el => {
    observer.observe(el);
  });
}

window.addEventListener('popstate', e => {
  const page = (e.state && e.state.page) || 'home';
  if (page === 'home') { window.location.href = 'index.html'; return; }
  navigateTo(page);
});

document.addEventListener('DOMContentLoaded', () => {
  attachRouting();
  initReveal();
});
