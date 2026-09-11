from __future__ import annotations

import json
import threading

from knowledge_distiller.database import connect
from knowledge_distiller.growth_modeling import (
    GrowthRuntimeResult,
    HistoricalRecallAdapter,
    RelationInsightAdapter,
)
from knowledge_distiller.organization_models import EventStatus, OrganizationFailureCode
from knowledge_distiller.organization_service import OrganizationService
from knowledge_distiller.topic_indexing import TopicDraft, TopicIndexing, TopicPlan
from knowledge_distiller.topic_library import (
    TopicLibrary,
    TopicRefreshKind,
    build_topic_overwrite_guard,
)
from tests.fixtures.growth import add_formal_knowledge, empty_growth_plan_payload


class Runtime:
    def __init__(self, payload):
        self.payload = payload

    def is_available(self):
        return True

    def complete(self, *, system_prompt, input_payload, max_tokens):
        return GrowthRuntimeResult(json.dumps(self.payload), "end_turn")


class EmptyIndexer:
    def __init__(self, *, entered=None, release=None):
        self.entered = entered
        self.release = release
        self.calls = 0

    def is_available(self):
        return True

    def organize(self, points, existing_topics):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        return TopicIndexing.succeeded(
            TopicPlan((), tuple(point.reference for point in points))
        )


class AllPointsIndexer(EmptyIndexer):
    def organize(self, points, existing_topics):
        self.calls += 1
        topic_id = existing_topics[0].topic_id if existing_topics else None
        return TopicIndexing.succeeded(
            TopicPlan(
                (
                    TopicDraft(
                        topic_id,
                        None if topic_id else "all",
                        "共同导航",
                        "收纳全部测试观点。",
                        tuple(point.reference for point in points),
                    ),
                ),
                (),
            )
        )


def service(path, *, indexer=None):
    return OrganizationService(
        path,
        topic_indexer=indexer or EmptyIndexer(),
        recall_planner=HistoricalRecallAdapter(
            Runtime(
                {
                    "codec": "historical-recall-v1",
                    "source_knowledge_ids": [],
                    "accepted_insight_version_ids": [],
                    "current_relation_version_ids": [],
                    "reconsideration_hint_version_ids": [],
                }
            )
        ),
        relation_insight_planner=RelationInsightAdapter(
            Runtime(empty_growth_plan_payload())
        ),
    )


def database(tmp_path):
    path = tmp_path / "topic-concurrency.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    return path


def test_refresh_first_changes_guard_and_manual_fails_without_partial_results(tmp_path):
    """E2E-04: refresh legal-empty commit wins; late manual fails baseline."""
    path = database(tmp_path)
    manual = service(path)
    event_id = manual.start_or_reuse().event_id

    refresh = TopicLibrary(path, EmptyIndexer()).refresh(force=True)
    result = manual.drive(event_id)

    assert refresh.kind is TopicRefreshKind.REFRESHED
    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.TOPIC_BASELINE_CHANGED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM relation_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 0


def test_manual_first_changes_guard_and_late_refresh_fails_without_retry(tmp_path):
    """E2E-05: manual wins; refresh detects baseline change and calls model once."""
    path = database(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    refresh_indexer = EmptyIndexer(entered=entered, release=release)
    refresh_library = TopicLibrary(path, refresh_indexer)
    holder = {}

    thread = threading.Thread(
        target=lambda: holder.setdefault("result", refresh_library.refresh(force=True))
    )
    thread.start()
    assert entered.wait(timeout=5)
    manual = service(path)
    event_id = manual.start_or_reuse().event_id
    manual_result = manual.drive(event_id)
    release.set()
    thread.join(timeout=5)

    assert manual_result.event.status is EventStatus.SUCCEEDED
    assert holder["result"].kind is TopicRefreshKind.BASELINE_CHANGED
    assert refresh_indexer.calls == 1
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 2


def test_same_canonical_guard_does_not_create_false_manual_conflict(tmp_path):
    """E2E-06: indexed_at-only refresh is the same canonical guard."""
    path = database(tmp_path)
    refresh_library = TopicLibrary(path, EmptyIndexer())
    assert refresh_library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    manual = service(path)
    event_id = manual.start_or_reuse().event_id

    assert refresh_library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    result = manual.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    assert result.event.failure_code is None


def test_source_only_later_c_does_not_change_guard_without_refresh(tmp_path):
    """E2E-02/06: later uncovered C changes live staleness, not overwrite guard."""
    path = database(tmp_path)
    manual = service(path)
    event_id = manual.start_or_reuse().event_id
    _, kr_c = add_formal_knowledge(path, "c")

    result = manual.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE knowledge_result_id = ?",
            (kr_c,),
        ).fetchone()[0] == 0


def test_guard_uses_stale_safe_intersection_and_hides_single_member_topic(tmp_path):
    """E2E-07/E2E-08: raw history remains; guard exposes only safe 2+ subset."""
    path = tmp_path / "stale-guard.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    add_formal_knowledge(path, "c")
    assert TopicLibrary(path, AllPointsIndexer()).refresh(force=True).kind is TopicRefreshKind.REFRESHED
    with connect(path) as connection:
        connection.execute(
            "UPDATE knowledge_results SET invalidated_at='invalid-c' WHERE knowledge_result_id=3"
        )
        guard = build_topic_overwrite_guard(connection)
        assert guard["committed_shape"] == "has_topics"
        assert {
            member["knowledge_result_id"]
            for member in guard["safe_topics"][0]["members"]
        } == {1, 2}
        connection.execute(
            "UPDATE knowledge_results SET invalidated_at='invalid-b' WHERE knowledge_result_id=2"
        )
        guard = build_topic_overwrite_guard(connection)
        assert guard["committed_shape"] == "has_topics"
        assert guard["safe_topics"] == []
