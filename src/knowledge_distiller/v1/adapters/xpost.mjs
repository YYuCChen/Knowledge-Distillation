// Use the installed OpenCLI TweetDetail acquisition, retaining its raw response
// before the presentation mapper drops media types or picks conversation order.
import {pathToFileURL} from 'node:url';
import {readerPage} from './reader-page.mjs';
const [root, url, boundContext, endpoint] = process.argv.slice(2);
const base = pathToFileURL(root + '/');
const {getRegistry} = await import(new URL('dist/src/registry.js', base));
let page;
try {
  const reader = await readerPage(root, 'x', boundContext, endpoint);
  page = reader.page;
  const contextId = reader.contextId;
  await page.goto('https://x.com/home');
  await page.wait({selector: '[data-testid="AppTabBar_Profile_Link"]'});
  const loggedIn = await page.evaluate(`Boolean(document.querySelector('[data-testid="AppTabBar_Profile_Link"]'))`);
  if (loggedIn !== true && loggedIn?.data !== true) throw Error('x_login_required');
  if (url === 'https://x.com/home') {
    console.log(JSON.stringify({loggedIn: true, contextId}));
  } else {
    const key = new URL(url).pathname.match(/\/status\/([0-9]+)/)?.[1];
    if (!key) throw Error('x_identity_mismatch');
    await import(new URL('clis/twitter/thread.js', base));
    const evaluate = page.evaluate.bind(page);
    const captured = Symbol('raw response captured');
    let raw;
    page.evaluate = async (...args) => {
      const result = await evaluate(...args);
      raw = result;
      throw captured; // Exact single-source task: do not acquire reply pages.
    };
    try {
      await getRegistry().get('twitter/thread').func(page, {'tweet-id': key, limit: 1});
    } catch (error) {
      if (error !== captured) throw error;
    }
    if (raw?.errors?.some(e => [32, 89, 215].includes(e.code))) throw Error('x_login_required');
    if (!raw?.data || raw.errors?.length) throw Error('x_upstream_failed');
    console.log(JSON.stringify({loggedIn: true, contextId, requestedId: key, raw}));
  }
} catch (error) {
  const code = error.code === 'AUTH_REQUIRED' ? 'x_login_required' : /^x_[a-z_]+$/.test(error.message) ? error.message : 'x_upstream_failed';
  console.log(JSON.stringify({error: code}));
  process.exitCode = 1;
} finally {
  if (page) await page.closeWindow();
}
