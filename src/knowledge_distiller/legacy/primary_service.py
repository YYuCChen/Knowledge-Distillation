from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    clear_task_diagnostics,
    get_task,
    get_task_material_identity,
    record_task_source_failure,
)
from ..media import VerifiedTemporaryMedia
from ..primary import (
    AudioNormalizer,
    PrimaryRecognizer,
    PrimaryRecovery,
    StandardAudio,
)


class PrimaryProcessingKind(StrEnum):
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class PrimaryProcessingResult:
    task_id: int
    kind: PrimaryProcessingKind
    audio: StandardAudio | None = None
    recovery: PrimaryRecovery | None = None


def recover_task_primary(
    database_path: Path,
    task_id: int,
    media: VerifiedTemporaryMedia,
    normalizer: AudioNormalizer,
    recognizer: PrimaryRecognizer,
    runtime_root: Path,
) -> PrimaryProcessingResult:
    task = get_task(database_path, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} does not exist")
    identity = get_task_material_identity(database_path, task_id)
    if identity is None:
        raise ValueError("Task has no confirmed material identity")
    if (
        media.platform != identity.platform
        or media.platform_item_id != identity.platform_item_id
    ):
        raise ValueError("Verified media does not belong to task material")

    work_dir = (
        runtime_root
        / "tasks"
        / str(task_id)
        / "source-fact"
        / _safe_runtime_segment(identity.platform)
        / _safe_runtime_segment(identity.platform_item_id)
        / "primary"
    )
    normalization = normalizer.normalize(media, work_dir)
    if normalization.failure is not None:
        record_task_source_failure(
            database_path,
            task_id,
            f"primary_audio_{normalization.failure.value}",
        )
        return PrimaryProcessingResult(task_id, PrimaryProcessingKind.FAILED)
    if normalization.audio is None:
        raise AssertionError("Successful audio normalization has no audio")

    recognition = recognizer.recognize(normalization.audio)
    if recognition.failure is not None:
        record_task_source_failure(
            database_path,
            task_id,
            f"primary_{recognition.failure.value}",
        )
        return PrimaryProcessingResult(task_id, PrimaryProcessingKind.FAILED)
    if recognition.recovery is None:
        raise AssertionError("Successful Primary recognition has no recovery")

    clear_task_diagnostics(database_path, task_id)
    return PrimaryProcessingResult(
        task_id,
        PrimaryProcessingKind.READY,
        normalization.audio,
        recognition.recovery,
    )


def _safe_runtime_segment(value: str) -> str:
    if not value or any(
        not (character.isalnum() or character in "-_") for character in value
    ):
        raise ValueError("Material identity cannot be used for runtime isolation")
    return value
