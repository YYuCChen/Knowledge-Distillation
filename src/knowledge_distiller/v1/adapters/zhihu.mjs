// Fetch the authenticated browser's native text payload, retaining JSON as text
// so platform ids above 2^53 never pass through JavaScript Number rounding.
import {pathToFileURL} from 'node:url';
import {readerPage} from './reader-page.mjs';
import {readZhihuPayload} from './zhihu-page.mjs';
const [root, url, boundContext, endpoint] = process.argv.slice(2);
const base = pathToFileURL(root + '/');
const {getRegistry} = await import(new URL('dist/src/registry.js', base));
let page;
try {
  const reader = await readerPage(root, 'zhihu', boundContext, endpoint);
  page = reader.page;
  const contextId = reader.contextId;
  await import(new URL('clis/zhihu/auth.js', base));
  await getRegistry().get('zhihu/whoami').func(page, {});
  if (url === 'https://www.zhihu.com/') {
    console.log(JSON.stringify({loggedIn: true, contextId}));
  } else {
    const parsed = new URL(url);
    const answer = parsed.pathname.match(/\/(?:question\/[0-9]+\/)?answer\/([0-9]+)\/?$/);
    const article = parsed.pathname.match(/^\/p\/([0-9]+)\/?$/);
    const pin = parsed.pathname.match(/^\/pin\/([0-9]+)\/?$/);
    const kind = answer ? 'answer' : article ? 'article' : pin ? 'pin' : null;
    const key = (answer || article || pin)?.[1];
    if (!kind) throw Error('zhihu_input_unsupported');
    const result = await readZhihuPayload(page, url, kind, key);
    console.log(JSON.stringify({loggedIn: true, contextId, requestedId: key, kind, ...result}));
  }
} catch (error) {
  const code = error.code === 'AUTH_REQUIRED' ? 'zhihu_login_required' : /^zhihu_[a-z_]+$/.test(error.message) ? error.message : 'zhihu_upstream_failed';
  console.log(JSON.stringify({error: code}));
  process.exitCode = 1;
} finally {
  if (page) await page.closeWindow();
}
