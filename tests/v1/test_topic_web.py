from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.topic_web import search_records
from .test_topics import plan_for


def establish_historical_knowledge(store, text, vault):
    """Seed retained V2 knowledge; R01 raw-only intake must not recreate it."""
    from knowledge_distiller.v1.file_sources import prepare_direct_text, parse_submitted_source
    from knowledge_distiller.v1.publisher import publish
    from .test_submitted_sources import Model

    source = prepare_direct_text(text)
    item = store.submit_source(source, ingestion_contract='legacy')
    fact = store.establish_submitted_fact(item, source, parse_submitted_source(source))
    assert fact is not None
    result = store.establish_knowledge(fact, Model().derive(text))
    publish(store, item, vault)
    return result


def historical_library(tmp_path):
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.topics import TopicLibrary

    store = Store(tmp_path / 'data.sqlite3')
    store.initialize()
    vault = tmp_path / 'vault'
    vault.mkdir()
    for text in ('正文甲。', '正文乙。'):
        establish_historical_knowledge(store, text, vault)
    return TopicLibrary(store), store, vault


def test_topic_pages_search_and_settings_return_are_pure_reads(tmp_path):
    lib, store, _ = historical_library(tmp_path)
    points, old, guard = lib.prepare()
    lib.commit(plan_for(points), guard)
    before = store.path.read_bytes()
    client = create_app(store, object()).test_client()
    index = client.get('/topics')
    assert index.status_code == 200
    assert '1 个主题、2 篇知识' in index.text
    assert '历史内容只读保留' in index.text
    detail = client.get('/topics/1')
    assert detail.status_code == 200
    assert '历史内容只读保留' in detail.text
    assert detail.text.count('data-point=') == 2
    assert '证据指向提交正文。' in detail.text
    assert '阅读原文' in detail.text
    assert client.get('/topics/999').status_code == 404
    search = client.get('/topics?q=正文')
    assert '来源型观点 · 2' in search.text
    assert '来源阅读与核对' not in search.text
    empty = client.get('/topics?q=不存在')
    assert 'value="不存在"' in empty.text and '没有找到' in empty.text
    page = client.get('/settings?return_to=/topics/1')
    assert 'href="/topics/1" class="back-link"' in page.text
    for target in ['//example.org', '/topics/1\\example.org', '/outside']:
        page = client.get('/settings', query_string={'return_to':target})
        assert 'href="/" class="back-link"' in page.text
    assert store.path.read_bytes() == before


def test_search_and_matching_is_literal_unicode_and_not_context_only():
    point = dict(statement='ＡＩ 保留 原文', argument='证据支持观点', title='文本阅读',
                 summary='核对', role='other',knowledge_result_id=1,point_id='p')
    snapshot = {'topics':[]}
    assert search_records(snapshot,[point],'ai\t原文')[1] == [point]
    assert search_records(snapshot,[point],'原文 缺少')[1] == []
    assert search_records(snapshot,[point],'阅读')[1] == []
    assert search_records(snapshot,[point],'%')[1] == []
    assert search_records(snapshot,[point],'原文 阅读')[1] == [point]


def test_markup_in_query_is_inert(tmp_path):
    lib, store, _ = historical_library(tmp_path)
    client = create_app(store, object()).test_client()
    response = client.get('/topics',query_string={'q':'<script>alert(1)</script>'})
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text


def test_snapshot_detail_ignores_new_unrelated_corrupt_knowledge(tmp_path):
    from knowledge_distiller.v1.database import connect
    lib, store, vault = historical_library(tmp_path)
    points, old, guard = lib.prepare()
    lib.commit(plan_for(points), guard)
    assert establish_historical_knowledge(store, '新增正文', vault) == 3
    with connect(store.path) as db:
        guard_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='knowledge_results_content_no_update'"
        ).fetchone()[0]
        db.execute('DROP TRIGGER knowledge_results_content_no_update')
        db.execute("UPDATE knowledge_results SET payload_json='{}' WHERE knowledge_result_id=3")
        db.execute(guard_sql)
        assert db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='knowledge_results_content_no_update'"
        ).fetchone()[0] == guard_sql
    client = create_app(store, object()).test_client()
    assert client.get('/topics/1').status_code == 200
    assert '1 个主题、2 篇知识' in client.get('/topics').text
    failed = client.get('/topics?q=正文')
    assert failed.status_code == 503
    assert '暂时无法完整读取' in failed.text
    assert '没有找到' not in failed.text
