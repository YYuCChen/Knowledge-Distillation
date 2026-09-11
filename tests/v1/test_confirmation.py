import wave
from pathlib import Path

import pytest

from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.media import CommandResult
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery, StandardAudio
from knowledge_distiller.v1.confirmation import (
    ConfirmationAudioError,
    FFmpegConfirmationClipper,
    locate_concern_audio,
)


class Runner:
    def __init__(self, *, returncode: int = 0):
        self.returncode = returncode
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        if self.returncode == 0:
            duration = float(command[command.index("-t") + 1])
            frames = round(duration * 16_000)
            with wave.open(command[-1], "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\0\0" * frames)
        return CommandResult(self.returncode, "", "")


def inputs(tmp_path: Path):
    path = tmp_path / "standard.wav"
    path.write_bytes(b"standard audio")
    text = "前文持续切换会带来额外损耗后文"
    recovery = PrimaryRecovery(
        text,
        "zh",
        (PrimaryChunk(text, 0.0, 20.0, "zh"),),
    )
    start = text.index("持续")
    concern = ReviewConcern(
        start,
        start + 2,
        "持续",
        "可能改变原意",
        True,
        ("继续",),
    )
    return StandardAudio(path, 20.0), recovery, text, concern


def test_concern_maps_to_one_bounded_local_audio_range(tmp_path: Path) -> None:
    audio, recovery, text, concern = inputs(tmp_path)

    result = locate_concern_audio(audio, recovery, text, concern)

    assert result is not None
    start, end = result
    assert start == 0.0
    assert end == 10.0


def test_clipper_writes_complete_pcm_wav(tmp_path: Path) -> None:
    audio, recovery, text, concern = inputs(tmp_path)
    runner = Runner()
    output = tmp_path / "confirmation" / "concern-1.wav"

    result = FFmpegConfirmationClipper(runner).clip(
        audio, recovery, text, concern, output
    )

    assert result == output
    assert output.is_file()
    command = runner.commands[0]
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert float(command[command.index("-t") + 1]) == 10.0


def test_clipper_failure_never_leaves_partial_audio(tmp_path: Path) -> None:
    audio, recovery, text, concern = inputs(tmp_path)
    output = tmp_path / "confirmation" / "concern-1.wav"

    with pytest.raises(ConfirmationAudioError):
        FFmpegConfirmationClipper(Runner(returncode=1)).clip(
            audio, recovery, text, concern, output
        )

    assert not output.exists()
    assert not output.with_suffix(".tmp.wav").exists()


def test_missing_timeline_never_fabricates_an_audio_location(tmp_path: Path) -> None:
    audio, _, text, concern = inputs(tmp_path)
    recovery = PrimaryRecovery(text, "zh", ())

    assert locate_concern_audio(audio, recovery, text, concern) is None


@pytest.mark.parametrize("split", [False, True])
def test_reviewed_fillers_use_bounded_preview_of_enclosing_chunks(tmp_path: Path, split: bool) -> None:
    audio, _, _, _ = inputs(tmp_path)
    raw = "前文咱干这行应该都这个这个给供应商是这个这个再找售后就不好找了后文"
    candidate = "前文咱干这行应该都……给供应商是……再找售后就不好找了后文"
    chunks = (PrimaryChunk(raw, 0, 20),)
    if split:
        chunks = (PrimaryChunk(raw[:16], 0, 10), PrimaryChunk(raw[16:], 10, 20))
    recovery = PrimaryRecovery(raw, "zh", chunks)
    concern = ReviewConcern(2, len(candidate) - 2, candidate[2:-2], "回听", True, ())
    left, right = locate_concern_audio(audio, recovery, candidate, concern)
    assert right - left == 10
    assert 0 <= left < right <= 20


def test_unanchored_review_rewrite_has_no_invented_audio_range(tmp_path: Path) -> None:
    audio, recovery, _, _ = inputs(tmp_path)
    candidate = "完全没有来源依据"
    concern = ReviewConcern(0, len(candidate), candidate, "回听", True, ())
    assert locate_concern_audio(audio, recovery, candidate, concern) is None


@pytest.mark.parametrize('reading', ['腰板', '腰板站直', '站直腰板'])
def test_cleaned_replacement_boundaries_use_original_enclosing_segment(tmp_path, reading):
    path = tmp_path / 'standard.wav'
    path.write_bytes(b'audio')
    raw = '开场。前文要不然站直后文。结束。'
    candidate = '开场。前文腰板站直腰板后文。结束。'
    chunks = (PrimaryChunk('开场。', 0, 5),
        PrimaryChunk('前文要不然站直后文。', 5, 25), PrimaryChunk('结束。', 25, 30))
    start = candidate.index(reading)
    concern = ReviewConcern(start, start + len(reading), reading, '回听原词', True)
    located = locate_concern_audio(StandardAudio(path, 30), PrimaryRecovery(raw, 'zh', chunks), candidate, concern)
    assert located is not None
    assert located[1] - located[0] == 10
    assert 5 <= (located[0] + located[1]) / 2 <= 25


def test_boundary_rounding_noise_does_not_hide_exact_concern(
    tmp_path: Path,
) -> None:
    path = tmp_path / "standard.wav"
    path.write_bytes(b"standard audio")
    first = PrimaryChunk("前文", 0.0, 35.228750000000005, "zh")
    second = PrimaryChunk("概率型的这种赌差后文", 35.22875, 50.0, "zh")
    text = first.text + second.text
    start = text.index("概率型的这种赌差")
    concern = ReviewConcern(
        start,
        start + len("概率型的这种赌差"),
        "概率型的这种赌差",
        "同音词需要回听",
        True,
        ("概率型的这种赌注",),
    )

    result = locate_concern_audio(
        StandardAudio(path, 50.0),
        PrimaryRecovery(text, "zh", (first, second)),
        text,
        concern,
    )

    assert result is not None


@pytest.mark.parametrize('start,end,duration', [(0, 1, 30), (12, 14, 30), (28, 30, 30), (1, 2, 4), (5, 28, 30)])
def test_preview_stays_ten_seconds_even_when_asr_segment_is_long(start, end, duration):
    from knowledge_distiller.v1.confirmation import _preview_window
    left, right = _preview_window(start, end, duration)
    assert 0 <= left < right <= duration
    assert right - left == min(10, duration)
    assert left <= (start + end) / 2 <= right


def test_english_preview_is_local_and_independent_of_word_length(tmp_path):
    audio, _, _, _ = inputs(tmp_path)
    for prefix in ('a', 'extraordinarily'):
        text = f'{prefix} first disputed final word'
        recovery = PrimaryRecovery(text, 'en', (PrimaryChunk(text, 0, 20, 'en'),))
        start = text.index('disputed')
        result = locate_concern_audio(audio, recovery, text,
            ReviewConcern(start, start + len('disputed'), 'disputed', '听辨', True))
        assert result == (5, 15)


def test_long_english_alignment_uses_words_and_exact_character_anchors(monkeypatch):
    import knowledge_distiller.v1.confirmation as module
    original = 'The speaker discusses the market and technology. ' * 800
    candidate = original[:15000] + 'Updated context. ' + original[15000:]
    real = module.SequenceMatcher
    sizes = []
    def measured(junk, a, b, **kwargs):
        sizes.append((len(a), len(b)))
        return real(junk, a, b, **kwargs)
    monkeypatch.setattr(module, 'SequenceMatcher', measured)
    blocks = module.alignment_blocks(original, candidate)
    assert sizes[0][0] < len(original) / 4
    assert all(original[b.a:b.a+b.size] == candidate[b.b:b.b+b.size] for b in blocks)
    assert sum(b.size for b in blocks) > len(original) * .95


def test_unchanged_transcript_needs_no_alignment_search(monkeypatch):
    import knowledge_distiller.v1.confirmation as module
    monkeypatch.setattr(module, 'SequenceMatcher', lambda *a, **k: pytest.fail('unnecessary search'))
    text = 'A long unchanged recording. ' * 1000
    blocks = module.alignment_blocks(text, text)
    assert blocks[0] == (0, 0, len(text))
