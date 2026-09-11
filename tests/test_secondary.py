import wave
from pathlib import Path

import pytest

from knowledge_distiller.media import CommandResult
from knowledge_distiller.primary import StandardAudio
from knowledge_distiller.secondary import (
    FFmpegLocalAudioClipper,
    SecondaryAudio,
    SecondaryAudioError,
    SecondaryFailure,
    SeedAsrConfig,
    SeedAsrSecondaryResolver,
)


class ClipRunner:
    def __init__(self, *, returncode=0, frames=32_000):
        self.returncode = returncode
        self.frames = frames
        self.commands = []

    def run(self, command):
        self.commands.append(list(command))
        if self.returncode == 0:
            output = Path(command[-1])
            with wave.open(str(output), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16_000)
                audio.writeframes(b"\x00\x00" * self.frames)
        return CommandResult(self.returncode, "", "")


def standard_audio(tmp_path):
    path = tmp_path / "standard.wav"
    path.write_bytes(b"project-controlled-audio")
    return StandardAudio(path, 10.0)


def empty_config():
    return SeedAsrConfig("", "", "", "", "", "", "")


def test_clipper_produces_project_controlled_local_pcm_audio(tmp_path):
    runner = ClipRunner(frames=32_000)

    result = FFmpegLocalAudioClipper(runner).clip(
        standard_audio(tmp_path),
        2.0,
        4.0,
        tmp_path / "secondary" / "concern.wav",
    )

    assert result.source_start_seconds == 2.0
    assert result.source_end_seconds == 4.0
    assert result.duration_seconds == 2.0
    assert result.path.is_file()
    command = runner.commands[0]
    assert command[command.index("-ss") + 1] == "2.000"
    assert command[command.index("-t") + 1] == "2.000"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"


def test_clipper_failure_never_exposes_a_partial_local_audio(tmp_path):
    output = tmp_path / "secondary" / "concern.wav"

    with pytest.raises(SecondaryAudioError):
        FFmpegLocalAudioClipper(ClipRunner(returncode=1)).clip(
            standard_audio(tmp_path),
            2.0,
            4.0,
            output,
        )

    assert not output.exists()
    assert not output.with_suffix(".tmp.wav").exists()


def test_seed_readiness_is_checked_only_when_resolver_is_called(tmp_path):
    resolver = SeedAsrSecondaryResolver(empty_config())

    result = resolver.resolve(
        SecondaryAudio(tmp_path / "missing.wav", 0.0, 1.0, 1.0)
    )

    assert result.failure is SecondaryFailure.RUNTIME_UNAVAILABLE
    assert result.transcript is None
