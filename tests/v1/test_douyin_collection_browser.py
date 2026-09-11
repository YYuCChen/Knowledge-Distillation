import asyncio
import json
import shutil
import subprocess

import pytest

from knowledge_distiller.v1.douyin_collection_browser import (
    BrowserCollectionClient, _GALLERY_SSR, _complete_gallery, _COLLECTION_OBSERVER, _BrowserRequestDenied, _log_read,
)
from knowledge_distiller.v1.douyin_collections import CollectionError


def partial():
    return {'aweme_id': '123', 'aweme_type': 0,
            'desc': '开头……版本过低，升级后可展示全部信息',
            'images': [{'uri': 'one', 'url_list': ['https://example.org/one']},
                       {'uri': 'two', 'url_list': ['https://example.org/two']}],
            'author': {'nickname': '作者'}, 'create_time': 1234}


def complete():
    return {**partial(), 'desc': '完整正文，不是相关推荐或评论。'}


def test_complete_main_aweme_replaces_only_text_and_same_ordered_images():
    value = _complete_gallery(partial(), complete())
    assert value['desc'] == complete()['desc']
    assert value['author'] == partial()['author']
    assert value['create_time'] == 1234
    assert value['native_text_origin'] == 'douyin_note_main_aweme_ssr'


@pytest.mark.parametrize('native', [None, {}, {'aweme_id': 'other'}])
def test_missing_or_wrong_native_identity_is_incomplete(native):
    with pytest.raises(CollectionError, match='douyin_gallery_incomplete'):
        _complete_gallery(partial(), native)


def test_still_partial_native_text_is_incomplete():
    with pytest.raises(CollectionError, match='douyin_gallery_incomplete'):
        _complete_gallery(partial(), partial())


@pytest.mark.parametrize('operation', ['reorder', 'missing', 'replace'])
def test_native_gallery_image_change_cannot_be_combined_with_old_capture(operation):
    native = complete()
    if operation == 'reorder':
        native['images'].reverse()
    elif operation == 'missing':
        native['images'].pop()
    else:
        native['images'][0]['uri'] = 'new'
    with pytest.raises(CollectionError, match='source_snapshot_changed'):
        _complete_gallery(partial(), native)


def test_browser_fetches_note_only_on_explicit_truncation_signal():
    class Page:
        def __init__(self):
            self.calls = []
        def call(self, method, params, **kwargs):
            self.calls.append((method, params))
        def wait_for(self, expression):
            return True if 'document.readyState' in expression else complete()
    client = BrowserCollectionClient(object())
    client.page = Page()
    async def fetch(*_):
        return {'aweme_detail': partial()}
    client._fetch = fetch
    assert asyncio.run(client.get_video_detail('123'))['desc'] == complete()['desc']
    assert client.page.calls == [('Page.navigate', {'url': 'about:blank'}),
                                 ('Page.navigate', {'url': 'https://www.douyin.com/note/123'})]
    async def full_fetch(*_):
        return {'aweme_detail': complete()}
    client._fetch = full_fetch
    client.page.calls.clear()
    assert asyncio.run(client.get_video_detail('123')) == complete()
    assert client.page.calls == []


def _script_record(aweme_id='123', *, nested_only=False):
    detail = {'awemeId': aweme_id, 'awemeType': 68, 'desc': '完整原生正文',
              'authenticationToken': 'must-not-leak',
              'images': [{'uri': 'one', 'urlList': ['https://example.org/one'], 'video': None}]}
    props = {'awemeId': aweme_id, 'aweme': {'statusCode': 0, 'detail': detail},
             'comment': {'desc': '不能读取评论'}, 'defaultSeoRelatedAweme': {'desc': '不能读取推荐'}}
    if nested_only:
        props = {'recommendation': props}
    return 'self.__pace_f.push(' + json.dumps([1, '7:' + json.dumps(['$', '$L9', None, props]) + '\n']) + ')'


def _extract(scripts):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute browser extraction expression')
    source = 'const document={scripts:' + json.dumps([{'textContent': s} for s in scripts]) + '};\n'
    source += 'console.log(JSON.stringify(' + _GALLERY_SSR.replace('ITEM_ID', '"123"') + '))'
    result = subprocess.run([node, '-e', source], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def test_exact_ssr_main_aweme_extraction_filters_private_and_unrelated_fields():
    value = _extract([_script_record('999'), _script_record(nested_only=True), _script_record()])
    assert value['aweme_id'] == '123'
    assert value['desc'] == '完整原生正文'
    assert set(value) == {'aweme_id', 'aweme_type', 'desc', 'images'}
    assert value['images'][0]['url_list'] == ['https://example.org/one']
    assert 'must-not-leak' not in json.dumps(value)


def test_ambiguous_or_unrelated_ssr_main_aweme_is_not_accepted():
    assert _extract([_script_record(), _script_record()]) is None
    assert _extract([_script_record('999'), _script_record(nested_only=True)]) is None


def test_ssr_flight_text_reference_resolves_exact_utf8_body_without_adjacent_records():
    text = '完整正文包含中文与 emoji🤔，不接受React引用本身。'
    header = 'a:T' + format(len(text.encode('utf-8')), 'x') + ','
    text_header = 'self.__pace_f.push(' + json.dumps([1, header]) + ')'
    text_body = 'self.__pace_f.push(' + json.dumps([1, text]) + ')'
    # json.dumps emits escaped Chinese; decode/re-encode the script container.
    chunk = json.loads(_script_record()[len('self.__pace_f.push('):-1])
    row = json.loads(chunk[1][2:])
    row[3]['aweme']['detail']['desc'] = '$a'
    record = 'self.__pace_f.push(' + json.dumps([1, '7:' + json.dumps(row) + '\n']) + ')'
    assert _extract([text_header, text_body, record])['desc'] == text
    assert _extract([record]) is None


def test_ssr_escaped_literal_dollar_is_not_flight_reference():
    chunk = json.loads(_script_record()[len('self.__pace_f.push('):-1])
    row = json.loads(chunk[1][2:])
    row[3]['aweme']['detail']['desc'] = '$$a'
    record = 'self.__pace_f.push(' + json.dumps([1, '7:' + json.dumps(row) + '\n']) + ')'
    assert _extract([record])['desc'] == '$a'


def test_current_series_endpoint_reuses_first_page_and_preserves_native_mix():
    client = BrowserCollectionClient(object())
    client._mix_kinds['900'] = 'series'
    detail = {'mix_id': '900', 'mix_name': '原合集', 'author': {'sec_uid': 'creator'}}
    first = {'status_code': 0, 'has_more': 1, 'max_cursor': 6,
             'aweme_list': [{'mix_info': detail}]}
    last = {'status_code': 0, 'has_more': 0, 'max_cursor': 7, 'aweme_list': []}
    calls = []
    async def fetch(path, params):
        calls.append((path, params))
        return first if params['cursor'] == 0 else last
    client._fetch = fetch
    async def read():
        assert await client.get_mix_detail('900') == detail
        assert await client.get_mix_aweme('900', 0) == {'raw': first}
        assert await client.get_mix_aweme('900', 6) == {'raw': last}
    asyncio.run(read())
    assert calls == [('/aweme/v1/web/series/aweme/', {
        'series_id': '900', 'pull_type': 2, 'cursor': cursor,
        'source': 'playlet_homepage_hot', 'count': 6,
    }) for cursor in (0, 6)]


@pytest.mark.parametrize('rows,code', [([], 'collection_membership_incomplete'),
    ([None], 'collection_membership_incomplete'),
    ([{'mix_info': {'mix_id': '901'}}], 'collection_identity_mismatch')])
def test_series_cannot_supply_missing_or_different_collection_metadata(rows, code):
    client = BrowserCollectionClient(object())
    client._mix_kinds['900'] = 'series'
    async def fetch(*args):
        return {'status_code': 0, 'aweme_list': rows}
    client._fetch = fetch
    with pytest.raises(CollectionError, match=code):
        asyncio.run(client.get_mix_detail('900'))


def test_browser_request_keeps_uifid_inside_page_and_uses_native_header():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute browser request expression')
    class Page:
        def call(self, method, params, **kwargs):
            script = '''const document={cookie:'other=1; UIFID=session-value'};
const fetch=async(url,options)=>{
 if(options.headers.uifid!=='session-value'||options.credentials!=='include') throw Error('session missing');
 return {status:200,text:async()=>'{"status_code":0}'};
};\n'''
            script += params['expression'] + '.then(x=>console.log(JSON.stringify(x)))'
            result = subprocess.run([node, '-e', script], check=True, text=True, capture_output=True)
            return {'result': {'value': json.loads(result.stdout)}}
    client = BrowserCollectionClient(object())
    client.page = Page()
    assert asyncio.run(client._fetch('/aweme/v1/web/series/aweme/', {})) == {'status_code': 0}


def test_native_observer_records_only_collection_json_without_touching_responses():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute browser observation expression')
    script = '''globalThis.window=globalThis;
const location={href:'https://www.douyin.com/'};
class XMLHttpRequest {
 addEventListener(name, callback){this.callback=callback}
 open(method,url){this.responseURL=url;return 'opened'}
}
let cloned=0;
window.fetch=async(url)=>({url,status:200,clone(){cloned++;return {text:async()=>'{"status_code":0}'}}});
'''
    script += _COLLECTION_OBSERVER + ''';
(async()=>{
 const x=new XMLHttpRequest();
 const result=x.open('GET','https://www-hj.douyin.com/aweme/v1/web/mix/aweme/?mix_id=900&cursor=6&signature=private');
 x.status=200;x.responseText='{"status_code":0,"aweme_list":[]}';x.callback();
 await window.fetch('https://www.douyin.com/unrelated-video');
 await window.fetch('https://www.douyin.com/aweme/v1/web/series/list/?sec_user_id=creator&cursor=0');
 await new Promise(resolve=>setImmediate(resolve));
 console.log(JSON.stringify({result,cloned,events:window.__kdCollections}));
})()
'''
    result = subprocess.run([node, '-e', script], check=True, text=True, capture_output=True)
    data = json.loads(result.stdout)
    assert data['result'] == 'opened' and data['cloned'] == 1
    assert [(r['key'], r['cursor']) for r in data['events']] == [('900', 6), ('creator', 0)]
    assert 'private' not in result.stdout and 'signature' not in result.stdout


def test_profile_combines_both_native_lists_and_keeps_zero_member_counts():
    client = BrowserCollectionClient(object())
    client._native_start = lambda url: None
    class Page:
        def wait_for(self, expression): return True
        def evaluate(self, expression): pass
    client.page = Page()
    calls = []
    def native(path, key, cursor, **kwargs):
        calls.append((path, key, cursor))
        kind = 'series' if '/series/' in path else 'mix'
        identity = str(900 + cursor) if kind == 'mix' else '800'
        count = 0 if cursor else 2
        row = {kind + '_id': identity, kind + '_name': identity,
               ('stats' if kind == 'series' else 'statis'): {'updated_to_episode': count}}
        return {'status_code': 0, 'has_more': int(kind == 'mix' and cursor == 0),
                'cursor': cursor + 1, kind + '_infos': [row]}
    client._native_page = native
    result = asyncio.run(client.get_user_mix('creator', 0))['raw']
    assert result['has_more'] == 0
    assert [(r['mix_id'], r['member_count']) for r in result['mix_infos']] == [('800', 2), ('900', 2), ('901', 0)]
    assert [r[2] for r in calls] == [0, 0, 1]


def test_legacy_direct_link_preserves_all_pages_while_resolving_author_metadata():
    client = BrowserCollectionClient(object())
    client._native_start = lambda url: None
    class Page:
        def wait_for(self, expression):
            return {'path': '/aweme/v1/web/mix/aweme/'}
    client.page = Page()
    detail = {'mix_id': '900', 'statis': {'updated_to_episode': 2}}
    pages = {cursor: {'status_code': 0, 'has_more': int(cursor == 0), 'cursor': cursor + 1,
                      'aweme_list': [{'mix_info': detail, 'author': {'sec_uid': 'creator'}}]}
             for cursor in (0, 1)}
    client._native_page = lambda path, key, cursor: pages[cursor]
    async def author(key, cursor):
        assert key == 'creator'
        client._mix_metadata['900'] = {**detail, 'author': {'sec_uid': key}}
        client._native_page = lambda *args: pytest.fail('must retain pages before navigating away')
    client.get_user_mix = author
    async def read():
        assert (await client.get_mix_detail('900'))['author']['sec_uid'] == 'creator'
        assert (await client.get_mix_aweme('900', 0))['raw'] == pages[0]
        assert (await client.get_mix_aweme('900', 1))['raw'] == pages[1]
    asyncio.run(read())


@pytest.mark.parametrize('conflict', ['type', 'count', 'title', None])
def test_native_list_duplicates_must_agree_in_type_title_and_count(conflict):
    client = BrowserCollectionClient(object())
    client._native_start = lambda url: None
    class Page:
        def wait_for(self, expression): return True
        def evaluate(self, expression): pass
    client.page = Page()
    def native(path, key, cursor, **kwargs):
        kind = 'series' if '/series/' in path else 'mix'
        row = {kind + '_id': '900', kind + '_name': '合集',
               ('stats' if kind == 'series' else 'statis'): {'updated_to_episode': 2}}
        rows = [row] if kind == 'mix' or conflict == 'type' else []
        if kind == 'mix' and conflict != 'type':
            other = {**row, 'statis': {'updated_to_episode': 3 if conflict == 'count' else 2}}
            if conflict == 'title': other['mix_name'] = '改名'
            rows.append(other)
        return {'status_code': 0, 'has_more': 0, 'cursor': 0, kind + '_infos': rows}
    client._native_page = native
    if conflict:
        with pytest.raises(CollectionError, match='collection_scope_changed'):
            asyncio.run(client.get_user_mix('creator', 0))
    else:
        assert len(asyncio.run(client.get_user_mix('creator', 0))['raw']['mix_infos']) == 1


def test_denied_single_fetch_reads_the_same_native_work_page():
    client = BrowserCollectionClient(object())
    started = []
    client._native_start = started.append
    detail = {'aweme_id': '123', 'aweme_type': 0, 'desc': '原文', 'video': {'duration': 1000}}
    class Page:
        def wait_for(self, expression):
            assert 'aweme/detail/' in expression and 'x.key==="123"' in expression
            return {'status': 200, 'data': {'status_code': 0, 'aweme_detail': detail}}
    client.page = Page()
    async def denied(*args): raise _BrowserRequestDenied('collection_upstream_failed')
    client._fetch = denied
    assert asyncio.run(client.get_video_detail('123')) == detail
    assert started == ['https://www.douyin.com/video/123']


@pytest.mark.parametrize('event,code', [
    (None, 'collection_upstream_failed'),
    ({'status': 403}, 'collection_upstream_failed'),
    ({'status': 200, 'data': {'status_code': 1}}, 'collection_upstream_failed'),
    ({'status': 200, 'data': {'status_code': 0}}, 'collection_identity_mismatch'),
    ({'status': 200, 'data': {'status_code': 0, 'aweme_detail': {'aweme_id': '999'}}}, 'collection_identity_mismatch'),
])
def test_native_single_read_never_accepts_empty_failed_or_other_work(event, code):
    client = BrowserCollectionClient(object())
    client._native_start = lambda url: None
    class Page:
        def wait_for(self, expression): return event
    client.page = Page()
    async def denied(*args): raise _BrowserRequestDenied('collection_upstream_failed')
    client._fetch = denied
    with pytest.raises(CollectionError, match=code):
        asyncio.run(client.get_video_detail('123'))


def test_other_single_fetch_failures_do_not_retry_through_a_page():
    client = BrowserCollectionClient(object())
    client._native_start = lambda url: pytest.fail('not a raw HTTP 403')
    async def failed(*args): raise CollectionError('collection_upstream_failed')
    client._fetch = failed
    with pytest.raises(CollectionError, match='collection_upstream_failed'):
        asyncio.run(client.get_video_detail('123'))


@pytest.mark.parametrize('status,expected', [(403, _BrowserRequestDenied), (500, CollectionError)])
def test_only_http_403_marks_raw_request_as_denied(status, expected):
    class Page:
        def call(self, *args, **kwargs):
            return {'result': {'value': {'status': status, 'body': 'upstream error'}}}
    client = BrowserCollectionClient(object()); client.page = Page()
    with pytest.raises(expected) as caught:
        asyncio.run(client._fetch('/aweme/v1/web/aweme/detail/', {'aweme_id': '123'}))
    assert type(caught.value) is expected


def test_read_diagnostics_allow_only_stage_status_and_identity_boolean(caplog):
    with caplog.at_level('INFO'):
        _log_read('native', 'detail', 2,
                  {'status': 200, 'url': 'PRIVATE URL', 'cookie': 'PRIVATE COOKIE'},
                  {'status_code': 0, 'aweme_detail': {'aweme_id': 'PRIVATE ID', 'desc': 'PRIVATE TEXT'}},
                  key='PRIVATE ID')
        _log_read('raw', 'self', 0, {'status': True}, {'status_code': 'PRIVATE ERROR'})
    assert 'PRIVATE' not in caplog.text
    values = [json.loads(record.message.split(' ', 1)[1]) for record in caplog.records]
    assert values[0] == {'stage': 'native', 'kind': 'detail', 'read_number': 2,
                         'http_status': 200, 'status_code': 0, 'exception': False,
                         'detail_present': True, 'identity_matches': True}
    assert values[1]['http_status'] is None and values[1]['status_code'] is None


def test_denied_self_request_keeps_original_failure_and_logs_boundary(caplog):
    class Page:
        def call(self, *args, **kwargs):
            return {'result': {'value': {'status': 403, 'body': 'PRIVATE CONTENT'}}}
    client = BrowserCollectionClient(object()); client.page = Page()
    with caplog.at_level('INFO'), pytest.raises(_BrowserRequestDenied, match='collection_upstream_failed'):
        asyncio.run(client._fetch('/aweme/v1/web/user/profile/self/', {}))
    assert 'PRIVATE' not in caplog.text
    value = json.loads(caplog.records[-1].message.split(' ', 1)[1])
    assert value['kind'] == 'self' and value['read_number'] == 0 and value['http_status'] == 403


@pytest.mark.parametrize('navigation,expected', [
    ('https://www.douyin.com/collection/7655813871541684264?previous_page=app_code_link',
     'https://www.douyin.com/collection/7655813871541684264?previous_page=app_code_link'),
    ('https://www.douyin.com/video/123', 'https://www.douyin.com/video/123'),
    ('https://evil.test/collection/7655813871541684264', 'https://www.douyin.com/video/123'),
])
def test_shortlink_keeps_native_collection_document_after_history_replacement(navigation, expected):
    from types import SimpleNamespace
    class Page:
        def call(self, *a, **k): pass
        def wait_for(self, *a): return True
        def evaluate(self, expression):
            script = 'const location={href:"https://www.douyin.com/video/123"};const performance={getEntriesByType:()=>[{name:' + json.dumps(navigation) + '}]};console.log(JSON.stringify(' + expression + '));'
            return json.loads(subprocess.run(['node', '-e', script], check=True, text=True, capture_output=True).stdout)
    client = BrowserCollectionClient(SimpleNamespace())
    client.page = Page()
    assert asyncio.run(client.resolve_short_url('https://v.douyin.com/example/')) == expected
