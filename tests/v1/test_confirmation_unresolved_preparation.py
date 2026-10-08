"""Human unable replay on a clean 85f3b1b base, synthetic PCM and fake models.

Run against that committed base plus these two files, not unrelated dirty schema
work. No test imports the dirty test_pipeline/test_review_resume fixtures.
"""
from copy import deepcopy
import io
import json
import wave

import pytest

from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery
from knowledge_distiller.v1.confirmation_preparation import (
    PreparationError, input_binding, protected_pending, ready, read_wav,
    validate_change,
)
from knowledge_distiller.v1.confirmation_revision import revision
from knowledge_distiller.v1.domain import CapturedMaterial
from knowledge_distiller.v1.knowledge_model import KnowledgeModelError
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.worker import SingleWorker
from tests.v1.test_confirmation_obligation import Clipper, wav


MARKER = '[听辨不清]'
ORIGINAL = '持续切换会带来额外损耗。'


class NoModels:
    def recognize(self, *args, **kwargs):
        pytest.fail('Completed ASR must not run again')

    def review(self, *args, **kwargs):
        pytest.fail('Human decisions must not be replaced by another review')

    def prepare_candidate_assistance(self, *args, **kwargs):
        pytest.fail('This Chinese replay needs no English model assistance')


class InsufficientKnowledge:
    def __init__(self):
        self.snapshots = []

    def derive(self, snapshot, uncertainties):
        self.snapshots.append(snapshot)
        raise KnowledgeModelError('knowledge_not_qualified')


@pytest.fixture
def replay(tmp_path, monkeypatch):
    import httpx
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail('Synthetic replay cannot use HTTP or DNS')

    monkeypatch.setattr(httpx, 'Client', forbidden)
    monkeypatch.setattr(httpx, 'post', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite', runtime_root=root / 'runtime')
    store.initialize()
    item = store.create_item('https://v.douyin.com/synthetic/')
    container = root / 'owned-synthetic.mp4'
    container.write_bytes(b'synthetic container; only PCM is playback evidence')
    store.attach_material(item, CapturedMaterial('douyin', '123', 'fixture',
                                               'fixture', {}, container, 10))
    directory = root / 'runtime' / 'items' / str(item)
    wav(directory / 'audio' / 'standard.wav', seconds=10)
    recovery = PrimaryRecovery(ORIGINAL, 'zh', (PrimaryChunk(ORIGINAL, 0, 10, 'zh'),))
    pending = {'snapshot': ORIGINAL, 'review_identity': 'synthetic-unable-review',
        'review_required': False, 'uncertainties': [], 'resolved': [],
        'deferred_concerns': [], 'correction_locations': [],
        'lineage': {'original_asr': ORIGINAL},
        'concerns': [{'start': 0, 'end': 2, 'text': '持续', 'candidates': ['持续'],
            'reason': '原音待人工判断', 'audio_name': 'concern-1-' + 'a'*32 + '.wav'}],
        'audio_timeline': {'text': ORIGINAL, 'chunks': [vars(recovery.chunks[0])],
            'duration_seconds': 10, 'timeline_status': recovery.timeline_status}}
    store.mark_waiting(item, pending)
    model = InsufficientKnowledge()
    service = Distiller(store=store, source=None, normalizer=None,
        recognizer=NoModels(), reviewer=NoModels(), confirmation_clipper=Clipper(),
        knowledge_model=model, runtime_root=root / 'runtime', vault=None, ocr=object())
    worker = SingleWorker(store, service)
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert worker.run_one() == item
    context = store.presentation_context(item)
    assert ready(context['pending'], directory, source_descriptor=context['source_descriptor'])
    before = deepcopy(context['pending'])
    member = before['concerns'][0]
    decision_revision = revision(before, member)
    result = service.resolve(item, 'unable', token=before['token'],
        concern_id=member['audio_name'], concern_revision=decision_revision)
    assert result.state == 'queued'
    assert store.confirmation_decision(item, decision_revision, 'unable', '') == 'queued'
    # This actual FIFO processing takes _finish_partial_source's insufficient
    # branch, reopening the deferred marker without inventing recognized text.
    assert worker.run_one() == item
    reopened = store.presentation_context(item)
    assert reopened['pending']['snapshot'] == MARKER + ORIGINAL[2:]
    assert model.snapshots == [MARKER + ORIGINAL[2:]]
    return service, worker, item, directory, before, reopened, decision_revision


def test_unable_insufficient_replay_prepares_real_audio_then_manual_preserves_audit(replay):
    service, worker, item, directory, before, reopened, decision_revision = replay
    store = service.store
    pending = reopened['pending']
    member = pending['concerns'][0]
    original_member = before['concerns'][0]
    assert member['text'] == MARKER and member['candidates'] == ['持续']
    assert member['original_span'] == [0, 2]
    assert member['concern_uid'] == original_member['concern_uid']
    assert member['source_version_id'] == original_member['source_version_id']
    assert pending['uncertainties'][0] == {
        'start': 0, 'end': len(MARKER), 'text': MARKER, 'original_text': '持续',
        'reason': original_member['reason'], 'status': 'unresolved', 'by': 'human'}
    assert 'concern_uid' not in pending['uncertainties'][0]
    source = read_wav(directory, 'audio/standard.wav')
    assert not ready(pending, directory, source_descriptor=reopened['source_descriptor'])
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    # A new worker/service instance replays persistent Store/checkpoints.
    service = Distiller(store=store, source=None, normalizer=None,
        recognizer=NoModels(), reviewer=NoModels(),
        confirmation_clipper=service.confirmation_clipper,
        knowledge_model=service.knowledge_model, runtime_root=directory.parents[1],
        vault=None, ocr=object())
    assert SingleWorker(store, service).run_one() == item
    context = store.presentation_context(item)
    prepared = context['pending']
    assert ready(prepared, directory, source_descriptor=context['source_descriptor'])
    assert protected_pending(prepared) == protected_pending(pending)
    tampered = deepcopy(prepared)
    tampered['uncertainties'][0]['reason'] = 'a different human judgment'
    assert not ready(tampered, directory, source_descriptor=context['source_descriptor'])
    assert 'concern_uid' not in prepared['uncertainties'][0]
    assert prepared['audio_timeline']['text'] == ORIGINAL
    assert prepared['lineage'] == before['lineage']
    assert read_wav(directory, 'audio/standard.wav') == source
    assert service.confirmation_clipper.calls == 2
    audio = service.prepared_confirmation_audio(item, member['concern_uid'])
    assert isinstance(audio, bytes)
    prepared_member = next(c for c in prepared['concerns']
                           if c['concern_uid'] == member['concern_uid'])
    assert audio == (directory / 'confirmation' / prepared_member['audio_file']).read_bytes()
    with wave.open(io.BytesIO(audio), 'rb') as preview:
        assert (preview.getframerate(), preview.getnchannels(), preview.getsampwidth()) == (16000, 1, 2)
        assert preview.getcomptype() == 'NONE'
        # This fixture's single 10-second chunk yields the full 10-second preview.
        assert preview.getnframes() == 10 * 16000
        pcm = preview.readframes(preview.getnframes())
        assert pcm == bytes((1, 0)) * (10 * 16000)
    assert store.item_bundle(item)['source_fact_id'] is None
    assert store.item_bundle(item)['knowledge_result_id'] is None
    assert store.confirmation_decision(item, decision_revision, 'unable', '') == 'queued'
    assert store.discover_pending_presentations()['enqueued'] == ()
    assert SingleWorker(store, service).run_one() is None
    current = prepared['concerns'][0]
    manual_revision = revision(prepared, current)
    assert service.resolve(item, 'manual', '继续', token=prepared['token'],
        concern_id=current['audio_name'], concern_revision=manual_revision).state == 'waiting_user'
    final = store.presentation_context(item)['pending']
    assert final['snapshot'] == '继续' + ORIGINAL[2:]
    assert final['resolved'][-1] == {'text': MARKER, 'replacement': '继续', 'by': 'human'}
    assert final['deferred_concerns'] == [] and final['uncertainties'] == []
    assert store.confirmation_decision(item, decision_revision, 'unable', '') == 'queued'
    assert store.confirmation_decision(item, manual_revision, 'manual', '继续') == 'waiting_user'
    assert read_wav(directory, 'audio/standard.wav') == source


@pytest.mark.parametrize('case', [
    'missing_human', 'missing_deferred', 'human_span', 'human_text',
    'missing_original', 'wrong_original', 'wrong_candidates', 'duplicate_candidates',
    'duplicate_human', 'duplicate_deferred', 'deferred_span', 'deferred_text',
    'deferred_uid', 'deferred_source', 'deferred_candidates', 'deferred_original_span',
    'original_span', 'source_inheritance', 'review_inheritance', 'cross_member',
    'marker_candidate', 'ordinary_non_candidate', 'current_span',
    'missing_original_span', 'malformed_duplicate_human',
])
def test_marker_exception_rejects_incomplete_or_cross_member_proof(replay, case):
    *_, context, _revision = replay
    pending = deepcopy(context['pending'])
    member = pending['concerns'][0]
    deferred = pending['deferred_concerns'][0]
    human = pending['uncertainties'][0]
    if case == 'missing_human': pending['uncertainties'] = []
    elif case == 'missing_deferred': pending['deferred_concerns'] = []
    elif case == 'human_span': human['end'] -= 1
    elif case == 'human_text': human['text'] = '持续'
    elif case == 'missing_original': human.pop('original_text')
    elif case == 'wrong_original': human['original_text'] = '继续'
    elif case == 'wrong_candidates': member['candidates'] = deferred['candidates'] = ['继续']
    elif case == 'duplicate_candidates': member['candidates'] = deferred['candidates'] = ['持续', '持续']
    elif case == 'duplicate_human': pending['uncertainties'].append(deepcopy(human))
    elif case == 'duplicate_deferred': pending['deferred_concerns'].append(deepcopy(deferred))
    elif case == 'deferred_span': deferred['end'] -= 1
    elif case == 'deferred_text': deferred['text'] = '持续'
    elif case == 'deferred_uid': deferred['concern_uid'] = 'different-member'
    elif case == 'deferred_source': deferred['source_version_id'] = 'different-source'
    elif case == 'deferred_candidates': deferred['candidates'] = ['持续', '继续']
    elif case == 'deferred_original_span': deferred['original_span'] = [0, 1]
    elif case == 'original_span': member['original_span'] = deferred['original_span'] = [0, 1]
    elif case == 'source_inheritance': member['source_version_id'] = deferred['source_version_id'] = 'different-source'
    elif case == 'review_inheritance': member['original_review_hash'] = deferred['original_review_hash'] = 'different-review'
    elif case == 'cross_member': human['concern_uid'] = 'different-member'
    elif case == 'marker_candidate': member['candidates'] = deferred['candidates'] = ['持续', MARKER]
    elif case == 'current_span': member['current_span'] = deferred['current_span'] = [0, 2]
    elif case == 'missing_original_span': member.pop('original_span')
    elif case == 'malformed_duplicate_human':
        pending['uncertainties'].append({**human, 'start': None})
    elif case == 'ordinary_non_candidate':
        pending['snapshot'] = '错误' + ORIGINAL[2:]
        member.update(text='错误', start=0, end=2)
    with pytest.raises(PreparationError, match='^review_incomplete$'):
        input_binding(pending, source_descriptor=context['source_descriptor'])


def test_human_decision_remains_protected_from_preparer_edits(replay):
    *_, context, _revision = replay
    before = context['pending']
    after = deepcopy(before)
    after['uncertainties'][0]['reason'] = 'replacement judgment'
    assert input_binding(before, source_descriptor=context['source_descriptor']) != input_binding(
        after, source_descriptor=context['source_descriptor'])
    with pytest.raises(PreparationError):
        validate_change(before, after)


def test_marker_does_not_make_missing_original_audio_ready(replay):
    service, worker, item, directory, *_ = replay
    (directory / 'audio' / 'standard.wav').unlink()
    assert service.store.discover_pending_presentations()['enqueued'] == (item,)
    assert worker.run_one() == item
    row = service.store.item_bundle(item)
    assert row['state'] == 'failed' and row['error_code'] == 'confirmation_audio_unavailable'
    pending = json.loads(row['confirmation_json'])
    assert pending['snapshot'] == MARKER + ORIGINAL[2:]
    assert pending['uncertainties'][0]['by'] == 'human'
    assert pending['concerns'][0]['candidates'] == ['持续']
    assert service.prepared_confirmation_audio(item, pending['concerns'][0]['concern_uid']) is None
