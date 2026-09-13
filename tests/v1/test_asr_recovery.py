import struct
import wave
import pytest

from knowledge_distiller.primary import (PrimaryFailure, PrimaryRecognition,
    PrimaryRecovery, PrimaryChunk, StandardAudio)
from knowledge_distiller.v1.asr_recovery import recognize_resumable, MAX_RECOVERY_CALLS


def audio_fixture(tmp_path, count=64):
    path = tmp_path / 'input.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 10, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<' + 'h' * count, *range(1, count + 1)))
    return StandardAudio(path, count / 10, 10)


class Recognizer:
    def __init__(self, fail_right=False):
        self.calls = []
        self.fail_right = fail_right

    def recognize(self, audio):
        with wave.open(str(audio.path)) as stream:
            pcm = stream.readframes(stream.getnframes())
        frames = struct.unpack('<' + 'h' * (len(pcm) // 2), pcm)
        # Decoder-only silence is not source speech. This fixture numbers every
        # source frame and deliberately uses no zero-valued source samples.
        frames = tuple(frame for frame in frames if frame)
        self.calls.append((frames[0], frames[-1]))
        if len(frames) > 32:
            return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
        if self.fail_right and frames[0] == 33:
            self.fail_right = False
            return PrimaryRecognition.failed(PrimaryFailure.RUNTIME_FAILED)
        text = ' '.join(str(frame) for frame in frames)
        return PrimaryRecognition.succeeded(PrimaryRecovery(text, 'en',
            (PrimaryChunk(text, 0, audio.duration_seconds),)))


def test_persistent_incomplete_subdivides_without_losing_or_repeating_frames(tmp_path):
    audio = audio_fixture(tmp_path)
    recognizer = Recognizer()
    assert recognize_resumable(recognizer, audio, tmp_path).failure == PrimaryFailure.INCOMPLETE
    result = recognize_resumable(recognizer, audio, tmp_path)
    assert result.failure is None
    assert result.recovery.text.split() == [str(i) for i in range(1, 65)]
    assert [(c.start_seconds, c.end_seconds) for c in result.recovery.chunks] == [(0, 3.2), (3.2, 6.4)]
    assert recognizer.calls == [(1, 64), (1, 64), (1, 32), (33, 64)]
    restarted = Recognizer()
    assert recognize_resumable(restarted, audio, tmp_path) == result
    assert restarted.calls == []


def test_subdivision_restart_only_retries_failed_child(tmp_path):
    audio = audio_fixture(tmp_path)
    recognizer = Recognizer(fail_right=True)
    recognize_resumable(recognizer, audio, tmp_path)
    assert recognize_resumable(recognizer, audio, tmp_path).failure == PrimaryFailure.RUNTIME_FAILED
    restarted = Recognizer()
    result = recognize_resumable(restarted, audio, tmp_path)
    assert result.failure is None and restarted.calls == [(33, 64)]
    assert result.recovery.text.split() == [str(i) for i in range(1, 65)]


def test_incomplete_recovery_has_bounded_work_and_never_promotes_partial_result(tmp_path):
    audio = audio_fixture(tmp_path, 4096)
    class Fails:
        calls = 0
        def recognize(self, audio):
            self.calls += 1
            return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
    recognizer = Fails()
    recognize_resumable(recognizer, audio, tmp_path)
    before = recognizer.calls
    result = recognize_resumable(recognizer, audio, tmp_path)
    assert result.failure == PrimaryFailure.INCOMPLETE and result.recovery is None
    assert recognizer.calls - before <= MAX_RECOVERY_CALLS
    assert not (tmp_path / 'primary-recovery.json').exists()


def test_changed_audio_cannot_reuse_subdivision_or_merged_cache(tmp_path):
    audio = audio_fixture(tmp_path)
    recognizer = Recognizer()
    recognize_resumable(recognizer, audio, tmp_path)
    recognize_resumable(recognizer, audio, tmp_path)
    changed = audio_fixture(tmp_path, 16)
    restarted = Recognizer()
    result = recognize_resumable(restarted, changed, tmp_path)
    assert restarted.calls == [(1, 16)]
    assert result.recovery.text.split() == [str(i) for i in range(1, 17)]


def test_cut_prefers_quantization_noise_gap_over_midword(tmp_path):
    from knowledge_distiller.v1.asr_recovery import _cut
    path = tmp_path / 'gap.wav'
    frames = [10000] * 6400
    frames[2700:2900] = [3] * 200  # A quiet pause, not digital zero.
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 1000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<' + 'h' * len(frames), *frames))
    with wave.open(str(path), 'rb') as stream:
        cut = _cut(stream)
    assert 2700 < cut < 2900


def test_decoder_padding_does_not_expand_source_timeline(tmp_path):
    from knowledge_distiller.v1.asr_recovery import _decode_child
    audio = audio_fixture(tmp_path, 16)
    class BoundarySensitive:
        def recognize(self, padded):
            with wave.open(str(padded.path)) as stream:
                frames = struct.unpack('<20h', stream.readframes(stream.getnframes()))
            assert frames == (0, 0, *range(1, 17), 0, 0)
            return PrimaryRecognition.succeeded(PrimaryRecovery('whole speech', 'en',
                (PrimaryChunk('whole speech', 0, padded.duration_seconds),)))
    result = _decode_child(BoundarySensitive(), audio, tmp_path)
    assert result.recovery.text == 'whole speech'
    assert [(c.start_seconds, c.end_seconds) for c in result.recovery.chunks] == [(0, 1.6)]


def test_recovery_rejects_linked_child_root(tmp_path):
    import pytest
    audio = audio_fixture(tmp_path)
    recognizer = Recognizer()
    recognize_resumable(recognizer, audio, tmp_path)
    other = tmp_path / 'untouched'
    other.mkdir()
    (tmp_path / 'asr-recovery').symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError, match='asr_recovery_directory_link'):
        recognize_resumable(recognizer, audio, tmp_path)
    assert list(other.iterdir()) == []


def test_changed_runtime_identity_cannot_reuse_old_recovery(tmp_path):
    from types import SimpleNamespace
    audio = audio_fixture(tmp_path, 16)
    recognizer = Recognizer()
    recognizer.binding = SimpleNamespace(cache_identity=['runtime-one', 'model-one'])
    recognize_resumable(recognizer, audio, tmp_path)
    restarted = Recognizer()
    restarted.binding = SimpleNamespace(cache_identity=['runtime-two', 'model-two'])
    result = recognize_resumable(restarted, audio, tmp_path)
    assert result.failure is None
    assert restarted.calls == [(1, 16)]


@pytest.mark.parametrize('payload', ['null', '[]', 'true', '42', '"invalid"'])
def test_non_object_checkpoint_recomputes_instead_of_crashing(tmp_path, payload):
    audio = audio_fixture(tmp_path, 16)
    (tmp_path / 'primary-subdivision.json').write_text(payload)
    (tmp_path / 'primary-recovery.json').write_text(payload)
    recognizer = Recognizer()
    result = recognize_resumable(recognizer, audio, tmp_path)
    assert result.failure is None and recognizer.calls == [(1, 16)]
