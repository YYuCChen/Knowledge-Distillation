"""Disposable genuine SQL-built 21/22/23/25/26 lanes; no App/native helpers."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from knowledge_distiller.v1 import database as database_module
from knowledge_distiller.v1 import data_upgrade_probe as probe


FIXTURES = Path(__file__).with_name('fixtures')
SENTINEL = 'private-upgrade-original-中文-é\r\nnever-in-report'
BODY = b'\x00\xff\r\nA'


def _directory(path):
    assert path.resolve().is_relative_to(Path('/tmp').resolve())
    assert path.parent.stat().st_uid == os.getuid()
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


def _prepare(tmp_path, version, *, sparse_wiki=False):
    if version == 26:
        assert not sparse_wiki
        path, vault, report = _prepare(tmp_path, 25)
        _upgrade_to26(path)
        _marker(path, vault, 26)
        return path, vault, report
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
        if sparse_wiki:
            assert version == 22
            _seed_sparse22(db, vault)
        elif version >= 22:
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
        if version >= 23:
            db.executescript((FIXTURES / 'upgrade-probe-schema23.sql').read_text())
            db.execute('INSERT INTO wiki_observations VALUES(?,?,?,?,?,?,NULL)',
                       ('a' * 64, str(vault), '1' * 32, 2, 3, '2099-01-01T00:00:00Z'))
            db.commit()
        if version == 25:
            db.executescript((FIXTURES / 'upgrade-probe-schema25.sql').read_text())
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    path.chmod(0o600)
    if version == 25:
        _seed_prior25(path, vault)
    _marker(path, vault, version)
    _directory(tmp_path / 'evidence')
    return path, vault, tmp_path / 'evidence/report.json'


def _seed_prior25(path, vault):
    """Real typed observation/writer plus a real legacy validated candidate.

    Never initialize this historical DB with the current27 engine. The private
    WikiOutcomes writer produces the checked payload; its reserved canonical
    storage projection is validated only, never an accepted/published receipt.
    The deterministic checker below covers the complete synthetic closing note;
    it does not stand in for a live model or C/source-completeness certificate.
    """
    from dataclasses import asdict
    from knowledge_distiller.v1.ingestion import Ingestion, encoded
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.wiki_tasks import FrozenRaw, WikiTask, WikiBatch, _boundary
    from knowledge_distiller.v1.wiki_outcomes import (
        WikiOutcomes, Outcome, NoKnowledgeReview, CONTRACT, VALUE_CHECKS)
    from .test_raw import material

    stamp = '2099-01-01T00:00:00Z'
    store = Store(path, runtime_root=path.parent / 'synthetic-runtime')
    # A separate real SourceFact and locked raw writer supply old raw_verified
    # and raw_saved rows, without dropping a guard or registering bool1 proof.
    mid = material(store, 'x', '仅含合成来源原文，否定与条件保持。',
                   key='prior25-real-terminal', url='synthetic://prior25-terminal')
    item = store.create_item('synthetic://prior25-terminal',
        ingestion_contract='raw-verified-v1', source_binding_sha256='a' * 64,
        relation_binding_sha256='b' * 64)
    with database_module.connect(path) as db:
        db.execute('''INSERT INTO collection_operations(operation_id,kind,source_key,title,
            manifest_json,signature,content_signature,authority_json,confirmation_token,state,
            queued_at,created_at,updated_at,ingestion_contract,source_binding_sha256,relation_binding_sha256)
            VALUES(72,'same_topic','prior25-bound','合成已绑定集合','{}','kept-signature',
            'kept-content','{}','prior25-token','queued',?,?,?,'raw-verified-v1',?,?)''',
            (stamp, stamp, stamp, 'c' * 64, 'd' * 64))
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (mid, item))
        db.execute('''INSERT INTO submitted_sources(item_id,input_kind,input_key,input_label,
            input_metadata,content,retain_until,retryable) VALUES(?,'direct_text',?,? ,?,NULL,?,0)''',
            (item, 'prior25-null', '\ufeff中文\r\n标签', '{ "retain" : "原样" }', stamp))
    store.append_ingestion_event(item, kind='source_ready', code='source_fact_ready')
    store.append_ingestion_event(item, kind='raw_pending', code='writer_pending')
    assert store.claim_next_item() == item
    Ingestion(store).complete_raw_owner(item, vault, subject_kind='material', subject_id=mid,
                                       expected_revision=store.item_bundle(item)['review_revision'])
    assert store.item_bundle(item)['state'] == 'raw_saved'

    rid = 'R-20261008-9999'
    closing_mid = material(store, 'x', '谢谢阅读，再见。', key='prior25-closing-note',
                           url='synthetic://prior25-closing-note')
    relative = f'raw/外部/2026/10/{rid}.md'
    content = (f'---\n编号: {rid}\n格式版本: 1\n身份: 第三方\n作者: 合成作者\n标题: 结束致意\n'
               '---\n\n谢谢阅读，再见。\r\n\n^source-1\n').encode()
    raw_path = vault / relative
    raw_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_path.write_bytes(content)
    raw_path.chmod(0o600)
    raw = FrozenRaw(relative, rid, '第三方', len(content), hashlib.sha256(content).hexdigest(), 1, 1)
    boundary = _boundary(((raw,),))
    plan = encoded({'raw_ids': [rid], 'contract': CONTRACT})
    plan_hash = hashlib.sha256(plan.encode()).hexdigest()
    task = WikiTask('2' * 32, str(vault), 'd' * 64, 'all', 'cli', 'codex_cli',
        'synthetic-model', 'medium', '3.0.0', 'c' * 64, boundary, 'running', 1, 1, 0,
        None, 'not_needed', 'none', stamp, stamp, (WikiBatch(1, 'running', 1, None),), (raw,))
    class ClosingNoteChecker:
        def review(self, *, raw, full_raw, outcome, context):
            assert full_raw == content and context == ((task.raw[0], content),)
            return NoKnowledgeReview('verified', '合成全文仅谢谢阅读与告别，没有定义、方法、参考线索或关系。',
                                     True, tuple(sorted(VALUE_CHECKS)))
    candidate = WikiOutcomes(path.parent / 'synthetic-outcomes.sqlite3')
    candidate.initialize()
    proposed = Outcome(rid, raw.content_sha256, CONTRACT, boundary, 'processed_no_knowledge',
        'non_substantive', '完整合成正文仅为阅读致意与告别，无其他实质主张。', (('wiki/log.md', 'e' * 64),))
    receipt = candidate.validate(task, 1, {rid: content}, (proposed,), checker=ClosingNoteChecker())
    payload = candidate.get(receipt)
    assert payload['raw'] == [asdict(raw)]
    with database_module.connect(path) as db:
        db.execute('''INSERT INTO wiki_tasks(task_id,vault_path,vault_key,request_kind,
            trigger_source,backend,model,effort,kit_version,kit_manifest_sha256,
            boundary_sha256,state,raw_count,batch_count,created_at,updated_at,
            outcome_contract,plan_json,plan_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (task.task_id, str(vault), task.vault_key, 'all', 'cli', 'codex_cli',
             task.model, 'medium', '3.0.0', 'c' * 64, boundary, 'running', 1, 1,
             stamp, stamp, CONTRACT, plan, plan_hash))
        db.execute('INSERT INTO wiki_task_batches VALUES(?,?,?, ?,NULL)', (task.task_id, 1, 'running', 1))
        db.execute('INSERT INTO wiki_task_raw VALUES(?,?,?,?,?,?,?,?)',
            (task.task_id, 1, 1, rid, '第三方', relative, len(content), raw.content_sha256))
        db.execute('''INSERT INTO raw_records(raw_id,subject_kind,subject_id,identity,
            relative_path,content,content_sha256,attachments_json,origin,created_at)
            VALUES(?, 'material', ?, '第三方', ?, ?, ?, '[]', 'app', ?)''',
            (rid, closing_mid, relative, content.decode(), raw.content_sha256, stamp))
        db.execute("UPDATE raw_counters SET last=9999 WHERE day='20261008'")
        db.execute('''INSERT INTO wiki_outcome_receipts(receipt_id,task_id,batch_no,phase,
            contract,boundary_sha256,plan_sha256,payload_json,created_at)
            VALUES(?,?,1,'validated',?,?,?,?,?)''',
            (receipt, task.task_id, CONTRACT, boundary, plan_hash, encoded(payload), stamp))
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT count(*) FROM ingestion_events').fetchone()[0] >= 3
        assert db.execute('SELECT count(*) FROM wiki_outcome_receipts').fetchone()[0] == 1


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


@pytest.mark.parametrize('version', [21, 22, 23, 25, 26])
def test_real_sql_versions_preserve_all_original_columns_and_vault(tmp_path, version):
    path, vault, report = _prepare(tmp_path, version)
    selected, before = _read_rows(path)
    vault_before = probe._digest_tree(vault)
    assert _run(path, report) == 0
    result = json.loads(report.read_text())
    assert result['database']['schema_before'] == version
    assert result['database']['schema_after'] == 27
    assert result['legacy']['identity_contract'] == 'all-prior-schema-and-columns-v2'
    assert set(result['legacy']['table_counts_before']) == set(selected)
    assert result['legacy']['table_counts_after'] == {name: len(rows) for name, rows in before.items()}
    assert result['legacy']['digest_before'] == result['legacy']['digest_after']
    assert _read_rows(path, selected)[1] == before
    assert result['vault']['sha256_after'] == vault_before == probe._digest_tree(vault)
    assert result['vault']['unchanged'] and report.stat().st_mode & 0o777 == 0o600
    assert SENTINEL not in report.read_text() and str(vault) not in report.read_text()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT ingestion_contract,source_binding_sha256,relation_binding_sha256 FROM distill_items WHERE item_id=51').fetchall() == [('legacy', None, None)]
        assert db.execute('SELECT ingestion_contract,source_binding_sha256,relation_binding_sha256 FROM collection_operations WHERE operation_id=71').fetchall() == [('legacy', None, None)]
        if version not in (25, 26):
            assert all(row == ('legacy', '{}', None) for row in db.execute('SELECT outcome_contract,plan_json,plan_sha256 FROM wiki_tasks'))
            assert db.execute('SELECT COUNT(*) FROM ingestion_events').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM wiki_outcome_receipts').fetchone()[0] == 0
        else:
            assert db.execute('SELECT count(*) FROM ingestion_events').fetchone()[0] >= 3
            assert db.execute('SELECT count(*) FROM wiki_outcome_receipts').fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM distill_items WHERE state='raw_saved'").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM wiki_tasks WHERE outcome_contract!='legacy'").fetchone()[0] == 1
        assert all(row == ('legacy',) for row in db.execute('SELECT binding_scope FROM submitted_sources'))
        assert db.execute('SELECT content FROM source_media').fetchone()[0] == BODY
        assert db.execute('SELECT legacy_material_id FROM media_lifecycle').fetchone()[0] == 40
        if version not in (25, 26):
            assert db.execute("SELECT count(*) FROM distill_items WHERE state='raw_saved'").fetchone()[0] == 0
    with database_module.connect(path) as ordinary:
        with pytest.raises(sqlite3.IntegrityError, match='raw terminal proof unavailable'):
            ordinary.execute("UPDATE distill_items SET state='raw_saved',phase='done' WHERE item_id=51")


@pytest.mark.parametrize('version,declared', [(21, 22), (22, 23), (23, 21), (25, 23), (23, 25)])
def test_real_version_marker_mismatch_never_initializes(tmp_path, monkeypatch, version, declared):
    path, vault, report = _prepare(tmp_path, version)
    _marker(path, vault, declared)
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not migrate'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'unsupported_schema'
    assert path.read_bytes() == before


@pytest.mark.parametrize('declared', [True, 21.0, '21', 20, 24, 27, 28, None])
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
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27  # No restore claim.


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
                assert db.execute('PRAGMA user_version').fetchone()[0] == 27
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
            assert db.execute('PRAGMA user_version').fetchone()[0] == 27  # Postcheck rejected, not rolled back.
            assert db.execute('SELECT source_binding_sha256 FROM distill_items WHERE item_id=51').fetchone()[0] == 'a' * 64
            assert db.execute("SELECT sql FROM sqlite_master WHERE name='distill_items_ingestion_binding_immutable'").fetchone()[0] == observed['guard']


@pytest.mark.parametrize('version', [22, 25])
def test_postcheck_failure_retains_committed_27_not_restored_claim(tmp_path, monkeypatch, version):
    path, _, report = _prepare(tmp_path, version)
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
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27


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
    fingerprints, counts = [], []
    # A known original BLOB-affinity nullable column, not an unknown extra table.
    for value in (None, '', b'', 'é', 'é'):
        with closing(sqlite3.connect(path)) as db:
            db.execute('UPDATE submitted_sources SET content=? WHERE item_id=51', (value,))
            db.commit()
        snapshot = probe._snapshot(path, vault)
        fingerprints.append(snapshot['legacy_digest'])
        counts.append(snapshot['legacy_table_counts'])
    assert len(set(fingerprints)) == 5
    assert all(value == counts[0] for value in counts)


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
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27  # Committed, no recovery claim.


@pytest.mark.parametrize('change', ['state', 'paired_check', 'table_name'])
def test_v25_parent_catalog_projection_must_match_exact_approved_transform(tmp_path, monkeypatch, change):
    path, _, report = _prepare(tmp_path, 21)
    actual = probe._sqlite_readonly
    def reader(database):
        def alter(sql, rows):
            if sql != 'SELECT type,name,tbl_name,sql FROM sqlite_schema':
                return rows
            damaged = []
            for kind, name, parent, ddl in rows:
                if name == 'distill_items':
                    old, new = {
                        'state': (",'raw_saved'", ''),
                        'paired_check': ("AND ingestion_contract='raw-verified-v1'", 'AND 1'),
                        'table_name': ('CREATE TABLE "distill_items"', 'CREATE TABLE "unapproved_parent"'),
                    }[change]
                    assert ddl.count(old) == 1
                    ddl = ddl.replace(old, new, 1)
                damaged.append((kind, name, parent, ddl))
            return damaged
        return _MetadataReader(actual(database), alter)
    monkeypatch.setattr(probe, '_sqlite_readonly', reader)
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27


def test_target_version_drift_is_rejected_before_initialize(tmp_path, monkeypatch):
    path, _, report = _prepare(tmp_path, 21)
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'SCHEMA_VERSION', 28)
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


@pytest.mark.parametrize('change', [
    'old_guard_missing', 'old_guard_changed', 'observation_changed', 'terminal_missing',
    'input_view', 'input_index', 'input_trigger', 'incoming_fk', 'parent_view',
    'reserved_trigger', 'reserved_index', 'temporary_input', 'temporary_parent',
    'intake_frozen', 'wrong_input_type', 'wrong_input_default', 'wrong_outcome_default',
])
def test_prior25_damage_is_rejected_before_any_initialize(tmp_path, monkeypatch, change):
    path, vault, report = _prepare(tmp_path, 25)
    with database_module.connect(path) as db:
        if change in ('old_guard_missing', 'old_guard_changed', 'observation_changed', 'terminal_missing'):
            name = {'old_guard_missing': 'submitted_sources_ingestion_no_delete',
                    'old_guard_changed': 'submitted_sources_ingestion_no_release',
                    'observation_changed': 'ingestion_events_observation_typed',
                    'terminal_missing': 'distill_items_raw_terminal_proof'}[change]
            sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
            db.execute('DROP TRIGGER ' + name)
            if change in ('old_guard_changed', 'observation_changed'):
                db.execute(sql.replace('ingestion input is retained', 'unapproved retained')
                           if change == 'old_guard_changed' else sql.replace('writer_pending', 'unknown_pending'))
        elif change == 'input_view':
            db.execute('CREATE VIEW unapproved_input AS SELECT input_key FROM submitted_sources')
        elif change == 'input_index':
            db.execute('CREATE INDEX unapproved_input ON submitted_sources(input_label)')
        elif change == 'input_trigger':
            db.execute("CREATE TRIGGER unapproved_input BEFORE INSERT ON submitted_sources BEGIN SELECT RAISE(ABORT,'extra'); END")
        elif change == 'incoming_fk':
            db.execute('CREATE TABLE unapproved_child(item_id INTEGER REFERENCES submitted_sources(item_id))')
        elif change == 'parent_view':
            db.execute('CREATE VIEW unapproved_parent AS SELECT item_id FROM distill_items')
        elif change == 'reserved_trigger':
            db.execute('CREATE TABLE submitted_sources_local_insert(value)')
        elif change == 'reserved_index':
            db.execute('CREATE TABLE ingestion_events_local_owner(value)')
        elif change == 'temporary_input':
            db.execute('CREATE TABLE submitted_sources_v26(value)')
        elif change == 'temporary_parent':
            db.execute('CREATE TABLE distill_items_v25(value)')
        elif change == 'intake_frozen':
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='ingestion_events_observation_typed'").fetchone()[0]
            db.execute('DROP TRIGGER ingestion_events_observation_typed')
            db.execute('''INSERT INTO ingestion_events(event_key,contract,subject_kind,subject_id,
                item_id,kind,binding_sha256,detail_json,created_at) VALUES(?,'raw-verified-v1',
                'item',51,51,'raw_pending',?,'{"code":"intake_frozen"}',?)''',
                ('f' * 64, 'e' * 64, '2099-01-01T00:00:00Z'))
            db.execute(sql)  # Only hostile row remains; all pre-DDL bytes are restored.
        elif change == 'wrong_input_type':
            db.execute('UPDATE submitted_sources SET content=0 WHERE item_id=51')
        else:
            # A synthetic catalog corruption after ordinary valid seeding.
            # Use a real rebuild; never writable_schema or a version-only disguise.
            table = 'submitted_sources' if change == 'wrong_input_default' else 'wiki_tasks'
            _rebuild_table(db, table, lambda sql: sql.replace(
                'DEFAULT 1', 'DEFAULT 0', 1) if table == 'submitted_sources' else sql.replace(
                "DEFAULT '{}'", "DEFAULT '{\"wrong\":true}'", 1))
        db.commit()
    _marker(path, vault, 25)
    before, vault_before = path.read_bytes(), probe._digest_tree(vault)
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('precheck must not migrate'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'precheck_failed'}
    assert path.read_bytes() == before and probe._digest_tree(vault) == vault_before


def _rebuild_table(db, table, transform, *, generated_scope=False):
    """Deliberate negative-test tampering, after valid fixture/actual migration.

    All original dependent SQL and data are restored; only the requested table
    definition changes. Foreign-key disabling is confined to this synthetic
    corruption connection, never the lawful seed/writer or the probe.
    """
    assert not db.in_transaction
    db.execute('PRAGMA foreign_keys=OFF')
    sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
    old = tuple(row[1] for row in db.execute(f'PRAGMA table_xinfo("{table}")'))
    columns = tuple(name for name in old if not (generated_scope and name == 'binding_scope'))
    names = ','.join('"' + name + '"' for name in columns)
    # Schema27 also has guards on other tables which read submitted_sources.
    # Preserve their exact SQL while the negative fixture briefly removes it;
    # otherwise SQLite rejects ALTER before the intended corruption is committed.
    dependents = list(db.execute("""SELECT type,name,sql FROM sqlite_master
        WHERE sql IS NOT NULL AND (type='trigger' OR (type='index' AND tbl_name=?))
        ORDER BY type,name""", (table,)))
    db.execute('BEGIN IMMEDIATE')
    temporary = table + '_negative'
    changed = transform(sql)
    assert changed != sql
    changed = changed.replace(f'CREATE TABLE "{table}"', f'CREATE TABLE {temporary}', 1)
    changed = changed.replace(f'CREATE TABLE {table} (', f'CREATE TABLE {temporary} (', 1)
    assert changed.startswith('CREATE TABLE ' + temporary + ' (')
    db.execute(changed)
    db.execute(f'INSERT INTO {temporary}({names}) SELECT {names} FROM "{table}"')
    for kind, name, _ in dependents:
        db.execute('DROP ' + kind.upper() + ' "' + name + '"')
    db.execute('DROP TABLE "' + table + '"')
    db.execute(f'ALTER TABLE {temporary} RENAME TO "{table}"')
    for _, _, statement in dependents:
        db.execute(statement)
    for kind, name, statement in dependents:
        assert db.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[:] == (kind, statement)
    db.commit()
    db.execute('PRAGMA foreign_keys=ON')


@pytest.mark.parametrize('version', [21, 25])
@pytest.mark.parametrize('change', [
    'scope_default', 'scope_type', 'scope_hidden', 'key_order', 'input_fk', 'scope_value',
    'guard_missing', 'guard_changed', 'observation_missing', 'observation_changed',
    'partial_missing', 'partial_predicate', 'partial_nonunique', 'partial_collation',
    'blob_empty', 'null_empty', 'old_event_changed', 'old_receipt_changed',
])
def test_committed27_finite_contract_damage_keeps27_and_reports_failure(tmp_path, monkeypatch, version, change):
    if version not in (25, 26) and change in ('null_empty', 'old_event_changed', 'old_receipt_changed'):
        pytest.skip('Requires independently frozen nonempty25 input')
    path, vault, report = _prepare(tmp_path, version)
    actual = probe.initialize
    seen = {}
    def corrupt(database):
        actual(database)
        with database_module.connect(database) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 27
            if change in ('scope_default', 'scope_type', 'scope_hidden', 'key_order', 'input_fk'):
                old, new = {
                    'scope_default': ("DEFAULT 'legacy'", "DEFAULT 'wrong'"),
                    'scope_type': ('binding_scope TEXT', 'binding_scope INTEGER'),
                    'scope_hidden': ("binding_scope TEXT NOT NULL DEFAULT 'legacy' CHECK(typeof(binding_scope)='text')",
                                     "binding_scope TEXT GENERATED ALWAYS AS ('legacy') VIRTUAL"),
                    'key_order': ('UNIQUE(input_kind, input_key, binding_scope)', 'UNIQUE(binding_scope, input_kind, input_key)'),
                    'input_fk': ('REFERENCES distill_items(item_id)', 'REFERENCES materials(material_id)'),
                }[change]
                _rebuild_table(db, 'submitted_sources', lambda sql: sql.replace(old, new, 1),
                               generated_scope=change == 'scope_hidden')
            elif change == 'scope_value':
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name='submitted_sources_local_tuple'").fetchone()[0]
                db.execute('DROP TRIGGER submitted_sources_local_tuple')
                db.execute("UPDATE submitted_sources SET binding_scope='wrong' WHERE item_id=51")
                db.execute(sql)
            elif change in ('guard_missing', 'guard_changed', 'observation_missing', 'observation_changed'):
                name = 'submitted_sources_local_insert' if change.startswith('guard') else 'ingestion_events_observation_typed'
                sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
                db.execute('DROP TRIGGER ' + name)
                if change.endswith('changed'):
                    db.execute(sql.replace('local_intake_insert(', 'unapproved_insert(')
                        if change.startswith('guard') else sql.replace('local_intake_event(', 'unapproved_event('))
            elif change.startswith('partial_'):
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name='ingestion_events_local_owner'").fetchone()[0]
                db.execute('DROP INDEX ingestion_events_local_owner')
                if change != 'partial_missing':
                    old, new = {
                        'partial_predicate': ("kind='raw_pending' AND ", ''),
                        'partial_nonunique': ('CREATE UNIQUE INDEX', 'CREATE INDEX'),
                        'partial_collation': ('(item_id)', '(item_id COLLATE NOCASE DESC)'),
                    }[change]
                    db.execute(sql.replace(old, new, 1))
            elif change in ('blob_empty', 'null_empty'):
                predicate = 'item_id=51' if change == 'blob_empty' else 'content IS NULL'
                # Retained nonlegacy NULL belongs to the lawful terminal writer;
                # restore its exact guard after negative-test-only tampering.
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name='submitted_sources_ingestion_no_release'").fetchone()[0]
                db.execute('DROP TRIGGER submitted_sources_ingestion_no_release')
                db.execute("UPDATE submitted_sources SET content=X'' WHERE " + predicate)
                db.execute(sql)
            else:
                table, guard, field = ('ingestion_events', 'ingestion_events_no_update', 'created_at') if change == 'old_event_changed' else (
                    'wiki_outcome_receipts', 'wiki_outcome_receipts_no_update', 'payload_json')
                sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (guard,)).fetchone()[0]
                db.execute('DROP TRIGGER ' + guard)
                value = '2099-01-02T00:00:00Z' if change == 'old_event_changed' else '{"changed":true}'
                db.execute('UPDATE ' + table + ' SET ' + field + '=?', (value,))
                db.execute(sql)
            db.commit()
        seen['bytes'] = Path(database).read_bytes()
    monkeypatch.setattr(probe, 'initialize', corrupt)
    vault_before = probe._digest_tree(vault)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'legacy_changed'}
    assert path.read_bytes() == seen['bytes']  # Failure is postcommit; no recovery claim.
    assert probe._digest_tree(vault) == vault_before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27


@pytest.mark.parametrize('version', [21, 22, 23, 25])
@pytest.mark.parametrize('stage', ['copied_input', 'final_preservation'])
def test_actual26_transaction_failure_restores_complete_prior(tmp_path, monkeypatch, version, stage):
    path, vault, report = _prepare(tmp_path, version)
    selected, rows = _read_rows(path)
    before, vault_before = path.read_bytes(), probe._digest_tree(vault)
    with closing(sqlite3.connect(path)) as db:
        catalog = probe._objects(db)
    actual = database_module.migrate_v26
    class InterruptedConnection:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, name):
            return getattr(self.db, name)
        def execute(self, sql, parameters=()):
            result = self.db.execute(sql, parameters)
            if sql.startswith('INSERT INTO submitted_sources_v26('):
                raise RuntimeError(SENTINEL)
            return result
    def interrupted(db):
        actual(InterruptedConnection(db) if stage == 'copied_input' else db)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='submitted_sources_local_insert'").fetchone()
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(database_module, 'migrate_v26', interrupted)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'migration_failed'}
    assert path.read_bytes() == before and _read_rows(path, selected)[1] == rows
    assert probe._digest_tree(vault) == vault_before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert probe._objects(db) == catalog
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('table', ['submitted_sources', 'ingestion_events'])
def test_complete_index_xinfo_is_checked_independently(tmp_path, monkeypatch, table):
    path, _, report = _prepare(tmp_path, 25)
    actual = probe._sqlite_readonly
    name = 'sqlite_autoindex_submitted_sources_1' if table == 'submitted_sources' else 'ingestion_events_local_owner'
    seen = []
    def reader(database):
        def alter(sql, rows):
            if sql == f'PRAGMA index_xinfo("{name}")':
                rows = list(rows)
                tail = tuple(rows[-1]); assert tail[1] == -1 and tail[-1] == 0
                rows[-1] = (tail[0], -2, *tail[2:])
                seen.append(name)
            return rows
        return _MetadataReader(actual(database), alter)
    monkeypatch.setattr(probe, '_sqlite_readonly', reader)
    assert _run(path, report) == 1 and seen == [name]
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27


def test_prior25_rowid_of_text_primary_key_is_not_lost_in_digest(tmp_path, monkeypatch):
    path, vault, report = _prepare(tmp_path, 25)
    tid = '1' * 32
    with closing(sqlite3.connect(path)) as db:
        db.execute('UPDATE wiki_tasks SET rowid=17 WHERE task_id=?', (tid,))
        db.commit()
    _marker(path, vault, 25)
    actual = probe.initialize
    def changed(database):
        actual(database)
        with closing(sqlite3.connect(database)) as db:
            db.execute('UPDATE wiki_tasks SET rowid=18 WHERE task_id=?', (tid,))
            db.commit()
    monkeypatch.setattr(probe, 'initialize', changed)
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'


@pytest.mark.parametrize('udf,args', [
    ('local_intake_insert', (1, 'local', 'direct_text', 'key', 'label', '{}', b'body')),
    ('local_intake_event', ('a' * 64, 'raw-verified-v1', 'item', 1, 1, 'b' * 64, '{}')),
    ('ingestion_raw_terminal', (1, 0, 'raw_saved', 'done')),
    ('wiki_outcome_accept', ('receipt', 'task', 1, 'accepted', 'contract', 'boundary', 'plan', '{}', 'stamp')),
    ('source_identity_append', ('a' * 64, '["synthetic"]')),
])
def test_ordinary27_connection_keeps_local_and_terminal_capabilities_defaultdeny(tmp_path, udf, args):
    path, _, report = _prepare(tmp_path, 25)
    assert _run(path, report) == 0
    with database_module.connect(path) as db:
        placeholders = ','.join('?' for _ in args)
        assert db.execute(f'SELECT {udf}({placeholders})', args).fetchone()[0] == 0


def _upgrade_to26(path):
    """Fixed historical25 -> original unchanged26 migration, never init27/downcast.

    migrate_v26 and its input/guard literals are AST-exact to the accepted
    5cafd0058a821b01ed4a4b9eff84ca7ac2da9aed primary (also e76/1ede).
    The version assignment records that completed real migration, not a facade.
    """
    with database_module.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25
        assert not db.in_transaction
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        database_module.migrate_v26(db)
        db.execute('PRAGMA user_version=26')
        db.commit()
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert tuple(db.execute('PRAGMA quick_check').fetchone()) == ('ok',)


def _seed_sparse22(db, vault):
    """Legitimate INSERT rowids, no UPDATE of immutable raw or guard removal."""
    stamp = '2099-01-01T00:00:00Z'
    db.execute('''INSERT INTO wiki_tasks(rowid,task_id,vault_path,vault_key,request_kind,
        trigger_source,backend,model,effort,kit_version,kit_manifest_sha256,
        boundary_sha256,state,raw_count,batch_count,created_at,updated_at)
        VALUES(197,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('1' * 32, str(vault), 'a' * 64, 'all', 'cli', 'codex_cli', 'synthetic-model',
         'medium', '3.0.0', 'c' * 64, 'b' * 64, 'queued', 1, 1, stamp, stamp))
    db.execute('''INSERT INTO wiki_task_batches(rowid,task_id,batch_no,state,item_count)
        VALUES(701,?,1,'queued',1)''', ('1' * 32,))
    db.execute('''INSERT INTO wiki_task_raw(rowid,task_id,ordinal,batch_no,raw_id,identity,
        relative_path,byte_count,content_sha256) VALUES(991,?,1,1,?,?,?, ?,?)''',
        ('1' * 32, 'R-20261008-0001', '第三方', 'raw/合成/1.md',
         len(SENTINEL.encode()), hashlib.sha256(SENTINEL.encode()).hexdigest()))


class _MetadataRows(list):
    def fetchall(self):
        return self


class _MetadataReader:
    """Negative read-boundary injection only; actual DB/validator stay unchanged."""
    def __init__(self, db, alter):
        self.db, self.alter = db, alter

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, parameters=()):
        rows = self.db.execute(sql, parameters)
        metadata = (sql == 'SELECT type,name,tbl_name,sql FROM sqlite_schema'
                    or sql.startswith('PRAGMA index_xinfo('))
        if metadata and self.db.execute('PRAGMA user_version').fetchone()[0] == 27:
            return _MetadataRows(self.alter(sql, list(rows)))
        return rows


def test_schema22_sparse_parent_and_child_rowids_are_preserved(tmp_path):
    path, vault, report = _prepare(tmp_path, 22, sparse_wiki=True)
    selected, before = _read_rows(path)
    assert [before[n][0][0] for n in ('wiki_tasks', 'wiki_task_batches', 'wiki_task_raw')] == [197, 701, 991]
    vault_before = probe._digest_tree(vault)
    # This strict positive guards the repaired historical23 copy: physical rowids
    # remain part of the independent old-row oracle, never normalized or ignored.
    assert _run(path, report) == 0
    assert _read_rows(path, selected)[1] == before
    assert probe._digest_tree(vault) == vault_before
    with closing(probe._sqlite_readonly(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert [tuple(row) for row in db.execute('''SELECT t.rowid,b.rowid,r.rowid,r.task_id,r.batch_no
            FROM wiki_tasks t JOIN wiki_task_batches b USING(task_id)
            JOIN wiki_task_raw r USING(task_id,batch_no)''')] == [(197, 701, 991, '1' * 32, 1)]


def test_schema26_real_local_scope_and_nonempty_receipts_are_preserved(tmp_path):
    from knowledge_distiller.v1.file_sources import prepare_direct_text
    from knowledge_distiller.v1.intake_binding import build_local_binding
    from knowledge_distiller.v1.store import Store

    path, vault, report = _prepare(tmp_path, 26)
    source = prepare_direct_text('仅合成本地提交，条件保持。\r\n')
    binding = build_local_binding(source)
    store = Store(path, runtime_root=path.parent / 'synthetic-runtime')
    item = store.submit_local_bound_source(source, envelope_json=binding.envelope_json)
    assert store.local_intake_binding(item)[1] == binding
    with closing(probe._sqlite_readonly(path)) as db:
        scope = db.execute('SELECT binding_scope FROM submitted_sources WHERE item_id=?', (item,)).fetchone()[0]
        assert scope != 'legacy'
        assert db.execute("SELECT count(*) FROM ingestion_events WHERE json_extract(detail_json,'$.code')='intake_frozen'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM wiki_outcome_receipts').fetchone()[0] == 1
    _marker(path, vault, 26)
    selected, before = _read_rows(path)
    vault_before = probe._digest_tree(vault)
    assert _run(path, report) == 0
    assert _read_rows(path, selected)[1] == before
    assert probe._digest_tree(vault) == vault_before
    assert store.local_intake_binding(item)[1] == binding
    with closing(probe._sqlite_readonly(path)) as db:
        assert db.execute('SELECT binding_scope FROM submitted_sources WHERE item_id=?', (item,)).fetchone()[0] == scope


@pytest.mark.parametrize('version', [25, 26])
def test_historical_typed_success_plan_json_and_receipts_are_preserved(tmp_path, version):
    path, vault, report = _prepare(tmp_path, version)
    # These are lawful historical completion transitions before27 existed;
    # validated is retained storage, never fabricated accepted publication.
    with database_module.connect(path) as db:
        for state in ('validating', 'publishing', 'succeeded'):
            db.execute('UPDATE wiki_task_batches SET state=? WHERE task_id=?', (state, '2' * 32))
            db.execute('UPDATE wiki_tasks SET state=?,completed_batch_count=? WHERE task_id=?',
                       (state, int(state == 'succeeded'), '2' * 32))
        assert db.execute("SELECT phase FROM wiki_outcome_receipts").fetchone()[0] == 'validated'
    _marker(path, vault, version)
    selected, before = _read_rows(path)
    assert _run(path, report) == 0
    assert _read_rows(path, selected)[1] == before
    with closing(probe._sqlite_readonly(path)) as db:
        assert tuple(db.execute('SELECT state,completed_batch_count FROM wiki_tasks WHERE task_id=?',
                                ('2' * 32,)).fetchone()) == ('succeeded', 1)


NEW27_NAMES = (
    'wiki_task_contract_plan_unique', 'wiki_task_legacy_boundary_unique',
    'wiki_batch_typed_success_requires_accepted', 'wiki_task_typed_success_requires_all_accepted',
    'wiki_batch_typed_insert_requires_queued', 'source_identity_events',
    'source_identity_one_root', 'source_identity_one_successor', 'source_identity_item_order',
    'source_identity_events_no_update', 'source_identity_events_no_delete',
    'source_identity_events_append_verified',
)


@pytest.mark.parametrize('version', [21, 22, 23, 25, 26])
@pytest.mark.parametrize('name', NEW27_NAMES)
def test_schema27_reserved_names_rejected_before_initializer(tmp_path, monkeypatch, version, name):
    path, vault, report = _prepare(tmp_path, version)
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE "' + name + '"(value TEXT)')
        db.commit()
    _marker(path, vault, version)
    before, vault_before = path.read_bytes(), probe._digest_tree(vault)
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not initialize unknown input'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'precheck_failed'
    assert path.read_bytes() == before and probe._digest_tree(vault) == vault_before


@pytest.mark.parametrize('version', [21, 22, 23, 25, 26])
@pytest.mark.parametrize('kind', ['incoming_fk', 'view', 'crossguard', 'table'])
def test_whole_catalog_unknown_dependency_is_rejected_before_rows(tmp_path, monkeypatch, version, kind):
    path, vault, report = _prepare(tmp_path, version)
    statements = {
        'incoming_fk': 'CREATE TABLE hostile(item_id INTEGER REFERENCES distill_items(item_id))',
        'view': 'CREATE VIEW hostile AS SELECT value FROM settings',
        'crossguard': "CREATE TRIGGER hostile BEFORE UPDATE ON settings BEGIN SELECT item_id FROM distill_items; END",
        'table': 'CREATE TABLE hostile(body BLOB)',
    }
    with closing(sqlite3.connect(path)) as db:
        db.execute(statements[kind]); db.commit()
    _marker(path, vault, version)
    before, vault_before = path.read_bytes(), probe._digest_tree(vault)
    actual = probe._sqlite_readonly
    reads = []
    def reader(database):
        db = actual(database)
        db.set_authorizer(lambda action, table, col, *_: (
            reads.append((table, col)) or sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table == 'settings' else sqlite3.SQLITE_OK))
        return db
    monkeypatch.setattr(probe, '_sqlite_readonly', reader)
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('must not initialize unknown input'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text())['error_code'] == 'precheck_failed'
    assert reads == []
    assert path.read_bytes() == before and probe._digest_tree(vault) == vault_before


@pytest.mark.parametrize('version', [21, 22, 23, 25, 26])
@pytest.mark.parametrize('stage', ['copied_parent', 'final_preservation'])
def test_actual27_transaction_failure_restores_complete_prior(tmp_path, monkeypatch, version, stage):
    path, vault, report = _prepare(tmp_path, version)
    selected, rows = _read_rows(path)
    before, vault_before = path.read_bytes(), probe._digest_tree(vault)
    with closing(probe._sqlite_readonly(path)) as db:
        catalog = probe._objects(db)
    actual = database_module.migrate_v27
    class Interrupted:
        def __init__(self, db): self.db = db
        def __getattr__(self, name): return getattr(self.db, name)
        def execute(self, sql, parameters=()):
            result = self.db.execute(sql, parameters)
            if sql.startswith('INSERT INTO wiki_tasks_v27('):
                raise RuntimeError(SENTINEL)
            return result
    def interrupted(db):
        actual(Interrupted(db) if stage == 'copied_parent' else db)
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='source_identity_events'").fetchone()
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(database_module, 'migrate_v27', interrupted)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'migration_failed'}
    assert path.read_bytes() == before and _read_rows(path, selected)[1] == rows
    assert probe._digest_tree(vault) == vault_before
    with closing(probe._sqlite_readonly(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert probe._objects(db) == catalog
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('version', [21, 22, 23, 25, 26])
def test_initializer_postcommit_error_keeps27_and_reports_not_success(tmp_path, monkeypatch, version):
    path, vault, report = _prepare(tmp_path, version)
    selected, before = _read_rows(path)
    vault_before = probe._digest_tree(vault)
    original = database_module._schema27_enable_foreign_keys
    def fail(db):
        original(db)
        assert not db.in_transaction and db.execute('PRAGMA user_version').fetchone()[0] == 27
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(database_module, '_schema27_enable_foreign_keys', fail)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'migration_failed'}
    assert _read_rows(path, selected)[1] == before and probe._digest_tree(vault) == vault_before
    with closing(probe._sqlite_readonly(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('name', NEW27_NAMES + (
    'wiki_tasks', 'wiki_task_outcome_binding_required', 'wiki_outcome_receipts_publish_unavailable'))
@pytest.mark.parametrize('change', ['missing', 'changed'])
def test_complete27_catalog_metadata_failure_is_not_success_or_rollback(tmp_path, monkeypatch, name, change):
    path, _, report = _prepare(tmp_path, 26)
    actual_reader, actual_initialize = probe._sqlite_readonly, probe.initialize
    seen = {}
    def initialized(database):
        actual_initialize(database)
        seen['committed'] = Path(database).read_bytes()
    def reader(database):
        def alter(sql, rows):
            if sql != 'SELECT type,name,tbl_name,sql FROM sqlite_schema':
                return rows
            result = []
            for kind, obj, owner, ddl in rows:
                if obj == name:
                    if change == 'missing': continue
                    ddl += '\n--unapproved catalog text'
                result.append((kind, obj, owner, ddl))
            return result
        return _MetadataReader(actual_reader(database), alter)
    monkeypatch.setattr(probe, 'initialize', initialized)
    monkeypatch.setattr(probe, '_sqlite_readonly', reader)
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'legacy_changed'}
    assert path.read_bytes() == seen['committed']
    with closing(actual_reader(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        database_module._schema27_check_catalog(db, 27)


@pytest.mark.parametrize('name', ['wiki_task_contract_plan_unique', 'wiki_task_legacy_boundary_unique',
                                  'sqlite_autoindex_source_identity_events_1'])
@pytest.mark.parametrize('field', ['cid', 'desc', 'coll', 'key', 'aux'])
def test_new27_full_index_xinfo_is_independently_checked(tmp_path, monkeypatch, name, field):
    path, _, report = _prepare(tmp_path, 26)
    actual = probe._sqlite_readonly
    seen = []
    def reader(database):
        def alter(sql, rows):
            if sql != f'PRAGMA index_xinfo("{name}")': return rows
            rows = [tuple(r) for r in rows]
            index = -1 if field == 'aux' else 0
            values = list(rows[index])
            column = {'cid': 1, 'desc': 3, 'coll': 4, 'key': 5, 'aux': 1}[field]
            values[column] = ('NOCASE' if values[column] != 'NOCASE' else 'BINARY') if field == 'coll' else (
                0 if values[column] else 1) if field in ('desc', 'key') else -99
            rows[index] = tuple(values); seen.append((name, field))
            return rows
        return _MetadataReader(actual(database), alter)
    monkeypatch.setattr(probe, '_sqlite_readonly', reader)
    assert _run(path, report) == 1 and seen == [(name, field)]
    assert json.loads(report.read_text())['error_code'] == 'legacy_changed'
    with closing(actual(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27


def test_actual24_is_not_a_probe_source_even_when_database_can_migrate_it(tmp_path, monkeypatch):
    path, vault, report = _prepare(tmp_path, 23)
    with database_module.connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        database_module.migrate_v24(db)
        db.execute('PRAGMA user_version=24')
        db.commit()
    _marker(path, vault, 24)
    before = path.read_bytes()
    monkeypatch.setattr(probe, 'initialize', lambda *_: pytest.fail('24 is not an approved probe source'))
    assert _run(path, report) == 1
    assert json.loads(report.read_text()) == {'schema_version': 1, 'ok': False, 'error_code': 'fixture_invalid'}
    assert path.read_bytes() == before
    with closing(probe._sqlite_readonly(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 24


@pytest.mark.parametrize('version', [21, 22, 23, 25, 26])
def test_whole_catalog_source_qualification_has_no_database_side_effects(tmp_path, version):
    path, vault, _ = _prepare(tmp_path, version)
    before, vault_before = path.read_bytes(), probe._digest_tree(vault)
    with closing(probe._sqlite_readonly(path)) as db:
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        assert not db.in_transaction and db.total_changes == 0
        prior = probe._freeze(db, version)
        assert prior['version'] == version
        assert not db.in_transaction and db.total_changes == 0
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
    assert path.read_bytes() == before and probe._digest_tree(vault) == vault_before
