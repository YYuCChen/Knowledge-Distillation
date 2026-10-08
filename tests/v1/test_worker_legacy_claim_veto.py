"""Synthetic actual schema26/P2 queues and transaction-local legacy veto.

No models, manufactured authority or downgraded DB. Service spies observe
dispatch only; all eligibility decisions run the actual shared writer gate.
"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge_distiller.v1 import raw
from knowledge_distiller.v1.captures import Captures
from knowledge_distiller.v1.collections import Collections
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.domain import CapturedMaterial
from knowledge_distiller.v1.douyin_collections import Scope, Member, connection_authority
from knowledge_distiller.v1.feishu_inbox import bind_item
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.worker import SingleWorker
from .test_bound_source_legacy_writer_veto import bound as bound_with_fact
from .test_capture_legacy_writer_veto import checkpoint, receive, bound
from .test_confirmation_preparation_storage import _pending, _wav

PENDING = 'local_source_qualification_pending'
SCHEMA = 'candidate_schema_rebuild_required'


@pytest.fixture
def world(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    store = Store(root / 'synthetic.sqlite', runtime_root=root / 'runtime')
    store.initialize()
    with connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 26, 'main must freeze the designated schema26 test base'
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    return store, vault, Captures(store), raw.RawLedger(store)


def guarded_claim(store):
    return store.claim_next_work(item_guard=raw._legacy_item_gate)


def prohibit_failure(monkeypatch, store):
    spy = Mock(side_effect=AssertionError('qualification is not generic failure'))
    monkeypatch.setattr(store, 'mark_failed', spy)
    return spy


@pytest.mark.parametrize('fact', [False, True])
def test_queued_bound_stays_identical_before_worker_construction(world, monkeypatch, fact):
    store = world[0]
    item = bound_with_fact(world, fact=fact)
    if fact:
        assert store.item_bundle(item)['source_fact_id'] is not None
        assert store.requeue_interrupted() == 1  # actual existing recovery API, no SQL state manufacture
    before = checkpoint(world)
    builder = Mock(side_effect=AssertionError('consumer must not be constructed'))
    failure = prohibit_failure(monkeypatch, store)
    worker = SingleWorker(store, builder)
    assert worker.run_one() is None
    assert worker.run_one() is None
    assert worker.pending_work() and not worker.update_ready()
    assert checkpoint(world) == before
    assert store.item_bundle(item)['state'] == 'queued'
    builder.assert_not_called()
    failure.assert_not_called()


def test_blocked_first_preserves_fifo_of_independent_legacy_items(world):
    store = world[0]
    blocked = bound(world)
    first = store.create_item('synthetic://first')
    second = store.create_item('synthetic://second')
    blocked_row = store.item_bundle(blocked)
    calls = []
    def run(item):
        assert store.item_bundle(item)['state'] == 'working'
        calls.append(item)
        store.mark_failed(item, 'collecting', 'douyin_source_unavailable')
    worker = SingleWorker(store, SimpleNamespace(run=run))
    assert worker.run_one() == first
    assert worker.run_one() == second
    assert worker.run_one() is None
    assert calls == [first, second]
    assert store.item_bundle(blocked) == blocked_row


@pytest.mark.parametrize('route', ['second_part', 'multi_step', 'cycle'])
def test_actual_message_graph_without_material_blocks_claim(world, route):
    store, _, captures, _ = world
    root = receive(world, 'root')
    other = receive(world, 'other', at=1790003600000)
    middle = receive(world, 'middle', at=1790007200000) if route == 'multi_step' else None
    item = store.create_item('synthetic://message-owned-legacy')
    with connect(store.path) as db:
        bind_item(db, ('synthetic', 'root', 0), item)
    owner = bound(world)
    if route == 'second_part':
        with connect(store.path) as db:
            bind_item(db, ('synthetic', 'root', 1), owner)
    else:
        captures._link(other, owner)  # actual first link, no raw/event proof exists
        captures.decide(root, 'annotation', target='middle' if middle else 'other')
        if middle:
            captures.decide(middle, 'annotation', target='other')
        else:
            captures.decide(other, 'annotation', target='root')
    assert store.item_bundle(item)['material_id'] is None
    before = checkpoint(world)
    builder = Mock(side_effect=AssertionError('no eligible consumer'))
    assert SingleWorker(store, builder).run_one() is None
    assert checkpoint(world) == before
    builder.assert_not_called()


def test_streaming_skipped_rows_never_write_or_load_input_blob(world):
    from knowledge_distiller.v1.file_sources import prepare_direct_text
    from knowledge_distiller.v1.intake_binding import build_local_binding
    store = world[0]
    first = bound(world)
    source = prepare_direct_text('第二份独立合成本地bound输入')
    second = store.submit_local_bound_source(source,
        envelope_json=build_local_binding(source).envelope_json)
    assert first != second  # identical input legitimately deduplicates; this case needs two real queue rows
    blocked = [first, second]
    statements = []
    seen = []
    def guard(db, item):
        assert db.in_transaction
        db.set_authorizer(lambda action, table, column, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and
            (table, column) in {('submitted_sources', 'content'), ('source_media', 'content')}
            else sqlite3.SQLITE_OK)
        db.set_trace_callback(statements.append)
        seen.append(item)
        raw._legacy_item_gate(db, item)
    before = checkpoint(world)
    assert store.claim_next_work(item_guard=guard) is None
    assert seen == blocked
    assert checkpoint(world) == before
    readonly = ('SELECT ', 'PRAGMA USER_VERSION', 'PRAGMA TABLE_XINFO(',
                'PRAGMA FOREIGN_KEY_LIST(', 'PRAGMA INDEX_LIST(', 'PRAGMA INDEX_XINFO(', 'COMMIT')
    assert all(s.lstrip().upper().startswith(readonly) for s in statements)


def test_actual_guard_and_update_share_immediate_transaction(world):
    store = world[0]
    item = store.create_item('synthetic://legacy')
    observed = []
    def guard(db, candidate):
        assert db.in_transaction
        assert db.execute('SELECT state FROM distill_items WHERE item_id=?', (candidate,)).fetchone()[0] == 'queued'
        raw._legacy_item_gate(db, candidate)
        other = sqlite3.connect(store.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')  # contention only, no fabricated business row
        finally:
            other.close()
        observed.append(candidate)
    assert store.claim_next_work(item_guard=guard) == ('item', item)
    assert observed == [item]
    assert store.item_bundle(item)['state'] == 'working'


@pytest.mark.parametrize('damage', ['guard', 'abi'])
def test_schema_damage_propagates_instead_of_skipping_or_generic_failure(world, monkeypatch, damage):
    store = world[0]
    store.create_item('synthetic://legacy')
    with connect(store.path) as db:
        if damage == 'guard':
            db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
        else:
            db.execute('ALTER TABLE capture_state ADD COLUMN synthetic_unknown TEXT')
    before = checkpoint(world)
    builder = Mock(side_effect=AssertionError('schema veto before constructor'))
    failure = prohibit_failure(monkeypatch, store)
    with pytest.raises(raw.LegacySourceVeto, match='^' + SCHEMA + '$'):
        SingleWorker(store, builder).run_one()
    assert checkpoint(world) == before
    builder.assert_not_called()
    failure.assert_not_called()


@pytest.mark.parametrize('args', [(SCHEMA,), (PENDING, 'extra')])
def test_claim_only_skips_exact_pending_exception(world, args):
    store = world[0]
    store.create_item('synthetic://first')
    store.create_item('synthetic://second')
    before = checkpoint(world)
    def guard(db, item):
        raw._legacy_item_gate(db, item)  # never fake a green gate
        raise raw.LegacySourceVeto(*args)  # controlled dispatcher exception boundary
    with pytest.raises(raw.LegacySourceVeto) as caught:
        store.claim_next_work(item_guard=guard)
    assert caught.value.args == args
    assert checkpoint(world) == before


@pytest.mark.parametrize('when', ['constructor', 'service'])
def test_real_bound_owner_race_after_claim_preserves_working_not_failed(world, monkeypatch, when):
    store = world[0]
    receive(world, 'root')
    item = store.create_item('synthetic://legacy')
    with connect(store.path) as db:
        bind_item(db, ('synthetic', 'root', 0), item)
    old = store.item_bundle(item)
    calls = []
    def mutate_and_check():
        assert store.item_bundle(item)['state'] == 'working'
        owner = bound(world)
        with connect(store.path) as db:
            bind_item(db, ('synthetic', 'root', 1), owner)
        with connect(store.path) as db:
            raw._legacy_item_gate(db, item)  # actual new dependency, raises the real pending veto
    def run(candidate):
        calls.append(candidate)
        mutate_and_check()
    def build():
        if when == 'constructor':
            mutate_and_check()
        return SimpleNamespace(run=run)
    failure = prohibit_failure(monkeypatch, store)
    assert SingleWorker(store, build).run_one() == item
    current = store.item_bundle(item)
    assert current['state'] == 'working'  # claim truly happened; no misleading queued rewind
    assert current['error_code'] == old['error_code']
    assert current['phase'] == old['phase']
    assert current['confirmation_json'] == old['confirmation_json']
    assert calls == ([] if when == 'constructor' else [item])
    failure.assert_not_called()


def test_real_schema_damage_after_claim_is_not_translated_to_failure(world, monkeypatch):
    store = world[0]
    item = store.create_item('synthetic://legacy')
    def build():
        with connect(store.path) as db:
            db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
        with connect(store.path) as db:
            raw._legacy_item_gate(db, item)
    failure = prohibit_failure(monkeypatch, store)
    with pytest.raises(raw.LegacySourceVeto, match='^' + SCHEMA + '$'):
        SingleWorker(store, build).run_one()
    assert store.item_bundle(item)['state'] == 'working'
    failure.assert_not_called()


@pytest.mark.parametrize('when', ['constructor', 'service'])
def test_normal_exception_still_isolates_one_item_and_continues_fifo(world, caplog, when):
    store = world[0]
    first = store.create_item('synthetic://first')
    second = store.create_item('synthetic://second')
    calls = []
    def run(item):
        calls.append(item)
        if item == first:
            raise ValueError('private-provider-body')
        store.mark_failed(item, 'collecting', 'douyin_source_unavailable')
    def build():
        if when == 'constructor' and store.item_bundle(first)['state'] == 'working':
            raise ValueError('private-provider-body')
        return SimpleNamespace(run=run)
    worker = SingleWorker(store, build)
    assert worker.run_one() == first
    assert worker.run_one() == second
    assert store.item_bundle(first)['error_code'] == 'processing_unexpected_failure'
    assert calls == ([second] if when == 'constructor' else [first, second])
    assert 'private-provider-body' not in caplog.text


def test_actual_requeue_then_guard_does_not_reclaim_bound_item(world):
    store = world[0]
    item = bound(world)
    assert store.claim_next_work() == ('item', item)  # intentionally unchanged unguarded Store ABI
    assert store.requeue_interrupted() == 1
    before = checkpoint(world)
    assert SingleWorker(store, Mock(side_effect=AssertionError('consumer'))).run_one() is None
    assert checkpoint(world) == before
    assert store.item_bundle(item)['state'] == 'queued'


def test_all_blocked_loop_uses_original_wait_instead_of_busy_spin(world):
    bound(world)
    builder = Mock(side_effect=AssertionError('consumer'))
    worker = SingleWorker(world[0], builder, idle_seconds=0.37)
    waited = []
    def wait(timeout):
        waited.append(timeout)
        worker._stopping.set()
    worker._wake = SimpleNamespace(wait=wait, clear=lambda: None)
    before = checkpoint(world)
    worker._loop()  # one controlled loop iteration, no thread/App/model/service startup
    assert waited == [0.37]
    assert checkpoint(world) == before
    builder.assert_not_called()


def test_mixed_real_presentation_claim_keeps_ownership_branch(world):
    store = world[0]
    blocked = bound(world)
    item = store.create_item('https://www.douyin.com/video/123')
    media = world[1].parent / 'synthetic.mp4'
    media.write_bytes(b'fixture-owned-material')
    store.attach_material(item, CapturedMaterial('douyin', '123', 'fixture', 'fixture',
                                                {'source_version': 'v1'}, media, 2))
    store.mark_waiting(item, _pending())
    _wav(store.preparation_runtime_root / 'items' / str(item) / 'audio' / 'standard.wav')
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    blocked_row = store.item_bundle(blocked)
    seen = []
    def guard(db, candidate):
        seen.append(candidate)
        raw._legacy_item_gate(db, candidate)
    assert store.claim_next_work(item_guard=guard) == ('presentation', item)
    assert seen == [blocked]
    assert store.presentation_ownership(item) is not None
    assert store.item_bundle(item)['state'] == 'working'
    assert store.item_bundle(blocked) == blocked_row
    # No preparer called, no ready/display/accepted receipt fabricated.


def test_mixed_real_collection_claim_does_not_gate_its_members_as_items(world):
    store = world[0]
    blocked = bound(world)
    store.save_connection('douyin', None)
    scope = Scope('creator_collection', '900', '合成集合', 'creator',
        (Member('101', '作品一', True, 0, 'v1'), Member('102', '作品二', True, 0, 'v2')),
        '2026-09-06T00:00:00+00:00', connection_authority(store))
    discovery = SimpleNamespace(discover=lambda *_args, **_kwargs: {'scopes': [scope], 'choices': []})
    collections = Collections(store, discovery)
    preview = collections.preview(['https://www.douyin.com/collection/900'])
    operation = collections.confirm(preview['token'], [scope.signature])[0]
    seen = []
    def guard(db, candidate):
        seen.append(candidate)
        raw._legacy_item_gate(db, candidate)
    assert store.claim_next_work(item_guard=guard) == ('collection', operation)
    assert seen == [blocked]
    with connect(store.path) as db:
        assert db.execute('SELECT state FROM collection_operations WHERE operation_id=?', (operation,)).fetchone()[0] == 'working'
        assert {r[0] for r in db.execute('SELECT i.state FROM distill_items i JOIN collection_members cm USING(item_id) WHERE operation_id=?', (operation,))} == {'queued'}


def test_unguarded_store_and_direct_claim_next_item_keep_original_abi(world):
    store = world[0]
    first = bound(world)
    second = store.create_item('synthetic://legacy')
    assert store.claim_next_work() == ('item', first)
    assert store.claim_next_item() == second
    assert store.claim_next_work(item_guard=None) is None
    # These direct APIs are not this task's fixed SingleWorker legacy boundary.


def test_non_callable_guard_rejects_before_any_row_change(world):
    world[0].create_item('synthetic://legacy')
    before = checkpoint(world)
    with pytest.raises(TypeError, match='item_guard must be callable'):
        world[0].claim_next_work(item_guard=True)
    assert checkpoint(world) == before
