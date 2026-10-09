import wave
import json

from knowledge_distiller.primary import PrimaryRecovery, PrimaryRecognition, StandardAudio
from knowledge_distiller.v1.audio_location_recovery import recover_locations


def test_replay_asr_never_replaces_completed_source_and_reuses_windows(tmp_path):
    path = tmp_path / 'source.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 16000 * 12)
    class Recognizer:
        calls = 0
        def recognize(self, audio):
            self.calls += 1
            return PrimaryRecognition.succeeded(PrimaryRecovery('独立定位文字', None, ()))
    recognizer = Recognizer()
    source = PrimaryRecovery('这份完整来源文本必须原样保留。', None, ())
    result = recover_locations(recognizer, StandardAudio(path, 12), source, tmp_path)
    assert result.text == source.text
    assert result.timeline_status == 'recovered_windows'
    assert [(c.start_seconds, c.end_seconds) for c in result.chunks] == [(0, 8), (8, 12)]
    recover_locations(recognizer, StandardAudio(path, 12), source, tmp_path)
    assert recognizer.calls == 2


def test_incomplete_location_scan_retries_only_failed_window(tmp_path):
    from knowledge_distiller.primary import PrimaryFailure
    path = tmp_path / 'source.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 16000 * 12)
    class Recognizer:
        def __init__(self):
            self.calls = []
        def recognize(self, audio):
            self.calls.append(audio.duration_seconds)
            if self.calls == [8, 4]:
                return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
            return PrimaryRecognition.succeeded(PrimaryRecovery('独立定位文字', None, ()))
    recognizer = Recognizer()
    source = PrimaryRecovery('完整转写不得替换。', None, ())
    first = recover_locations(recognizer, StandardAudio(path, 12), source, tmp_path)
    assert first is source  # A missed window cannot establish a unique location.
    second = recover_locations(recognizer, StandardAudio(path, 12), source, tmp_path)
    assert recognizer.calls == [8, 4, 4]
    assert second.text == source.text and second.timeline_status == 'recovered_windows'
    status = json.loads((tmp_path / 'location-recovery' / 'status.json').read_text())
    import hashlib
    assert status['source_audio_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert status['source_text_sha256'] == hashlib.sha256(source.text.encode()).hexdigest()
    assert status['failures'] == []


def test_working_preparation_keeps_question_identity_and_review(tmp_path, monkeypatch):
    from tests.v1.test_pipeline import distiller
    from knowledge_distiller.faithful_review import ReviewConcern
    from knowledge_distiller.primary import PrimaryChunk
    from knowledge_distiller.v1.confirmation_revision import revision
    service, store, *_ = distiller(tmp_path,
        concerns=(ReviewConcern(0, 4, '持续切换', 'unclear', True),))
    from knowledge_distiller.primary import AudioNormalization
    from types import SimpleNamespace
    def write_wav(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\x01\x00' * 16000 * 10)
    def normalize(media, directory):
        path = directory / 'standard.wav'
        write_wav(path)
        return AudioNormalization.succeeded(StandardAudio(path, 10))
    service.normalizer = SimpleNamespace(normalize=normalize)
    service.recognizer = SimpleNamespace(recognize=lambda audio: PrimaryRecognition.succeeded(
        PrimaryRecovery('持续切换会带来额外损耗。', 'zh',
                        (PrimaryChunk('持续切换会带来额外损耗。', 0, 10, 'zh'),))))
    class Clipper:
        def clip(self, audio, recovery, text, issue, output):
            write_wav(output)
    service.confirmation_clipper = Clipper()
    item = store.create_item('https://v.douyin.com/test/')
    service.run(item)
    before = json.loads(store.item_bundle(item)['confirmation_json'])
    old = before['concerns'][0]
    directory = service.runtime_root / 'items' / str(item)
    # Controlled legacy pending requiring preparation, not a repair-button call.
    pending = json.loads(json.dumps(before))
    pending.pop('presentation_preparation', None)
    pending['audio_timeline']['chunks'] = []
    pending['audio_timeline']['timeline_status'] = 'needs_recovery'
    store.mark_waiting(item, pending)
    # Test-owned clean absence, not acceptance of a corrupt checkpoint.
    (directory / 'confirmation-preparation.json').unlink()
    monkeypatch.setattr('knowledge_distiller.v1.audio_location_recovery.recover_locations',
        lambda recognizer, audio, recovery, directory: PrimaryRecovery(recovery.text, 'zh',
            (PrimaryChunk(recovery.text, 0, 10, 'zh'),), timeline_status='recovered_windows'))
    assert store.discover_pending_presentations()['enqueued'] == (item,)
    assert store.claim_next_work() == ('presentation', item)
    prepared = service.prepare_pending_presentation(item)
    assert prepared['status'] == 'prepared'
    store.finish_pending_presentation(item, prepared['ownership'], prepared)
    after = json.loads(store.item_bundle(item)['confirmation_json'])
    assert after['snapshot'] == before['snapshot']
    assert after['lineage'] == before['lineage']
    assert revision(before, old) == revision(after, after['concerns'][0])
    with wave.open(str(service.confirmation_audio(item, old['audio_name'])), 'rb') as clip:
        assert clip.getnframes() / clip.getframerate() == 10
    assert store.item_bundle(item)['source_fact_id'] is None


def test_changed_source_bytes_do_not_reuse_old_window_asr(tmp_path):
    from knowledge_distiller.primary import PrimaryRecognition
    path = tmp_path.resolve() / 'source.wav'
    def write(seed):
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(bytes((seed, 0)) * 16000 * 12)
    class Recognizer:
        calls = 0
        def recognize(self, audio):
            self.calls += 1
            return PrimaryRecognition.succeeded(PrimaryRecovery('仅供定位', None, ()))
    recognizer = Recognizer()
    source = PrimaryRecovery('原始完成转写', None, ())
    write(1)
    first = recover_locations(recognizer, StandardAudio(path, 12), source, path.parent)
    write(2)
    second = recover_locations(recognizer, StandardAudio(path, 12), source, path.parent)
    assert recognizer.calls == 4
    assert first.text == second.text == source.text
