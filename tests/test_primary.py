import subprocess
import wave
from dataclasses import asdict
from pathlib import Path

import pytest

from knowledge_distiller.media import CommandResult, VerifiedTemporaryMedia
from knowledge_distiller.primary import (
    AudioNormalizationFailure,
    FFmpegAudioNormalizer,
    PrimaryFailure,
    QwenPrimaryAdapter,
    StandardAudio,
    QwenRuntimeFailure,
    QwenRuntimeResult,
    QwenRuntimeUnavailable,
)


class AudioConversionRunner:
    def __init__(self, *, returncode=0, valid_output=True):
        self.returncode = returncode
        self.valid_output = valid_output
        self.commands = []

    def run(self, command):
        self.commands.append(list(command))
        if self.returncode == 0:
            output = Path(command[-1])
            if self.valid_output:
                with wave.open(str(output), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16_000)
                    audio.writeframes(b"\x00\x00" * 16_000)
            else:
                output.write_bytes(b"not-wave")
        return CommandResult(self.returncode, "", "")


class QwenBinding:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def transcribe(self, audio_path):
        if self.error is not None:
            raise self.error
        return self.result


def verified_media(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"verified-media")
    return VerifiedTemporaryMedia(
        platform="douyin",
        platform_item_id="stable-work-1",
        path=path,
        duration_seconds=1.0,
    )


def qwen_result(**overrides):
    values = {
        "text": "这是一段完整的识别文本。",
        "language": "Chinese",
        "finish_reason": "eos",
        "truncated": False,
        "chunks": [
            {
                "text": "这是一段完整的识别文本。",
                "start": 0.0,
                "end": 62.02,
                "language": "Chinese",
                "finish_reason": "eos",
                "truncated": False,
                "generated_tokens": 10,
                "max_new_tokens": 100,
            }
        ],
    }
    values.update(overrides)
    return QwenRuntimeResult(**values)


def test_verified_media_is_normalized_to_project_controlled_pcm_wav(tmp_path):
    runner = AudioConversionRunner()

    result = FFmpegAudioNormalizer(runner).normalize(
        verified_media(tmp_path),
        tmp_path / "primary",
    )

    assert result.failure is None
    assert result.audio.path == tmp_path / "primary" / "standard.wav"
    assert result.audio.sample_rate_hz == 16_000
    assert result.audio.channels == 1
    assert result.audio.sample_width_bytes == 2
    assert result.audio.duration_seconds == 1.0
    command = runner.commands[0]
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"


def test_audio_normalization_rejects_unverified_input(tmp_path):
    result = FFmpegAudioNormalizer(AudioConversionRunner()).normalize(
        tmp_path / "source.mp4",  # type: ignore[arg-type]
        tmp_path / "primary",
    )

    assert result.failure is AudioNormalizationFailure.UNVERIFIED_INPUT


@pytest.mark.parametrize(
    "runner,expected",
    [
        (
            AudioConversionRunner(returncode=1),
            AudioNormalizationFailure.CONVERSION_FAILED,
        ),
        (
            AudioConversionRunner(valid_output=False),
            AudioNormalizationFailure.OUTPUT_INVALID,
        ),
    ],
)
def test_audio_normalization_failures_are_stable(tmp_path, runner, expected):
    result = FFmpegAudioNormalizer(runner).normalize(
        verified_media(tmp_path),
        tmp_path / "primary",
    )

    assert result.failure is expected
    assert result.audio is None


def test_audio_normalization_rejects_truncated_standard_audio(tmp_path):
    media = verified_media(tmp_path)
    media = VerifiedTemporaryMedia(
        platform=media.platform,
        platform_item_id=media.platform_item_id,
        path=media.path,
        duration_seconds=10.0,
    )

    result = FFmpegAudioNormalizer(AudioConversionRunner()).normalize(
        media,
        tmp_path / "primary",
    )

    assert result.failure is AudioNormalizationFailure.OUTPUT_INVALID


def test_real_ffmpeg_standardizes_verified_media(tmp_path):
    media_path = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x32:rate=10:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=44100:duration=0.5",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(media_path),
        ],
        check=True,
        capture_output=True,
    )
    media = VerifiedTemporaryMedia(
        platform="douyin",
        platform_item_id="fixture",
        path=media_path,
        duration_seconds=0.5,
    )

    result = FFmpegAudioNormalizer().normalize(media, tmp_path / "primary")

    assert result.failure is None
    assert result.audio.sample_rate_hz == 16_000
    assert result.audio.channels == 1
    assert result.audio.sample_width_bytes == 2
    assert 0.45 <= result.audio.duration_seconds <= 0.55


def test_qwen_complete_output_is_translated_to_project_primary_material(tmp_path):
    audio = StandardAudio(tmp_path / "standard.wav", 62.02)

    result = QwenPrimaryAdapter(QwenBinding(qwen_result())).recognize(audio)

    assert result.failure is None
    assert asdict(result.recovery) == {
        "text": "这是一段完整的识别文本。",
        "language": "Chinese",
        "chunks": (
            {
                "text": "这是一段完整的识别文本。",
                "start_seconds": 0.0,
                "end_seconds": 62.02,
                "language": "Chinese",
            },
        ),
        "completed_normally": True,
        "truncated": False,
    }
    assert "generated_tokens" not in repr(result.recovery)
    assert "QwenRuntimeResult" not in repr(result.recovery)


@pytest.mark.parametrize(
    "runtime_result",
    [
        qwen_result(truncated=True),
        qwen_result(finish_reason="length"),
        qwen_result(finish_reason="repetition"),
        qwen_result(
            chunks=[
                {
                    "text": "局部",
                    "start": 0,
                    "end": 62.02,
                    "language": "Chinese",
                    "finish_reason": "length",
                    "truncated": True,
                }
            ]
        ),
    ],
)
def test_qwen_incomplete_endings_are_not_primary_success(tmp_path, runtime_result):
    result = QwenPrimaryAdapter(QwenBinding(runtime_result)).recognize(
        StandardAudio(tmp_path / "standard.wav", 62.02)
    )

    assert result.failure is PrimaryFailure.INCOMPLETE
    assert result.recovery is None


def test_qwen_empty_text_is_not_primary_success(tmp_path):
    result = QwenPrimaryAdapter(QwenBinding(qwen_result(text="  "))).recognize(
        StandardAudio(tmp_path / "standard.wav", 62.02)
    )

    assert result.failure is PrimaryFailure.EMPTY_OUTPUT


@pytest.mark.parametrize(
    "error,expected",
    [
        (QwenRuntimeUnavailable(), PrimaryFailure.RUNTIME_UNAVAILABLE),
        (QwenRuntimeFailure(), PrimaryFailure.RUNTIME_FAILED),
    ],
)
def test_qwen_runtime_failures_are_translated(tmp_path, error, expected):
    result = QwenPrimaryAdapter(QwenBinding(error=error)).recognize(
        StandardAudio(tmp_path / "standard.wav", 62.02)
    )

    assert result.failure is expected
    assert result.recovery is None


def test_installed_runtime_keeps_detected_source_language(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import sys
    import knowledge_distiller.primary as primary
    from knowledge_distiller.primary import InstalledQwenRuntime
    runtime=InstalledQwenRuntime(str(tmp_path))
    def transcribe(path,**kwargs):
        return SimpleNamespace(text='Source evidence stays in its original language.',
                               language=kwargs['language'] or 'English',chunks=[])
    monkeypatch.setattr(primary,'version',lambda name:primary.QWEN_RUNTIME_VERSION)
    monkeypatch.setitem(sys.modules,'mlx_qwen3_asr',SimpleNamespace(transcribe=transcribe))
    result=runtime.transcribe(tmp_path/'english.wav')
    assert result.language=='English'
    assert result.text=='Source evidence stays in its original language.'
