"""A1/A2 synthetic process storage; actual isolated Vault proof, no product hook."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from knowledge_distiller.v1 import database, store as store_module, wiki_schema
from knowledge_distiller.v1.database import connect, initialize, INGESTION_CONTRACT
from knowledge_distiller.v1.file_sources import prepare_direct_text, parse_submitted_source
from knowledge_distiller.v1.media_lifecycle import preview, release_completed
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.temporary_artifacts import TemporaryArtifacts, MARKER
from tests.v1.test_media_lifecycle import captured, raw_written
from tests.v1.test_wiki_schema import _create_schema22, _insert_frozen_task


BINDING = dict(ingestion_contract=INGESTION_CONTRACT,
               source_binding_sha256='a' * 64, relation_binding_sha256='b' * 64)
STAMP = '2026-10-01T00:00:00+00:00'


def _contents(path):
    with sqlite3.connect(path) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: ([r[1] for r in db.execute(f'PRAGMA table_info({t})')],
                    list(db.execute(f'SELECT * FROM {t} ORDER BY rowid'))) for t in tables}


def _prior(path, version):
    _create_schema22(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO materials VALUES (41,'douyin','synthetic','input','source','{}',?,'version-1')", (STAMP,))
        db.execute("""INSERT INTO distill_items
            (item_id,submitted_url,state,phase,material_id,queued_at,created_at,updated_at)
            VALUES (51,'synthetic','queued','collecting',41,?,?,?)""", (STAMP, STAMP, STAMP))
        db.execute("INSERT INTO source_media VALUES (41,'image-1',0,'image/png',?,?)",
                   (hashlib.sha256(b'original').hexdigest(), b'original'))
        db.execute("INSERT INTO source_facts VALUES (61,41,'immutable original','[]','{}',?)", (STAMP,))
        db.execute("INSERT INTO knowledge_results(source_fact_id,payload_json,created_at) VALUES (61,'{}',?)", (STAMP,))
        content = 'immutable raw'
        db.execute("""INSERT INTO raw_records
            (raw_id,subject_kind,subject_id,identity,relative_path,content,content_sha256,origin,created_at,written_at,written_vault)
            VALUES ('R-20261008-0001','material',41,'第三方','raw/外部/2026/10/R-20261008-0001.md',?,?, 'app',?,?, '/synthetic/vault')""",
                   (content, hashlib.sha256(content.encode()).hexdigest(), STAMP, STAMP))
        # The schema-only fixture has no lifecycle singleton; seed prior state.
        db.execute('''INSERT INTO media_lifecycle
            (singleton,legacy_material_id,released_bytes,compacted_bytes)
            VALUES (1,40,128,64)''')
        db.execute("""INSERT INTO captures(capture_id,app_id,message_id,message_type,created_ms,received_ms,text,raw_id)
            VALUES (81,'synthetic-app','message','text',1,1,'immutable capture','R-20261008-0002')""")
        db.execute("""INSERT INTO collection_operations
            (operation_id,kind,source_key,title,manifest_json,signature,content_signature,authority_json,
             confirmation_token,state,queued_at,created_at,updated_at)
            VALUES (71,'same_topic','synthetic','title','{}','signature','content','{}','token','queued',?,?,?)""", (STAMP, STAMP, STAMP))
        db.execute("INSERT INTO collection_members VALUES (71,1,'synthetic','version-1',51,0,61,1)")
        _insert_frozen_task(db)
        if version == 23:
            wiki_schema.migrate_v23(db)
            db.execute('PRAGMA user_version=23')


@pytest.mark.parametrize('version', [22, 23])
def test_upgrade_preserves_every_original_column_and_immutable_byte(tmp_path, version):
    path = tmp_path / 'synthetic.sqlite3'
    _prior(path, version)
    before = _contents(path)
    with sqlite3.connect(path) as db:
        guards = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name LIKE 'raw_records_%' OR name LIKE 'source_facts_%' OR name LIKE 'captures_%')"))
    initialize(path)
    after = _contents(path)
    for table, (columns, rows) in before.items():
        new_columns, new_rows = after[table]
        assert new_columns[:len(columns)] == columns
        assert [r[:len(columns)] for r in new_rows] == rows
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 24
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT legacy_material_id FROM media_lifecycle').fetchone()[0] == 40
        assert db.execute('SELECT COUNT(*) FROM ingestion_events').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM wiki_outcome_receipts').fetchone()[0] == 0
        assert dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name LIKE 'raw_records_%' OR name LIKE 'source_facts_%' OR name LIKE 'captures_%')")) == guards
        assert tuple(db.execute('SELECT ingestion_contract,source_binding_sha256,relation_binding_sha256 FROM distill_items').fetchone()) == ('legacy', None, None)
        assert tuple(db.execute('SELECT outcome_contract,plan_json,plan_sha256 FROM wiki_tasks').fetchone()) == ('legacy', '{}', None)
        for sql in ("UPDATE source_facts SET snapshot='changed'", "UPDATE raw_records SET content='changed'",
                    "UPDATE captures SET text='changed'", "UPDATE collection_operations SET manifest_json='changed'", 'DELETE FROM source_facts'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(sql)
    frozen = _contents(path)
    initialize(path)
    assert _contents(path) == frozen


def test_fresh_schema_keeps_default_creation_claim_and_requeue_legacy(tmp_path):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    item = store.create_item('synthetic://first')
    submitted = store.submit_source(prepare_direct_text('synthetic second'))
    assert store.item_bundle(item)['ingestion_contract'] == 'legacy'
    assert store.item_bundle(submitted)['ingestion_contract'] == 'legacy'
    assert store.claim_next_work() == ('item', item)
    assert store.requeue_interrupted() == 1
    assert store.claim_next_item() == item
    store.mark_failed(item, 'collecting', 'temporary')
    store.retry_item(item)
    assert store.item_bundle(item)['state'] == 'queued'
    assert store.ingestion_events(item) == ()
    assert store.claim_next_item() == submitted


@pytest.mark.parametrize('version', [22, 23])
def test_v24_failure_rolls_back_even_after_all_new_objects(tmp_path, monkeypatch, version):
    path = tmp_path / 'synthetic.sqlite3'
    _prior(path, version)
    before = _contents(path)
    with sqlite3.connect(path) as db:
        schema = list(db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
    real = database.migrate_v24
    def interrupted(db):
        real(db)
        raise RuntimeError('synthetic_migration_interruption')
    monkeypatch.setattr(database, 'migrate_v24', interrupted)
    with pytest.raises(RuntimeError, match='synthetic_migration_interruption'):
        initialize(path)
    assert _contents(path) == before
    with sqlite3.connect(path) as db:
        assert list(db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')) == schema
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
    monkeypatch.setattr(database, 'migrate_v24', real)
    initialize(path)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 24
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_v23_fk_failure_does_not_commit_v24(tmp_path):
    path = tmp_path / 'synthetic.sqlite3'
    _prior(path, 23)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE distill_items SET material_id=999 WHERE item_id=51')
    before = _contents(path)
    with pytest.raises(RuntimeError, match='broken ingestion references'):
        initialize(path)
    assert _contents(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 23


@pytest.mark.parametrize('action', ['source_fact', 'ttl', 'reject'])
def test_exact_new_submission_survives_fact_expiry_and_rejection(tmp_path, action):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    source = prepare_direct_text('short but complete synthetic definition')
    item = store.submit_source(source, **BINDING)
    if action == 'source_fact':
        store.establish_submitted_fact(item, source, parse_submitted_source(source))
    elif action == 'reject':
        store.reject_submitted_source(item, 'direct_text_input_unsupported')
    else:
        store.mark_failed(item, 'collecting', 'temporary')
        store.dismiss_item(item)
        with connect(store.path) as db:
            db.execute('UPDATE submitted_sources SET retain_until=? WHERE item_id=?', (STAMP, item))
        store.expire_submitted_sources()
    assert store.submitted_source(item).content == source.content
    with connect(store.path) as db:
        assert db.execute('SELECT content,input_metadata FROM submitted_sources WHERE item_id=?', (item,)).fetchone()['content'] == source.content
        for sql in ('UPDATE submitted_sources SET content=NULL', "UPDATE submitted_sources SET input_metadata='{}'", 'DELETE FROM submitted_sources'):
            # The original metadata is nonempty; even a reset is data loss.
            with pytest.raises(sqlite3.IntegrityError, match='retained'):
                db.execute(sql)


@pytest.mark.parametrize('state', ['queued', 'failed', 'dismissed', 'succeeded'])
def test_new_shared_owner_blocks_written_raw_release_and_working_file_cleanup(tmp_path, state):
    store, legacy, material, fact, content = captured(tmp_path)
    store.mark_succeeded(legacy)
    raw_written(store, material, tmp_path)
    new = store.create_item('synthetic://new', **BINDING)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (material, new))
        db.execute("INSERT INTO knowledge_results(source_fact_id,payload_json,created_at) VALUES (?,'{}',?)", (fact, STAMP))
    if state in {'failed', 'dismissed'}:
        store.mark_failed(new, 'collecting', 'temporary')
        if state == 'dismissed':
            store.dismiss_item(new)
    elif state == 'succeeded':
        store.mark_succeeded(new)
    store.append_ingestion_event(new, kind='source_ready', code='source_fact_ready')
    store.append_ingestion_event(new, kind='raw_pending', code='readback_pending')
    assert store.item_bundle(new)['knowledge_result_id'] is not None
    assert release_completed(store.path) == 0
    assert preview(store.path)[0]['disposition'] == 'protected'
    assert store.media_members(material)[0]['content'] == content
    with connect(store.path) as db:
        for sql in ("UPDATE source_media SET content=X''", 'DELETE FROM source_media'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
    cleaner = TemporaryArtifacts(store, tmp_path / 'runtime')
    cleaner.prepare(new)
    target = tmp_path / 'runtime/items' / str(new)
    (target / 'original.wav').write_bytes(b'original recovery audio')
    marker = json.loads((target / MARKER).read_text())
    marker['retained_at'] = STAMP
    (target / MARKER).write_text(json.dumps(marker))
    cleaner.prepare(new)  # Old prepare TTL must not erase the directory.
    cleaner.clean_item(new, now=datetime.now(UTC) + timedelta(days=30))
    cleaner.clean_item(legacy, now=datetime.now(UTC) + timedelta(days=30))
    cleaner.sweep()
    assert (target / 'original.wav').read_bytes() == b'original recovery audio'
    assert store.media_members(material)[0]['content'] == content


def test_platform_ttl_protects_new_owner_without_source_fact(tmp_path):
    store, legacy, material, fact, content = captured(tmp_path)
    # Use another unfrozen material: deleting a SourceFact is rightly forbidden.
    with connect(store.path) as db:
        db.execute("INSERT INTO materials(source_kind,source_key,submitted_url,canonical_url,metadata_json,created_at,snapshot_key) VALUES ('douyin','pending','input','source','{}',?,'pending')", (STAMP,))
        pending = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.execute("INSERT INTO source_media VALUES (?,'image-1',0,'image/png',?,?)", (pending, hashlib.sha256(b'pending').hexdigest(), b'pending'))
    item = store.create_item('synthetic://pending', **BINDING)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (pending, item))
    store.mark_failed(item, 'collecting', 'douyin_input_unsupported')
    store.dismiss_item(item)
    store.expire_platform_media()
    assert store.media_members(pending)[0]['content'] == b'pending'


def test_contract_and_binding_cannot_be_downgraded_or_owner_detached(tmp_path):
    store, legacy, material, fact, content = captured(tmp_path)
    new = store.create_item('synthetic://new', **BINDING)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (material, new))
        for sql in ("UPDATE distill_items SET ingestion_contract='legacy' WHERE item_id=?",
                    "UPDATE distill_items SET source_binding_sha256=? WHERE item_id=?",
                    "UPDATE distill_items SET relation_binding_sha256=? WHERE item_id=?",
                    'UPDATE distill_items SET material_id=NULL WHERE item_id=?',
                    'DELETE FROM distill_items WHERE item_id=?'):
            args = ('c' * 64, new) if 'sha256=?' in sql else (new,)
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE distill_items SET ingestion_contract='raw-verified-v1',source_binding_sha256=?,relation_binding_sha256=? WHERE item_id=?", ('c' * 64, 'd' * 64, legacy))
    source = prepare_direct_text('another owned original')
    submitted = store.submit_source(source, **BINDING)
    with connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match='owner is immutable'):
            db.execute('UPDATE submitted_sources SET item_id=? WHERE item_id=?', (legacy, submitted))


def test_append_observations_is_idempotent_typed_and_never_a_proof(tmp_path):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    item = store.create_item('synthetic://new', **BINDING)
    first = store.append_ingestion_event(item, kind='raw_pending', code='writer_pending')
    assert store.append_ingestion_event(item, kind='raw_pending', code='writer_pending') == first
    detail = json.loads(store.ingestion_events(item)[0]['detail_json'])
    assert detail == {'code': 'writer_pending', 'manifest': {'source_fact_id': None, 'snapshot_sha256': None},
                      'source_binding_sha256': 'a' * 64, 'relation_binding_sha256': 'b' * 64}
    for kind, code in [('raw_verified', 'readback_pending'), ('release_authorized', 'writer_pending'),
                       ('raw_pending', 'external source or arbitrary exception text')]:
        with pytest.raises(ValueError, match='ingestion_event_invalid'):
            store.append_ingestion_event(item, kind=kind, code=code)
    with pytest.raises(ValueError, match='source_fact_required'):
        store.append_ingestion_event(item, kind='source_ready', code='source_fact_ready')
    with connect(store.path) as db:
        for action in ("UPDATE ingestion_events SET detail_json='{}'", 'DELETE FROM ingestion_events'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(action)
        for kind in ('raw_verified', 'release_authorized', 'media_released'):
            with pytest.raises(sqlite3.IntegrityError, match='filesystem proof unavailable'):
                db.execute("""INSERT INTO ingestion_events(event_key,contract,subject_kind,subject_id,item_id,kind,binding_sha256,detail_json,created_at)
                    VALUES (?,'raw-verified-v1','item',?,?,?,?,'{}',?)""", ('c' * 64, item, item, kind, 'd' * 64, STAMP))
        with pytest.raises(sqlite3.IntegrityError, match='observation invalid'):
            db.execute("""INSERT INTO ingestion_events(event_key,contract,subject_kind,subject_id,item_id,kind,binding_sha256,detail_json,created_at)
                VALUES (?,'raw-verified-v1','item',?,?,'raw_pending',?,?,?)""", ('e' * 64, item, item, 'f' * 64, json.dumps(detail | {'body': 'external source'}), STAMP))


def test_append_crash_boundary_rolls_back_and_retry_preserves_single_event(tmp_path, monkeypatch):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    item = store.create_item('synthetic://new', **BINDING)
    real = store_module.connect
    @contextmanager
    def interrupted(path):
        with real(path) as db:
            yield db
            raise RuntimeError('synthetic_before_commit')
    monkeypatch.setattr(store_module, 'connect', interrupted)
    with pytest.raises(RuntimeError, match='synthetic_before_commit'):
        store.append_ingestion_event(item, kind='raw_pending', code='writer_pending')
    monkeypatch.setattr(store_module, 'connect', real)
    assert store.ingestion_events(item) == ()
    one = store.append_ingestion_event(item, kind='raw_pending', code='writer_pending')
    assert store.append_ingestion_event(item, kind='raw_pending', code='writer_pending') == one


def test_two_processes_append_one_observation(tmp_path):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    item = store.create_item('synthetic://new', **BINDING)
    script = "from pathlib import Path; import sys; from knowledge_distiller.v1.store import Store; print(Store(Path(sys.argv[1])).append_ingestion_event(int(sys.argv[2]),kind='raw_pending',code='writer_pending'))"
    env = {'PATH': os.defpath, 'TZ': 'Asia/Taipei',
           'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
    children = [subprocess.Popen([sys.executable, '-c', script, str(store.path), str(item)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    try:
        results = [p.communicate(timeout=30) for p in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
    assert [p.returncode for p in children] == [0, 0], results
    assert results[0][0] == results[1][0]
    assert len(store.ingestion_events(item)) == 1


def test_reserved_wiki_receipt_cannot_claim_publish_or_change_task_binding(tmp_path):
    path = tmp_path / 'synthetic.sqlite3'
    initialize(path)
    with connect(path) as db:
        task = _insert_frozen_task(db)
        with pytest.raises(sqlite3.IntegrityError, match='outcome binding is immutable'):
            db.execute("UPDATE wiki_tasks SET outcome_contract='r08-wiki-outcomes-v1',plan_sha256=? WHERE task_id=?", ('a' * 64, task))
        with pytest.raises(sqlite3.IntegrityError, match='publish proof unavailable'):
            db.execute("""INSERT INTO wiki_outcome_receipts VALUES (?, ?, 1, 'accepted', 'r08-wiki-outcomes-v1', ?, ?, '{}', ?)""",
                       ('a' * 64, task, 'b' * 64, 'c' * 64, STAMP))


def test_explicit_contract_requires_hashes_and_dedup_cannot_upgrade_legacy(tmp_path):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    for values in ({'ingestion_contract': INGESTION_CONTRACT},
                   BINDING | {'source_binding_sha256': 'not-a-hash'},
                   BINDING | {'ingestion_contract': 'legacy'}):
        with pytest.raises(ValueError, match='ingestion_binding_invalid'):
            store.create_item('synthetic://new', **values)
    source = prepare_direct_text('exact original synthetic text')
    legacy = store.submit_source(source)
    with pytest.raises(ValueError, match='ingestion_binding_conflict'):
        store.submit_source(source, **BINDING)
    assert store.item_bundle(legacy)['ingestion_contract'] == 'legacy'
    assert store.submitted_source(legacy).content == source.content


def test_new_collection_binding_is_immutable_and_requires_new_member_contract(tmp_path):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    legacy = store.create_item('synthetic://legacy')
    new = store.create_item('synthetic://new', **BINDING)
    with connect(store.path) as db:
        db.execute("""INSERT INTO collection_operations
            (operation_id,kind,source_key,title,manifest_json,signature,content_signature,authority_json,
             confirmation_token,state,queued_at,created_at,updated_at,
             ingestion_contract,source_binding_sha256,relation_binding_sha256)
            VALUES (1,'same_topic','synthetic','title','{}','signature','content','{}','token','queued',?,?,?, 'raw-verified-v1',?,?)""",
                   (STAMP, STAMP, STAMP, 'a' * 64, 'b' * 64))
        with pytest.raises(sqlite3.IntegrityError, match='contract mismatch'):
            db.execute("INSERT INTO collection_members VALUES (1,1,'first','v1',?,0,NULL,NULL)", (legacy,))
        db.execute("INSERT INTO collection_members VALUES (1,1,'first','v1',?,0,NULL,NULL)", (new,))
        for sql in ("UPDATE collection_operations SET ingestion_contract='legacy'",
                    "UPDATE collection_operations SET relation_binding_sha256=NULL",
                    "UPDATE collection_operations SET source_binding_sha256=NULL"):
            with pytest.raises(sqlite3.IntegrityError, match='binding is immutable'):
                db.execute(sql)


def test_legacy_submission_still_clears_temporary_input_after_fact(tmp_path):
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    source = prepare_direct_text('legacy synthetic original')
    item = store.submit_source(source)
    store.establish_submitted_fact(item, source, parse_submitted_source(source))
    with connect(store.path) as db:
        assert tuple(db.execute('SELECT content,input_metadata,retain_until FROM submitted_sources WHERE item_id=?', (item,)).fetchone()) == (None, '{}', None)


def test_reserved_validated_receipts_are_append_only_and_fk_bound(tmp_path):
    path = tmp_path / 'synthetic.sqlite3'
    initialize(path)
    with connect(path) as db:
        task = _insert_frozen_task(db)
        # A stored candidate is deliberately not an accepted outcome certificate.
        values = ('a' * 64, task, 'b' * 64, 'c' * 64, STAMP)
        sql = """INSERT INTO wiki_outcome_receipts VALUES (?, ?, 1, 'validated', 'r08-wiki-outcomes-v1', ?, ?, '{}', ?)"""
        db.execute(sql, values)
        for statement in ("UPDATE wiki_outcome_receipts SET payload_json='{}'", 'DELETE FROM wiki_outcome_receipts'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(statement)
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
            db.execute(sql, ('d' * 64, 'f' * 32, 'b' * 64, 'c' * 64, STAMP))


def _checked_world(tmp_path):
    from knowledge_distiller.v1.ingestion import Ingestion
    store, legacy, mid, fact, content = captured(tmp_path)
    vault = tmp_path / 'vault'
    vault.mkdir()
    store.set_setting('vault_path', str(vault))
    new = store.create_item('synthetic://checked', **BINDING)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (mid, new))
    store.mark_succeeded(legacy)
    store.mark_succeeded(new)
    ingestion = Ingestion(store)
    ingestion.initialize()
    return store, vault, ingestion, new, mid, content


def test_real_readback_and_release_then_restart_readback(tmp_path):
    from knowledge_distiller.v1.ingestion import Ingestion
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    receipt = ingestion.material(mid, vault, item_id=item)
    events = store.ingestion_events(item)
    proof = json.loads([e for e in events if e['kind'] == 'raw_verified'][0]['detail_json'])
    manifest = proof['manifest']
    assert manifest['source_fact_id'] == store.item_bundle(item)['source_fact_id']
    assert manifest['source_binding_sha256'] == BINDING['source_binding_sha256']
    assert manifest['relation_binding_sha256'] == BINDING['relation_binding_sha256']
    assert manifest['attachment_manifest'][0]['byte_count'] == len(content)
    assert manifest['attachment_manifest'][0]['member_id'] == 'image-1'
    assert '永远保留的来源文本' not in json.dumps(proof, ensure_ascii=False)
    assert release_completed(store.path) == 0  # Default sweep still has no authority.
    assert ingestion.release_material(mid, vault) == len(content)
    assert store.media_members(mid) == []
    assert store.media_manifest(mid)[0]['member_id'] == 'image-1'
    assert store.media_manifest(mid)[0]['sha256'] == hashlib.sha256(content).hexdigest()
    assert not store.media_manifest(mid)[0]['content_available']
    with connect(store.path) as db:
        assert tuple(db.execute('SELECT member_id,sha256,content FROM source_media WHERE material_id=?',
                                (mid,)).fetchone()) == ('image-1', hashlib.sha256(content).hexdigest(), b'')
    restarted = Ingestion(store)
    assert restarted.material(mid, vault, item_id=item) == receipt
    assert restarted.release_material(mid, vault) == 0
    assert (vault / receipt.attachments[0][0]).read_bytes() == content
    with connect(store.path) as db:
        assert db.execute('SELECT released_bytes FROM media_lifecycle').fetchone()[0] == len(content)
        assert db.execute('SELECT COUNT(*) FROM knowledge_results').fetchone()[0] == 0


@pytest.mark.parametrize('damage', ['raw', 'attachment', 'missing'])
def test_release_rechecks_actual_files_even_with_verified_receipt(tmp_path, damage):
    from knowledge_distiller.v1.ingestion import IngestionError
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    receipt = ingestion.material(mid, vault, item_id=item)
    path = vault / (receipt.relative_path if damage == 'raw' else receipt.attachments[0][0])
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'corrupted external bytes')
    with pytest.raises(IngestionError):
        ingestion.release_material(mid, vault)
    assert store.media_members(mid)[0]['content'] == content
    assert not any(e['kind'] in {'release_authorized', 'media_released'} for e in store.ingestion_events(item))


def test_new_owner_requires_its_own_current_proof(tmp_path):
    from knowledge_distiller.v1.ingestion import IngestionError
    store, vault, ingestion, first, mid, content = _checked_world(tmp_path)
    ingestion.material(mid, vault, item_id=first)
    second = store.create_item('synthetic://second-owner', **BINDING)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (mid, second))
    store.mark_succeeded(second)
    with pytest.raises(IngestionError, match='release_proof_pending'):
        ingestion.release_material(mid, vault)
    ingestion.material(mid, vault, item_id=first)  # New owner invalidates old aggregate binding.
    with pytest.raises(IngestionError, match='release_proof_pending'):
        ingestion.release_material(mid, vault)
    assert store.media_members(mid)[0]['content'] == content
    ingestion.material(mid, vault, item_id=second)
    assert ingestion.release_material(mid, vault) == len(content)


def test_release_confirmation_and_cancellation_still_retain_bytes(tmp_path):
    from knowledge_distiller.v1.ingestion import IngestionError
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    ingestion.material(mid, vault, item_id=item)
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET confirmation_json='{}' WHERE item_id=?", (item,))
    with pytest.raises(IngestionError, match='release_owner_pending'):
        ingestion.release_material(mid, vault)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET confirmation_json=NULL WHERE item_id=?', (item,))
    store.mark_failed(item, 'collecting', 'synthetic_failure')
    store.dismiss_item(item)
    assert release_completed(store.path) == 0
    with pytest.raises(IngestionError, match='release_owner_pending'):
        ingestion.release_material(mid, vault)
    assert store.media_members(mid)[0]['content'] == content


def test_ordinary_api_and_sql_cannot_clone_actual_verified_event(tmp_path):
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    ingestion.material(mid, vault, item_id=item)
    proof = [e for e in store.ingestion_events(item) if e['kind'] == 'raw_verified'][0]
    with pytest.raises(ValueError, match='ingestion_event_invalid'):
        store.append_ingestion_event(item, kind='raw_verified', code='source_fact_ready')
    with connect(store.path) as db:
        for kind in ('raw_verified', 'release_authorized', 'media_released'):
            with pytest.raises(sqlite3.IntegrityError, match='filesystem proof unavailable'):
                db.execute('''INSERT INTO ingestion_events
                    (event_key,contract,subject_kind,subject_id,item_id,kind,binding_sha256,detail_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)''', ('f' * 64, proof['contract'], proof['subject_kind'], mid,
                        item, kind, proof['binding_sha256'], proof['detail_json'], STAMP))
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            db.execute("UPDATE source_media SET content=X'' WHERE material_id=?", (mid,))
    assert store.media_members(mid)[0]['content'] == content


def test_release_transaction_failure_keeps_events_counter_and_bytes_atomic(tmp_path, monkeypatch):
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    ingestion.material(mid, vault, item_id=item)
    original = ingestion._insert_proven
    def interrupted(db, record, item_id, kind, binding, lock):
        receipt = original(db, record, item_id, kind, binding, lock)
        if kind == 'media_released':
            raise RuntimeError('synthetic_release_before_clear')
        return receipt
    monkeypatch.setattr(ingestion, '_insert_proven', interrupted)
    with pytest.raises(RuntimeError, match='synthetic_release_before_clear'):
        ingestion.release_material(mid, vault)
    assert store.media_members(mid)[0]['content'] == content
    assert not any(e['kind'] in {'release_authorized', 'media_released'} for e in store.ingestion_events(item))
    with connect(store.path) as db:
        assert db.execute('SELECT released_bytes FROM media_lifecycle').fetchone()[0] == 0


def test_existing_candidate24_is_not_silently_unlocked(tmp_path):
    from knowledge_distiller.v1.ingestion import Ingestion, IngestionError
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    # Model the previous unreleased candidate guard on a disposable fixture.
    with connect(store.path) as db:
        db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
        db.execute("""CREATE TRIGGER ingestion_events_proof_unavailable BEFORE INSERT ON ingestion_events
            WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')
            BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END""")
    store.initialize()
    with pytest.raises(IngestionError, match='candidate_schema_rebuild_required'):
        Ingestion(store).initialize()
    with connect(store.path) as db:
        assert 'ingestion_proof' not in db.execute("SELECT sql FROM sqlite_master WHERE name='ingestion_events_proof_unavailable'").fetchone()[0]


def _checked_audio(tmp_path, *, initial_audio_path=None):
    from knowledge_distiller.v1.ingestion import Ingestion
    from tests.v1.test_ingestion import _capture
    from tests.v1.test_raw import material
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    vault = tmp_path / 'vault'
    vault.mkdir()
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    cid = _capture(store, ingestion, voice=True)
    mid = material(store, 'feishu_voice', '合成完整ASR自述')
    ingestion.captures.audio_root.mkdir(parents=True)
    audio = ingestion.captures.audio_root / f'{cid}.opus'
    audio.write_bytes(b'synthetic audio placeholder, no decoder or model')
    initial_audio = audio if initial_audio_path is None else initial_audio_path
    if initial_audio != audio:
        initial_audio.write_bytes(audio.read_bytes())
    # The unbound capture's initial audio slot is mutable. Once linked to the
    # new owner it is protected by the original production immutable guard.
    with connect(store.path) as db:
        db.execute('UPDATE capture_state SET audio_path=? WHERE capture_id=?', (str(initial_audio), cid))
    item = store.create_item('feishu-voice://synthetic/current', **BINDING)
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET material_id=?,state='working' WHERE item_id=?", (mid, item))
        db.execute('UPDATE capture_state SET item_id=? WHERE capture_id=?', (item, cid))
    return store, vault, ingestion, cid, item, mid, audio


def test_audio_proof_does_not_need_success_and_default_release_stays_protected(tmp_path):
    from knowledge_distiller.v1.ingestion import IngestionError
    store, vault, ingestion, cid, item, mid, audio = _checked_audio(tmp_path)
    before = audio.read_bytes()
    receipt = ingestion.capture(cid, vault)
    assert receipt.identity == '本人' and receipt.attachments == ()
    assert store.item_bundle(item)['state'] == 'working'
    assert ingestion.ledger.heads('material', mid) == ()
    ingestion.captures.release_audio(ingestion.captures.get(cid))
    with pytest.raises(IngestionError, match='release_owner_pending'):
        ingestion.release_capture(cid, vault)
    with connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            db.execute('UPDATE capture_state SET audio_released_at=? WHERE capture_id=?', (STAMP, cid))
        for sql in ('UPDATE capture_state SET item_id=NULL WHERE capture_id=?',
                    'UPDATE capture_state SET audio_path=NULL WHERE capture_id=?',
                    'DELETE FROM capture_state WHERE capture_id=?'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, (cid,))
    assert audio.read_bytes() == before
    store.mark_succeeded(item)  # Synthetic terminal projection, no product hook.
    assert ingestion.release_capture(cid, vault) == len(before)
    assert not audio.exists() and ingestion.captures.get(cid)['audio_released_at']
    assert ingestion.capture(cid, vault) == receipt
    assert ingestion.release_capture(cid, vault) == 0
    assert (vault / receipt.relative_path).read_bytes() == ingestion.ledger.record(receipt.raw_id)['content'].encode()


def test_audio_unlink_crash_keeps_durable_intent_and_rechecks_on_retry(tmp_path, monkeypatch):
    from knowledge_distiller.v1.ingestion import Ingestion
    store, vault, ingestion, cid, item, mid, audio = _checked_audio(tmp_path)
    receipt = ingestion.capture(cid, vault)
    original = audio.read_bytes()
    store.mark_succeeded(item)
    insert = ingestion._insert_proven
    def after_unlink(db, record, item_id, kind, binding, lock):
        if kind == 'media_released':
            assert not audio.exists()
            raise RuntimeError('synthetic_after_unlink_before_db')
        return insert(db, record, item_id, kind, binding, lock)
    monkeypatch.setattr(ingestion, '_insert_proven', after_unlink)
    with pytest.raises(RuntimeError, match='synthetic_after_unlink_before_db'):
        ingestion.release_capture(cid, vault)
    assert ingestion.captures.get(cid)['audio_released_at'] is None
    assert [e[0] for e in ingestion.events(f'capture:{cid}')] == ['raw_verified', 'release_authorized']
    restarted = Ingestion(store)
    assert restarted.release_capture(cid, vault) == len(original)
    assert restarted.capture(cid, vault) == receipt
    assert [e[0] for e in restarted.events(f'capture:{cid}')] == ['raw_verified', 'release_authorized', 'media_released']


def test_shared_audio_or_missing_new_owner_proof_never_releases(tmp_path):
    from knowledge_distiller.v1.ingestion import IngestionError
    store, vault, ingestion, cid, item, mid, audio = _checked_audio(tmp_path)
    ingestion.capture(cid, vault)
    store.mark_succeeded(item)
    second = store.create_item('synthetic://new-audio-owner', **BINDING)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (mid, second))
    store.mark_succeeded(second)
    with pytest.raises(IngestionError, match='release_owner_pending'):
        ingestion.release_capture(cid, vault)
    assert audio.exists() and ingestion.captures.get(cid)['audio_released_at'] is None


def test_released_attachment_loss_is_not_repaired_from_empty_source(tmp_path):
    from knowledge_distiller.v1.ingestion import Ingestion, IngestionError
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    receipt = ingestion.material(mid, vault, item_id=item)
    ingestion.release_material(mid, vault)
    attachment = vault / receipt.attachments[0][0]
    attachment.unlink()
    with pytest.raises(IngestionError, match='readback_unavailable'):
        Ingestion(store).material(mid, vault, item_id=item)
    assert not attachment.exists() and store.media_members(mid) == []
    assert store.media_manifest(mid)[0]['member_id'] == 'image-1'
    assert store.media_manifest(mid)[0]['sha256'] == hashlib.sha256(content).hexdigest()
    assert not store.media_manifest(mid)[0]['content_available']
    with connect(store.path) as db:
        assert tuple(db.execute('SELECT member_id,sha256,content FROM source_media WHERE material_id=?',
                                (mid,)).fetchone()) == ('image-1', hashlib.sha256(content).hexdigest(), b'')


def test_internal_proof_endpoint_rejects_unheld_lock(tmp_path):
    from knowledge_distiller.v1.ingestion import IngestionError
    from knowledge_distiller.v1.wiki_lock import VaultWriteLock
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    record = ingestion.ledger.ensure_material(mid)
    ingestion.ledger.write(record, vault)
    closed = VaultWriteLock(vault, -1)
    with connect(store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        binding = ingestion._binding(db, record, item)
        with pytest.raises(IngestionError, match='filesystem_proof_unavailable'):
            ingestion._insert_proven(db, record, item, 'raw_verified', binding, closed)
    assert not any(e['kind'] == 'raw_verified' for e in store.ingestion_events(item))


def test_unrepresented_source_member_cannot_be_certified_or_released(tmp_path):
    from knowledge_distiller.v1.ingestion import IngestionError
    from tests.v1.test_raw import material
    store = Store(tmp_path / 'synthetic.sqlite3')
    store.initialize()
    vault = tmp_path / 'vault'
    vault.mkdir()
    store.set_setting('vault_path', str(vault))
    # A non-image member is ignored by the historical body renderer. It must
    # remain retained, rather than a render omission becoming raw completion.
    mid = material(store, 'x', 'source', media=[('unrepresented', b'opaque')])
    item = store.create_item('synthetic://unrepresented', **BINDING)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (mid, item))
    store.mark_succeeded(item)
    from knowledge_distiller.v1.ingestion import Ingestion
    ingestion = Ingestion(store)
    with pytest.raises(IngestionError, match='attachment_manifest_incomplete'):
        ingestion.material(mid, vault, item_id=item)
    assert store.media_members(mid)[0]['content'] == b'opaque'
    assert not any(e['kind'] == 'raw_verified' for e in store.ingestion_events(item))


def test_failure_after_actual_clear_rolls_back_bytes_and_release_events(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import ingestion as module
    store, vault, ingestion, item, mid, content = _checked_world(tmp_path)
    ingestion.material(mid, vault, item_id=item)
    real_connect = module.connect
    class Interrupted:
        def __init__(self, inner):
            self.inner = inner
        def __getattr__(self, key):
            return getattr(self.inner, key)
        def execute(self, sql, args=()):
            if sql.startswith('UPDATE media_lifecycle SET released_bytes='):
                assert self.inner.execute('SELECT length(content) FROM source_media WHERE material_id=?', (mid,)).fetchone()[0] == 0
                raise RuntimeError('synthetic_after_clear_before_commit')
            return self.inner.execute(sql, args)
    @contextmanager
    def interrupted_connect(path, **kwargs):
        with real_connect(path, **kwargs) as db:
            yield Interrupted(db)
    monkeypatch.setattr(module, 'connect', interrupted_connect)
    with pytest.raises(RuntimeError, match='synthetic_after_clear_before_commit'):
        ingestion.release_material(mid, vault)
    assert store.media_members(mid)[0]['content'] == content
    assert not any(e['kind'] in {'release_authorized', 'media_released'} for e in store.ingestion_events(item))
    with connect(store.path) as db:
        assert db.execute('SELECT released_bytes FROM media_lifecycle').fetchone()[0] == 0


def test_audio_release_requires_existing_capture_owned_path(tmp_path):
    from knowledge_distiller.v1.ingestion import IngestionError
    outside = tmp_path / 'different-owner.opus'
    store, vault, ingestion, cid, item, mid, audio = _checked_audio(tmp_path, initial_audio_path=outside)
    # New-owner immutability remains enforced; an externally located initial
    # recording does not grant ownership of that path to the raw verifier.
    with connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match='ingestion capture binding is immutable'):
            db.execute('UPDATE capture_state SET audio_path=? WHERE capture_id=?', (str(audio), cid))
    ingestion.capture(cid, vault)
    store.mark_succeeded(item)
    with pytest.raises(IngestionError, match='capture_audio_owner_pending'):
        ingestion.release_capture(cid, vault)
    assert outside.exists() and audio.exists()
