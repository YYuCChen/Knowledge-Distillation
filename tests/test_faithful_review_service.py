import pytest

from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    get_task,
    initialize_database,
)
from knowledge_distiller.faithful_review import (
    FaithfulReview,
    FaithfulReviewCandidate,
    ReviewConcern,
    ReviewFailure,
)
from knowledge_distiller.legacy.faithful_review_service import (
    ReviewProcessingKind,
    review_task_primary,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery


class StaticReviewer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def review(self, recovery):
        self.calls.append(recovery)
        return self.result


class SequenceReviewer:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    def review(self, recovery):
        self.calls.append(recovery)
        return next(self.results)


def identified_task(database_path):
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            platform="douyin",
            platform_item_id="stable-work-1",
            original_url="https://v.douyin.com/example/",
            canonical_url="https://www.douyin.com/video/stable-work-1",
        ),
    )
    return task_id


def recovery():
    text = "这是已经完整结束的 Primary 口播恢复文本，保留了来源中的主要表达。"
    return PrimaryRecovery(
        text=text,
        language="Chinese",
        chunks=(PrimaryChunk(text, 0.0, 10.0, "Chinese"),),
    )


def successful_review():
    text = recovery().text
    start = text.index("主要表达")
    return FaithfulReview.succeeded(
        FaithfulReviewCandidate(
            text,
            (
                ReviewConcern(
                    start,
                    start + 4,
                    "主要表达",
                    "局部字面仍值得回听。",
                    False,
                ),
            ),
        )
    )


def test_review_candidate_remains_temporary_source_boundary_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    reviewer = StaticReviewer(successful_review())

    result = review_task_primary(database_path, task_id, recovery(), reviewer)

    assert result.kind is ReviewProcessingKind.READY
    assert result.candidate.text == recovery().text
    assert len(result.candidate.concerns) == 1
    assert reviewer.calls == [recovery()]
    assert decide_next_boundary(database_path, task_id) is NextBoundary.SOURCE_FACT_PRODUCTION
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
        columns = {
            row[1]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
    assert not {
        "review_checkpoint",
        "review_raw_response",
        "review_prompt",
        "candidate_snapshot",
        "review_retry_count",
    } & columns


def test_invalid_output_is_retried_once_and_valid_second_result_continues(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    reviewer = SequenceReviewer(
        FaithfulReview.failed(ReviewFailure.INVALID_OUTPUT),
        successful_review(),
    )

    result = review_task_primary(database_path, task_id, recovery(), reviewer)

    assert result.kind is ReviewProcessingKind.READY
    assert result.candidate == successful_review().candidate
    assert reviewer.calls == [recovery(), recovery()]
    task = get_task(database_path, task_id)
    assert task["last_failure_boundary"] is None
    assert task["last_failure_reason"] is None
    assert task["waiting_boundary"] is None
    assert task["waiting_reason"] is None
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "failure,kind,reason,waiting_reason,expected_calls",
    [
        (
            ReviewFailure.RUNTIME_UNAVAILABLE,
            ReviewProcessingKind.WAITING,
            None,
            "faithful_review_unavailable",
            1,
        ),
        (
            ReviewFailure.RUNTIME_FAILED,
            ReviewProcessingKind.FAILED,
            "review_runtime_failed",
            None,
            1,
        ),
        (
            ReviewFailure.INCOMPLETE,
            ReviewProcessingKind.FAILED,
            "review_incomplete",
            None,
            1,
        ),
        (
            ReviewFailure.INSUFFICIENT_COVERAGE,
            ReviewProcessingKind.FAILED,
            "review_insufficient_coverage",
            None,
            1,
        ),
        (
            ReviewFailure.INVALID_OUTPUT,
            ReviewProcessingKind.FAILED,
            "review_invalid_output",
            None,
            2,
        ),
    ],
)
def test_review_failures_do_not_create_source_fact(
    tmp_path,
    failure,
    kind,
    reason,
    waiting_reason,
    expected_calls,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    reviewer = StaticReviewer(FaithfulReview.failed(failure))

    result = review_task_primary(
        database_path,
        task_id,
        recovery(),
        reviewer,
    )

    assert result.kind is kind
    assert reviewer.calls == [recovery()] * expected_calls
    task = get_task(database_path, task_id)
    assert task["last_failure_reason"] == reason
    assert task["waiting_reason"] == waiting_reason
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_review_requires_identified_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")

    with pytest.raises(ValueError, match="confirmed material"):
        review_task_primary(
            database_path,
            task_id,
            recovery(),
            StaticReviewer(successful_review()),
        )
