"""Synthetic real schema24 SQL ->25 and actual locked writer; never App/default data.

The embedded 24 DDL is a frozen SQL fixture, not current initialize output.
Source: previously checked database/media/wiki primary expressions (42-match
stdout SHA256: 1230823e50adf41e95c4ba94e725df62290b675ee0ee74314f986ded79d4462a).
No running migration is used to fabricate the pre-upgrade schema.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import wave

import pytest

from knowledge_distiller.v1 import database, ingestion, raw
from knowledge_distiller.v1.database import connect, INGESTION_CONTRACT
from knowledge_distiller.v1.ingestion import Ingestion, IngestionError
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.captures import record_capture
from tests.v1.test_raw import material, PNG

FIXTURES = Path(__file__).with_name('fixtures')
STAMP = '2026-10-08T00:00:00+00:00'
V24_SQL = r"""
BEGIN IMMEDIATE;
ALTER TABLE distill_items ADD COLUMN ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1'));
ALTER TABLE distill_items ADD COLUMN source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE distill_items ADD COLUMN relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE collection_operations ADD COLUMN ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1'));
ALTER TABLE collection_operations ADD COLUMN source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE collection_operations ADD COLUMN relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE wiki_tasks ADD COLUMN outcome_contract TEXT NOT NULL DEFAULT 'legacy' CHECK(outcome_contract IN ('legacy','r08-wiki-outcomes-v1'));
ALTER TABLE wiki_tasks ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(plan_json) AND json_type(plan_json)='object');
ALTER TABLE wiki_tasks ADD COLUMN plan_sha256 TEXT CHECK(plan_sha256 IS NULL OR (length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'));
CREATE TRIGGER distill_items_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON distill_items
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END;
CREATE TRIGGER distill_items_ingestion_binding_required
            BEFORE INSERT ON distill_items WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END;
CREATE TRIGGER collection_operations_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON collection_operations
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END;
CREATE TRIGGER collection_operations_ingestion_binding_required
            BEFORE INSERT ON collection_operations WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END;
CREATE TRIGGER distill_items_ingestion_owner_immutable
        BEFORE UPDATE OF material_id ON distill_items
        WHEN OLD.ingestion_contract!='legacy' AND OLD.material_id IS NOT NULL
          AND NEW.material_id IS NOT OLD.material_id
        BEGIN SELECT RAISE(ABORT,'ingestion owner is immutable'); END;
CREATE TRIGGER distill_items_ingestion_no_delete
        BEFORE DELETE ON distill_items WHEN OLD.ingestion_contract!='legacy'
        BEGIN SELECT RAISE(ABORT,'ingestion owner is durable'); END;
CREATE TRIGGER collection_members_ingestion_contract_match
        BEFORE INSERT ON collection_members
        WHEN (SELECT ingestion_contract FROM collection_operations WHERE operation_id=NEW.operation_id)
          IS NOT (SELECT ingestion_contract FROM distill_items WHERE item_id=NEW.item_id)
        BEGIN SELECT RAISE(ABORT,'collection ingestion contract mismatch'); END;
CREATE TABLE ingestion_events (
        event_id INTEGER PRIMARY KEY,
        event_key TEXT NOT NULL UNIQUE CHECK(length(event_key)=64 AND event_key NOT GLOB '*[^0-9a-f]*'),
        contract TEXT NOT NULL CHECK(contract='raw-verified-v1'),
        subject_kind TEXT NOT NULL CHECK(subject_kind IN ('item','material','capture')),
        subject_id INTEGER NOT NULL CHECK(subject_id>0),
        item_id INTEGER REFERENCES distill_items(item_id),
        kind TEXT NOT NULL CHECK(kind IN ('source_ready','raw_pending','raw_verified','release_authorized','media_released')),
        binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
        detail_json TEXT NOT NULL CHECK(json_valid(detail_json) AND json_type(detail_json)='object'),
        created_at TEXT NOT NULL CHECK(trim(created_at)!='')
    );
CREATE INDEX ingestion_events_subject ON ingestion_events(subject_kind,subject_id,event_id);
CREATE TRIGGER ingestion_events_observation_typed
        BEFORE INSERT ON ingestion_events WHEN NEW.kind IN ('source_ready','raw_pending')
          AND COALESCE(NOT (
            NEW.subject_kind='item' AND NEW.subject_id=NEW.item_id
            AND EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=NEW.item_id
                        AND i.ingestion_contract=NEW.contract
                        AND i.source_binding_sha256=json_extract(NEW.detail_json,'$.source_binding_sha256')
                        AND i.relation_binding_sha256=json_extract(NEW.detail_json,'$.relation_binding_sha256'))
            AND (SELECT count(*) FROM json_each(NEW.detail_json))=4
            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json))=4
            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json)
                            WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256'))
            AND json_type(NEW.detail_json,'$.manifest')='object'
            AND (SELECT count(*) FROM json_each(NEW.detail_json,'$.manifest'))=2
            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json,'$.manifest'))=2
            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json,'$.manifest')
                            WHERE key NOT IN ('source_fact_id','snapshot_sha256'))
            AND ((NEW.kind='source_ready' AND json_extract(NEW.detail_json,'$.code')='source_fact_ready')
                 OR (NEW.kind='raw_pending' AND json_extract(NEW.detail_json,'$.code')
                     IN ('context_pending','readback_pending','writer_pending')))
            AND ((json_type(NEW.detail_json,'$.manifest.source_fact_id')='null'
                  AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='null'
                  AND NEW.kind='raw_pending')
                 OR (json_type(NEW.detail_json,'$.manifest.source_fact_id')='integer'
                     AND json_extract(NEW.detail_json,'$.manifest.source_fact_id')>0
                     AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='text'
                     AND length(json_extract(NEW.detail_json,'$.manifest.snapshot_sha256'))=64
                     AND json_extract(NEW.detail_json,'$.manifest.snapshot_sha256') NOT GLOB '*[^0-9a-f]*'))
          ),1)
        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END;
CREATE TRIGGER ingestion_events_no_update
            BEFORE UPDATE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END;
CREATE TRIGGER ingestion_events_no_delete
            BEFORE DELETE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END;
CREATE TRIGGER ingestion_events_proof_unavailable
        BEFORE INSERT ON ingestion_events
        WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')
          AND (ingestion_proof(NEW.kind,NEW.binding_sha256,NEW.detail_json)!=1
               OR NEW.kind IS NOT json_extract(NEW.detail_json,'$.code')
               OR NEW.subject_kind IS NOT json_extract(NEW.detail_json,'$.manifest.subject_kind')
               OR NEW.subject_id IS NOT json_extract(NEW.detail_json,'$.manifest.subject_id')
               OR NEW.item_id IS NOT json_extract(NEW.detail_json,'$.manifest.owner_item_id')
               OR NEW.binding_sha256 IS NOT json_extract(NEW.detail_json,'$.final_binding_sha256'))
        BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END;
CREATE TRIGGER wiki_task_outcome_binding_immutable
        BEFORE UPDATE OF outcome_contract,plan_json,plan_sha256 ON wiki_tasks
        WHEN NEW.outcome_contract IS NOT OLD.outcome_contract OR NEW.plan_json IS NOT OLD.plan_json
          OR NEW.plan_sha256 IS NOT OLD.plan_sha256
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding is immutable'); END;
CREATE TRIGGER wiki_task_outcome_binding_required
        BEFORE INSERT ON wiki_tasks WHEN NEW.outcome_contract!='legacy' AND NEW.plan_sha256 IS NULL
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding required'); END;
CREATE TABLE wiki_outcome_receipts (
        receipt_id TEXT NOT NULL CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
        task_id TEXT NOT NULL,
        batch_no INTEGER NOT NULL CHECK(batch_no>0),
        phase TEXT NOT NULL CHECK(phase IN ('validated','accepted')),
        contract TEXT NOT NULL CHECK(contract='r08-wiki-outcomes-v1'),
        boundary_sha256 TEXT NOT NULL CHECK(length(boundary_sha256)=64 AND boundary_sha256 NOT GLOB '*[^0-9a-f]*'),
        plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
        created_at TEXT NOT NULL CHECK(trim(created_at)!=''),
        PRIMARY KEY(receipt_id,phase),
        FOREIGN KEY(task_id,batch_no) REFERENCES wiki_task_batches(task_id,batch_no)
    );
CREATE UNIQUE INDEX wiki_outcome_one_accepted_batch
        ON wiki_outcome_receipts(task_id,batch_no) WHERE phase='accepted';
CREATE TRIGGER wiki_outcome_receipts_no_update
            BEFORE UPDATE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END;
CREATE TRIGGER wiki_outcome_receipts_no_delete
            BEFORE DELETE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END;
CREATE TRIGGER wiki_outcome_receipts_publish_unavailable
        BEFORE INSERT ON wiki_outcome_receipts WHEN NEW.phase='accepted'
        BEGIN SELECT RAISE(ABORT,'publish proof unavailable'); END;
CREATE TRIGGER source_media_ingestion_no_update
            BEFORE UPDATE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
              AND ingestion_release(OLD.material_id,OLD.member_id,OLD.sha256,NEW.content)!=1
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END;
CREATE TRIGGER source_media_ingestion_no_delete
            BEFORE DELETE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
""" "\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20" r"""
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_no_release
        BEFORE UPDATE OF content,input_metadata ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
          AND (NEW.content IS NOT OLD.content OR NEW.input_metadata IS NOT OLD.input_metadata)
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_no_delete
        BEFORE DELETE ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_owner_immutable
        BEFORE UPDATE OF item_id ON submitted_sources
        WHEN NEW.item_id IS NOT OLD.item_id AND EXISTS (
            SELECT 1 FROM distill_items i WHERE i.item_id IN (OLD.item_id,NEW.item_id)
            AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input owner is immutable'); END;
CREATE TRIGGER source_media_ingestion_capture_binding
        BEFORE UPDATE OF item_id,audio_path ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND (NEW.item_id IS NOT OLD.item_id
            OR (OLD.audio_path IS NOT NULL AND NEW.audio_path IS NOT OLD.audio_path))
        BEGIN SELECT RAISE(ABORT,'ingestion capture binding is immutable'); END;
CREATE TRIGGER source_media_ingestion_capture_release
        BEFORE UPDATE OF audio_released_at ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND NEW.audio_released_at IS NOT OLD.audio_released_at
          AND ingestion_release('capture',OLD.capture_id,OLD.audio_path,NEW.audio_released_at)!=1
        BEGIN SELECT RAISE(ABORT,'ingestion audio is retained'); END;
CREATE TRIGGER source_media_ingestion_capture_no_delete
        BEFORE DELETE ON capture_state WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1'))
        BEGIN SELECT RAISE(ABORT,'ingestion capture owner is durable'); END;
PRAGMA user_version=24;
COMMIT;
"""


def create_v24(path):
    with closing(sqlite3.connect(path)) as db:
        for name in ('wiki-schema21.sql', 'wiki-schema22.sql', 'upgrade-probe-schema23.sql'):
            db.executescript((FIXTURES / name).read_text())
        db.executescript(V24_SQL)
        assert db.execute('PRAGMA user_version').fetchone()[0] == 24
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript((FIXTURES / 'upgrade-probe-seed.sql').read_text())
        for offset, state in enumerate(('working', 'waiting_user', 'succeeded', 'failed'), 1):
            db.execute('''INSERT INTO distill_items(item_id,submitted_url,state,phase,queued_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)''', (51+offset, 'synthetic://legacy', state, 'done', STAMP, STAMP, STAMP))
        original = '旧原件\r\n中文é完整保留'
        db.execute('''INSERT INTO raw_records(raw_id,subject_kind,subject_id,identity,relative_path,content,
            content_sha256,origin,created_at) VALUES ('R-20261008-0001','material',41,'第三方',
            'raw/外部/2026/10/R-20261008-0001.md',?,?,'app',?)''',
                   (original, hashlib.sha256(original.encode()).hexdigest(), STAMP))
        # Populate the post-21 domains too: observation and frozen wiki ownership.
        db.execute("INSERT INTO wiki_tasks(task_id,vault_path,vault_key,request_kind,trigger_source,backend,model,effort,kit_version,kit_manifest_sha256,boundary_sha256,state,raw_count,batch_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('1'*32, '/synthetic/never-opened', 'a'*64, 'all', 'cli', 'codex_cli', 'synthetic', 'medium', '3', 'b'*64, 'c'*64, 'queued', 1, 1, STAMP, STAMP))
        db.execute("INSERT INTO wiki_task_batches VALUES(?,1,'queued',1,NULL)", ('1'*32,))
        db.execute('INSERT INTO wiki_task_raw VALUES(?,1,1,?,?,?,?,?)',
                   ('1'*32, 'R-20261008-0001', '第三方', 'raw/外部/2026/10/R-20261008-0001.md', 5, 'd'*64))
        db.execute('INSERT INTO wiki_observations VALUES(?,?,?,?,?,?,NULL)',
                   ('a'*64, '/synthetic/never-opened', '1'*32, 1, 2, STAMP))
        db.commit()


def contents(path):
    # Independent assertion inventory: values, storage types, physical column order and FKs.
    with closing(sqlite3.connect(path)) as db:
        result = {}
        for (table,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall():
            quoted = '"'+table.replace('"','""')+'"'
            columns = list(db.execute(f'PRAGMA table_xinfo({quoted})'))
            names = ','.join('"'+c[1]+'"' for c in columns)
            types = ','.join('typeof("'+c[1]+'")' for c in columns)
            result[table] = (columns, list(db.execute(f'SELECT rowid,{names},{types} FROM {quoted} ORDER BY rowid')),
                             list(db.execute(f'PRAGMA foreign_key_list({quoted})')),
                             list(db.execute(f'PRAGMA index_list({quoted})')))
        return result


def catalog(path):
    with closing(sqlite3.connect(path)) as db:
        return {(kind,name):(table,sql) for kind,name,table,sql in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema')}


def test_real24_preserves_all_old_typed_data_objects_and_does_not_backfill(tmp_path):
    path = tmp_path.resolve() / 'synthetic24.sqlite3'
    create_v24(path)
    before, old_catalog = contents(path), catalog(path)
    database.initialize(path)
    assert contents(path) == before
    after = catalog(path)
    for key,value in old_catalog.items():
        if key != ('table','distill_items'):
            assert after[key] == value
    assert "'raw_saved'" in after['table','distill_items'][1]
    assert set(after)-set(old_catalog) == {('trigger', 'distill_items_raw_terminal_'+suffix)
                                         for suffix in ('no_insert','proof','no_reopen')}
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert db.execute("SELECT count(*) FROM distill_items WHERE state='raw_saved'").fetchone()[0] == 0
        assert db.execute('SELECT ingestion_contract FROM distill_items WHERE item_id=51').fetchone()[0] == 'legacy'
    database.initialize(path)
    assert contents(path) == before and catalog(path) == after


def test_failure_after_actual_rebuild_rolls_back_everything(tmp_path, monkeypatch):
    path = tmp_path.resolve() / 'synthetic24.sqlite3'
    create_v24(path)
    before, old_catalog = contents(path), catalog(path)
    check = database._check_v25_preservation
    def fail(db, *args):
        check(db, *args)
        assert db.execute("SELECT sql FROM sqlite_schema WHERE name='distill_items'").fetchone()[0].count('raw_saved') >= 2
        raise RuntimeError('synthetic_postcheck_failure')
    monkeypatch.setattr(database, '_check_v25_preservation', fail)
    with pytest.raises(RuntimeError, match='synthetic_postcheck_failure'):
        database.initialize(path)
    assert contents(path) == before and catalog(path) == old_catalog
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 24


@pytest.mark.parametrize('extra', [
    'CREATE INDEX unapproved_parent_index ON distill_items(state)',
    'CREATE VIEW unapproved_parent_view AS SELECT item_id FROM distill_items',
    "CREATE TRIGGER unapproved_parent_guard BEFORE UPDATE ON distill_items BEGIN SELECT RAISE(ABORT,'extra'); END",
    'ALTER TABLE distill_items ADD COLUMN extra TEXT',
])
def test_unknown_parent_catalog_is_retained_and_rejected(tmp_path, extra):
    path = tmp_path.resolve() / 'synthetic24.sqlite3'
    create_v24(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(extra)
        db.commit()
    before, old_catalog = contents(path), catalog(path)
    with pytest.raises(RuntimeError, match='raw terminal migration unsupported'):
        database.initialize(path)
    assert contents(path) == before and catalog(path) == old_catalog


@pytest.fixture
def world(tmp_path):
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite3')
    store.initialize()
    vault = root / 'vault'
    vault.mkdir()
    store.set_setting('vault_path', str(vault))
    mid = material(store, 'x', '来源全文保留。', media=[('image-1', PNG)])
    item = store.create_item('synthetic://raw-terminal', ingestion_contract=INGESTION_CONTRACT,
                             source_binding_sha256='a'*64, relation_binding_sha256='b'*64)
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (mid,item))
    return store, vault, Ingestion(store), mid, item


def revision(store,item):
    return store.item_bundle(item)['review_revision']


def complete(world, *, expected=None, kind='material', subject=None):
    store,vault,candidate,mid,item = world
    return candidate.complete_raw_owner(item, vault, subject_kind=kind,
        subject_id=mid if subject is None else subject,
        expected_revision=revision(store,item) if expected is None else expected)


def test_fifo_required_and_actual_writer_terminal_keeps_source_and_has_no_knowledge(world, monkeypatch):
    store,vault,candidate,mid,item = world
    with pytest.raises(IngestionError, match='raw_terminal_owner_pending'):
        complete(world)
    assert candidate.ledger.heads('material',mid) == ()
    assert store.claim_next_item() == item
    old = dict(store.item_bundle(item))
    monkeypatch.setattr(store,'mark_succeeded',lambda *_: pytest.fail('knowledge terminal called'))
    monkeypatch.setattr(store,'establish_knowledge',lambda *_: pytest.fail('knowledge called'))
    receipt = complete(world)
    row = store.item_bundle(item)
    assert (row['state'],row['phase']) == ('raw_saved','done')
    assert row['review_revision'] == old['review_revision']+1
    for key in ('item_id','material_id','source_binding_sha256','relation_binding_sha256','snapshot','lineage_json'):
        assert row[key] == old[key]
    assert (vault/receipt.relative_path).read_bytes() == candidate.ledger.record(receipt.raw_id)['content'].encode()
    assert (vault/receipt.attachments[0][0]).read_bytes() == PNG
    with connect(store.path) as db:
        for table in ('knowledge_results','wiki_tasks','collection_results'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        assert db.execute('SELECT content FROM source_media WHERE material_id=?',(mid,)).fetchone()[0] == PNG
    assert store.claim_next_item() is None
    assert store.requeue_interrupted() == 0
    with pytest.raises(IngestionError,match='release_owner_pending'):
        candidate.release_material(mid,vault)


def test_sql_default_deny_even_after_real_raw_verified(world):
    store,vault,candidate,mid,item = world
    assert store.claim_next_item() == item
    candidate.material(mid,vault,item_id=item)  # Existing writer stays nonterminal.
    assert store.item_bundle(item)['state'] == 'working'
    assert any(e[0]=='raw_verified' for e in candidate.events(f'material:{mid}'))
    with connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError,match='raw terminal proof unavailable'):
            db.execute("UPDATE distill_items SET state='raw_saved',phase='done' WHERE item_id=?",(item,))
        with pytest.raises(sqlite3.IntegrityError,match='raw terminal writer required'):
            db.execute("INSERT INTO distill_items(submitted_url,state,phase,queued_at,created_at,updated_at,ingestion_contract,source_binding_sha256,relation_binding_sha256) VALUES ('synthetic','raw_saved','done',?,?,?,'raw-verified-v1',?,?)",(STAMP,STAMP,STAMP,'a'*64,'b'*64))
    complete(world)
    for method in (lambda:store.mark_working(item,'collecting'),lambda:store.mark_succeeded(item)):
        with pytest.raises(sqlite3.IntegrityError,match='raw terminal is durable'):
            method()


@pytest.mark.parametrize('state', ['waiting_user', 'failed', 'succeeded'])
def test_non_working_terminal_and_legacy_are_not_converted(world, state):
    store, vault, candidate, mid, item = world
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET state=? WHERE item_id=?', (state, item))
    with pytest.raises(IngestionError, match='raw_terminal_owner_pending'):
        complete(world)
    legacy = store.create_item('synthetic://legacy')
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET material_id=?,state='working' WHERE item_id=?", (mid, legacy))
    with pytest.raises(IngestionError, match='raw_terminal_owner_pending'):
        candidate.complete_raw_owner(legacy, vault, subject_kind='material', subject_id=mid,
                                     expected_revision=revision(store, legacy))
    assert candidate.ledger.heads('material', mid) == ()
    assert store.item_bundle(item)['state'] == state


def test_boolean_proof_result_is_not_terminal_authority(world, monkeypatch):
    store, vault, candidate, mid, item = world
    store.claim_next_item()
    actual = candidate._insert_proven
    calls = []
    def boolean(db, *args):
        result = actual(db, *args)  # Still requires real bytes in the writer.
        calls.append(result)
        return False if len(calls) == 2 else result
    monkeypatch.setattr(candidate, '_insert_proven', boolean)
    with pytest.raises(IngestionError, match='raw_terminal_proof_invalid'):
        complete(world)
    assert len(calls) == 2 and store.item_bundle(item)['state'] == 'working'


def test_replay_requires_current_revision_and_fresh_all_attachment_readback(world,monkeypatch):
    store,vault,candidate,mid,item = world
    store.claim_next_item()
    first_revision = revision(store,item)
    receipt = complete(world)
    with pytest.raises(IngestionError,match='raw_terminal_stale'):
        complete(world,expected=first_revision)
    reads=[]
    actual=ingestion.read_regular
    def observe(root,relative):
        reads.append(relative)
        return actual(root,relative)
    monkeypatch.setattr(ingestion,'read_regular',observe)
    current = revision(store,item)
    assert complete(world) == receipt
    assert revision(store,item) == current
    assert receipt.relative_path in reads and receipt.attachments[0][0] in reads
    target=vault/receipt.attachments[0][0]
    target.write_bytes(b'changed synthetic attachment')
    with pytest.raises(IngestionError):
        complete(world)
    assert (store.item_bundle(item)['state'],revision(store,item)) == ('raw_saved',current)


@pytest.mark.parametrize('damage',['raw','attachment','missing'])
def test_prior_event_or_already_bool_never_substitutes_for_bytes(world,monkeypatch,damage):
    store,vault,candidate,mid,item=world
    store.claim_next_item()
    receipt=candidate.material(mid,vault,item_id=item)
    target=vault/(receipt.attachments[0][0] if damage=='attachment' else receipt.relative_path)
    if damage=='missing':target.unlink()
    else:target.write_bytes(b'bad synthetic bytes')
    monkeypatch.setattr(candidate.ledger,'write',lambda *_:'already')
    with pytest.raises((IngestionError,raw.RawError)):
        complete(world)
    assert store.item_bundle(item)['state']=='working'


@pytest.mark.parametrize('change',["phase='publishing'","state='queued'","review_revision=review_revision+1","submitted_title='changed'"])
def test_owner_changes_after_actual_readback_rollback_terminal(world,monkeypatch,change):
    store,vault,candidate,mid,item=world
    store.claim_next_item()
    candidate.material(mid,vault,item_id=item)
    before=dict(store.item_bundle(item))
    actual=candidate._insert_proven
    calls=[]
    def changed(db,*args):
        result=actual(db,*args)
        calls.append(args[2])
        # The first readback belongs to the unchanged writer transaction;
        # inject only into the second, terminal transaction after real proof.
        if len(calls)==2:
            db.execute(f'UPDATE distill_items SET {change} WHERE item_id=?',(item,))
        return result
    monkeypatch.setattr(candidate,'_insert_proven',changed)
    with pytest.raises(IngestionError,match='raw_terminal'):
        complete(world)
    assert len(calls)==2
    assert dict(store.item_bundle(item))==before
    with connect(store.path) as db:
        assert db.execute("SELECT ingestion_raw_terminal(?,?,'raw_saved','done')",(item,revision(store,item))).fetchone()[0]==0


@pytest.mark.parametrize('kind',['multiowner','collection'])
def test_ambiguous_or_operation_owned_item_does_not_assign_raw(world,kind):
    store,vault,candidate,mid,item=world
    store.claim_next_item()
    if kind=='multiowner':
        other=store.create_item('synthetic://other')
        with connect(store.path) as db:db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?',(mid,other))
    else:
        with connect(store.path) as db:
            op=db.execute("INSERT INTO collection_operations(kind,source_key,title,manifest_json,signature,content_signature,authority_json,confirmation_token,state,queued_at,created_at,updated_at,ingestion_contract,source_binding_sha256,relation_binding_sha256) VALUES ('same_topic','synthetic','title','{}','s','c','{}','token','queued',?,?,?,'raw-verified-v1',?,?)",(STAMP,STAMP,STAMP,'a'*64,'b'*64)).lastrowid
            db.execute('INSERT INTO collection_members(operation_id,ordinal,native_id,native_version,item_id,known_unsupported) VALUES (?,0,?,?,?,0)',(op,'synthetic','v1',item))
    with pytest.raises(IngestionError,match='raw_terminal_owner_ambiguous'):
        complete(world)
    assert candidate.ledger.heads('material',mid)==()


@pytest.mark.parametrize('decision',['pending','annotation','my_thought'])
def test_audio_identity_target_and_real_pcm_are_not_guessed(world,decision):
    store,vault,candidate,mid,item=world
    store.claim_next_item()
    with connect(store.path) as db:
        record_capture(db,'synthetic-app','synthetic-audio',message_type='audio',created_ms=1790000000000,
                       received_ms=1790000000000,file_key='synthetic-file')
        cid=db.execute('SELECT capture_id FROM captures').fetchone()[0]
        audio=Path(candidate.captures.audio_root).resolve()/f'{cid}.wav'
        audio.parent.mkdir(parents=True,exist_ok=True)
        with wave.open(str(audio),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            wav.writeframes(b''.join(struct.pack('<h',i%128-64) for i in range(16000)))
        db.execute('UPDATE capture_state SET item_id=?,audio_path=? WHERE capture_id=?',(item,str(audio),cid))
    candidate.captures._event(cid,decision,'用户',1.0,'missing-target' if decision=='annotation' else None)
    if decision!='my_thought':
        with pytest.raises(IngestionError):complete(world,kind='capture',subject=cid)
        assert candidate.ledger.heads('capture',cid)==()
        assert store.item_bundle(item)['state']=='working'
    else:
        receipt=complete(world,kind='capture',subject=cid)
        assert receipt.identity=='本人' and store.item_bundle(item)['state']=='raw_saved'
        assert audio.exists() and audio.stat().st_size==32044
        assert (vault/receipt.relative_path).read_bytes()==candidate.ledger.record(receipt.raw_id)['content'].encode()
        assert complete(world,kind='capture',subject=cid)==receipt


def create_pre24(path, version):
    """Real historical SQL only; seed before the genuine 22->23 rebuild."""
    assert version in (21, 22, 23)
    with closing(sqlite3.connect(path)) as db:
        db.executescript((FIXTURES / 'wiki-schema21.sql').read_text())
        if version >= 22:
            db.executescript((FIXTURES / 'wiki-schema22.sql').read_text())
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript((FIXTURES / 'upgrade-probe-seed.sql').read_text())
        original = '旧原件\r\n中文é完整保留'
        db.execute('''INSERT INTO raw_records(raw_id,subject_kind,subject_id,identity,relative_path,content,
            content_sha256,origin,created_at) VALUES ('R-20261008-0001','material',41,'第三方',
            'raw/外部/2026/10/R-20261008-0001.md',?,?,'app',?)''',
                   (original, hashlib.sha256(original.encode()).hexdigest(), STAMP))
        db.execute("INSERT INTO confirmation_decisions VALUES(51,'old-revision','manual','继续','waiting_user')")
        db.execute('''INSERT INTO group_decisions VALUES
            (51,'old-request','old-group','old-revision','old-selection','old-payload',
             '{ "state" : "waiting_user" }','{ "human" : "继续" }',?)''', (STAMP,))
        db.execute('''INSERT INTO manual_cards(scope_kind,scope_id,item_id,review_round_id,group_id,
            lifecycle,ordering_basis,ordering_reason,entered_at,mapping_json)
            VALUES ('items','independent',51,'old-round','old-group','active','observed',
                    'explicit synthetic human',?,'{ "human" : "继续" }')''', (STAMP,))
        if version >= 22:
            db.execute('''INSERT INTO wiki_tasks(task_id,vault_path,vault_key,request_kind,trigger_source,
                backend,model,effort,kit_version,kit_manifest_sha256,boundary_sha256,state,
                raw_count,batch_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       ('1'*32, '/synthetic/never-opened', 'a'*64, 'all', 'cli', 'codex_cli',
                        'synthetic', 'medium', '3', 'b'*64, 'c'*64, 'queued', 1, 1, STAMP, STAMP))
            db.execute("INSERT INTO wiki_task_batches VALUES(?,1,'queued',1,NULL)", ('1'*32,))
            db.execute('INSERT INTO wiki_task_raw VALUES(?,1,1,?,?,?,?,?)',
                       ('1'*32, 'R-20261008-0001', '第三方', 'raw/外部/2026/10/R-20261008-0001.md',
                        len(original.encode()), hashlib.sha256(original.encode()).hexdigest()))
        db.commit()
        if version == 23:
            db.executescript((FIXTURES / 'upgrade-probe-schema23.sql').read_text())
            db.execute('INSERT INTO wiki_observations VALUES(?,?,?,?,?,?,NULL)',
                       ('a'*64, '/synthetic/never-opened', '1'*32, 1, 2, STAMP))
            db.commit()
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def frozen_creates(script):
    """Extract exact CREATE statements from fixed SQL fixtures, without rewriting DDL."""
    import re
    result, statement = {}, ''
    for line in script.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip().removesuffix(';')
        statement = ''
        match = re.match(r'CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER) (\w+)', sql)
        if match:
            kind, name = match.groups()
            table = name if kind == 'TABLE' else re.search(r'\bON\s+(\w+)', sql).group(1)
            assert (kind.lower(), name) not in result
            result[kind.lower(), name] = (table, sql)
    assert not statement.strip()
    return result


def assert_pre24_upgrade(path, before, old_catalog, version):
    after, new_catalog = contents(path), catalog(path)
    # Only approved additive columns may follow the original physical columns.
    appended = {
        'distill_items': ('ingestion_contract', 'source_binding_sha256', 'relation_binding_sha256'),
        'collection_operations': ('ingestion_contract', 'source_binding_sha256', 'relation_binding_sha256'),
        'wiki_tasks': ('outcome_contract', 'plan_json', 'plan_sha256'),
    }
    for table, (columns, rows, fks, indexes) in before.items():
        new_columns, new_rows, new_fks, new_indexes = after[table]
        count = len(columns)
        assert new_columns[:count] == columns
        assert tuple(c[1] for c in new_columns[count:]) == appended.get(table, ())
        # Inventory rows are rowid + values + typeof; select old positions in each half.
        new_count = len(new_columns)
        assert [(r[0], *r[1:1+count], *r[1+new_count:1+new_count+count]) for r in new_rows] == rows
        assert new_fks == fks and new_indexes == indexes

    # Frozen 23 contains the sole approved historical wiki recovery DDL changes;
    # frozen 24 contains the reviewed additions, not runtime-generated target SQL.
    expected = dict(old_catalog)
    additions23 = frozen_creates((FIXTURES / 'upgrade-probe-schema23.sql').read_text())
    additions24 = frozen_creates(V24_SQL)
    expected.update(additions23)
    expected.update(additions24)
    import re
    definitions = re.findall(r'^ALTER TABLE (\w+) ADD COLUMN (.+);$', V24_SQL, re.MULTILINE)
    assert len(definitions) == 9
    last_columns = {
        'distill_items': 'review_revision INTEGER NOT NULL DEFAULT 0',
        'collection_operations': 'updated_at TEXT NOT NULL',
        'wiki_tasks': "updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != '')",
    }
    for table, last_column in last_columns.items():
        owner, sql = expected['table', table]
        assert sql.count(last_column) == 1
        added = [definition for name, definition in definitions if name == table]
        assert len(added) == 3
        # ADD COLUMN inserts after the last physical column, before table constraints.
        # These three anchors belong to the frozen fixtures, not a general SQL parser.
        expected['table', table] = (owner, sql.replace(last_column, last_column+', '+', '.join(added), 1))
    owner, parent = expected['table', 'distill_items']
    old_check = "state IN ('queued', 'working', 'waiting_user', 'succeeded', 'failed')"
    assert parent.count(old_check) == 1
    parent = parent.replace(old_check, "state IN ('queued','working','waiting_user','succeeded','failed','raw_saved')")
    parent = parent[:-1] + ", CHECK(state!='raw_saved' OR (phase='done' AND ingestion_contract='raw-verified-v1')))"
    expected['table', 'distill_items'] = (owner, parent.replace('CREATE TABLE distill_items', 'CREATE TABLE "distill_items"', 1))
    # These fixed constraint indexes are created by the corresponding approved tables.
    for table, count in (('wiki_tasks', 2), ('wiki_task_batches', 1), ('wiki_task_raw', 2),
                         ('wiki_observations', 1), ('ingestion_events', 1), ('wiki_outcome_receipts', 1)):
        for number in range(1, count+1):
            expected['index', f'sqlite_autoindex_{table}_{number}'] = (table, None)
    terminal_names = {('trigger', 'distill_items_raw_terminal_'+suffix)
                      for suffix in ('no_insert', 'proof', 'no_reopen')}
    assert set(new_catalog) == set(expected) | terminal_names
    for key, value in expected.items():
        assert new_catalog[key] == value, key
    # Explicitly distinguish new 22/23 domains from new 24 storage and 25 guards.
    assert set(new_catalog)-set(old_catalog) == (set(expected)-set(old_catalog)) | terminal_names
    if version == 21:
        assert 'wiki_tasks' not in before and 'wiki_observations' not in before
        assert after['wiki_tasks'][1] == [] and after['wiki_observations'][1] == []
    elif version == 22:
        assert 'wiki_observations' not in before and after['wiki_observations'][1] == []
    else:
        assert before['wiki_observations'][1] == after['wiki_observations'][1]
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 25
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert db.execute("SELECT count(*) FROM distill_items WHERE state='raw_saved'").fetchone()[0] == 0
        assert tuple(db.execute('SELECT ingestion_contract,source_binding_sha256,relation_binding_sha256 FROM distill_items WHERE item_id=51').fetchone()) == ('legacy', None, None)
        assert db.execute('SELECT count(*) FROM ingestion_events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM wiki_outcome_receipts').fetchone()[0] == 0


@pytest.mark.parametrize('version', [21, 22, 23])
def test_real_pre24_sql_lanes_preserve_old_typed_rows_ids_fks_and_approved_objects(tmp_path, version):
    path = tmp_path.resolve() / f'synthetic{version}.sqlite3'
    create_pre24(path, version)
    before, old_catalog = contents(path), catalog(path)
    database.initialize(path)
    assert_pre24_upgrade(path, before, old_catalog, version)


@pytest.mark.parametrize('version', [21, 22, 23])
def test_real_pre24_sql_lanes_postcheck_failure_restores_original_version_and_contents(tmp_path, monkeypatch, version):
    path = tmp_path.resolve() / f'synthetic{version}.sqlite3'
    create_pre24(path, version)
    before, old_catalog = contents(path), catalog(path)
    actual_check = database._check_v25_preservation
    def interrupted(db, *args):
        actual_check(db, *args)
        raise RuntimeError('synthetic_pre24_postcheck_failure')
    monkeypatch.setattr(database, '_check_v25_preservation', interrupted)
    with pytest.raises(RuntimeError, match='synthetic_pre24_postcheck_failure'):
        database.initialize(path)
    assert contents(path) == before and catalog(path) == old_catalog
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
