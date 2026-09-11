import json
import sqlite3
from dataclasses import replace

import pytest

from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_source_fact,
    get_task,
    initialize_database,
    utc_now,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.knowledge_derivation import (
    KnowledgeCandidate,
    KnowledgeEvidence,
    KnowledgePoint,
    knowledge_candidate_payload,
)
from knowledge_distiller.legacy.knowledge_qualification import (
    KnowledgeQualification,
    QualificationFailure,
    QualificationIssue,
)
from knowledge_distiller.legacy.knowledge_result_service import (
    KnowledgeResultProcessingKind,
    qualify_and_establish_knowledge_result,
)
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary


SNAPSHOT = "医院先上报采购量，企业中选后按约定供应。"


class StaticQualifier:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def qualify(self, source_fact_id, snapshot, uncertainties, candidate):
        self.calls.append((source_fact_id, snapshot, uncertainties, candidate))
        return self.result


def task_with_source_fact(database_path, *, item_id="work-1", uncertainties=None):
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
        uncertainties or [],
    )
    return task_id, creation.source_fact_id


def candidate(source_fact_id):
    evidence_text = SNAPSHOT
    return KnowledgeCandidate(
        "集采从报量进入履约",
        "医院先报量，企业中选后按约定供应。",
        (
            KnowledgePoint(
                "p1",
                "医院报量是采购安排的起点。",
                "医院先报量，企业中选后再按约定供应。",
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


def test_qualified_candidate_atomically_establishes_knowledge_result(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)
    candidate_value = candidate(source_fact_id)
    qualifier = StaticQualifier(KnowledgeQualification.passed())
    with connect(database_path) as connection:
        source_before = dict(connection.execute("SELECT * FROM source_facts").fetchone())

    result = qualify_and_establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        candidate_value,
        qualifier,
    )

    assert result.kind is KnowledgeResultProcessingKind.ESTABLISHED
    assert result.created is True
    with connect(database_path) as connection:
        knowledge = connection.execute(
            "SELECT * FROM knowledge_results"
        ).fetchone()
        material = connection.execute("SELECT * FROM materials").fetchone()
        source_after = dict(connection.execute("SELECT * FROM source_facts").fetchone())
    assert int(knowledge["knowledge_result_id"]) == result.knowledge_result_id
    assert int(knowledge["source_fact_id"]) == source_fact_id
    assert json.loads(knowledge["payload_json"]) == knowledge_candidate_payload(
        candidate_value
    )
    assert material["current_knowledge_result_id"] == result.knowledge_result_id
    assert material["current_source_fact_id"] == source_fact_id
    assert source_after == source_before
    assert decide_next_boundary(database_path, task_id) is NextBoundary.OBSIDIAN_PUBLISHING


@pytest.mark.parametrize(
    "invalid_candidate",
    [
        lambda value: replace(value, title=""),
        lambda value: replace(value, summary=""),
        lambda value: replace(value, core_points=()),
        lambda value: replace(
            value,
            core_points=(replace(value.core_points[0], argument=""),),
        ),
        lambda value: replace(
            value,
            core_points=(replace(value.core_points[0], evidence_ids=()),),
        ),
        lambda value: replace(
            value,
            evidence_registry=(
                replace(value.evidence_registry[0], end_offset=3),
            ),
        ),
    ],
)
def test_incomplete_or_invalid_candidate_is_rejected_before_semantic_review(
    tmp_path,
    invalid_candidate,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)
    qualifier = StaticQualifier(KnowledgeQualification.passed())

    result = qualify_and_establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        invalid_candidate(candidate(source_fact_id)),
        qualifier,
    )

    assert result.kind is KnowledgeResultProcessingKind.REJECTED
    assert qualifier.calls == []
    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "reason",
    [
        "证据只与主题相关，不能支持观点中的条件和结论。",
        "观点把来源中的局部不确定内容升级成确定结论。",
    ],
)
def test_semantic_rejection_never_creates_knowledge_result(tmp_path, reason):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(
        database_path,
        uncertainties=[
            {
                "start": 0,
                "end": 2,
                "text": "医院",
                "reason": "来源局部存在不确定性。",
                "meaning_may_change": False,
                "candidate_readings": [],
            }
        ],
    )
    issue = QualificationIssue("p1", reason)
    qualifier = StaticQualifier(KnowledgeQualification.rejected((issue,)))

    result = qualify_and_establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        candidate(source_fact_id),
        qualifier,
    )

    assert result.kind is KnowledgeResultProcessingKind.REJECTED
    assert result.issues == (issue,)
    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_knowledge_result_id FROM materials"
        ).fetchone()[0] is None
    assert get_task(database_path, task_id)["last_failure_reason"] == (
        "knowledge_candidate_rejected"
    )


@pytest.mark.parametrize(
    "qualification,kind,waiting_reason,failure_reason",
    [
        (
            KnowledgeQualification.failed(QualificationFailure.RUNTIME_UNAVAILABLE),
            KnowledgeResultProcessingKind.WAITING,
            "knowledge_qualification_unavailable",
            None,
        ),
        (
            KnowledgeQualification.failed(QualificationFailure.RUNTIME_FAILED),
            KnowledgeResultProcessingKind.FAILED,
            None,
            "knowledge_qualification_runtime_failed",
        ),
    ],
)
def test_qualification_failures_preserve_source_fact(
    tmp_path,
    qualification,
    kind,
    waiting_reason,
    failure_reason,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)

    result = qualify_and_establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        candidate(source_fact_id),
        StaticQualifier(qualification),
    )

    assert result.kind is kind
    task = get_task(database_path, task_id)
    assert task["waiting_reason"] == waiting_reason
    assert task["last_failure_reason"] == failure_reason
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 0


def test_repeated_execution_reuses_current_result_without_requalification(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)
    candidate_value = candidate(source_fact_id)
    first_qualifier = StaticQualifier(KnowledgeQualification.passed())
    first = qualify_and_establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        candidate_value,
        first_qualifier,
    )
    second_qualifier = StaticQualifier(
        KnowledgeQualification.rejected(
            (QualificationIssue("p1", "不应再次审查。"),)
        )
    )

    second = qualify_and_establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        candidate_value,
        second_qualifier,
    )

    assert second.kind is KnowledgeResultProcessingKind.ESTABLISHED
    assert second.created is False
    assert second.knowledge_result_id == first.knowledge_result_id
    assert second_qualifier.calls == []
    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 1


def test_wrong_source_fact_is_rejected(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    first_task, first_source = task_with_source_fact(database_path, item_id="work-1")
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
        qualify_and_establish_knowledge_result(
            database_path,
            first_task,
            second_source,
            candidate(second_source),
            StaticQualifier(KnowledgeQualification.passed()),
        )
    assert first_source != second_source


def test_result_and_current_pointer_rollback_together(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)
    with connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_knowledge_pointer
            BEFORE UPDATE OF current_knowledge_result_id ON materials
            BEGIN
                SELECT RAISE(ABORT, 'test pointer failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test pointer failure"):
        qualify_and_establish_knowledge_result(
            database_path,
            task_id,
            source_fact_id,
            candidate(source_fact_id),
            StaticQualifier(KnowledgeQualification.passed()),
        )

    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_knowledge_result_id FROM materials"
        ).fetchone()[0] is None


def test_knowledge_result_content_and_source_binding_are_immutable(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, source_fact_id = task_with_source_fact(database_path)
    result = qualify_and_establish_knowledge_result(
        database_path,
        task_id,
        source_fact_id,
        candidate(source_fact_id),
        StaticQualifier(KnowledgeQualification.passed()),
    )

    with connect(database_path) as connection:
        for statement, parameters in (
            (
                "UPDATE knowledge_results SET payload_json = '{}' WHERE knowledge_result_id = ?",
                (result.knowledge_result_id,),
            ),
            (
                "UPDATE knowledge_results SET source_fact_id = ? WHERE knowledge_result_id = ?",
                (source_fact_id, result.knowledge_result_id),
            ),
            (
                "DELETE FROM knowledge_results WHERE knowledge_result_id = ?",
                (result.knowledge_result_id,),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
        connection.execute(
            """
            UPDATE knowledge_results
            SET published_at = ?, published_path = ?
            WHERE knowledge_result_id = ?
            """,
            (utc_now(), "知识/结果.md", result.knowledge_result_id),
        )


def test_knowledge_result_adds_no_candidate_or_qualification_checkpoint(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)

    with connect(database_path) as connection:
        columns = {
            row[1]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    assert not {
        "knowledge_candidate",
        "derivation_raw_response",
        "derivation_prompt",
        "qualification_response",
        "qualification_checkpoint",
        "evidence_checkpoint",
    } & columns
