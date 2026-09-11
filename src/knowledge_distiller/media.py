from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Sequence

from .identity import ConfirmedMaterialIdentity


class MediaAcquisitionKind(StrEnum):
    DOWNLOADED = "downloaded"
    REUSED = "reused"


class MediaFailure(StrEnum):
    LOGIN_REQUIRED = "login_required"
    SOURCE_UNAVAILABLE = "source_unavailable"
    INTEGRITY_FAILED = "integrity_failed"
    UPSTREAM_RESULT_INVALID = "upstream_result_invalid"
    UPSTREAM_FAILURE = "upstream_failure"


@dataclass(frozen=True)
class VerifiedTemporaryMedia:
    platform: str
    platform_item_id: str
    path: Path
    duration_seconds: float
    author_name: str | None = None
    author_platform_id: str | None = None
    original_description: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class MediaAcquisition:
    kind: MediaAcquisitionKind | None = None
    media: VerifiedTemporaryMedia | None = None
    failure: MediaFailure | None = None

    def __post_init__(self) -> None:
        if self.failure is not None:
            if self.kind is not None or self.media is not None:
                raise ValueError("Failed media acquisition cannot contain media")
        elif self.kind is None or self.media is None:
            raise ValueError("Successful media acquisition requires kind and media")

    @classmethod
    def succeeded(
        cls,
        kind: MediaAcquisitionKind,
        media: VerifiedTemporaryMedia,
    ) -> MediaAcquisition:
        return cls(kind=kind, media=media)

    @classmethod
    def failed(cls, failure: MediaFailure) -> MediaAcquisition:
        return cls(failure=failure)


class MaterialMediaAcquirer(Protocol):
    def acquire(
        self,
        identity: ConfirmedMaterialIdentity,
        target_url: str,
        work_dir: Path,
    ) -> MediaAcquisition: ...


class MediaVerificationError(Exception):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, command: Sequence[str]) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, command: Sequence[str]) -> CommandResult:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class FFmpegMediaVerifier:
    _MINIMUM_DURATION_SECONDS = 0.1
    _ABSOLUTE_DURATION_TOLERANCE_SECONDS = 2.0
    _RELATIVE_DURATION_TOLERANCE = 0.05

    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or SubprocessCommandRunner()

    def verify(
        self,
        media_path: Path,
        *,
        expected_duration_seconds: float | None,
        complete_decode: bool,
        require_video: bool = True,
    ) -> float:
        try:
            if not media_path.is_file() or media_path.stat().st_size <= 0:
                raise MediaVerificationError("Media file is missing or empty")
        except OSError as error:
            raise MediaVerificationError("Media file cannot be inspected") from error

        probe = self._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(media_path),
            ]
        )
        if probe.returncode != 0:
            raise MediaVerificationError("Media container cannot be parsed")
        try:
            payload = json.loads(probe.stdout)
            streams = payload["streams"]
            duration_seconds = float(payload["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise MediaVerificationError("Media probe result is invalid") from error

        if not isinstance(streams, list):
            raise MediaVerificationError("Media stream list is invalid")
        if not all(isinstance(stream, dict) for stream in streams):
            raise MediaVerificationError("Media stream entry is invalid")
        if require_video and not any(stream.get("codec_type") == "video" for stream in streams):
            raise MediaVerificationError("Media has no video stream")
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise MediaVerificationError("Media has no audio stream")
        if (
            not math.isfinite(duration_seconds)
            or duration_seconds < self._MINIMUM_DURATION_SECONDS
        ):
            raise MediaVerificationError("Media duration is not reasonable")
        self._verify_expected_duration(duration_seconds, expected_duration_seconds)

        if complete_decode:
            if require_video:
                self._verify_complete_decode(media_path, "0:v:0")
            self._verify_complete_decode(media_path, "0:a:0")
        return duration_seconds

    def _verify_expected_duration(
        self,
        actual: float,
        expected: float | None,
    ) -> None:
        if expected is None or expected <= 0:
            return
        tolerance = max(
            self._ABSOLUTE_DURATION_TOLERANCE_SECONDS,
            expected * self._RELATIVE_DURATION_TOLERANCE,
        )
        if abs(actual - expected) > tolerance:
            raise MediaVerificationError("Media duration conflicts with platform duration")

    def _verify_complete_decode(self, media_path: Path, stream: str) -> None:
        decoded = self._run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(media_path),
                "-map",
                stream,
                "-f",
                "null",
                "-",
            ]
        )
        if decoded.returncode != 0:
            raise MediaVerificationError("Media stream cannot be completely decoded")

    def _run(self, command: Sequence[str]) -> CommandResult:
        try:
            return self.runner.run(command)
        except OSError as error:
            raise MediaVerificationError("Required media tool is unavailable") from error
