const key = 'kd-reading:' + location.pathname + location.search;
const area = document.querySelector('[data-scroll-area]');
const details = [...document.querySelectorAll('[data-point]')];
let restoring = true;
function saveReading() {
  if (restoring) return;
  try { sessionStorage.setItem(key, JSON.stringify({scroll: window.scrollY, open: details.filter(d => d.open).map(d => d.dataset.point)})); } catch (_) {}
}
try {
  const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
  if (saved) {
    details.forEach(d => { d.open = saved.open.includes(d.dataset.point); });
    window.scrollTo(0, saved.scroll);
  }
} catch (_) {}
restoring = false;
window.addEventListener('scroll', saveReading, {passive:true});
details.forEach(d => d.addEventListener('toggle', saveReading));
window.addEventListener('pagehide', saveReading);
document.querySelector('input[name=q]')?.addEventListener('input', event => {
  if (!event.isComposing && !event.target.value && new URL(location.href).searchParams.get('q')?.trim()) location.assign('/topics');
});
document.querySelectorAll('[data-open-source]').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const response = await fetch(form.action, {method:'POST'});
    if (!response.ok) throw new Error(await response.text());
  } catch (error) { await window.kdDialog(error.message || '未能打开原文件副本。'); }
  finally { button.disabled = false; }
}));
