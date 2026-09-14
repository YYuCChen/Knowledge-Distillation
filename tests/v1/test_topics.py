from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from knowledge_distiller.topic_indexing import TopicDraft, TopicPlan, TopicIndexing, TopicIndexFailure
from knowledge_distiller.v1.database import connect, SCHEMA, SUBMITTED_SCHEMA
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.topics import TopicLibrary, TopicError
from .test_submitted_sources import setup


def library(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    for body in ['正文甲。', '正文乙。']:
        store.submit_source(prepare_direct_text(body))
        worker.run_one()
    return TopicLibrary(store), store, worker


def plan_for(points, existing=()):
    return TopicPlan((TopicDraft(existing[0].topic_id if existing else None,
        None if existing else 'reading', '来源阅读与核对', '通过来源与证据核对已有观点。',
        tuple(p.reference for p in points)),), ())


def test_frozen_counts_identity_order_and_unchanged_timestamp(tmp_path):
    lib, store, worker = library(tmp_path)
    points, old, guard = lib.prepare()
    result = lib.commit(plan_for(points), guard)
    assert result['knowledge_count'] == 2
    points, old, guard = lib.prepare()
    assert lib.commit(plan_for(points, old), guard) == result
    store.submit_source(prepare_direct_text('新正文。'))
    worker.run_one()
    assert lib.snapshot() == result
    points, old, guard = lib.prepare()
    later = lib.commit(plan_for(points, old), guard)
    assert later['topics'][0]['id'] == result['topics'][0]['id']
    assert later['knowledge_count'] == 3
    assert later['topics'][0]['updated_at'] != result['topics'][0]['updated_at']


def test_changes_reject_old_plan_and_transaction_failure_keeps_snapshot(tmp_path):
    lib, store, worker = library(tmp_path)
    points, old, guard = lib.prepare()
    lib.commit(plan_for(points), guard)
    points, old, guard = lib.prepare()
    before = lib.snapshot()
    with connect(store.path) as db:
        db.execute("CREATE TRIGGER reject_topic BEFORE UPDATE ON topic_entries BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        lib.commit(plan_for(points, old), guard)
    assert lib.snapshot() == before
    store.submit_source(prepare_direct_text('更多正文。'))
    worker.run_one()
    with pytest.raises(TopicError, match='已变化'):
        lib.commit(plan_for(points, old), guard)
    assert lib.snapshot() == before


def test_invalid_plan_failure_and_empty_organization(tmp_path):
    lib, store, worker = library(tmp_path)
    points, old, guard = lib.prepare()
    lib.commit(plan_for(points), guard)
    before = lib.snapshot()
    points, old, guard = lib.prepare()
    one = replace(plan_for(points, old).topics[0], members=(points[0].reference,))
    with pytest.raises(TopicError):
        lib.commit(TopicPlan((one,), (points[1].reference,)), guard)
    with pytest.raises(TopicError):
        lib.refresh(SimpleNamespace(organize=lambda *a: TopicIndexing.failed(TopicIndexFailure.RUNTIME_FAILED)))
    assert lib.snapshot() == before
    zero = lib.commit(TopicPlan((), tuple(p.reference for p in points)), guard)
    assert zero == {'topics': [], 'knowledge_count': 2}
    points, old, guard = lib.prepare()
    new = lib.commit(plan_for(points), guard)
    assert new['topics'][0]['id'] > before['topics'][0]['id']


def test_outer_organization_rollback_rolls_back_topic_snapshot(tmp_path):
    lib, store, worker = library(tmp_path)
    points, old, guard = lib.prepare()
    with pytest.raises(RuntimeError):
        with connect(store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            lib.commit(plan_for(points), guard, db)
            raise RuntimeError('growth failed')
    assert lib.snapshot() == {'topics': [], 'knowledge_count': 0}


def test_v5_upgrade_keeps_knowledge_and_does_not_create_topics(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA + SUBMITTED_SCHEMA)
        db.execute("INSERT INTO settings VALUES('vault_path','/old')")
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=5')
    store = Store(path)
    store.initialize()
    store.initialize()
    assert store.setting('vault_path') == '/old'
    assert TopicLibrary(store).snapshot() == {'topics': [], 'knowledge_count': 0}


def test_baseline_conflict_and_model_input_excludes_private_source(tmp_path):
    import json
    from knowledge_distiller.v1.topic_model import TopicModel
    lib, store, worker = library(tmp_path)
    points, old, guard = lib.prepare()
    plan = plan_for(points)
    lib.commit(plan, guard)
    with pytest.raises(TopicError, match='已变化'):
        lib.commit(plan, guard)
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return json.dumps({'topics':[], 'decisions':{str(n):[] for n, _ in enumerate(points)}})
    result = TopicModel(SimpleNamespace(complete=complete, model="fixture", base_url="fixture://local")).organize(points, ())
    assert result.failure is None
    payload = json.loads(calls[0]['user'])
    assert set(payload['points'][0]) == {'input_key','knowledge_result_id','point_id','role','statement','argument','title','summary'}
    assert '正文甲' not in calls[0]['user']
    assert str(tmp_path) not in calls[0]['user']


def test_schema_upgrade_failure_rolls_back_version_and_tables(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import database
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA + SUBMITTED_SCHEMA)
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=5')
    monkeypatch.setattr(database, 'TOPIC_STATEMENTS', (*database.TOPIC_STATEMENTS[:1], 'INVALID SQL'))
    with pytest.raises(sqlite3.OperationalError):
        Store(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert db.execute("SELECT name FROM sqlite_master WHERE name='topic_entries'").fetchone() is None


def test_same_database_libraries_share_refresh_lock_and_empty_never_calls_model(tmp_path):
    store = Store(tmp_path / 'empty.sqlite3')
    store.initialize()
    first, second = TopicLibrary(store), TopicLibrary(store)
    assert first._refresh_lock is second._refresh_lock
    class Forbidden:
        def organize(self, *args):
            raise AssertionError('empty input must not call model')
    assert first.refresh(Forbidden()) == {'topics': [], 'knowledge_count': 0}
