// Narrow raw-state reader over the installed OpenCLI transport. Never use the
// display adapter's whitespace normalization or its DOM/media fallbacks.
import {pathToFileURL} from 'node:url';
import {readerPage} from './reader-page.mjs';
const [root, url, boundContext, endpoint] = process.argv.slice(2);
const base = pathToFileURL(root + '/dist/src/browser/');
let page;
try {
  const reader = await readerPage(root, 'xiaohongshu', boundContext, endpoint);
  page = reader.page;
  const contextId = reader.contextId;
  await page.goto(url);
  let result;
  for (let attempt = 0; attempt < 20; attempt++) {
    result = await page.evaluate(`(() => {
      const s = window.__INITIAL_STATE__;
      const id = location.pathname.split('/').filter(Boolean).pop();
      const login = s?.user?.loggedIn;
      const note = s?.note?.noteDetailMap?.[id]?.note;
      return {pageUrl: location.href, loggedIn: typeof login === 'boolean' ? login : login?.value,
        securityRestricted: location.pathname === '/website-login/error' && /安全限制|IP存在风险/.test(document.body.innerText),
        loginPrompt: Array.from(document.querySelectorAll('.login-container')).some(e => e.getClientRects().length),
        note: note ? JSON.parse(JSON.stringify(note)) : null};
    })()`);
    if (result?.securityRestricted) throw Error('xiaohongshu_security_restricted');
    if (result?.loggedIn === false || result?.loginPrompt || result?.note || (url.endsWith('/explore') && result?.loggedIn === true)) break;
    await page.wait({time: 0.5});
  }
  if (result?.loggedIn !== true || result?.loginPrompt) throw Error('xiaohongshu_login_required');
  console.log(JSON.stringify({...result, contextId}));
} catch (error) {
  const code = /^xiaohongshu_[a-z_]+$/.test(error.message) ? error.message : 'xiaohongshu_upstream_failed';
  console.log(JSON.stringify({error: code}));
  process.exitCode = 1;
} finally {
  if (page) await page.closeWindow();
}
