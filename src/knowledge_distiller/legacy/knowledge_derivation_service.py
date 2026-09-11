from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    clear_task_diagnostics,
    get_task,
    get_task_current_source_fact,
    record_task_failure,
    record_task_waiting,
)
from ..knowledge_derivation import (
    DerivationFailure,
    KnowledgeCandidate,
    KnowledgeDeriver,
)


class DerivationProcessingKind(StrEnum):
    READY = "ready"
    WAITING = "waiting"
    FAILED = "failed"


@dataclass(frozen=True)
class DerivationProcessingResult:
    task_id: int
    kind: DerivationProcessingKind
    candidate: KnowledgeCandidate | None = None


def derive_task_knowledge(
    database_path: Path,
    task_id: int,
    source_fact_id: int,
    deriver: KnowledgeDeriver,
) -> DerivationProcessingResult:
    task = get_task(database_path, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} does not exist")
    source_fact = get_task_current_source_fact(database_path, task_id)
    if source_fact is None:
        raise ValueError("Task has no current SourceFact")
    if int(source_fact["source_fact_id"]) != source_fact_id:
        raise ValueError("SourceFact does not belong to task current material")

    uncertainties = json.loads(source_fact["uncertainty_json"])
    if not isinstance(uncertainties, list):
        raise ValueError("SourceFact uncertainty payload is invalid")
    derivation = deriver.derive(
        source_fact_id,
        str(source_fact["content_snapshot"]),
        uncertainties,
    )
    if derivation.failure is DerivationFailure.RUNTIME_UNAVAILABLE:
        record_task_waiting(
            database_path,
            task_id,
            "knowledge_derivation",
            "knowledge_derivation_unavailable",
        )
        return DerivationProcessingResult(task_id, DerivationProcessingKind.WAITING)
    if derivation.failure is not None:
        record_task_failure(
            database_path,
            task_id,
            "knowledge_derivation",
            f"knowledge_derivation_{derivation.failure.value}",
        )
        return DerivationProcessingResult(task_id, DerivationProcessingKind.FAILED)
    if derivation.candidate is None:
        raise AssertionError("Successful derivation has no candidate")

    clear_task_diagnostics(database_path, task_id)
    return DerivationProcessingResult(
        task_id,
        DerivationProcessingKind.READY,
        derivation.candidate,
    )
