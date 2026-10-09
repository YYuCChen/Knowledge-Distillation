"""Finite27 admission with real historical sources and accepted Inbox captures.

No model, version downgrade, fabricated canonical rows, or green scope mocks.
Only negative disposable catalog cases deliberately damage their own schema.
"""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from knowledge_distiller.v1 import database, raw
from knowledge_distiller.v1.database import connect, SUBMITTED_BINDING_COLUMNS
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message, bind_item
from knowledge_distiller.v1.feishu_intake import FeishuIntake
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.ingestion import (Ingestion, IngestionError, verify_record,
    require_legacy_item, require_legacy_sources, require_legacy_message,
    require_legacy_item_sources)
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.store import Store
from .test_source_schema26_compat import historical_module, material, PNG
from .test_legacy_source_scope import full_state, assert_read_only_catalog_queries

SCHEMA = 'candidate_schema_rebuild_required'
PENDING = 'local_source_qualification_pending'


def current(root):
    root = root.resolve(); root.chmod(0o700)
    store = Store(root / 'synthetic.sqlite3'); store.initialize()
    vault = root / 'vault'; vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    with connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
    return SimpleNamespace(store=store, vault=vault, ingestion=Ingestion(store))


def self_text(world, message='own', *, canonical=True, app=None, at=1790000000000,
              identity='my_thought', target=None):
    app = app or 'synthetic-' + message
    inbox = FeishuInbox(world.store, app)
    inbox.bind(bot_open_id='synthetic-bot', user_open_id='synthetic-user',
               chat_id='synthetic-chat', start_ms=0)
    actual = Message(message, 'synthetic-chat', 'synthetic-user', 'user', 'p2p',
        at, 'text', json.dumps({'text': '完整合成自述 ' + message}), (),
        {'fixture_contract': 'controlled-authenticated-synthetic-message-v1'})
    with patch('knowledge_distiller.v1.captures.now_ms', return_value=at):
        assert inbox.receive(actual)['state'] == 'received'
    cid = world.ingestion.captures.for_message(app, message)['capture_id']
    world.ingestion.captures.decide(cid, identity, target=target)
    assert FeishuIntake(inbox, links=None, wake=None, api=None, jev=None).process(message) == 'accepted'
    receipt = world.ingestion.capture(cid, world.vault) if canonical else None
    return app, message, cid, receipt


@pytest.fixture
def world(tmp_path):
    result = current(tmp_path)
    result.app, result.message, result.cid, result.receipt = self_text(result)
    return result


def scope(world, call, error=None):
    before = full_state(world.store, world.vault)
    with connect(world.store.path) as db:
        db.execute('PRAGMA query_only=ON')
        statements = []
        db.set_trace_callback(statements.append)
        def authorizer(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and (table, column) in {
                    ('source_media', 'content'), ('submitted_sources', 'content')}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        if error:
            with pytest.raises(IngestionError, match='^' + error + '$'):
                call(db)
        else:
            assert call(db) is None
        assert db.total_changes == 0
    assert_read_only_catalog_queries(statements)
    assert full_state(world.store, world.vault) == before


def historical(root):
    root = root.resolve(); root.chmod(0o700)
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    db26 = historical_module('database', '2fb751a17abb28620d2dee2ebe1086914675eb6a',
        'e1fc6ae6cd0bfb90b56a4314a53e8c098947f53edef809647f3d03fad82eccf2')
    path = root / 'synthetic.sqlite3'; old_db.initialize(path)
    store = old_store.Store(path)
    vault = root / 'vault'; vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    item, mid, record = material((store, vault, ingestion), 'historical27')
    return SimpleNamespace(store=store, vault=vault, ingestion=ingestion,
                           item=item, mid=mid, record=record, db26=db26)


def retained(world):
    with connect(world.store.path) as db:
        binding = tuple(tuple(r) for r in db.execute('SELECT ' + ','.join(SUBMITTED_BINDING_COLUMNS) +
                                                   ' FROM submitted_sources ORDER BY item_id'))
        events = tuple(tuple(r) for r in db.execute('SELECT * FROM ingestion_events ORDER BY event_id'))
        media = tuple(tuple(r) for r in db.execute('SELECT * FROM source_media ORDER BY material_id,position'))
        records = tuple(tuple(r) for r in db.execute('SELECT * FROM raw_records ORDER BY raw_id'))
    files = {p.relative_to(world.vault).as_posix(): p.read_bytes()
             for p in world.vault.rglob('*') if p.is_file()}
    return binding, events, media, records, files


def test_actual_25_to_26_to_27_keeps_binding_events_full_media_and_scope(tmp_path):
    w = historical(tmp_path)
    original = retained(w)
    for version in (25, 26, 27):
        if version == 26:
            w.db26.initialize(w.store.path)
        elif version == 27:
            database.initialize(w.store.path)
        with connect(w.store.path) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert retained(w) == original
        scope(w, lambda db: require_legacy_item(db, w.item))
        scope(w, lambda db: require_legacy_sources(db, 'material', w.mid,
              referenced_raw_ids=(w.record['raw_id'],)))  # actual owners, no requested item
        proof = next(e[1] for e in w.ingestion.events(f'material:{w.mid}') if e[0] == 'raw_verified')
        receipt = verify_record(w.ingestion.ledger, w.record, w.vault, source_version=proof)
        assert receipt.attachments and (w.vault / receipt.attachments[0][0]).read_bytes() == PNG
        assert retained(w) == original


@pytest.mark.parametrize('version', [25, 26, 27])
def test_historical_material_can_first_write_and_replay_after_upgrade(tmp_path, version):
    w = historical(tmp_path)
    item, mid, _ = material((w.store, w.vault, w.ingestion),
                           'pending-before-upgrade', write_raw=False)
    original = retained(w)
    if version == 26:
        w.db26.initialize(w.store.path)
    elif version == 27:
        database.initialize(w.store.path)
    assert retained(w) == original
    with connect(w.store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
    receipt = w.ingestion.material(mid, w.vault, item_id=item)
    record = w.ingestion.ledger.record(receipt.raw_id)
    assert receipt.subject_kind == 'material' and receipt.subject_id == mid
    assert verify_record(w.ingestion.ledger, record, w.vault,
                         source_version=receipt.source_version) == receipt
    assert receipt.attachments and (w.vault / receipt.attachments[0][0]).read_bytes() == PNG
    before = full_state(w.store, w.vault)
    assert w.ingestion.material(mid, w.vault, item_id=item) == receipt
    assert full_state(w.store, w.vault) == before


@pytest.mark.parametrize('version', [25, 26, 27])
def test_accepted_user_expression_can_first_write_and_replay_after_upgrade(tmp_path, version):
    w = historical(tmp_path)
    app, message, cid, _ = self_text(w, 'before-upgrade', canonical=False)
    original = retained(w)
    if version == 26:
        w.db26.initialize(w.store.path)
    elif version == 27:
        database.initialize(w.store.path)
    assert retained(w) == original
    with connect(w.store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('SELECT state FROM feishu_receipts WHERE app_id=? AND message_id=?',
                          (app, message)).fetchone()[0] == 'accepted'
    scope(w, lambda db: require_legacy_message(db, app, message))
    receipt = w.ingestion.capture(cid, w.vault)
    record = w.ingestion.ledger.record(receipt.raw_id)
    assert receipt.subject_kind == 'capture' and receipt.subject_id == cid
    assert verify_record(w.ingestion.ledger, record, w.vault,
                         source_version=receipt.source_version) == receipt
    assert '完整合成自述 before-upgrade' in (w.vault / receipt.relative_path).read_text()
    before = full_state(w.store, w.vault)
    assert w.ingestion.capture(cid, w.vault) == receipt
    assert full_state(w.store, w.vault) == before


def test_current27_real_accepted_capture_and_all_scope_entries_are_read_only(world):
    for call in (
        lambda db: require_legacy_sources(db, 'capture', world.cid,
            referenced_raw_ids=(world.receipt.raw_id, world.receipt.raw_id)),
        lambda db: require_legacy_message(db, world.app, world.message),
        lambda db: world.ingestion._require_legacy_sources(db, 'capture', world.cid),
    ):
        scope(world, call)
    before = full_state(world.store, world.vault)
    assert world.ingestion.capture(world.cid, world.vault) == world.receipt
    assert full_state(world.store, world.vault) == before


@pytest.mark.parametrize('route', ['owner', 'part', 'annotation', 'adjacency', 'multi_step'])
def test_actual_bound_dependency_without_requested_owner_is_pending(tmp_path, monkeypatch, route):
    world = current(tmp_path)
    # Real accepted literal capture, before canonical ownership becomes immutable.
    world.app, world.message, world.cid, _ = self_text(world, canonical=False, app='synthetic-bound')
    root_cid = world.cid
    # Establish real dependencies while all sources are still legacy. Binding
    # the root afterwards exercises traversal without bypassing intake's veto.
    if route in {'annotation', 'adjacency', 'multi_step'}:
        _, message, cid, _ = self_text(world, 'dependent', canonical=False,
            app=world.app, at=1790000001000 if route == 'adjacency' else 1790003600000,
            identity='my_thought' if route == 'adjacency' else 'annotation',
            target=None if route == 'adjacency' else world.message)
        if route == 'multi_step':
            _, later, later_cid, _ = self_text(world, 'later', canonical=False,
                app=world.app, at=1790007200000, identity='annotation', target=message)
            message, cid = later, later_cid
        world.message, world.cid = message, cid
    source = prepare_direct_text('独立合成本地bound27输入')
    owner = world.store.submit_local_bound_source(source,
        envelope_json=build_local_binding(source).envelope_json)
    if route != 'part':
        world.ingestion.captures._link(root_cid, owner)
    else:
        with connect(world.store.path) as db:
            bind_item(db, (world.app, world.message, 0), owner)
    scope(world, lambda db: require_legacy_item(db, owner), PENDING)
    scope(world, lambda db: require_legacy_item_sources(db, owner), PENDING)
    scope(world, lambda db: require_legacy_sources(db, 'capture', world.cid), PENDING)
    scope(world, lambda db: require_legacy_message(db, world.app, world.message), PENDING)
    before = full_state(world.store, world.vault)
    spies = []
    for obj, name in ((raw, 'allocate'), (raw, 'insert'), (raw, 'place'), (world.ingestion.captures, 'render')):
        spy = Mock(side_effect=AssertionError('blocked source reached ' + name))
        monkeypatch.setattr(obj, name, spy); spies.append(spy)
    with pytest.raises(IngestionError, match='^' + PENDING + '$'):
        world.ingestion.capture(world.cid, world.vault)
    assert full_state(world.store, world.vault) == before
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize('damage', ['proof_missing', 'proof_comment', 'identity_guard',
                                  'wiki_guard', 'unknown_table', 'unique_index'])
def test_27_whole_catalog_damage_denies_before_writer_effects(world, monkeypatch, damage):
    with connect(world.store.path) as db:
        if damage in {'proof_missing', 'proof_comment'}:
            db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
            if damage == 'proof_comment':
                db.execute('''CREATE TRIGGER ingestion_events_proof_unavailable BEFORE INSERT ON ingestion_events
                    BEGIN SELECT 1; /* ingestion_proof is only a comment */ END''')
        elif damage == 'identity_guard':
            db.execute('DROP TRIGGER source_identity_events_append_verified')
        elif damage == 'wiki_guard':
            db.execute('DROP TRIGGER wiki_batch_typed_success_requires_accepted')
        elif damage == 'unique_index':
            db.execute('DROP INDEX source_identity_one_root')
        else:
            db.execute('CREATE TABLE unknown_source_extra(value TEXT)')
    scope(world, lambda db: require_legacy_sources(db, 'capture', world.cid), SCHEMA)
    before = full_state(world.store, world.vault)
    place = Mock(side_effect=AssertionError('bad catalog reached filesystem'))
    monkeypatch.setattr(raw, 'place', place)
    with pytest.raises(IngestionError, match='^' + SCHEMA + '$'):
        world.ingestion.capture(world.cid, world.vault)
    assert full_state(world.store, world.vault) == before
    place.assert_not_called()


@pytest.mark.parametrize('version', [25, 26])
def test_25_26_missing_proof_guard_keeps_pure_scope_but_writer_denies(tmp_path, version):
    w = historical(tmp_path)
    if version == 26:
        w.db26.initialize(w.store.path)
    with connect(w.store.path) as db:
        db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
    scope(w, lambda db: require_legacy_sources(db, 'material', w.mid, item_id=w.item))
    before = full_state(w.store, w.vault)
    with pytest.raises(IngestionError, match='^' + SCHEMA + '$'):
        w.ingestion.material(w.mid, w.vault, item_id=w.item)
    assert full_state(w.store, w.vault) == before


def test_actual_empty_unsupported_database_is_not_initialized(tmp_path):
    path = tmp_path / 'empty.sqlite3'
    with sqlite3.connect(path):
        pass  # genuine version0, no version spoof
    before = path.read_bytes()
    with connect(path) as db:
        with pytest.raises(IngestionError, match='^' + SCHEMA + '$'):
            require_legacy_sources(db, 'capture', 1)
    assert path.read_bytes() == before


def test_valid27_catalog_does_not_bypass_full_body_readback(world):
    record = world.ingestion.ledger.record(world.receipt.raw_id)
    (world.vault / record['relative_path']).write_bytes(b'controlled body damage')
    scope(world, lambda db: require_legacy_sources(db, 'capture', world.cid))
    before = full_state(world.store, world.vault)
    with pytest.raises(IngestionError, match='^raw_bytes_mismatch$'):
        verify_record(world.ingestion.ledger, record, world.vault,
                      source_version=world.receipt.source_version)
    assert full_state(world.store, world.vault) == before
    with pytest.raises(IngestionError, match='^raw_target_conflict$'):
        world.ingestion.capture(world.cid, world.vault)
    columns = tuple(record.keys())  # same SELECT * column order as full_state
    raw_rows = list(before[2]['raw_records'])
    matching = [i for i, row in enumerate(raw_rows)
                if row[columns.index('raw_id')] == record['raw_id']]
    assert len(matching) == 1
    expected_row = list(raw_rows[matching[0]])
    expected_row[columns.index('attempts')] += 1
    expected_row[columns.index('last_error')] = 'raw_target_conflict'
    raw_rows[matching[0]] = tuple(expected_row)
    expected_tables = dict(before[2])
    expected_tables['raw_records'] = tuple(raw_rows)
    assert full_state(world.store, world.vault) == (before[0], before[1], expected_tables, before[3])
