from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    attach_task_to_material,
    get_task,
    record_task_identity_failure,
    record_task_waiting_for_login,
)
from ..identity import IdentityFailure, MaterialIdentityResolver


class IdentityProcessingKind(StrEnum):
    CONFIRMED = "confirmed"
    LOGIN_REQUIRED = "login_required"
    FAILED = "failed"


@dataclass(frozen=True)
class IdentityProcessingResult:
    task_id: int
    kind: IdentityProcessingKind


def confirm_task_identity(
    database_path: Path,
    task_id: int,
    resolver: MaterialIdentityResolver,
) -> IdentityProcessingResult:
    task = get_task(database_path, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} does not exist")
    if task["material_id"] is not None:
        return IdentityProcessingResult(task_id, IdentityProcessingKind.CONFIRMED)

    submitted_url = str(task["submitted_url"])
    resolution = resolver.identify(submitted_url, submitted_url)
    if resolution.failure is IdentityFailure.LOGIN_REQUIRED:
        record_task_waiting_for_login(database_path, task_id)
        return IdentityProcessingResult(task_id, IdentityProcessingKind.LOGIN_REQUIRED)
    if resolution.failure is not None:
        record_task_identity_failure(database_path, task_id, resolution.failure.value)
        return IdentityProcessingResult(task_id, IdentityProcessingKind.FAILED)

    if resolution.identity is None:
        raise AssertionError("Confirmed identity resolution has no identity")
    attachment = attach_task_to_material(database_path, task_id, resolution.identity)
    return IdentityProcessingResult(
        attachment.task_id,
        IdentityProcessingKind.CONFIRMED,
    )
