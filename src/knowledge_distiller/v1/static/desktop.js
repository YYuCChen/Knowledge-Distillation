/* App-owned document identity. No tab enumeration or additional permissions. */
(() => {
  const config = document.getElementById('desktop-state');
  if (!config) return;
  const token = JSON.parse(config.textContent);
  const storageKey = 'knowledge-distiller.desktop-page';
  let page;
  try { page = sessionStorage.getItem(storageKey); } catch (_) { /* private storage */ }
  page ||= crypto.randomUUID();
  const documentId = crypto.randomUUID();
  let epoch = null, lifecycle = 0, stopped = false, controller;
  let generation = 0, requestId = null, navigating = false;
  const currentUrl = new URL(location.href);
  let launch = currentUrl.searchParams.get('_desktop_launch') || '';
  if (launch) {
    currentUrl.searchParams.delete('_desktop_launch');
    history.replaceState(history.state, '', currentUrl);
  }
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua) ? 'edge' : /Chrome\//.test(ua) ? 'chrome' :
    /Firefox\//.test(ua) ? 'firefox' : /Safari\//.test(ua) ? 'safari' : 'unknown';
  const send = (path, values = {}) => {
    if (!epoch) return Promise.resolve();
    return fetch(`/desktop/${path}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, page, epoch, ...values}), keepalive: true,
    }).catch(() => {});
  };
  const report = (interaction = false) => send('ack', {
    generation, request_id: requestId,
    visible: document.visibilityState === 'visible', focused: document.hasFocus(), interaction,
  });
  async function listen() {
    const cycle = ++lifecycle;
    while (!stopped && cycle === lifecycle) {
      controller = new AbortController();
      try {
        const query = new URLSearchParams({page, document: documentId, route: location.pathname, browser, launch});
        const response = await fetch(`/desktop/events?${query}`, {
          headers: {'X-Desktop-Token': token}, signal: controller.signal,
        });
        if (!response.ok) {
          if (response.status === 403) {
            // An old process token cannot silently register against the new one.
            const notice = document.createElement('div');
            notice.setAttribute('role', 'status');
            notice.textContent = '应用已重新启动。请先保留未提交文字，再刷新页面以恢复连接。';
            (document.body || document.documentElement).append(notice);
          }
          return;
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let pending = '';
        while (!stopped && cycle === lifecycle) {
          const {value, done} = await reader.read();
          if (done) break;
          pending += decoder.decode(value, {stream: true});
          let boundary;
          while ((boundary = pending.indexOf('\n\n')) >= 0) {
            const event = pending.slice(0, boundary);
            pending = pending.slice(boundary + 2);
            if (!event.startsWith('data: ')) continue;
            const message = JSON.parse(event.slice(6));
            if (message.type === 'hello') {
              page = message.page; epoch = message.epoch; launch = '';
              try { sessionStorage.setItem(storageKey, page); } catch (_) { /* optional */ }
              generation = 0; requestId = null;
              await report();
            } else if (message.type === 'show') {
              generation = message.generation; requestId = message.request_id;
              window.focus(); // Request only; hidden ACK is not display success.
              await report();
            }
          }
        }
      } catch (_) {
        if (stopped || cycle !== lifecycle) return;
      }
      if (!stopped && cycle === lifecycle) await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }
  document.addEventListener('visibilitychange', () => report());
  window.addEventListener('focus', () => report());
  window.addEventListener('blur', () => report());
  let lastInteraction = 0;
  for (const name of ['pointerdown', 'keydown']) document.addEventListener(name, event => {
    if (!event.isTrusted) return;
    navigating = false;
    const now = performance.now();
    if (now - lastInteraction > 250) { lastInteraction = now; report(true); }
  }, {passive: true});
  // Known same-tab navigations reserve identity even when their response is slow.
  // An arbitrary browser-toolbar reload has no pre-navigation identity channel.
  document.addEventListener('click', event => {
    const anchor = event.target.closest?.('a[href]');
    if (!anchor || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey || anchor.download || (anchor.target && anchor.target !== '_self')) return;
    const destination = new URL(anchor.href, location.href);
    if (destination.origin === location.origin &&
        destination.pathname + destination.search !== location.pathname + location.search) {
      queueMicrotask(() => { if (!event.defaultPrevented) navigating = true; });
    }
  });
  document.addEventListener('submit', event => {
    const form = event.target;
    if ((!form.target || form.target === '_self') &&
        new URL(form.action, location.href).origin === location.origin) {
      queueMicrotask(() => { if (!event.defaultPrevented) navigating = true; });
    }
  });
  window.addEventListener('pagehide', event => {
    stopped = true; lifecycle++;
    if (epoch) navigator.sendBeacon('/desktop/close', new Blob([
      JSON.stringify({token, page, epoch, navigating: navigating && !event.persisted}),
    ], {type: 'application/json'}));
    controller?.abort();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) { stopped = false; navigating = false; epoch = null; listen(); }
  });
  listen();
})();
