import json

import pytest

from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_source_fact,
    get_task,
    initialize_database,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.knowledge_derivation import (
    DerivationFailure,
    KnowledgeCandidate,
    KnowledgeDerivation,
    KnowledgeEvidence,
    KnowledgePoint,
)
from knowledge_distiller.legacy.knowledge_derivation_service import (
    DerivationProcessingKind,
    derive_task_knowledge,
)
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary


SNAPSHOT = "药品集采先由医院报量，企业中选后按约定供应。"


class StaticDeriver:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def derive(self, source_fact_id, snapshot, uncertainties):
        self.calls.append((source_fact_id, snapshot, uncertainties))
        return self.result


def task_with_source_fact(database_path, item_id="work-1"):
    initialize_database(database_path)
    task_id = create_task(database_path, f"https://v.douyin.com/{item_id}/")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            "douyin",
            item_id,
            f"https://v.douyin.com/{item_id}/",
            f"https://www.douyin.com/video/{item_id}",
        ),
    )
    creation = establish_source_fact(
        database_path,
        task_id,
        {"platform": "douyin", "platform_item_id": item_id},
        SNAPSHOT,
        [],
    )
    return task_id, creation.source_fact_id


def candidate(source_fact_id):
    evidence_text = "药品集采先由医院报量"
    return KnowledgeCandidate(
        "药品集采从报量进入履约",
        "医院先报量，企业中选后按约定供应。",
        (
            KnowledgePoint(
                "p1",
                "医院报量是集采采购安排的起点。",
                "来源先说明医院报量，随后才进入企业中选和供应。",
                ("e1",),
            ),
        ),
        (),
        (
            KnowledgeEvidence(
                "e1",
                source_fact_id,
                0,
                len(evidence_text),
                evidence_text,
            ),
        ),
    )


def test_source_fact_produces_temporary_candidate_without_knowledge_result(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)
    deriver = StaticDeriver(
        KnowledgeDerivation.succeeded(candidate(source_fact_id))
    )
    with connect(database_path) as connection:
        source_before = dict(connection.execute("SELECT * FROM source_facts").fetchone())

    result = derive_task_knowledge(
        database_path,
        task_id,
        source_fact_id,
        deriver,
    )

    assert result.kind is DerivationProcessingKind.READY
    assert result.candidate.title == "药品集采从报量进入履约"
    assert deriver.calls == [(source_fact_id, SNAPSHOT, [])]
    with connect(database_path) as connection:
        source_after = dict(connection.execute("SELECT * FROM source_facts").fetchone())
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    assert source_after == source_before
    assert decide_next_boundary(database_path, task_id) is NextBoundary.KNOWLEDGE_DERIVATION


@pytest.mark.parametrize(
    "failure,kind,failure_reason,waiting_reason",
    [
        (
            DerivationFailure.RUNTIME_UNAVAILABLE,
            DerivationProcessingKind.WAITING,
            None,
            "knowledge_derivation_unavailable",
        ),
        (
            DerivationFailure.RUNTIME_FAILED,
            DerivationProcessingKind.FAILED,
            "knowledge_derivation_runtime_failed",
            None,
        ),
        (
            DerivationFailure.INVALID_OUTPUT,
            DerivationProcessingKind.FAILED,
            "knowledge_derivation_invalid_output",
            None,
        ),
    ],
)
def test_derivation_failures_do_not_create_knowledge_result(
    tmp_path,
    failure,
    kind,
    failure_reason,
    waiting_reason,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)

    result = derive_task_knowledge(
        database_path,
        task_id,
        source_fact_id,
        StaticDeriver(KnowledgeDerivation.failed(failure)),
    )

    assert result.kind is kind
    task = get_task(database_path, task_id)
    assert task["last_failure_boundary"] == (
        "knowledge_derivation" if failure_reason else None
    )
    assert task["last_failure_reason"] == failure_reason
    assert task["waiting_boundary"] == (
        "knowledge_derivation" if waiting_reason else None
    )
    assert task["waiting_reason"] == waiting_reason
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1


def test_derivation_requires_current_source_fact(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")

    with pytest.raises(ValueError, match="no current SourceFact"):
        derive_task_knowledge(
            database_path,
            task_id,
            1,
            StaticDeriver(KnowledgeDerivation.succeeded(candidate(1))),
        )


def test_derivation_rejects_source_fact_from_another_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    first_task, first_source = task_with_source_fact(database_path, "work-1")
    second_task = create_task(database_path, "https://v.douyin.com/work-2/")
    attach_task_to_material(
        database_path,
        second_task,
        ConfirmedMaterialIdentity(
            "douyin",
            "work-2",
            "https://v.douyin.com/work-2/",
            "https://www.douyin.com/video/work-2",
        ),
    )
    second_source = establish_source_fact(
        database_path,
        second_task,
        {"platform": "douyin", "platform_item_id": "work-2"},
        SNAPSHOT,
        [],
    ).source_fact_id

    with pytest.raises(ValueError, match="does not belong"):
        derive_task_knowledge(
            database_path,
            first_task,
            second_source,
            StaticDeriver(
                KnowledgeDerivation.succeeded(candidate(second_source))
            ),
        )
    assert first_source != second_source


def test_derivation_adds_no_checkpoint_columns(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)

    with connect(database_path) as connection:
        columns = {
            row[1]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    assert not {
        "derivation_checkpoint",
        "derivation_raw_response",
        "derivation_prompt",
        "knowledge_candidate",
        "draft_payload",
    } & columns
