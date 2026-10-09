"""Synthetic R05/R06 entrance tests; all data live under pytest tmp_path."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1 import web_article as web
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, history_message
from knowledge_distiller.v1.feishu_intake import FeishuIntake
from knowledge_distiller.v1.intake import links_in, message_links, source_route
from knowledge_distiller.v1.link_intake import LinkIntake
from knowledge_distiller.v1.pipeline import Distiller, DistillResult
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from .test_feishu_inbox import message
from .test_web_article import network, HTML, PUBLIC, injected, response


def synthetic_store(path):
    store = Store(path)
    store.initialize()
    return store


def build(store, tmp_path, reader):
    def forbidden(*args, **kwargs):
        raise AssertionError('web collection must not call ASR, OCR or knowledge generation')
    unused = SimpleNamespace(normalize=forbidden, recognize=forbidden, derive=forbidden)
    return Distiller(store=store, source=unused, normalizer=unused, recognizer=unused,
        reviewer=unused, confirmation_clipper=unused, knowledge_model=unused,
        ocr=unused, runtime_root=tmp_path / 'runtime', vault=None,
        web_article_source=web.WebArticleSource(reader))


def test_real_submission_worker_sourcefact_and_original_media(network, monkeypatch):
    tmp_path = network[3]
    store = synthetic_store(tmp_path / 'synthetic.sqlite3')
    app = create_app(store, object())
    result = app.test_client().post('/submissions', data={'content':
        '[一篇合成文章](https://article.example/page)'})
    assert result.status_code == 302
    item = store.item_bundle(1)
    assert item['submitted_url'] == 'https://article.example/page'
    network[0].append(response())
    reader = lambda url: web.read_web_article(url, resolver=lambda h, p: [PUBLIC], extractor=injected)
    distiller = build(store, tmp_path, reader)
    # Exercise real SingleWorker dispatch, but isolate the source-stage handoff
    # from the legacy _finish owned by the raw-only integration worker.
    monkeypatch.setattr(distiller, '_finish', lambda item_id: DistillResult(item_id, 'working'))
    from knowledge_distiller.v1.worker import SingleWorker
    assert SingleWorker(store, distiller).run_one() == 1
    row = store.item_bundle(1)
    assert row['snapshot'] == 'Source fixture text.'
    assert row['knowledge_result_id'] is None
    metadata = json.loads(row['metadata_json'])
    assert metadata['coverage']['http_body_complete'] is True
    assert metadata['coverage']['rendered'] is False
    assert metadata['canonical_url'] == 'http://127.0.0.1/unfetched'
    assert row['canonical_url'] == 'https://article.example/page'
    media = {m['member_id']: m for m in store.media_members(row['material_id'])}
    assert media['html-1']['content'] == HTML == media['wire-1']['content']
    assert media['body-1']['content'] == row['snapshot'].encode()
    assert media['html-1']['sha256'] == hashlib.sha256(HTML).hexdigest()
    assert len(network[1]) == 1  # no canonical/image/reference fetch
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM knowledge_results').fetchone()[0] == 0


def test_web_ingestion_saves_verified_raw_and_exact_html_original(network):
    # Production intake remains legacy until the ingestion owner wires its
    # qualification/binding contract. This separate synthetic owner proves the
    # capture payload and the raw attachment exporter share the existing schema.
    from knowledge_distiller.v1.database import INGESTION_CONTRACT
    from knowledge_distiller.v1.domain import SourceFact
    from knowledge_distiller.v1.ingestion import Ingestion
    tmp_path = network[3]
    store = synthetic_store(tmp_path / 'qualified.sqlite3')
    item_id = store.create_item('https://article.example/page', ingestion_contract=INGESTION_CONTRACT,
        source_binding_sha256=hashlib.sha256(b'synthetic web input').hexdigest(),
        relation_binding_sha256=hashlib.sha256(b'synthetic independent no-selection plan').hexdigest())
    network[0].append(response())
    capture = web.WebArticleSource(lambda url: web.read_web_article(url,
        resolver=lambda h, p: [PUBLIC], extractor=injected)).capture(
            'https://article.example/page', tmp_path / 'capture')
    material_id = store.attach_material(item_id, capture)
    store.establish_source_fact(material_id, SourceFact(capture.metadata['original_description']),
        lineage=capture.metadata['web_lineage'])
    vault = tmp_path / 'synthetic-vault'
    vault.mkdir()
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    receipt = ingestion.material(material_id, vault, item_id=item_id)
    assert receipt.subject_kind == 'material' and receipt.subject_id == material_id
    assert 'Source fixture text.' in (vault / receipt.relative_path).read_text()
    original = next(vault / path for path, digest in receipt.attachments if path.endswith('/html-1.html'))
    assert original.read_bytes() == HTML
    assert len(receipt.attachments) == len(capture.members)
    for path, digest in receipt.attachments:
        assert hashlib.sha256((vault / path).read_bytes()).hexdigest() == digest
    assert ingestion.material(material_id, vault, item_id=item_id) == receipt
    assert store.media_members(material_id)[0]['content'] == HTML


def test_retained_web_capture_restarts_without_fetch_or_asr(network, monkeypatch):
    tmp_path = network[3]
    store = synthetic_store(tmp_path / 'retained.sqlite3')
    item_id = store.create_item('https://article.example/page')
    store.mark_working(item_id, 'collecting')
    network[0].append(response())
    source = web.WebArticleSource(lambda url: web.read_web_article(url,
        resolver=lambda h, p: [PUBLIC], extractor=injected))
    capture = source.capture('https://article.example/page', tmp_path / 'first')
    store.attach_material(item_id, capture)
    def forbidden(url):
        raise AssertionError('retained original must not be fetched again')
    distiller = build(store, tmp_path, forbidden)
    distiller._establish_source(item_id, store.item_bundle(item_id))
    assert store.item_bundle(item_id)['snapshot'] == 'Source fixture text.'
    assert len(network[1]) == 1
    members = store.media_members(store.item_bundle(item_id)['material_id'])
    members[0]['content'] = b'tampered'
    with pytest.raises(web.WebReadError, match='web_originals_invalid'):
        source.reuse_retained(source_key=capture.source_key, submitted_url=capture.submitted_url,
            canonical_url=capture.canonical_url, metadata=capture.metadata,
            work_dir=tmp_path / 'tampered', members=members)


@pytest.mark.parametrize('html,code', [
    (b'<noscript>Please enable JavaScript to read.</noscript>', 'web_render_required'),
    (b'<html><body>empty</body></html>', 'web_body_missing'),
])
def test_collection_failure_keeps_original_and_never_creates_fact(network, html, code, monkeypatch):
    tmp_path = network[3]
    store = synthetic_store(tmp_path / 'failure.sqlite3')
    item_id = store.create_item('https://article.example/page')
    store.mark_working(item_id, 'collecting')
    network[0].append(response(html))
    distiller = build(store, tmp_path, lambda url: web.read_web_article(url,
        resolver=lambda h, p: [PUBLIC], extractor=lambda h, u: None))
    assert distiller.run(item_id).state == 'failed'
    assert store.item_bundle(item_id)['error_code'] == code
    assert store.item_bundle(item_id)['source_fact_id'] is None
    assert (tmp_path / 'runtime/items/1/web-originals/html-1').read_bytes() == html


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'http://localhost/x',
    'http://10.0.0.1/x', 'https://article.example:444/x', 'https://user:pass@article.example/x',
    'https://www.reddit.com/r/test/comments/abc'])
def test_intake_refuses_unsafe_or_separately_authorized_routes(tmp_path, url):
    store = synthetic_store(tmp_path / 'unsafe.sqlite3')
    intake = LinkIntake(store, None, None, {})
    with pytest.raises(ValueError):
        intake.submit(url)
    assert store.recent_items() == ()


def test_dedicated_platforms_never_fall_through_to_web():
    assert source_route('https://www.zhihu.com/unsupported') == 'zhihu'
    assert source_route('https://www.douyin.com/unsupported') == 'douyin'
    assert source_route('https://x.com/u/status/1') == 'x'
    assert source_route('https://article.example/page') == 'web_article'


def test_markdown_labels_duplicates_fourteen_and_queries_keep_original_order():
    urls = [f'https://www.douyin.com/video/{100 + i}' for i in range(14)]
    urls[4] = urls[1]
    assert links_in('\n'.join(f'[素材{i}]({url})' for i, url in enumerate(urls))) == urls
    assert links_in('[https://www.douyin.com/video/1](https://www.douyin.com/video/2)') == ['']
    query = 'https://www.zhihu.com/question/1/answer/2?signature=(a(b))&other=你好。'
    assert links_in(query) == [query]
    assert links_in('[标题](https://www.douyin.com/video/1)123\n\nhttps://article.example/next') == ['', 'https://article.example/next']


def test_feishu_fourteen_mixed_invalid_duplicate_and_replay_use_same_positions(tmp_path):
    store = synthetic_store(tmp_path / 'receipt.sqlite3')
    inbox = FeishuInbox(store, 'app-synthetic')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=100000)
    urls = [f'https://article.example/page/{i}' for i in range(14)]
    urls[3] = 'http://127.0.0.1/private'
    urls[8] = urls[1]
    text = '\n'.join(f'[文章{i}]({url})' for i, url in enumerate(urls))
    inbox.receive(history_message(message(text=text)), history=True)
    intake = FeishuIntake(inbox, LinkIntake(store, None, None, {}))
    assert intake.process('om_1') == 'needs_desktop'
    with connect(store.path) as db:
        parts = db.execute('SELECT * FROM feishu_parts ORDER BY position').fetchall()
        assert [p['position'] for p in parts] == list(range(14))
        assert parts[3]['error'] and parts[3]['item_id'] is None
        assert db.execute('SELECT count(*) FROM collection_operations').fetchone()[0] == 0
    assert [r['submitted_url'] for r in sorted(store.recent_items(), key=lambda r: r['item_id'])] == [u for i, u in enumerate(urls) if i != 3]
    assert intake.process('om_1') == 'needs_desktop'
    assert len(store.recent_items()) == 13


def test_feishu_post_anchor_uses_href_not_flattened_label_and_replays(tmp_path):
    store = synthetic_store(tmp_path / 'post.sqlite3')
    inbox = FeishuInbox(store, 'app-synthetic')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=100000)
    content = json.dumps({'zh_cn': {'title': '', 'content': [[
        {'tag': 'a', 'text': '说明文字', 'href': 'https://article.example/one'},
        {'tag': 'a', 'text': 'https://www.douyin.com/video/1', 'href': 'https://www.douyin.com/video/2'},
        {'tag': 'a', 'text': '第二篇', 'href': 'https://article.example/two'}]]}})
    raw = message(msg_type='post', body={'content': content})
    inbox.receive(history_message(raw), history=True)
    assert message_links('', message_type='post', content=content) == [
        'https://article.example/one', '', 'https://article.example/two']
    intake = FeishuIntake(inbox, LinkIntake(store, None, None, {}))
    assert intake.process('om_1') == 'needs_desktop'
    assert intake.process('om_1') == 'needs_desktop'
    assert len(store.recent_items()) == 2
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM captures').fetchone()[0] == 0
        assert db.execute('SELECT error FROM feishu_parts WHERE position=1').fetchone()[0]


@pytest.mark.parametrize('href', ['https://article.example/one', 'https://www.douyin.com/video/123'])
def test_feishu_post_paragraph_and_anchor_waits_for_content_choice(tmp_path, href):
    store = synthetic_store(tmp_path / 'prose-post.sqlite3')
    inbox = FeishuInbox(store, 'app-synthetic')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=100000)
    prose = '这是我人工编写的完整段落，链接只是其中一个参考，不能替代这段正文。'
    content = json.dumps({'zh_cn': {'title': '', 'content': [
        [{'tag': 'text', 'text': prose}], [{'tag': 'a', 'text': '参考材料', 'href': href}]]}})
    row = inbox.receive(history_message(message(msg_type='post', body={'content': content})), history=True)
    intake = FeishuIntake(inbox, LinkIntake(store, None, None, {}))
    assert intake.process('om_1') == 'waiting_input'
    assert store.recent_items() == ()
    with connect(store.path) as db:
        assert db.execute('SELECT content_kind FROM feishu_receipts').fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM feishu_parts').fetchone()[0] == 0
    intake.choose('om_1', 'text')
    assert intake.process('om_1') == 'accepted'
    assert store.submitted_source(1).content.decode() == row['text']
    assert prose in row['text'] and href in row['text']


def test_feishu_post_only_anchor_label_does_not_require_content_choice(tmp_path):
    store = synthetic_store(tmp_path / 'anchor-post.sqlite3')
    inbox = FeishuInbox(store, 'app-synthetic')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=100000)
    content = json.dumps({'zh_cn': {'title': '', 'content': [[
        {'tag': 'a', 'text': '这只是链接对应的显示标题', 'href': 'https://article.example/one'}]]}})
    inbox.receive(history_message(message(msg_type='post', body={'content': content})), history=True)
    intake = FeishuIntake(inbox, LinkIntake(store, None, None, {}))
    assert intake.process('om_1') == 'accepted'
    assert store.item_bundle(1)['submitted_url'] == 'https://article.example/one'
    with connect(store.path) as db:
        assert db.execute('SELECT content_kind FROM feishu_receipts').fetchone()[0] == 'links'
