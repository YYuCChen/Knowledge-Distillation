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

// Patch stable nodes in place: a response must not reset a live audio/input node.
function nodeKey(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  if (node.dataset.syncKey) return `key:${node.dataset.syncKey}`;
  if (node.id) return `id:${node.id}`;
  if (node.dataset.persistDetails) return `details:${node.dataset.persistDetails}`;
  if (node.matches('audio')) return `audio:${node.dataset.audioIdentity || node.getAttribute('src')}`;
  if (node.matches('form')) return `form:${node.getAttribute('action')}:${node.querySelector('[name="concern_id"]')?.value || ''}:${node.querySelector('[name="action"]')?.value || ''}:${node.querySelector('button[name="value"]')?.value || ''}`;
  return null;
}

function retainsLocalWork(node) {
  return node.nodeType === Node.ELEMENT_NODE &&
    (node.hasAttribute('data-stale-confirmation') || node.querySelector('[data-stale-confirmation]'));
}

function preserveCompletedCards(current, next, submittedCard) {
  for (const card of current.querySelectorAll('.todo-card-shell[data-sync-key^="member-"]')) {
    if (card.dataset.syncKey === submittedCard ||
        next.querySelector(`[data-sync-key="${CSS.escape(card.dataset.syncKey)}"]`)) continue;
    const hasDraft = Array.from(card.querySelectorAll('.manual-confirmation [name="value"]')).some(input => input.value);
    const playing = Array.from(card.querySelectorAll('audio')).some(audio => !audio.paused && !audio.ended);
    if (!hasDraft && !playing && !card.hasAttribute('data-stale-confirmation')) continue;
    card.dataset.staleConfirmation = 'true';
    card.title = '已在另一端处理，草稿可复制。';
    for (const input of card.querySelectorAll('input:not([type="hidden"]), textarea')) input.readOnly = true;
    for (const button of card.querySelectorAll('button:not([data-card-toggle])')) button.disabled = true;
  }
  const todo = current.querySelector('[data-sync-key="todo"]');
  if (todo && retainsLocalWork(todo) && !next.querySelector('[data-sync-key="todo"]')) {
    for (const card of todo.querySelectorAll('.todo-card-shell:not([data-stale-confirmation])')) card.remove();
    const heading = todo.querySelector('h2');
    if (heading) heading.textContent = '已处理';
    const summary = todo.querySelector('.summary-text');
    if (summary) summary.textContent = '已在另一端处理，草稿可复制。';
  }
}

function reconcile(current, next) {
  if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) {
    current.replaceWith(next.cloneNode(true)); return;
  }
  if (current.nodeType !== Node.ELEMENT_NODE) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }
  if (current.matches('audio')) {
    if (current.dataset.audioRevision !== next.dataset.audioRevision || current.getAttribute('src') !== next.getAttribute('src')) {
      let choice = current.nextElementSibling;
      if (!choice?.hasAttribute('data-audio-switch')) {
        choice = document.createElement('button');
        choice.type = 'button'; choice.dataset.audioSwitch = 'true';
        choice.className = 'secondary-small';
        current.after(choice);
      }
      choice.textContent = '原音已更新，切换回听';
      choice.onclick = () => {
        current.pause(); current.src = next.getAttribute('src');
        current.dataset.audioRevision = next.dataset.audioRevision || '';
        current.load(); choice.remove();
      };
    }
    return;
  }
  const editing = current.matches('input:not([type="hidden"]), textarea');
  for (const attribute of Array.from(current.attributes)) {
    if (!next.hasAttribute(attribute.name) && !(editing && attribute.name === 'value') &&
        !(current.matches('details') && attribute.name === 'open')) current.removeAttribute(attribute.name);
  }
  for (const attribute of next.attributes) {
    if (editing && attribute.name === 'value') continue;
    if (current.matches('details') && attribute.name === 'open') continue;
    if (current.hasAttribute('data-card-toggle') && attribute.name === 'aria-expanded') continue;
    if (current.id?.startsWith('actions-') && attribute.name === 'hidden') continue;
    if (current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name, attribute.value);
  }
  if (editing) return;
  const old = Array.from(current.childNodes);
  const keyed = new Map(old.map(node => [nodeKey(node), node]).filter(([key]) => key));
  const incomingKeys = new Set(Array.from(next.childNodes).map(nodeKey).filter(Boolean));
  for (const node of old) {
    if (nodeKey(node) && !incomingKeys.has(nodeKey(node)) && !node.hasAttribute?.('data-audio-switch') && !retainsLocalWork(node)) node.remove();
  }
  const used = new Set();
  const serverNode = node => { while (node?.hasAttribute?.('data-audio-switch')) node = node.nextSibling; return node; };
  let cursor = serverNode(current.firstChild);
  for (const incoming of Array.from(next.childNodes)) {
    const key = nodeKey(incoming);
    let match = key ? keyed.get(key) : old.find(node => !used.has(node) && !nodeKey(node) && !node.hasAttribute?.('data-audio-switch') && node.nodeName === incoming.nodeName);
    if (used.has(match)) match = null;
    if (!match) match = incoming.cloneNode(true);
    else reconcile(match, incoming);
    used.add(match);
    if (match !== cursor) {
      if (match.parentNode === current && typeof current.moveBefore === 'function') current.moveBefore(match, cursor);
      else current.insertBefore(match, cursor);
    }
    cursor = serverNode(match.nextSibling);
  }
  for (const node of old) if (!used.has(node) && node.parentNode === current && !node.hasAttribute?.('data-audio-switch') && !retainsLocalWork(node)) node.remove();
}

function applyPage(html, submittedForm, submittedCard) {
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
  if (status) reconcile(document.querySelector('.topbar-status'), status);
  if (next.innerHTML === lastServerHTML && !submittedForm) return;
  const anchors = Array.from(current.querySelectorAll('[data-sync-key^="member-"], [data-sync-key^="task-"], [data-sync-key^="group-"]'))
    .filter(node => node.getBoundingClientRect().bottom > 0);
  const anchor = anchors.filter(node => !anchors.some(child => child !== node && node.contains(child))).find(node => page.querySelector(`[data-sync-key="${CSS.escape(node.dataset.syncKey)}"]`));
  const anchorTop = anchor?.getBoundingClientRect().top;
  const active = document.activeElement;
  const activeCard = active?.closest?.('[data-sync-key]');
  const activeIndex = anchors.indexOf(activeCard);
  for (const form of current.querySelectorAll('form[id]')) {
    if (form.elements.value?.value) drafts.set(form.id, form.elements.value.value);
  }
  lastServerHTML = next.innerHTML;
  preserveCompletedCards(current, next, submittedCard);
  reconcile(current, next);
  if (submittedForm) {
    drafts.delete(submittedForm);
    const input = document.getElementById(submittedForm)?.elements.value;
    if (input) input.value = '';
  }
  for (const [id, value] of drafts) {
    const input = document.getElementById(id)?.elements?.value;
    if (input && !input.value) input.value = value;
  }
  for (const form of current.querySelectorAll('[data-group-confirmation]')) {
    const count = form.querySelector('[data-selection-count]');
    if (count) count.textContent = form.querySelectorAll('[name="selected_member_uids"]:checked').length;
  }
  if (active && !active.isConnected && activeIndex >= 0) {
    const surviving = [...anchors.slice(activeIndex + 1), ...anchors.slice(0, activeIndex).reverse()].find(node => node.isConnected);
    const target = surviving?.querySelector('input:not([type="hidden"]), button:not([disabled]), summary, a');
    target?.focus({preventScroll: true});
  }
  observeFragments();
  if (anchor?.isConnected) window.scrollBy(0, anchor.getBoundingClientRect().top - anchorTop);
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

document.addEventListener('change', event => {
  const form = event.target.closest('[data-group-confirmation]');
  if (!form) return;
  const count = form.querySelector('[data-selection-count]');
  if (count) count.textContent = form.querySelectorAll('[name="selected_member_uids"]:checked').length;
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
  if (form.closest('[data-stale-confirmation]')) return;
  if (updating) return;
  if ((form.matches('.manual-confirmation') || (form.matches('[data-group-confirmation]') && event.submitter?.value === 'manual')) && !form.elements.value.value.trim()) {
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
  const recovering = form.matches('[data-recover-audio]');
  const buttonLabel = suggesting || recovering ? button?.textContent : null;
  if (button) {
    button.disabled = true;
    if (suggesting) button.textContent = '正在结合上下文生成候选…';
    if (recovering) button.textContent = '正在恢复局部原音…';
  }
  try {
    const response = await fetch(button?.getAttribute('formaction') || form.getAttribute('action'), { method: 'POST', body: data });
    const html = await response.text();
    if (response.status >= 500) throw new Error(response.headers.get('Content-Type')?.startsWith('text/plain') ? html : '处理暂时失败，已保留输入，请稍后再试。');
    if (!response.ok && !html.includes('id="home-results"')) throw new Error(html);
    applyPage(html, response.ok ? form.id : null,
      response.ok ? form.closest('.todo-card-shell')?.dataset.syncKey : null);
  } catch (error) {
    await window.kdDialog(error.message);
  } finally {
    updating = false;
    if (button) {
      button.disabled = Boolean(button.closest('[data-stale-confirmation]'));
      if (suggesting || recovering) button.textContent = buttonLabel;
    }
  }
});

let pollTimer;
async function pollStatus() {
  clearTimeout(pollTimer);
  if (!polling && !updating && document.querySelector('#home-results')) {
    polling = true;
    const generation = actionGeneration;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(window.location.href, {cache: 'no-store', signal: controller.signal});
      const html = response.ok ? await response.text() : null;
      if (html && !updating && generation === actionGeneration) applyPage(html);
    } catch (_) {
      // Keep the current view and retry; an offline response cannot clear drafts.
    } finally {
      clearTimeout(timeout);
      polling = false;
    }
  }
  pollTimer = setTimeout(pollStatus, document.hidden ? 15000 : 2000);
}
pollTimer = setTimeout(pollStatus, 2000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollStatus(); });
window.addEventListener('online', pollStatus);

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
  const split = value => typeof Intl.Segmenter === 'function'
    ? Array.from(new Intl.Segmenter(undefined, {granularity: 'grapheme'}).segment(value), part => part.segment)
    : (value ? [value] : []);
  const before = split(fragment.dataset.contextBefore);
  const after = split(fragment.dataset.contextAfter);
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
