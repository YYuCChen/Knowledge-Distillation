/* Presence belongs to this app's page, not to the lifetime of its browser. */
(() => {
  const config = document.getElementById('desktop-state');
  if (!config) return;
  const token = JSON.parse(config.textContent);
  let page = crypto.randomUUID();
  let lifecycle = 0;
  let stopped = false;
  let controller;
  const send = (path, values = {}) => fetch(`/desktop/${path}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token, page, ...values}), keepalive: true,
  }).catch(() => {});
  async function listen() {
    const cycle = ++lifecycle;
    while (!stopped && cycle === lifecycle) {
      controller = new AbortController();
      try {
        const response = await fetch(`/desktop/events?page=${page}`, {
          headers: {'X-Desktop-Token': token}, signal: controller.signal,
        });
        if (!response.ok) return;
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
            if (event.startsWith('data: ')) {
              const {generation} = JSON.parse(event.slice(6));
              window.focus(); // Browser policy may refuse selecting a hidden tab.
              await send('ack', {generation, visible: document.visibilityState === 'visible'});
            }
          }
        }
      } catch (error) {
        if (stopped || cycle !== lifecycle) return;
      }
      if (!stopped && cycle === lifecycle) await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) send('ack', {generation: 0, visible: true});
  });
  window.addEventListener('pagehide', () => {
    stopped = true;
    lifecycle++;
    controller?.abort();
    navigator.sendBeacon('/desktop/close', new Blob([JSON.stringify({token, page})], {type:'application/json'}));
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) { page = crypto.randomUUID(); stopped = false; listen(); }
  });
  listen();
})();
