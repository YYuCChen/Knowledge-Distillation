from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    clear_task_diagnostics,
    get_task,
    get_task_material_identity,
    record_task_source_failure,
    record_task_waiting_for_source_condition,
)
from ..faithful_review import (
    FaithfulReviewCandidate,
    FaithfulReviewer,
    ReviewFailure,
)
from ..primary import PrimaryRecovery


class ReviewProcessingKind(StrEnum):
    READY = "ready"
    WAITING = "waiting"
    FAILED = "failed"


@dataclass(frozen=True)
class ReviewProcessingResult:
    task_id: int
    kind: ReviewProcessingKind
    candidate: FaithfulReviewCandidate | None = None


def review_task_primary(
    database_path: Path,
    task_id: int,
    recovery: PrimaryRecovery,
    reviewer: FaithfulReviewer,
) -> ReviewProcessingResult:
    task = get_task(database_path, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} does not exist")
    if get_task_material_identity(database_path, task_id) is None:
        raise ValueError("Task has no confirmed material identity")

    review = reviewer.review(recovery)
    if review.failure is ReviewFailure.INVALID_OUTPUT:
        review = reviewer.review(recovery)
    if review.failure is ReviewFailure.RUNTIME_UNAVAILABLE:
        record_task_waiting_for_source_condition(
            database_path,
            task_id,
            "faithful_review_unavailable",
        )
        return ReviewProcessingResult(task_id, ReviewProcessingKind.WAITING)
    if review.failure is not None:
        record_task_source_failure(
            database_path,
            task_id,
            f"review_{review.failure.value}",
        )
        return ReviewProcessingResult(task_id, ReviewProcessingKind.FAILED)
    if review.candidate is None:
        raise AssertionError("Successful faithful review has no candidate")

    clear_task_diagnostics(database_path, task_id)
    return ReviewProcessingResult(
        task_id,
        ReviewProcessingKind.READY,
        review.candidate,
    )
