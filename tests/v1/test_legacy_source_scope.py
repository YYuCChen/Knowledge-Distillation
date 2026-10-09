"""Disposable real owner/message/raw graphs; no model or manufactured proof.

Historical material setup reuses the accepted hash-checked schema25 fixture
helpers. New bound owners always use the current P2 Store public API.
"""
import hashlib
import json
import re
import sqlite3

import pytest

from knowledge_distiller.v1 import database, raw
from knowledge_distiller.v1.captures import Captures
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message, bind_item
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.ingestion import (
    Ingestion, IngestionError, require_legacy_item, require_legacy_sources,
)
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.store import Store
from .test_source_schema26_compat import historical_module, material


@pytest.fixture
def current(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    store = Store(root / 'synthetic.sqlite')
    store.initialize()
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    return store, vault, Captures(store), raw.RawLedger(store)


def full_state(store, vault):
    with connect(store.path) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        rows = {name: tuple(tuple(r) for r in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'))
                for name in names}
        catalog = tuple(tuple(r) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name'))
        version = db.execute('PRAGMA user_version').fetchone()[0]
    files = {p.relative_to(vault).as_posix(): (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
             for p in vault.rglob('*') if p.is_file()}
    return version, catalog, rows, files


def assert_read_only_catalog_queries(statements):
    # Only the actual finite-catalog ABI reads, never an arbitrary PRAGMA or
    # an assignment to user_version, are admitted by this query budget.
    pragma = re.compile(r'PRAGMA\s+(?:USER_VERSION|(?:TABLE_XINFO|FOREIGN_KEY_LIST|'
                        r'INDEX_LIST|INDEX_XINFO)\("(?:[^"]|"")*"\))\s*;?')
    assert all(s.lstrip().upper().startswith('SELECT ') or
               pragma.fullmatch(s.strip().upper()) is not None for s in statements)


def read_scope(world, kind, subject, *, item_id=None, refs=(), error=None):
    store, vault = world[:2]
    before = full_state(store, vault)
    statements = []
    with connect(store.path) as db:
        db.execute('PRAGMA query_only=ON')
        # Reject BLOB access by the scope function; snapshots above are outside
        # this budget boundary and deliberately inspect the complete state.
        def authorizer(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and (table, column) in {
                    ('source_media', 'content'), ('submitted_sources', 'content')}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        db.set_trace_callback(statements.append)
        if error:
            with pytest.raises(IngestionError, match='^' + error + '$'):
                require_legacy_sources(db, kind, subject, item_id=item_id, referenced_raw_ids=refs)
        else:
            assert require_legacy_sources(db, kind, subject, item_id=item_id, referenced_raw_ids=refs) is None
        assert db.total_changes == 0
    assert_read_only_catalog_queries(statements)
    assert full_state(store, vault) == before


def receive(world, name, *, at=1790000000000, app='synthetic'):
    store, _, captures, _ = world
    inbox = FeishuInbox(store, app)
    inbox.bind(bot_open_id='synthetic-bot', user_open_id='synthetic-user',
               chat_id='synthetic-chat', start_ms=0)
    message = Message(name, 'synthetic-chat', 'synthetic-user', 'user', 'p2p', at,
                      'text', json.dumps({'text': '合成原话 ' + name}), (),
                      {'fixture_contract': 'controlled-authenticated-synthetic-message-v1'})
    assert inbox.receive(message)['state'] == 'received'
    return captures.for_message(app, name)['capture_id']


def bound_owner(world, label='bound'):
    source = prepare_direct_text('独立合成本地来源 ' + label)
    return world[0].submit_local_bound_source(source,
        envelope_json=build_local_binding(source).envelope_json)


def literal_record(world, cid, *, target=None, adjacency=()):
    _, vault, captures, ledger = world
    record = captures.ensure_raw(captures.get(cid), captures.identity(cid), list(adjacency), target, ledger=ledger)
    assert ledger.write(record, vault) in {'placed', 'already'}
    # This is an actual literal raw placement, never a canonical ingestion event.
    return record


@pytest.mark.parametrize('version', [25, 26])
def test_historical_material_scope_and_delegates_preserve_all_state(tmp_path, version):
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    root = tmp_path.resolve()
    root.chmod(0o700)
    path = root / 'historical.sqlite'
    old_db.initialize(path)  # genuine schema25; never PRAGMA-downgrade26
    store = old_store.Store(path)
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    world = store, vault, ingestion
    item, mid, record = material(world)
    if version == 26:
        historical_module('database', '2fb751a17abb28620d2dee2ebe1086914675eb6a',
            'e1fc6ae6cd0bfb90b56a4314a53e8c098947f53edef809647f3d03fad82eccf2').initialize(path)
    read_scope(world, 'material', mid, item_id=item, refs=(record['raw_id'],))
    before = full_state(store, vault)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert require_legacy_item(db, item) is None
        assert ingestion._require_legacy_item(db, item) is None
        assert ingestion._require_legacy_sources(db, 'material', mid, item,
            referenced_raw_ids=(record['raw_id'],)) is None
    assert full_state(store, vault) == before


def test_current27_capture_scope_requires_complete_proof_guard_catalog(current):
    cid = receive(current, 'literal')
    current[2].decide(cid, 'my_thought')
    record = literal_record(current, cid)
    with connect(current[0].path) as db:
        db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
    read_scope(current, 'capture', cid, refs=(record['raw_id'], record['raw_id']),
               error='candidate_schema_rebuild_required')
    before = full_state(*current[:2])
    ingestion = Ingestion(current[0])
    with connect(current[0].path) as db:
        with pytest.raises(IngestionError, match='^candidate_schema_rebuild_required$'):
            ingestion._require_writer_sources(db, 'capture', cid)
    assert full_state(*current[:2]) == before


@pytest.mark.parametrize('statement', ['PRAGMA user_version=27', 'PRAGMA journal_mode',
                                      'PRAGMA writable_schema=ON', 'DELETE FROM raw_records'])
def test_read_only_query_budget_rejects_mutation_and_unknown_pragmas(statement):
    with pytest.raises(AssertionError):
        assert_read_only_catalog_queries([statement])


@pytest.mark.parametrize('route', ['owner', 'annotation', 'adjacency', 'parts', 'multi_step'])
def test_actual_bound_owner_and_message_graph_is_pending_without_effects(current, route):
    owner = bound_owner(current)
    bound = receive(current, 'bound')
    current[2].decide(bound, 'my_thought')
    cid = bound
    if route != 'owner':
        cid = receive(current, 'current', at=1790000001000 if route == 'adjacency' else 1790003600000)
        if route == 'parts':
            with connect(current[0].path) as db:
                bind_item(db, ('synthetic', 'current', 0), owner)
            current[2].decide(cid, 'my_thought')
        else:
            current[2].decide(cid, 'annotation' if route in {'annotation', 'multi_step'} else 'my_thought',
                              target='bound' if route in {'annotation', 'multi_step'} else None)
        if route == 'multi_step':
            cid = receive(current, 'third', at=1790007200000)
            current[2].decide(cid, 'annotation', target='current')
    # Form the actual historical graph before the root acquires a bound owner;
    # current intake correctly refuses adding adjacency to an already bound root.
    current[2]._link(bound, owner)
    read_scope(current, 'capture', cid, error='local_source_qualification_pending')


def test_cycle_of_actual_identity_targets_terminates_and_checks_bound_owner(current):
    a = receive(current, 'a')
    b = receive(current, 'b', at=1790003600000)
    current[2].decide(a, 'annotation', target='b')
    current[2].decide(b, 'annotation', target='a')
    read_scope(current, 'capture', a)
    owner = bound_owner(current)
    current[2]._link(b, owner)
    read_scope(current, 'capture', a, error='local_source_qualification_pending')


def test_shared_actual_legacy_owner_reaches_other_capture_target(tmp_path):
    # Reuse the accepted historical fixture APIs, then only current P2 intake.
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    root = tmp_path.resolve()
    root.chmod(0o700)
    path = root / 'historical.sqlite'
    old_db.initialize(path)
    historical = old_store.Store(path)
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    historical.set_setting('vault_path', str(vault))
    item, mid, _ = material((historical, vault, Ingestion(historical)), write_raw=False)
    database.initialize(path)
    store = Store(path)
    world = store, vault, Captures(store), raw.RawLedger(store)
    a = receive(world, 'shared-a')
    b = receive(world, 'shared-b', at=1790003600000)
    world[2]._link(a, item)
    world[2]._link(b, item)
    world[2].decide(a, 'my_thought')
    world[2].decide(b, 'my_thought')
    read_scope(world, 'material', mid)
    bound = receive(world, 'bound', at=1790007200000)
    world[2]._link(bound, bound_owner(world))
    world[2].decide(bound, 'my_thought')
    world[2].decide(b, 'annotation', target='bound')
    read_scope(world, 'capture', a, error='local_source_qualification_pending')


def test_explicit_raw_root_uses_actual_subject_not_envelope_identity(current):
    root = receive(current, 'independent')
    current[2].decide(root, 'my_thought')
    other = receive(current, 'reference', at=1790003600000, app='other-app')
    current[2].decide(other, 'my_thought')
    record = literal_record(current, other)
    read_scope(current, 'capture', root, refs=(record['raw_id'],))
    current[2]._link(other, bound_owner(current))
    # Existing literal envelope still says 本人; actual DB bound scope wins.
    read_scope(current, 'capture', root, refs=(record['raw_id'],),
               error='local_source_qualification_pending')
    before = full_state(*current[:2])
    with connect(current[0].path) as db:
        with pytest.raises(IngestionError, match='^local_source_qualification_pending$'):
            Ingestion(current[0])._require_writer_sources(db, 'capture', root,
                referenced_raw_ids=(record['raw_id'],))
    assert full_state(*current[:2]) == before


def test_current_raw_head_reference_reaches_actual_bound_subject(current):
    target = receive(current, 'target', app='other-app')
    current[2].decide(target, 'my_thought')
    reference = literal_record(current, target)
    cid = receive(current, 'root')
    current[2].decide(cid, 'my_thought')
    literal_record(current, cid, adjacency=({'编号': reference['raw_id'], '间隔秒': 1},))
    current[2]._link(target, bound_owner(current))
    read_scope(current, 'capture', cid, error='local_source_qualification_pending')


@pytest.mark.parametrize('refs', [None, 'R-20261008-0001', {'complete': True},
    (None,), (True,), (1,), ('bad-id',), ('R-20990101-9999',)])
def test_explicit_reference_missing_bad_or_wrong_type_is_fixed_pending(current, refs):
    cid = receive(current, 'root')
    current[2].decide(cid, 'my_thought')
    read_scope(current, 'capture', cid, refs=refs, error='local_source_qualification_pending')


def test_reserved_capture_id_is_not_an_actual_raw_reference(current):
    cid = receive(current, 'root')
    current[2].decide(cid, 'my_thought')
    reserved = current[2].get(cid)['raw_id']
    assert current[3].record(reserved) is None
    read_scope(current, 'capture', cid, refs=(reserved,), error='local_source_qualification_pending')
