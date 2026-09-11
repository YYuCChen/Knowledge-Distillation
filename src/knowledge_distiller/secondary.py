from __future__ import annotations

import json
import math
import os
import time
import uuid
import wave
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .media import CommandRunner, SubprocessCommandRunner
from .primary import StandardAudio


SEED_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
SEED_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
SEED_RESOURCE_ID = "volc.seedasr.auc"
_SEED_SUCCESS = "20000000"
_SEED_PENDING = {"20000001", "20000002"}


@dataclass(frozen=True)
class SecondaryAudio:
    path: Path
    source_start_seconds: float
    source_end_seconds: float
    duration_seconds: float


class SecondaryFailure(StrEnum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    RUNTIME_FAILED = "runtime_failed"
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True)
class SecondaryResolution:
    transcript: str | None = None
    failure: SecondaryFailure | None = None

    def __post_init__(self) -> None:
        if (self.transcript is None) == (self.failure is None):
            raise ValueError("Secondary resolution must contain one result")

    @classmethod
    def succeeded(cls, transcript: str) -> SecondaryResolution:
        return cls(transcript=transcript)

    @classmethod
    def failed(cls, failure: SecondaryFailure) -> SecondaryResolution:
        return cls(failure=failure)


class SecondaryResolver(Protocol):
    @property
    def available(self) -> bool: ...

    def resolve(self, audio: SecondaryAudio) -> SecondaryResolution: ...


class SecondaryAudioError(Exception):
    pass


class LocalAudioClipper(Protocol):
    def clip(
        self,
        audio: StandardAudio,
        start_seconds: float,
        end_seconds: float,
        output_path: Path,
    ) -> SecondaryAudio: ...


class FFmpegLocalAudioClipper:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or SubprocessCommandRunner()

    def clip(
        self,
        audio: StandardAudio,
        start_seconds: float,
        end_seconds: float,
        output_path: Path,
    ) -> SecondaryAudio:
        if (
            not isinstance(audio, StandardAudio)
            or not audio.path.is_file()
            or audio.path.stat().st_size <= 0
            or not math.isfinite(start_seconds)
            or not math.isfinite(end_seconds)
            or start_seconds < 0
            or end_seconds <= start_seconds
            or end_seconds > audio.duration_seconds + 0.05
        ):
            raise SecondaryAudioError("Secondary audio range is invalid")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(".tmp.wav")
        temporary_path.unlink(missing_ok=True)
        try:
            converted = self.runner.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    f"{start_seconds:.3f}",
                    "-i",
                    str(audio.path),
                    "-t",
                    f"{end_seconds - start_seconds:.3f}",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(temporary_path),
                ]
            )
            if converted.returncode != 0:
                raise SecondaryAudioError("Secondary audio extraction failed")
            duration = _inspect_clip(temporary_path)
            if abs(duration - (end_seconds - start_seconds)) > 0.25:
                raise SecondaryAudioError("Secondary audio duration is invalid")
            os.replace(temporary_path, output_path)
        except (OSError, ValueError, wave.Error):
            temporary_path.unlink(missing_ok=True)
            raise SecondaryAudioError("Secondary audio extraction failed") from None
        return SecondaryAudio(output_path, start_seconds, end_seconds, duration)


@dataclass(frozen=True)
class SeedAsrConfig:
    api_key: str
    tos_access_key_id: str
    tos_secret_access_key: str
    tos_endpoint: str
    tos_region: str
    tos_bucket: str
    tos_object_prefix: str

    @property
    def ready(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.api_key,
                self.tos_access_key_id,
                self.tos_secret_access_key,
                self.tos_endpoint,
                self.tos_region,
                self.tos_bucket,
                self.tos_object_prefix,
            )
        )


class SeedAsrSecondaryResolver:
    def __init__(
        self,
        config: SeedAsrConfig,
        *,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 600.0,
    ):
        self.config = config
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        if not self.config.ready:
            return False
        try:
            import httpx  # noqa: F401
            import tos  # noqa: F401
        except ImportError:
            return False
        return True

    def resolve(self, audio: SecondaryAudio) -> SecondaryResolution:
        if not self.available:
            return SecondaryResolution.failed(SecondaryFailure.RUNTIME_UNAVAILABLE)
        try:
            import httpx
            import tos
        except ImportError:
            return SecondaryResolution.failed(SecondaryFailure.RUNTIME_UNAVAILABLE)
        try:
            if not audio.path.is_file() or audio.path.stat().st_size <= 0:
                return SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)
        except OSError:
            return SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)

        try:
            client = tos.TosClientV2(
                self.config.tos_access_key_id,
                self.config.tos_secret_access_key,
                self.config.tos_endpoint,
                self.config.tos_region,
            )
        except Exception:
            return SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)
        object_key = (
            self.config.tos_object_prefix.rstrip("/")
            + "/knowledge-distiller-secondary/"
            + uuid.uuid4().hex
            + ".wav"
        )
        uploaded = False
        result = SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)
        try:
            with audio.path.open("rb") as source:
                client.put_object(
                    self.config.tos_bucket,
                    object_key,
                    content_length=audio.path.stat().st_size,
                    content_type="audio/wav",
                    content=source,
                )
            uploaded = True
            signed_url = client.pre_signed_url(
                tos.HttpMethodType.Http_Method_Get,
                self.config.tos_bucket,
                object_key,
                expires=3600,
            ).signed_url
            result = self._recognize(httpx, signed_url)
        except Exception:
            result = SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)
        finally:
            if uploaded:
                try:
                    client.delete_object(self.config.tos_bucket, object_key)
                except Exception:
                    result = SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)
        return result

    def _recognize(self, httpx, signed_url: str) -> SecondaryResolution:
        request_id = str(uuid.uuid4())
        payload = {
            "audio": {
                "url": signed_url,
                "format": "wav",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": False,
                "enable_punc": False,
                "enable_ddc": False,
                "show_utterances": True,
                "enable_speaker_info": False,
                "enable_channel_split": False,
                "vad_segment": False,
            },
        }
        submit = httpx.post(
            SEED_SUBMIT_URL,
            headers=seed_headers(self.config.api_key, request_id, submit=True),
            json=payload,
            timeout=30.0,
        )
        if submit.headers.get("X-Api-Status-Code") != _SEED_SUCCESS:
            return SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)
        try:
            submit_body = submit.json()
            if not isinstance(submit_body, dict):
                raise TypeError
            task_id = str(submit_body.get("task_id") or request_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            return SecondaryResolution.failed(SecondaryFailure.INVALID_OUTPUT)

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            query = httpx.post(
                SEED_QUERY_URL,
                headers=seed_headers(self.config.api_key, task_id, submit=False),
                json={},
                timeout=30.0,
            )
            status = query.headers.get("X-Api-Status-Code")
            if status == _SEED_SUCCESS:
                try:
                    transcript = query.json()["result"]["text"]
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    return SecondaryResolution.failed(SecondaryFailure.INVALID_OUTPUT)
                if not isinstance(transcript, str) or not transcript.strip():
                    return SecondaryResolution.failed(SecondaryFailure.INVALID_OUTPUT)
                return SecondaryResolution.succeeded(transcript.strip())
            if status not in _SEED_PENDING:
                return SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)
            time.sleep(self.poll_interval_seconds)
        return SecondaryResolution.failed(SecondaryFailure.RUNTIME_FAILED)


def build_seed_secondary_resolver() -> SeedAsrSecondaryResolver:
    return SeedAsrSecondaryResolver(
        SeedAsrConfig(
            api_key=os.environ.get("KNOWLEDGE_DISTILLER_SEED_API_KEY", ""),
            tos_access_key_id=os.environ.get(
                "KNOWLEDGE_DISTILLER_TOS_ACCESS_KEY_ID", ""
            ),
            tos_secret_access_key=os.environ.get(
                "KNOWLEDGE_DISTILLER_TOS_SECRET_ACCESS_KEY", ""
            ),
            tos_endpoint=os.environ.get("KNOWLEDGE_DISTILLER_TOS_ENDPOINT", ""),
            tos_region=os.environ.get("KNOWLEDGE_DISTILLER_TOS_REGION", ""),
            tos_bucket=os.environ.get("KNOWLEDGE_DISTILLER_TOS_BUCKET", ""),
            tos_object_prefix=os.environ.get(
                "KNOWLEDGE_DISTILLER_TOS_OBJECT_PREFIX", ""
            ),
        )
    )


def seed_headers(api_key: str, request_id: str, *, submit: bool) -> dict[str, str]:
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": SEED_RESOURCE_ID,
        "X-Api-Request-Id": request_id,
        "Content-Type": "application/json",
    }
    if submit:
        headers["X-Api-Sequence"] = "-1"
    return headers


def _inspect_clip(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getnchannels() != 1
            or audio.getframerate() != 16_000
            or audio.getsampwidth() != 2
            or audio.getnframes() <= 0
        ):
            raise ValueError("Secondary audio format is invalid")
        return audio.getnframes() / audio.getframerate()
