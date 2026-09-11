// Read only native payloads. Error-page text is inspected for an explicit
// restriction, never used as source content or as evidence of a logout.
export async function readZhihuPayload(page, url, kind, key) {
  await page.goto(url);
  let snapshot;
  for (let attempt = 0; attempt < (kind === 'article' ? 20 : 1); attempt++) {
    snapshot = await page.evaluate(`(() => {
      let restricted = false;
      try {
        const error = JSON.parse(document.body?.innerText || '').error;
        restricted = String(error?.code) === '40362';
      } catch {}
      return {body: document.getElementById('js-initialData')?.textContent,
              pageUrl: location.href, restricted};
    })()`);
    if (snapshot?.restricted) throw Error('zhihu_source_unavailable');
    if (snapshot?.body || kind !== 'article') break;
    await page.wait({time: 0.5});
  }
  if (kind === 'article') {
    if (!snapshot?.body) throw Error('zhihu_snapshot_unknown');
    return {body: snapshot.body, pageUrl: snapshot.pageUrl, format: 'initial_state'};
  }
  const endpoint = `https://www.zhihu.com/api/v4/${kind === 'answer' ? 'answers' : 'pins'}/${key}?include=content,author,created_time,updated_time,question`;
  const result = await page.evaluate(`(async () => {
    const r = await fetch(${JSON.stringify(endpoint)}, {credentials: 'include'});
    return {status: r.status, body: await r.text(), pageUrl: location.href};
  })()`);
  if (result?.status === 401) throw Error('zhihu_login_required');
  if (result?.status === 403) throw Error('zhihu_source_unavailable');
  if (result?.status !== 200 || typeof result.body !== 'string') throw Error('zhihu_upstream_failed');
  try {
    if (String(JSON.parse(result.body)?.error?.code) === '40362') throw Error('zhihu_source_unavailable');
  } catch (error) {
    if (error.message === 'zhihu_source_unavailable') throw error;
  }
  return result;
}
