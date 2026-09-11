import json
from pathlib import Path

import pytest

from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.primary import PrimaryRecognition, PrimaryRecovery, PrimaryChunk, StandardAudio
from knowledge_distiller.v1.confirmation import locate_concern_audio
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.domain import SourceFact
from knowledge_distiller.v1.worker import SingleWorker
from knowledge_distiller.v1.web import create_app
from .test_pipeline import distiller, confirmation_token


TEXT = '🙂咱不存在底气不足、不自信，要不然啥时候都是硬的。面对老板和客户，价值观正，人是有底气的。'


def test_zero_issues_proceeds_without_user_review(tmp_path):
    service, store, _, model, _ = distiller(tmp_path)
    item = store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state == 'succeeded'
    row = store.item_bundle(item)
    assert row['source_fact_id'] is not None and model.calls == 1
    assert row['confirmation_json'] is None
    page = create_app(store, service).test_client().get('/').text
    assert '确认全文' not in page and '校对全文' not in page
    assert SingleWorker(store, service).run_one() is None


def test_legacy_review_stays_pending_until_user_explicitly_continues(tmp_path):
    service, store, model, item, _ = sample(tmp_path)
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    pending.update(concerns=[], review_required=True)
    store.mark_waiting(item, pending)
    restarted = Store(store.path)
    service.store = restarted
    assert SingleWorker(restarted, service).run_one() is None
    assert restarted.item_bundle(item)['source_fact_id'] is None and model.calls == 0
    page = create_app(restarted, service).test_client().get('/').text
    assert '继续生成知识' in page and '确认全文' not in page
    assert service.finish_transcript(item, token=confirmation_token(restarted, item)).state == 'queued'
    assert SingleWorker(restarted, service).run_one() == item
    assert restarted.item_bundle(item)['state'] == 'succeeded'


def sample(tmp_path):
    offset = TEXT.index('硬')
    service, store, source, model, vault = distiller(tmp_path, concerns=(
        ReviewConcern(offset, offset + 1, '硬', '需要回听', True, ('赢',)),))
    class Recognition:
        def recognize(self, audio):
            return PrimaryRecognition.succeeded(PrimaryRecovery(TEXT, 'zh', (PrimaryChunk(TEXT, 0, 10),)))
    service.recognizer = Recognition()
    item = store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state == 'waiting_user'
    return service, store, model, item, vault


def correct(service, store, item, original='要不然', replacement='腰板', **kwargs):
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    start = pending['snapshot'].index(original)
    return service.correct_transcript(item, token=kwargs.get('token', pending['token']),
        start=start, end=start + len(original), original=original, replacement=replacement)


def test_missed_word_correction_restarts_then_manual_candidate_and_evidence(tmp_path):
    service, store, model, item, vault = sample(tmp_path)
    old = confirmation_token(store, item)
    assert correct(service, store, item).state == 'waiting_user'
    saved = store.item_bundle(item)['confirmation_json']
    pending = json.loads(saved)
    assert pending['snapshot'].endswith('人是有底气的。')
    assert '腰板啥时候都是硬的' in pending['snapshot']
    c = pending['concerns'][0]
    assert pending['snapshot'][c['start']:c['end']] == '硬'
    assert c['start'] == TEXT.index('硬') - 1
    assert store.item_bundle(item)['source_fact_id'] is None and model.calls == 0
    with pytest.raises(ValueError):
        service.correct_transcript(item, token=old, start=TEXT.index('要不然'),
            end=TEXT.index('要不然') + 3, original='要不然', replacement='腰板')
    assert store.item_bundle(item)['confirmation_json'] == saved
    service.store = Store(store.path)
    assert service.store.item_bundle(item)['confirmation_json'] == saved
    # A correct reading absent from all candidates uses the existing manual path.
    assert service.resolve(item, 'manual', '硬气', token=pending['token']).state == 'waiting_user'
    assert service.store.item_bundle(item)['source_fact_id'] is None
    client = create_app(service.store, service).test_client()
    assert '继续生成知识' in client.get('/').text
    assert '确认全文' not in client.get('/').text
    finish_token = confirmation_token(store, item)
    assert client.post(f'/items/{item}/continue', data={'action': 'finish', 'token': finish_token}).status_code == 302
    assert SingleWorker(service.store, service).run_one() == item
    row = store.item_bundle(item)
    assert row['state'] == 'succeeded' and '腰板啥时候都是硬气的' in row['snapshot']
    assert '要不然' not in row['snapshot']
    payload = json.loads(row['payload_json'])
    for evidence in payload['evidence']:
        assert row['snapshot'][evidence['start']:evidence['end']] == evidence['text']
    before = dict(row)
    assert client.post(f'/items/{item}/continue', data={'action': 'finish', 'token': finish_token}).status_code == 400
    assert dict(store.item_bundle(item)) == before
    assert (vault / row['published_path']).is_file()


@pytest.mark.parametrize('original,replacement', [('硬', '硬气'), ('要不然', ''), ('要不然', '[听辨不清]')])
def test_invalid_or_overlapping_edit_preserves_pending(tmp_path, original, replacement):
    service, store, _, item, _ = sample(tmp_path)
    before = store.item_bundle(item)['confirmation_json']
    with pytest.raises(ValueError):
        correct(service, store, item, original, replacement)
    assert store.item_bundle(item)['confirmation_json'] == before
    assert store.item_bundle(item)['source_fact_id'] is None


def test_edit_and_final_confirmation_are_transactionally_guarded(tmp_path, monkeypatch):
    service, store, _, item, _ = sample(tmp_path)
    commit = store.resolve_confirmation
    def race(item, expected, **kwargs):
        store.mark_waiting(item, json.loads(expected))
        return commit(item, expected, **kwargs)
    monkeypatch.setattr(store, 'resolve_confirmation', race)
    with pytest.raises(ValueError, match='来源确认已更新'):
        correct(service, store, item)
    assert '要不然' in json.loads(store.item_bundle(item)['confirmation_json'])['snapshot']
    assert store.item_bundle(item)['source_fact_id'] is None


def test_deleted_prior_occurrence_does_not_mislocate_later_audio(tmp_path):
    path = tmp_path / 'standard.wav'
    path.write_bytes(b'audio')
    original = '前文硬。中间过渡。后文硬。'
    current = '前文。中间过渡。后文硬。'
    recovery = PrimaryRecovery(original, 'zh', (
        PrimaryChunk('前文硬。中间过渡。', 0, 10), PrimaryChunk('后文硬。', 20, 30)))
    start = current.index('硬')
    located = locate_concern_audio(StandardAudio(path, 30), recovery, current,
        ReviewConcern(start, start + 1, '硬', '回听', True))
    assert located == (20, 30)  # Ten-second window still selects the later occurrence.


def test_concurrent_fact_establishment_never_silently_discards_correction(tmp_path, monkeypatch):
    service, store, _, item, _ = sample(tmp_path)
    row = store.item_bundle(item)
    commit = store.resolve_confirmation
    def competing_fact(item, expected, **kwargs):
        store.establish_source_fact(row['material_id'], SourceFact(TEXT))
        return commit(item, expected, **kwargs)
    monkeypatch.setattr(store, 'resolve_confirmation', competing_fact)
    with pytest.raises(ValueError, match='当前纠正尚未保存'):
        correct(service, store, item)
    after = store.item_bundle(item)
    assert after['snapshot'] == TEXT
    assert after['confirmation_json'] == row['confirmation_json']


def test_corrected_word_keeps_original_audio_anchor_after_restart_and_prior_edit(tmp_path):
    service, store, _, item, _ = sample(tmp_path)
    audio = service.runtime_root / 'items' / str(item) / 'audio/standard.wav'
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b'test audio')
    start = TEXT.index('要不然')
    original = service.transcript_location(item, token=confirmation_token(store, item), start=start, end=start+3)
    correct(service, store, item)
    correct(service, store, item, '🙂', '开场')
    service.store = Store(store.path)
    pending = json.loads(service.store.item_bundle(item)['confirmation_json'])
    start = pending['snapshot'].index('腰板')
    assert service.transcript_location(item, token=pending['token'], start=start, end=start+2) == original


def test_unknown_after_edit_stays_pending_until_explicit_finish(tmp_path):
    service, store, _, item, _ = sample(tmp_path)
    correct(service, store, item)
    assert service.resolve(item, 'unable', token=confirmation_token(store, item)).state == 'waiting_user'
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    unknown = pending['uncertainties'][0]
    assert pending['snapshot'][unknown['start']:unknown['end']] == '[听辨不清]'
    assert correct(service, store, item, '[听辨不清]', '硬').state == 'waiting_user'
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    assert pending['deferred_concerns'] == [] and pending['uncertainties'] == []
    assert service.finish_transcript(item, token=pending['token']).state == 'queued'
