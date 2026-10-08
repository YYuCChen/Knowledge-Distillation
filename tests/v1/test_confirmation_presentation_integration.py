"""Disposable DB/PCM and real Web/worker lifecycle; no provider or network."""
from copy import deepcopy
import json
import struct
import threading
import time
import wave

import pytest

from knowledge_distiller.v1.confirmation_preparation import (
    PreparationError, build_evidence, digest, ready,
)
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.domain import CapturedMaterial
from knowledge_distiller.v1.pipeline import DistillResult
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app, _home_context, _ready_presentation_context
from knowledge_distiller.v1.worker import SingleWorker
from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate


def _pending():
    return {'review_identity': 'synthetic-review', 'snapshot': 'Ship it now',
        'review_required': True, 'resolved': [], 'deferred_concerns': [],
        'lineage': {'source': 'original'},
        'audio_timeline': {'text': 'Ship it now', 'duration_seconds': 2.0,
            'timeline_status': 'available', 'chunks': [
                {'text': 'Ship it now', 'start_seconds': 0.0, 'end_seconds': 2.0}]},
        'concerns': [
            {'start': 0, 'end': 4, 'text': 'Ship', 'reason': 'uncertain action',
             'audio_name': 'concern-1.wav', 'candidates': ['Ship', 'Sheep']},
            {'start': 8, 'end': 11, 'text': 'now', 'reason': 'uncertain time',
             'audio_name': 'concern-2.wav', 'candidates': ['now', 'later']}]}


def _item(store, root, key='123'):
    item = store.create_item('https://www.douyin.com/video/' + key)
    media = root / ('fixture-' + key + '.mp4')
    media.write_bytes(b'synthetic-owned-material')
    store.attach_material(item, CapturedMaterial('douyin', key, 'fixture', 'fixture',
                          {'source_title': 'synthetic source'}, media, 2))
    store.mark_waiting(item, _pending())
    path = store.preparation_runtime_root / 'items' / str(item) / 'audio/standard.wav'
    path.parent.mkdir(parents=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b''.join(struct.pack('<h', n % 30000) for n in range(32000)))
    return item


class SyntheticPreparer:
    """Controlled model output; real shared binding/readback, not fake ready."""
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.resolutions = []
        self.entered = threading.Event()
        self.release = None
        self.failure = None
        self.raise_error = None
        self.supersede = False

    def prepare_pending_presentation(self, item):
        owner = self.store.presentation_ownership(item)
        assert self.store.item_bundle(item)['state'] == 'working'
        self.calls.append((item, owner))
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(3), 'test must release the bounded fake model'
        if self.supersede:
            # Simulate a legitimate intervening lifecycle completion followed
            # by the existing user dismiss API; working cannot be dismissed.
            self.store.finish_pending_presentation(item, owner, {
                'ownership': owner, 'status': 'failed', 'code': 'review_incomplete',
                'pending': None, 'evidence': None})
            self.store.dismiss_item(item)
            raise PreparationError('review_incomplete')
        if self.raise_error:
            raise self.raise_error
        if self.failure:
            return {'ownership': owner, 'status': 'failed', 'code': self.failure,
                    'pending': None, 'evidence': None}
        context = self.store.presentation_context(item)
        pending = deepcopy(context['pending'])
        directory = context['item_runtime_root']
        original = (directory / 'audio/standard.wav').read_bytes()
        (directory / 'confirmation').mkdir(exist_ok=True)
        for ordinal, concern in enumerate(pending['concerns'], 1):
            filename = f'concern-{ordinal}.wav'
            (directory / 'confirmation' / filename).write_bytes(original)
            concern.update(audio_file=filename, audio_recovery_required=False,
                sentence_span={'start': 0, 'end': len(pending['snapshot'])},
                candidate_translations={v: '替换后的整句中文释义：' + v for v in concern['candidates']},
                candidate_basis={v: '当前完整句式中的独立上下文依据：' + v for v in concern['candidates']})
        pending['audio_alignment'] = 'asr_chunk_v2'
        final = self.store.presentation_context(item, pending)
        evidence = build_evidence(final['pending'], directory,
            source_descriptor=final['source_descriptor'],
            model={'reviewer_type': 'synthetic', 'model': 'fake-model',
                   'config_sha256': digest('nonsecret'), 'recognizer_sha256': digest('synthetic-asr')},
            ranges={c['concern_uid']: [0.0, 2.0] for c in final['pending']['concerns']})
        return {'ownership': owner, 'status': 'prepared', 'code': None,
                'pending': final['pending'], 'evidence': evidence}

    def resolve(self, item, action, value, **kwargs):
        self.resolutions.append((item, action, value))
        row = self.store.item_bundle(item)
        pending = self.store.confirmation_view(item)
        pending['resolved'].append({'action': action, 'value': value})
        state = self.store.resolve_confirmation(item, row['confirmation_json'], next_confirmation=pending)
        return DistillResult(item, state)

    def resolve_group(self, item, action, value, **kwargs):
        return self.resolve(item, action, value, **kwargs)

    def run(self, _item):
        raise AssertionError('presentation must never rerun ordinary source/ASR/derive')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / 'synthetic.sqlite3', runtime_root=tmp_path / 'explicit-runtime')
    store.initialize()
    preparer = SyntheticPreparer(store)
    factory_calls = []
    def factory():
        factory_calls.append('factory')
        return preparer
    def forbidden(*_args, **_kwargs):
        raise AssertionError('GET must not construct client, read secret or use network')
    from knowledge_distiller.v1.settings import SettingsService
    monkeypatch.setattr(SettingsService, 'llm_client', forbidden)
    monkeypatch.setattr(SettingsService, 'doubao_secret', forbidden)
    import socket
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    gate = WorkAdmissionGate()
    app = create_app(store, factory, admission_gate=gate)
    app.config.update(TESTING=True)
    item = _item(store, tmp_path)
    worker = SingleWorker(store, factory, idle_seconds=.01,
                          maintenance=lambda: store.discover_pending_presentations(limit=8))
    yield app.test_client(), store, preparer, factory_calls, worker, item, gate
    assert worker.stop(3)


def _prepare(store, worker, item):
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert worker.run_one() == item
    assert store.item_bundle(item)['state'] == 'waiting_user'


def test_old_bad_card_first_frame_hidden_without_get_writes_or_model(setup):
    client, store, producer, calls, worker, item, gate = setup
    before = store.item_bundle(item)['confirmation_json']
    assert client.get(f'/?item={item}').status_code == 200
    assert '/confirm-group' not in client.get('/').text
    assert 'data-confirmation-card=' not in client.get('/').text
    assert _home_context(store, item)['todo'] == ()
    assert _home_context(store, item)['selected']['state'] == 'waiting_user'
    assert store.item_bundle(item)['state'] == 'waiting_user'
    assert store.item_bundle(item)['confirmation_json'] == before
    assert client.get(f'/items/{item}/confirmation-audio?concern_id=concern-1.wav').status_code == 404
    assert client.get(f'/items/{item}/confirmation-context/concern-1.wav').status_code == 404
    assert calls == producer.calls == []


def test_whole_pending_is_verified_before_first_group_member_projection(setup):
    client, store, producer, calls, worker, item, gate = setup
    _prepare(store, worker, item)
    queue = store.manual_cards()
    before = [(c['group_id'], c['enqueue_seq']) for c in queue]
    context = _home_context(store, item)
    assert context['todo']
    assert [(c['group']['group_id'], c['enqueue_seq']) for c in context['todo']] == before
    for card in context['todo']:
        assert len(card['confirmation']['concerns']) == 1
        assert card['confirmation']['concerns'][0]['display']['sentence'] == 'Ship it now'
    factory_count = len(calls)
    clip = store.preparation_runtime_root / 'items' / str(item) / 'confirmation/concern-2.wav'
    clip.write_bytes(b'damaged hidden second member')
    assert _home_context(store, item)['todo'] == ()
    assert 'data-confirmation-card=' not in client.get('/').text
    assert store.item_bundle(item)['state'] == 'waiting_user'
    assert len(calls) == factory_count and len(producer.calls) == 1


def test_verified_audio_retains_range_seek_actual_slice_and_no_factory(setup):
    client, store, producer, calls, worker, item, gate = setup
    _prepare(store, worker, item)
    expected = (store.preparation_runtime_root / 'items' / str(item) / 'confirmation/concern-1.wav').read_bytes()
    count = len(calls)
    response = client.get(f'/items/{item}/confirmation-audio?concern_id=concern-1.wav',
                          headers={'Range': 'bytes=44-75'})
    assert response.status_code == 206
    assert response.headers['Content-Range'] == f'bytes 44-75/{len(expected)}'
    assert response.data == expected[44:76]
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.get(f'/items/{item}/confirmation-audio').status_code == 404
    assert client.get(f'/items/{item}/confirmation-audio?concern_id=not-a-member').status_code == 404
    assert client.get(f'/items/{item}/confirmation-audio?concern_id=../audio/standard.wav').status_code == 404
    assert len(calls) == count and len(producer.calls) == 1
    (store.preparation_runtime_root / 'items' / str(item) / 'confirmation/concern-2.wav').unlink()
    assert client.get(f'/items/{item}/confirmation-audio?concern_id=concern-1.wav',
                      headers={'Range': 'bytes=44-75'}).status_code == 404
    assert len(calls) == count and len(producer.calls) == 1


def test_single_verified_audio_legacy_url_is_scoped_and_read_only(setup):
    client, store, producer, calls, worker, item, gate = setup
    pending = _pending()
    pending['concerns'] = pending['concerns'][:1]
    store.mark_waiting(item, pending)
    assert client.get(f'/items/{item}/confirmation-audio').status_code == 404
    assert calls == producer.calls == []
    _prepare(store, worker, item)
    current = store.presentation_context(item)
    assert len(current['pending']['concerns']) == 1
    assert ready(current['pending'], current['item_runtime_root'],
                 source_descriptor=current['source_descriptor'])
    member = current['pending']['concerns'][0]
    expected = (current['item_runtime_root'] / 'confirmation' / member['audio_file']).read_bytes()
    saved = store.item_bundle(item)['confirmation_json']
    count = len(calls)
    for suffix in ('', '?concern_id=' + member['concern_uid'],
                   '?concern_id=' + member['audio_name']):
        response = client.get(f'/items/{item}/confirmation-audio{suffix}')
        assert response.status_code == 200 and response.data == expected
        assert response.headers['Cache-Control'] == 'no-store'
    response = client.get(f'/items/{item}/confirmation-audio', headers={'Range': 'bytes=44-75'})
    assert response.status_code == 206 and response.data == expected[44:76]
    assert response.headers['Content-Range'] == f'bytes 44-75/{len(expected)}'
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.get(f'/items/{item + 1}/confirmation-audio').status_code == 404
    assert client.get(f'/items/{item}/confirmation-audio?concern_id=not-a-member').status_code == 404
    assert store.item_bundle(item)['confirmation_json'] == saved
    assert len(calls) == count and len(producer.calls) == 1
    (current['item_runtime_root'] / 'confirmation' / member['audio_file']).write_bytes(b'damaged clip')
    assert client.get(f'/items/{item}/confirmation-audio').status_code == 404
    assert len(calls) == count and len(producer.calls) == 1


@pytest.mark.parametrize('endpoint', ['confirm', 'confirm-group'])
def test_post_gate_checks_real_current_proof_before_factory(setup, endpoint):
    client, store, producer, calls, worker, item, gate = setup
    assert client.post(f'/items/{item}/{endpoint}', data={'action': 'manual', 'value': 'saved'}).status_code == 409
    assert calls == []
    _prepare(store, worker, item)
    assert client.post(f'/items/{item}/{endpoint}', data={'action': 'manual', 'value': 'saved'}).status_code == 302
    assert producer.resolutions == [(item, 'manual', 'saved')]
    assert store.confirmation_view(item)['resolved'][-1]['value'] == 'saved'
    count = len(calls)
    assert client.post(f'/items/{item}/{endpoint}', data={'action': 'manual', 'value': 'stale'}).status_code == 409
    assert len(calls) == count


def test_failed_preparation_uses_real_generic_retry_without_losing_concerns(setup):
    client, store, producer, calls, worker, item, gate = setup
    producer.failure = 'llm_request_timeout'
    original = store.confirmation_view(item)
    store.discover_pending_presentations()
    assert worker.run_one() == item
    row = store.item_bundle(item)
    assert row['state'] == 'failed' and row['error_code'] == 'llm_request_timeout'
    assert store.confirmation_view(item)['concerns'] == original['concerns']
    assert '/retry' in client.get('/').text
    assert client.post(f'/items/{item}/retry').status_code == 302
    assert store.item_bundle(item)['state'] == 'queued'
    assert store.confirmation_view(item)['presentation_preparation']['attempt'] == 2
    producer.failure = None
    assert worker.run_one() == item
    assert _ready_presentation_context(store, item) is not None
    assert len(producer.calls) == 2


@pytest.mark.parametrize('failure', ['processing_unexpected_failure', 'llm_request_failed', 'llm_request_timeout'])
def test_worker_factory_exception_finishes_same_owner_without_generic_mark_failed(setup, monkeypatch, failure):
    client, store, producer, calls, worker, item, gate = setup
    store.discover_pending_presentations()
    def unexpected():
        if failure != 'processing_unexpected_failure':
            from knowledge_distiller.v1.llm import LLMRequestError
            raise LLMRequestError(failure)
        raise RuntimeError('external secret/body must not become DB reason')
    def forbidden(*_args, **_kwargs):
        raise AssertionError('generic mark_failed would lose preparation ownership')
    worker.distiller = unexpected
    monkeypatch.setattr(store, 'mark_failed', forbidden)
    assert worker.run_one() == item
    assert store.item_bundle(item)['state'] == 'failed'
    assert store.item_bundle(item)['error_code'] == failure
    assert 'external secret' not in store.item_bundle(item)['confirmation_json']


def test_superseded_model_failure_cannot_overwrite_dismissed_state(setup):
    client, store, producer, calls, worker, item, gate = setup
    producer.supersede = True
    store.discover_pending_presentations()
    assert worker.run_one() == item
    row = store.item_bundle(item)
    assert row['dismissed_at'] is not None
    assert row['error_code'] == 'review_incomplete'


def test_real_worker_activity_blocks_update_during_model_and_stop_does_not_claim_next(setup, tmp_path):
    client, store, producer, calls, worker, item, gate = setup
    producer.release = threading.Event()
    second = _item(store, tmp_path, '456')
    worker.start()
    try:
        assert producer.entered.wait(3)
        assert worker.reserve_for_update() is False
        assert worker.update_ready() is False
        assert worker.stop(.01) is False
        producer.release.set()
        assert worker.stop(3) is True
        assert store.item_bundle(item)['state'] == 'waiting_user'
        assert store.item_bundle(second)['state'] == 'queued'
        assert len(producer.calls) == 1
    finally:
        producer.release.set()


def test_update_reservation_prevents_maintenance_claim_and_restart(setup):
    client, store, producer, calls, worker, item, gate = setup
    # An old waiting card is not durable queued work until maintenance runs.
    assert worker.reserve_for_update() is True
    assert worker.start() is False
    assert worker.run_one() is None
    assert store.item_bundle(item)['state'] == 'waiting_user'
    assert gate.reserve() is True
    try:
        assert client.post(f'/items/{item}/confirm', data={'action': 'manual', 'value': 'x'}).status_code == 503
        assert client.get('/').status_code == 200
        assert calls == producer.calls == []
    finally:
        gate.release_reservation()
        worker.release_update()


def test_reboot_running_attempt_fails_once_and_does_not_auto_prepare(setup):
    client, store, producer, calls, worker, item, gate = setup
    store.discover_pending_presentations()
    assert store.claim_next_work() == ('presentation', item)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while store.item_bundle(item)['state'] != 'failed' and time.monotonic() < deadline:
            time.sleep(.01)
        assert store.item_bundle(item)['state'] == 'failed'
        assert store.item_bundle(item)['error_code'] == 'review_incomplete'
        worker.wake()
        assert store.discover_pending_presentations()['enqueued'] == ()
        assert producer.calls == calls == []
    finally:
        assert worker.stop(3)


def test_collection_member_uses_same_complete_gate_and_audio_endpoint(setup):
    client, store, producer, calls, worker, item, gate = setup
    with connect(store.path) as db:
        stamp = store.item_bundle(item)['created_at']
        manifest = {'members': [{'item_id': '123', 'title': 'fixture title', 'url': 'synthetic://123'}]}
        operation = db.execute('''INSERT INTO collection_operations
            (kind,source_key,title,manifest_json,signature,content_signature,authority_json,
             confirmation_token,state,queued_at,created_at,updated_at)
            VALUES ('same_topic','fixture','fixture',?,'sig','content','{}','token',
                    'waiting_user',?,?,?)''', (json.dumps(manifest), stamp, stamp, stamp)).lastrowid
        db.execute('INSERT INTO collection_members VALUES (?,?,?,?,?,?,?,?)',
                   (operation, 1, '123', 'v1', item, 0, None, None))
    assert client.get(f'/collections/{operation}').status_code == 200
    assert not _home_context(store, item)['todo']
    _prepare(store, worker, item)
    assert _home_context(store, item)['todo']
    assert client.get(f'/collections/{operation}').status_code == 200
    (store.preparation_runtime_root / 'items' / str(item) / 'confirmation/concern-2.wav').unlink()
    assert not _home_context(store, item)['todo']
    from knowledge_distiller.v1.collection_web import _presentation_members
    from knowledge_distiller.v1.collections import Collections
    member = _presentation_members(Collections(store).detail(operation), store)['members'][0]
    assert member['state'] == store.item_bundle(item)['state'] == 'waiting_user'
    assert member['confirmation_json'] is None
    assert client.get(f'/items/{item}/confirmation-audio?concern_id=concern-1.wav').status_code == 404


def test_application_uses_selected_runtime_and_one_bounded_discovery_page(tmp_path, monkeypatch):
    from knowledge_distiller.v1.app import AppPaths, create_application
    from knowledge_distiller.v1.raw import RawLedger
    from knowledge_distiller.v1.captures import Captures
    from knowledge_distiller.v1.temporary_artifacts import TemporaryArtifacts
    from knowledge_distiller.v1.settings import SettingsService
    def forbidden(*_args, **_kwargs):
        raise AssertionError('maintenance discovery must not prepare models or read credentials')
    monkeypatch.setattr(SettingsService, 'llm_client', forbidden)
    monkeypatch.setattr(SettingsService, 'jev_client', forbidden)
    monkeypatch.setattr(SettingsService, 'doubao_secret', forbidden)
    import socket
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(RawLedger, 'write_pending', lambda _self: None)
    monkeypatch.setattr(Captures, 'write_ready', lambda _self: None)
    monkeypatch.setattr(TemporaryArtifacts, 'sweep', lambda _self: None)
    class Chrome:
        def verify(self):
            raise AssertionError('no browser')
        def cookies(self):
            raise AssertionError('no credentials')
    app = create_application(AppPaths(tmp_path / 'selected'), chrome=Chrome(), start_workers=False)
    store = app.config['KNOWLEDGE_DISTILLER_STORE']
    worker = app.config['KNOWLEDGE_DISTILLER_WORKER']
    assert store.preparation_runtime_root == AppPaths(tmp_path / 'selected').runtime
    items = [_item(store, tmp_path, str(n)) for n in range(10)]
    # Inspect the actual application's maintenance callback under its original
    # activity lock; no worker/model/ASR thread is started by this fixture.
    try:
        with worker._activity:
            assert not worker._update_reserved
            worker.maintenance()
        assert [store.item_bundle(i)['state'] for i in items] == ['queued'] * 8 + ['waiting_user'] * 2
        with worker._activity:
            worker.maintenance()
        assert [store.item_bundle(i)['state'] for i in items] == ['queued'] * 10
    finally:
        assert app.config['KNOWLEDGE_DISTILLER_WORKERS'].stop()
        app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()


def test_real_distiller_producer_worker_finish_and_get_share_actual_proof(setup):
    """Use Harvey's actual producer, with only model and clipper faked."""
    from knowledge_distiller.v1.pipeline import Distiller
    from knowledge_distiller.v1.confirmation import locate_concern_audio
    client, store, producer, calls, worker, item, gate = setup
    class Reviewer:
        def __init__(self):
            self.calls = 0
        def prepare_candidate_assistance(self, snapshot, concerns):
            self.calls += 1
            return {c['audio_name']: {
                'sentence_span': {'start': 0, 'end': len(snapshot)},
                'candidate_translations': {v: '当前替换后的整句中文：' +
                    snapshot[:c['start']] + v + snapshot[c['end']:] for v in c['candidates']},
                'candidate_basis': {v: '独立上下文句式及限制依据：' + v for v in c['candidates']}}
                for c in concerns}
    class Clipper:
        def clip(self, audio, recovery, snapshot, issue, output):
            span = locate_concern_audio(audio, recovery, snapshot, issue)
            assert span is not None
            output.parent.mkdir(exist_ok=True)
            with wave.open(str(audio.path), 'rb') as source:
                source.setpos(round(round(span[0], 3) * source.getframerate()))
                pcm = source.readframes(round((span[1] - span[0]) * source.getframerate()))
                with wave.open(str(output), 'wb') as target:
                    target.setparams(source.getparams())
                    target.writeframes(pcm)
            return output
    reviewer = Reviewer()
    actual = Distiller(store=store, source=None, normalizer=None, recognizer=None,
        reviewer=reviewer, confirmation_clipper=Clipper(), knowledge_model=None,
        runtime_root=store.preparation_runtime_root, vault=None, ocr=object())
    worker.distiller = actual
    _prepare(store, worker, item)
    assert reviewer.calls == 1
    context = store.presentation_context(item)
    assert ready(context['pending'], context['item_runtime_root'],
                 source_descriptor=context['source_descriptor'])
    uid = context['pending']['concerns'][0]['concern_uid']
    clip = context['item_runtime_root'] / 'confirmation' / context['pending']['concerns'][0]['audio_file']
    expected = clip.read_bytes()
    assert 'data-confirmation-card=' in client.get('/').text
    response = client.get(f'/items/{item}/confirmation-audio?concern_id={uid}',
                          headers={'Range': 'bytes=44-75'})
    assert response.status_code == 206
    assert response.headers['Content-Range'] == f'bytes 44-75/{len(expected)}'
    assert response.data == expected[44:76]
    assert reviewer.calls == 1 and calls == producer.calls == []
    assert store.item_bundle(item)['source_fact_id'] is None


@pytest.mark.parametrize('endpoint', ['confirm', 'confirm-group'])
@pytest.mark.parametrize('final_fact', [False, True])
def test_real_first_post_replays_ledger_when_remaining_queued_or_final_fact(setup, endpoint, final_fact):
    from knowledge_distiller.v1.pipeline import Distiller
    from knowledge_distiller.v1.confirmation_revision import revision
    client, store, producer, calls, worker, item, gate = setup
    pending = _pending()
    pending['review_required'] = False
    if final_fact:
        pending['concerns'] = pending['concerns'][:1]
    store.mark_waiting(item, pending)
    _prepare(store, worker, item)
    # Actual Distiller resolves and Store establishes the fact/decision ledger.
    # Its reviewer/recognizer/model are absent: judgment must not invoke them.
    service = Distiller(store=store, source=None, normalizer=None, recognizer=None,
        reviewer=None, confirmation_clipper=None, knowledge_model=None,
        runtime_root=store.preparation_runtime_root, vault=None, ocr=object())
    factory_calls = []
    def factory():
        factory_calls.append(item)
        return service
    app = create_app(store, factory, admission_gate=gate)
    app.config.update(TESTING=True)
    actual_client = app.test_client()
    shown = store.confirmation_view(item)
    concern = shown['concerns'][0]
    payload = {'token': shown['token'], 'action': 'candidate', 'value': 'Ship'}
    if endpoint == 'confirm-group':
        group = next(g for g in shown['groups'] if concern['concern_uid'] in g['member_uids'])
        payload.update(request_id='synthetic-request', group_id=group['group_id'],
            group_revision=group['group_revision'], selected_member_uids=[concern['concern_uid']])
    else:
        payload.update(concern_id=concern['audio_name'], concern_revision=revision(shown, concern))
    assert actual_client.post(f'/items/{item}/{endpoint}', data=payload).status_code == 302
    assert factory_calls == [item]
    def records():
        with connect(store.path) as db:
            return {
                'item': tuple(db.execute('SELECT * FROM distill_items WHERE item_id=?', (item,)).fetchone()),
                'fact': [tuple(r) for r in db.execute('SELECT * FROM source_facts')],
                'group_events': [tuple(r) for r in db.execute('SELECT * FROM group_decisions')],
                'member_events': [tuple(r) for r in db.execute('SELECT * FROM confirmation_decisions')],
            }
    if not final_fact:
        assert store.item_bundle(item)['state'] == 'waiting_user'
        current = store.presentation_context(item)
        assert not ready(current['pending'], current['item_runtime_root'],
            source_descriptor=current['source_descriptor'])
        assert _ready_presentation_context(store, item) is None
        partial_records = records()
        # A saved receipt also survives the not-ready interval before discovery
        # changes the remaining member's real state to queued.
        response = actual_client.post(f'/items/{item}/{endpoint}', data=payload)
        assert response.status_code == 302
        assert records() == partial_records
        assert factory_calls == [item]
        assert len(producer.calls) == 1
        assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert store.item_bundle(item)['state'] == 'queued'
    before = records()
    assert len(before['fact']) == int(final_fact)
    assert len(before['group_events']) == int(endpoint == 'confirm-group')
    assert len(before['member_events']) == int(endpoint == 'confirm')
    # The ready gate now deliberately rejects the queued/finished item. Only
    # the existing exact receipt permits this repeated POST, without factory.
    assert _ready_presentation_context(store, item) is None
    for _ in range(2):
        response = actual_client.post(f'/items/{item}/{endpoint}', data=payload)
        assert response.status_code == 302
        assert response.headers['Location'].endswith(f'/?item={item}')
        assert records() == before
    changed = {**payload, 'value': 'Sheep'}
    assert actual_client.post(f'/items/{item}/{endpoint}', data=changed).status_code == 409
    if endpoint == 'confirm-group':
        changed = {**payload, 'selected_member_uids': ['different-member']}
        assert actual_client.post(f'/items/{item}/{endpoint}', data=changed).status_code == 409
    assert records() == before
    assert factory_calls == [item]
    assert len(producer.calls) == 1
