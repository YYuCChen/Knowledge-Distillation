import json

import pytest

from knowledge_distiller.v1.weibo import WeiboSource, WeiboSourceError, connection_authority, qualify_weibo, weibo_identity
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.domain import Evidence, Knowledge, Point
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.worker import SingleWorker

KEY = '5339055678030665'
BID = 'Rgo11xH8R'
URL = f'https://weibo.com/7072370695/{BID}'


def state(**changes):
    data = {'id': int(KEY), 'idstr': KEY, 'mblogid': BID, 'user': {'id': 7072370695},
            'isLongText': False, 'pic_num': 0, 'pic_ids': [], 'text_raw': '原文  保留\n第二段。'}
    data.update(changes)
    return {'loggedIn': True, 'contextId': 'context', 'requestedId': BID, 'body': json.dumps(data)}


@pytest.mark.parametrize('url,expected', [(URL, (BID, '7072370695')), (f'https://weibo.com/detail/{KEY}', (KEY, None)),
    (f'https://m.weibo.cn/status/{BID}', (BID, None))])
def test_exact_locator(url, expected):
    assert weibo_identity(url) == expected


@pytest.mark.parametrize('url', ['https://weibo.com/u/7072370695', 'https://weibo.com/7072370695',
    'https://weibo.com/ttarticle/p/show', 'https://weibo.com/ttarticle/p/show?id=1&id=2',
    URL.replace('weibo.com', 'weibo.com.evil.test')])
def test_non_status_rejected(url):
    with pytest.raises(ValueError):
        weibo_identity(url)


def test_preserves_primary_text_and_excludes_nested_payload():
    _, key, text, locator = qualify_weibo(state(retweeted_status={'text_raw': '不可混入'}), BID, '7072370695')
    assert key == KEY and locator == f'https://weibo.com/detail/{KEY}'
    assert text == '原文  保留\n第二段。'


@pytest.mark.parametrize('change', [{'isLongText': True}, {'is_long_text': True}, {'truncated': True},
    {'pic_num': 1}, {'pic_ids': ['image']}, {'page_info': {'type': 'video'}}, {'media_info': {'x': 1}},
    {'text_raw': None, 'text': '展示摘要'}, {'isLongText': None}, {'id': 123}, {'idstr': '123'},
    {'retweeted_status': {'text_raw': '嵌套正文'}, 'text_raw': '转发微博'}])
def test_no_partial_or_wrong_shape_source_fact(change):
    with pytest.raises(WeiboSourceError):
        qualify_weibo(state(**change), BID)


def test_uid_context_and_requested_id_must_agree():
    with pytest.raises(WeiboSourceError):
        qualify_weibo(state(), BID, '999')
    with pytest.raises(WeiboSourceError):
        qualify_weibo(state(), KEY)


def test_single_worker_plain_text_publish_and_session_replacement(tmp_path):
    store = Store(tmp_path / 'isolated.sqlite3'); store.initialize()
    store.save_connection('weibo', None, browser_context='context')
    class Session:
        def read(self, url, context):
            assert (url, context) == (URL, 'context')
            return state()
    class Model:
        def derive(self, snapshot, uncertainties):
            assert snapshot == '原文  保留\n第二段。'
            return Knowledge('标题', '副标题', '摘要', (Point('p1', '观点', '论证', ('e1',)),), (), (Evidence('e1', 0, 6, '原文  保留'),))
    vault = tmp_path / 'vault'; vault.mkdir()
    adapter = WeiboSource(store, Session())
    service = Distiller(store=store, source=None, normalizer=None, recognizer=None, reviewer=None, confirmation_clipper=None,
        knowledge_model=Model(), runtime_root=tmp_path / 'runtime', vault=vault, weibo_source=adapter)
    item = store.create_item(URL); SingleWorker(store, service).run_one(); row = store.item_bundle(item)
    assert row['state'] == 'succeeded', row['error_code']
    assert row['source_key'] == KEY
    assert '原文  保留' in (vault / row['published_path']).read_text()
    authority = connection_authority(store.connection('weibo'))
    store.save_connection('weibo', None, browser_context='context')
    from knowledge_distiller.v1.chrome import ChromeSessionError
    with pytest.raises(ChromeSessionError, match='weibo_connection_changed'):
        adapter.capture(URL, tmp_path, expected_authority=authority)


def test_settings_and_intake_bind_weibo(tmp_path):
    from knowledge_distiller.v1.web import create_app
    from knowledge_distiller.v1.settings import SettingsService
    store = Store(tmp_path / 'isolated.sqlite3')
    class Browser:
        def verify(self): return 'context'
    client = create_app(store, None, SettingsService(store, weibo=Browser())).test_client()
    assert client.post('/submissions', data={'content': URL}).status_code == 400
    assert client.post('/settings/weibo/connect').status_code == 302
    assert client.post('/submissions', data={'content': URL}).status_code == 302
    assert json.loads(store.item_bundle(1)['platform_authority_json']) == connection_authority(store.connection('weibo'))
    assert client.post('/settings/weibo/clear').status_code == 302
    assert store.connection('weibo')['state'] == 'unconfigured'


def test_failed_capture_retries_same_item_with_new_binding(tmp_path):
    from knowledge_distiller.v1.chrome import ChromeSessionError
    store = Store(tmp_path / 'isolated.sqlite3'); store.initialize()
    store.save_connection('weibo', None, browser_context='context')
    class Session:
        def read(self, url, context): raise ChromeSessionError('weibo_login_required')
    item = store.create_item(URL)
    service = Distiller(store=store, source=None, normalizer=None, recognizer=None, reviewer=None, confirmation_clipper=None,
        knowledge_model=None, runtime_root=tmp_path / 'runtime', vault=None, weibo_source=WeiboSource(store, Session()))
    SingleWorker(store, service).run_one()
    row = store.item_bundle(item)
    assert row['state'] == 'failed' and row['source_fact_id'] is None
    assert row['error_code'] == 'weibo_login_required'
    assert store.connection('weibo')['state'] == 'relogin_required'
    store.save_connection('weibo', None, browser_context='new-context')
    store.retry_item(item)
    assert store.item_bundle(item)['state'] == 'queued'
    assert json.loads(store.item_bundle(item)['platform_authority_json']) == connection_authority(store.connection('weibo'))


ARTICLE_ID = '2309634596749858897947'
ARTICLE_URL = f'https://weibo.com/ttarticle/p/show?id={ARTICLE_ID}'


def article_state(*, images=False, **changes):
    article = {'requestedId': ARTICLE_ID, 'pageUrl': ARTICLE_URL, 'title': '完整文章标题',
        'text': '第一段原文。\n\n' + ('〔图片 1〕\n\n' if images else '') + '末段原文。',
        'html': '<p>第一段原文。</p><p>末段原文。</p>', 'isMask': '0', 'isPay': '0',
        'unsupportedMedia': False, 'images': ([{'id': 'image-1', 'url': 'https://r.sinaimg.cn/large/article/test', 'alt': ''}] if images else []),
        'authorName': '作者', 'publishedAt': '2026-09-07'}
    article.update(changes)
    return {'loggedIn': True, 'contextId': 'context', 'requestedId': 'article:' + ARTICLE_ID, 'article': article}


@pytest.mark.parametrize('url', [ARTICLE_URL, f'https://weibo.com/ttarticle/x/m/show#/id={ARTICLE_ID}&_wb_client_=1'])
def test_native_article_locator_is_separate_from_status_identity(url):
    assert weibo_identity(url) == ('article:' + ARTICLE_ID, None)


def test_native_long_text_never_uses_short_status_excerpt():
    value = state(isLongText=True, text_raw='短摘要')
    complete = '完整原文\n\n' + '后半段。' * 100
    value['longText'] = {'requestedId': KEY, 'status': 200,
        'body': json.dumps({'ok': 1, 'data': {'longTextContent_raw': complete, 'longTextContent': '渲染版'}})}
    data, key, text, _ = qualify_weibo(value, BID)
    assert key == KEY and data['_is_long'] is True and text == complete
    value['longText']['body'] = json.dumps({'ok': 0, 'data': {}})
    with pytest.raises(WeiboSourceError, match='weibo_text_incomplete'):
        qualify_weibo(value, BID)
    value['longText']['requestedId'] = '999'
    with pytest.raises(WeiboSourceError, match='weibo_identity_mismatch'):
        qualify_weibo(value, BID)


def test_long_text_native_html_preserves_paragraphs_and_entities():
    value = state(isLongText=True)
    value['longText'] = {'requestedId': KEY, 'status': 200,
        'body': json.dumps({'ok': 1, 'data': {'longTextContent': '完整 &amp; 原文<br>第二行<a href="https://weibo.com">链接</a>'}})}
    assert qualify_weibo(value, BID)[2] == '完整 & 原文\n第二行链接'


@pytest.mark.parametrize('changes,code', [
    ({'pageUrl': ARTICLE_URL + '999'}, 'identity_mismatch'),
    ({'requestedId': '999'}, 'identity_mismatch'),
    ({'isMask': '1'}, 'article_restricted'), ({'isPay': '1'}, 'article_restricted'),
    ({'text': ''}, 'article_incomplete'), ({'html': ''}, 'article_incomplete'),
    ({'unsupportedMedia': True}, 'media_unsupported'),
    ({'images': [{'id': 'image-1', 'url': 'https://evil.test/image'}]}, 'article_incomplete'),
])
def test_article_incomplete_or_restricted_never_becomes_source(changes, code):
    value = article_state()
    value['article'].update(changes)
    with pytest.raises(WeiboSourceError, match='weibo_' + code):
        qualify_weibo(value, 'article:' + ARTICLE_ID)


def test_status_article_attachment_includes_original_status_and_full_article():
    value = state(page_info={'object_type': 'article', 'page_id': ARTICLE_ID, 'object_id': '1022:' + ARTICLE_ID})
    value['article'] = article_state()['article']
    data, key, text, _ = qualify_weibo(value, BID)
    assert key == KEY and text == '原文  保留\n第二段。\n\n完整文章标题\n\n第一段原文。\n\n末段原文。'
    assert data['_article']['requestedId'] == ARTICLE_ID
    value['article']['requestedId'] = 'other'
    with pytest.raises(WeiboSourceError, match='identity_mismatch'):
        qualify_weibo(value, BID)


def test_article_original_image_retained_verified_and_reused(tmp_path):
    import hashlib
    from knowledge_distiller.v1.xiaohongshu import NoteMedia
    store = Store(tmp_path / 'isolated.sqlite3'); store.initialize()
    store.save_connection('weibo', None, browser_context='context')
    class Session:
        def read(self, url, context): return article_state(images=True)
    def download(url, path): path.write_bytes(b'original-image')
    def verify(path, member_id, kind):
        return NoteMedia(member_id, kind, path, hashlib.sha256(path.read_bytes()).hexdigest(), 'image/png', 10, 10)
    source = WeiboSource(store, Session(), download, verify)
    authority = connection_authority(store.connection('weibo'))
    captured = source.capture(ARTICLE_URL, tmp_path, expected_authority=authority)
    assert captured.source_key == 'article:' + ARTICLE_ID
    assert captured.metadata['native_kind'] == 'article'
    assert captured.metadata['media_members'] == [captured.members[0].manifest()]
    assert captured.members[0].path.read_bytes() == b'original-image'
    assert '〔图片 1〕' in captured.metadata['original_description']
    assert 'OCR' not in captured.metadata['original_description']
    options = dict(source_key=captured.source_key, submitted_url=ARTICLE_URL, canonical_url=ARTICLE_URL,
        metadata=captured.metadata, work_dir=tmp_path, expected_authority=authority)
    assert source.reuse_retained(**options).members == captured.members
    captured.members[0].path.write_bytes(b'tampered')
    with pytest.raises(WeiboSourceError, match='media_invalid'):
        source.reuse_retained(**options)


def test_article_media_failure_removes_partial_capture(tmp_path):
    from knowledge_distiller.v1.xiaohongshu import XiaohongshuSourceError
    store = Store(tmp_path / 'isolated.sqlite3'); store.initialize()
    store.save_connection('weibo', None, browser_context='context')
    class Session:
        def read(self, url, context): return article_state(images=True)
    def download(url, path): path.write_bytes(b'unsupported-gif')
    def verify(*args): raise XiaohongshuSourceError('xiaohongshu_media_invalid')
    source = WeiboSource(store, Session(), download, verify)
    with pytest.raises(WeiboSourceError, match='weibo_media_invalid'):
        source.capture(ARTICLE_URL, tmp_path, expected_authority=connection_authority(store.connection('weibo')))
    assert not (tmp_path / 'weibo-media').exists()


def test_article_enters_same_worker_source_fact_and_publishing(tmp_path):
    store = Store(tmp_path / 'isolated.sqlite3'); store.initialize()
    store.save_connection('weibo', None, browser_context='context')
    class Session:
        def read(self, url, context): return article_state()
    class Model:
        def derive(self, snapshot, uncertainties):
            assert snapshot == '完整文章标题\n\n第一段原文。\n\n末段原文。'
            return Knowledge('标题', '副标题', '摘要', (Point('p1', '观点', '论证', ('e1',)),), (),
                (Evidence('e1', 8, 14, '第一段原文。'),))
    vault = tmp_path / 'vault'; vault.mkdir()
    service = Distiller(store=store, source=None, normalizer=None, recognizer=None, reviewer=None,
        confirmation_clipper=None, knowledge_model=Model(), runtime_root=tmp_path/'runtime', vault=vault,
        weibo_source=WeiboSource(store, Session()))
    item = store.create_item(ARTICLE_URL)
    assert SingleWorker(store, service).run_one() == item
    row = store.item_bundle(item)
    assert row['state'] == 'succeeded', row['error_code']
    assert row['source_key'] == 'article:' + ARTICLE_ID
    assert '末段原文' in (vault / row['published_path']).read_text()


def test_article_image_ocr_keeps_inline_position_and_original_attachment(tmp_path):
    from PIL import Image
    from .test_xiaohongshu import FakeOcr
    store = Store(tmp_path / 'isolated.sqlite3'); store.initialize()
    store.save_connection('weibo', None, browser_context='context')
    class Session:
        def read(self, url, context): return article_state(images=True)
    def download(url, path): Image.new('RGB', (64, 64), 'white').save(path, format='PNG')
    class Model:
        def derive(self, snapshot, uncertainties):
            assert snapshot.index('第一段') < snapshot.index('图片文字') < snapshot.index('末段')
            start = snapshot.index('图片文字')
            return Knowledge('标题', '副标题', '摘要', (Point('p1', '观点', '论证', ('e1',)),), (),
                (Evidence('e1', start, start + 4, '图片文字'),))
    vault = tmp_path / 'vault'; vault.mkdir()
    service = Distiller(store=store, source=None, normalizer=None, recognizer=None, reviewer=None,
        confirmation_clipper=None, knowledge_model=Model(), runtime_root=tmp_path/'runtime', vault=vault,
        weibo_source=WeiboSource(store, Session(), downloader=download), ocr=FakeOcr())
    item = store.create_item(ARTICLE_URL)
    assert SingleWorker(store, service).run_one() == item
    row = store.item_bundle(item)
    assert row['state'] == 'succeeded', row['error_code']
    assert len(store.media_members(row['material_id'])) == 1
    assert len(list((vault/'知识蒸馏器').glob('附件/*/*.png'))) == 0
    assert not (tmp_path/'runtime/items'/str(item)/'weibo-media').exists()
