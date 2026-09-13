from __future__ import annotations

import logging
import math
import os
import wave
from dataclasses import dataclass, replace
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Mapping, Protocol

from .media import CommandRunner, SubprocessCommandRunner, VerifiedTemporaryMedia


logger = logging.getLogger(__name__)

QWEN_RUNTIME_VERSION = "0.3.5"
QWEN_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
QWEN_MODEL_REVISION = "7278e1e70fe206f11671096ffdd38061171dd6e5"


class AudioNormalizationFailure(StrEnum):
    UNVERIFIED_INPUT = "unverified_input"
    CONVERSION_FAILED = "conversion_failed"
    OUTPUT_INVALID = "output_invalid"


@dataclass(frozen=True)
class StandardAudio:
    path: Path
    duration_seconds: float
    sample_rate_hz: int = 16_000
    channels: int = 1
    sample_width_bytes: int = 2


@dataclass(frozen=True)
class AudioNormalization:
    audio: StandardAudio | None = None
    failure: AudioNormalizationFailure | None = None

    def __post_init__(self) -> None:
        if (self.audio is None) == (self.failure is None):
            raise ValueError("Audio normalization must contain one result")

    @classmethod
    def succeeded(cls, audio: StandardAudio) -> AudioNormalization:
        return cls(audio=audio)

    @classmethod
    def failed(cls, failure: AudioNormalizationFailure) -> AudioNormalization:
        return cls(failure=failure)


class AudioNormalizer(Protocol):
    def normalize(
        self,
        media: VerifiedTemporaryMedia,
        work_dir: Path,
    ) -> AudioNormalization: ...


class FFmpegAudioNormalizer:
    _ABSOLUTE_DURATION_TOLERANCE_SECONDS = 0.25
    _RELATIVE_DURATION_TOLERANCE = 0.01

    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or SubprocessCommandRunner()

    def normalize(
        self,
        media: VerifiedTemporaryMedia,
        work_dir: Path,
    ) -> AudioNormalization:
        if not isinstance(media, VerifiedTemporaryMedia):
            return AudioNormalization.failed(
                AudioNormalizationFailure.UNVERIFIED_INPUT
            )
        try:
            if not media.path.is_file() or media.path.stat().st_size <= 0:
                return AudioNormalization.failed(
                    AudioNormalizationFailure.UNVERIFIED_INPUT
                )
        except OSError:
            return AudioNormalization.failed(AudioNormalizationFailure.UNVERIFIED_INPUT)

        work_dir.mkdir(parents=True, exist_ok=True)
        audio_path = work_dir / "standard.wav"
        temporary_path = work_dir / "standard.tmp.wav"
        temporary_path.unlink(missing_ok=True)
        try:
            converted = self.runner.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(media.path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(temporary_path),
                ]
            )
        except OSError:
            return AudioNormalization.failed(
                AudioNormalizationFailure.CONVERSION_FAILED
            )
        if converted.returncode != 0:
            temporary_path.unlink(missing_ok=True)
            return AudioNormalization.failed(
                AudioNormalizationFailure.CONVERSION_FAILED
            )

        try:
            standard_audio = _inspect_standard_audio(temporary_path)
            if not self._duration_matches_media(
                standard_audio.duration_seconds,
                media.duration_seconds,
            ):
                raise ValueError("Standard audio duration conflicts with media")
            os.replace(temporary_path, audio_path)
        except (OSError, wave.Error, ValueError):
            temporary_path.unlink(missing_ok=True)
            return AudioNormalization.failed(AudioNormalizationFailure.OUTPUT_INVALID)
        return AudioNormalization.succeeded(
            StandardAudio(
                path=audio_path,
                duration_seconds=standard_audio.duration_seconds,
            )
        )

    def _duration_matches_media(self, audio_duration: float, media_duration: float) -> bool:
        if not math.isfinite(media_duration) or media_duration <= 0:
            return False
        tolerance = max(
            self._ABSOLUTE_DURATION_TOLERANCE_SECONDS,
            media_duration * self._RELATIVE_DURATION_TOLERANCE,
        )
        return abs(audio_duration - media_duration) <= tolerance


def _inspect_standard_audio(path: Path) -> StandardAudio:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("Standard audio is missing or empty")
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_rate = audio.getframerate()
        sample_width = audio.getsampwidth()
        frame_count = audio.getnframes()
    if channels != 1 or sample_rate != 16_000 or sample_width != 2:
        raise ValueError("Standard audio format is invalid")
    if frame_count <= 0:
        raise ValueError("Standard audio contains no frames")
    duration = frame_count / sample_rate
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Standard audio duration is invalid")
    return StandardAudio(path, duration, sample_rate, channels, sample_width)


@dataclass(frozen=True)
class PrimaryChunk:
    text: str
    start_seconds: float
    end_seconds: float
    language: str | None = None


@dataclass(frozen=True)
class PrimaryRecovery:
    text: str
    language: str | None
    chunks: tuple[PrimaryChunk, ...]
    completed_normally: bool = True
    truncated: bool = False
    timeline_status: str = 'unverified'
    timeline_diagnostics: tuple[str, ...] = ()


def qualify_primary_timeline(recovery, audio):
    if not recovery.chunks:
        return replace(recovery,timeline_status='needs_recovery')
    previous = 0.0
    for index,chunk in enumerate(recovery.chunks):
        if (not isinstance(chunk.text, str) or not chunk.text.strip()
            or not all(type(t) in (int,float) and math.isfinite(t) for t in (chunk.start_seconds, chunk.end_seconds))
            or chunk.start_seconds < previous-1e-6 or chunk.end_seconds <= chunk.start_seconds
            or chunk.end_seconds > audio.duration_seconds + 0.25):
            return replace(recovery,chunks=(),timeline_status='needs_recovery',
                timeline_diagnostics=(f'invalid_timeline_chunk:{index}',))
        previous = chunk.end_seconds
    return replace(recovery,timeline_status='available')


class PrimaryFailure(StrEnum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    RUNTIME_FAILED = "runtime_failed"
    INCOMPLETE = "incomplete"
    EMPTY_OUTPUT = "empty_output"


@dataclass(frozen=True)
class PrimaryRecognition:
    recovery: PrimaryRecovery | None = None
    failure: PrimaryFailure | None = None

    def __post_init__(self) -> None:
        if (self.recovery is None) == (self.failure is None):
            raise ValueError("Primary recognition must contain one result")

    @classmethod
    def succeeded(cls, recovery: PrimaryRecovery) -> PrimaryRecognition:
        return cls(recovery=recovery)

    @classmethod
    def failed(cls, failure: PrimaryFailure) -> PrimaryRecognition:
        return cls(failure=failure)


class PrimaryRecognizer(Protocol):
    def recognize(self, audio: StandardAudio) -> PrimaryRecognition: ...


class QwenRuntimeUnavailable(Exception):
    pass


class QwenRuntimeFailure(Exception):
    pass


@dataclass(frozen=True)
class QwenRuntimeResult:
    text: str
    language: str | None
    finish_reason: str | None
    truncated: bool
    chunks: object


class QwenRuntimeBinding(Protocol):
    def transcribe(self, audio_path: Path) -> QwenRuntimeResult: ...


class InstalledQwenRuntime:
    def __init__(self, model: str):
        self.model = model

    def transcribe(self, audio_path: Path) -> QwenRuntimeResult:
        try:
            from .v1.adapters.python_policy import check_current
            try:
                check_current()
            except RuntimeError as error:
                raise QwenRuntimeUnavailable from error
            installed_version = version("mlx-qwen3-asr")
            if installed_version != QWEN_RUNTIME_VERSION:
                raise QwenRuntimeUnavailable
            from mlx_qwen3_asr import transcribe
        except (ImportError, PackageNotFoundError) as error:
            raise QwenRuntimeUnavailable from error

        model_path = self._resolve_model()
        try:
            result = transcribe(
                audio_path,
                model=model_path,
                language=None,
                return_chunks=True,
                return_timestamps=False,
                forced_aligner=None,
                verbose=False,
            )
        except Exception as error:
            logger.warning("Qwen Primary runtime failed: %s", type(error).__name__)
            raise QwenRuntimeFailure from error
        return QwenRuntimeResult(
            text=str(getattr(result, "text", "") or ""),
            language=_optional_text(getattr(result, "language", None)),
            finish_reason=_optional_text(getattr(result, "finish_reason", None)),
            truncated=bool(getattr(result, "truncated", False)),
            chunks=getattr(result, "chunks", None),
        )

    def _resolve_model(self) -> str:
        candidate = Path(self.model).expanduser()
        if candidate.exists():
            if not candidate.is_dir():
                raise QwenRuntimeUnavailable
            return str(candidate.resolve())
        if self.model != QWEN_MODEL_ID:
            raise QwenRuntimeUnavailable
        try:
            from huggingface_hub import snapshot_download

            return snapshot_download(
                repo_id=QWEN_MODEL_ID,
                revision=QWEN_MODEL_REVISION,
            )
        except Exception as error:
            raise QwenRuntimeUnavailable from error


class QwenPrimaryAdapter:
    def __init__(self, binding: QwenRuntimeBinding):
        self.binding = binding

    def recognize(self, audio: StandardAudio) -> PrimaryRecognition:
        try:
            result = self.binding.transcribe(audio.path)
        except QwenRuntimeUnavailable:
            return PrimaryRecognition.failed(PrimaryFailure.RUNTIME_UNAVAILABLE)
        except QwenRuntimeFailure:
            return PrimaryRecognition.failed(PrimaryFailure.RUNTIME_FAILED)

        if result.truncated or result.finish_reason != "eos":
            return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
        text = result.text.strip()
        if not text:
            return PrimaryRecognition.failed(PrimaryFailure.EMPTY_OUTPUT)
        chunks = _translate_qwen_chunks(result.chunks)
        if chunks is None:
            # Explicitly interrupted pieces are incomplete even when a global
            # marker claims eos. Bad/missing positioning alone is not truncation.
            if isinstance(result.chunks, list) and any(isinstance(c, Mapping) and (
                    c.get('truncated') is True or c.get('finish_reason') not in (None, 'eos'))
                    for c in result.chunks):
                return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
            return PrimaryRecognition.succeeded(PrimaryRecovery(text,result.language,(),
                timeline_status='needs_recovery',timeline_diagnostics=('qwen_timeline_invalid_or_missing',)))
        return PrimaryRecognition.succeeded(qualify_primary_timeline(
            PrimaryRecovery(text=text, language=result.language, chunks=chunks), audio))


def build_qwen_primary(model: str | None = None) -> QwenPrimaryAdapter:
    selected_model = model or os.environ.get(
        "KNOWLEDGE_DISTILLER_QWEN_MODEL",
        QWEN_MODEL_ID,
    )
    return QwenPrimaryAdapter(InstalledQwenRuntime(selected_model))


def _translate_qwen_chunks(value: object) -> tuple[PrimaryChunk, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    chunks: list[PrimaryChunk] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        if bool(item.get("truncated")) or item.get("finish_reason") != "eos":
            return None
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(number) for number in (start, end)):
            return None
        if start < 0 or end <= start:
            return None
        chunk_text = str(item.get("text") or "").strip()
        chunks.append(
            PrimaryChunk(
                text=chunk_text,
                start_seconds=start,
                end_seconds=end,
                language=_optional_text(item.get("language")),
            )
        )
    return tuple(chunks)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
