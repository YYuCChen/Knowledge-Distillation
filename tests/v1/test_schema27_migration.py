"""Synthetic historical storage migration; no live identity/publication claim.

Luna runs this module only after main freezes the candidate. Positive coverage
is migration/catalog and inert queued storage. No always-true proof callback.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3

import pytest

from knowledge_distiller.v1 import database


FIXTURES = Path(__file__).with_name('fixtures')
STAMP = '2099-01-01T00:00:00Z'
CONTRACT = 'r08-wiki-outcomes-v1'
NEW_NAMES = (
    'wiki_task_contract_plan_unique', 'wiki_task_legacy_boundary_unique',
    'wiki_batch_typed_success_requires_accepted',
    'wiki_task_typed_success_requires_all_accepted',
    'wiki_batch_typed_insert_requires_queued', 'source_identity_events',
    'source_identity_one_root', 'source_identity_one_successor',
    'source_identity_item_order', 'source_identity_events_no_update',
    'source_identity_events_no_delete', 'source_identity_events_append_verified',
)


@pytest.fixture
def case_root(tmp_path, record_property):
    resolved = tmp_path.resolve()
    assert resolved.is_relative_to(Path('/tmp').resolve())
    assert resolved.stat().st_uid == os.getuid()
    resolved.chmod(0o700)
    record_property('synthetic_directory', str(resolved))
    return resolved


def _task(db, tid, *, typed=False, state='succeeded', plan=None,
          vault='a', boundary='b', completed=1, rowid=None):
    columns = [r[1] for r in db.execute('PRAGMA table_info(wiki_tasks)')]
    values = dict(task_id=tid, vault_path='/synthetic-vault-not-accessed',
                  vault_key=vault * 64, request_kind='all', trigger_source='cli',
                  backend='codex_cli', model='synthetic-model', effort='medium',
                  kit_version='3.0.0', kit_manifest_sha256='c' * 64,
                  boundary_sha256=boundary * 64, state=state, raw_count=1,
                  batch_count=1, completed_batch_count=completed,
                  error_code='interrupted' if state == 'failed' else None,
                  recovery_state='not_needed', recovery_phase='none',
                  created_at=STAMP, updated_at=STAMP)
    if 'outcome_contract' in columns:
        values.update(outcome_contract=CONTRACT if typed else 'legacy',
                      plan_json='{ "kept" : "中文\\r\\n é" }', plan_sha256=plan)
    if rowid is not None:
        values = {'rowid': rowid, **values}
    names = ','.join(values)
    db.execute(f'INSERT INTO wiki_tasks({names}) VALUES({",".join("?" for _ in values)})',
               tuple(values.values()))


def _seed_wiki(db, version):
    _task(db, '1' * 32, rowid=197 if version >= 24 else None)
    db.execute('INSERT INTO wiki_task_batches VALUES(?,?,?, ?,NULL)',
               ('1' * 32, 1, 'succeeded', 1))
    db.execute('INSERT INTO wiki_task_raw VALUES(?,?,?,?,?,?,?,?)',
               ('1' * 32, 1, 1, 'R-20261008-0001', '第三方',
                'raw/synthetic.md', 4, 'd' * 64))
    if version >= 23:
        db.execute('INSERT INTO wiki_observations VALUES(?,?,?,?,?,?,NULL)',
                   ('a' * 64, '/synthetic-vault-not-accessed', '1' * 32, 1, 2, STAMP))
    if version >= 24:
        # Grandfathered typed success is legal historical25 storage. This does
        # not claim an accepted outcome or a production-complete plan.
        _task(db, '2' * 32, typed=True, plan='e' * 64, vault='d', boundary='e', rowid=911)
        db.execute('INSERT INTO wiki_task_batches(rowid,task_id,batch_no,state,item_count) '
                   'VALUES(701,?,?,?,?)', ('2' * 32, 1, 'succeeded', 1))
        db.execute('INSERT INTO wiki_outcome_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                   ('f' * 64, '2' * 32, 1, 'validated', CONTRACT,
                    'e' * 64, 'e' * 64, '{ "synthetic_storage" : true }', STAMP))


def _seed_observation(db):
    db.execute('''INSERT INTO distill_items(item_id,submitted_url,state,phase,
        queued_at,created_at,updated_at,ingestion_contract,
        source_binding_sha256,relation_binding_sha256)
        VALUES(52,'synthetic typed owner','queued','collecting',?,?,?,
               'raw-verified-v1',?,?)''', (STAMP, STAMP, STAMP, 'a' * 64, 'b' * 64))
    detail = json.dumps({'code': 'writer_pending',
                         'manifest': {'source_fact_id': None, 'snapshot_sha256': None},
                         'source_binding_sha256': 'a' * 64,
                         'relation_binding_sha256': 'b' * 64}, ensure_ascii=False)
    db.execute('INSERT INTO ingestion_events VALUES(?,?,?,?,?,?,?,?,?,?)',
               (801, '9' * 64, 'raw-verified-v1', 'item', 52, 52,
                'raw_pending', 'a' * 64, detail, STAMP))


def _old(root, version=26):
    path = root / 'synthetic.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.create_function('ingestion_proof', 3, lambda *_: 0)
        db.executescript((FIXTURES / 'wiki-schema21.sql').read_text())
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript((FIXTURES / 'upgrade-probe-seed.sql').read_text())
        if version >= 22:
            db.executescript((FIXTURES / 'wiki-schema22.sql').read_text())
        if version >= 23:
            db.executescript((FIXTURES / 'upgrade-probe-schema23.sql').read_text())
        if version == 24:
            db.execute('BEGIN IMMEDIATE')
            database.migrate_v24(db)
            db.execute('PRAGMA user_version=24')
            db.commit()
        if version >= 25:
            # Genuine frozen historical25, never a current27 downgrade.
            db.executescript((FIXTURES / 'upgrade-probe-schema25.sql').read_text())
        if version >= 22:
            _seed_wiki(db, version)
        if version >= 24:
            _seed_observation(db)
        db.commit()
        if version == 26:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            database.migrate_v26(db)  # Independent unchanged primary26 migration.
            db.execute('PRAGMA user_version=26')
            db.commit()
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
    path.chmod(0o600)
    return path


def _rows(db, projection=None):
    if projection is None:
        projection = {r[0]: tuple(c[1] for c in db.execute(f'PRAGMA table_info("{r[0]}")'))
                      for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    result = {}
    for name, columns in projection.items():
        names = ','.join('"' + c + '"' for c in columns)
        types = ','.join(f'typeof("{c}")' for c in columns)
        result[name] = tuple(tuple(row) for row in db.execute(f'SELECT rowid,{names},{types} FROM "{name}" ORDER BY rowid'))
    return projection, result


def _snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        return (db.execute('PRAGMA user_version').fetchone()[0],
                tuple(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name')),
                _rows(db))


def _trace_connect(monkeypatch, trace):
    original = database.connect

    @contextmanager
    def traced(path, **kwargs):
        with original(path, **kwargs) as db:
            db.set_trace_callback(trace.append)
            yield db

    monkeypatch.setattr(database, 'connect', traced)


@pytest.mark.parametrize('version', [21, 22, 23, 24, 25, 26])
def test_historical_chain_single_commit_all_typed_rows(case_root, monkeypatch, version):
    path = _old(case_root, version)
    with closing(sqlite3.connect(path)) as db:
        projection, before = _rows(db)
    trace = []
    _trace_connect(monkeypatch, trace)
    database.initialize(path)
    with database.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        assert _rows(db, projection)[1] == before
        assert db.execute('SELECT count(*) FROM source_identity_events').fetchone()[0] == 0
        assert database.source_schema_inventory(db) == (27, ())
        assert database._wiki_execution_schema_inventory(db) == 27
        assert database._source_identity_schema_inventory(db) == 27
        if version >= 24:
            assert db.execute('SELECT count(*) FROM ingestion_events').fetchone()[0] > 0
            assert db.execute('SELECT count(*) FROM wiki_outcome_receipts').fetchone()[0] > 0
            assert tuple(db.execute('SELECT rowid,state,completed_batch_count FROM wiki_tasks '
                                    'WHERE task_id=?', ('2' * 32,)).fetchone()) == (911, 'succeeded', 1)
    controls = [s.strip().upper() for s in trace if s.strip().upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK'))]
    assert controls == ['BEGIN IMMEDIATE', 'COMMIT']


def test_fresh_and_migrated_catalog_identical_reinitialize_readonly(case_root):
    fresh = case_root / 'fresh.sqlite3'
    database.initialize(fresh)
    old = _old(case_root)
    database.initialize(old)
    with closing(sqlite3.connect(fresh)) as a, closing(sqlite3.connect(old)) as b:
        # Only documented final table-name quoting may differ.
        def catalog(db):
            return tuple((kind, name, owner, sql) for kind, name, owner, sql in db.execute(
                'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name'))
        assert catalog(a) == catalog(b)
    before = _snapshot(old)
    database.initialize(old)
    assert _snapshot(old) == before


@pytest.mark.parametrize('reserved', [*NEW_NAMES, 'wiki_tasks_v27'])
def test_every_reserved_name_rejected_before_write(case_root, monkeypatch, reserved):
    path = _old(case_root)
    with closing(sqlite3.connect(path)) as db:
        db.execute(f'CREATE TABLE "{reserved}"(x TEXT)')
        db.commit()
    before = _snapshot(path)
    trace = []
    _trace_connect(monkeypatch, trace)
    with pytest.raises(RuntimeError, match='catalog closure'):
        database.initialize(path)
    assert _snapshot(path) == before
    assert not any(s.upper().startswith(('BEGIN', 'INSERT', 'UPDATE', 'DROP', 'CREATE', 'ALTER')) for s in trace)


@pytest.mark.parametrize('sql', [
    'CREATE TABLE unknown_incoming(task_id TEXT REFERENCES wiki_tasks(task_id))',
    'CREATE TABLE unknown_owner(item_id INTEGER REFERENCES distill_items(item_id))',
    'CREATE TABLE unknown_identity(event_id INTEGER REFERENCES source_identity_events(event_id))',
    'CREATE VIEW unknown_view AS SELECT task_id FROM wiki_tasks',
    "CREATE TRIGGER unknown_guard BEFORE INSERT ON settings BEGIN SELECT count(*) FROM wiki_tasks; END",
    'CREATE INDEX unknown_index ON wiki_tasks(model)',
])
def test_unknown_dependency_closed_before_write(case_root, monkeypatch, sql):
    path = _old(case_root)
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql)
        db.commit()
    before = _snapshot(path)
    trace = []
    _trace_connect(monkeypatch, trace)
    with pytest.raises(RuntimeError, match='catalog closure'):
        database.initialize(path)
    assert _snapshot(path) == before
    assert not any(s.upper().startswith(('BEGIN', 'INSERT', 'UPDATE', 'DROP', 'CREATE', 'ALTER')) for s in trace)


@pytest.mark.parametrize('version', [26, 27])
@pytest.mark.parametrize('name,old,new', [
    ('wiki_one_unresolved_task_per_vault', "'publishing'", "'publishing','succeeded'"),
    ('wiki_tasks', "DEFAULT 'legacy'", "DEFAULT 'r08-wiki-outcomes-v1'"),
    ('wiki_tasks', 'CHECK ((state', 'CHECK (1 OR (state'),
    ('materials', 'metadata_json TEXT NOT NULL', 'metadata_json BLOB NOT NULL'),
    ('wiki_outcome_receipts_publish_unavailable', 'BEFORE INSERT', 'AFTER INSERT'),
])
def test_known_ddl_drift_never_repaired(case_root, version, name, old, new):
    path = _old(case_root)
    if version == 27:
        database.initialize(path)
    # Corrupt only this disposable negative fixture, including a same-name ABI
    # drift. No writable_schema appears on a production connection.
    with closing(sqlite3.connect(path)) as db:
        text = db.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
        assert text.count(old) == 1
        db.execute('PRAGMA writable_schema=ON')
        db.execute('UPDATE sqlite_schema SET sql=? WHERE name=?', (text.replace(old, new, 1), name))
        db.execute('PRAGMA writable_schema=OFF')
        db.commit()
    # The malformed CHECK case may fail SQLite's own schema parser first; that
    # is also a closed refusal, never initialization/repair authority.
    before = path.read_bytes()
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        database.initialize(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('name,old,new', [
    ('wiki_task_contract_plan_unique', "COALESCE(plan_sha256,'')", 'plan_sha256'),
    ('wiki_task_legacy_boundary_unique', "WHERE outcome_contract='legacy'", "WHERE outcome_contract!='legacy'"),
    ('source_identity_one_root', 'IS NULL', 'IS NOT NULL'),
    ('source_identity_item_order', 'item_id,event_id', 'item_id,event_id DESC'),
    ('source_identity_events', 'length(event_key)=64', 'length(event_key)>=0'),
    ('source_identity_events', 'REFERENCES distill_items(item_id)', 'REFERENCES topic_entries(topic_id)'),
])
def test_schema27_index_hash_and_fk_drift_denied(case_root, name, old, new):
    path = _old(case_root)
    database.initialize(path)
    with closing(sqlite3.connect(path)) as db:
        sql = db.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
        assert sql.count(old) == 1
        db.execute('PRAGMA writable_schema=ON')
        db.execute('UPDATE sqlite_schema SET sql=? WHERE name=?', (sql.replace(old, new, 1), name))
        db.execute('PRAGMA writable_schema=OFF')
        db.commit()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='DDL mismatch'):
        database.initialize(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('version', [26, 27])
def test_missing_known_guard_is_not_repaired(case_root, version):
    path = _old(case_root)
    if version == 27:
        database.initialize(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute('DROP TRIGGER wiki_task_no_delete')
        db.commit()
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match='catalog closure'):
        database.initialize(path)
    assert _snapshot(path) == before


@pytest.mark.parametrize('version', [21, 22, 23, 24, 25, 26])
def test_final27_failure_rolls_back_entire_old_chain(case_root, monkeypatch, version):
    path = _old(case_root, version)
    before = _snapshot(path)

    def fail(*_):
        raise RuntimeError('synthetic final27 postcheck failure')

    monkeypatch.setattr(database, '_schema27_check_preservation', fail)
    with pytest.raises(RuntimeError, match='synthetic final27'):
        database.initialize(path)
    assert _snapshot(path) == before


def test_postcommit_failure_reports_committed27(case_root, monkeypatch):
    path = _old(case_root)

    def fail(*_):
        raise RuntimeError('synthetic FK enable failure')

    monkeypatch.setattr(database, '_schema27_enable_foreign_keys', fail)
    with pytest.raises(RuntimeError, match='schema27 committed; postcommit'):
        database.initialize(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        assert db.execute('SELECT count(*) FROM wiki_outcome_receipts').fetchone()[0] > 0


def test_contract_plan_dedup_preserves_legacy_six_keys(case_root):
    path = _old(case_root)
    database.initialize(path)
    with database.connect(path) as db:
        # Same old raw boundary: successful legacy plus new typed is permitted.
        _task(db, '3' * 32, typed=True, state='queued', completed=0, plan='1' * 64)
        db.execute("UPDATE wiki_tasks SET state='failed',error_code='interrupted' WHERE task_id=?", ('3' * 32,))
        with pytest.raises(sqlite3.IntegrityError):
            _task(db, '4' * 32, typed=True, state='queued', completed=0, plan='1' * 64)
        _task(db, '5' * 32, typed=True, state='queued', completed=0, plan='2' * 64)
        for plan in (None, '3' * 64):
            with pytest.raises(sqlite3.IntegrityError):
                _task(db, '6' * 32, plan=plan)


@pytest.mark.parametrize('state,completed,plan', [
    ('succeeded', 1, '1' * 64), ('failed', 0, '1' * 64),
    ('queued', 1, '1' * 64), ('queued', 0, None),
])
def test_typed_task_insert_requires_inert_queued(case_root, state, completed, plan):
    path = _old(case_root)
    database.initialize(path)
    with database.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match='wiki outcome binding required'):
            _task(db, '3' * 32, typed=True, state=state, completed=completed, plan=plan)


@pytest.mark.parametrize('parent,state,error', [
    ('3' * 32, 'succeeded', None), ('3' * 32, 'failed', 'interrupted'),
    ('9' * 32, 'queued', None),
])
def test_typed_batch_insert_requires_parent_and_queued(case_root, parent, state, error):
    path = _old(case_root)
    database.initialize(path)
    with database.connect(path) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        _task(db, '3' * 32, typed=True, state='queued', completed=0, plan='1' * 64)
        with pytest.raises(sqlite3.IntegrityError, match='wiki typed batch must start queued'):
            db.execute('INSERT INTO wiki_task_batches VALUES(?,?,?,?,?)', (parent, 1, state, 1, error))


def test_typed_completion_requires_same_batch_accepted(case_root):
    path = _old(case_root)
    database.initialize(path)
    with database.connect(path) as db:
        _task(db, '3' * 32, typed=True, state='queued', completed=0, plan='1' * 64)
        db.execute('INSERT INTO wiki_task_batches VALUES(?,?,?,1,NULL)', ('3' * 32, 1, 'queued'))
        for state in ('preparing', 'running', 'validating', 'publishing'):
            db.execute('UPDATE wiki_tasks SET state=? WHERE task_id=?', (state, '3' * 32))
            db.execute('UPDATE wiki_task_batches SET state=? WHERE task_id=?', (state, '3' * 32))
        with pytest.raises(sqlite3.IntegrityError, match='accepted'):
            db.execute("UPDATE wiki_task_batches SET state='succeeded' WHERE task_id=?", ('3' * 32,))
        with pytest.raises(sqlite3.IntegrityError, match='accepted'):
            db.execute("UPDATE wiki_tasks SET state='succeeded',completed_batch_count=1 WHERE task_id=?", ('3' * 32,))


@pytest.mark.parametrize('authority', ['absent', 'default', 'null'])
def test_two_proof_functions_default_deny_no_null_bypass(case_root, authority):
    path = _old(case_root)
    database.initialize(path)
    manager = closing(sqlite3.connect(path)) if authority == 'absent' else database.connect(path)
    with manager as db:
        if authority == 'null':
            db.create_function('wiki_outcome_accept', 9, lambda *_: None)
            db.create_function('source_identity_append', 2, lambda *_: None)
        if authority != 'absent':
            functions = {r[0]: r for r in db.execute('PRAGMA function_list')}
            assert functions['wiki_outcome_accept'][4] == 9
            assert functions['source_identity_append'][4] == 2
            assert not functions['wiki_outcome_accept'][5] & 2048
            assert not functions['source_identity_append'][5] & 2048
        error = sqlite3.OperationalError if authority == 'absent' else sqlite3.IntegrityError
        with pytest.raises(error):
            db.execute('INSERT INTO wiki_outcome_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                       ('8' * 64, '2' * 32, 1, 'accepted', CONTRACT,
                        'e' * 64, 'e' * 64, '{}', STAMP))
        with pytest.raises(error):
            db.execute('''INSERT INTO source_identity_events(event_key,item_id,
                expected_prior_event_id,kind,contract,source_binding_sha256,
                relation_binding_sha256,review_version,detail_json,created_at)
                VALUES(?,52,NULL,'model_proposal','local-source-identity-v1',?,?,?,'{}',?)''',
                       ('7' * 64, 'a' * 64, 'b' * 64, 'c' * 64, STAMP))
        assert db.execute('SELECT count(*) FROM source_identity_events').fetchone()[0] == 0


def test_identity_exact_tuple_includes_created_at_without_grant(case_root):
    path = _old(case_root)
    database.initialize(path)
    seen = []
    values = ('7' * 64, 52, None, 'model_proposal', 'local-source-identity-v1',
              'a' * 64, 'b' * 64, 'c' * 64, '{ "kept" : "中文" }', STAMP)
    with database.connect(path) as db:
        def deny(item, payload):
            seen.append((item, json.loads(payload)))
            return 0
        db.create_function('source_identity_append', 2, deny)
        with pytest.raises(sqlite3.IntegrityError, match='append proof unavailable'):
            db.execute('''INSERT INTO source_identity_events(event_key,item_id,
                expected_prior_event_id,kind,contract,source_binding_sha256,
                relation_binding_sha256,review_version,detail_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''', values)
    assert seen == [(52, list(values))]


@pytest.mark.parametrize('field', range(9))
def test_formal_full_nine_tuple_denied_without_authority(case_root, field):
    path = _old(case_root)
    database.initialize(path)
    values = ['8' * 64, '2' * 32, 1, 'accepted', CONTRACT,
              'e' * 64, 'e' * 64, '{}', STAMP]
    # The callback is an observer that denies, never a publication certificate.
    changed = list(values)
    changed[field] = 2 if field == 2 else 'changed'
    seen = []
    with database.connect(path) as db:
        def deny(*args):
            seen.append(args)
            return 0
        db.create_function('wiki_outcome_accept', 9, deny)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO wiki_outcome_receipts VALUES(?,?,?,?,?,?,?,?,?)', changed)
    if field == 3:
        # Nonaccepted phase goes to the table CHECK, not the accepted UDF.
        assert seen == []
    else:
        assert seen == [tuple(changed)]
