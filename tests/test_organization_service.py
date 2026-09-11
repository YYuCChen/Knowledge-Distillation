from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest
import knowledge_distiller.organization_service as organization_service_module

from knowledge_distiller.accepted_insight_library import (
    AcceptedDetailKind,
    AcceptedRole,
    list_current_accepted_inputs,
    list_topic_auxiliary_insights,
    read_accepted_detail,
    search_accepted_insights,
)
from knowledge_distiller.database import connect
from knowledge_distiller.growth_modeling import (
    GrowthRuntimeResult,
    HistoricalRecallAdapter,
    RelationInsightAdapter,
)
from knowledge_distiller.insight_judgment_service import InsightJudgmentService
from knowledge_distiller.organization_models import (
    EventStatus,
    GrowthPlan,
    OrganizationFailureCode,
    decode_success_payload,
    encode_success_payload,
    parse_growth_plan,
)
from knowledge_distiller.organization_service import (
    OrganizationDriveKind,
    OrganizationService,
    OrganizationStartKind,
    list_pending_candidates,
    read_event,
)
from knowledge_distiller.topic_indexing import TopicDraft, TopicIndexing, TopicPlan
from knowledge_distiller.topic_library import TopicLibrary, TopicRefreshKind
from knowledge_distiller.relation_insight_store import insert_insight_version
from tests.fixtures.growth import (
    accepted_participant,
    add_formal_knowledge,
    capture_service_growth_qualification,
    empty_growth_plan_payload,
)
from tests.fixtures.growth import (
    insight_payload,
    relation_payload,
    source_participant,
)


class JsonRuntime:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.inputs = []

    def is_available(self):
        return True

    def complete(self, *, system_prompt, input_payload, max_tokens):
        self.calls += 1
        self.inputs.append(input_payload)
        return GrowthRuntimeResult(json.dumps(self.payload), "end_turn")


class EmptyTopicIndexer:
    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def organize(self, points, existing_topics):
        self.calls += 1
        return TopicIndexing.succeeded(
            TopicPlan((), tuple(point.reference for point in points))
        )


class SeedTopicIndexer(EmptyTopicIndexer):
    def organize(self, points, existing_topics):
        self.calls += 1
        return TopicIndexing.succeeded(
            TopicPlan(
                (
                    TopicDraft(
                        None,
                        "seed-topic",
                        "原始主题",
                        "原始范围。",
                        tuple(point.reference for point in points),
                    ),
                ),
                (),
            )
        )


class LabelOnlyTopicIndexer(EmptyTopicIndexer):
    def __init__(self, *, change_labels=True):
        super().__init__()
        self.change_labels = change_labels

    def organize(self, points, existing_topics):
        self.calls += 1
        current = existing_topics[0]
        return TopicIndexing.succeeded(
            TopicPlan(
                (
                    TopicDraft(
                        current.topic_id,
                        None,
                        "新主题名" if self.change_labels else current.name,
                        "新范围。" if self.change_labels else current.scope,
                        current.members,
                    ),
                ),
                (),
            )
        )


def _service(
    path,
    *,
    injector=None,
    plan=None,
    source_ids=(),
    accepted_ids=(),
    current_relation_ids=(),
    hint_ids=(),
    topic_indexer=None,
):
    recall_runtime = JsonRuntime(
        {
            "codec": "historical-recall-v1",
            "source_knowledge_ids": list(source_ids),
            "accepted_insight_version_ids": list(accepted_ids),
            "current_relation_version_ids": list(current_relation_ids),
            "reconsideration_hint_version_ids": list(hint_ids),
        }
    )
    growth_runtime = JsonRuntime(plan or empty_growth_plan_payload())
    service = OrganizationService(
        path,
        topic_indexer=topic_indexer or EmptyTopicIndexer(),
        recall_planner=HistoricalRecallAdapter(recall_runtime),
        relation_insight_planner=RelationInsightAdapter(growth_runtime),
        failure_injector=injector,
    )
    return service, recall_runtime, growth_runtime


def _productive_plan(kr_a=1, kr_b=2):
    payload = empty_growth_plan_payload()
    payload["new_input_reviews"] = [
        {
            "knowledge_result_id": kr_a,
            "outcome": "participated",
            "reason_text": "Contributed to formal result",
        },
        {
            "knowledge_result_id": kr_b,
            "outcome": "participated",
            "reason_text": "Contributed to formal result",
        },
    ]
    payload["new_relations"] = [
        {
            "new_relation_key": "r-new",
            "target_kind": "create_identity",
            "payload": relation_payload(),
            "participants": [
                source_participant("a", kr_a, position=0),
                source_participant("b", kr_b, position=1),
            ],
            "used_relations": [],
        }
    ]
    payload["candidate_versions"] = [
        {
            "new_insight_key": "i-new",
            "target_kind": "create_identity",
            "payload": insight_payload(),
            "participants": [
                source_participant("a", kr_a, position=0),
                source_participant("b", kr_b, position=1),
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
    return payload


def _accepted_candidate_plan(
    accepted_insight_version_id,
    source_knowledge_result_id,
):
    payload = empty_growth_plan_payload()
    payload["new_input_reviews"] = [
        {
            "knowledge_result_id": source_knowledge_result_id,
            "outcome": "participated",
            "reason_text": "Contributed a new independent source basis",
        }
    ]
    payload["candidate_versions"] = [
        {
            "new_insight_key": "nested-candidate",
            "target_kind": "create_identity",
            "payload": insight_payload(
                claim=(
                    "Accepted history and source "
                    f"{source_knowledge_result_id} narrow the boundary"
                )
            ),
            "participants": [
                accepted_participant(
                    "a", accepted_insight_version_id, position=0
                ),
                source_participant(
                    "b", source_knowledge_result_id, position=1
                ),
            ],
            "used_relations": [],
        }
    ]
    payload["rejected_outputs"] = []
    return payload


def _candidate_only_plan(frozen_id, kr_a, kr_b):
    payload = empty_growth_plan_payload()
    payload["new_input_reviews"] = [
        {
            "knowledge_result_id": frozen_id,
            "outcome": "participated",
            "reason_text": "Completed duplicate identity review",
        }
    ]
    candidate = _productive_plan(kr_a, kr_b)["candidate_versions"][0]
    candidate["used_relations"] = []
    payload["candidate_versions"] = [candidate]
    payload["rejected_outputs"] = []
    return payload


def _relation_only_plan(frozen_id, kr_a, kr_b):
    payload = empty_growth_plan_payload()
    payload["new_input_reviews"] = [
        {
            "knowledge_result_id": frozen_id,
            "outcome": "participated",
            "reason_text": "Completed duplicate relation review",
        }
    ]
    payload["new_relations"] = [
        _productive_plan(kr_a, kr_b)["new_relations"][0]
    ]
    payload["rejected_outputs"] = []
    return payload


def _zero_review_plan(*frozen_ids):
    payload = empty_growth_plan_payload()
    payload["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "considered_no_formal_result",
            "reason_text": "Completed full review with no formal increment",
        }
        for value in frozen_ids
    ]
    return payload


def _accepted_disqualification_plan(frozen_id, version_id, fact_kind):
    payload = _zero_review_plan(frozen_id)
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": version_id,
            "fact_kind": fact_kind,
            "reason_text": f"Formal review established {fact_kind}",
        }
    ]
    return payload


def _dual_accepted_disqualification_plan(frozen_id, version_id):
    payload = _zero_review_plan(frozen_id)
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": version_id,
            "fact_kind": fact_kind,
            "reason_text": f"Formal review established {fact_kind}",
        }
        for fact_kind in ("refuted", "basis_invalid")
    ]
    return payload


def _evolved_candidate_plan(
    insight_id,
    previous_version_id,
    kr_a,
    kr_b,
    *,
    claim,
):
    payload = _zero_review_plan(kr_a, kr_b)
    payload["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to the evolved candidate",
        }
        for value in (kr_a, kr_b)
    ]
    payload["candidate_versions"] = [
        {
            "new_insight_key": "evolved-insight",
            "target_kind": "evolve_identity",
            "insight_id": insight_id,
            "previous_insight_version_id": previous_version_id,
            "payload": insight_payload(claim=claim),
            "participants": [
                source_participant("a", kr_a, position=0),
                source_participant("b", kr_b, position=1),
            ],
            "used_relations": [],
        }
    ]
    payload["rejected_outputs"] = []
    return payload


def _insert_competing_candidate(path, event_id, candidate_payload):
    plan_payload = empty_growth_plan_payload()
    plan_payload["candidate_versions"] = [candidate_payload]
    candidate = parse_growth_plan(plan_payload).candidate_versions[0]
    with connect(path) as connection:
        return insert_insight_version(
            connection,
            event_id=event_id,
            plan=candidate,
            dependency_signature="d" * 64,
            created_at="2026-08-21T00:02:00+00:00",
        )


def _accept_event_candidate(path, event_id):
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT insight_id, insight_version_id
            FROM insight_versions
            WHERE produced_event_id = ?
            """,
            (event_id,),
        ).fetchone()
        judgment_id = int(
            connection.execute(
                """
                INSERT INTO user_insight_judgments (
                    insight_id, insight_version_id, decision,
                    annotation_text, decided_at
                ) VALUES (?, ?, 'interesting', NULL, '2026-08-21T00:00:00+00:00')
                """,
                (int(row["insight_id"]), int(row["insight_version_id"])),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id,
                judgment_decision, initial_role, current_role, accepted_at
            ) VALUES (?, ?, ?, 'interesting', 'current', 'current',
                      '2026-08-21T00:01:00+00:00')
            """,
            (int(row["insight_version_id"]), int(row["insight_id"]), judgment_id),
        )
        return int(row["insight_version_id"])


@pytest.fixture
def two_source_database(tmp_path):
    path = tmp_path / "organization.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    return path


def test_empty_input_does_not_create_event_or_call_models(two_source_database):
    """E2E-01: after exact success coverage, EMPTY is a read-only no-event result."""
    service, _, growth_runtime = _service(two_source_database)
    first = service.start_or_reuse()
    assert service.drive(first.event_id).kind is OrganizationDriveKind.SUCCEEDED

    second = service.start_or_reuse()
    assert second.kind is OrganizationStartKind.EMPTY
    assert growth_runtime.calls == 1
    with connect(two_source_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_events"
        ).fetchone()[0] == 1


def test_successful_zero_plan_commits_topic_event_and_exact_coverage_together(
    two_source_database,
):
    """E2E-10/14: completed review may succeed while exploration leaves no relation."""
    service, recall_runtime, growth_runtime = _service(two_source_database)
    started = service.start_or_reuse()
    assert started.kind is OrganizationStartKind.STARTED

    result = service.drive(started.event_id)

    assert result.kind is OrganizationDriveKind.SUCCEEDED
    assert result.event.success is not None
    assert (result.event.success.n, result.event.success.m, result.event.success.k) == (
        2,
        0,
        0,
    )
    assert recall_runtime.calls == growth_runtime.calls == 1
    with connect(two_source_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM relation_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM organization_events WHERE event_id = ?",
            (started.event_id,),
        ).fetchone()[0] == "succeeded"


def test_legacy_success_v1_remains_readable_after_v2_observable_extension(
    two_source_database,
):
    service, _, _ = _service(two_source_database)
    event_id = service.start_or_reuse().event_id
    success = service.drive(event_id).event.success
    legacy = replace(
        success,
        accepted_disqualifications=(),
        codec="organization-success-v1",
    )

    assert decode_success_payload(encode_success_payload(legacy)) == legacy


def test_source_created_after_start_stays_uncovered_for_next_event(two_source_database):
    """E2E-02: C created after freeze is not guessed into plan or coverage."""
    service, _, _ = _service(two_source_database)
    started = service.start_or_reuse()
    _, kr_c = add_formal_knowledge(two_source_database, "c")

    result = service.drive(started.event_id)

    assert result.kind is OrganizationDriveKind.SUCCEEDED
    assert result.event.frozen_new_ids == (1, 2)
    with connect(two_source_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE knowledge_result_id = ?",
            (kr_c,),
        ).fetchone()[0] == 0
    next_start = service.start_or_reuse()
    assert next_start.kind is OrganizationStartKind.STARTED
    assert read_event(two_source_database, next_start.event_id).frozen_new_ids == (kr_c,)


def test_overlapping_starts_reuse_one_event_and_same_process_drives_once(two_source_database):
    """E2E-03: two start/driver requests share one freeze and one model driver."""
    service, recall_runtime, growth_runtime = _service(two_source_database)
    barrier = threading.Barrier(2)

    def start():
        barrier.wait()
        return service.start_or_reuse()

    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = list(pool.map(lambda _: start(), range(2)))
    assert {item.event_id for item in starts} == {starts[0].event_id}
    assert {item.kind for item in starts} == {
        OrganizationStartKind.STARTED,
        OrganizationStartKind.REUSED,
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.drive(starts[0].event_id), range(2)))
    assert all(item.event.status is EventStatus.SUCCEEDED for item in results)
    assert recall_runtime.calls == growth_runtime.calls == 1
    with connect(two_source_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 2


def test_provider_or_codec_failure_never_becomes_zero_success(two_source_database):
    invalid = empty_growth_plan_payload()
    invalid["unknown"] = True
    service, _, _ = _service(two_source_database, plan=invalid)
    started = service.start_or_reuse()

    result = service.drive(started.event_id)

    assert result.kind is OrganizationDriveKind.FAILED
    assert result.event.failure_code is OrganizationFailureCode.INVALID_MODEL_OUTPUT
    with connect(two_source_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0


@pytest.mark.parametrize(
    "crash_point",
    ["after_topic_replace", "after_event_success", "after_coverage"],
)
def test_h_to_l_crash_rolls_back_and_next_explicit_request_reuses_running_event(
    two_source_database, crash_point
):
    """§13.4B: J/K/L crash leaves no partial result and the event remains reusable."""
    crashed = False

    def injector(point, connection):
        nonlocal crashed
        if point == crash_point and not crashed:
            crashed = True
            raise SystemExit("simulated crash")

    service, _, _ = _service(two_source_database, injector=injector)
    started = service.start_or_reuse()
    with pytest.raises(SystemExit, match="simulated crash"):
        service.drive(started.event_id)

    event = read_event(two_source_database, started.event_id)
    assert event.status is EventStatus.RUNNING
    with connect(two_source_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0
    resumed = service.start_or_reuse()
    assert resumed.kind is OrganizationStartKind.REUSED
    assert service.drive(resumed.event_id).event.status is EventStatus.SUCCEEDED


def test_pending_candidate_collection_only_reads_succeeded_unjudged_versions(
    two_source_database,
):
    service, _, _ = _service(two_source_database)
    started = service.start_or_reuse()
    service.drive(started.event_id)

    result = list_pending_candidates(two_source_database)
    assert result.candidates == ()
    assert result.unreadable_count == 0


def test_relation_candidate_lineage_edge_and_coverage_commit_as_one_result(
    two_source_database,
):
    """E2E-19/43: stable relation precedes pending candidate used-edge in H→I."""
    service, _, _ = _service(two_source_database, plan=_productive_plan())
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    assert result.event.success.k == 1
    assert len(result.event.success.relation_local_map) == 1
    assert len(result.event.success.insight_local_map) == 1
    pending = list_pending_candidates(two_source_database)
    assert len(pending.candidates) == 1
    with connect(two_source_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM relation_current").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_version_participants"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_participants"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_used_relations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 2


def test_second_round_exact_candidate_semantic_cannot_create_a_new_identity(tmp_path):
    """Cross-round exact pending duplicate is rejected before identity creation."""
    path = tmp_path / "candidate-duplicate.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        insight_version_id = int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions WHERE produced_event_id = ?",
                (first_event,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, 'basis_invalid', ?, 'Source basis was invalidated',
                      '2026-08-21T00:03:00+00:00')
            """,
            (insight_version_id, first_event),
        )

    _, kr_c = add_formal_knowledge(path, "c")
    second, _, second_runtime = _service(
        path,
        plan=_candidate_only_plan(kr_c, kr_a, kr_b),
        source_ids=(kr_a, kr_b),
    )
    second_event = second.start_or_reuse().event_id
    result = second.drive(second_event)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    catalog = second_runtime.inputs[0]["identity_catalog"]
    assert catalog["insight_versions"][0]["state"] == "pending"
    assert catalog["insight_versions"][0]["permanent_facts"] == ["basis_invalid"]
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_identities").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(DISTINCT semantic_signature) FROM insight_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (second_event,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("output_kind", ["relation", "candidate"])
def test_same_plan_exact_semantic_duplicate_rolls_back_every_output(
    two_source_database, output_kind
):
    """Same-plan duplicate local targets fail with no Topic/output/coverage commit."""
    payload = _productive_plan()
    if output_kind == "relation":
        duplicate = copy.deepcopy(payload["new_relations"][0])
        duplicate["new_relation_key"] = "r-duplicate"
        payload["new_relations"].append(duplicate)
        payload["candidate_versions"] = []
    else:
        duplicate = copy.deepcopy(payload["candidate_versions"][0])
        duplicate["new_insight_key"] = "i-duplicate"
        payload["candidate_versions"].append(duplicate)
    service, _, _ = _service(two_source_database, plan=payload)
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(two_source_database) as connection:
        for table in (
            "relation_versions",
            "relation_version_participants",
            "insight_versions",
            "insight_version_participants",
            "topics",
            "organization_event_coverages",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_identity_catalog_drift_before_lock_fails_and_rolls_back_event(tmp_path):
    """Two-connection catalog drift becomes DEPENDENCY_CHANGED with no event output."""
    path = tmp_path / "candidate-catalog-drift.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path)
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED

    _, kr_c = add_formal_knowledge(path, "c")
    payload = _candidate_only_plan(kr_c, kr_a, kr_b)
    competing_candidate = payload["candidate_versions"][0]
    injected = False

    def injector(point, connection):
        nonlocal injected
        if point == "before_begin_immediate" and not injected:
            injected = True
            _insert_competing_candidate(path, first_event, competing_candidate)

    second, _, _ = _service(
        path,
        plan=payload,
        injector=injector,
        source_ids=(kr_a, kr_b),
    )
    second_event = second.start_or_reuse().event_id
    result = second.drive(second_event)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.DEPENDENCY_CHANGED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (second_event,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (second_event,),
        ).fetchone()[0] == 0


def test_cross_identity_same_semantic_catalog_is_explicit_read_failure(tmp_path):
    path = tmp_path / "cross-identity-semantic-corruption.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    competing = _productive_plan(kr_a, kr_b)["candidate_versions"][0]
    competing["used_relations"] = []
    _insert_competing_candidate(path, first_event, competing)
    service, recall_runtime, growth_runtime = _service(
        path, plan=_zero_review_plan(kr_c, kr_d)
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.INPUT_READ_FAILED
    assert recall_runtime.calls == 0
    assert growth_runtime.calls == 0
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_identities").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_same_identity_basis_catalog_drift_is_dependency_changed(tmp_path):
    path = tmp_path / "basis-catalog-drift.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        original = connection.execute(
            "SELECT insight_id, insight_version_id FROM insight_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    bridge, _, _ = _service(path, plan=_zero_review_plan(kr_c, kr_d))
    bridge_event = bridge.start_or_reuse().event_id
    assert bridge.drive(bridge_event).event.status is EventStatus.SUCCEEDED

    _, kr_e = add_formal_knowledge(path, "e")
    _, kr_f = add_formal_knowledge(path, "f")
    competing = _productive_plan(kr_c, kr_d)["candidate_versions"][0]
    competing.update(
        target_kind="evolve_identity",
        insight_id=int(original["insight_id"]),
        previous_insight_version_id=int(original["insight_version_id"]),
    )
    competing["used_relations"] = []
    injected = False

    def injector(point, connection):
        nonlocal injected
        if point == "before_begin_immediate" and not injected:
            injected = True
            _insert_competing_candidate(path, bridge_event, competing)

    service, _, growth_runtime = _service(
        path,
        plan=_zero_review_plan(kr_e, kr_f),
        injector=injector,
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.DEPENDENCY_CHANGED
    assert growth_runtime.calls == 1
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_identities").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_same_event_two_evolutions_of_one_insight_fail_in_qualification(tmp_path):
    path = tmp_path / "same-insight-evolution.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan())
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        existing = connection.execute(
            "SELECT insight_id, insight_version_id FROM insight_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    payload = _zero_review_plan(kr_c, kr_d)
    candidates = []
    for key, claim in (
        ("first-evolution", "C and D establish the first changed boundary"),
        ("second-evolution", "C and D establish a different changed boundary"),
    ):
        candidates.append(
            {
                "new_insight_key": key,
                "target_kind": "evolve_identity",
                "insight_id": int(existing["insight_id"]),
                "previous_insight_version_id": int(
                    existing["insight_version_id"]
                ),
                "payload": insight_payload(claim=claim),
                "participants": [
                    source_participant("a", kr_c, position=0),
                    source_participant("b", kr_d, position=1),
                ],
                "used_relations": [],
            }
        )
    payload["candidate_versions"] = candidates
    service, _, _ = _service(path, plan=payload)
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("forbidden_action", ["evolve", "replace_again"])
def test_permanently_replaced_identity_cannot_revive_or_be_replaced_again(
    tmp_path, forbidden_action
):
    path = tmp_path / f"replaced-identity-{forbidden_action}.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan())
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        old = connection.execute(
            "SELECT insight_id, insight_version_id FROM insight_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    replacement_plan = _zero_review_plan(kr_c, kr_d)
    replacement_plan["candidate_versions"] = [
        {
            "new_insight_key": "replacement",
            "target_kind": "create_identity",
            "replaces_insight_id": int(old["insight_id"]),
            "payload": insight_payload(claim="C and D replace the old core identity"),
            "participants": [
                source_participant("a", kr_c, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [],
        }
    ]
    replacement, _, _ = _service(path, plan=replacement_plan)
    replacement_event = replacement.start_or_reuse().event_id
    assert replacement.drive(replacement_event).event.status is EventStatus.SUCCEEDED
    pending_after_replacement = list_pending_candidates(path)
    assert len(pending_after_replacement.candidates) == 1
    assert pending_after_replacement.candidates[0].insight_id != int(old["insight_id"])

    _, kr_e = add_formal_knowledge(path, "e")
    _, kr_f = add_formal_knowledge(path, "f")
    forbidden_plan = _zero_review_plan(kr_e, kr_f)
    forbidden_candidate = {
        "new_insight_key": "forbidden",
        "target_kind": "create_identity",
        "payload": insight_payload(claim="E and F attempt a forbidden old identity action"),
        "participants": [
            source_participant("a", kr_e, position=0),
            source_participant("b", kr_f, position=1),
        ],
        "used_relations": [],
    }
    if forbidden_action == "evolve":
        forbidden_candidate.update(
            target_kind="evolve_identity",
            insight_id=int(old["insight_id"]),
            previous_insight_version_id=int(old["insight_version_id"]),
        )
    else:
        forbidden_candidate["replaces_insight_id"] = int(old["insight_id"])
    forbidden_plan["candidate_versions"] = [forbidden_candidate]
    service, _, _ = _service(path, plan=forbidden_plan)
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    assert len(list_pending_candidates(path).candidates) == 1
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_recursive_accepted_lineage_is_preserved_in_success_and_participants(tmp_path):
    """E2E-38: nested accepted inputs retain intermediate nodes and source leaves."""
    path = tmp_path / "recursive-accepted.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    initial, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = initial.start_or_reuse().event_id
    assert initial.drive(first_event).event.status is EventStatus.SUCCEEDED
    accepted_first = _accept_event_candidate(path, first_event)

    _, kr_c = add_formal_knowledge(path, "c")
    second, _, _ = _service(
        path,
        plan=_accepted_candidate_plan(accepted_first, kr_c),
        accepted_ids=(accepted_first,),
    )
    second_event = second.start_or_reuse().event_id
    assert second.drive(second_event).event.status is EventStatus.SUCCEEDED
    accepted_second = _accept_event_candidate(path, second_event)

    _, kr_d = add_formal_knowledge(path, "d")
    third, _, third_growth_runtime = _service(
        path,
        plan=_accepted_candidate_plan(accepted_second, kr_d),
        accepted_ids=(accepted_second,),
    )
    third_event = third.start_or_reuse().event_id
    result = third.drive(third_event)

    assert result.event.status is EventStatus.SUCCEEDED
    assert result.event.success.final_required_accepted == (accepted_second,)
    expanded = third_growth_runtime.inputs[0]["expanded_inputs"]
    assert [
        item["insight_version_id"]
        for item in expanded["accepted_current"]
    ] == [accepted_second]
    selected = expanded["accepted_current"][0]
    assert selected["produced_event_id"] == second_event
    assert selected["payload"]["short_discussion"]
    assert selected["payload"]["limitations"] == [
        {"kind": "boundary", "text": "Only the stated cases"}
    ]
    assert selected["payload"]["required_premises"]
    lineage = {
        item["insight_version_id"]: item
        for item in selected["recursive_lineage"]
    }
    assert set(lineage) == {accepted_first, accepted_second}
    assert lineage[accepted_first]["produced_event_id"] == first_event
    assert lineage[accepted_second]["produced_event_id"] == second_event
    with connect(path) as connection:
        first_relation = connection.execute(
            """
            SELECT relation_id, relation_version_id, version_no
            FROM relation_versions WHERE produced_event_id = ?
            """,
            (first_event,),
        ).fetchone()
    assert lineage[accepted_first]["used_relations"] == [
        {
            "relation_version_id": int(first_relation["relation_version_id"]),
            "relation_id": int(first_relation["relation_id"]),
            "version_no": int(first_relation["version_no"]),
            "produced_event_id": first_event,
            "position": 0,
            "role_text": "Explains the candidate connection",
            "payload": relation_payload(),
            "authority": "historical_basis_edge_only",
        }
    ]
    assert lineage[accepted_second]["participants"][0][
        "resolved_accepted"
    ] == {
        "insight_version_id": accepted_first,
        "resolution": "recursive_lineage_node",
    }
    assert lineage[accepted_second]["participants"][1]["resolved_source"][
        "point"
    ]["statement"]
    assert {
        (
            item["knowledge_result_id"],
            item["point_id"],
        )
        for item in selected["source_leaves"]
    } == {(kr_a, "p1"), (kr_b, "p1"), (kr_c, "p1")}
    closure = dict(result.event.success.recursive_dependency_closure)
    assert set(closure) == {accepted_first, accepted_second}
    assert {
        (leaf.knowledge_result_id, leaf.point_id)
        for leaf in closure[accepted_first]
    } == {(kr_a, "p1"), (kr_b, "p1")}
    assert {
        (leaf.knowledge_result_id, leaf.point_id)
        for leaf in closure[accepted_second]
    } == {(kr_a, "p1"), (kr_b, "p1"), (kr_c, "p1")}
    with connect(path) as connection:
        participants = connection.execute(
            """
            SELECT input_kind, accepted_insight_version_id, knowledge_result_id
            FROM insight_version_participants
            WHERE insight_version_id = (
                SELECT insight_version_id FROM insight_versions
                WHERE produced_event_id = ?
            )
            ORDER BY position
            """,
            (third_event,),
        ).fetchall()
    assert [row["input_kind"] for row in participants] == [
        "accepted_insight",
        "source_knowledge",
    ]
    assert participants[0]["accepted_insight_version_id"] == accepted_second
    assert participants[1]["knowledge_result_id"] == kr_d


@pytest.mark.parametrize(
    "crash_point",
    ["after_relation_version", "after_relation_edges", "after_candidate"],
)
def test_h_i_crash_rolls_back_relation_candidate_and_coverage(
    two_source_database, crash_point
):
    """§13.4B: H/I crash exposes no partial relation, candidate or coverage."""
    crashed = False

    def injector(point, connection):
        nonlocal crashed
        if point == crash_point and not crashed:
            crashed = True
            raise SystemExit("H/I crash")

    service, _, _ = _service(
        two_source_database, injector=injector, plan=_productive_plan()
    )
    event_id = service.start_or_reuse().event_id
    with pytest.raises(SystemExit, match="H/I crash"):
        service.drive(event_id)

    with connect(two_source_database) as connection:
        for table in (
            "relation_versions",
            "relation_current",
            "insight_versions",
            "insight_version_used_relations",
            "organization_event_coverages",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert read_event(two_source_database, event_id).status is EventStatus.RUNNING
    assert service.drive(event_id).event.status is EventStatus.SUCCEEDED


def test_used_edge_rowcount_failure_rolls_back_whole_event(two_source_database):
    """E2E-43/§13.4B: current lost after G is caught by I INSERT…SELECT rowcount."""
    def injector(point, connection):
        if point == "after_relation_edges":
            connection.execute("DELETE FROM relation_current")

    service, _, _ = _service(
        two_source_database, injector=injector, plan=_productive_plan()
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.DEPENDENCY_CHANGED
    with connect(two_source_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM relation_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0


class AdaptiveRecall:
    def is_available(self):
        return True

    def recall(self, boundary):
        from knowledge_distiller.growth_modeling import RecallPlanning, RecallSelection

        return RecallPlanning.succeeded(
            RecallSelection(
                tuple(item.knowledge_result_id for item in boundary.eligible_history),
                (),
                tuple(
                    item.relation_version_id
                    for item in boundary.current_relations
                ),
                (),
            )
        )


class AdaptiveGrowth:
    def __init__(self, path, h1):
        self.path = path
        self.h1 = h1
        self.calls = 0

    def is_available(self):
        return True

    def plan(
        self, boundary, recall, *, identity_catalog, expanded_inputs,
        topic_before, topic_plan
    ):
        from knowledge_distiller.growth_modeling import GrowthPlanning
        from knowledge_distiller.organization_models import parse_growth_plan

        self.calls += 1
        history_id = recall.source_knowledge_ids[0]
        frozen_ids = [item.knowledge_result_id for item in boundary.frozen_new]
        payload = empty_growth_plan_payload()
        payload["new_input_reviews"] = [
            {
                "knowledge_result_id": value,
                "outcome": "participated" if value == frozen_ids[0] else "considered_no_formal_result",
                "reason_text": "Completed review",
            }
            for value in frozen_ids
        ]
        candidate = _productive_plan(frozen_ids[0], history_id)["candidate_versions"][0]
        candidate["used_relations"] = []
        payload["candidate_versions"] = [candidate]
        plan = parse_growth_plan(payload)
        if self.calls == 1:
            with connect(self.path) as connection:
                connection.execute(
                    "UPDATE knowledge_results SET invalidated_at = 'invalidated' WHERE knowledge_result_id = ?",
                    (self.h1,),
                )
        return GrowthPlanning.succeeded(plan)


def test_history_input_drift_reforms_once_with_new_participant_and_lineage(tmp_path):
    """E2E-12: H1 loss withdraws its plan; H2 is selected anew from start boundary."""
    path = tmp_path / "history-reformation.sqlite3"
    _, h1 = add_formal_knowledge(path, "h1")
    _, h2 = add_formal_knowledge(path, "h2")
    first, _, _ = _service(path)
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    _, a = add_formal_knowledge(path, "a")
    _, b = add_formal_knowledge(path, "b")
    adaptive = AdaptiveGrowth(path, h1)
    service = OrganizationService(
        path,
        topic_indexer=EmptyTopicIndexer(),
        recall_planner=AdaptiveRecall(),
        relation_insight_planner=adaptive,
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    assert adaptive.calls == 2
    with connect(path) as connection:
        participants = connection.execute(
            """
            SELECT knowledge_result_id FROM insight_version_participants
            WHERE insight_version_id = (
              SELECT MAX(insight_version_id) FROM insight_versions
            ) ORDER BY position
            """
        ).fetchall()
        assert [row[0] for row in participants] == [a, h2]
        assert h1 not in [row[0] for row in participants]
        covered = {
            row[0]
            for row in connection.execute(
                "SELECT knowledge_result_id FROM organization_event_coverages WHERE event_id = ?",
                (event_id,),
            ).fetchall()
        }
        assert covered == {a, b}


def test_frozen_new_invalidation_fails_without_shrinking_boundary(two_source_database):
    """E2E-11: a frozen-new loss fails the whole event and preserves frozen history."""
    service, _, _ = _service(two_source_database)
    event_id = service.start_or_reuse().event_id
    with connect(two_source_database) as connection:
        connection.execute(
            "UPDATE knowledge_results SET invalidated_at = 'invalidated' WHERE knowledge_result_id = 1"
        )

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.FROZEN_INPUT_INELIGIBLE
    assert result.event.frozen_new_ids == (1, 2)
    with connect(two_source_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0


class BarrierGrowth:
    def __init__(self, parties=2):
        self.barrier = threading.Barrier(parties)
        self.calls = 0
        self.lock = threading.Lock()

    def is_available(self):
        return True

    def plan(
        self, boundary, recall, *, identity_catalog, expanded_inputs,
        topic_before, topic_plan
    ):
        from knowledge_distiller.growth_modeling import GrowthPlanning
        from knowledge_distiller.organization_models import parse_growth_plan

        with self.lock:
            self.calls += 1
        self.barrier.wait(timeout=5)
        return GrowthPlanning.succeeded(parse_growth_plan(empty_growth_plan_payload()))


def test_two_independent_drivers_can_plan_but_only_one_commits(two_source_database):
    """E2E-03/§13.4B: cross-instance drivers converge on one terminal and coverage."""
    barrier_growth = BarrierGrowth()

    def make_service():
        return OrganizationService(
            two_source_database,
            topic_indexer=EmptyTopicIndexer(),
            recall_planner=HistoricalRecallAdapter(
                JsonRuntime(
                    {
                        "codec": "historical-recall-v1",
                        "source_knowledge_ids": [],
                        "accepted_insight_version_ids": [],
                        "current_relation_version_ids": [],
                        "reconsideration_hint_version_ids": [],
                    }
                )
            ),
            relation_insight_planner=barrier_growth,
        )

    first = make_service()
    second = make_service()
    event_id = first.start_or_reuse().event_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda service: service.drive(event_id), (first, second))
        )

    assert barrier_growth.calls == 2
    assert all(item.event.status is EventStatus.SUCCEEDED for item in results)
    with connect(two_source_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_events WHERE status='succeeded'"
        ).fetchone()[0] == 1


class RelationReviewGrowth:
    def __init__(self, action):
        self.action = action

    def is_available(self):
        return True

    def plan(
        self, boundary, recall, *, identity_catalog, expanded_inputs,
        topic_before, topic_plan
    ):
        from knowledge_distiller.growth_modeling import GrowthPlanning
        from knowledge_distiller.organization_models import parse_growth_plan

        frozen = [item.knowledge_result_id for item in boundary.frozen_new]
        current = boundary.current_relations[0]
        payload = empty_growth_plan_payload()
        payload["new_input_reviews"] = [
            {
                "knowledge_result_id": value,
                "outcome": "participated",
                "reason_text": "Directly affected relation review",
            }
            for value in frozen
        ]
        review = {
            "relation_id": current.relation_id,
            "relation_version_id": current.relation_version_id,
            "action": self.action,
            "reason_text": f"Formal {self.action} conclusion",
            "directly_affected": True,
        }
        if self.action in {"replaced", "wrong_and_replaced"}:
            review["replacement_new_relation_key"] = "replacement"
            payload["new_relations"] = [
                {
                    "new_relation_key": "replacement",
                    "target_kind": "create_identity",
                    "payload": relation_payload(statement="A different replacement mechanism"),
                    "participants": [
                        source_participant("a", frozen[0], position=0),
                        source_participant("b", frozen[1], position=1),
                    ],
                    "used_relations": [],
                }
            ]
        payload["relation_reviews"] = [review]
        return GrowthPlanning.succeeded(parse_growth_plan(payload))


@pytest.mark.parametrize(
    ("action", "expected_facts", "replacement_current"),
    [
        ("basis_invalid", {"basis_invalid"}, False),
        ("wrong", {"wrong"}, False),
        ("replaced", {"replaced"}, True),
        ("wrong_and_replaced", {"wrong", "replaced"}, True),
    ],
)
def test_relation_fact_axes_have_one_current_exit_and_independent_history(
    tmp_path, action, expected_facts, replacement_current
):
    """E2E-21/E2E-22/E2E-23/E2E-24: fact axes preserve exact combinations."""
    path = tmp_path / f"relation-{action}.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    initial, _, _ = _service(path, plan=_productive_plan())
    first_event = initial.start_or_reuse().event_id
    assert initial.drive(first_event).event.status is EventStatus.SUCCEEDED
    add_formal_knowledge(path, "c")
    add_formal_knowledge(path, "d")
    service = OrganizationService(
        path,
        topic_indexer=EmptyTopicIndexer(),
        recall_planner=AdaptiveRecall(),
        relation_insight_planner=RelationReviewGrowth(action),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        event_facts = {
            row[0]
            for row in connection.execute(
                "SELECT fact_kind FROM relation_facts WHERE event_id = ?",
                (event_id,),
            ).fetchall()
        }
        assert event_facts == expected_facts
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_current"
        ).fetchone()[0] == int(replacement_current)
        if action == "wrong_and_replaced":
            old_relation_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT relation_id FROM relation_facts WHERE event_id = ?",
                    (event_id,),
                ).fetchall()
            }
            assert len(old_relation_ids) == 1


def _same_semantic_historical_relation_evolution_plan(
    relation_id, relation_version_id, *frozen_ids
):
    plan = _zero_review_plan(*frozen_ids)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Supplies a newly qualified formal basis",
        }
        for value in frozen_ids
    ]
    plan["relation_reviews"] = [
        {
            "relation_id": relation_id,
            "relation_version_id": relation_version_id,
            "action": "evolved",
            "reason_text": "A new basis restores explanatory force",
            "directly_affected": True,
            "successor_key": "restored",
        }
    ]
    plan["new_relations"] = [
        {
            "new_relation_key": "restored",
            "target_kind": "evolve_identity",
            "relation_id": relation_id,
            "previous_relation_version_id": relation_version_id,
            "payload": relation_payload(),
            "participants": [
                source_participant("a", frozen_ids[0], position=0),
                source_participant("b", frozen_ids[1], position=1),
            ],
            "used_relations": [],
        }
    ]
    return plan


def test_basis_invalid_relation_can_form_changed_basis_direct_successor(tmp_path):
    path = tmp_path / "basis-invalid-reformed.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    assert first.drive(first.start_or_reuse().event_id).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        original = connection.execute(
            "SELECT relation_id, relation_version_id FROM relation_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    invalidate_plan = _zero_review_plan(kr_c, kr_d)
    invalidate_plan["relation_reviews"] = [
        {
            "relation_id": int(original["relation_id"]),
            "relation_version_id": int(original["relation_version_id"]),
            "action": "basis_invalid",
            "reason_text": "The old formation basis is invalid",
            "directly_affected": True,
        }
    ]
    invalidate, _, _ = _service(
        path,
        plan=invalidate_plan,
        current_relation_ids=(int(original["relation_version_id"]),),
    )
    invalidate_event = invalidate.start_or_reuse().event_id
    assert invalidate.drive(invalidate_event).event.status is EventStatus.SUCCEEDED

    _, kr_e = add_formal_knowledge(path, "e")
    _, kr_f = add_formal_knowledge(path, "f")
    reform, _, _ = _service(
        path,
        plan=_same_semantic_historical_relation_evolution_plan(
            int(original["relation_id"]),
            int(original["relation_version_id"]),
            kr_e,
            kr_f,
        ),
    )
    reform_event = reform.start_or_reuse().event_id

    result = reform.drive(reform_event)

    assert result.event.status is EventStatus.SUCCEEDED
    assert result.event.success is not None
    assert result.event.success.relation_reviews[0].action.value == "evolved"
    assert result.event.success.final_required_boundary_relations == ()
    assert result.event.success.final_reconsideration_hints == ()
    assert result.event.success.final_requalified_current == ()
    with connect(path) as connection:
        versions = connection.execute(
            """
            SELECT relation_id, relation_version_id, version_no,
                   previous_version_id, semantic_signature
            FROM relation_versions ORDER BY version_no
            """
        ).fetchall()
        assert len(versions) == 2
        assert versions[0]["relation_id"] == versions[1]["relation_id"]
        assert versions[0]["semantic_signature"] == versions[1]["semantic_signature"]
        assert versions[1]["previous_version_id"] == versions[0]["relation_version_id"]
        assert connection.execute(
            "SELECT relation_version_id FROM relation_current"
        ).fetchone()[0] == versions[1]["relation_version_id"]
        facts = {
            row[0]
            for row in connection.execute(
                "SELECT fact_kind FROM relation_facts WHERE relation_version_id = ?",
                (versions[0]["relation_version_id"],),
            ).fetchall()
        }
        assert facts == {"basis_invalid", "evolved"}


def test_basis_invalid_relation_same_basis_cannot_reform(tmp_path):
    path = tmp_path / "basis-invalid-same-basis.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    assert first.drive(first.start_or_reuse().event_id).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        original = connection.execute(
            "SELECT relation_id, relation_version_id FROM relation_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    invalidate_plan = _zero_review_plan(kr_c)
    invalidate_plan["relation_reviews"] = [
        {
            "relation_id": int(original["relation_id"]),
            "relation_version_id": int(original["relation_version_id"]),
            "action": "basis_invalid",
            "reason_text": "The old formation basis is invalid",
            "directly_affected": True,
        }
    ]
    invalidate, _, _ = _service(
        path,
        plan=invalidate_plan,
        current_relation_ids=(int(original["relation_version_id"]),),
    )
    assert invalidate.drive(
        invalidate.start_or_reuse().event_id
    ).event.status is EventStatus.SUCCEEDED

    _, kr_d = add_formal_knowledge(path, "d")
    _, kr_e = add_formal_knowledge(path, "e")
    reform_plan = _same_semantic_historical_relation_evolution_plan(
        int(original["relation_id"]),
        int(original["relation_version_id"]),
        kr_d,
        kr_e,
    )
    reform_plan["new_relations"][0]["participants"] = [
        source_participant("b", kr_b, position=0),
        source_participant("a", kr_a, position=1),
    ]
    reform_plan["new_relations"][0]["payload"]["required_premises"].reverse()
    reform, _, _ = _service(
        path,
        plan=reform_plan,
        source_ids=(kr_a, kr_b),
    )
    event_id = reform.start_or_reuse().event_id

    result = reform.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM relation_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("exit_action", ["wrong", "replaced"])
def test_wrong_or_replaced_relation_cannot_reform_from_catalog(tmp_path, exit_action):
    path = tmp_path / f"{exit_action}-cannot-reform.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    assert first.drive(first.start_or_reuse().event_id).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        original = connection.execute(
            "SELECT relation_id, relation_version_id FROM relation_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    exit_plan = _zero_review_plan(kr_c, kr_d)
    review = {
        "relation_id": int(original["relation_id"]),
        "relation_version_id": int(original["relation_version_id"]),
        "action": exit_action,
        "reason_text": f"The relation is permanently {exit_action}",
        "directly_affected": True,
    }
    if exit_action == "replaced":
        review["replacement_new_relation_key"] = "replacement"
        exit_plan["new_relations"] = [
            {
                "new_relation_key": "replacement",
                "target_kind": "create_identity",
                "payload": relation_payload(statement="A truly different replacement"),
                "participants": [
                    source_participant("a", kr_c, position=0),
                    source_participant("b", kr_d, position=1),
                ],
                "used_relations": [],
            }
        ]
    exit_plan["relation_reviews"] = [review]
    exit_service, _, _ = _service(
        path,
        plan=exit_plan,
        current_relation_ids=(int(original["relation_version_id"]),),
    )
    assert exit_service.drive(
        exit_service.start_or_reuse().event_id
    ).event.status is EventStatus.SUCCEEDED

    _, kr_e = add_formal_knowledge(path, "e")
    _, kr_f = add_formal_knowledge(path, "f")
    reform, _, _ = _service(
        path,
        plan=_same_semantic_historical_relation_evolution_plan(
            int(original["relation_id"]),
            int(original["relation_version_id"]),
            kr_e,
            kr_f,
        ),
    )
    event_id = reform.start_or_reuse().event_id

    result = reform.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
def test_wrong_relation_exact_semantic_cannot_revive_under_a_new_identity(tmp_path):
    """A wrong historical relation cannot return via create_identity disguise."""
    path = tmp_path / "wrong-relation-duplicate.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED

    add_formal_knowledge(path, "c")
    add_formal_knowledge(path, "d")
    second = OrganizationService(
        path,
        topic_indexer=EmptyTopicIndexer(),
        recall_planner=AdaptiveRecall(),
        relation_insight_planner=RelationReviewGrowth("wrong"),
    )
    second_event = second.start_or_reuse().event_id
    assert second.drive(second_event).event.status is EventStatus.SUCCEEDED

    _, kr_e = add_formal_knowledge(path, "e")
    third, _, third_runtime = _service(
        path,
        plan=_relation_only_plan(kr_e, kr_a, kr_b),
        source_ids=(kr_a, kr_b),
    )
    third_event = third.start_or_reuse().event_id
    result = third.drive(third_event)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    catalog_relation = third_runtime.inputs[0]["identity_catalog"][
        "relation_versions"
    ][0]
    assert catalog_relation["is_current"] is False
    assert catalog_relation["permanent_facts"] == ["wrong"]
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM relation_identities").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM relation_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM relation_current").fetchone()[0] == 0
        assert connection.execute(
            "SELECT fact_kind FROM relation_facts WHERE event_id = ?",
            (second_event,),
        ).fetchone()[0] == "wrong"
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (third_event,),
        ).fetchone()[0] == 0


def test_unselected_attention_hint_is_not_sent_to_relation_planner(tmp_path):
    path = tmp_path / "unselected-hint.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan())
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        relation = connection.execute(
            """
            SELECT relation_id, relation_version_id
            FROM relation_versions
            """
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    retire_plan = _zero_review_plan(kr_c, kr_d)
    retire_plan["relation_reviews"] = [
        {
            "relation_id": int(relation["relation_id"]),
            "relation_version_id": int(relation["relation_version_id"]),
            "action": "attention",
            "attention_state": "retired",
            "reason_text": "No longer useful for default attention",
            "directly_affected": True,
        }
    ]
    retire, _, _ = _service(
        path,
        plan=retire_plan,
        current_relation_ids=(int(relation["relation_version_id"]),),
    )
    retire_event = retire.start_or_reuse().event_id
    assert retire.drive(retire_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM relation_current").fetchone()[0] == 0

    _, kr_e = add_formal_knowledge(path, "e")
    _, kr_f = add_formal_knowledge(path, "f")
    third, _, growth_runtime = _service(
        path, plan=_zero_review_plan(kr_e, kr_f)
    )
    event_id = third.start_or_reuse().event_id
    result = third.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    planner_input = growth_runtime.inputs[0]
    assert planner_input["recall_selection"]["reconsideration_hint_version_ids"] == []
    assert planner_input["reconsideration_hints"] == []
    catalog_relation = planner_input["identity_catalog"]["relation_versions"][0]
    assert catalog_relation["permanent_facts"] == ["attention_retired"]
    assert planner_input["identity_catalog"]["authority"] == (
        "identity_reuse_and_evolution_comparison_only"
    )

    _, kr_g = add_formal_knowledge(path, "g")
    _, kr_h = add_formal_knowledge(path, "h")
    activate_plan = _zero_review_plan(kr_g, kr_h)
    activate_plan["relation_reviews"] = [
        {
            "relation_id": int(relation["relation_id"]),
            "relation_version_id": int(relation["relation_version_id"]),
            "action": "attention",
            "attention_state": "activated",
            "reason_text": "Full basis review found the exact relation unchanged",
            "directly_affected": True,
        }
    ]
    fourth, _, fourth_growth = _service(
        path,
        plan=activate_plan,
        hint_ids=(int(relation["relation_version_id"]),),
    )
    fourth_event = fourth.start_or_reuse().event_id
    activated = fourth.drive(fourth_event)

    assert activated.event.status is EventStatus.SUCCEEDED
    expanded_hint = fourth_growth.inputs[0]["expanded_inputs"][
        "selected_reconsideration_hints"
    ]
    assert len(expanded_hint) == 1
    assert expanded_hint[0]["relation_version_id"] == int(
        relation["relation_version_id"]
    )
    assert expanded_hint[0]["authority"] == (
        "requalification_only_until_review_and_event_activation"
    )
    assert {
        item["resolved_source"]["knowledge_result_id"]
        for item in expanded_hint[0]["participants"]
    } == {1, 2}
    with connect(path) as connection:
        assert connection.execute(
            "SELECT relation_version_id FROM relation_current"
        ).fetchone()[0] == int(relation["relation_version_id"])


def test_recall_selected_current_expands_participants_and_used_edge_only_for_selection(
    tmp_path,
):
    path = tmp_path / "selected-current-graph.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        first_relation_version = int(
            connection.execute(
                "SELECT relation_version_id FROM relation_versions WHERE produced_event_id = ?",
                (first_event,),
            ).fetchone()[0]
        )

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    second_plan = _zero_review_plan(kr_c, kr_d)
    second_plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to a stable relation",
        }
        for value in (kr_c, kr_d)
    ]
    second_plan["relation_reviews"] = [
        {
            "relation_id": 1,
            "relation_version_id": first_relation_version,
            "action": "unchanged",
            "reason_text": "Expanded basis remains current",
            "directly_affected": True,
        }
    ]
    second_plan["new_relations"] = [
        {
            "new_relation_key": "r-with-edge",
            "target_kind": "create_identity",
            "payload": relation_payload(
                statement="C and D use the earlier mechanism as a connection"
            ),
            "participants": [
                source_participant("a", kr_c, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [
                {
                    "ref_kind": "boundary_current",
                    "relation_version_id": first_relation_version,
                    "role_text": "Historical mechanism connecting C and D",
                }
            ],
        }
    ]
    second, _, _ = _service(
        path,
        plan=second_plan,
        current_relation_ids=(first_relation_version,),
    )
    second_event = second.start_or_reuse().event_id
    assert second.drive(second_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        second_relation_version = int(
            connection.execute(
                "SELECT relation_version_id FROM relation_versions WHERE produced_event_id = ?",
                (second_event,),
            ).fetchone()[0]
        )

    _, kr_e = add_formal_knowledge(path, "e")
    _, kr_f = add_formal_knowledge(path, "f")
    third, _, third_growth = _service(
        path,
        plan=_zero_review_plan(kr_e, kr_f),
        current_relation_ids=(second_relation_version,),
    )
    third_event = third.start_or_reuse().event_id
    assert third.drive(third_event).event.status is EventStatus.SUCCEEDED

    planner_input = third_growth.inputs[0]
    assert {
        item["relation_version_id"] for item in planner_input["relation_current"]
    } == {first_relation_version, second_relation_version}
    expanded = planner_input["expanded_inputs"]["selected_current_relations"]
    assert [item["relation_version_id"] for item in expanded] == [
        second_relation_version
    ]
    graph = expanded[0]
    assert graph["authority"] == "review_and_used_only_after_qualification"
    assert {
        item["resolved_source"]["knowledge_result_id"]
        for item in graph["participants"]
    } == {kr_c, kr_d}
    assert graph["used_relations"] == [
        {
            "relation_version_id": first_relation_version,
            "relation_id": 1,
            "version_no": 1,
            "produced_event_id": first_event,
            "position": 0,
            "role_text": "Historical mechanism connecting C and D",
            "payload": relation_payload(),
            "authority": "historical_basis_edge_only",
        }
    ]


class RelationEvolvedGrowth:
    def is_available(self):
        return True

    def plan(
        self, boundary, recall, *, identity_catalog, expanded_inputs,
        topic_before, topic_plan
    ):
        from knowledge_distiller.growth_modeling import GrowthPlanning
        from knowledge_distiller.organization_models import parse_growth_plan

        frozen = [item.knowledge_result_id for item in boundary.frozen_new]
        current = boundary.current_relations[0]
        payload = empty_growth_plan_payload()
        payload["new_input_reviews"] = [
            {
                "knowledge_result_id": value,
                "outcome": "participated",
                "reason_text": "Contributed to evolved relation",
            }
            for value in frozen
        ]
        payload["relation_reviews"] = [
            {
                "relation_id": current.relation_id,
                "relation_version_id": current.relation_version_id,
                "action": "evolved",
                "reason_text": "Boundary narrowed with new sources",
                "directly_affected": True,
                "successor_key": "evolved",
            }
        ]
        payload["new_relations"] = [
            {
                "new_relation_key": "evolved",
                "target_kind": "evolve_identity",
                "relation_id": current.relation_id,
                "previous_relation_version_id": current.relation_version_id,
                "payload": relation_payload(statement="Same mechanism with a narrower boundary"),
                "participants": [
                    source_participant("a", frozen[0], position=0),
                    source_participant("b", frozen[1], position=1),
                ],
                "used_relations": [],
            }
        ]
        return GrowthPlanning.succeeded(parse_growth_plan(payload))


def test_service_evolved_relation_is_exact_same_identity_successor(tmp_path):
    """E2E-25: H writes one direct successor, evolved fact and new current."""
    path = tmp_path / "relation-evolved.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    initial, _, _ = _service(path, plan=_productive_plan())
    assert initial.drive(initial.start_or_reuse().event_id).event.status is EventStatus.SUCCEEDED
    add_formal_knowledge(path, "c")
    add_formal_knowledge(path, "d")
    service = OrganizationService(
        path,
        topic_indexer=EmptyTopicIndexer(),
        recall_planner=AdaptiveRecall(),
        relation_insight_planner=RelationEvolvedGrowth(),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        versions = connection.execute(
            "SELECT relation_id, relation_version_id, version_no, previous_version_id FROM relation_versions ORDER BY version_no"
        ).fetchall()
        assert len(versions) == 2
        assert versions[0]["relation_id"] == versions[1]["relation_id"]
        assert versions[1]["version_no"] == versions[0]["version_no"] + 1
        assert versions[1]["previous_version_id"] == versions[0]["relation_version_id"]
        assert connection.execute(
            "SELECT fact_kind FROM relation_facts WHERE event_id = ?", (event_id,)
        ).fetchone()[0] == "evolved"
        assert connection.execute(
            "SELECT relation_version_id FROM relation_current"
        ).fetchone()[0] == versions[1]["relation_version_id"]


def test_same_semantic_relation_and_insight_basis_change_create_direct_successors(
    tmp_path,
):
    path = tmp_path / "same-semantic-direct-successors.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        relation_v1 = connection.execute(
            "SELECT relation_id, relation_version_id FROM relation_versions"
        ).fetchone()
        insight_v1 = connection.execute(
            "SELECT insight_id, insight_version_id FROM insight_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    plan = _zero_review_plan(kr_c, kr_d)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Changed the exact formation basis",
        }
        for value in (kr_c, kr_d)
    ]
    plan["relation_reviews"] = [
        {
            "relation_id": int(relation_v1["relation_id"]),
            "relation_version_id": int(relation_v1["relation_version_id"]),
            "action": "evolved",
            "reason_text": "Same semantic now has a different formal basis",
            "directly_affected": True,
            "successor_key": "r-v2",
        }
    ]
    plan["new_relations"] = [
        {
            "new_relation_key": "r-v2",
            "target_kind": "evolve_identity",
            "relation_id": int(relation_v1["relation_id"]),
            "previous_relation_version_id": int(
                relation_v1["relation_version_id"]
            ),
            "payload": relation_payload(),
            "participants": [
                source_participant("a", kr_c, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [],
        }
    ]
    plan["candidate_versions"] = [
        {
            "new_insight_key": "i-v2",
            "target_kind": "evolve_identity",
            "insight_id": int(insight_v1["insight_id"]),
            "previous_insight_version_id": int(
                insight_v1["insight_version_id"]
            ),
            "payload": insight_payload(),
            "participants": [
                source_participant("a", kr_c, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [
                {
                    "ref_kind": "planned_stable",
                    "new_relation_key": "r-v2",
                    "role_text": "The newly formed exact relation is required",
                }
            ],
        }
    ]
    second, _, _ = _service(
        path,
        plan=plan,
        current_relation_ids=(int(relation_v1["relation_version_id"]),),
    )
    second_event = second.start_or_reuse().event_id

    result = second.drive(second_event)

    assert result.event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        relation_versions = connection.execute(
            """
            SELECT relation_id, relation_version_id, version_no,
                   previous_version_id, semantic_signature
            FROM relation_versions ORDER BY version_no
            """
        ).fetchall()
        insight_versions = connection.execute(
            """
            SELECT insight_id, insight_version_id, version_no,
                   previous_version_id, semantic_signature
            FROM insight_versions ORDER BY version_no
            """
        ).fetchall()
        assert len(relation_versions) == len(insight_versions) == 2
        assert relation_versions[0]["relation_id"] == relation_versions[1]["relation_id"]
        assert relation_versions[0]["semantic_signature"] == relation_versions[1]["semantic_signature"]
        assert relation_versions[1]["previous_version_id"] == relation_versions[0]["relation_version_id"]
        assert insight_versions[0]["insight_id"] == insight_versions[1]["insight_id"]
        assert insight_versions[0]["semantic_signature"] == insight_versions[1]["semantic_signature"]
        assert insight_versions[1]["previous_version_id"] == insight_versions[0]["insight_version_id"]


def test_same_semantic_insight_same_basis_with_renamed_handles_rolls_back(tmp_path):
    path = tmp_path / "same-semantic-same-basis.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        relation = connection.execute(
            "SELECT relation_version_id FROM relation_versions"
        ).fetchone()
        insight = connection.execute(
            "SELECT insight_id, insight_version_id FROM insight_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    plan = _zero_review_plan(kr_c)
    plan["relation_reviews"] = [
        {
            "relation_id": 1,
            "relation_version_id": int(relation["relation_version_id"]),
            "action": "unchanged",
            "reason_text": "The exact relation remains current",
            "directly_affected": True,
        }
    ]
    renamed_payload = insight_payload()
    for index, premise in enumerate(renamed_payload["required_premises"]):
        premise["premise_id"] = f"renamed-{index}"
        premise["supported_by"] = [
            "source_a" if key == "a" else "source_b"
            for key in premise["supported_by"]
        ]
    renamed_payload["required_premises"].reverse()
    plan["candidate_versions"] = [
        {
            "new_insight_key": "same-basis",
            "target_kind": "evolve_identity",
            "insight_id": int(insight["insight_id"]),
            "previous_insight_version_id": int(insight["insight_version_id"]),
            "payload": renamed_payload,
            "participants": [
                {
                    **source_participant("source_b", kr_b, position=0),
                    "contribution_text": "Reworded B contribution",
                },
                {
                    **source_participant("source_a", kr_a, position=1),
                    "contribution_text": "Reworded A contribution",
                },
            ],
            "used_relations": [
                {
                    "ref_kind": "boundary_current",
                    "relation_version_id": int(relation["relation_version_id"]),
                    "role_text": "Reworded relation role",
                }
            ],
        }
    ]
    service, _, _ = _service(
        path,
        plan=plan,
        source_ids=(kr_a, kr_b),
        current_relation_ids=(int(relation["relation_version_id"]),),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0


def test_local_handle_renaming_cannot_create_cross_round_duplicate_identity(tmp_path):
    path = tmp_path / "renamed-handle-create-duplicate.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    assert first.drive(first.start_or_reuse().event_id).event.status is EventStatus.SUCCEEDED

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    renamed_payload = insight_payload()
    for index, premise in enumerate(renamed_payload["required_premises"]):
        premise["premise_id"] = f"local-{index}"
        premise["supported_by"] = [
            "source_a" if key == "a" else "source_b"
            for key in premise["supported_by"]
        ]
    renamed_payload["required_premises"].reverse()
    plan = _zero_review_plan(kr_c, kr_d)
    plan["candidate_versions"] = [
        {
            "new_insight_key": "duplicate-core",
            "target_kind": "create_identity",
            "payload": renamed_payload,
            "participants": [
                source_participant("source_a", kr_c, position=0),
                source_participant("source_b", kr_d, position=1),
            ],
            "used_relations": [],
        }
    ]
    service, _, _ = _service(path, plan=plan)
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_identities").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_reordered_relation_payload_cannot_create_cross_round_duplicate_identity(
    tmp_path,
):
    path = tmp_path / "reordered-relation-create-duplicate.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    assert first.drive(first.start_or_reuse().event_id).event.status is EventStatus.SUCCEEDED

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    reordered_payload = relation_payload()
    reordered_payload["required_premises"].reverse()
    plan = _zero_review_plan(kr_c, kr_d)
    plan["new_relations"] = [
        {
            "new_relation_key": "duplicate-relation",
            "target_kind": "create_identity",
            "payload": reordered_payload,
            "participants": [
                source_participant("b", kr_d, position=0),
                source_participant("a", kr_c, position=1),
            ],
            "used_relations": [],
        }
    ]
    service, _, _ = _service(path, plan=plan)
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM relation_identities").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM relation_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_same_event_insight_evolution_and_replacement_conflict_rolls_back(tmp_path):
    path = tmp_path / "insight-evolve-replace-conflict.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        old = connection.execute(
            "SELECT insight_id, insight_version_id FROM insight_versions"
        ).fetchone()

    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    plan = _zero_review_plan(kr_c, kr_d)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to identity action review",
        }
        for value in (kr_c, kr_d)
    ]
    evolved = {
        "new_insight_key": "evolved",
        "target_kind": "evolve_identity",
        "insight_id": int(old["insight_id"]),
        "previous_insight_version_id": int(old["insight_version_id"]),
        "payload": insight_payload(claim="C and D evolve the existing core"),
        "participants": [
            source_participant("a", kr_c, position=0),
            source_participant("b", kr_d, position=1),
        ],
        "used_relations": [],
    }
    replacement = copy.deepcopy(evolved)
    replacement.update(
        new_insight_key="replacement",
        target_kind="create_identity",
        payload=insight_payload(claim="C and D replace the existing core"),
        replaces_insight_id=int(old["insight_id"]),
    )
    replacement.pop("insight_id")
    replacement.pop("previous_insight_version_id")
    plan["candidate_versions"] = [evolved, replacement]
    service, _, _ = _service(path, plan=plan)
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM insight_versions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_identity_replacements"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def _accepted_relation_plan(accepted_version_id, frozen_id):
    plan = _zero_review_plan(frozen_id)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": frozen_id,
            "outcome": "participated",
            "reason_text": "Contributed with accepted formal input",
        }
    ]
    plan["new_relations"] = [
        {
            "new_relation_key": "accepted-dependent",
            "target_kind": "create_identity",
            "payload": relation_payload(
                statement="Accepted history and the new source share a boundary"
            ),
            "participants": [
                accepted_participant("a", accepted_version_id, position=0),
                source_participant("b", frozen_id, position=1),
            ],
            "used_relations": [],
        }
    ]
    return plan


def _core_replacement_plan(old_insight_id, *frozen_ids):
    plan = _zero_review_plan(*frozen_ids)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to the replacement decision",
        }
        for value in frozen_ids
    ]
    plan["candidate_versions"] = [
        {
            "new_insight_key": "replacement",
            "target_kind": "create_identity",
            "replaces_insight_id": old_insight_id,
            "payload": insight_payload(claim="The new sources replace the old core"),
            "participants": [
                source_participant("a", frozen_ids[0], position=0),
                source_participant("b", frozen_ids[1], position=1),
            ],
            "used_relations": [],
        }
    ]
    return plan


def _disqualify_via_used_relation_plan(
    frozen_ids,
    accepted_version_id,
    relation,
    *,
    output_kind,
    ref_kind,
):
    plan = _zero_review_plan(*frozen_ids)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to the relation-backed output",
        }
        for value in frozen_ids
    ]
    review = {
        "relation_id": int(relation["relation_id"]),
        "relation_version_id": int(relation["relation_version_id"]),
        "action": "unchanged" if ref_kind == "boundary_current" else "attention",
        "reason_text": "The direct relation was formally reviewed",
        "directly_affected": True,
    }
    if ref_kind == "requalified_current":
        review["attention_state"] = "activated"
    plan["relation_reviews"] = [review]
    reference = {
        "ref_kind": ref_kind,
        "relation_version_id": int(relation["relation_version_id"]),
        "role_text": "Directly depends on the accepted participant",
    }
    participants = [
        source_participant("a", frozen_ids[0], position=0),
        source_participant("b", frozen_ids[1], position=1),
    ]
    if output_kind == "relation":
        plan["new_relations"] = [
            {
                "new_relation_key": "indirect-relation",
                "target_kind": "create_identity",
                "payload": relation_payload(
                    statement="A new relation attempts to use the direct dependency"
                ),
                "participants": participants,
                "used_relations": [reference],
            }
        ]
    else:
        plan["candidate_versions"] = [
            {
                "new_insight_key": "indirect-candidate",
                "target_kind": "create_identity",
                "payload": insight_payload(
                    claim="A candidate attempts to use the direct dependency"
                ),
                "participants": participants,
                "used_relations": [reference],
            }
        ]
    plan["accepted_disqualifications"] = [
        {
            "insight_version_id": accepted_version_id,
            "fact_kind": "basis_invalid",
            "reason_text": "The accepted direct relation basis is invalid",
        }
    ]
    plan["rejected_outputs"] = []
    return plan


def test_accepted_replacement_requires_dependent_current_relation_exit(tmp_path):
    path = tmp_path / "accepted-replacement-impact.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    accepted_version = _accept_event_candidate(path, first_event)
    with connect(path) as connection:
        old_insight_id = int(
            connection.execute(
                "SELECT insight_id FROM insight_versions WHERE insight_version_id = ?",
                (accepted_version,),
            ).fetchone()[0]
        )

    _, kr_c = add_formal_knowledge(path, "c")
    second, _, _ = _service(
        path,
        plan=_accepted_relation_plan(accepted_version, kr_c),
        accepted_ids=(accepted_version,),
    )
    second_event = second.start_or_reuse().event_id
    assert second.drive(second_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        dependent = connection.execute(
            """
            SELECT relation_id, relation_version_id
            FROM relation_versions WHERE produced_event_id = ?
            """,
            (second_event,),
        ).fetchone()

    _, kr_d = add_formal_knowledge(path, "d")
    _, kr_e = add_formal_knowledge(path, "e")
    failed, _, _ = _service(
        path,
        plan=_core_replacement_plan(old_insight_id, kr_d, kr_e),
        accepted_ids=(accepted_version,),
    )
    failed_event = failed.start_or_reuse().event_id
    failed_result = failed.drive(failed_event)

    assert failed_result.event.status is EventStatus.FAILED
    assert failed_result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (failed_event,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (failed_event,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT relation_version_id FROM relation_current WHERE relation_id = ?",
            (int(dependent["relation_id"]),),
        ).fetchone()[0] == int(dependent["relation_version_id"])
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (accepted_version,),
        ).fetchone()[0] == "current"

    success_plan = _core_replacement_plan(old_insight_id, kr_d, kr_e)
    success_plan["relation_reviews"] = [
        {
            "relation_id": int(dependent["relation_id"]),
            "relation_version_id": int(dependent["relation_version_id"]),
            "action": "basis_invalid",
            "reason_text": "The accepted core is replaced in this event",
            "directly_affected": True,
        }
    ]
    success, _, _ = _service(
        path,
        plan=success_plan,
        accepted_ids=(accepted_version,),
        current_relation_ids=(int(dependent["relation_version_id"]),),
    )
    success_event = success.start_or_reuse().event_id
    success_result = success.drive(success_event)

    assert success_result.event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_current WHERE relation_id = ?",
            (int(dependent["relation_id"]),),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (accepted_version,),
        ).fetchone()[0] == "historical"
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (success_event,),
        ).fetchone()[0] == 2


def test_same_event_outputs_cannot_use_accepted_identity_being_replaced(tmp_path):
    path = tmp_path / "replacement-output-authority.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    accepted_version = _accept_event_candidate(path, first_event)
    with connect(path) as connection:
        old_insight_id = int(
            connection.execute(
                "SELECT insight_id FROM insight_versions WHERE insight_version_id = ?",
                (accepted_version,),
            ).fetchone()[0]
        )
    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    plan = _core_replacement_plan(old_insight_id, kr_c, kr_d)
    plan["candidate_versions"][0]["participants"] = [
        accepted_participant("a", accepted_version, position=0),
        source_participant("b", kr_c, position=1),
    ]
    plan["new_relations"] = [
        {
            "new_relation_key": "also-invalid",
            "target_kind": "create_identity",
            "payload": relation_payload(
                statement="A relation also tries to use the retiring accepted input"
            ),
            "participants": [
                accepted_participant("a", accepted_version, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [],
        }
    ]
    service, _, _ = _service(
        path,
        plan=plan,
        accepted_ids=(accepted_version,),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_unselected_accepted_current_cannot_be_replaced_from_catalog_only(tmp_path):
    path = tmp_path / "unselected-accepted-replacement.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    accepted_version = _accept_event_candidate(path, first_event)
    with connect(path) as connection:
        old_insight_id = int(
            connection.execute(
                "SELECT insight_id FROM insight_versions WHERE insight_version_id = ?",
                (accepted_version,),
            ).fetchone()[0]
        )
    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    service, _, _ = _service(
        path,
        plan=_core_replacement_plan(old_insight_id, kr_c, kr_d),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (accepted_version,),
        ).fetchone()[0] == "current"
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("fact_kind", ["basis_invalid", "refuted"])
def test_e2e_32_accepted_current_disqualification_is_historical_everywhere(
    tmp_path, fact_kind
):
    path = tmp_path / f"e2e-32-{fact_kind}.sqlite3"
    _, kr_a = add_formal_knowledge(path, "accepted-a")
    _, kr_b = add_formal_knowledge(path, "accepted-b")
    first, _, _ = _service(
        path,
        plan=_productive_plan(kr_a, kr_b),
        topic_indexer=SeedTopicIndexer(),
    )
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    version_id = _accept_event_candidate(path, first_event)

    _, frozen_id = add_formal_knowledge(path, f"disqualify-{fact_kind}")
    service, _, _ = _service(
        path,
        plan=_accepted_disqualification_plan(
            frozen_id, version_id, fact_kind
        ),
        accepted_ids=(version_id,),
        topic_indexer=SeedTopicIndexer(),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    assert result.event.success.accepted_disqualifications == (
        parse_growth_plan(
            _accepted_disqualification_plan(frozen_id, version_id, fact_kind)
        ).accepted_disqualifications[0],
    )
    detail_result = read_accepted_detail(path, version_id)
    assert detail_result.kind is AcceptedDetailKind.FOUND
    assert detail_result.detail.current_role is AcceptedRole.HISTORICAL
    assert detail_result.detail.historical_reason == fact_kind
    search = search_accepted_insights(path, "narrower")
    assert search.current == ()
    assert [item.insight_version_id for item in search.historical] == [
        version_id
    ]
    assert search.historical[0].historical_reason == fact_kind
    assert list_current_accepted_inputs(path) == ()
    with connect(path) as connection:
        fact = connection.execute(
            """
            SELECT fact_kind, event_id, reason_text
            FROM insight_version_disqualifications
            WHERE insight_version_id = ?
            """,
            (version_id,),
        ).fetchone()
        assert tuple(fact) == (
            fact_kind,
            event_id,
            f"Formal review established {fact_kind}",
        )
        insight_id = int(
            connection.execute(
                "SELECT insight_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM accepted_insight_versions WHERE insight_id = ? AND current_role = 'current'",
            (insight_id,),
        ).fetchone()[0] == 0
        topic_id = int(
            connection.execute("SELECT topic_id FROM topics").fetchone()[0]
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    auxiliary = list_topic_auxiliary_insights(path, topic_id)
    assert auxiliary.topic_found
    assert auxiliary.insights == ()


def test_same_event_dual_disqualification_saves_both_with_refuted_primary(tmp_path):
    path = tmp_path / "accepted-dual-cause.sqlite3"
    _, kr_a = add_formal_knowledge(path, "dual-a")
    _, kr_b = add_formal_knowledge(path, "dual-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    version_id = _accept_event_candidate(path, first_event)
    _, frozen_id = add_formal_knowledge(path, "dual-review")
    plan = _dual_accepted_disqualification_plan(frozen_id, version_id)
    service, _, _ = _service(
        path,
        plan=plan,
        accepted_ids=(version_id,),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    assert result.event.success.accepted_disqualifications == (
        parse_growth_plan(plan).accepted_disqualifications
    )
    detail = read_accepted_detail(path, version_id).detail
    assert detail.current_role is AcceptedRole.HISTORICAL
    assert detail.historical_reason == "refuted"
    assert [fact.fact_kind for fact in detail.additional_exit_facts] == [
        "basis_invalid"
    ]
    with connect(path) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT fact_kind, event_id
                FROM insight_version_disqualifications
                WHERE insight_version_id = ?
                ORDER BY disqualification_id
                """,
                (version_id,),
            ).fetchall()
        ] == [
            ("basis_invalid", event_id),
            ("refuted", event_id),
        ]
        assert tuple(
            connection.execute(
                """
                SELECT current_role, historical_reason, caused_by_event_id,
                       disqualification_reason
                FROM accepted_insight_versions
                WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchone()
        ) == ("historical", "refuted", event_id, "refuted")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_accepted_disqualification_fact",
        "after_accepted_disqualification_retirement",
        "after_event_success",
        "after_coverage",
    ],
)
def test_dual_disqualification_crash_rolls_back_whole_group(
    tmp_path,
    crash_point,
):
    path = tmp_path / f"accepted-dual-crash-{crash_point}.sqlite3"
    _, kr_a = add_formal_knowledge(path, "dual-crash-a")
    _, kr_b = add_formal_knowledge(path, "dual-crash-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    version_id = _accept_event_candidate(path, first_event)
    _, frozen_id = add_formal_knowledge(path, "dual-crash-review")
    crashed = False

    def injector(point, _connection):
        nonlocal crashed
        if point == crash_point and not crashed:
            crashed = True
            raise SystemExit("dual disqualification crash")

    service, _, _ = _service(
        path,
        plan=_dual_accepted_disqualification_plan(frozen_id, version_id),
        accepted_ids=(version_id,),
        injector=injector,
    )
    event_id = service.start_or_reuse().event_id

    with pytest.raises(SystemExit, match="dual disqualification crash"):
        service.drive(event_id)

    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_disqualifications WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (version_id,),
        ).fetchone()[0] == "current"
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
    assert service.drive(event_id).event.status is EventStatus.SUCCEEDED


def test_disqualified_newer_current_does_not_resurrect_older_accepted(tmp_path):
    path = tmp_path / "accepted-no-resurrection.sqlite3"
    _, kr_a = add_formal_knowledge(path, "v1-a")
    _, kr_b = add_formal_knowledge(path, "v1-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    v1 = _accept_event_candidate(path, first_event)
    with connect(path) as connection:
        insight_id = int(
            connection.execute(
                "SELECT insight_id FROM insight_versions WHERE insight_version_id = ?",
                (v1,),
            ).fetchone()[0]
        )

    _, kr_c = add_formal_knowledge(path, "v2-c")
    _, kr_d = add_formal_knowledge(path, "v2-d")
    evolution, _, _ = _service(
        path,
        plan=_evolved_candidate_plan(
            insight_id,
            v1,
            kr_c,
            kr_d,
            claim="A materially evolved accepted claim",
        ),
    )
    evolution_event = evolution.start_or_reuse().event_id
    assert evolution.drive(evolution_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        v2 = int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions WHERE produced_event_id = ?",
                (evolution_event,),
            ).fetchone()[0]
        )
    judgment = InsightJudgmentService(path).record_judgment(v2, "interesting")
    assert judgment.kind == "recorded"

    _, frozen_id = add_formal_knowledge(path, "retire-v2")
    disqualifier, _, _ = _service(
        path,
        plan=_accepted_disqualification_plan(frozen_id, v2, "refuted"),
        accepted_ids=(v2,),
    )
    event_id = disqualifier.start_or_reuse().event_id
    assert disqualifier.drive(event_id).event.status is EventStatus.SUCCEEDED

    with connect(path) as connection:
        roles = connection.execute(
            """
            SELECT iv.version_no, a.current_role, a.historical_reason
            FROM accepted_insight_versions AS a
            JOIN insight_versions AS iv
              ON iv.insight_version_id = a.insight_version_id
            WHERE a.insight_id = ? ORDER BY iv.version_no
            """,
            (insight_id,),
        ).fetchall()
        assert [tuple(row) for row in roles] == [
            (1, "historical", "newer_accepted_current"),
            (2, "historical", "refuted"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM accepted_insight_versions WHERE insight_id = ? AND current_role = 'current'",
            (insight_id,),
        ).fetchone()[0] == 0


def test_newer_interesting_drift_after_planning_fails_without_partial_event(
    tmp_path,
):
    path = tmp_path / "accepted-final-lock-drift.sqlite3"
    _, kr_a = add_formal_knowledge(path, "drift-v1-a")
    _, kr_b = add_formal_knowledge(path, "drift-v1-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    v1 = _accept_event_candidate(path, first_event)
    with connect(path) as connection:
        insight_id = int(
            connection.execute(
                "SELECT insight_id FROM insight_versions WHERE insight_version_id = ?",
                (v1,),
            ).fetchone()[0]
        )
    _, kr_c = add_formal_knowledge(path, "drift-v2-c")
    _, kr_d = add_formal_knowledge(path, "drift-v2-d")
    evolution, _, _ = _service(
        path,
        plan=_evolved_candidate_plan(
            insight_id,
            v1,
            kr_c,
            kr_d,
            claim="A newer pending claim",
        ),
    )
    evolution_event = evolution.start_or_reuse().event_id
    assert evolution.drive(evolution_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        v2 = int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions WHERE produced_event_id = ?",
                (evolution_event,),
            ).fetchone()[0]
        )
    _, frozen_id = add_formal_knowledge(path, "drift-trigger")
    changed = False

    def injector(point, _connection):
        nonlocal changed
        if point == "before_begin_immediate" and not changed:
            changed = True
            assert InsightJudgmentService(path).record_judgment(
                v2, "interesting"
            ).kind == "recorded"

    service, _, _ = _service(
        path,
        plan=_accepted_disqualification_plan(
            frozen_id, v1, "basis_invalid"
        ),
        accepted_ids=(v1,),
        injector=injector,
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.DEPENDENCY_CHANGED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_disqualifications WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (v2,),
        ).fetchone()[0] == "current"


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_accepted_disqualification_fact",
        "after_accepted_disqualification_retirement",
        "after_event_success",
        "after_coverage",
    ],
)
def test_accepted_disqualification_crash_rolls_back_and_reuses_event(
    tmp_path, crash_point
):
    path = tmp_path / f"accepted-crash-{crash_point}.sqlite3"
    _, kr_a = add_formal_knowledge(path, "crash-a")
    _, kr_b = add_formal_knowledge(path, "crash-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    version_id = _accept_event_candidate(path, first_event)
    _, frozen_id = add_formal_knowledge(path, f"crash-{crash_point}")
    crashed = False

    def injector(point, _connection):
        nonlocal crashed
        if point == crash_point and not crashed:
            crashed = True
            raise SystemExit("accepted disqualification crash")

    service, _, _ = _service(
        path,
        plan=_accepted_disqualification_plan(
            frozen_id, version_id, "basis_invalid"
        ),
        accepted_ids=(version_id,),
        injector=injector,
    )
    event_id = service.start_or_reuse().event_id

    with pytest.raises(SystemExit, match="accepted disqualification crash"):
        service.drive(event_id)

    with connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM organization_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == "running"
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_disqualifications WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (version_id,),
        ).fetchone()[0] == "current"
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
    assert service.start_or_reuse().kind is OrganizationStartKind.REUSED
    assert service.drive(event_id).event.status is EventStatus.SUCCEEDED


def test_historical_version_cannot_be_disqualified_again_by_planner(tmp_path):
    path = tmp_path / "accepted-second-cause.sqlite3"
    _, kr_a = add_formal_knowledge(path, "cause-a")
    _, kr_b = add_formal_knowledge(path, "cause-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    version_id = _accept_event_candidate(path, first_event)
    _, first_frozen = add_formal_knowledge(path, "first-cause")
    first_action, _, _ = _service(
        path,
        plan=_accepted_disqualification_plan(
            first_frozen, version_id, "basis_invalid"
        ),
        accepted_ids=(version_id,),
    )
    first_action_event = first_action.start_or_reuse().event_id
    assert first_action.drive(first_action_event).event.status is EventStatus.SUCCEEDED

    _, second_frozen = add_formal_knowledge(path, "second-cause")
    second_action, _, _ = _service(
        path,
        plan=_accepted_disqualification_plan(
            second_frozen, version_id, "refuted"
        ),
    )
    second_action_event = second_action.start_or_reuse().event_id
    result = second_action.drive(second_action_event)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                """
                SELECT fact_kind, event_id
                FROM insight_version_disqualifications
                WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchall()
        ] == [("basis_invalid", first_action_event)]
        assert tuple(
            connection.execute(
                """
                SELECT current_role, historical_reason, caused_by_event_id
                FROM accepted_insight_versions
                WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchone()
        ) == ("historical", "basis_invalid", first_action_event)
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (second_action_event,),
        ).fetchone()[0] == 0


def test_disqualified_identity_can_later_gain_additional_replacement_fact(tmp_path):
    path = tmp_path / "disqualified-then-replacement.sqlite3"
    _, kr_a = add_formal_knowledge(path, "old-a")
    _, kr_b = add_formal_knowledge(path, "old-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    version_id = _accept_event_candidate(path, first_event)
    with connect(path) as connection:
        old_insight_id = int(
            connection.execute(
                "SELECT insight_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
    _, disqualify_id = add_formal_knowledge(path, "disqualify-old")
    disqualifier, _, _ = _service(
        path,
        plan=_accepted_disqualification_plan(
            disqualify_id, version_id, "refuted"
        ),
        accepted_ids=(version_id,),
    )
    disqualification_event = disqualifier.start_or_reuse().event_id
    assert disqualifier.drive(disqualification_event).event.status is (
        EventStatus.SUCCEEDED
    )

    _, kr_c = add_formal_knowledge(path, "replacement-after-c")
    _, kr_d = add_formal_knowledge(path, "replacement-after-d")
    replacer, _, _ = _service(
        path,
        plan=_core_replacement_plan(old_insight_id, kr_c, kr_d),
    )
    replacement_event = replacer.start_or_reuse().event_id
    result = replacer.drive(replacement_event)

    assert result.event.status is EventStatus.SUCCEEDED
    detail = read_accepted_detail(path, version_id)
    assert detail.kind is AcceptedDetailKind.FOUND
    assert detail.detail.current_role is AcceptedRole.HISTORICAL
    assert detail.detail.historical_reason == "refuted"
    assert len(detail.detail.additional_exit_facts) == 1
    additional = detail.detail.additional_exit_facts[0]
    assert additional.fact_kind == "identity_replaced"
    assert additional.event_id == replacement_event
    assert additional.replacement_insight_id is not None
    search = search_accepted_insights(path, "narrower")
    assert search.unreadable_count == 0
    assert [item.insight_version_id for item in search.historical] == [
        version_id
    ]
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_identity_replacements WHERE replaced_insight_id = ?",
            (old_insight_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (replacement_event,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT success_payload_json FROM organization_events WHERE event_id = ?",
            (replacement_event,),
        ).fetchone()[0] is not None
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (replacement_event,),
        ).fetchone()[0] == 2
        assert tuple(
            connection.execute(
                """
                SELECT historical_reason, caused_by_event_id,
                       replacement_insight_id
                FROM accepted_insight_versions
                WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchone()
        ) == ("refuted", disqualification_event, None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("output_kind", "ref_kind"),
    [
        ("relation", "boundary_current"),
        ("candidate", "requalified_current"),
    ],
)
def test_used_relation_direct_accepted_dependency_blocks_same_event_action(
    tmp_path, output_kind, ref_kind
):
    path = tmp_path / f"used-relation-{output_kind}-{ref_kind}.sqlite3"
    _, kr_a = add_formal_knowledge(path, "basis-a")
    _, kr_b = add_formal_knowledge(path, "basis-b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    accepted_version = _accept_event_candidate(path, first_event)

    _, relation_source = add_formal_knowledge(path, "relation-source")
    relation_service, _, _ = _service(
        path,
        plan=_accepted_relation_plan(accepted_version, relation_source),
        accepted_ids=(accepted_version,),
    )
    relation_event = relation_service.start_or_reuse().event_id
    assert relation_service.drive(relation_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        relation = connection.execute(
            """
            SELECT relation_id, relation_version_id
            FROM relation_versions WHERE produced_event_id = ?
            """,
            (relation_event,),
        ).fetchone()

    if ref_kind == "requalified_current":
        _, retirement_source = add_formal_knowledge(path, "retire-relation")
        retirement_plan = _zero_review_plan(retirement_source)
        retirement_plan["relation_reviews"] = [
            {
                "relation_id": int(relation["relation_id"]),
                "relation_version_id": int(relation["relation_version_id"]),
                "action": "attention",
                "attention_state": "retired",
                "reason_text": "Temporarily left active attention",
                "directly_affected": False,
            }
        ]
        retirement, _, _ = _service(
            path,
            plan=retirement_plan,
            current_relation_ids=(int(relation["relation_version_id"]),),
        )
        retirement_event = retirement.start_or_reuse().event_id
        assert retirement.drive(retirement_event).event.status is EventStatus.SUCCEEDED

    _, frozen_a = add_formal_knowledge(path, f"output-{output_kind}-a")
    _, frozen_b = add_formal_knowledge(path, f"output-{output_kind}-b")
    plan = _disqualify_via_used_relation_plan(
        (frozen_a, frozen_b),
        accepted_version,
        relation,
        output_kind=output_kind,
        ref_kind=ref_kind,
    )
    service, _, _ = _service(
        path,
        plan=plan,
        accepted_ids=(accepted_version,),
        current_relation_ids=(
            (int(relation["relation_version_id"]),)
            if ref_kind == "boundary_current"
            else ()
        ),
        hint_ids=(
            (int(relation["relation_version_id"]),)
            if ref_kind == "requalified_current"
            else ()
        ),
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_disqualifications WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM insight_version_used_relations AS edge
            JOIN insight_versions AS owner
              ON owner.insight_version_id = edge.insight_version_id
            WHERE owner.produced_event_id = ?
            """,
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT success_payload_json FROM organization_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (accepted_version,),
        ).fetchone()[0] == "current"


def test_relation_graph_signature_drift_under_final_lock_is_dependency_changed(
    tmp_path, monkeypatch
):
    path = tmp_path / "relation-graph-signature-drift.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    with connect(path) as connection:
        relation = connection.execute(
            "SELECT relation_id, relation_version_id FROM relation_versions"
        ).fetchone()
    _, kr_c = add_formal_knowledge(path, "c")
    _, kr_d = add_formal_knowledge(path, "d")
    plan = _zero_review_plan(kr_c, kr_d)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to a relation-backed candidate",
        }
        for value in (kr_c, kr_d)
    ]
    plan["relation_reviews"] = [
        {
            "relation_id": int(relation["relation_id"]),
            "relation_version_id": int(relation["relation_version_id"]),
            "action": "unchanged",
            "reason_text": "The fully expanded basis remains current",
            "directly_affected": True,
        }
    ]
    plan["candidate_versions"] = [
        {
            "new_insight_key": "relation-backed",
            "target_kind": "create_identity",
            "payload": insight_payload(
                claim="C and D produce a distinct relation-backed candidate"
            ),
            "participants": [
                source_participant("a", kr_c, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [
                {
                    "ref_kind": "boundary_current",
                    "relation_version_id": int(relation["relation_version_id"]),
                    "role_text": "Connects the two new sources",
                }
            ],
        }
    ]
    service, _, _ = _service(
        path,
        plan=plan,
        current_relation_ids=(int(relation["relation_version_id"]),),
    )
    event_id = service.start_or_reuse().event_id
    real_load = organization_service_module._load_event_boundary
    observed = 0

    def load_with_lock_drift(connection, requested_event_id):
        nonlocal observed
        boundary = real_load(connection, requested_event_id)
        if requested_event_id == event_id:
            observed += 1
            if observed >= 3:
                drifted = replace(
                    boundary.current_relations[0],
                    qualification_signature="f" * 64,
                )
                return replace(boundary, current_relations=(drifted,))
        return boundary

    monkeypatch.setattr(
        organization_service_module, "_load_event_boundary", load_with_lock_drift
    )

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.DEPENDENCY_CHANGED
    assert observed == 3
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_broken_recursive_accepted_lineage_fails_before_event_creation(tmp_path):
    path = tmp_path / "broken-accepted-lineage.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    first, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    first_event = first.start_or_reuse().event_id
    assert first.drive(first_event).event.status is EventStatus.SUCCEEDED
    accepted_version = _accept_event_candidate(path, first_event)
    add_formal_knowledge(path, "c")
    with connect(path) as connection:
        trigger_names = [
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'insight_version_participants'
                """
            ).fetchall()
        ]
        for name in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            """
            DELETE FROM insight_version_participants
            WHERE insight_version_id = ? AND position = 1
            """,
            (accepted_version,),
        )
    service, _, _ = _service(path)

    started = service.start_or_reuse()

    assert started.kind is OrganizationStartKind.READ_FAILED
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_events"
        ).fetchone()[0] == 1


def test_real_drive_observer_captures_same_decoded_plan_and_exact_failure_code(
    tmp_path, caplog
):
    path = tmp_path / "real-drive-qualification-observer.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    plan = _zero_review_plan(1, 2)
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to observer coverage",
        }
        for value in (1, 2)
    ]
    plan["candidate_versions"] = [
        {
            "new_insight_key": "observer-candidate",
            "target_kind": "create_identity",
            "payload": insight_payload(claim="Observer-only invalid relation reference"),
            "participants": [
                source_participant("a", 1, position=0),
                source_participant("b", 2, position=1),
            ],
            "used_relations": [
                {
                    "ref_kind": "boundary_current",
                    "relation_version_id": 999,
                    "role_text": "Strictly decoded but outside the boundary",
                }
            ],
        }
    ]
    service, _, growth_runtime = _service(path, plan=plan)
    event_id = service.start_or_reuse().event_id
    original = organization_service_module.qualify_growth_plan

    with capture_service_growth_qualification() as capture:
        result = service.drive(event_id)

    assert organization_service_module.qualify_growth_plan is original
    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    assert isinstance(capture.growth_plan, GrowthPlan)
    assert capture.error_code == "invalid_boundary_current"
    assert capture.call_count == 1
    assert capture.topic_change_stage is None
    assert capture.topic_change_error_code is None
    assert capture.topic_change_call_count == 0
    assert growth_runtime.calls == 1
    assert any(
        f"event_id={event_id} stage=planning code=invalid_boundary_current"
        in record.getMessage()
        for record in caplog.records
    )
    assert all(
        "Observer-only invalid relation reference" not in record.getMessage()
        for record in caplog.records
    )
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


def test_real_drive_observer_captures_topic_assessment_gate_after_growth_passes(
    tmp_path,
):
    path = tmp_path / "real-drive-topic-assessment-observer.sqlite3"
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    plan = _zero_review_plan(kr_a, kr_b)
    plan["topic_change_assessments"] = [
        {
            "topic_ref": "topic:999",
            "changed": True,
            "reason_text": "Invalid extra assessment for a deterministic new Topic",
        }
    ]
    indexer = SeedTopicIndexer()
    service, _, growth_runtime = _service(
        path,
        plan=plan,
        topic_indexer=indexer,
    )
    event_id = service.start_or_reuse().event_id
    original_growth = organization_service_module.qualify_growth_plan
    original_topic_changes = organization_service_module._qualify_topic_changes

    with capture_service_growth_qualification() as capture:
        result = service.drive(event_id)

    assert organization_service_module.qualify_growth_plan is original_growth
    assert organization_service_module._qualify_topic_changes is original_topic_changes
    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    assert isinstance(capture.growth_plan, GrowthPlan)
    assert capture.call_count == 1
    assert capture.error_code is None
    assert capture.topic_change_stage == "topic_changes"
    assert capture.topic_change_call_count == 1
    assert capture.topic_change_error_code == "unknown_topic_assessment"
    assert indexer.calls == growth_runtime.calls == 1
    persisted = read_event(path, event_id)
    assert persisted is not None
    assert persisted.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
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


def _seed_safe_topic(path):
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    refreshed = TopicLibrary(path, SeedTopicIndexer()).refresh(force=True)
    assert refreshed.kind is TopicRefreshKind.REFRESHED
    with connect(path) as connection:
        topic_id = int(connection.execute("SELECT topic_id FROM topics").fetchone()[0])
    return kr_a, kr_b, topic_id


@pytest.mark.parametrize(("changed", "expected_m"), [(True, 1), (False, 0)])
def test_label_only_topic_assessment_controls_exact_m(
    tmp_path, changed, expected_m
):
    path = tmp_path / f"topic-label-assessment-{changed}.sqlite3"
    kr_a, kr_b, topic_id = _seed_safe_topic(path)
    plan = _zero_review_plan(kr_a, kr_b)
    plan["topic_change_assessments"] = [
        {
            "topic_ref": f"topic:{topic_id}",
            "changed": changed,
            "reason_text": (
                "主题标签的新表达改变了用户理解。"
                if changed
                else "只是措辞调整，不计实质变化。"
            ),
        }
    ]
    indexer = LabelOnlyTopicIndexer()
    service, _, _ = _service(path, plan=plan, topic_indexer=indexer)
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.SUCCEEDED
    assert result.event.success.m == expected_m
    assert len(result.event.success.topic_changes) == expected_m
    assert indexer.calls == 1
    with connect(path) as connection:
        topic = connection.execute(
            "SELECT name, scope FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        assert tuple(topic) == ("新主题名", "新范围。")
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("case", "error_code"),
    [
        ("missing", "missing_topic_assessment"),
        ("unknown", "unknown_topic_assessment"),
        ("duplicate", "duplicate_topic_assessment"),
        ("extra", "unknown_topic_assessment"),
    ],
)
def test_invalid_label_only_topic_assessment_fails_before_any_write(
    tmp_path, caplog, case, error_code
):
    path = tmp_path / f"topic-label-invalid-{case}.sqlite3"
    kr_a, kr_b, topic_id = _seed_safe_topic(path)
    plan = _zero_review_plan(kr_a, kr_b)
    assessment = {
        "topic_ref": f"topic:{topic_id}",
        "changed": True,
        "reason_text": "标签变化需要完整判断。",
    }
    if case == "missing":
        assessments = []
    elif case == "unknown":
        assessments = [{**assessment, "topic_ref": "topic:999"}]
    elif case == "duplicate":
        assessments = [assessment, {**assessment, "reason_text": "重复判断。"}]
    else:
        assessments = [assessment]
    plan["topic_change_assessments"] = assessments
    indexer = LabelOnlyTopicIndexer(change_labels=case != "extra")
    service, _, growth_runtime = _service(
        path,
        plan=plan,
        topic_indexer=indexer,
    )
    event_id = service.start_or_reuse().event_id

    result = service.drive(event_id)

    assert result.event.status is EventStatus.FAILED
    assert result.event.failure_code is OrganizationFailureCode.QUALIFICATION_FAILED
    assert indexer.calls == growth_runtime.calls == 1
    assert any(
        f"event_id={event_id} stage=planning code={error_code}"
        in record.getMessage()
        for record in caplog.records
    )
    with connect(path) as connection:
        topic = connection.execute(
            "SELECT name, scope FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        assert tuple(topic) == ("原始主题", "原始范围。")
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_versions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0
