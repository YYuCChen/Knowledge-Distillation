from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    clear_task_diagnostics,
    get_task,
    get_task_material_identity,
    record_task_source_failure,
    record_task_waiting_for_login,
)
from ..media import (
    MaterialMediaAcquirer,
    MediaAcquisitionKind,
    MediaFailure,
    VerifiedTemporaryMedia,
)


class MediaProcessingKind(StrEnum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    FAILED = "failed"


@dataclass(frozen=True)
class MediaProcessingResult:
    task_id: int
    kind: MediaProcessingKind
    acquisition_kind: MediaAcquisitionKind | None = None
    media: VerifiedTemporaryMedia | None = None


def prepare_task_media(
    database_path: Path,
    task_id: int,
    acquirer: MaterialMediaAcquirer,
    runtime_root: Path,
) -> MediaProcessingResult:
    task = get_task(database_path, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} does not exist")
    identity = get_task_material_identity(database_path, task_id)
    if identity is None:
        raise ValueError("Task has no confirmed material identity")

    platform_segment = _safe_runtime_segment(identity.platform)
    item_segment = _safe_runtime_segment(identity.platform_item_id)

    work_dir = (
        runtime_root
        / "tasks"
        / str(task_id)
        / "source-fact"
        / platform_segment
        / item_segment
    )
    acquisition = acquirer.acquire(
        identity,
        identity.canonical_url,
        work_dir,
    )
    if acquisition.failure is MediaFailure.LOGIN_REQUIRED:
        record_task_waiting_for_login(database_path, task_id)
        return MediaProcessingResult(task_id, MediaProcessingKind.LOGIN_REQUIRED)
    if acquisition.failure is not None:
        record_task_source_failure(
            database_path,
            task_id,
            f"media_{acquisition.failure.value}",
        )
        return MediaProcessingResult(task_id, MediaProcessingKind.FAILED)

    if acquisition.kind is None or acquisition.media is None:
        raise AssertionError("Successful media acquisition has no media")
    clear_task_diagnostics(database_path, task_id)
    return MediaProcessingResult(
        task_id,
        MediaProcessingKind.READY,
        acquisition.kind,
        acquisition.media,
    )


def _safe_runtime_segment(value: str) -> str:
    if not value or any(
        not (character.isalnum() or character in "-_") for character in value
    ):
        raise ValueError("Material identity cannot be used for runtime isolation")
    return value
