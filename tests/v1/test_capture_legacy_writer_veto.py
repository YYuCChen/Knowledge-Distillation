"""Real Inbox/Store graphs in disposable roots; no model or fabricated proof.

Historical setup proves finite legacy25/26 compatibility, not new D5 authority.
Spies observe forbidden effects; they never make a scope/proof gate pass.
"""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from knowledge_distiller.v1 import database, raw
from knowledge_distiller.v1.captures import Captures, record_capture
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message, bind_item
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.ingestion import (
    Ingestion, IngestionError, require_legacy_item_sources, require_legacy_message,
    require_legacy_sources,
)
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.store import Store
from .test_legacy_source_scope import full_state
from .test_source_schema26_compat import historical_module, material

AT = 1790000000000
PENDING = 'local_source_qualification_pending'
SCHEMA = 'candidate_schema_rebuild_required'


@pytest.fixture
def current(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    store = Store(root / 'synthetic.sqlite')
    store.initialize()
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    return store, vault, Captures(store), raw.RawLedger(store, version='synthetic')


def checkpoint(world):
    state = full_state(*world[:2])
    files = {p.relative_to(world[1].parent).as_posix():
             (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
             for p in world[1].parent.rglob('*') if p.is_file()}
    return state, files


def inbox(world, app):
    result = FeishuInbox(world[0], app)
    result.bind(bot_open_id='synthetic-bot', user_open_id='synthetic-user',
                chat_id='synthetic-chat', start_ms=0)
    return result


def message(name, *, at=AT, link=False, voice=False):
    content = ({'file_key': 'synthetic-audio', 'duration': 1000} if voice else
               {'text': 'https://example.invalid/synthetic' if link else '合成原话 ' + name})
    return Message(name, 'synthetic-chat', 'synthetic-user', 'user', 'p2p', at,
                   'audio' if voice else 'text', json.dumps(content), (),
                   {'fixture_contract': 'controlled-authenticated-synthetic-message-v1'})


def receive(world, name, *, app='synthetic', at=AT, link=False, voice=False):
    assert inbox(world, app).receive(message(name, at=at, link=link, voice=voice))['state'] == 'received'
    capture = world[2].for_message(app, name)
    return capture['capture_id'] if capture else None


def bound(world):
    source = prepare_direct_text('合成本地bound来源')
    return world[0].submit_local_bound_source(source,
        envelope_json=build_local_binding(source).envelope_json)


def assign(world, cid, *, refs=(), target=None):
    c = world[2]
    return c.ensure_raw(c.get(cid), c.identity(cid), list(refs), target, ledger=world[3])


def stop_calls(monkeypatch, world):
    spies = []
    for obj, name in ((raw, 'allocate'), (raw, 'insert'), (raw, 'place'),
                      (world[2], 'render'), (world[2], 'release_audio')):
        spy = Mock(side_effect=AssertionError('unexpected ' + name))
        monkeypatch.setattr(obj, name, spy)
        spies.append(spy)
    return spies


def unchanged(world, before, spies=()):
    assert checkpoint(world) == before
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize('app,name', [('synthetic', 'unknown'), ('', 'unknown'), ('synthetic', None)])
def test_direct_strings_without_real_message_reject_before_reserve(current, monkeypatch, app, name):
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with connect(current[0].path) as db:
        with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
            record_capture(db, app, name, message_type='text', created_ms=AT,
                           received_ms=AT, text='caller正文不是receipt证明', vault=current[1])
    unchanged(current, before, spies)


@pytest.mark.parametrize('root', ['message', 'item'])
def test_real_roots_are_read_only_no_blob_and_check_all_parts(current, root):
    receive(current, 'delivery', link=True)
    legacy = current[0].create_item('synthetic://legacy')
    with connect(current[0].path) as db:
        bind_item(db, ('synthetic', 'delivery', 0), legacy)
    before = checkpoint(current)
    statements = []
    with connect(current[0].path) as db:
        db.execute('PRAGMA query_only=ON')
        db.set_authorizer(lambda action, table, column, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and
            (table, column) in {('source_media', 'content'), ('submitted_sources', 'content')}
            else sqlite3.SQLITE_OK)
        db.set_trace_callback(statements.append)
        if root == 'message':
            require_legacy_message(db, 'synthetic', 'delivery')
        else:
            require_legacy_item_sources(db, legacy)
        assert db.total_changes == 0
    unchanged(current, before)
    assert all(s.lstrip().upper().startswith(('SELECT ', 'PRAGMA USER_VERSION')) for s in statements)
    with connect(current[0].path) as db:
        bind_item(db, ('synthetic', 'delivery', 1), bound(current))
    before = checkpoint(current)
    with connect(current[0].path) as db:
        with pytest.raises(IngestionError, match='^' + PENDING + '$'):
            if root == 'message':
                require_legacy_message(db, 'synthetic', 'delivery')
            else:
                require_legacy_item_sources(db, legacy)
    unchanged(current, before)


def test_raw_item_gate_reaches_bound_second_part_without_material(current):
    receive(current, 'delivery', link=True)
    item = current[0].create_item('synthetic://legacy')
    owner = bound(current)
    with connect(current[0].path) as db:
        bind_item(db, ('synthetic', 'delivery', 0), item)
        bind_item(db, ('synthetic', 'delivery', 1), owner)
    before = checkpoint(current)
    with connect(current[0].path) as db:
        with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
            raw._legacy_item_gate(db, item)
    unchanged(current, before)


@pytest.mark.parametrize('entry', ['item', 'subject', 'target'])
def test_missing_actual_graph_roots_fail_closed(current, entry):
    cid = receive(current, 'root')
    if entry == 'target':
        current[2].decide(cid, 'annotation', target='missing-actual-message')
    before = checkpoint(current)
    with connect(current[0].path) as db:
        with pytest.raises(IngestionError, match='^' + PENDING + '$'):
            if entry == 'item':
                require_legacy_item_sources(db, 987654321)
            else:
                require_legacy_sources(db, 'capture', cid if entry == 'target' else 987654321)
    unchanged(current, before)


def test_actual_receipt_bound_part_blocks_direct_reservation(current, monkeypatch):
    receive(current, 'delivery', link=True)  # actual receipt, no capture allocated
    owner = bound(current)
    with connect(current[0].path) as db:
        bind_item(db, ('synthetic', 'delivery', 0), owner)
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with connect(current[0].path) as db:
        with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
            record_capture(db, 'synthetic', 'delivery', message_type='text',
                           created_ms=AT, received_ms=AT, text='合成scope候选', vault=current[1])
    unchanged(current, before, spies)


def test_inbox_adjacent_bound_part_rolls_back_receipt_and_counter(current, monkeypatch):
    receive(current, 'delivery', link=True)
    owner = bound(current)
    with connect(current[0].path) as db:
        bind_item(db, ('synthetic', 'delivery', 0), owner)
    receiver = inbox(current, 'synthetic')
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
        receiver.receive(message('following', at=AT + 1000))
    unchanged(current, before, spies)
    assert current[2].for_message('synthetic', 'following') is None


def test_duplicate_capture_rechecks_actual_owner_before_early_return(current, monkeypatch):
    cid = receive(current, 'existing')
    current[2]._link(cid, bound(current))
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with connect(current[0].path) as db:
        with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
            record_capture(db, 'synthetic', 'existing', message_type='text',
                           created_ms=AT, received_ms=AT, text='ignored duplicate', vault=current[1])
    unchanged(current, before, spies)


@pytest.mark.parametrize('route', ['owner', 'parts', 'adjacency', 'target', 'cycle'])
def test_default_ensure_scope_before_render_even_with_noop_hook(current, monkeypatch, route):
    # Establish actual messages before binding so setup does not bypass the new ingress gate.
    other = receive(current, 'other')
    cid = receive(current, 'root', at=AT + (1000 if route == 'adjacency' else 3600000))
    current[2].decide(other, 'my_thought')
    current[2].decide(cid, 'annotation' if route in {'target', 'cycle'} else 'my_thought',
                      target='other' if route in {'target', 'cycle'} else None)
    if route == 'cycle':
        current[2].decide(other, 'annotation', target='root')
    owner = bound(current)
    if route == 'parts':
        with connect(current[0].path) as db:
            bind_item(db, ('synthetic', 'root', 1), owner)
    else:
        current[2]._link(cid if route == 'owner' else other, owner)
    hook = Mock()
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
        current[2].ensure_raw(current[2].get(cid), current[2].identity(cid), [], None,
                              ledger=current[3], check_source=hook)
    hook.assert_not_called()
    unchanged(current, before, spies)


@pytest.mark.parametrize('reference', ['missing', 'reserved', 'bound'])
def test_explicit_reference_checked_before_render(current, monkeypatch, reference):
    cid = receive(current, 'root')
    other = receive(current, 'reference', app='other-app')
    current[2].decide(cid, 'my_thought')
    current[2].decide(other, 'my_thought')
    ref = 'R-20261008-9999'
    if reference == 'reserved':
        ref = current[2].get(other)['raw_id']
    elif reference == 'bound':
        record = assign(current, other)
        ref = record['raw_id']
        current[2]._link(other, bound(current))
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
        assign(current, cid, refs=({'编号': ref},))
    unchanged(current, before, spies)


def test_existing_head_is_gated_before_reuse(current, monkeypatch):
    cid = receive(current, 'root')
    current[2].decide(cid, 'my_thought')
    assign(current, cid)
    current[2]._link(cid, bound(current))
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
        assign(current, cid)
    unchanged(current, before, spies)


def test_same_connection_projection_does_not_call_get_inside_transaction(current, monkeypatch):
    cid = receive(current, 'root')
    current[2].decide(cid, 'my_thought')
    capture, decision = current[2].get(cid), current[2].identity(cid)
    forbidden = Mock(side_effect=AssertionError('second capture read connection'))
    monkeypatch.setattr(current[2], 'get', forbidden)
    record = current[2].ensure_raw(capture, decision, [], None, ledger=current[3])
    assert record['subject_id'] == cid
    forbidden.assert_not_called()


@pytest.mark.parametrize('drift', ['capture', 'decision'])
def test_stale_capture_or_decision_rejects_before_render(current, monkeypatch, drift):
    cid = receive(current, 'root')
    current[2].decide(cid, 'my_thought')
    capture, decision = current[2].get(cid), current[2].identity(cid)
    if drift == 'capture':
        current[2]._link(cid, current[0].create_item('synthetic://legacy'))
    else:
        current[2].decide(cid, 'my_thought')
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with pytest.raises(raw.RawError, match='^capture_' + ('source' if drift == 'capture' else 'decision') + '_changed$'):
        current[2].ensure_raw(capture, decision, [], None, ledger=current[3])
    unchanged(current, before, spies)


def test_write_ready_skips_only_bound_capture_and_continues_independent_legacy(current, monkeypatch):
    blocked = receive(current, 'blocked', app='blocked-app')
    good = receive(current, 'good', app='good-app')
    for cid in (blocked, good):
        current[2].decide(cid, 'my_thought')
    current[2]._link(blocked, bound(current))
    blocked_capture = current[2].get(blocked)
    release = Mock(wraps=current[2].release_audio)
    monkeypatch.setattr(current[2], 'release_audio', release)
    result = current[2].write_ready(current[3])
    assert result[blocked_capture['raw_id']] == PENDING
    assert result[current[2].get(good)['raw_id']] in {'placed', 'already'}
    release.assert_called_once()
    assert release.call_args.args[0]['capture_id'] == good
    assert current[3].current('capture', blocked) is None
    assert current[2].get(blocked) == blocked_capture


@pytest.mark.parametrize('entry', ['record', 'ensure', 'ready'])
def test_schema_damage_is_not_swallowed_or_rendered(current, monkeypatch, entry):
    cid = receive(current, 'root')
    current[2].decide(cid, 'my_thought')
    with connect(current[0].path) as db:
        db.execute('DROP TRIGGER ingestion_events_proof_unavailable')  # damage-only negative fixture
    spies = stop_calls(monkeypatch, current)
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^' + SCHEMA + '$'):
        if entry == 'record':
            with connect(current[0].path) as db:
                record_capture(db, 'synthetic', 'root', message_type='text',
                               created_ms=AT, received_ms=AT, text='ignored', vault=current[1])
        elif entry == 'ensure':
            assign(current, cid)
        else:
            current[2].write_ready(current[3])
    unchanged(current, before, spies)


def test_known_supersede_reference_veto_before_callback(current, monkeypatch):
    root = receive(current, 'root')
    target = receive(current, 'target', at=AT + 3600000)
    for cid in (root, target):
        current[2].decide(cid, 'my_thought')
    written, reference = assign(current, root), assign(current, target)
    current[2]._link(target, bound(current))
    current[2]._event(root, 'annotation', '用户', 1.0, 'target')
    forwarded = []
    supersede = raw.RawLedger.supersede
    def observe(ledger, *args, **kwargs):
        forwarded.append(kwargs['referenced_raw_ids'])
        return supersede(ledger, *args, **kwargs)
    monkeypatch.setattr(raw.RawLedger, 'supersede', observe)
    render = Mock(side_effect=AssertionError('supersede callback'))
    monkeypatch.setattr(current[2], 'render', render)
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
        current[2]._supersede(current[2].get(root), written, 'annotation')
    assert forwarded == [(reference['raw_id'],)]
    unchanged(current, before, (render,))


def test_late_callback_reference_refuses_insert_but_not_callback_file(current, monkeypatch):
    root = receive(current, 'root')
    target = receive(current, 'target', app='other-app')
    for cid in (root, target):
        current[2].decide(cid, 'my_thought')
    written, reference = assign(current, root), assign(current, target)
    current[2]._link(target, bound(current))
    marker = current[1].parent / 'callback-effect.txt'
    render = current[2].render
    before = full_state(*current[:2])
    def late(capture, decided, adjacency, unsettled, target_id, **kwargs):
        marker.write_text('合成callback已发生，不声称FS回滚', encoding='utf-8')
        return render(capture, decided, [{'编号': reference['raw_id']}], unsettled, target_id, **kwargs)
    monkeypatch.setattr(current[2], 'render', late)
    with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
        current[2]._supersede(current[2].get(root), written, 'my_thought')
    assert marker.is_file()
    assert full_state(*current[:2]) == before  # raw counter/rows rolled back, not callback FS


def historical_world(tmp_path, version):
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    root = tmp_path.resolve()
    root.chmod(0o700)
    path = root / 'historical.sqlite'
    old_db.initialize(path)
    store = old_store.Store(path)
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    item, _, _ = material((store, vault, Ingestion(store)), write_raw=False)
    if version == 26:
        database.initialize(path)  # actual migration, never lowering a current DB
    captures = Captures(store)
    return (store, vault, captures, raw.RawLedger(store)), item


def prepared_legacy_capture(world, item, *, voice):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b'\0\0' * 16000)
    api = SimpleNamespace(download_message_file=lambda *_: stream.getvalue())
    store, _, captures, _ = world
    captures.api = api
    cid = receive(world, 'legacy', voice=voice)
    captures._link(cid, item)  # actual existing legacy owner, before any decision/raw
    captures.decide(cid, 'my_thought')
    # Populate retained synthetic audio through the real voice downloader path,
    # then keep the existing owner (_link does not rebind an owned capture).
    if voice:
        captures._start_voice(captures.get(cid))
        assert captures.get(cid)['item_id'] == item
    store.mark_succeeded(item)
    return cid


@pytest.mark.parametrize('version', [25, 26])
@pytest.mark.parametrize('voice', [False, True])
def test_real_legacy25_to26_text_audio_keep_bytes_and_ready_contract(tmp_path, version, voice):
    world, item = historical_world(tmp_path, version)
    store, vault, captures, _ = world
    cid = prepared_legacy_capture(world, item, voice=voice)
    capture = captures.get(cid)
    assert any(c['capture_id'] == cid for c, _ in captures.ready())
    results = captures.write_ready(world[3])
    assert results[capture['raw_id']] in {'placed', 'already'}
    record = world[3].record(capture['raw_id'])
    body = (vault / record['relative_path']).read_bytes()
    assert ('完整合成正文 old' if voice else '合成原话 legacy').encode() in body
    assert assign(world, cid)['raw_id'] == record['raw_id']
    assert world[3].write(record, vault) == 'already'
    assert (vault / record['relative_path']).read_bytes() == body
    # This legacy-scope historical owner has the existing explicit ingestion
    # contract, so release_audio retains it; scope is not a release authority.
    assert captures.get(cid)['audio_released_at'] is None
    if voice:
        assert Path(capture['audio_path']).is_file()


def test_bound_second_part_retains_ready_audio_and_independent_text_continues(tmp_path, monkeypatch):
    world, item = historical_world(tmp_path, 26)
    cid = prepared_legacy_capture(world, item, voice=True)
    store, vault, captures, ledger = world
    good = receive(world, 'independent', app='independent-app')
    captures.decide(good, 'my_thought')
    # Bound intake always uses current P2 public API, not the historical Store engine.
    actual = Store(store.path)
    owner = bound((actual, vault, captures, ledger))
    with connect(store.path) as db:
        bind_item(db, ('synthetic', 'legacy', 1), owner)
    capture = captures.get(cid)
    retained = Path(capture['audio_path'])
    before_audio = (retained.stat().st_ino, retained.read_bytes())
    release = Mock(wraps=captures.release_audio)
    monkeypatch.setattr(captures, 'release_audio', release)
    result = captures.write_ready(ledger)
    assert result[capture['raw_id']] == PENDING
    assert result[captures.get(good)['raw_id']] in {'placed', 'already'}
    release.assert_called_once()
    assert release.call_args.args[0]['capture_id'] == good
    assert captures.get(cid) == capture
    assert (retained.stat().st_ino, retained.read_bytes()) == before_audio
    assert ledger.current('capture', cid) is None
