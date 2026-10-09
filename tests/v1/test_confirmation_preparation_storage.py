"""Real disposable SQLite/WAV contracts, no models, browser or real sources."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import wave

import pytest

from knowledge_distiller.v1.confirmation_preparation import (
    PROTOCOL, PreparationError, build_evidence, digest, input_binding, manifest_sha256,
    member_identity, output_sha256, prepared_audio, read_wav, ready,
    text_sha256, validate_change, validate_evidence, required,
)
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.domain import CapturedMaterial, SourceFact
from knowledge_distiller.v1.store import Store, SourceReviewConflict


def _wav(path, *, frames=32000, seed=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b''.join(struct.pack('<h', (n * seed) % 30000) for n in range(frames)))


def _pending():
    return {'review_identity': 'review-fixture', 'snapshot': 'Ship it now',
            'review_required': True, 'lineage': {'asr': 'original'}, 'resolved': [],
            'deferred_concerns': [], 'correction_locations': [],
            'audio_timeline': {'text': 'Ship it now', 'duration_seconds': 2.0,
                'timeline_status': 'available', 'chunks': [
                    {'text': 'Ship it now', 'start_seconds': 0.0, 'end_seconds': 2.0}]},
            'concerns': [{'start': 0, 'end': 4, 'text': 'Ship', 'reason': 'uncertain word',
                          'audio_name': 'concern-1.wav', 'candidates': ['Ship', 'Sheep']}]}


def _setup(tmp_path, *, waiting=True, source_kind='douyin'):
    store = Store(tmp_path / 'isolated.sqlite3', runtime_root=tmp_path / 'runtime')
    store.initialize()
    item = store.create_item('https://www.douyin.com/video/123')
    media = tmp_path / 'synthetic.mp4'
    media.write_bytes(b'fixture-owned-material')
    store.attach_material(item, CapturedMaterial(source_kind, '123', 'fixture', 'fixture',
                                               {'source_version': 'v1'}, media, 2))
    if waiting:
        store.mark_waiting(item, _pending())
    else:
        store.mark_working(item, 'reviewing')
    root = store.preparation_runtime_root / 'items' / str(item)
    _wav(root / 'audio' / 'standard.wav')
    return store, item, root


def _prepare(context):
    """Controlled preparer: exact synthetic source PCM and complete evidence."""
    pending = deepcopy(context['pending'])
    root = context['item_runtime_root']
    source = read_wav(root, 'audio/standard.wav')
    pending['audio_alignment'] = 'asr_chunk_v2'
    for ordinal, c in enumerate(pending['concerns']):
        filename = f'concern-{ordinal + 1}.wav'
        target = root / 'confirmation' / filename
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((root / 'audio' / 'standard.wav').read_bytes())
        c.update(audio_file=filename, audio_recovery_required=False,
                 candidate_explanations={v: '旧混合解释：' + v for v in c['candidates']},
                 sentence_span={'start': 0, 'end': len(pending['snapshot'])},
                 candidate_translations={v: '替换后的完整句义：' + v for v in c['candidates']},
                 candidate_basis={v: '此候选在当前整句指令结构中的独立文本依据：' + v for v in c['candidates']})
    # Normalize output revisions through the actual source context, before
    # evidence construction, just as the producer must do after form_groups.
    store = context['store']
    output = store.presentation_context(context['item_id'], pending,
                                        new_review=context.get('new_review'))
    pending = output['pending']
    descriptor = output['source_descriptor']
    evidence = build_evidence(pending, root, source_descriptor=descriptor,
        model={'reviewer_type': 'synthetic-reviewer', 'model': 'fixture',
               'config_sha256': digest('safe-config'), 'recognizer_sha256': digest('fake-asr')},
        ranges={c['concern_uid']: [0.0, 2.0] for c in pending['concerns']})
    return pending, evidence


def _context(store, item, **kwargs):
    return {**store.presentation_context(item, **kwargs), 'store': store, 'item_id': item,
            **({'new_review': kwargs['new_review']} if 'new_review' in kwargs else {})}


def _claimed(store, item):
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert store.claim_next_work() == ('presentation', item)
    ownership = store.presentation_ownership(item)
    return ownership, _context(store, item)


def _success(store, item, ownership, context):
    pending, evidence = _prepare(context)
    return {'status': 'prepared', 'code': None, 'ownership': ownership,
            'pending': pending, 'evidence': evidence}


def test_real_claim_readback_finish_and_ready_survive_restart(tmp_path):
    store, item, root = _setup(tmp_path)
    card = store.manual_cards()[0]
    ownership, context = _claimed(store, item)
    assert store.item_bundle(item)['state'] == 'working'
    assert store.manual_cards(include_inactive=True)[0]['lifecycle'] == 'suspended'
    result = _success(store, item, ownership, context)
    assert store.finish_pending_presentation(item, ownership, result) == 'waiting_user'
    reopened = Store(store.path, runtime_root=store.preparation_runtime_root)
    current = _context(reopened, item)
    assert ready(current['pending'], root, source_descriptor=current['source_descriptor'])
    assert reopened.discover_pending_presentations()['enqueued'] == ()
    assert reopened.manual_cards()[0]['enqueue_seq'] == card['enqueue_seq']
    uid = current['pending']['concerns'][0]['concern_uid']
    assert prepared_audio(current['pending'], root, uid, source_descriptor=current['source_descriptor']) == (root / 'confirmation/concern-1.wav').read_bytes()
    assert reopened.item_bundle(item)['source_fact_id'] is None
    # Provenance belongs to the preparation, not later settings.
    reopened.set_setting('llm_model', 'a-new-legal-config')
    assert ready(current['pending'], root, source_descriptor=_context(reopened, item)['source_descriptor'])


@pytest.mark.parametrize('code', ['confirmation_audio_unavailable', 'llm_request_failed',
                                  'llm_request_timeout', 'review_incomplete'])
def test_failure_generic_retry_keeps_all_pending_and_only_increments_attempt(tmp_path, code):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    before = context['pending']
    result = {'status': 'failed', 'code': code, 'ownership': ownership, 'pending': None, 'evidence': None}
    assert store.finish_pending_presentation(item, ownership, result) == 'failed'
    store.retry_item(item)
    after = store.confirmation_view(item)
    assert store.item_bundle(item)['state'] == 'queued'
    assert after['concerns'] == before['concerns']
    for key in ('snapshot', 'lineage', 'resolved', 'deferred_concerns', 'correction_locations'):
        assert after[key] == before[key]
    assert after['presentation_preparation']['attempt'] == 2
    assert (root / 'audio/standard.wav').is_file()
    assert store.claim_next_work() == ('presentation', item)


def test_reboot_does_not_auto_repeat_indeterminate_running_model_attempt(tmp_path):
    store, item, root = _setup(tmp_path)
    _claimed(store, item)
    Store(store.path).requeue_interrupted()
    row = store.item_bundle(item)
    assert row['state'] == 'failed' and row['error_code'] == 'review_incomplete'
    assert store.claim_next_work() is None
    assert store.discover_pending_presentations()['enqueued'] == ()
    assert json.loads(row['confirmation_json'])['presentation_preparation']['attempt'] == 1
    store.retry_item(item)
    assert store.claim_next_work() == ('presentation', item)


@pytest.mark.parametrize('tamper', ['missing_clip', 'different_pcm', 'source_change', 'missing_member',
                                  'duplicate_member', 'missing_chinese', 'wrong_output', 'protected_change'])
def test_false_success_cannot_commit_or_replace_the_running_pending(tmp_path, tamper):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    result = _success(store, item, ownership, context)
    evidence = result['evidence']
    if tamper == 'missing_clip':
        (root / 'confirmation/concern-1.wav').unlink()
    elif tamper == 'different_pcm':
        _wav(root / 'confirmation/concern-1.wav', seed=2)
        evidence['members'][0]['clip'].update(read_wav(root, 'confirmation/concern-1.wav'))
    elif tamper == 'source_change':
        _wav(root / 'audio/standard.wav', seed=3)
    elif tamper == 'missing_member':
        evidence['members'] = []
    elif tamper == 'duplicate_member':
        evidence['members'] *= 2
    elif tamper == 'missing_chinese':
        result['pending']['concerns'][0]['candidate_translations']['Ship'] = 'only English'
        evidence['members'][0]['candidate_translations']['Ship'] = 'only English'
        evidence['members'][0]['candidate_translations_sha256'] = digest(evidence['members'][0]['candidate_translations'])
        evidence['output_sha256'] = output_sha256(result['pending'])
    elif tamper == 'wrong_output':
        result['pending']['concerns'][0]['candidate_explanations']['Ship'] = '另一份解释'
    else:
        result['pending']['lineage'] = {'asr': 'rewritten'}
    evidence['manifest_sha256'] = manifest_sha256(evidence)
    with pytest.raises(PreparationError):
        store.finish_pending_presentation(item, ownership, result)
    assert store.item_bundle(item)['confirmation_json'] == ownership['expected_confirmation_json']
    assert store.item_bundle(item)['state'] == 'working'


@pytest.mark.parametrize('race', ['dismiss', 'new_fact', 'new_pending'])
def test_finish_success_and_failure_both_reject_superseded_ownership(tmp_path, race):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    success = _success(store, item, ownership, context)
    with connect(store.path) as db:
        if race == 'dismiss':
            db.execute("UPDATE distill_items SET dismissed_at='synthetic' WHERE item_id=?", (item,))
        elif race == 'new_fact':
            store.establish_source_fact(store.item_bundle(item)['material_id'], SourceFact('another confirmed source'))
        else:
            db.execute("UPDATE distill_items SET confirmation_json=json_set(confirmation_json,'$.resolved',json('[{\"action\":\"manual\"}]')) WHERE item_id=?", (item,))
    current = store.item_bundle(item)['confirmation_json']
    for result in (success, {'status': 'failed', 'code': 'llm_request_failed',
                          'ownership': ownership, 'pending': None, 'evidence': None}):
        with pytest.raises(SourceReviewConflict):
            store.finish_pending_presentation(item, ownership, result)
    assert store.item_bundle(item)['confirmation_json'] == current


def test_resolve_requires_actual_current_source_and_clip_not_prepared_label(tmp_path):
    store, item, root = _setup(tmp_path)
    raw = store.item_bundle(item)['confirmation_json']
    with pytest.raises(PreparationError):
        store.resolve_confirmation(item, raw, unable=True)
    ownership, context = _claimed(store, item)
    store.finish_pending_presentation(item, ownership, _success(store, item, ownership, context))
    raw = store.item_bundle(item)['confirmation_json']
    (root / 'confirmation/concern-1.wav').write_bytes(b'broken')
    with pytest.raises(PreparationError):
        store.resolve_confirmation(item, raw, fact=SourceFact('Ship it now'))
    assert store.item_bundle(item)['source_fact_id'] is None
    assert store.item_bundle(item)['confirmation_json'] == raw


def test_new_source_commit_requires_final_normalized_evidence(tmp_path):
    store, item, root = _setup(tmp_path, waiting=False)
    revision = store.item_bundle(item)['review_revision']
    review = (revision, 'source-review', {'failure': None, 'schema': 1})
    context = _context(store, item, pending=_pending(), new_review=review)
    with pytest.raises(PreparationError):
        store.commit_source_review(item, revision, review[1], review[2], confirmation=context['pending'])
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM source_review_results').fetchone()[0] == 0
    pending, evidence = _prepare(context)
    pending['presentation_preparation'] = {'protocol': PROTOCOL, 'input_binding': evidence['input_binding'],
                                          'attempt': 1, 'outcome': 'prepared', 'evidence': evidence}
    store.commit_source_review(item, revision, review[1], review[2], confirmation=pending)
    current = _context(store, item)
    assert current['source_descriptor'] == context['source_descriptor']
    assert current['source_descriptor']['review_revision'] == revision
    assert store.item_bundle(item)['review_revision'] > revision
    assert context['pending']['concern_total'] == current['pending']['concern_total'] == 1
    assert output_sha256(current['pending']) == output_sha256(pending)
    assert ready(current['pending'], root, source_descriptor=current['source_descriptor'])
    assert current['pending']['concerns'][0]['concern_uid'] == pending['concerns'][0]['concern_uid']


def test_no_timeline_old_audio_still_claimed_then_fails_without_a_normal_card(tmp_path):
    store, item, root = _setup(tmp_path)
    pending = _pending()
    pending.pop('audio_timeline')
    store.mark_waiting(item, pending)
    ownership, context = _claimed(store, item)
    assert not ready(context['pending'], root, source_descriptor=context['source_descriptor'])
    store.finish_pending_presentation(item, ownership, {'status': 'failed',
        'code': 'confirmation_audio_unavailable', 'ownership': ownership, 'pending': None, 'evidence': None})
    assert store.item_bundle(item)['state'] == 'failed'


@pytest.mark.parametrize('relative', ['../audio/standard.wav', '/audio/standard.wav',
                                      'C:/absolute.wav', 'C:drive.wav', 'audio\\standard.wav'])
def test_readback_rejects_escape_and_drive_paths(tmp_path, relative):
    _wav(tmp_path / 'audio/standard.wav')
    with pytest.raises(PreparationError, match='confirmation_audio_unavailable'):
        read_wav(tmp_path, relative)


def test_readback_rejects_symlink_and_fifo_without_reading_them(tmp_path):
    import os
    _wav(tmp_path / 'audio/standard.wav')
    (tmp_path / 'alias.wav').symlink_to(tmp_path / 'audio/standard.wav')
    with pytest.raises(PreparationError):
        read_wav(tmp_path, 'alias.wav')
    if hasattr(os, 'mkfifo'):
        os.mkfifo(tmp_path / 'pipe.wav')
        with pytest.raises(PreparationError):
            read_wav(tmp_path, 'pipe.wav')


def test_protected_unknown_fields_and_candidate_order_cannot_change(tmp_path):
    store, item, root = _setup(tmp_path)
    context = _context(store, item)
    before = context['pending']
    before['user_owned_future_field'] = {'note': 'keep'}
    after = deepcopy(before)
    after['user_owned_future_field']['note'] = 'changed'
    with pytest.raises(PreparationError):
        validate_change(before, after)
    after = deepcopy(before)
    after['concerns'][0]['candidates'].reverse()
    with pytest.raises(PreparationError):
        validate_change(before, after)
    after = deepcopy(before)
    after['token'] = 'new-token'
    assert input_binding(before, source_descriptor=context['source_descriptor']) == input_binding(after, source_descriptor=context['source_descriptor'])


def test_directory_sibling_mutation_does_not_reject_unchanged_held_audio(tmp_path):
    from knowledge_distiller.v1.confirmation_preparation import _regular
    _wav(tmp_path / 'audio' / 'standard.wav')
    expected = (tmp_path / 'audio' / 'standard.wav').read_bytes()
    with _regular(tmp_path, 'audio/standard.wav') as held:
        (tmp_path / 'unrelated').write_bytes(b'parallel worker')
        (tmp_path / 'audio' / 'unrelated').write_bytes(b'parallel clip')
        assert held.read() == expected


def test_unknown_origin_without_timeline_or_wav_name_still_requires_preparation(tmp_path):
    store, item, root = _setup(tmp_path)
    pending = _pending()
    pending.pop('audio_timeline')
    pending['snapshot'] = '正文'
    pending['concerns'] = [{'start': 0, 'end': 2, 'text': '正文', 'audio_name': '',
                            'candidates': ['正文', '原文']}]
    store.mark_waiting(item, pending)
    context = store.presentation_context(item)
    assert context['source_descriptor']['source_modality'] == 'unknown'
    assert required(context['pending'], context['source_descriptor'])
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert not ready(context['pending'], root, source_descriptor=context['source_descriptor'])


def test_proven_text_english_requires_sentence_evidence_without_audio(tmp_path):
    store, item, root = _setup(tmp_path, source_kind='direct_text')
    with connect(store.path) as db:
        db.execute("UPDATE submitted_sources SET input_kind='direct_text' WHERE item_id=?", (item,))
    pending = _pending()
    pending.pop('audio_timeline')
    pending['concerns'][0]['audio_name'] = ''
    store.mark_waiting(item, pending)
    ownership, context = _claimed(store, item)
    assert context['source_descriptor']['source_modality'] == 'text'
    pending = deepcopy(context['pending'])
    pending['concerns'][0].update(sentence_span={'start': 0, 'end': 11},
        candidate_translations={'Ship': '现在运送它。', 'Sheep': '现在这只羊。'},
        candidate_basis={'Ship': '上下文包含发货动作。', 'Sheep': '上下文另有动物讨论。'})
    context = store.presentation_context(item, pending)
    pending = context['pending']
    ranges = {c['concern_uid']: None for c in pending['concerns']}
    (root / 'audio' / 'standard.wav').unlink()
    evidence = build_evidence(pending, root, source_descriptor=context['source_descriptor'],
                              model={'reviewer_type': 'synthetic-reviewer', 'model': 'fixture',
                                     'config_sha256': digest('safe-config'),
                                     'recognizer_sha256': digest('fake-asr')}, ranges=ranges)
    assert evidence['source_audio'] is None
    assert evidence['members'][0]['clip'] is None
    assert store.finish_pending_presentation(item, ownership,
        {'ownership': ownership, 'status': 'prepared', 'code': None,
         'pending': pending, 'evidence': evidence}) == 'waiting_user'
    current = store.presentation_context(item)
    assert ready(current['pending'], root, source_descriptor=current['source_descriptor'])
    assert prepared_audio(current['pending'], root, pending['concerns'][0]['concern_uid'],
                          source_descriptor=current['source_descriptor']) is None


def test_non_audio_image_and_no_unresolved_keep_legacy_resolve(tmp_path):
    store, item, root = _setup(tmp_path, source_kind='direct_text')
    with connect(store.path) as db:
        db.execute("UPDATE submitted_sources SET input_kind='direct_text' WHERE item_id=?", (item,))
    for pending in ({'snapshot': 'text', 'concerns': [], 'review_required': True},
                    {'kind': 'image', 'snapshot': '图片', 'concerns': [{'text': '图片'}]},
                    {'snapshot': '正文', 'concerns': [{'text': '正文', 'candidates': ['正文']}]}):
        store.mark_waiting(item, pending)
        assert store.discover_pending_presentations()['enqueued'] == ()
        raw = store.item_bundle(item)['confirmation_json']
        assert store.resolve_confirmation(item, raw, unable=True) == 'failed'


def test_valid_proof_allows_exact_fact_once_and_guard_stays_immutable(tmp_path):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    store.finish_pending_presentation(item, ownership, _success(store, item, ownership, context))
    raw = store.item_bundle(item)['confirmation_json']
    assert store.resolve_confirmation(item, raw, fact=SourceFact('Ship it now')) == 'queued'
    fact = store.item_bundle(item)['source_fact_id']
    assert fact is not None
    assert store.discover_pending_presentations()['enqueued'] == ()
    with pytest.raises(ValueError):
        store.resolve_confirmation(item, raw, fact=SourceFact('another result'))
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM source_facts').fetchone()[0] == 1
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE source_facts SET snapshot='overwrite' WHERE source_fact_id=?", (fact,))


def test_legitimate_saved_decision_invalidates_remaining_cards_without_losing_it(tmp_path):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    store.finish_pending_presentation(item, ownership, _success(store, item, ownership, context))
    row = store.item_bundle(item)
    pending = store.confirmation_view(item)
    pending['resolved'].append({'action': 'manual', 'value': 'saved judgment'})
    assert store.resolve_confirmation(item, row['confirmation_json'], next_confirmation=pending) == 'waiting_user'
    current = _context(store, item)
    assert not ready(current['pending'], root, source_descriptor=current['source_descriptor'])
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert store.confirmation_view(item)['resolved'] == pending['resolved']


def test_snapshot_length_change_shifts_remaining_span_and_preserves_original_review(tmp_path):
    store, item, root = _setup(tmp_path, waiting=False)
    pending = _pending()
    pending['concerns'].append({'start': 8, 'end': 11, 'text': 'now',
        'reason': 'uncertain time', 'audio_name': 'concern-2.wav',
        'candidates': ['now', 'later']})
    revision = store.item_bundle(item)['review_revision']
    review = (revision, 'source-review', {'failure': None, 'schema': 1,
                                        'original_snapshot': 'Ship it now'})
    context = _context(store, item, pending=pending, new_review=review)
    pending, evidence = _prepare(context)
    pending['presentation_preparation'] = {'protocol': PROTOCOL,
        'input_binding': evidence['input_binding'], 'attempt': 1,
        'outcome': 'prepared', 'evidence': evidence}
    store.commit_source_review(item, revision, review[1], review[2], confirmation=pending)
    before = store.confirmation_view(item)
    committed = store.presentation_context(item)
    assert committed['source_descriptor'] == context['source_descriptor']
    assert committed['source_descriptor']['review_revision'] == revision
    assert output_sha256(before) == output_sha256(pending)
    assert before['concern_total'] == context['pending']['concern_total'] == 2
    assert ready(before, root, source_descriptor=committed['source_descriptor'])
    row = store.item_bundle(item)
    original_wav = (root / 'audio/standard.wav').read_bytes()
    original_clip = (root / 'confirmation/concern-2.wav').read_bytes()
    with connect(store.path) as db:
        original_review = tuple(db.execute('SELECT * FROM source_review_results WHERE item_id=?',
                                          (item,)).fetchone())
    # Exact _resolve_member snapshot/delta shape; Store must not guess the
    # remaining sentence boundary, refresh model output or rebuild decisions.
    updated = deepcopy(before)
    first, remaining = updated['concerns']
    replacement = 'Deliver'
    delta = len(replacement) - (first['end'] - first['start'])
    updated['snapshot'] = (updated['snapshot'][:first['start']] + replacement
                           + updated['snapshot'][first['end']:])
    remaining['start'] += delta
    remaining['end'] += delta
    updated['concerns'] = [remaining]
    decision = {'action': 'manual', 'concern_uid': first['concern_uid'],
                'original_text': first['text'], 'value': replacement}
    updated['resolved'].append(decision)
    assert store.resolve_confirmation(item, row['confirmation_json'],
                                      next_confirmation=updated) == 'waiting_user'
    current = store.presentation_context(item)
    saved = current['pending']
    assert saved['snapshot'] == 'Deliver it now'
    assert (saved['concerns'][0]['start'], saved['concerns'][0]['end']) == (11, 14)
    assert saved['concerns'][0]['sentence_span'] == before['concerns'][1]['sentence_span']
    assert saved['concerns'][0]['concern_uid'] == before['concerns'][1]['concern_uid']
    assert saved['resolved'][-1] == decision
    assert saved['original_review_hash'] == before['original_review_hash']
    assert saved['source_version_id'] == before['source_version_id']
    with pytest.raises(PreparationError):
        validate_evidence(saved, evidence, root, source_descriptor=current['source_descriptor'])
    assert not ready(saved, root, source_descriptor=current['source_descriptor'])
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert store.confirmation_view(item)['resolved'][-1] == decision
    assert (root / 'audio/standard.wav').read_bytes() == original_wav
    assert (root / 'confirmation/concern-2.wav').read_bytes() == original_clip
    with connect(store.path) as db:
        assert tuple(db.execute('SELECT * FROM source_review_results WHERE item_id=?',
                                (item,)).fetchone()) == original_review


def test_current_source_change_before_claim_stops_old_bound_attempt(tmp_path):
    store, item, root = _setup(tmp_path)
    store.discover_pending_presentations()
    with connect(store.path) as db:
        db.execute("UPDATE materials SET metadata_json='{}' WHERE material_id=?", (store.item_bundle(item)['material_id'],))
    assert store.claim_next_work() is None
    assert store.item_bundle(item)['state'] == 'failed'
    assert store.item_bundle(item)['error_code'] == 'review_incomplete'


def test_bounded_discovery_keeps_one_fifo_and_pages_current_pending(tmp_path):
    store, first, root = _setup(tmp_path)
    material = store.item_bundle(first)['material_id']
    items = [first]
    for n in range(9):
        item = store.create_item(f'synthetic://audio/{n}')
        with connect(store.path) as db:
            db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (material, item))
        store.mark_waiting(item, _pending())
        items.append(item)
    page = store.discover_pending_presentations(limit=3)
    assert page['enqueued'] == tuple(items[:3])
    following = store.discover_pending_presentations(after_item_id=page['after_item_id'], limit=64)
    assert following['enqueued'] == tuple(items[3:])
    for item in items:
        assert store.claim_next_work() == ('presentation', item)
    assert store.claim_next_work() is None


def test_collection_member_is_claimed_for_preparation_without_collection_run(tmp_path):
    store, item, root = _setup(tmp_path)
    with connect(store.path) as db:
        # Fixture only: an already frozen waiting operation/member. No remote
        # collection adapter, authority lookup, or business collection retry.
        stamp = store.item_bundle(item)['created_at']
        cursor = db.execute('''INSERT INTO collection_operations
            (kind,source_key,title,manifest_json,signature,content_signature,authority_json,
             confirmation_token,state,queued_at,created_at,updated_at)
            VALUES ('same_topic','fixture','fixture','{}','sig','content','{}','token',
                    'waiting_user',?,?,?)''', (stamp, stamp, stamp))
        operation = cursor.lastrowid
        db.execute('INSERT INTO collection_members VALUES (?,?,?,?,?,?,?,?)',
                   (operation, 1, '123', 'fixture-version', item, 0, None, None))
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert store.claim_next_work() == ('presentation', item)
    with connect(store.path) as db:
        assert db.execute('SELECT state FROM collection_operations').fetchone()[0] == 'waiting_user'


def test_only_current_version_requeue_preserves_ordinary_queue_semantics(tmp_path):
    store, item, root = _setup(tmp_path)
    normal = store.create_item('synthetic://ordinary')
    store.mark_working(normal, 'collecting')
    store.requeue_interrupted()
    assert store.item_bundle(normal)['state'] == 'queued'
    assert store.item_bundle(item)['state'] == 'waiting_user'
    assert store.claim_next_work() == ('item', normal)


def test_manifest_and_boolean_typed_fields_are_not_proof(tmp_path):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    result = _success(store, item, ownership, context)
    result['evidence']['members'][0]['clip']['channels'] = True
    result['evidence']['manifest_sha256'] = manifest_sha256(result['evidence'])
    with pytest.raises(PreparationError):
        store.finish_pending_presentation(item, ownership, result)
    pending = context['pending']
    pending['presentation_preparation'] = {'protocol': PROTOCOL, 'input_binding': ownership['input_binding'],
                                         'attempt': 1, 'outcome': 'prepared', 'evidence': {'ready': True}}
    assert not ready(pending, root, source_descriptor=context['source_descriptor'])


def test_original_ffmpeg_preview_bytes_satisfy_read_only_verifier(tmp_path):
    """Synthetic PCM interoperability; requires the already installed ffmpeg."""
    import shutil
    from knowledge_distiller.faithful_review import ReviewConcern
    from knowledge_distiller.primary import StandardAudio, PrimaryChunk, PrimaryRecovery
    from knowledge_distiller.v1.confirmation import FFmpegConfirmationClipper, locate_concern_audio
    assert shutil.which('ffmpeg'), 'test runner must expose existing ffmpeg in PATH'
    store, item, root = _setup(tmp_path)
    _wav(root / 'audio/standard.wav', frames=15 * 16000)
    pending = _pending()
    pending['snapshot'] = pending['audio_timeline']['text'] = 'Go Ship it now'
    pending['audio_timeline'].update(duration_seconds=15.0, chunks=[
        {'text': 'Go Ship it now', 'start_seconds': 0.0, 'end_seconds': 15.0}])
    pending['concerns'][0].update(start=3, end=7)
    store.mark_waiting(item, pending)
    context = _context(store, item)
    pending = context['pending']
    concern = pending['concerns'][0]
    audio = StandardAudio(root / 'audio/standard.wav', 15.0)
    recovery = PrimaryRecovery('Go Ship it now', None,
                               (PrimaryChunk('Go Ship it now', 0.0, 15.0),), timeline_status='available')
    issue = ReviewConcern(3, 7, 'Ship', 'uncertain word', True)
    span = list(locate_concern_audio(audio, recovery, pending['snapshot'], issue))
    FFmpegConfirmationClipper().clip(audio, recovery, pending['snapshot'], issue,
                                   root / 'confirmation/concern-1.wav')
    concern.update(audio_file='concern-1.wav', audio_recovery_required=False,
                   sentence_span={'start': 0, 'end': len(pending['snapshot'])},
                   candidate_translations={'Ship': '去，现在发货。', 'Sheep': '去，羊，现在。'},
                   candidate_basis={'Ship': '此处是指令句，Ship可作为动作词与it搭配。',
                                    'Sheep': 'Sheep是名词，不能自然充当it之前的指令动词。'})
    pending['audio_alignment'] = 'asr_chunk_v2'
    context = _context(store, item, pending=pending)
    evidence = build_evidence(context['pending'], root, source_descriptor=context['source_descriptor'],
        model={'reviewer_type': 'synthetic', 'model': None, 'config_sha256': digest('config'),
               'recognizer_sha256': digest('recognizer')},
        ranges={concern['concern_uid']: span})
    assert validate_evidence(context['pending'], evidence, root,
                             source_descriptor=context['source_descriptor'])


def test_persisted_failed_version_is_not_rescanned_or_auto_retried(tmp_path):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    store.finish_pending_presentation(item, ownership, {'status': 'failed', 'code': 'review_incomplete',
        'ownership': ownership, 'pending': None, 'evidence': None})
    # A legal return to waiting of the same failed version must not start a
    # model round implicitly. Discovery reads its durable failure marker.
    pending = store.confirmation_view(item)
    store.mark_waiting(item, pending)
    assert store.discover_pending_presentations()['enqueued'] == ()
    assert store.claim_next_work() is None
    assert store.confirmation_view(item)['presentation_preparation']['attempt'] == 1


def test_finish_database_failure_rolls_back_ready_and_preserves_owned_pending(tmp_path):
    import sqlite3
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    result = _success(store, item, ownership, context)
    with connect(store.path) as db:
        db.execute("""CREATE TRIGGER synthetic_finish_failure BEFORE UPDATE ON distill_items
            WHEN NEW.state='waiting_user' AND OLD.state='working'
            BEGIN SELECT RAISE(ABORT,'synthetic_finish_disk_failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic_finish_disk_failure'):
        store.finish_pending_presentation(item, ownership, result)
    assert store.presentation_ownership(item) == ownership
    assert store.item_bundle(item)['confirmation_json'] == ownership['expected_confirmation_json']


def test_source_fact_or_finished_history_never_enters_discovery(tmp_path):
    store, item, root = _setup(tmp_path)
    store.establish_source_fact(store.item_bundle(item)['material_id'], SourceFact('already human completed'))
    assert store.discover_pending_presentations()['enqueued'] == ()
    assert store.item_bundle(item)['state'] == 'waiting_user'
    store.mark_succeeded(item)
    assert store.discover_pending_presentations()['enqueued'] == ()


def test_first_slot_is_not_enough_to_certify_the_full_pending(tmp_path):
    store, item, root = _setup(tmp_path)
    pending = _pending()
    pending['concerns'].append({'start': 5, 'end': 7, 'text': 'it', 'reason': 'second issue',
                               'audio_name': 'concern-2.wav', 'candidates': ['it', 'eat']})
    store.mark_waiting(item, pending)
    ownership, context = _claimed(store, item)
    result = _success(store, item, ownership, context)
    result['evidence']['members'].pop()
    result['evidence']['manifest_sha256'] = manifest_sha256(result['evidence'])
    with pytest.raises(PreparationError):
        store.finish_pending_presentation(item, ownership, result)
    assert store.item_bundle(item)['state'] == 'working'


@pytest.mark.parametrize('defect', ['missing_translations', 'missing_basis', 'reason_alias',
                                   'missing_candidate', 'span_outside', 'span_not_covering', 'old_only'])
def test_sentence_translations_and_independent_basis_are_mandatory(tmp_path, defect):
    store, item, root = _setup(tmp_path)
    ownership, context = _claimed(store, item)
    result = _success(store, item, ownership, context)
    c = result['pending']['concerns'][0]
    if defect == 'missing_translations':
        c.pop('candidate_translations')
    elif defect == 'missing_basis':
        c.pop('candidate_basis')
    elif defect == 'reason_alias':
        c['candidate_basis']['Ship'] = c['reason']
    elif defect == 'missing_candidate':
        c['candidate_translations'].pop('Sheep')
    elif defect == 'span_outside':
        c['sentence_span']['end'] += 1
    elif defect == 'span_not_covering':
        c['sentence_span']['start'] = 1
    else:
        c.pop('candidate_translations')
        c.pop('candidate_basis')
    # Even a caller rehashing its own manifest cannot turn mixed explanations
    # or the original review reason into this new prepared sentence contract.
    result['evidence']['output_sha256'] = output_sha256(result['pending'])
    result['evidence']['manifest_sha256'] = manifest_sha256(result['evidence'])
    with pytest.raises(PreparationError, match='review_incomplete'):
        store.finish_pending_presentation(item, ownership, result)
    assert store.item_bundle(item)['confirmation_json'] == ownership['expected_confirmation_json']
