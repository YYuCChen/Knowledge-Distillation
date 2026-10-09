const page = document.querySelector('[data-insight-state]');
const scroll = page.querySelector('[data-scroll-area]');
const key = `insight-view:${page.dataset.insightState}`;
const draftKey = 'insight-drafts';
const reloaded = performance.getEntriesByType('navigation')[0]?.type === 'reload';
if (reloaded) sessionStorage.removeItem(draftKey);
let drafts = JSON.parse(sessionStorage.getItem(draftKey) || '{}');
function resize(input) { input.style.height = 'auto'; input.style.height = `${input.scrollHeight}px`; }
function draftId(form) { return `${form.closest('[data-version]').dataset.version}:${form.dataset.insightAction}`; }
function bind(card) {
  card.addEventListener('toggle', () => {
    if (card.open) page.querySelectorAll('.insight-card[open]').forEach(other => { if (other !== card) other.open = false; });
    card.querySelectorAll('textarea').forEach(resize);
  });
  const form = card.querySelector('form');
  const input = form.querySelector('textarea');
  input.value = drafts[draftId(form)]?.text || '';
  if (drafts[draftId(form)]?.operation) form.elements.operation_id.value = drafts[draftId(form)].operation;
  function changed() {
    resize(input);
    if (form.dataset.insightAction === 'interesting') form.querySelector('button').disabled = !input.value.trim();
  }
  input.addEventListener('input', () => { changed(); save(); });
  changed();
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.dataset.busy) return;
    const body = new FormData(form);
    if (event.submitter?.name) body.set(event.submitter.name, event.submitter.value);
    form.dataset.busy = 'true';
    const buttons = [...form.querySelectorAll('button')]; buttons.forEach(button => button.disabled = true);
    const error = form.querySelector('[role=alert]'); error.hidden = true;
    save();
    try {
      const response = await fetch(form.getAttribute('action'), {method: 'POST', body, headers: {'X-Requested-With': 'insight'}});
      if (!response.ok) throw new Error(await response.text());
      delete drafts[draftId(form)]; sessionStorage.setItem(draftKey, JSON.stringify(drafts));
      if (response.status === 204) {
        const height = card.getBoundingClientRect().height;
        await card.animate([{height: `${height}px`, opacity: 1}, {height: '0px', opacity: 0}], {duration: 180}).finished;
        card.remove();
        page.querySelectorAll('.insight-number').forEach((number, index) => number.textContent = index + 1);
        const count = page.querySelectorAll('.insight-card').length;
        page.querySelector('[data-insight-count]').textContent = page.dataset.insightState === 'pending' ? `${count} 条值得看看` : `有 ${count} 条新知可以再琢磨一下`;
        page.querySelector('[data-empty]').hidden = count > 0;
      } else {
        const html = new DOMParser().parseFromString(await response.text(), 'text/html');
        const next = html.querySelector('.insight-card'); card.replaceWith(next); bind(next);
      }
    } catch (failure) {
      error.textContent = failure.message || '暂时无法确认保存结果，请保留输入后重试。'; error.hidden = false;
    } finally { delete form.dataset.busy; buttons.forEach(button => button.disabled = false); changed(); }
  });
}
function save() {
  page.querySelectorAll('form[data-insight-action]').forEach(form => {
    drafts[draftId(form)] = {text: form.elements.text.value, operation: form.elements.operation_id.value};
  });
  sessionStorage.setItem(draftKey, JSON.stringify(drafts));
  sessionStorage.setItem(key, JSON.stringify({open: page.querySelector('.insight-card[open]')?.id, scroll: scroll.scrollTop}));
}
page.querySelectorAll('.insight-card').forEach(bind);
const previous = JSON.parse(sessionStorage.getItem(key) || '{}');
const target = document.getElementById(location.hash.slice(1)) || (previous.open && document.getElementById(previous.open));
if (target) { target.open = true; requestAnimationFrame(() => scroll.scrollTop = location.hash ? target.offsetTop - scroll.offsetTop : (previous.scroll || 0)); }
window.addEventListener('pagehide', save);
