"""Disposable SQL-built 21/22/23 lanes; no app, installer or native helpers."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from knowledge_distiller.v1 import database as database_module
from knowledge_distiller.v1 import data_upgrade_probe as probe


FIXTURES = Path(__file__).with_name('fixtures')
SENTINEL = 'private-upgrade-original-中文-é\r\nnever-in-report'
BODY = b'\x00\xff\r\nA'


def _directory(path):
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _marker(database, vault, version):
    path = database.parent / probe.MARKER_NAME
    path.write_text(json.dumps({
        'schema_version': 1, 'purpose': probe.MARKER_PURPOSE,
        'database_name': probe.DATABASE_NAME,
        'database_before_sha256': hashlib.sha256(database.read_bytes()).hexdigest(),
        'expected_schema': version, 'vault_relpath': probe.VAULT_RELPATH,
        'vault_tree_sha256': probe._digest_tree(vault),
    }), encoding='utf-8')
    path.chmod(0o600)


def _prepare(tmp_path, version):
    root = _directory(tmp_path / 'data')
    vault = _directory(root / probe.VAULT_RELPATH)
    raw = _directory(vault / 'raw')
    for name, content in [('原始.md', SENTINEL.encode()), ('附件.bin', BODY)]:
        path = raw / name
        path.write_bytes(content)
        path.chmod(0o600)
    _directory(vault / '空目录')
    original_files = _directory(raw / '合成')
    for ordinal in (1, 2):
        original = original_files / f'{ordinal}.md'
        original.write_bytes(SENTINEL.encode())
        original.chmod(0o600)
    path = root / probe.DATABASE_NAME
    with closing(sqlite3.connect(path)) as db:
        db.executescript((FIXTURES / 'wiki-schema21.sql').read_text())
        if version >= 22:
            db.executescript((FIXTURES / 'wiki-schema22.sql').read_text())
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript((FIXTURES / 'upgrade-probe-seed.sql').read_text())
        db.execute("INSERT INTO confirmation_decisions VALUES(51,'old-revision','manual','继续','waiting_user')")
        db.execute('''INSERT INTO group_decisions VALUES
            (51,'old-request','old-group','old-revision','old-selection','old-payload',
             '{ "state" : "waiting_user" }','{ "human" : "继续" }',?)''', (SENTINEL,))
        db.execute('''INSERT INTO manual_cards(scope_kind,scope_id,item_id,review_round_id,group_id,
            lifecycle,ordering_basis,ordering_reason,entered_at,mapping_json)
            VALUES ('items','independent',51,'old-round','old-group','active','observed',
                    'explicit synthetic human',?,'{ "human" : "继续" }')''', (SENTINEL,))
        db.execute("INSERT INTO settings VALUES('vault_path',?)", (str(vault),))
        db.execute("INSERT INTO settings VALUES('private_sentinel',?)", (SENTINEL,))
        for ordinal, kind, subject, identity in [(1, 'material', 41, '第三方'), (2, 'capture', 81, '本人附言')]:
            db.execute('''INSERT INTO raw_records(raw_id,subject_kind,subject_id,identity,
                relative_path,content,content_sha256,attachments_json,origin,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (f'R-20261008-{ordinal:04}', kind, subject, identity,
                 f'raw/合成/{ordinal}.md', SENTINEL, hashlib.sha256(SENTINEL.encode()).hexdigest(),
                 '[ ]', 'app', '2099-01-01T00:00:00Z'))
        if version >= 22:
            db.execute('''INSERT INTO wiki_tasks(task_id,vault_path,vault_key,request_kind,
                trigger_source,backend,model,effort,kit_version,kit_manifest_sha256,
                boundary_sha256,state,raw_count,batch_count,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('1' * 32, str(vault), 'a' * 64, 'all', 'cli', 'codex_cli', 'synthetic-model',
                 'medium', '3.0.0', 'c' * 64, 'b' * 64, 'queued', 1, 1,
                 '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z'))
            db.execute('INSERT INTO wiki_task_batches VALUES(?,?,?, ?,NULL)', ('1' * 32, 1, 'queued', 1))
            db.execute('INSERT INTO wiki_task_raw VALUES(?,?,?,?,?,?,?,?)',
                       ('1' * 32, 1, 1, 'R-20261008-0001', '第三方', 'raw/合成/1.md',
                        len(SENTINEL.encode()), hashlib.sha256(SENTINEL.encode()).hexdigest()))
        db.commit()
        if version == 23:
            db.executescript((FIXTURES / 'upgrade-probe-schema23.sql').read_text())
            db.execute('INSERT INTO wiki_observations VALUES(?,?,?,?,?,?,NULL)',
                       ('a' * 64, str(vault), '1' * 32, 2, 3, '2099-01-01T00:00:00Z'))
            db.commit()
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    path.chmod(0o600)
    _marker(path, vault, version)
    _directory(tmp_path / 'evidence')
    return path, vault, tmp_path / 'evidence/report.json'


def _run(database, report, formal=None):
    return probe.run(database.parent, report, formal_root=formal or report.parent.parent / 'formal-never-opened')


def _read_rows(path, selected=None):
    """Independent old-column SQL projection, including internal sequence rows."""
    with closing(sqlite3.connect(path)) as db:
        if selected is None:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            selected = {name: tuple(row[1] for row in db.execute(f'PRAGMA table_xinfo("{name}")')) for name in names}
        return selected, {name: db.execute('SELECT rowid,' + ','.join('"' + col + '"' for col in cols)
            + ',' + ','.join('typeof("' + col + '")' for col in cols)
            + ' FROM "' + name + '" ORDER BY rowid').fetchall() for name, cols in selected.items()}


@pytest.mark.parametrize('version', [21, 22, 23])
def test_real_sql_versions_preserve_all_original_columns_and_vault(tmp_path, version):
    path, vault, report = _prepare(tmp_path, version)
    selected, before = _read_rows(path)
    vault_before = probe._digest_tree(vault)
    assert _run(path, report) == 0
    result = json.loads(report.read_text())
    assert result['database']['schema_before'] == version
    assert result['database']['schema_after'] == 25
    assert result['legacy']['identity_contract'] == 'all-prior-schema-and-columns-v2'
    assert set(result['legacy']['table_counts_before']) == set(selected)
    assert result['legacy']['table_counts_after'] == {name: len(rows) for name, rows in before.items()}
    assert result['legacy']['digest_before'] == result['legacy']['digest_after']
    assert _read_rows(path, selected)[1] == before
    assert result['vault']['sha256_after'] == vault_before == probe._digest_tree(vault)
    assert result['vault']['unchanged'] and report.stat().st_mode & 0o777 == 0o600
    assert SENTINEL not in report.read_text() and str(vault) not in report.read_text()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT ingestion_contract,source_binding_sha256,relation_binding_sha256 FROM distill_items').fetchall() == [('legacy', None, None)]
        assert db.execute('SELECT ingestion_contract,source_binding_sha256,relation_binding_sha256 FROM collection_operations').fetchall() == [('legacy', None, None)]
        assert all(row == ('legacy', '{}', None) for row in db.execute('SELECT outcome_contract,plan_json,plan_sha256 FROM wiki_tasks'))
        assert db.execute('SELECT COUNT(*) FROM ingestion_events').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM wiki_outcome_receipts').fetchone()[0] == 0
        assert db.execute('SELECT content FROM source_media').fetchone()[0] == BODY
        assert db.execute('SELECT legacy_material_id FROM media_lifecycle').fetchone()[0] == 40
        assert db.execute("SELECT count(*) FROM distill_items WHERE state='raw_saved'").fetchone()[0] == 0
    with database_module.connect(path) as ordinary:
        with pytest.raises(sqlite3.IntegrityError, match='raw terminal proof unavailable'):
            ordinary.execute("UPDATE distill_items SET state='raw_saved',phase='done' WHERE item_id=51")


@pytest.mark.parametrize('version,declared', [(21, 22), (22, 23), (23, 21)])
def test_real_version_marker_mismatch_never_initializes(tmp_path, monkeypatch, version, declared):
    path, vault, report = _prepare(tmp_path, version)
    _marker(path, vault, declared)
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not migrate'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'unsupported_schema'
    assert path.read_bytes() == before


@pytest.mark.parametrize('declared', [True, 21.0, '21', 20, 24, 25, None])
def test_marker_expected_schema_is_exact_supported_integer(tmp_path, monkeypatch, declared):
    path, vault, report = _prepare(tmp_path, 21)
    _marker(path, vault, declared)
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not migrate'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'fixture_invalid'
    assert path.read_bytes() == before


@pytest.mark.parametrize('version', [21, 22, 23])
def test_real_v24_migration_exception_rolls_back_all_ddl_and_rows(tmp_path, monkeypatch, version):
    path, vault, report = _prepare(tmp_path, version)
    before = path.read_bytes()
    vault_before = probe._digest_tree(vault)
    real = database_module.migrate_v24
    def interrupted(connection):
        real(connection)
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(database_module, 'migrate_v24', interrupted)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'migration_failed'}
    assert path.read_bytes() == before
    assert probe._digest_tree(vault) == vault_before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_committed_observation_mutation_is_not_hidden_by_wiki_prefix(tmp_path, monkeypatch):
    path, _, report = _prepare(tmp_path, 23)
    real = probe.initialize
    def corrupt(database):
        real(database)
        with closing(sqlite3.connect(database)) as db:
            db.execute('UPDATE wiki_observations SET candidate_count=4')
            db.commit()
    monkeypatch.setattr(probe, 'initialize', corrupt)
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25  # No restore claim.


@pytest.mark.parametrize('change', ['extra_column', 'drop_guard', 'wrong_default', 'new_event'])
def test_unapproved_committed_schema_or_backfill_is_rejected(tmp_path, monkeypatch, change):
    path, _, report = _prepare(tmp_path, 23)
    real = probe.initialize
    observed = {}
    def corrupt(database):
        real(database)
        with (database_module.connect(database) if change == 'wrong_default'
              else closing(sqlite3.connect(database))) as db:
            if change == 'extra_column':
                db.execute('ALTER TABLE distill_items ADD COLUMN unapproved TEXT')
            elif change == 'drop_guard':
                db.execute('DROP TRIGGER source_media_ingestion_capture_binding')
            elif change == 'wrong_default':
                guard = db.execute("SELECT sql FROM sqlite_master WHERE name='distill_items_ingestion_binding_immutable'").fetchone()[0]
                observed['guard'] = guard
                assert db.execute("SELECT ingestion_raw_terminal(51,0,'raw_saved','done')").fetchone()[0] == 0
                db.execute('DROP TRIGGER distill_items_ingestion_binding_immutable')
                db.execute("UPDATE distill_items SET source_binding_sha256=?", ('a' * 64,))
                db.execute(guard)  # Restore DDL so only the added-column value is corrupt.
                assert db.execute('SELECT source_binding_sha256 FROM distill_items WHERE item_id=51').fetchone()[0] == 'a' * 64
                assert db.execute("SELECT sql FROM sqlite_master WHERE name='distill_items_ingestion_binding_immutable'").fetchone()[0] == guard
                assert db.execute('PRAGMA user_version').fetchone()[0] == 25
            else:
                # No fake filesystem proof: an unauthorized backfill is a new
                # observation on legacy data, with typed guard deliberately absent.
                guard = db.execute("SELECT sql FROM sqlite_master WHERE name='ingestion_events_observation_typed'").fetchone()[0]
                db.execute('DROP TRIGGER ingestion_events_observation_typed')
                db.create_function('ingestion_proof', 3, lambda *_: 0)
                db.execute('''INSERT INTO ingestion_events VALUES(1,?,'raw-verified-v1',
                    'item',51,51,'raw_pending',?,'{}','2099-01-01T00:00:00Z')''', ('d' * 64, 'e' * 64))
                db.execute(guard)  # DDL again exact; nonempty new table alone must fail.
            db.commit()
    monkeypatch.setattr(probe, 'initialize', corrupt)
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'
    if change == 'wrong_default':
        with closing(sqlite3.connect(path)) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 25  # Postcheck rejected, not rolled back.
            assert db.execute('SELECT source_binding_sha256 FROM distill_items WHERE item_id=51').fetchone()[0] == 'a' * 64
            assert db.execute("SELECT sql FROM sqlite_master WHERE name='distill_items_ingestion_binding_immutable'").fetchone()[0] == observed['guard']


def test_postcheck_failure_retains_committed_25_not_restored_claim(tmp_path, monkeypatch):
    path, _, report = _prepare(tmp_path, 22)
    original = probe._snapshot
    def unhealthy(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('prior') is not None:
            result['quick_check'] = False
        return result
    monkeypatch.setattr(probe, '_snapshot', unhealthy)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'postcheck_failed'}
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25


@pytest.mark.parametrize('change', ['bytes', 'permissions', 'directory'])
def test_committed_vault_changes_fail_without_recovery_claim(tmp_path, monkeypatch, change):
    path, vault, report = _prepare(tmp_path, 21)
    real = probe.initialize
    def corrupt(database):
        real(database)
        target = vault / 'raw/原始.md'
        if change == 'bytes':
            target.write_bytes(target.read_bytes() + b'changed')
        elif change == 'permissions':
            target.chmod(0o644)
        else:
            (vault / 'extra').mkdir(mode=0o700)
    monkeypatch.setattr(probe, 'initialize', corrupt)
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'


@pytest.mark.parametrize('suffix,linked', [('-wal', False), ('-shm', False), ('-wal', True), ('-shm', True)])
def test_no_wal_shm_including_dangling_links_before_any_read(tmp_path, monkeypatch, suffix, linked):
    path, _, report = _prepare(tmp_path, 21)
    before = path.read_bytes()
    target = Path(str(path) + suffix)
    if linked:
        target.symlink_to(tmp_path / 'missing-sidecar')
    else:
        target.write_bytes(b'')
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not migrate'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'fixture_invalid'
    assert path.read_bytes() == before


@pytest.mark.parametrize('relation', ['same', 'ancestor', 'child'])
def test_formal_directory_relations_fail_before_migration(tmp_path, monkeypatch, relation):
    path, _, report = _prepare(tmp_path, 21)
    formal = {'same': path.parent, 'ancestor': tmp_path, 'child': path.parent / 'formal'}[relation]
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not migrate'))
    assert _run(path, report, formal=formal) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize('target', ['root', 'vault', 'marker', 'database'])
def test_permissions_do_not_get_repaired_implicitly(tmp_path, monkeypatch, target):
    path, vault, report = _prepare(tmp_path, 21)
    selected = {'root': path.parent, 'vault': vault, 'marker': path.parent / probe.MARKER_NAME, 'database': path}[target]
    selected.chmod(0o755 if selected.is_dir() else 0o644)
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not migrate'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'fixture_invalid'
    assert path.read_bytes() == before


def test_source_digest_distinguishes_text_blob_null_and_empty(tmp_path):
    path, vault, _ = _prepare(tmp_path, 21)
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE original_extra(value)')
        db.executemany('INSERT INTO original_extra VALUES(?)', [(None,), ('',), (b'',), ('é',), ('é',)])
        db.commit()
    before = probe._snapshot(path, vault)
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE original_extra SET value='' WHERE value IS NULL")
        db.commit()
    changed = probe._snapshot(path, vault)
    assert before['legacy_table_counts'] == changed['legacy_table_counts']
    assert before['legacy_digest'] != changed['legacy_digest']


@pytest.mark.parametrize('version', [21, 22, 23])
def test_actual_v25_postcheck_exception_rolls_back_the_whole_version_chain(tmp_path, monkeypatch, version):
    path, vault, report = _prepare(tmp_path, version)
    selected, rows = _read_rows(path)
    before = path.read_bytes()
    vault_before = probe._digest_tree(vault)
    with closing(sqlite3.connect(path)) as db:
        objects = probe._objects(db)
    actual = database_module._check_v25_preservation
    def interrupted(db, *args):
        actual(db, *args)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='distill_items_raw_terminal_proof'").fetchone()
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(database_module, '_check_v25_preservation', interrupted)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'migration_failed'}
    assert path.read_bytes() == before
    assert _read_rows(path, selected)[1] == rows
    assert probe._digest_tree(vault) == vault_before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert probe._objects(db) == objects
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('change', ['missing', 'proof_udf', 'reopen'])
def test_committed_v25_terminal_guard_damage_is_rejected(tmp_path, monkeypatch, change):
    path, _, report = _prepare(tmp_path, 23)
    actual = probe.initialize
    def corrupt(database):
        actual(database)
        name = 'distill_items_raw_terminal_no_reopen' if change == 'reopen' else 'distill_items_raw_terminal_proof'
        with closing(sqlite3.connect(database)) as db:
            sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
            db.execute('DROP TRIGGER ' + name)
            if change != 'missing':
                sql = sql.replace('ingestion_raw_terminal(', 'incorrect_raw_terminal(') if change == 'proof_udf' else sql.replace(' OR NEW.phase IS NOT OLD.phase', '')
                db.execute(sql)
            db.commit()
    monkeypatch.setattr(probe, 'initialize', corrupt)
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25  # Committed, no recovery claim.


@pytest.mark.parametrize('change', ['state', 'paired_check', 'table_name'])
def test_v25_parent_catalog_projection_must_match_exact_approved_transform(tmp_path, monkeypatch, change):
    # Corrupt the catalog read boundary, not writable_schema or an invented target DB.
    # Actual initialization still commits 25; this verifies the probe's independent comparison.
    path, _, report = _prepare(tmp_path, 21)
    actual = probe._objects
    def damaged_catalog(db):
        result = actual(db)
        if db.execute('PRAGMA user_version').fetchone()[0] == 25:
            kind, parent, sql = result['distill_items']
            old, new = {
                'state': (",'raw_saved'", ''),
                'paired_check': ("AND ingestion_contract='raw-verified-v1'", 'AND 1'),
                'table_name': ('CREATE TABLE "distill_items"', 'CREATE TABLE distill_items'),
            }[change]
            assert sql.count(old) == 1
            result['distill_items'] = (kind, parent, sql.replace(old, new, 1))
        return result
    monkeypatch.setattr(probe, '_objects', damaged_catalog)
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25


def test_target_version_drift_is_rejected_before_initialize(tmp_path, monkeypatch):
    path, _, report = _prepare(tmp_path, 21)
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'SCHEMA_VERSION', 26)
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not migrate'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'unsupported_schema'
    assert path.read_bytes() == before


def test_old_fk_definition_drift_is_independently_rejected(tmp_path):
    path, _, _ = _prepare(tmp_path, 23)
    with closing(sqlite3.connect(path)) as db:
        prior = probe._freeze(db, 23)
    database_module.initialize(path)
    # Keep all actual schema/rows/indices correct; a stale FK source descriptor must fail.
    prior['foreign_keys']['submitted_sources'] = ()
    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(probe.UpgradeProbeError, match='legacy_changed'):
            probe._check_after(db, prior)
