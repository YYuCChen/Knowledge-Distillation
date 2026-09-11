import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_distiller.v1.database import SCHEMA, SCHEMA_VERSION, connect
from knowledge_distiller.v1.domain import Evidence, Knowledge, Point
from knowledge_distiller.v1.file_sources import prepare_direct_text, prepare_file
from knowledge_distiller.v1.knowledge_model import KnowledgeModelError
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.worker import SingleWorker


class ForbiddenAudio:
    def __getattr__(self, name):
        raise AssertionError(f'text must not use media: {name}')


class Model:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def derive(self, snapshot, uncertainties=()):
        self.calls += 1
        if self.fail:
            raise KnowledgeModelError('llm_runtime_failed')
        evidence = '正文' if '正文' in snapshot else 'body'
        start = snapshot.index(evidence)
        return Knowledge('文本保留完整身份', '同一来源的字节和文字变化如何区分', '文本按原样保留且使用精确证据。',
                         (), (Point('p1', '正文应原样保留。', '证据指向提交正文。', ('e1',)),),
                         (Evidence('e1', start, start + len(evidence), evidence),))


def setup(tmp_path):
    store = Store(tmp_path / 'data.sqlite3')
    store.initialize()
    vault = tmp_path / 'vault'
    vault.mkdir()
    model = Model()
    audio = ForbiddenAudio()
    service = Distiller(store=store, source=audio, normalizer=audio, recognizer=audio,
                       reviewer=audio, confirmation_clipper=audio, knowledge_model=model,
                       runtime_root=tmp_path / 'runtime', vault=vault)
    worker = SingleWorker(store, service)
    return store, service, model, worker, vault


@pytest.mark.parametrize('file', [False, True])
def test_exact_input_survives_restart_publishes_once_and_cleans_copy(tmp_path, file):
    store, service, model, worker, vault = setup(tmp_path)
    text = '\n\n正文保持 Unicode：é e\u0301。\r\n![[private]]\n```code\n正文\n```\n\n'
    source = prepare_file('/private/name.md', text.encode()) if file else prepare_direct_text(text)
    item = store.submit_source(source)
    renamed = prepare_file('renamed.md', text.encode()) if file else source
    assert store.submit_source(renamed) == item
    assert store.claim_next_item() == item
    store = Store(store.path)
    store.initialize()
    assert store.requeue_interrupted() == 1
    assert worker.run_one() == item
    row = store.item_bundle(item)
    assert row['state'] == 'succeeded'
    assert row['snapshot'] == text
    lineage = json.loads(row['lineage_json'])
    assert lineage['source_key'] == source.source_key
    assert not row['input_available']
    assert model.calls == 1
    published = vault / row['published_path']
    before = published.read_bytes()
    # The source is quoted for its native Obsidian region; removing that one
    # presentation prefix restores exact Unicode, CRLF, and inert code fences.
    unquoted='\n'.join(line[2:] if line.startswith('> ') else '' if line=='>' else line
                       for line in before.decode().split('\n'))
    assert text in unquoted
    assert '````text' in before.decode()
    assert '/private/' not in before.decode()
    assert store.submit_source(source) == item
    assert worker.run_one() is None
    assert published.read_bytes() == before
    changed = prepare_file('name.md', (text + '新版本').encode()) if file else prepare_direct_text(text + '新版本')
    assert store.submit_source(changed) != item


def test_model_failure_reuses_fact_without_original_or_audio(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    item = store.submit_source(prepare_direct_text('正文是可信来源。'))
    model.fail = True
    worker.run_one()
    row = store.item_bundle(item)
    assert row['state'] == 'failed' and row['source_fact_id']
    assert not row['input_available']
    fact = row['source_fact_id']
    store.retry_item(item)
    model.fail = False
    worker.run_one()
    assert store.item_bundle(item)['source_fact_id'] == fact
    assert store.item_bundle(item)['state'] == 'succeeded'


def test_invalid_file_is_not_retried_or_partial_fact_and_queue_continues(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    bad = store.submit_source(prepare_file('broken.md', b'---\ntitle: [\n---\nbody'))
    good = store.submit_source(prepare_direct_text('正文完整。'))
    worker.run_one()
    row = store.item_bundle(bad)
    assert row['state'] == 'failed'
    assert row['source_fact_id'] is None and row['material_id'] is None
    assert not row['retryable'] and not row['input_available']
    with pytest.raises(ValueError):
        store.retry_item(bad)
    assert worker.run_one() == good
    assert store.item_bundle(good)['state'] == 'succeeded'


def test_failure_expiry_requires_exact_rehydration_and_does_not_refresh_replay(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    source = prepare_direct_text('正文原样。', {'author': '甲'})
    item = store.submit_source(source)
    store.claim_next_item()
    store.mark_failed(item, 'reviewing', 'processing_unexpected_failure')
    deadline = store.item_bundle(item)['retain_until']
    assert store.submit_source(source) == item
    assert store.item_bundle(item)['retain_until'] == deadline
    with connect(store.path) as db:
        db.execute('UPDATE submitted_sources SET retain_until = ? WHERE item_id = ?',
                   ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), item))
    store.expire_submitted_sources()
    assert not store.item_bundle(item)['input_available']
    with pytest.raises(ValueError):
        store.retry_item(item)
    with pytest.raises(ValueError):
        store.retry_item(item, prepare_direct_text('正文原样。'))
    with pytest.raises(ValueError):
        store.retry_item(item, prepare_direct_text('正文改动。', {'author': '甲'}))
    store.retry_item(item, source)
    assert store.item_bundle(item)['retain_until'] is None
    worker.run_one()
    assert store.item_bundle(item)['state'] == 'succeeded'


def test_intake_post_is_durable_not_synchronous_and_single_file_scope(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    client = create_app(store, service).test_client()
    response = client.post('/submissions', data={'content': ' 正文不能 trim。\n'})
    assert response.status_code == 302
    row = store.item_bundle(1)
    assert row['state'] == 'queued' and model.calls == 0
    assert store.submitted_source(1).content.decode() == ' 正文不能 trim。\n'
    duplicate = client.post('/submissions', data={'content': ' 正文不能 trim。\n'})
    assert duplicate.location == response.location
    upload = client.post('/submissions', data={'attachment': (io.BytesIO('正文来自文件。'.encode()), 'file.md')})
    assert upload.status_code == 302
    assert 'Markdown' in client.get('/').text
    for data in [
        {'attachment': (io.BytesIO(b'x'), 'file.pdf')},
        {'content': '正文', 'attachment': (io.BytesIO(b'x'), 'file.md')},
        {'attachment': [(io.BytesIO(b'a'), 'a.md'), (io.BytesIO(b'b'), 'b.md')]},
    ]:
        assert client.post('/submissions', data=data).status_code == 400
    assert len(store.recent_items()) == 2


def test_v3_migration_preserves_real_schema_fact_publication_and_immutability(tmp_path):
    path = tmp_path / 'v3.sqlite3'
    old = SCHEMA.replace("    lineage_json TEXT NOT NULL DEFAULT '{}',\n", '')
    with sqlite3.connect(path) as db:
        db.executescript(old)
        db.execute("INSERT INTO materials VALUES (1, 'douyin', '123', 'url', 'url', '{}', 'now')")
        db.execute("INSERT INTO source_facts VALUES (1, 1, '正文', '[]', 'now')")
        db.execute("INSERT INTO knowledge_results VALUES (1, 1, '{}', 'old.md', '/exact/vault', 'now', 'now')")
        db.execute('PRAGMA user_version = 3')
    Store(path).initialize()
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert tuple(db.execute('SELECT snapshot, lineage_json FROM source_facts').fetchone()) == ('正文', '{}')
        assert tuple(db.execute('SELECT published_path, published_vault FROM knowledge_results').fetchone()) == ('old.md', '/exact/vault')
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE source_facts SET snapshot = 'changed'")


def test_publication_recovery_compares_exact_bytes_with_crlf(tmp_path, monkeypatch):
    store, service, model, worker, vault = setup(tmp_path)
    item = store.submit_source(prepare_direct_text('正文原样。\r\n\r\n'))
    original = store.mark_published
    def crash(*args, **kwargs):
        raise RuntimeError('crash after file creation')
    monkeypatch.setattr(store, 'mark_published', crash)
    worker.run_one()
    assert store.item_bundle(item)['state'] == 'failed'
    before = next(vault.rglob('*.md')).read_bytes()
    monkeypatch.setattr(store, 'mark_published', original)
    store.retry_item(item)
    worker.run_one()
    assert store.item_bundle(item)['state'] == 'succeeded'
    assert next(vault.rglob('*.md')).read_bytes() == before


def test_failure_retry_cannot_replace_retained_source(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    item = store.submit_source(prepare_direct_text('正文原样。'))
    store.claim_next_item()
    store.mark_failed(item, 'reviewing', 'temporary')
    with pytest.raises(ValueError, match='同一份'):
        store.retry_item(item, prepare_direct_text('正文修改。'))
    assert store.submitted_source(item).content.decode() == '正文原样。'
    assert store.item_bundle(item)['state'] == 'failed'


def test_v4_migration_preserves_queued_and_failure_source_bytes(tmp_path):
    from knowledge_distiller.v1.database import SUBMITTED_SCHEMA
    path = tmp_path / 'v4.sqlite3'
    old_sources = SUBMITTED_SCHEMA.replace("'direct_text', 'markdown', 'pdf', 'epub'", "'direct_text', 'markdown'")
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA + old_sources)
        db.execute("INSERT INTO distill_items (item_id, submitted_url, state, phase, queued_at, created_at, updated_at) VALUES (1, '直接文本', 'failed', 'reviewing', 'a', 'a', 'a')")
        db.execute("INSERT INTO submitted_sources VALUES (1, 'direct_text', 'exact-key', '直接文本', '{}', ?, '2099-01-01T00:00:00+00:00', 1)", ('正文\r\n'.encode(),))
        before = db.execute('SELECT * FROM submitted_sources').fetchall()
        db.execute('PRAGMA user_version = 4')
    Store(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM submitted_sources').fetchall() == before
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize('kind', ['pdf', 'epub'])
def test_document_uses_same_durable_worker_and_locator_without_audio(tmp_path, kind):
    from .test_document_sources import pdf_bytes, epub_bytes
    store, service, model, worker, vault = setup(tmp_path)
    content = pdf_bytes(text='The body preserves this source.') if kind == 'pdf' else epub_bytes(body='正文完整保存于本章节。')
    source = prepare_file('sample.' + kind, content)
    item = store.submit_source(source)
    assert store.submit_source(prepare_file('renamed.' + kind, content)) == item
    assert store.claim_next_item() == item
    assert store.requeue_interrupted() == 1
    assert worker.run_one() == item
    row = store.item_bundle(item)
    assert row['state'] == 'succeeded', row['error_code']
    assert not row['input_available']
    lineage = json.loads(row['lineage_json'])
    assert lineage['source_key'] == source.source_key
    assert lineage['spans']
    assert ('body' if kind == 'pdf' else '正文') in row['snapshot']
    assert (vault / row['published_path']).is_file()
    assert model.calls == 1


def test_document_upload_preaccept_protection_creates_no_item(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    client = create_app(store, service).test_client()
    for name, content in [('broken.pdf', b'%PDF-1.4 broken'), ('broken.epub', b'PK broken')]:
        response = client.post('/submissions', data={'attachment': (io.BytesIO(content), name)})
        assert response.status_code == 400
    assert store.recent_items() == ()
