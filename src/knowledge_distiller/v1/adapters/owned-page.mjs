// Minimal Page surface used by our existing raw readers and their pinned helpers.
// This transport only connects to the application-owned endpoint supplied by Python.
export class OwnedPage {
  static async create(endpoint) {
    const page = new OwnedPage();
    page.socket = new WebSocket(endpoint);
    page.pending = new Map(); page.nextId = 0;
    page.loadedDocuments = new Set();
    page.socket.addEventListener('message', event => {
      const result = JSON.parse(event.data);
      if (result.sessionId === page.session && result.method === 'Page.lifecycleEvent' && result.params.name === 'load')
        page.loadedDocuments.add(result.params.loaderId);
      if (result.sessionId === page.session && result.method === 'Page.frameNavigated' && !result.params.frame.parentId)
        page.mainDocument = result.params.frame.loaderId;
      const pending = page.pending.get(result.id);
      if (!pending) return;
      page.pending.delete(result.id); clearTimeout(pending.timer);
      result.error ? pending.reject(Error('owned_browser_rpc_failed')) : pending.resolve(result.result || {});
    });
    await new Promise((resolve, reject) => {
      page.socket.addEventListener('open', resolve, {once:true});
      page.socket.addEventListener('error', () => reject(Error('owned_browser_connection_failed')), {once:true});
    });
    try {
      page.target = (await page.call('Target.createTarget', {url:'about:blank'}, false)).targetId;
      page.session = (await page.call('Target.attachToTarget', {targetId:page.target, flatten:true}, false)).sessionId;
      await page.call('Runtime.enable');
      await page.call('Page.enable');
      await page.call('Page.setLifecycleEventsEnabled', {enabled:true});
      return page;
    } catch (error) { await page.closeWindow(); throw error; }
  }
  call(method, params={}, scoped=true) {
    const id = ++this.nextId;
    return new Promise((resolve,reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(Error('owned_browser_timeout')); }, 25000);
      this.pending.set(id,{resolve,reject,timer});
      this.socket.send(JSON.stringify({id,method,params,...(scoped?{sessionId:this.session}:{})}));
    });
  }
  async evaluate(expression) {
    const source = expression.trim();
    // OpenCLI accepts either an expression or a function body to invoke.
    const code = `(async()=>{const value=(${source});return typeof value==='function'?await value():await value;})()`;
    const result = await this.call('Runtime.evaluate',{expression:code,returnByValue:true,awaitPromise:true});
    if (result.exceptionDetails) throw Error('owned_browser_evaluate_failed');
    return result.result?.value;
  }
  async goto(url) {
    this.loadedDocuments.clear();
    this.mainDocument = null;
    const navigation = await this.call('Page.navigate',{url});
    if (navigation.errorText || navigation.isDownload) throw Error('owned_browser_navigation_failed');
    // A completed old document must never satisfy a new navigation. Lifecycle
    // events can arrive before the navigate response, so collect them first.
    if (!navigation.loaderId) return; // Successful same-document navigation.
    for (let i=0;i<100;i++) {
      if (this.loadedDocuments.has(navigation.loaderId)) return;
      // A site may immediately navigate the main frame to a security/login
      // page. Its new document completes instead of the requested loader.
      if (this.mainDocument && this.loadedDocuments.has(this.mainDocument)) return;
      await this.wait(.2);
    }
    throw Error('owned_browser_navigation_timeout');
  }
  async wait(options) {
    if (typeof options === 'number') return new Promise(r=>setTimeout(r,options*1000));
    if (options.time !== undefined) return this.wait(options.time);
    if (options.selector) {
      for(let i=0;i<40;i++) {if(await this.evaluate(`Boolean(document.querySelector(${JSON.stringify(options.selector)}))`)) return; await this.wait(.5);}
      throw Object.assign(Error('login_required'),{code:'AUTH_REQUIRED'});
    }
  }
  async getCookies({url}) {return (await this.call('Network.getCookies',{urls:[url]})).cookies || [];}
  async closeWindow() {
    try {if(this.target) await this.call('Target.closeTarget',{targetId:this.target},false);} finally {
      for(const item of this.pending.values()) {clearTimeout(item.timer);item.reject(Error('owned_browser_closed'));}
      this.pending.clear(); this.socket.close();
    }
  }
}
