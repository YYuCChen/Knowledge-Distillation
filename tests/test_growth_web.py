from __future__ import annotations

import json
import threading
import time

import pytest

import knowledge_distiller.legacy.web as web_module
import knowledge_distiller.organization_service as organization_service_module
from knowledge_distiller.database import connect
from knowledge_distiller.growth_modeling import (
    GrowthRuntimeResult,
    HistoricalRecallAdapter,
    RelationInsightAdapter,
)
from knowledge_distiller.organization_models import EventStatus
from knowledge_distiller.organization_service import OrganizationReadError, read_event
from knowledge_distiller.topic_indexing import TopicIndexing, TopicPlan
from knowledge_distiller.topic_library import (
    TopicLibrary,
    TopicLibrarySnapshot,
    TopicRefreshKind,
    TopicRefreshResult,
)
from knowledge_distiller.legacy.web import create_app
from tests.fixtures.growth import (
    add_formal_knowledge,
    capture_service_growth_qualification,
    empty_growth_plan_payload,
    insight_payload,
    relation_payload,
    source_participant,
)


class Runtime:
    def __init__(self, payload, *, available=True, stop_reason="end_turn"):
        self.payload = payload
        self.available = available
        self.stop_reason = stop_reason
        self.calls = []

    def is_available(self):
        return self.available

    def complete(self, *, system_prompt, input_payload, max_tokens):
        self.calls.append(input_payload)
        return GrowthRuntimeResult(json.dumps(self.payload), self.stop_reason)


class BlockingRuntime(Runtime):
    def __init__(self, payload):
        super().__init__(payload)
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, *, system_prompt, input_payload, max_tokens):
        self.calls.append(input_payload)
        self.entered.set()
        assert self.release.wait(timeout=5)
        return GrowthRuntimeResult(json.dumps(self.payload), self.stop_reason)


class EmptyIndexer:
    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def organize(self, points, existing_topics):
        self.calls += 1
        return TopicIndexing.succeeded(
            TopicPlan((), tuple(point.reference for point in points))
        )


class BaselineChangingIndexer(EmptyIndexer):
    def __init__(self, database_path):
        super().__init__()
        self.database_path = database_path
        self.changed = False

    def organize(self, points, existing_topics):
        if not self.changed:
            self.changed = True
            result = TopicLibrary(self.database_path, EmptyIndexer()).refresh(force=True)
            assert result.kind is TopicRefreshKind.REFRESHED
        return super().organize(points, existing_topics)


class BaselineNoticeLibrary:
    def refresh(self, *, force=False):
        return TopicRefreshResult(TopicRefreshKind.BASELINE_CHANGED)

    def snapshot(self):
        return TopicLibrarySnapshot((), True, True, False, 0, 0)

    def indexer_available(self):
        return True


def recall_payload():
    return {
        "codec": "historical-recall-v1",
        "source_knowledge_ids": [],
        "accepted_insight_version_ids": [],
        "current_relation_version_ids": [],
        "reconsideration_hint_version_ids": [],
    }


def productive_plan(*, claim="A and B reveal a narrower boundary"):
    payload = empty_growth_plan_payload()
    payload["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to formal result",
        }
        for value in (1, 2)
    ]
    payload["new_relations"] = [
        {
            "new_relation_key": "r-new",
            "target_kind": "create_identity",
            "payload": relation_payload(),
            "participants": [
                source_participant("a", 1, position=0),
                source_participant("b", 2, position=1),
            ],
            "used_relations": [],
        }
    ]
    payload["candidate_versions"] = [
        {
            "new_insight_key": "i-new",
            "target_kind": "create_identity",
            "payload": insight_payload(claim=claim),
            "participants": [
                source_participant("a", 1, position=0),
                source_participant("b", 2, position=1),
            ],
            "used_relations": [
                {
                    "ref_kind": "planned_stable",
                    "new_relation_key": "r-new",
                    "role_text": "Explains the candidate connection",
                }
            ],
        }
    ]
    payload["rejected_outputs"] = []
    return payload


def growth_app(path, *, plan=None, indexer=None, growth_available=True):
    recall_runtime = Runtime(recall_payload())
    growth_runtime = Runtime(
        plan or empty_growth_plan_payload(), available=growth_available
    )
    app = create_app(
        path,
        topic_indexer=indexer or EmptyIndexer(),
        historical_recall_planner=HistoricalRecallAdapter(recall_runtime),
        relation_insight_planner=RelationInsightAdapter(growth_runtime),
    )
    app.config.update(TESTING=True)
    return app, recall_runtime, growth_runtime


def test_create_app_production_composition_builds_real_growth_adapters(tmp_path):
    app = create_app(tmp_path / "composition.sqlite3", topic_indexer=EmptyIndexer())
    service = app.config["ORGANIZATION_SERVICE"]

    assert isinstance(service.recall_planner, HistoricalRecallAdapter)
    assert isinstance(service.relation_insight_planner, RelationInsightAdapter)
    assert service.topic_indexer is app.config["TOPIC_LIBRARY"].indexer


def test_post_runs_normal_adapter_zero_plan_and_redirects_to_same_event(tmp_path):
    """Normal end_turn→strict codec→qualification can prove a legal zero result."""
    path = tmp_path / "zero-web.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    app, recall_runtime, growth_runtime = growth_app(path)

    response = app.test_client().post(
        "/knowledge/organization-events", data={"intent": "start"}
    )

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/knowledge/organization-events/1")
    feedback = app.test_client().get(response.headers["Location"])
    assert feedback.status_code == 200
    assert "2 份新知识已整理" in feedback.text
    assert "个主题发生实质变化" not in feedback.text
    assert "条新知候选等待查看" not in feedback.text
    assert len(recall_runtime.calls) == len(growth_runtime.calls) == 1
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 2


def test_empty_post_creates_no_event_and_returns_human_feedback(tmp_path):
    path = tmp_path / "empty-web.sqlite3"
    app, _, _ = growth_app(path)

    response = app.test_client().post(
        "/knowledge/organization-events", data={"intent": "start"}
    )

    assert response.status_code == 303
    assert "organization_notice=empty" in response.headers["Location"]
    feedback = app.test_client().get(response.headers["Location"])
    assert "当前没有尚未覆盖的新知识" in feedback.text
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_events"
        ).fetchone()[0] == 0


def test_provider_failure_redirects_to_failed_event_and_never_looks_like_zero(tmp_path):
    path = tmp_path / "provider-failed-web.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    app, _, _ = growth_app(path, growth_available=False)

    response = app.test_client().post(
        "/knowledge/organization-events", data={"intent": "start"}
    )
    feedback = app.test_client().get(response.headers["Location"])

    assert response.status_code == 303
    assert "这次整理没有完成" in feedback.text
    assert "份新知识已整理" not in feedback.text
    assert "provider" not in feedback.text.lower()
    assert "sql" not in feedback.text.lower()
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0


def test_qualification_code_is_logged_only_and_never_exposed_by_web(tmp_path):
    path = tmp_path / "qualification-code-private.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    plan = productive_plan()
    plan["new_relations"] = []
    plan["candidate_versions"][0]["used_relations"] = [
        {
            "ref_kind": "boundary_current",
            "relation_version_id": 999,
            "role_text": "Strictly decoded but outside the boundary",
        }
    ]
    app, _, growth_runtime = growth_app(path, plan=plan)

    response = app.test_client().post(
        "/knowledge/organization-events", data={"intent": "start"}
    )
    feedback = app.test_client().get(response.headers["Location"])

    assert response.status_code == 303
    assert feedback.status_code == 200
    assert "这次整理没有完成" in feedback.text
    assert "invalid_boundary_current" not in feedback.text
    assert "qualification" not in feedback.text.lower()
    assert len(growth_runtime.calls) == 1
    with connect(path) as connection:
        for table in (
            "relation_versions",
            "insight_versions",
            "topics",
            "organization_event_coverages",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0


def test_topic_assessment_observer_code_is_never_exposed_by_web(tmp_path):
    path = tmp_path / "topic-assessment-code-private.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    plan = empty_growth_plan_payload()
    plan["topic_change_assessments"] = [
        {
            "topic_ref": "topic:999",
            "changed": True,
            "reason_text": "Invalid extra assessment with no label-only diff",
        }
    ]
    app, _, growth_runtime = growth_app(path, plan=plan)

    with capture_service_growth_qualification() as capture:
        response = app.test_client().post(
            "/knowledge/organization-events", data={"intent": "start"}
        )
    feedback = app.test_client().get(response.headers["Location"])

    assert response.status_code == 303
    assert feedback.status_code == 200
    assert "这次整理没有完成" in feedback.text
    assert "unknown_topic_assessment" not in feedback.text
    assert "topic_changes" not in feedback.text
    assert "qualification" not in feedback.text.lower()
    assert capture.call_count == 1
    assert capture.error_code is None
    assert capture.topic_change_stage == "topic_changes"
    assert capture.topic_change_call_count == 1
    assert capture.topic_change_error_code == "unknown_topic_assessment"
    assert len(growth_runtime.calls) == 1
    with connect(path) as connection:
        for table in (
            "relation_versions",
            "insight_versions",
            "topics",
            "organization_event_coverages",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0


def test_manual_topic_baseline_conflict_returns_409_without_partial_result(tmp_path):
    path = tmp_path / "baseline-web.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    app, _, _ = growth_app(path, indexer=BaselineChangingIndexer(path))

    response = app.test_client().post(
        "/knowledge/organization-events", data={"intent": "start"}
    )

    assert response.status_code == 409
    assert "主题基线已经变化" in response.text
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM relation_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 0


def test_event_and_candidate_gets_are_read_only_and_never_drive_models(tmp_path):
    """E2E-09/E2E-42: GET renders frozen state without drive or model calls."""
    path = tmp_path / "read-only-web.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    app, recall_runtime, growth_runtime = growth_app(path)
    event_id = app.config["ORGANIZATION_SERVICE"].start_or_reuse().event_id

    event_response = app.test_client().get(
        f"/knowledge/organization-events/{event_id}"
    )
    candidate_response = app.test_client().get("/knowledge/insight-candidates")

    assert event_response.status_code == candidate_response.status_code == 200
    assert "上次运行已中断" in event_response.text
    assert "整理可以继续" in event_response.text
    assert "暂时没有待判断的新知候选" in candidate_response.text
    assert recall_runtime.calls == growth_runtime.calls == []
    assert read_event(path, event_id).status is EventStatus.RUNNING
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0


def test_active_driver_get_reports_waiting_without_retry_or_database_write(tmp_path):
    path = tmp_path / "active-driver-web.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    recall_runtime = Runtime(recall_payload())
    growth_runtime = BlockingRuntime(empty_growth_plan_payload())
    app = create_app(
        path,
        topic_indexer=EmptyIndexer(),
        historical_recall_planner=HistoricalRecallAdapter(recall_runtime),
        relation_insight_planner=RelationInsightAdapter(growth_runtime),
    )
    app.config.update(TESTING=True)
    holder = {}

    def post_organization():
        with app.test_client() as client:
            holder["response"] = client.post(
                "/knowledge/organization-events", data={"intent": "start"}
            )

    thread = threading.Thread(target=post_organization)
    thread.start()
    assert growth_runtime.entered.wait(timeout=5)
    with connect(path) as connection:
        event_id = int(
            connection.execute(
                "SELECT event_id FROM organization_events WHERE status = 'running'"
            ).fetchone()[0]
        )
        before = (
            connection.execute(
                "SELECT COUNT(*) FROM organization_event_coverages"
            ).fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0],
        )

    active = app.test_client().get(
        f"/knowledge/organization-events/{event_id}"
    )

    assert active.status_code == 200
    assert "正在整理，请等待" in active.text
    assert "无需重复发起" in active.text
    assert "继续整理" not in active.text
    assert len(growth_runtime.calls) == 1
    with connect(path) as connection:
        after = (
            connection.execute(
                "SELECT COUNT(*) FROM organization_event_coverages"
            ).fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0],
        )
    assert after == before

    growth_runtime.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert holder["response"].status_code == 303
    terminal = app.test_client().get(holder["response"].headers["Location"])
    assert "2 份新知识已整理" in terminal.text


def test_event_view_snapshot_never_turns_commit_race_into_recoverable(tmp_path, monkeypatch):
    path = tmp_path / "event-view-snapshot-race.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    recall_runtime = Runtime(recall_payload())
    growth_runtime = BlockingRuntime(empty_growth_plan_payload())
    app = create_app(
        path,
        topic_indexer=EmptyIndexer(),
        historical_recall_planner=HistoricalRecallAdapter(recall_runtime),
        relation_insight_planner=RelationInsightAdapter(growth_runtime),
    )
    app.config.update(TESTING=True)
    post_holder = {}

    def post_organization():
        with app.test_client() as client:
            post_holder["response"] = client.post(
                "/knowledge/organization-events", data={"intent": "start"}
            )

    post_thread = threading.Thread(target=post_organization, name="organization-post")
    post_thread.start()
    assert growth_runtime.entered.wait(timeout=5)
    with connect(path) as connection:
        event_id = int(
            connection.execute(
                "SELECT event_id FROM organization_events WHERE status = 'running'"
            ).fetchone()[0]
        )
    stale_running = read_event(path, event_id)
    snapshot_entered = threading.Event()
    database_terminal = threading.Event()
    real_read_event = organization_service_module.read_event

    def coordinated_read_event(database_path, requested_event_id):
        if threading.current_thread().name == "snapshot-get":
            snapshot_entered.set()
            assert database_terminal.wait(timeout=5)
            return stale_running
        return real_read_event(database_path, requested_event_id)

    monkeypatch.setattr(
        organization_service_module, "read_event", coordinated_read_event
    )
    get_holder = {}

    def get_event():
        with app.test_client() as client:
            get_holder["response"] = client.get(
                f"/knowledge/organization-events/{event_id}"
            )

    get_thread = threading.Thread(target=get_event, name="snapshot-get")
    get_thread.start()
    assert snapshot_entered.wait(timeout=5)
    growth_runtime.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connect(path) as connection:
            status = connection.execute(
                "SELECT status FROM organization_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
        if status == "succeeded":
            break
        time.sleep(0.01)
    assert status == "succeeded"
    database_terminal.set()
    get_thread.join(timeout=5)
    post_thread.join(timeout=5)

    assert not get_thread.is_alive()
    assert not post_thread.is_alive()
    response = get_holder["response"]
    assert response.status_code == 200
    assert "正在整理，请等待" in response.text
    assert "上次运行已中断" not in response.text
    assert len(growth_runtime.calls) == 1


def test_candidate_list_escapes_payload_and_distinguishes_one_bad_record(tmp_path):
    path = tmp_path / "candidate-web.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    app, _, _ = growth_app(
        path, plan=productive_plan(claim='<script>alert("x")</script>')
    )
    response = app.test_client().post(
        "/knowledge/organization-events", data={"intent": "start"}
    )
    assert response.status_code == 303

    listed = app.test_client().get("/knowledge/insight-candidates")
    assert listed.status_code == 200
    assert "<script>" not in listed.text
    assert "&lt;script&gt;alert" in listed.text

    with connect(path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET payload_json = '{broken candidate'"
        )
    degraded = app.test_client().get("/knowledge/insight-candidates")
    assert degraded.status_code == 200
    assert "有 1 条候选记录当前不完整" in degraded.text
    assert "暂时没有待判断的新知候选" in degraded.text


def test_candidate_overall_read_failure_is_not_rendered_as_normal_empty(
    tmp_path, monkeypatch
):
    app, _, _ = growth_app(tmp_path / "candidate-read-failed.sqlite3")

    def fail(_database_path):
        raise OrganizationReadError("private SQL detail")

    monkeypatch.setattr(web_module, "list_pending_candidates", fail)
    response = app.test_client().get("/knowledge/insight-candidates")

    assert response.status_code == 500
    assert "候选暂时无法读取" in response.text
    assert "private SQL detail" not in response.text


def test_topic_refresh_baseline_changed_maps_to_explicit_notice(tmp_path):
    app, _, _ = growth_app(tmp_path / "topic-notice.sqlite3")
    app.config["TOPIC_LIBRARY"] = BaselineNoticeLibrary()

    response = app.test_client().post("/knowledge/topics/refresh")
    feedback = app.test_client().get(response.headers["Location"])

    assert response.status_code == 303
    assert "topic_notice=baseline_changed" in response.headers["Location"]
    assert "整理期间主题基线发生了变化" in feedback.text


def test_organization_routes_validate_form_and_missing_event(tmp_path):
    app, _, _ = growth_app(tmp_path / "route-errors.sqlite3")
    client = app.test_client()

    assert client.post("/knowledge/organization-events").status_code == 400
    assert client.get("/knowledge/organization-events/999").status_code == 404
