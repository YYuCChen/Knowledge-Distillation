from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    establish_knowledge_result,
    get_task,
    get_task_current_knowledge_result,
    get_task_current_source_fact,
    record_task_failure,
    record_task_waiting,
)
from ..knowledge_derivation import (
    KnowledgeCandidate,
    knowledge_candidate_payload,
    validate_knowledge_candidate,
)
from .knowledge_qualification import (
    KnowledgeQualifier,
    QualificationFailure,
    QualificationIssue,
)


class KnowledgeResultProcessingKind(StrEnum):
    ESTABLISHED = "established"
    REJECTED = "rejected"
    WAITING = "waiting"
    FAILED = "failed"


@dataclass(frozen=True)
class KnowledgeResultProcessingResult:
    task_id: int
    kind: KnowledgeResultProcessingKind
    knowledge_result_id: int | None = None
    created: bool = False
    issues: tuple[QualificationIssue, ...] = ()


def qualify_and_establish_knowledge_result(
    database_path: Path,
    task_id: int,
    source_fact_id: int,
    candidate: KnowledgeCandidate,
    qualifier: KnowledgeQualifier,
) -> KnowledgeResultProcessingResult:
    task = get_task(database_path, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} does not exist")
    source_fact = get_task_current_source_fact(database_path, task_id)
    if source_fact is None:
        raise ValueError("Task has no current SourceFact")
    if int(source_fact["source_fact_id"]) != source_fact_id:
        raise ValueError("SourceFact does not belong to task current material")

    existing = get_task_current_knowledge_result(database_path, task_id)
    if existing is not None:
        if int(existing["source_fact_id"]) != source_fact_id:
            raise ValueError("Current KnowledgeResult uses another SourceFact")
        return KnowledgeResultProcessingResult(
            task_id,
            KnowledgeResultProcessingKind.ESTABLISHED,
            int(existing["knowledge_result_id"]),
            False,
        )

    snapshot = str(source_fact["content_snapshot"])
    if not validate_knowledge_candidate(source_fact_id, snapshot, candidate):
        record_task_failure(
            database_path,
            task_id,
            "knowledge_derivation",
            "knowledge_candidate_invalid",
        )
        return KnowledgeResultProcessingResult(
            task_id,
            KnowledgeResultProcessingKind.REJECTED,
            issues=(
                QualificationIssue(None, "候选知识结构或来源证据不完整。"),
            ),
        )

    uncertainties = json.loads(source_fact["uncertainty_json"])
    if not isinstance(uncertainties, list) or any(
        not isinstance(item, dict) for item in uncertainties
    ):
        raise ValueError("SourceFact uncertainty payload is invalid")
    qualification = qualifier.qualify(
        source_fact_id,
        snapshot,
        uncertainties,
        candidate,
    )
    if qualification.failure is QualificationFailure.RUNTIME_UNAVAILABLE:
        record_task_waiting(
            database_path,
            task_id,
            "knowledge_derivation",
            "knowledge_qualification_unavailable",
        )
        return KnowledgeResultProcessingResult(
            task_id,
            KnowledgeResultProcessingKind.WAITING,
        )
    if qualification.failure is not None:
        record_task_failure(
            database_path,
            task_id,
            "knowledge_derivation",
            f"knowledge_qualification_{qualification.failure.value}",
        )
        return KnowledgeResultProcessingResult(
            task_id,
            KnowledgeResultProcessingKind.FAILED,
        )
    if not qualification.qualified:
        record_task_failure(
            database_path,
            task_id,
            "knowledge_derivation",
            "knowledge_candidate_rejected",
        )
        return KnowledgeResultProcessingResult(
            task_id,
            KnowledgeResultProcessingKind.REJECTED,
            issues=qualification.issues,
        )

    creation = establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        knowledge_candidate_payload(candidate),
    )
    return KnowledgeResultProcessingResult(
        task_id,
        KnowledgeResultProcessingKind.ESTABLISHED,
        creation.knowledge_result_id,
        creation.created,
    )
