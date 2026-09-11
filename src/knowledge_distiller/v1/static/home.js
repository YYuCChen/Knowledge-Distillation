let updating = false;
let polling = false;
let actionGeneration = 0;
const drafts = new Map();
let lastServerHTML = document.querySelector('#home-results')?.innerHTML;

document.addEventListener('change', event => {
  if (!event.target.matches('[data-source-file]')) return;
  const file = event.target.files[0];
  if (file && !/\.(pdf|epub|md|markdown)$/i.test(file.name)) {
    window.kdDialog('请选择单个 PDF、EPUB 或 Markdown 文件。');
    event.target.value = '';
    document.querySelector('[data-source-filename]').textContent = '';
    return;
  }
  document.querySelector('[data-source-filename]').textContent = file?.name || '';
});

function applyPage(html, submittedForm) {
  const page = new DOMParser().parseFromString(html, 'text/html');
  const current = document.querySelector('#home-results');
  const next = page.querySelector('#home-results');
  if (!current || !next) throw new Error('没有收到完整页面，请稍后再试。');
  const feedback = document.querySelector('.organization-feedback');
  const nextFeedback = page.querySelector('.organization-feedback');
  if (feedback && nextFeedback) {
    feedback.textContent = nextFeedback.textContent;
    feedback.hidden = nextFeedback.hidden;
  }
  const status = page.querySelector('.topbar-status');
  if (status) document.querySelector('.topbar-status').replaceWith(status);
  if (next.innerHTML === lastServerHTML && !submittedForm) return;
  if (submittedForm) drafts.delete(submittedForm);
  current.querySelectorAll('.manual-confirmation').forEach(form => {
    if (form.id !== submittedForm) drafts.set(form.id, form.elements.value.value);
  });
  const expanded = new Map(Array.from(current.querySelectorAll('[data-card-toggle]'), b => [b.getAttribute('aria-controls'), b.getAttribute('aria-expanded')]));
  const details = new Map(Array.from(current.querySelectorAll('details[data-persist-details]'), d => [d.dataset.persistDetails, d.open]));
  const active = document.activeElement;
  const focused = active?.closest?.(".manual-confirmation");
  const focus = focused ? {id: focused.id, start: active.selectionStart, end: active.selectionEnd} : null;
  const scroll = window.scrollY;
  lastServerHTML = next.innerHTML;
  current.replaceWith(next);
  observeFragments();
  for (const [id, value] of drafts) {
    const form = document.getElementById(id);
    if (form) form.elements.value.value = value;
  }
  next.querySelectorAll('[data-card-toggle]').forEach(button => {
    const id = button.getAttribute('aria-controls');
    if (expanded.has(id) && !document.getElementById(id).querySelector('[aria-invalid="true"]')) {
      const open = expanded.get(id) === 'true';
      button.setAttribute('aria-expanded', String(open));
      document.getElementById(id).hidden = !open;
    }
  });
  next.querySelectorAll('details[data-persist-details]').forEach(d => {
    if (details.has(d.dataset.persistDetails) && !d.querySelector('[aria-invalid="true"]')) d.open = details.get(d.dataset.persistDetails);
  });
  if (focus) {
    const input = document.getElementById(focus.id)?.elements.value;
    if (input) { input.focus({preventScroll:true}); if (focus.start !== null) input.setSelectionRange(focus.start, focus.end); }
  }
  window.scrollTo(0, scroll);
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-card-toggle]');
  if (!button) return;
  const open = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(open));
  document.getElementById(button.getAttribute('aria-controls')).hidden = !open;
});

document.addEventListener('input', event => {
  if (event.target.matches('.manual-confirmation input[name="value"]')) {
    event.target.setAttribute('aria-invalid', 'false');
    event.target.placeholder = '自定义输入…';
  }
});

const intakeForm = document.querySelector('form[data-submit]');
const intakeContentDialog = document.getElementById('intake-content-dialog');
intakeContentDialog?.querySelectorAll('[data-content-kind]').forEach(button => {
  button.addEventListener('click', () => {
    intakeForm.elements.content_kind.value = button.dataset.contentKind;
    intakeContentDialog.close();
    intakeForm.requestSubmit();
  });
});
intakeContentDialog?.querySelector('[data-content-cancel]').addEventListener('click', () => intakeContentDialog.close());
if (intakeContentDialog?.hasAttribute('data-open-on-load')) intakeContentDialog.showModal();
intakeForm?.elements.content.addEventListener('input', () => {
  intakeForm.elements.content_kind.value = '';
  intakeForm.elements.processing_mode.value = '';
});
const intakeModeDialog = document.getElementById('intake-mode-dialog');
function chooseIntakeMode() {
  if (!intakeModeDialog.open) intakeModeDialog.showModal();
}
intakeModeDialog?.querySelectorAll('[data-intake-mode]').forEach(button => {
  button.addEventListener('click', () => {
    intakeForm.elements.processing_mode.value = button.dataset.intakeMode;
    intakeModeDialog.close();
    intakeForm.requestSubmit();
  });
});
intakeModeDialog?.querySelector('[data-intake-cancel]').addEventListener('click', () => intakeModeDialog.close());
intakeModeDialog?.addEventListener('close', () => {
  intakeForm.querySelector('button[type="submit"]').focus({preventScroll:true});
});
if (intakeModeDialog?.hasAttribute('data-open-on-load')) chooseIntakeMode();

document.addEventListener('submit', async event => {
  const form = event.target;
  if (form.matches('[data-submit]')) {
    const urls = form.elements.content.value.match(/https?:\/\/[^\s<>]+/g) || [];
    if (!form.elements.attachment.files.length && form.elements.content_kind?.value !== 'text' && urls.length > 1 && !form.elements.processing_mode.value) {
      event.preventDefault();
      chooseIntakeMode();
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = '正在读取';
    return;
  }
  if (!form.closest('#home-results')) return;
  event.preventDefault();
  if (updating) return;
  if (form.matches('.manual-confirmation') && !form.elements.value.value.trim()) {
    const input = form.elements.value;
    input.value = '';
    input.placeholder = '请输入正确文字';
    input.setAttribute('aria-invalid', 'true');
    input.focus();
    return;
  }
  updating = true;
  actionGeneration += 1;
  if (form.matches('[data-rerecognize]') && !await window.kdDialog('重新识别会重做本素材的转写与审阅，当前判断将重新开始。确定继续吗？', {confirm: true})) { updating = false; return; }
  const button = event.submitter;
  const data = new FormData(form);
  if (button?.name) data.append(button.name, button.value);
  const suggesting = form.matches('[data-suggest-candidates]');
  const buttonLabel = suggesting ? button?.textContent : null;
  if (button) {
    button.disabled = true;
    if (suggesting) button.textContent = '正在结合上下文生成候选…';
  }
  try {
    const response = await fetch(form.getAttribute('action'), { method: 'POST', body: data });
    const html = await response.text();
    if (response.status >= 500) throw new Error(response.headers.get('Content-Type')?.startsWith('text/plain') ? html : '处理暂时失败，已保留输入，请稍后再试。');
    if (!response.ok && !html.includes('id="home-results"')) throw new Error(html);
    applyPage(html, response.ok ? form.id : null);
  } catch (error) {
    await window.kdDialog(error.message);
  } finally {
    updating = false;
    if (button) {
      button.disabled = false;
      if (suggesting) button.textContent = buttonLabel;
    }
  }
});

async function pollStatus() {
  try {
    const generation = actionGeneration;
    const busy = polling || updating || document.querySelector('.app-dialog[open]') || Array.from(document.querySelectorAll('audio')).some(a => !a.paused);
    if (!busy && document.querySelector('[data-live-status]')) {
      polling = true;
      const response = await fetch(window.location.href, {cache: 'no-store'});
      const html = response.ok ? await response.text() : null;
      if (html && !updating && generation === actionGeneration) applyPage(html);
      polling = false;
    }
  } catch (_) { polling = false; }
  window.setTimeout(pollStatus, 2000);
}
window.setTimeout(pollStatus, 2000);

document.querySelectorAll('[data-persist-details]').forEach(d => {
  const saved = localStorage.getItem(`knowledge-distiller:home:${d.dataset.persistDetails}`);
  if (saved && !d.open) d.open = saved === 'open';
});
document.addEventListener('toggle', event => {
  const d = event.target;
  if (d.matches('details[data-persist-details]')) localStorage.setItem(`knowledge-distiller:home:${d.dataset.persistDetails}`, d.open ? 'open' : 'closed');
}, true);

// Fit context to rendered space, so Latin text is not limited by a CJK character count.
function fitFragment(fragment) {
  const width = fragment.getBoundingClientRect().width;
  if (!width) return;
  const before = Array.from(fragment.dataset.contextBefore);
  const after = Array.from(fragment.dataset.contextAfter);
  const probe = fragment.cloneNode(true);
  probe.removeAttribute('data-context-before');
  probe.removeAttribute('data-context-after');
  Object.assign(probe.style, {position: 'fixed', visibility: 'hidden',
    width: 'max-content', maxWidth: 'none', whiteSpace: 'pre', inset: '0 auto auto 0'});
  fragment.parentElement.append(probe);
  function context(count) {
    let left = Math.min(before.length, Math.ceil(count / 2));
    const right = Math.min(after.length, count - left);
    left = Math.min(before.length, count - right);
    let leading = before.slice(before.length - left).join('');
    let trailing = after.slice(0, right).join('');
    // Avoid partial English words at the outer edges; never shorten the concern.
    if (left < before.length && /[a-zA-Z0-9]/.test(before[before.length - left - 1] || ''))
      leading = leading.replace(/^[a-zA-Z0-9]+/, '');
    if (right < after.length && /[a-zA-Z0-9]/.test(after[right] || ''))
      trailing = trailing.replace(/[a-zA-Z0-9]+$/, '');
    return [left < before.length ? '…' + leading : leading,
      right < after.length ? trailing + '…' : trailing];
  }
  function render(target, count) {
    const [leading, trailing] = context(count);
    target.querySelector('[data-context-leading]').textContent = leading;
    target.querySelector('[data-context-trailing]').textContent = trailing;
  }
  let low = 0, high = before.length + after.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    render(probe, mid);
    if (probe.getBoundingClientRect().width <= width - 1) low = mid;
    else high = mid - 1;
  }
  render(fragment, low);
  probe.remove();
}
const fragmentObserver = new ResizeObserver(entries => {
  for (const entry of entries) fitFragment(entry.target);
});
function observeFragments() {
  fragmentObserver.disconnect();
  document.querySelectorAll('.source-fragment[data-context-before]').forEach(fragment => {
    fragmentObserver.observe(fragment);
    fitFragment(fragment);
  });
}
observeFragments();
document.fonts.ready.then(() => {
  document.querySelectorAll('.source-fragment[data-context-before]').forEach(fitFragment);
});

window.addEventListener('resize', () => {
  document.querySelectorAll('.source-fragment[data-context-before]').forEach(fitFragment);
});
document.addEventListener('toggle', () => {
  document.querySelectorAll('.source-fragment[data-context-before]').forEach(fitFragment);
}, true);
