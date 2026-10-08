"""Real disposable 25 -> 26 migration and source APIs; no model or App.

Run from the fixed c00198a checkout plus the four approved overlays. Historical
initializer/Store blobs are hash checked, not a PRAGMA downgrade or mock proof.
Only damage cases alter disposable schema; no test inserts canonical events.
"""
import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

from knowledge_distiller.v1 import database, wiki_source_proof as proof
from knowledge_distiller.v1.captures import record_capture
from knowledge_distiller.v1.database import connect, INGESTION_CONTRACT
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.ingestion import Ingestion, IngestionError
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.source_parsing import ParsedSource, ParsedMedia
from knowledge_distiller.v1.wiki_lock import VaultWriteLock, vault_key
from knowledge_distiller.v1.wiki_staging import StagingSnapshot, SnapshotFile
from knowledge_distiller.v1.wiki_tasks import FrozenRaw, WikiBatch, WikiTask, _boundary

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII=')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def historical_module(name, commit, expected):
    repo = Path(os.environ.get('KD_SCHEMA_HISTORY_REPO', str(Path(__file__).resolve().parents[2])))
    payload = subprocess.check_output(['git', '-C', str(repo), 'show',
        commit + ':src/knowledge_distiller/v1/' + name + '.py'])
    assert sha(payload) == expected
    module = ModuleType('knowledge_distiller.v1._synthetic_' + name + '_' + commit)
    module.__package__ = 'knowledge_distiller.v1'
    sys.modules[module.__name__] = module
    try:
        exec(compile(payload, '<hash-checked-synthetic-' + name + '>', 'exec'), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module


@pytest.fixture
def world(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    path = root / 'synthetic.sqlite'
    old_db.initialize(path)  # genuine schema25, never lowered from26
    store = old_store.Store(path)
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    return store, vault, Ingestion(store)


def material(world, label='old', *, canonical=True, write_raw=True):
    store, vault, ingestion = world
    submitted = prepare_direct_text('完整合成正文 ' + label)
    item = store.submit_source(submitted, ingestion_contract=INGESTION_CONTRACT,
        source_binding_sha256=sha(submitted.content), relation_binding_sha256=sha(b'no selected relations'))
    parsed = ParsedSource('完整合成正文 ' + label, {'source_title': 'synthetic'},
        {'fixture_contract': 'controlled-synthetic-source-v1'}, (ParsedMedia('image-1', 'image/png', PNG),))
    store.establish_submitted_fact(item, submitted, parsed)
    mid = store.item_bundle(item)['material_id']
    if not write_raw:
        return item, mid, None
    if canonical:
        receipt = ingestion.material(mid, vault, item_id=item)
        record = ingestion.ledger.record(receipt.raw_id)
    else:
        record = ingestion.ledger.ensure_material(mid)
        assert ingestion.ledger.write(record, vault) in {'placed', 'already'}
    return item, mid, record


def frozen(world, record):
    _, vault, _ = world
    root = vault.parent / 'staging'
    root.mkdir(mode=0o700)
    workspace = root / 'workspace'
    workspace.mkdir(mode=0o700)
    for branch in ('raw', '附件'):
        if (vault / branch).exists():
            shutil.copytree(vault / branch, workspace / branch)
    entries = tuple(SnapshotFile(p.relative_to(workspace).as_posix(),
        'raw' if p.relative_to(workspace).parts[0] == 'raw' else 'attachment', len(p.read_bytes()), sha(p.read_bytes()))
        for p in sorted(workspace.rglob('*')) if p.is_file())
    row = FrozenRaw(record['relative_path'], record['raw_id'], record['identity'],
        len(record['content'].encode()), record['content_sha256'], 1, 1)
    task = WikiTask('a' * 32, str(vault), vault_key(vault), 'manual', 'synthetic',
        'codex', 'generation-unchanged', 'medium', 'synthetic-kit', 'b' * 64,
        _boundary(((row,),)), 'running', 1, 1, 0, None, 'active', 'running', '', '',
        (WikiBatch(1, 'running', 1, None),), (row,))
    snapshot = StagingSnapshot(task.task_id, root, workspace, root / 'control', root / 'backup',
        entries, (row.raw_id,))
    return task, snapshot, ((row, (vault / row.relative_path).read_bytes()),)


def verify(world, inputs):
    store, vault, _ = world
    with VaultWriteLock.acquire(vault) as lock:
        return proof.verify_wiki_sources(store, *inputs, lock)


def state(world):
    store, vault, _ = world
    with connect(store.path) as db:
        rows = {table: tuple(tuple(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid'))
                for table in ('raw_records', 'raw_counters', 'source_facts', 'source_media',
                              'distill_items', 'ingestion_events', 'capture_state', 'capture_identity_events')}
    files = {p.relative_to(vault).as_posix(): (p.stat().st_ino, sha(p.read_bytes()))
             for p in vault.rglob('*') if p.is_file()}
    return rows, files


def test_real_migration_preserves_historical_binding_events_and_owned_bytes(world):
    store, _, ingestion = world
    item, mid, record = material(world)
    inputs = frozen(world, record)
    with connect(store.path) as db:
        old_binding = ingestion._binding(db, record, item)
        old_input = tuple(db.execute('SELECT * FROM submitted_sources WHERE item_id=?', (item,)).fetchone())
        assert database.source_schema_inventory(db) == (25, ())
    old_state, old_proof = state(world), verify(world, inputs)
    database.initialize(store.path)  # real atomic migration; immutable events retained
    with connect(store.path) as db:
        assert database.source_schema_inventory(db) == (26, ())
        assert ingestion._binding(db, record, item) == old_binding
        migrated = tuple(db.execute('SELECT * FROM submitted_sources WHERE item_id=?', (item,)).fetchone())
        assert migrated == old_input + ('legacy',)
    new_proof = verify(world, inputs)
    assert state(world) == old_state
    for result in (old_proof, new_proof):
        source = result.manifest['sources'][0]
        assert 'canonical_ingestion_event' in source['capabilities']
        assert source['gaps'] == []
        assert source['attachments'][0]['byte_count'] == len(PNG)
        assert not source['scope']['platform_total_verified']
    assert new_proof.digest != old_proof.digest  # honest schema observation, not a re-signed event
    assert new_proof.manifest['sources'][0]['source_schema_version'] == 26
    # Current 26 consumption still uses real bytes and the original POST/owner contract.
    assert ingestion.material(mid, world[1], item_id=item).raw_id == record['raw_id']


def test_legacy_without_event_stays_literal_after_migration(world):
    store, _, _ = world
    _, _, record = material(world, canonical=False)
    inputs = frozen(world, record)
    database.initialize(store.path)
    before = state(world)
    source = verify(world, inputs).manifest['sources'][0]
    assert 'canonical_ingestion_event' not in source['capabilities']
    assert source['gaps'] == ['canonical_event_unavailable']
    assert state(world) == before


@pytest.mark.parametrize('version', [25, 26])
@pytest.mark.parametrize('guard,damage', [
    ('ingestion_events_proof_unavailable', 'missing'),
    ('ingestion_events_proof_unavailable', 'comment'),
    ('ingestion_events_proof_unavailable', 'wrong_table'),
    ('ingestion_events_proof_unavailable', 'wrong_type'),
    ('distill_items_raw_terminal_proof', 'missing'),
    ('submitted_sources_ingestion_no_release', 'missing')])
def test_bad_guard_is_literal_and_all_writers_leave_no_changes(world, version, guard, damage):
    store, vault, ingestion = world
    item, mid, record = material(world)
    inputs = frozen(world, record)
    fresh_item, fresh_mid, _ = material(world, 'never-written', write_raw=False)
    if version == 26:
        database.initialize(store.path)
    with connect(store.path) as db:
        db.execute('DROP TRIGGER ' + guard)
        if damage == 'wrong_type':
            db.execute('CREATE VIEW ' + guard + " AS SELECT 'ingestion_proof comment' AS literal")
        elif damage != 'missing':
            table = 'source_media' if damage == 'wrong_table' else 'ingestion_events'
            db.execute('CREATE TRIGGER ' + guard + ' BEFORE INSERT ON ' + table +
                       ' BEGIN SELECT 1; /* ingestion_proof is only a comment */ END')
    before = state(world)
    source = verify(world, inputs).manifest['sources'][0]
    assert source['capabilities'] == ['declared_attachments_readback', 'literal_text']
    assert 'internal_event_guard_unavailable' in source['gaps']
    assert guard in source['schema_guard_gaps']
    for action in (lambda: ingestion.material(fresh_mid, vault, item_id=fresh_item),
                   lambda: ingestion.complete_raw_owner(fresh_item, vault, subject_kind='material',
                       subject_id=fresh_mid, expected_revision=store.item_bundle(fresh_item)['review_revision']),
                   lambda: ingestion.release_material(mid, vault)):
        with pytest.raises(IngestionError, match='candidate_schema_rebuild_required'):
            action()
        assert state(world) == before
        assert ingestion.ledger.heads('material', fresh_mid) == ()


@pytest.mark.parametrize('guard', ['submitted_sources_local_insert', 'submitted_sources_local_tuple',
                                 'submitted_sources_local_no_delete', 'ingestion_events_local_owner'])
def test_schema26_local_guards_required_even_for_legacy_consumption(world, guard):
    store, vault, ingestion = world
    item, mid, record = material(world)
    inputs = frozen(world, record)
    database.initialize(store.path)
    with connect(store.path) as db:
        db.execute('DROP ' + ('INDEX' if guard == 'ingestion_events_local_owner' else 'TRIGGER') + ' ' + guard)
    before = state(world)
    assert 'internal_event_guard_unavailable' in verify(world, inputs).manifest['sources'][0]['gaps']
    with pytest.raises(IngestionError, match='candidate_schema_rebuild_required'):
        ingestion.material(mid, vault, item_id=item)
    assert state(world) == before


def bound_store(world):
    store, _, _ = world
    pinned = historical_module('store', 'c00198a',
        '53adf63eb7efceb8aececa31def5a000062585ee6a00e913ed2ee9ef6c51d5fa')
    return pinned.Store(store.path)


@pytest.mark.parametrize('route', ['own', 'target', 'adjacency', 'material_adjacency'])
def test_bound_owner_and_message_dependencies_veto_before_raw_allocation(world, route):
    store, vault, ingestion = world
    if route == 'material_adjacency':
        legacy_item, legacy_mid, _ = material(world, 'historical-no-head', write_raw=False)
    database.initialize(store.path)
    actual = bound_store(world)
    source = prepare_direct_text('新 bound 合成原文')
    owner = actual.submit_local_bound_source(source, envelope_json=build_local_binding(source).envelope_json)
    from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message
    import json
    inbox = FeishuInbox(store, 'synthetic')
    inbox.bind(bot_open_id='synthetic-bot', user_open_id='synthetic-user', chat_id='synthetic-chat', start_ms=0)
    def receive(message_id, created_ms):
        message = Message(message_id, 'synthetic-chat', 'synthetic-user', 'user', 'p2p',
                          created_ms, 'text', json.dumps({'text': 'synthetic ' + message_id}), (),
                          {'fixture_contract': 'controlled-authenticated-synthetic-message-v1'})
        assert inbox.receive(message)['state'] == 'received'
        return ingestion.captures.for_message('synthetic', message_id)['capture_id']
    bound = receive('bound', 1790000000000)
    ingestion.captures._link(bound, owner)  # first link before any identity/raw proof
    ingestion.captures.decide(bound, 'my_thought')
    current = bound
    if route != 'own':
        current = receive('current', 1790000001000)
        if route == 'target':
            ingestion.captures.decide(current, 'annotation', target='bound')
        else:
            if route == 'material_adjacency':
                ingestion.captures._link(current, legacy_item)
            ingestion.captures.decide(current, 'third_party' if route == 'material_adjacency' else 'my_thought')
            # Real receive -> record_adjacency, no SQL-manufactured relation.
            with connect(store.path) as db:
                assert db.execute('SELECT earlier_message_id FROM delivery_adjacency WHERE message_id=?',
                                  ('current',)).fetchone()[0] == 'bound'
    before = state(world)
    with pytest.raises(IngestionError, match='local_source_qualification_pending'):
        if route == 'material_adjacency':
            ingestion.material(legacy_mid, vault)  # no requested owner supplied
        else:
            ingestion.capture(current, vault)
    assert state(world) == before
    assert ingestion.ledger.heads('capture', current) == ()
    if route == 'material_adjacency':
        assert ingestion.ledger.heads('material', legacy_mid) == ()
    with connect(store.path) as db:
        assert tuple(db.execute('SELECT kind FROM ingestion_events WHERE item_id=?', (owner,)).fetchone()) == ('raw_pending',)


def test_bound_requested_owner_cannot_use_legacy_material_or_terminal(world):
    store, vault, ingestion = world
    _, mid, _ = material(world)
    database.initialize(store.path)
    actual = bound_store(world)
    source = prepare_direct_text('独立 bound owner')
    owner = actual.submit_local_bound_source(source, envelope_json=build_local_binding(source).envelope_json)
    before = state(world)
    for action in (lambda: ingestion.material(mid, vault, item_id=owner),
                   lambda: ingestion.complete_raw_owner(owner, vault, subject_kind='material', subject_id=mid, expected_revision=0)):
        with pytest.raises(IngestionError, match='local_source_qualification_pending'):
            action()
        assert state(world) == before


def test_new_bound_literal_raw_never_inherits_canonical_capability(world):
    store, vault, ingestion = world
    database.initialize(store.path)
    with connect(store.path) as db:
        record_capture(db, 'synthetic', 'literal', message_type='text',
                       created_ms=1790000000000, received_ms=1790000000000,
                       text='合成有限原话', vault=vault)
    capture = ingestion.captures.for_message('synthetic', 'literal')
    cid = capture['capture_id']
    ingestion.captures.decide(cid, 'my_thought')
    capture = ingestion.captures.get(cid)
    record = ingestion.captures.ensure_raw(capture, ingestion.captures.identity(cid), [], None,
                                          ledger=ingestion.ledger)
    assert ingestion.ledger.write(record, vault) in {'placed', 'already'}
    actual = bound_store(world)
    submitted = prepare_direct_text('独立原始输入仍待实际资格')
    owner = actual.submit_local_bound_source(submitted, envelope_json=build_local_binding(submitted).envelope_json)
    # First real owner binding; no canonical event existed before this link.
    assert ingestion.events('capture:' + str(cid)) == []
    ingestion.captures._link(cid, owner)
    inputs = frozen(world, record)
    before = state(world)
    result = verify(world, inputs).manifest['sources'][0]
    assert result['capabilities'] == ['declared_attachments_readback', 'literal_text']
    assert 'local_source_qualification_pending' in result['gaps']
    assert result['event_keys'] == [] and result['source_binding_sha256'] is None
    with connect(store.path) as db:
        with pytest.raises(IngestionError, match='local_source_qualification_pending'):
            ingestion._binding(db, record, None)  # requested_owner=None cannot bypass
    assert state(world) == before


def test_changed_owned_media_still_rejects_full_attachment_readback(world):
    store, vault, _ = world
    _, _, record = material(world)
    database.initialize(store.path)
    inputs = frozen(world, record)
    attachment = next((vault / '附件').rglob('*.png'))
    attachment.chmod(0o600)
    attachment.write_bytes(b'changed synthetic bytes')
    before = state(world)
    # The frozen snapshot check rejects changed attachment bytes before cross-root comparison.
    with pytest.raises(proof.SourceProofError, match='source_snapshot_mismatch'):
        verify(world, inputs)
    assert state(world) == before


def test_current26_normal_capture_requires_no_local_material_or_bare_owner(tmp_path):
    from knowledge_distiller.v1.store import Store
    root = tmp_path.resolve()
    root.chmod(0o700)
    store = Store(root / 'current26.sqlite')
    store.initialize()  # actual current26, no historical Store engine
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    world = store, vault, ingestion
    with connect(store.path) as db:
        record_capture(db, 'synthetic', 'current26', message_type='text',
                       created_ms=1790000000000, received_ms=1790000000000,
                       text='当前26合成本人原话', vault=vault)
    cid = ingestion.captures.for_message('synthetic', 'current26')['capture_id']
    ingestion.captures.decide(cid, 'my_thought')
    record = ingestion.ledger.record(ingestion.capture(cid, vault).raw_id)
    before = state(world)
    result = verify(world, frozen(world, record)).manifest['sources'][0]
    assert 'canonical_ingestion_event' in result['capabilities']
    assert result['gaps'] == []
    assert ingestion.captures.get(cid)['item_id'] is None
    assert state(world) == before


def test_unknown_raw_table_abi_is_rejected_without_writer_effects(world):
    store, vault, ingestion = world
    item, mid, record = material(world)
    inputs = frozen(world, record)
    database.initialize(store.path)
    with connect(store.path) as db:
        db.execute('ALTER TABLE raw_records ADD COLUMN unknown_contract TEXT')
    before = state(world)
    with pytest.raises(proof.SourceProofError, match='source_schema_unsupported'):
        verify(world, inputs)
    with pytest.raises(IngestionError, match='candidate_schema_rebuild_required'):
        ingestion.material(mid, vault, item_id=item)
    assert state(world) == before


@pytest.mark.parametrize('version', [25, 26])
def test_checked_terminal_writer_retains_historical_scope_and_real_readback(world, version):
    store, vault, ingestion = world
    item, mid, _ = material(world, 'historical-terminal', write_raw=False)
    if version == 26:
        database.initialize(store.path)
    assert store.claim_next_item() == item
    revision = store.item_bundle(item)['review_revision']
    receipt = ingestion.complete_raw_owner(item, vault, subject_kind='material',
                                          subject_id=mid, expected_revision=revision)
    with connect(store.path) as db:
        owner = db.execute('SELECT state,phase,review_revision FROM distill_items WHERE item_id=?', (item,)).fetchone()
        assert tuple(owner) == ('raw_saved', 'done', revision + 1)
    record = ingestion.ledger.record(receipt.raw_id)
    assert (vault / record['relative_path']).read_bytes() == record['content'].encode()
    source = verify(world, frozen(world, record)).manifest['sources'][0]
    assert 'canonical_ingestion_event' in source['capabilities'] and source['gaps'] == []
