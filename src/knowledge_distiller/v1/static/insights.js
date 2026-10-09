const page = document.querySelector('[data-insight-state]');
const scroll = page?.querySelector('[data-scroll-area]');
const key = page ? `insight-view:${page.dataset.insightState}` : '';

function saveReading() {
  if (!page || !scroll) return;
  sessionStorage.setItem(key, JSON.stringify({
    open: page.querySelector('.insight-card[open]')?.id,
    scroll: scroll.scrollTop,
  }));
}

if (page && scroll) {
  const cards = [...page.querySelectorAll('.insight-card')];
  for (const card of cards) {
    card.addEventListener('toggle', () => {
      if (card.open) {
        for (const other of cards) if (other !== card) other.open = false;
      }
      saveReading();
    });
  }
  let previous = {};
  try { previous = JSON.parse(sessionStorage.getItem(key) || '{}'); } catch (_) {}
  const target = document.getElementById(location.hash.slice(1)) ||
    (previous.open && document.getElementById(previous.open));
  if (target) {
    target.open = true;
    requestAnimationFrame(() => {
      scroll.scrollTop = location.hash ? target.offsetTop - scroll.offsetTop : (previous.scroll || 0);
    });
  }
  window.addEventListener('pagehide', saveReading);
}
