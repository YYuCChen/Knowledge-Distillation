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


def test_local_recovery_keeps_question_identity_and_review(tmp_path, monkeypatch):
    from tests.v1.test_pipeline import distiller
    from knowledge_distiller.faithful_review import ReviewConcern
    from knowledge_distiller.primary import PrimaryChunk
    from knowledge_distiller.v1.confirmation_revision import revision
    service, store, *_ = distiller(tmp_path,
        concerns=(ReviewConcern(0, 4, '持续切换', 'unclear', True),))
    item = store.create_item('https://v.douyin.com/test/')
    service.run(item)
    before = json.loads(store.item_bundle(item)['confirmation_json'])
    old = before['concerns'][0]
    audio = service.runtime_root / 'items' / str(item) / 'audio' / 'standard.wav'
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b'preserved synthetic source')
    monkeypatch.setattr('knowledge_distiller.v1.audio_location_recovery.recover_locations',
        lambda recognizer, audio, recovery, directory: PrimaryRecovery(recovery.text, 'zh',
            (PrimaryChunk(recovery.text, 0, 8, 'zh'),), timeline_status='recovered_windows'))
    class Clipper:
        def clip(self, audio, recovery, text, issue, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'local preview')
    service.confirmation_clipper = Clipper()
    service.recover_confirmation_audio(item, token=before['token'], concern_id=old['audio_name'])
    after = json.loads(store.item_bundle(item)['confirmation_json'])
    assert after['snapshot'] == before['snapshot']
    assert after['lineage'] == before['lineage']
    assert revision(before, old) == revision(after, after['concerns'][0])
    assert service.confirmation_audio(item, old['audio_name']).read_bytes() == b'local preview'
    assert store.item_bundle(item)['source_fact_id'] is None
