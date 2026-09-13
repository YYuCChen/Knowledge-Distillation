import wave

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
