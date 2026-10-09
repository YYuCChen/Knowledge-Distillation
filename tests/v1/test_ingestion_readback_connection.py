"""Fixed schema26 readback adapter, actual historical sources and Inbox APIs.

No fake canonical receipts, guard removal, production data, or model calls.
External byte corruption touches only disposable fixtures after real proof.
"""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge_distiller.v1 import database, ingestion, raw
from knowledge_distiller.v1.captures import Captures
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.ingestion import Ingestion, IngestionError, verify_record
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.store import Store
from .test_capture_legacy_writer_veto import receive
from .test_source_schema26_compat import historical_module, material, PNG
from .test_legacy_source_scope import full_state


@pytest.fixture
def world(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    path = root / 'synthetic.sqlite3'
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    old_db.initialize(path)  # actual schema25, never PRAGMA downgrade
    store = old_store.Store(path)
    vault = root / 'vault'; vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    candidate = Ingestion(store)
    item, mid, record = material((store, vault, candidate), 'readback')
    version = next(e[1] for e in candidate.events(f'material:{mid}') if e[0] == 'raw_verified')
    database.initialize(path)  # real migration preserves the canonical event
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 26, 'use fixed2fb751a plus only two overlays'
    store = Store(path)
    return SimpleNamespace(store=store, vault=vault, candidate=Ingestion(store),
                           item=item, mid=mid, record=dict(record), version=version)


def checked(world, *, db=None, record=None, released=False):
    return verify_record(world.candidate.ledger, world.record if record is None else record,
                         world.vault, source_version=world.version, _released=released, db=db)


def forbid_connections(monkeypatch):
    spies = []
    for module in (ingestion, raw):
        spy = Mock(side_effect=AssertionError('independent readback connection'))
        monkeypatch.setattr(module, 'connect', spy)
        spies.append(spy)
    return spies


def test_default_none_compatibility_uses_original_media_connections(world, monkeypatch):
    before = full_state(world.store, world.vault)
    real_ingestion, real_raw = ingestion.connect, raw.connect
    manifests, blobs = [], []
    def manifest_connection(*args, **kwargs):
        manifests.append(args[0])
        return real_ingestion(*args, **kwargs)
    def blob_connection(*args, **kwargs):
        blobs.append(args[0])
        return real_raw(*args, **kwargs)
    monkeypatch.setattr(ingestion, 'connect', manifest_connection)
    monkeypatch.setattr(raw, 'connect', blob_connection)
    implicit = verify_record(world.candidate.ledger, world.record, world.vault, source_version=world.version)
    explicit_none = checked(world)
    assert implicit == explicit_none
    assert manifests == [world.store.path, world.store.path]
    assert blobs == [world.store.path, world.store.path]
    assert implicit.attachments and implicit.source_version == world.version
    assert full_state(world.store, world.vault) == before


def test_supplied_connection_reads_manifest_and_full_blob_in_same_caller_transaction(world, monkeypatch):
    expected = checked(world)
    before = full_state(world.store, world.vault)
    with connect(world.store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        statements, observed = [], []
        db.set_trace_callback(statements.append)
        original = raw._attachment_bytes
        def observe(store, record, attachment, *, db=None):
            assert db is connection and db.in_transaction
            result = original(store, record, attachment, db=db)
            assert result == PNG
            observed.append((attachment['member_id'], len(result), hashlib.sha256(result).hexdigest()))
            return result
        connection = db
        monkeypatch.setattr(raw, '_attachment_bytes', observe)
        spies = forbid_connections(monkeypatch)
        assert checked(world, db=db) == expected
        assert db.in_transaction and db.execute('SELECT 1').fetchone()[0] == 1
        assert observed == [('image-1', len(PNG), hashlib.sha256(PNG).hexdigest())]
        assert any('SELECT member_id,sha256,mime_type FROM source_media' in s for s in statements)
        assert any('SELECT content, sha256 FROM source_media' in s for s in statements)
        assert not any(s.lstrip().split()[0].upper() in {'BEGIN', 'COMMIT', 'ROLLBACK', 'INSERT', 'UPDATE', 'DELETE'}
                       for s in statements)
        for spy in spies:
            spy.assert_not_called()
        db.rollback()  # only caller ends the caller-owned transaction
    assert full_state(world.store, world.vault) == before


@pytest.mark.parametrize('damage,code', [
    ('body', 'raw_bytes_mismatch'), ('attachment', 'attachment_bytes_mismatch'),
    ('duplicate_id', 'raw_id_collision'), ('missing_manifest', 'attachment_manifest_incomplete'),
    ('duplicate_manifest', 'attachment_manifest_incomplete'), ('hash', 'ledger_hash_mismatch'),
])
def test_rejections_do_not_commit_rollback_or_close_caller_connection(world, monkeypatch, damage, code):
    record = dict(world.record)
    manifest = json.loads(record['attachments_json'])
    if damage == 'body':
        (world.vault / record['relative_path']).write_bytes(b'controlled body damage')
    elif damage == 'attachment':
        (world.vault / f"附件/raw/{record['raw_id']}/{manifest[0]['filename']}").write_bytes(PNG + b'damaged')
    elif damage == 'duplicate_id':
        duplicate = world.vault / 'raw' / 'other' / (record['raw_id'] + '.md')
        duplicate.parent.mkdir(mode=0o700)
        duplicate.write_bytes(record['content'].encode())
    elif damage == 'missing_manifest':
        record['attachments_json'] = '[]'
    elif damage == 'duplicate_manifest':
        record['attachments_json'] = json.dumps(manifest + manifest)
    else:
        record['content_sha256'] = '0' * 64
    before = full_state(world.store, world.vault)
    with connect(world.store.path) as db:
        db.execute('CREATE TEMP TABLE caller_marker(value TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO caller_marker VALUES ('uncommitted')")
        spies = forbid_connections(monkeypatch)
        with pytest.raises(IngestionError, match='^' + code + '$'):
            checked(world, db=db, record=record)
        assert db.in_transaction
        assert db.execute('SELECT value FROM caller_marker').fetchone()[0] == 'uncommitted'
        db.execute("INSERT INTO caller_marker VALUES ('still caller owned')")
        db.rollback()
        assert db.execute('SELECT COUNT(*) FROM caller_marker').fetchone()[0] == 0
        for spy in spies:
            spy.assert_not_called()
    assert full_state(world.store, world.vault) == before


@pytest.mark.parametrize('damage,code', [('metadata', 'attachment_manifest_incomplete'),
                                        ('blob', 'raw_attachment_unavailable')])
def test_actual_owned_media_drift_not_cached_or_read_from_other_connection(world, monkeypatch, damage, code):
    payload = world.store.path.read_bytes()
    old, new = ((b'image/png', b'image/gif') if damage == 'metadata' else
                (PNG, PNG[:-1] + bytes([PNG[-1] ^ 1])))
    assert len(old) == len(new) and old in payload
    if damage == 'blob':
        assert payload.count(PNG) == 1
    # Disposable external corruption, not a legal source revision or fake
    # canonical SQL write. Triggers are intact; old real record stays frozen.
    world.store.path.write_bytes(payload.replace(old, new))
    before = full_state(world.store, world.vault)
    with connect(world.store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        spies = forbid_connections(monkeypatch)
        with pytest.raises((IngestionError, raw.RawError), match='^' + code + '$'):
            checked(world, db=db)
        assert db.in_transaction and db.execute('SELECT 1').fetchone()[0] == 1
        db.rollback()
        for spy in spies:
            spy.assert_not_called()
    assert full_state(world.store, world.vault) == before


def test_autocommit_connection_is_not_started_or_closed(world, monkeypatch):
    expected = checked(world)
    with connect(world.store.path) as db:
        assert not db.in_transaction
        spies = forbid_connections(monkeypatch)
        assert checked(world, db=db) == expected
        assert not db.in_transaction and db.execute('SELECT 1').fetchone()[0] == 1
        for spy in spies:
            spy.assert_not_called()


def test_closed_connection_is_not_replaced_by_independent_connection(world, monkeypatch):
    import sqlite3
    with connect(world.store.path) as db:
        pass
    spies = forbid_connections(monkeypatch)
    with pytest.raises(sqlite3.ProgrammingError):
        checked(world, db=db)
    for spy in spies:
        spy.assert_not_called()


def test_real_p2_owner_does_not_turn_readback_into_local_source_authority(world, monkeypatch):
    source = prepare_direct_text('独立合成本地身份待授权输入')
    owner = world.store.submit_local_bound_source(source,
        envelope_json=build_local_binding(source).envelope_json)
    with connect(world.store.path) as db:
        assert db.execute('SELECT material_id FROM distill_items WHERE item_id=?', (owner,)).fetchone()[0] is None
    before = full_state(world.store, world.vault)
    with connect(world.store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        forbid_connections(monkeypatch)
        receipt = checked(world, db=db)
        assert receipt.subject_id == world.mid and receipt.subject_kind == 'material'
        assert db.in_transaction
        db.rollback()
    assert full_state(world.store, world.vault) == before


def test_real_inbox_capture_canonical_record_also_preserves_caller_connection(world, monkeypatch):
    from knowledge_distiller.v1.feishu_inbox import FeishuInbox
    from knowledge_distiller.v1.feishu_intake import FeishuIntake
    captures = Captures(world.store)
    cid = receive((world.store, world.vault, captures), 'readback-capture')
    captures.decide(cid, 'my_thought')
    inbox = FeishuInbox(world.store, 'synthetic')  # bound by the actual receive fixture
    FeishuIntake(inbox, links=None, wake=None, api=None, jev=None).process('readback-capture')
    actual = world.candidate.capture(cid, world.vault)
    record = dict(world.candidate.ledger.record(actual.raw_id))
    before = full_state(world.store, world.vault)
    with connect(world.store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        spies = forbid_connections(monkeypatch)
        result = verify_record(world.candidate.ledger, record, world.vault,
                               source_version=actual.source_version, db=db)
        assert result == actual and result.subject_kind == 'capture'
        assert db.in_transaction
        db.rollback()
        for spy in spies:
            spy.assert_not_called()
    assert full_state(world.store, world.vault) == before
