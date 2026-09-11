(() => {
  const seed = document.querySelector('#update-state');
  if (!seed) return;
  let state = JSON.parse(seed.textContent);
  let pending = false;
  const group = document.querySelector('#version-updates');
  const text = (selector, value) => { const node = document.querySelector(selector); if (node) node.textContent = value; };
  const size = bytes => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)}GB` : `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  async function action(name, data = {}) {
    if (name === 'archive') { window.location.href = '/settings/updates/archive?token=' + encodeURIComponent(state.token); return; }
    if (pending) return;
    pending = true;
    try {
      const response = await fetch(`/settings/updates/${name}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Update-Token': state.token}, body: JSON.stringify(data)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '操作未完成，请重试。');
      state = result; render();
    } catch (error) { text('[data-update-status]', error.message); }
    finally { pending = false; }
  }
  function render() {
    document.querySelectorAll('[data-update-attention]').forEach(node => { node.hidden = !state.attention; });
    const chevron = document.querySelector('[data-update-chevron]');
    if (chevron) { chevron.src = `/static/icons/${state.attention ? 'update-attention' : 'chevron-down'}.svg`; chevron.alt = state.attention ? '有新版更新说明' : ''; chevron.classList.toggle('update-attention', state.attention); }
    if (!group) return;
    const release = state.release;
    const busy = ['checking', 'downloading', 'installing'].includes(state.phase);
    const available = release ? `新版 ${release.display_version} 可用${release.full_reason ? ' · '+release.full_reason : ''}` : '';
    const packageSize = group.querySelector('[data-update-package-size]');
    packageSize.hidden = !release;
    packageSize.textContent = release ? `更新包大小 ${size(release.selected.size)}` : '';
    const messages = {idle: state.configured ? '尚未检查更新。' : '此版本尚未配置更新源。', checking: '正在检查更新…', latest: '已是最新版本', available,
      downloading: release ? `正在下载 ${size(state.received)} / ${size(release.selected.size)}` : '正在下载…',
      downloaded: state.block_reason || `新版 ${release?.display_version || ''} 已准备好，安装后将重新打开应用。`,
      installing: '正在安装更新，完成后会重新打开。', error: state.error};
    text('[data-update-status]', messages[state.phase] || state.error);
    const fallback=group.querySelector('[data-update-full-fallback]');
    if (fallback) {
      fallback.hidden=!state.full_update_required;
      fallback.disabled=busy;
      fallback.textContent=release ? `改用完整包（${size(release.full.size)}）` : '改用完整包';
      if (state.full_update_required) text('[data-update-status]', '差量更新未能应用，当前版本已保留。尚未下载完整包。');
    }
    text('[data-update-time]', state.checked_at ? `上次检查 ${new Date(state.checked_at * 1000).toLocaleString('zh-CN')}` : '');
    const notes = group.querySelector('[data-update-description]');
    notes.hidden = !release?.notes;
    notes.textContent = release?.notes || '';
    queueMicrotask(markVisibleNotesSeen);
    const button = group.querySelector('[data-update-primary]');
    button.hidden = !!state.full_update_required;
    let next = 'check', label = '检查更新';
    if (state.phase === 'available') { next = 'download'; label = '下载更新'; }
    if (state.phase === 'downloaded') { next = state.manual_update_only ? 'archive' : 'install'; label = state.manual_update_only ? '获取完整包，手动替换' : '安装并重启'; }
    if (state.phase === 'checking') label = '检查中…';
    if (state.phase === 'downloading') label = '下载中…';
    if (state.phase === 'installing') label = '安装中…';
    if (state.phase === 'error') { next = state.retry_action || 'check'; label = next === 'download' ? '重新下载' : '重新检查'; if (next === 'install') next = 'check'; }
    button.dataset.action = next; button.textContent = label;
    button.className = next === 'check' ? 'secondary-button' : 'primary-button';
    button.disabled = !state.configured || busy || (next === 'install' && (!state.can_install || !!state.block_reason));
    group.querySelector('[data-update-recheck]').hidden = busy || next === 'check' || !state.configured;
  }
  group?.querySelector('[data-update-primary]').addEventListener('click', event => action(event.currentTarget.dataset.action));
  group?.querySelector('[data-update-full-fallback]')?.addEventListener('click', () => action('download-full'));
  group?.querySelector('[data-update-recheck]').addEventListener('click', () => action('check'));
  function markVisibleNotesSeen() {
    if (group?.open && !document.hidden && !pending && state.release && state.attention) {
      action('seen', {version: state.release.version});
    }
  }
  group?.addEventListener('toggle', markVisibleNotesSeen);
  document.addEventListener('visibilitychange', markVisibleNotesSeen);
  render();
  setInterval(async () => {
    if (pending || document.hidden) return;
    try { const response = await fetch('/settings/updates/status'); if (response.ok) { state = await response.json(); render(); } }
    catch (_) { if (state.phase === 'installing') text('[data-update-status]', '应用正在重启，请使用重新打开的页面。'); }
  }, 3000);
})();
