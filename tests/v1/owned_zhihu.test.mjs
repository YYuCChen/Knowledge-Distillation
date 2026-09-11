import test from 'node:test';
import assert from 'node:assert/strict';
import {OwnedPage} from '../../src/knowledge_distiller/v1/adapters/owned-page.mjs';
import {readZhihuPayload} from '../../src/knowledge_distiller/v1/adapters/zhihu-page.mjs';

test('second navigation cannot return a completed old document', async () => {
  const page = new OwnedPage();
  page.loadedDocuments = new Set(['old']);
  let waits = 0;
  page.call = async () => ({loaderId:'new'});
  page.evaluate = async () => true; // Old implementation incorrectly returns here.
  page.wait = async () => { if (++waits === 3) page.loadedDocuments.add('new'); };
  await page.goto('https://example.test/new');
  assert.equal(waits, 3);
});

test('early load event, same-document navigation and navigation errors', async () => {
  const page = new OwnedPage();
  page.loadedDocuments = new Set();
  page.call = async () => { page.loadedDocuments.add('new'); return {loaderId:'new'}; };
  page.wait = async () => assert.fail('load already arrived');
  await page.goto('https://example.test/new');
  page.call = async () => ({});
  await page.goto('https://example.test/new#anchor');
  page.call = async () => ({errorText:'net::ERR_CONNECTION_REFUSED'});
  await assert.rejects(page.goto('https://example.test/bad'), /navigation_failed/);
});

test('missing current load times out, even with stale load events', async () => {
  const page = new OwnedPage();
  page.loadedDocuments = new Set();
  page.call = async () => { page.loadedDocuments.add('old'); return {loaderId:'new'}; };
  page.wait = async () => {};
  await assert.rejects(page.goto('https://example.test/new'), /navigation_timeout/);
});

for (const kind of ['article', 'answer', 'pin']) {
  test(`${kind} restriction stops before polling or detail fetch`, async () => {
    let reads = 0;
    const page = {goto:async () => {}, evaluate:async expression => {
      reads++;
      const document = {body:{innerText:'{"error":{"code":40362,"message":"受限"}}'}, getElementById:() => null};
      const location = {href:'https://zhuanlan.zhihu.com/p/1'};
      return eval(expression);
    }, wait:async () => assert.fail('must not retry restriction')};
    await assert.rejects(readZhihuPayload(page, 'unused', kind, '1'), /zhihu_source_unavailable/);
    assert.equal(reads, 1);
  });
}

test('successful answer keeps exact raw integer IDs and HTML', async () => {
  let reads = 0;
  const body = '{"id":2031839365702350794,"content":"<p>正文</p>"}';
  const page = {goto:async () => {}, evaluate:async () => (++reads === 1 ? {} : {status:200,body})};
  assert.equal((await readZhihuPayload(page, 'url', 'answer', '2031839365702350794')).body, body);
});

test('article retains raw JSON and waits only for initial data, never accepts text excerpt', async () => {
  let reads = 0, waits = 0;
  const body = '{"id":2031839365702350794}';
  const page = {goto:async () => {}, evaluate:async () => (++reads === 1 ? {} : {body, pageUrl:'url'}),
    wait:async () => { waits++; }};
  assert.deepEqual(await readZhihuPayload(page, 'url', 'article', '1'), {body, pageUrl:'url', format:'initial_state'});
  assert.equal(waits, 1);
  page.evaluate = async () => ({pageUrl:'url'});
  await assert.rejects(readZhihuPayload(page, 'url', 'article', '1'), /zhihu_snapshot_unknown/);
});

for (const [status, body, code] of [[401,'{}','login_required'],[403,'{}','source_unavailable'],
  [200,'{"error":{"code":40362}}','source_unavailable'],[500,'{}','upstream_failed']]) {
  test(`native detail status ${status} ${code}`, async () => {
    let reads = 0;
    const page = {goto:async () => {}, evaluate:async () => (++reads === 1 ? {} : {status,body})};
    await assert.rejects(readZhihuPayload(page, 'url', 'answer', '1'), new RegExp(code));
    assert.equal(reads, 2);
  });
}

test('completed main-frame security redirect is observable to the reader', async () => {
  const page = new OwnedPage();
  page.loadedDocuments = new Set(['old']);
  page.mainDocument = 'old';
  page.call = async () => ({loaderId:'requested'});
  let waits = 0;
  page.wait = async () => {
    waits++;
    page.mainDocument = 'security-page';
    page.loadedDocuments.add('security-page');
  };
  await page.goto('https://example.test/note');
  assert.equal(waits, 1);
});
