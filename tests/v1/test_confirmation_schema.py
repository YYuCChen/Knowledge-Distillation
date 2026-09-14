"""V1.3 K01: identity and queue invariants from frozen specification §§6–7."""
import json

import pytest

from knowledge_distiller.v1.database import connect, initialize
from knowledge_distiller.v1.store import Store


def pending(text='甲词乙词', names=('one.wav', 'two.wav')):
    return {'snapshot': text, 'concerns': [
        {'start': i * 2, 'end': i * 2 + 2, 'text': text[i*2:i*2+2],
         'audio_name': name, 'member_id': 'media-1', 'candidates': ['修正'],
         'reason': '听辨', 'unknown_member_field': {'preserve': True}}
        for i, name in enumerate(names)], 'resolved': [],
        'unknown_pending_field': ['keep'], 'review_identity': 'review-original'}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'isolated.sqlite3')
    result.initialize()
    return result


def read(store, item):
    return json.loads(store.item_bundle(item)['confirmation_json'])


def test_identity_separates_media_round_and_mutable_revision(store):
    item = store.create_item('synthetic:1')
    store.mark_waiting(item, pending())
    first = read(store, item)
    assert first['format_version'] == 2
    assert len({c['concern_uid'] for c in first['concerns']}) == 2
    assert {c['member_id'] for c in first['concerns']} == {'media-1'}
    old_revision = store.item_bundle(item)['review_revision']
    changed = read(store, item)
    changed['concerns'][0]['audio_name'] = 'new.wav'
    changed['concerns'][0]['audio_file'] = 'new.wav'
    store.update_confirmation_suggestions(item, store.item_bundle(item)['confirmation_json'], changed)
    second = read(store, item)
    assert store.item_bundle(item)['review_revision'] > old_revision
    assert second['review_round_id'] == first['review_round_id']
    assert second['concerns'][0]['concern_uid'] == first['concerns'][0]['concern_uid']
    assert second['concerns'][0]['decision_revision'] == first['concerns'][0]['decision_revision']
    assert second['unknown_pending_field'] == ['keep']
    assert second['concerns'][0]['unknown_member_field'] == {'preserve': True}


def test_fifo_suspend_return_and_later_group_on_old_item(store):
    a, b = (store.create_item(f'synthetic:{n}') for n in (1, 2))
    store.mark_waiting(a, pending(names=('one.wav',)))
    store.mark_waiting(b, pending(names=('two.wav',)))
    original = store.manual_cards()
    assert [c['item_id'] for c in original] == [a, b]
    store.mark_failed(a, 'reviewing', 'source_unconfirmed')
    assert [c['item_id'] for c in store.manual_cards()] == [b]
    store.return_to_confirmation(a)
    assert [c['enqueue_seq'] for c in store.manual_cards()] == [c['enqueue_seq'] for c in original]
    old = store.item_bundle(a)
    updated = read(store, a)
    updated['concerns'].append(pending()['concerns'][1])
    store.update_confirmation_suggestions(a, old['confirmation_json'], updated)
    assert [c['item_id'] for c in store.manual_cards()] == [a, b, a]
    store.initialize()
    assert [c['enqueue_seq'] for c in store.manual_cards()][:2] == [c['enqueue_seq'] for c in original]


def test_semantic_change_invalidates_group_but_not_position_only(store):
    item = store.create_item('synthetic:1')
    store.mark_waiting(item, pending())
    before = read(store, item)
    moved = read(store, item)
    moved['snapshot'] = '前' + moved['snapshot']
    for c in moved['concerns']:
        c['start'] += 1
        c['end'] += 1
    store.update_confirmation_suggestions(item, store.item_bundle(item)['confirmation_json'], moved)
    after = read(store, item)
    assert [g['group_revision'] for g in after['groups']] == [g['group_revision'] for g in before['groups']]
    after['concerns'][1]['candidates'].append('新候选')
    store.update_confirmation_suggestions(item, store.item_bundle(item)['confirmation_json'], after)
    last = read(store, item)
    assert last['groups'][1]['group_revision'] != before['groups'][1]['group_revision']


def test_legacy_migration_keeps_original_json_and_persistent_mapping(store):
    a, b = (store.create_item(f'synthetic:{n}') for n in (1, 2))
    raw = json.dumps({**pending(), 'token': 'old-token'}, ensure_ascii=False)
    with connect(store.path) as db:
        db.execute('DROP TABLE group_decisions')
        db.execute('DROP TABLE manual_cards')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=18')
        for item, created in [(a, ''), (b, '2025-01-01T00:00:00+00:00')]:
            db.execute("UPDATE distill_items SET state='waiting_user',confirmation_json=?,created_at=? WHERE item_id=?", (raw, created, item))
    initialize(store.path)
    assert store.item_bundle(a)['confirmation_json'] == raw
    cards = store.manual_cards()
    assert [c['item_id'] for c in cards] == [b, b, a, a]
    assert {c['ordering_basis'] for c in cards} == {'migration_inferred'}
    mapped = store.confirmation_view(a)
    initialize(store.path)
    assert store.confirmation_view(a)['review_round_id'] == mapped['review_round_id']
    mapped['concerns'][0]['audio_name'] = 'rebuilt.wav'
    store.update_confirmation_suggestions(a, raw, mapped)
    assert read(store, a)['concerns'][0]['concern_uid'] == mapped['concerns'][0]['concern_uid']
    assert len(store.manual_cards()) == 4


def test_new_round_requeues_and_cannot_expand_published_group(store):
    a, b = (store.create_item(f'synthetic:{n}') for n in (1, 2))
    store.mark_waiting(a, pending())
    store.mark_waiting(b, pending())
    before = read(store, a)
    changed = read(store, a)
    changed['groups'][0]['member_uids'].append(changed['concerns'][1]['concern_uid'])
    with pytest.raises(ValueError, match='published_group_cannot_expand'):
        store.update_confirmation_suggestions(a, store.item_bundle(a)['confirmation_json'], changed)
    assert read(store, a) == before
    store.mark_waiting(a, {**pending(), 'review_identity': 'new-explicit-round'})
    assert read(store, a)['review_round_id'] != before['review_round_id']
    assert [c['item_id'] for c in store.manual_cards()] == [b, b, a, a]


def test_migration_failure_rolls_back_tables_version_and_pending(store, monkeypatch):
    from knowledge_distiller.v1 import confirmation_schema
    item = store.create_item('synthetic:old')
    raw = json.dumps({**pending(), 'token': 'old'}, ensure_ascii=False)
    with connect(store.path) as db:
        db.execute('DROP TABLE group_decisions')
        db.execute('DROP TABLE manual_cards')
        db.execute('PRAGMA user_version=18')
        db.execute("UPDATE distill_items SET state='waiting_user',confirmation_json=? WHERE item_id=?", (raw,item))
    original = confirmation_schema.sync
    def fail_after_mapping(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('injected migration failure')
    monkeypatch.setattr(confirmation_schema, 'sync', fail_after_mapping)
    with pytest.raises(OSError, match='injected'):
        initialize(store.path)
    with connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 18
        assert db.execute("SELECT name FROM sqlite_master WHERE name='manual_cards'").fetchone() is None
        assert db.execute('SELECT confirmation_json FROM distill_items').fetchone()[0] == raw


def test_group_ledger_atomic_replay_and_unselected_revision_cas(store):
    from knowledge_distiller.v1.confirmation_revision import ConfirmationConflict
    item = store.create_item('synthetic:ledger')
    store.mark_waiting(item, pending())
    before = read(store, item)
    g = before['groups'][0]
    request = {'request_id': 'request-one', 'group_id': g['group_id'], 'group_revision': g['group_revision'],
               'selected_member_uids': g['member_uids'], 'action': 'manual', 'value': '修正',
               'audit': [{'concern_uid': g['member_uids'][0]}]}
    raw = store.item_bundle(item)['confirmation_json']
    assert store.resolve_confirmation(item, raw, next_confirmation=before, group_decision=request) == 'waiting_user'
    assert store.resolve_confirmation(item, 'stale', next_confirmation=before, group_decision=request) == 'waiting_user'
    with pytest.raises(ConfirmationConflict, match='payload_conflict'):
        store.resolve_confirmation(item, raw, group_decision={**request, 'value': '别的'})
    # A new request cannot bypass the published all-member version check.
    with pytest.raises(ConfirmationConflict, match='group_revision_conflict'):
        store.resolve_confirmation(item, store.item_bundle(item)['confirmation_json'], next_confirmation=before,
            group_decision={**request, 'request_id': 'two', 'group_revision': 'stale'})
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM group_decisions').fetchone()[0] == 1


def test_unselected_group_member_change_prevents_partial_commit(store):
    from knowledge_distiller.v1.confirmation_revision import ConfirmationConflict
    item = store.create_item('synthetic:two-member')
    initial = pending()
    for uid, member in zip(('a', 'b'), initial['concerns']):
        member['concern_uid'] = uid
    initial['groups'] = [{'group_id': 'g', 'member_uids': ['a', 'b'],
                          'equivalence_basis': {'kind': 'test_positive_basis'}}]
    store.mark_waiting(item, initial)
    old = read(store, item)
    changed = read(store, item)
    changed['concerns'][1]['candidates'].append('另一解释')
    store.update_confirmation_suggestions(item, store.item_bundle(item)['confirmation_json'], changed)
    raw = store.item_bundle(item)['confirmation_json']
    with pytest.raises(ConfirmationConflict, match='group_revision_conflict'):
        store.resolve_confirmation(item, raw, next_confirmation=old, group_decision={
            'request_id': 'partial', 'group_id': 'g', 'group_revision': old['groups'][0]['group_revision'],
            'selected_member_uids': ['a'], 'action': 'manual', 'value': '新词', 'audit': [{'concern_uid':'a'}]})
    assert store.item_bundle(item)['confirmation_json'] == raw
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM group_decisions').fetchone()[0] == 0


def test_request_identity_conflict_cannot_be_hidden_by_semantic_replay(tmp_path):
    from knowledge_distiller.v1.store import Store, _group_payload
    from knowledge_distiller.v1.confirmation_schema import digest
    from knowledge_distiller.v1.database import connect
    from knowledge_distiller.v1.confirmation_revision import ConfirmationConflict
    import json
    import pytest
    store = Store(tmp_path / 'ledger.sqlite3')
    store.initialize()
    item = store.create_item('https://www.douyin.com/video/ledger')
    first = {'request_id': 'first', 'group_id': 'group', 'group_revision': 'revision-one',
             'selected_member_uids': ['a'], 'action': 'keep', 'value': ''}
    second = {**first, 'request_id': 'second', 'group_revision': 'revision-two'}
    with connect(store.path) as db:
        for request in (first, second):
            db.execute('INSERT INTO group_decisions VALUES (?,?,?,?,?,?,?,?,?)',
                (item, request['request_id'], request['group_id'], request['group_revision'],
                 digest(request['selected_member_uids']), digest(_group_payload(request)),
                 json.dumps({'state': 'waiting_user'}), '[]', 'fixture'))
    with pytest.raises(ConfirmationConflict, match='payload_conflict'):
        store.group_decision(item, {**first, 'request_id': 'second'})
