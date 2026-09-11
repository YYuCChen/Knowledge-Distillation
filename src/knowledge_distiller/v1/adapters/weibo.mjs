// Keep native status/long-text payloads and render the exact native article page.
import {pathToFileURL} from 'node:url';
import {readerPage} from './reader-page.mjs';
const [root, url, boundContext, endpoint] = process.argv.slice(2);
const base = pathToFileURL(root + '/');
const {getRegistry} = await import(new URL('dist/src/registry.js', base));
let page;
async function nativeGet(path) {
  const result = await page.evaluate(`(async () => {
    const r = await fetch(${JSON.stringify('https://weibo.com' + path)}, {credentials:'include'});
    return {status:r.status, body:await r.text()};
  })()`);
  if (result?.status === 401) throw Error('weibo_login_required');
  if (result?.status === 403) throw Error('weibo_source_unavailable');
  if (result?.status !== 200 || typeof result.body !== 'string') throw Error('weibo_upstream_failed');
  return result;
}
async function article(id) {
  const canonical = 'https://weibo.com/ttarticle/p/show?id=' + encodeURIComponent(id);
  await page.goto(canonical);
  // Native page scripts render contentBody from article_content. Wait for that
  // exact container, never substitute a search excerpt or another article.
  for (let i = 0; i < 30; i++) {
    if (await page.evaluate(`Boolean(document.querySelector('[node-type="contentBody"]')?.textContent.trim())`)) break;
    await page.wait(.2);
  }
  return page.evaluate(`(() => {
    const root = document.querySelector('[node-type="contentBody"]');
    const config = window.$CONFIG || {};
    const result = {pageUrl:location.href, requestedId:${JSON.stringify(id)},
      title:document.querySelector('[node-type="articleTitle"]')?.textContent.trim(),
      isMask:config.isMask, isPay:config.isPay, authorId:config.oid,
      authorName:config.onick, publishedAt:document.querySelector('.authorinfo .time')?.textContent.trim()};
    if (!root) return result;
    result.unsupportedMedia = Boolean(root.querySelector('video,audio,iframe,embed,object'));
    result.html = root.innerHTML;
    const copy = root.cloneNode(true);
    copy.querySelectorAll('script,style,noscript').forEach(e=>e.remove());
    result.images = [...copy.querySelectorAll('img')].map((e,i)=> {
      const source = e.getAttribute('data-src') || e.getAttribute('src');
      const image = {id:'image-'+(i+1), url:source ? new URL(source,location.href).href : '', alt:e.getAttribute('alt') || ''};
      e.replaceWith(document.createTextNode('〔图片 '+(i+1)+'〕'));
      return image;
    });
    // Detached innerText loses layout, so keep native paragraph/table breaks.
    copy.querySelectorAll('br').forEach(e=>e.replaceWith(document.createTextNode('\\n')));
    copy.querySelectorAll('p,div,section,li,h1,h2,h3,h4,h5,h6,tr,blockquote,pre').forEach(e=>e.append(document.createTextNode('\\n')));
    copy.querySelectorAll('td,th').forEach(e=>e.append(document.createTextNode('\\t')));
    result.text = copy.textContent.replace(/[ \\t]+\\n/g,'\\n').replace(/\\n{3,}/g,'\\n\\n').trim();
    return result;
  })()`);
}
try {
  const reader = await readerPage(root, 'weibo', boundContext, endpoint);
  page = reader.page;
  const contextId = reader.contextId;
  await import(new URL('clis/weibo/auth.js', base));
  await getRegistry().get('weibo/whoami').func(page, {});
  if (url === 'https://weibo.com/') {
    console.log(JSON.stringify({loggedIn:true,contextId}));
  } else {
    const locator = new URL(url);
    const direct = locator.pathname.startsWith('/ttarticle/');
    const articleId = direct ? (locator.searchParams.get('id') || new URLSearchParams(locator.hash.replace(/^#\/?/, '')).get('id')) : null;
    if (direct) {
      if (!/^\d+$/.test(articleId || '')) throw Error('weibo_identity_mismatch');
      console.log(JSON.stringify({loggedIn:true,contextId,requestedId:'article:'+articleId,article:await article(articleId)}));
    } else {
      const key = locator.pathname.split('/').filter(Boolean).at(-1);
      const result = await nativeGet('/ajax/statuses/show?id=' + encodeURIComponent(key));
      const status = JSON.parse(result.body);
      let longText;
      if (status.isLongText || status.is_long_text || status.truncated) {
        longText = {...await nativeGet('/ajax/statuses/longtext?id=' + encodeURIComponent(status.idstr)), requestedId:status.idstr};
      }
      let expandedArticle;
      if (status.page_info?.object_type === 'article') {
        const id = status.page_info.page_id;
        if (!/^\d+$/.test(id || '') || status.page_info.object_id !== '1022:' + id) throw Error('weibo_identity_mismatch');
        expandedArticle = await article(id);
      }
      console.log(JSON.stringify({loggedIn:true,contextId,requestedId:key,...result,longText,article:expandedArticle}));
    }
  }
} catch (error) {
  const code = error.code === 'AUTH_REQUIRED' ? 'weibo_login_required' : /^weibo_[a-z_]+$/.test(error.message) ? error.message : 'weibo_upstream_failed';
  console.log(JSON.stringify({error:code}));
  process.exitCode = 1;
} finally {
  if (page) await page.closeWindow();
}
