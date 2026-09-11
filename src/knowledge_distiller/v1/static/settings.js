(() => {
  const addressForm = document.getElementById('local-address-form');
  if (addressForm) {
    const preview = document.getElementById('local-address-preview');
    const updateAddress = () => {
      const name = addressForm.elements.namedItem('name').value.trim().toLowerCase();
      preview.textContent = `http://${name || '…'}.localhost:${addressForm.elements.namedItem('port').value || '…'}/`;
    };
    addressForm.addEventListener('input', updateAddress);
    updateAddress();
  }
  const form = document.getElementById('llm-form');
  const provider = document.getElementById('llm-provider');
  const model = document.getElementById('llm-model');
  const picker = document.getElementById('llm-model-picker');
  const speed = document.getElementById('llm-speed');
  const effort = document.getElementById('llm-effort');
  const models = JSON.parse(document.getElementById('codex-model-data').textContent);
  const activeEffort = form.dataset.activeEffort || '';
  picker.value = models.some(row => row.model === model.value) ? model.value : '__custom__';
  let previousModel = '';
  function update() {
    const codex = provider.value === 'codex';
    form.dataset.provider = provider.value;
    document.getElementById('llm-key-form').hidden = codex;
    document.querySelectorAll('[data-api-field]').forEach(row => {
      row.hidden = codex;
      const input = row.querySelector('input');
      if (input) { input.disabled = codex; input.required = !codex; }
    });
    document.getElementById('llm-picker-row').hidden = !codex;
    document.getElementById('llm-custom-row').hidden = codex && picker.value !== '__custom__';
    const selected = codex ? models.find(row => row.model === model.value) : undefined;
    if (previousModel !== provider.value + model.value) {
      effort.replaceChildren(new Option('默认', ''));
      (selected?.efforts || []).forEach(value => effort.add(new Option(value, value)));
      effort.value = (selected?.efforts || []).includes(activeEffort) ? activeEffort : '';
      previousModel = provider.value + model.value;
    }
    document.getElementById('llm-effort-row').hidden = !selected?.efforts.length;
    effort.disabled = !selected?.efforts.length;
    document.getElementById('llm-speed-row').hidden = !codex;
    speed.disabled = !codex;
    speed.querySelector('[value="fast"]').disabled = !selected?.fast_supported;
    if (!selected?.fast_supported) speed.value = '';
    document.getElementById('llm-save').disabled = !form.checkValidity() || (!codex && form.dataset.apiKeySaved !== 'true');
  }
  picker.addEventListener('change', () => {
    model.value = picker.value === '__custom__' ? '' : picker.value;
    update();
  });
  provider.addEventListener('change', update);
  form.addEventListener('input', update);
  update();
  const asr = document.getElementById('asr-provider');
  function updateAsr() {
    const seed = asr.value === 'doubao';
    document.getElementById('doubao-credentials').hidden = !seed;
    document.getElementById('qwen-component').hidden = seed;
    const form = document.getElementById('asr-form');
    document.getElementById('asr-save').hidden = seed || form.dataset.qwenReady !== 'true' || document.getElementById('qwen-component').dataset.active === 'true';
    document.getElementById('asr-save').disabled = seed ? form.dataset.seedReady !== 'true' : form.dataset.qwenReady !== 'true';
  }
  const component = document.getElementById('qwen-component');
  const sizeLabel = bytes => bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${(bytes / 1048576).toFixed(1)} MB`;
  function renderComponent(status) {
    const active = component.dataset.active === 'true' && status.ready;
    document.getElementById('qwen-component-status').textContent = status.label;
    document.getElementById('asr-form').dataset.qwenReady = String(status.ready);
    const button = document.getElementById('qwen-install');
    button.disabled = !status.can_install;
    button.hidden = status.ready || status.state === 'unsupported';
    button.textContent = status.busy ? '安装中…' : ['failed','interrupted'].includes(status.state) ? '继续安装' : '下载并安装 Qwen';
    document.getElementById('qwen-step').textContent = status.state === 'unsupported' ? '' : active ? '3 / 3 · 首次使用' : status.ready ? '2 / 3 · 启用' : '1 / 3 · 安装';
    document.getElementById('qwen-heading').textContent = active ? 'Qwen 已启用' : status.ready ? '安装完成，下一步启用' : '安装本地语音识别';
    document.getElementById('qwen-install-info').hidden = status.ready || status.state === 'unsupported';
    document.getElementById('qwen-disk').textContent = status.free_bytes == null || status.ready ? '' : `当前磁盘可用 ${sizeLabel(status.free_bytes)}（安装还需解压与缓存空间）`;
    document.getElementById('qwen-install-detail').textContent = status.detail || '';
    document.getElementById('qwen-next-hint').textContent = active ? '投递一段短音视频，完成首次识别。' : status.ready ? `${status.self_test_passed ? '本机识别自检通过。' : ''}点击“启用 Qwen”，后续音视频将使用本机识别。` : status.state === 'interrupted' ? '安装已暂停；点击继续，无需从头配置。' : status.busy ? '可离开本页，请保持程序运行。当前模型不会被更换。' : '';
    document.getElementById('qwen-first-use').hidden = !active;
    const progress = status.progress || {};
    const showProgress = status.busy && Number.isFinite(progress.completed_bytes);
    document.getElementById('qwen-download-progress').hidden = !showProgress;
    const bar = document.getElementById('qwen-progress-bar');
    if (showProgress) {
      const total = progress.total_bytes;
      if (Number.isFinite(total) && total > 0) {
        bar.value = Math.min(100, progress.completed_bytes / total * 100);
        document.getElementById('qwen-progress-text').textContent = `${status.state === 'downloading_model' ? '模型文件已就绪' : '已下载'} ${sizeLabel(progress.completed_bytes)} / ${sizeLabel(total)}`;
      } else {
        bar.removeAttribute('value');
        document.getElementById('qwen-progress-text').textContent = `已下载 ${sizeLabel(progress.completed_bytes)}`;
      }
    }
    updateAsr();
  }
  async function pollComponent() {
    try {
      const response = await fetch(component.dataset.statusUrl, {cache:'no-store'});
      if (!response.ok) throw Error('status_unavailable');
      const status = await response.json();
      renderComponent(status);
      if (status.busy) setTimeout(pollComponent, 1500);
    } catch (_) {
      document.getElementById('qwen-install-detail').textContent = '暂时无法读取安装进度，正在重试；请保持程序运行。';
      setTimeout(pollComponent, 3000);
    }
  }
  const initialComponent = document.getElementById('qwen-initial-status');
  if (initialComponent) renderComponent(JSON.parse(initialComponent.textContent));
  if (component.dataset.busy === 'true') setTimeout(pollComponent, 1500);
  asr.addEventListener('change', updateAsr);
  updateAsr();
})();

// Full-page settings commands get the same in-flight state as inline actions.
// Restore the original disabled state when returning through the browser cache.
const submittedButtons = new Map();
document.addEventListener('submit', event => {
  if (event.defaultPrevented || !event.target.closest('.settings-page')) return;
  [...event.target.elements].filter(element => element.tagName === 'BUTTON').forEach(button => {
    submittedButtons.set(button, {disabled:button.disabled, text:button.textContent});
    if (button.dataset.busyLabel) button.textContent = button.dataset.busyLabel;
    button.disabled = true;
  });
});
window.addEventListener('pageshow', () => {
  submittedButtons.forEach((original, button) => { button.disabled = original.disabled; button.textContent = original.text; });
  submittedButtons.clear();
});

const pairingStatus = document.getElementById('feishu-pairing-status');
if (pairingStatus) {
  const pollPairing = async () => {
    try {
      const response = await fetch(pairingStatus.dataset.statusUrl, {cache:'no-store'});
      if (!response.ok) throw Error('connection unavailable');
      const status = await response.json();
      if (status.binding?.app_id) {
        pairingStatus.textContent = '私聊绑定已完成。现在可向机器人发送正文或链接，检查投递回执；刷新本页可查看连接配置。';
        document.querySelector('.feishu-pairing-code')?.setAttribute('hidden', '');
        return;
      }
      if (!status.pairing?.code || status.state === 'pairing_expired') {
        pairingStatus.textContent = '绑定口令已过期，请在第 2 步重新生成；密钥可留空。';
        document.querySelector('.feishu-pairing-code')?.setAttribute('hidden', '');
        return;
      }
      setTimeout(pollPairing, 3000);
    } catch (_) {
      pairingStatus.textContent = '暂时无法读取绑定状态，正在重试；已有绑定保留。';
      setTimeout(pollPairing, 5000);
    }
  };
  setTimeout(pollPairing, 3000);
}

const copyPairing = document.querySelector('[data-copy-pairing]');
copyPairing?.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(document.getElementById('feishu-pairing-code').textContent);
    copyPairing.textContent = '口令已复制';
  } catch (_) { copyPairing.textContent = '请手动复制'; }
});

// Show one onboarding action at a time; browsing steps never marks binding complete.
document.querySelectorAll('[data-setup-wizard]').forEach(onboarding => {
  const panels = [...onboarding.querySelectorAll('[data-setup-step]')];
  const doubao = onboarding.dataset.storageKey === 'doubao';
  const paired = onboarding.dataset.pairing === 'true';
  const maximum = doubao ? Number(onboarding.dataset.maxStep) : (paired ? 4 : 2);
  const storageKey = doubao ? 'doubao-onboarding-step' : 'feishu-onboarding-step:' + (onboarding.dataset.appId || 'new');
  let step = doubao ? Number(onboarding.dataset.defaultStep) : (paired ? 3 : 1);
  try {
    const saved = Number(sessionStorage.getItem(storageKey));
    if (saved >= 1 && saved <= maximum) step = saved;
  } catch (_) {}
  const query = new URLSearchParams(location.search);
  if (doubao) {
    const requested = Number(query.get('asr_step'));
    if (requested >= 1 && requested <= maximum) step = requested;
  } else {
    if (query.get('message') === 'feishu_pairing_started') step = 3;
    if (query.get('message') === 'feishu_configuration_failed') step = 2;
  }
  function show(next) {
    if (next < 1 || next > maximum) return;
    step = next;
    panels.forEach(panel => { panel.hidden = Number(panel.dataset.setupStep) !== step; });
    onboarding.querySelector('[data-setup-progress]').textContent = `第 ${step} / 4 步`;
    onboarding.querySelector('[data-setup-back]').hidden = step === 1;
    try { sessionStorage.setItem(storageKey, String(step)); } catch (_) {}
  }
  onboarding.querySelectorAll('[data-setup-next]').forEach(button => {
    button.addEventListener('click', () => show(Number(button.dataset.setupNext)));
  });
  onboarding.querySelector('[data-setup-back]').addEventListener('click', () => show(step - 1));
  show(step);
});

// On discovery failure keep the user's inputs in the existing DOM, never in a URL/storage.
document.querySelectorAll('form[data-discover-storage]').forEach(form => {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const buttons = [...form.elements].filter(e => e.tagName === 'BUTTON');
    const originals = buttons.map(b => ({button:b, text:b.textContent, disabled:b.disabled}));
    let status = form.querySelector('[data-discovery-error]');
    if (!status) {
      status = document.createElement('p');status.dataset.discoveryError='';status.setAttribute('role','status');
      form.append(status);
    }
    status.textContent = '正在读取存储桶…';
    originals.forEach(({button}) => { button.disabled=true; if(button.dataset.busyLabel) button.textContent=button.dataset.busyLabel; });
    try {
      const response = await fetch(form.action,{method:'POST',body:new FormData(form),headers:{Accept:'application/json'}});
      const result = await response.json();
      if (response.ok && result.redirect) { location.assign(result.redirect); return; }
      status.textContent = result.error || '读取失败，请重试；已填内容仍保留。';
    } catch (_) { status.textContent = '连接暂时中断，请重试；已填内容仍保留。'; }
    finally { originals.forEach(({button,text,disabled}) => { button.textContent=text;button.disabled=disabled; }); }
  });
});
