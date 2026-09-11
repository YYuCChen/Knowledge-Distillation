from __future__ import annotations

import sqlite3

import pytest

from knowledge_distiller.accepted_insight_library import (
    AcceptedDetailKind,
    AcceptedInsightLibraryError,
    AcceptedRole,
    list_accepted_insights,
    list_current_accepted_inputs,
    list_topic_auxiliary_insights,
    read_accepted_detail,
    search_accepted_insights,
)
from knowledge_distiller.database import connect
from knowledge_distiller.topic_indexing import TopicDraft, TopicIndexing, TopicPlan
from knowledge_distiller.topic_library import TopicLibrary, TopicRefreshKind
from tests.fixtures.growth import (
    add_formal_knowledge,
    empty_growth_plan_payload,
    insight_payload,
    source_participant,
)
from tests.test_insight_judgment_service import (
    _produce_first_version,
    _produce_successor,
    _service_for,
)
from tests.test_organization_service import (
    _accepted_candidate_plan,
    _core_replacement_plan,
    _service,
)


class AllPointsTopicIndexer:
    def is_available(self):
        return True

    def organize(self, points, existing_topics):
        if not points:
            return TopicIndexing.succeeded(TopicPlan((), ()))
        return TopicIndexing.succeeded(
            TopicPlan(
                (
                    TopicDraft(
                        None,
                        "accepted-topic",
                        "Accepted source topic",
                        "A safe source-membership projection.",
                        tuple(point.reference for point in points),
                    ),
                ),
                (),
            )
        )


def _accepted_first(path, *, annotation=None):
    insight_id, version_id = _produce_first_version(path)
    result = _service_for(path).record_judgment(
        version_id,
        "interesting",
        annotation,
    )
    assert result.kind == "recorded"
    return insight_id, version_id


def _accepted_nested(path, parent_version_id):
    _, kr_c = add_formal_knowledge(path, "nested-c")
    service, _, _ = _service(
        path,
        plan=_accepted_candidate_plan(parent_version_id, kr_c),
        accepted_ids=(parent_version_id,),
    )
    event_id = service.start_or_reuse().event_id
    result = service.drive(event_id)
    assert result.event.status == "succeeded"
    with connect(path) as connection:
        row = connection.execute(
            "SELECT insight_version_id FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()
    version_id = int(row[0])
    assert _service_for(path).record_judgment(version_id, "interesting").kind == "recorded"
    return version_id


def _accepted_independent(path, suffix):
    _, kr_a = add_formal_knowledge(path, f"{suffix}-a")
    _, kr_b = add_formal_knowledge(path, f"{suffix}-b")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to an independent accepted insight",
        }
        for value in (kr_a, kr_b)
    ]
    plan["candidate_versions"] = [
        {
            "new_insight_key": f"independent-{suffix}",
            "target_kind": "create_identity",
            "payload": insight_payload(claim=f"A distinct accepted claim {suffix}"),
            "participants": [
                source_participant("a", kr_a, position=0),
                source_participant("b", kr_b, position=1),
            ],
            "used_relations": [],
        }
    ]
    plan["rejected_outputs"] = []
    service, _, _ = _service(path, plan=plan)
    event_id = service.start_or_reuse().event_id
    assert service.drive(event_id).event.status == "succeeded"
    with connect(path) as connection:
        version_id = int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions WHERE produced_event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )
    assert _service_for(path).record_judgment(version_id, "interesting").kind == "recorded"
    return version_id


def test_detail_returns_exact_identity_judgment_event_lineage_and_source_handoff(tmp_path):
    path = tmp_path / "detail.sqlite3"
    _, version_id = _accepted_first(path, annotation="My exact note")

    result = read_accepted_detail(path, version_id)

    assert result.kind is AcceptedDetailKind.FOUND
    detail = result.detail
    assert detail.identity_kind == "ai_derived_insight"
    assert detail.current_role is AcceptedRole.CURRENT
    assert detail.payload.claim == "A and B reveal a narrower boundary"
    assert detail.judgment.decision == "interesting"
    assert detail.judgment.annotation_text == "My exact note"
    assert "does not certify truth" in detail.judgment.meaning
    assert detail.formation_event.completed_at
    assert len(detail.top_level_participants) == 2
    assert len(detail.used_relations) == 1
    assert len(detail.recursive_lineage) == 1
    assert {item.point_id for item in detail.source_leaves} == {"p1"}
    assert len(detail.source_leaves) == 2
    assert all(item.published_path.endswith(".md") for item in detail.source_leaves)
    assert all(item.evidence_ids == ("e1",) for item in detail.source_leaves)


def test_pending_and_rethink_are_mechanically_absent_from_accepted_consumers(tmp_path):
    """E2E-28/36: pending and rethink never enter accepted detail/search/input."""
    pending_path = tmp_path / "pending.sqlite3"
    _, pending = _produce_first_version(pending_path)
    assert read_accepted_detail(pending_path, pending).kind is AcceptedDetailKind.NOT_FOUND
    assert search_accepted_insights(pending_path, "narrower").current == ()
    assert list_current_accepted_inputs(pending_path) == ()

    rethink_path = tmp_path / "rethink.sqlite3"
    _, rethink = _produce_first_version(rethink_path)
    _service_for(rethink_path).record_judgment(rethink, "rethink")
    assert read_accepted_detail(rethink_path, rethink).kind is AcceptedDetailKind.NOT_FOUND
    search = search_accepted_insights(rethink_path, "narrower")
    assert search.current == () and search.historical == ()
    assert list_current_accepted_inputs(rethink_path) == ()


def test_search_uses_literal_and_primary_fields_with_limitations_only_auxiliary(tmp_path):
    path = tmp_path / "search.sqlite3"
    _, version_id = _accepted_first(path)

    matching = search_accepted_insights(path, "A   NARROWER")
    limitation_only = search_accepted_insights(path, "stated cases")
    mixed = search_accepted_insights(path, "narrower stated")

    assert [item.insight_version_id for item in matching.current] == [version_id]
    assert limitation_only.current == ()
    assert [item.insight_version_id for item in mixed.current] == [version_id]


def test_current_and_historical_search_partitions_are_independent(tmp_path):
    path = tmp_path / "partitions.sqlite3"
    insight_id, v1 = _accepted_first(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    _service_for(path).record_judgment(v2, "interesting")

    all_results = search_accepted_insights(path, "A")
    old_detail = read_accepted_detail(path, v1).detail
    new_detail = read_accepted_detail(path, v2).detail

    assert [item.insight_version_id for item in all_results.current] == [v2]
    assert [item.insight_version_id for item in all_results.historical] == [v1]
    assert old_detail.current_role is AcceptedRole.HISTORICAL
    assert old_detail.historical_reason == "newer_accepted_current"
    assert new_detail.current_role is AcceptedRole.CURRENT


def test_list_returns_current_and_historical_in_accepted_order(tmp_path):
    path = tmp_path / "list.sqlite3"
    insight_id, v1 = _accepted_first(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    _service_for(path).record_judgment(v2, "interesting")
    independent = _accepted_independent(path, "listed")

    result = list_accepted_insights(path)

    assert [item.insight_version_id for item in result.current] == [
        independent,
        v2,
    ]
    assert [item.insight_version_id for item in result.historical] == [v1]
    assert result.unreadable_count == 0


def test_list_empty_collection_is_not_a_read_failure(tmp_path):
    path = tmp_path / "empty-list.sqlite3"
    _produce_first_version(path)

    result = list_accepted_insights(path)

    assert result.current == ()
    assert result.historical == ()
    assert result.unreadable_count == 0


def test_recursive_detail_and_future_input_preserve_all_intermediate_ai_nodes(tmp_path):
    """E2E-38: recursive accepted lineage retains its intermediate AI layer."""
    path = tmp_path / "recursive.sqlite3"
    _, first = _accepted_first(path)
    second = _accepted_nested(path, first)

    detail = read_accepted_detail(path, second).detail
    inputs = list_current_accepted_inputs(path)

    assert [node.insight_version_id for node in detail.recursive_lineage] == [
        second,
        first,
    ]
    assert {item.knowledge_result_id for item in detail.source_leaves} == {1, 2, 3}
    assert [item.insight_version_id for item in inputs] == [first, second]
    nested = next(item for item in inputs if item.insight_version_id == second)
    assert nested.identity_kind == "ai_derived_insight"
    assert nested.current_role == "current"
    assert nested.disqualification_facts == ()
    assert nested.replacement_insight_id is None
    assert [node.insight_version_id for node in nested.lineage_nodes] == [
        second,
        first,
    ]


def test_historical_parent_does_not_by_itself_disqualify_current_child(tmp_path):
    """Formation history is not a read-path basis-invalidity judgment."""
    path = tmp_path / "historical-parent.sqlite3"
    parent_identity, parent_v1 = _accepted_first(path)
    child = _accepted_nested(path, parent_v1)
    parent_v2 = _produce_successor(
        path,
        parent_identity,
        parent_v1,
        "parent-v2",
    )
    _service_for(path).record_judgment(parent_v2, "interesting")

    inputs = list_current_accepted_inputs(path)

    assert {item.insight_version_id for item in inputs} == {child, parent_v2}
    child_input = next(item for item in inputs if item.insight_version_id == child)
    assert [node.insight_version_id for node in child_input.lineage_nodes] == [
        child,
        parent_v1,
    ]


def test_parent_disqualification_does_not_mechanically_cascade_to_child(tmp_path):
    path = tmp_path / "disqualified-parent.sqlite3"
    _, parent = _accepted_first(path)
    child = _accepted_nested(path, parent)
    _, frozen_id = add_formal_knowledge(path, "disqualify-parent")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": frozen_id,
            "outcome": "considered_no_formal_result",
            "reason_text": "Completed exact parent review only",
        }
    ]
    plan["accepted_disqualifications"] = [
        {
            "insight_version_id": parent,
            "fact_kind": "basis_invalid",
            "reason_text": "The parent exact version lost necessary basis",
        }
    ]
    service, _, _ = _service(path, plan=plan, accepted_ids=(parent,))
    event_id = service.start_or_reuse().event_id

    assert service.drive(event_id).event.status == "succeeded"

    inputs = list_current_accepted_inputs(path)
    assert [item.insight_version_id for item in inputs] == [child]
    child_input = inputs[0]
    assert [node.insight_version_id for node in child_input.lineage_nodes] == [
        child,
        parent,
    ]
    assert read_accepted_detail(path, parent).detail.current_role is (
        AcceptedRole.HISTORICAL
    )


def test_topic_auxiliary_is_current_only_safe_membership_intersection(tmp_path):
    path = tmp_path / "topic-auxiliary.sqlite3"
    insight_id, v1 = _accepted_first(path)
    library = TopicLibrary(path, AllPointsTopicIndexer())
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    topic_id = library.snapshot().topics[0].topic_id

    before = list_topic_auxiliary_insights(path, topic_id)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    _service_for(path).record_judgment(v2, "interesting")
    with connect(path) as connection:
        memberships_before_read = connection.execute(
            "SELECT COUNT(*) FROM topic_memberships"
        ).fetchone()[0]
    after = list_topic_auxiliary_insights(path, topic_id)

    assert before.topic_found
    assert [item.insight_version_id for item in before.insights] == [v1]
    # v2 uses newly added source leaves that the stale Topic has never indexed.
    # Historical v1 must not be used to manufacture a current auxiliary match.
    assert after.insights == ()
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM topic_memberships"
        ).fetchone()[0] == memberships_before_read


def test_bad_accepted_item_is_unreadable_and_search_reports_local_degradation(tmp_path):
    """E2E-41: one damaged accepted item is not disguised as missing or healthy."""
    path = tmp_path / "damaged.sqlite3"
    _, version_id = _accepted_first(path)
    with connect(path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
            ("f" * 64, version_id),
        )

    assert read_accepted_detail(path, version_id).kind is AcceptedDetailKind.UNREADABLE
    search = search_accepted_insights(path, "narrower")
    assert search.current == () and search.unreadable_count == 1
    with pytest.raises(AcceptedInsightLibraryError):
        list_current_accepted_inputs(path)


def test_existing_accepted_root_with_broken_required_reference_is_unreadable(tmp_path):
    path = tmp_path / "broken-accepted-reference.sqlite3"
    _, version_id = _accepted_first(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER user_insight_judgments_cannot_be_deleted")
        connection.execute(
            "DELETE FROM user_insight_judgments WHERE insight_version_id = ?",
            (version_id,),
        )

    assert read_accepted_detail(path, version_id).kind is AcceptedDetailKind.UNREADABLE


def test_search_returns_healthy_subset_with_explicit_unreadable_count(tmp_path):
    path = tmp_path / "partial-degradation.sqlite3"
    _, damaged = _accepted_first(path)
    healthy = _accepted_independent(path, "healthy")
    with connect(path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
            ("f" * 64, damaged),
        )

    result = search_accepted_insights(path, "A")

    assert [item.insight_version_id for item in result.current] == [healthy]
    assert result.unreadable_count == 1


def test_list_returns_healthy_subset_with_explicit_unreadable_count(tmp_path):
    path = tmp_path / "list-partial-degradation.sqlite3"
    _, damaged = _accepted_first(path)
    healthy = _accepted_independent(path, "list-healthy")
    with connect(path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
            ("f" * 64, damaged),
        )

    result = list_accepted_insights(path)

    assert [item.insight_version_id for item in result.current] == [healthy]
    assert result.historical == ()
    assert result.unreadable_count == 1


def test_recursive_lineage_cycle_is_unreadable_not_an_empty_lineage(tmp_path):
    path = tmp_path / "cycle.sqlite3"
    _, first = _accepted_first(path)
    second = _accepted_nested(path, first)
    with connect(path) as connection:
        connection.execute(
            "DROP TRIGGER insight_version_participants_cannot_be_updated"
        )
        connection.execute(
            """
            UPDATE insight_version_participants
            SET input_kind = 'accepted_insight', knowledge_result_id = NULL,
                point_id = NULL, accepted_insight_version_id = ?
            WHERE insight_version_id = ? AND position = 0
            """,
            (second, first),
        )

    assert read_accepted_detail(path, second).kind is AcceptedDetailKind.UNREADABLE
    with pytest.raises(AcceptedInsightLibraryError):
        list_current_accepted_inputs(path)


def test_broken_recursive_lineage_is_unreadable_not_a_partial_detail(tmp_path):
    path = tmp_path / "broken-lineage.sqlite3"
    _, first = _accepted_first(path)
    second = _accepted_nested(path, first)
    with connect(path) as connection:
        connection.execute(
            "DROP TRIGGER insight_version_participants_cannot_be_deleted"
        )
        connection.execute(
            """
            DELETE FROM insight_version_participants
            WHERE insight_version_id = ? AND position = 1
            """,
            (first,),
        )

    assert read_accepted_detail(path, second).kind is AcceptedDetailKind.UNREADABLE
    with pytest.raises(AcceptedInsightLibraryError):
        list_current_accepted_inputs(path)


def test_detail_preserves_used_relation_after_it_formally_retires(tmp_path):
    path = tmp_path / "historical-used-relation.sqlite3"
    _, version_id = _accepted_first(path)
    with connect(path) as connection:
        relation_version_id = int(
            connection.execute(
                """
                SELECT relation_version_id
                FROM insight_version_used_relations
                WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchone()[0]
        )
        relation_id = int(
            connection.execute(
                "SELECT relation_id FROM relation_versions WHERE relation_version_id = ?",
                (relation_version_id,),
            ).fetchone()[0]
        )
    _, kr_c = add_formal_knowledge(path, "retire-relation-c")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": kr_c,
            "outcome": "considered_no_formal_result",
            "reason_text": "The new input was fully reviewed",
        }
    ]
    plan["relation_reviews"] = [
        {
            "relation_id": relation_id,
            "relation_version_id": relation_version_id,
            "action": "attention",
            "attention_state": "retired",
            "reason_text": "The relation left active attention",
            "directly_affected": False,
        }
    ]
    plan["rejected_outputs"] = []
    service, _, _ = _service(
        path,
        plan=plan,
        current_relation_ids=(relation_version_id,),
    )
    event_id = service.start_or_reuse().event_id
    assert service.drive(event_id).event.status == "succeeded"
    with connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM relation_current WHERE relation_version_id = ?",
            (relation_version_id,),
        ).fetchone() is None

    detail = read_accepted_detail(path, version_id).detail
    current_inputs = list_current_accepted_inputs(path)

    assert [item.relation_version_id for item in detail.used_relations] == [
        relation_version_id
    ]
    assert detail.used_relations[0].role_text == "Explains the candidate connection"
    assert [item.insight_version_id for item in current_inputs] == [version_id]


def test_topic_auxiliary_returns_healthy_subset_and_unreadable_count(tmp_path):
    path = tmp_path / "topic-partial.sqlite3"
    _, damaged = _accepted_first(path)
    healthy = _accepted_independent(path, "topic-healthy")
    library = TopicLibrary(path, AllPointsTopicIndexer())
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    topic_id = library.snapshot().topics[0].topic_id
    with connect(path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
            ("f" * 64, damaged),
        )

    result = list_topic_auxiliary_insights(path, topic_id)

    assert [item.insight_version_id for item in result.insights] == [healthy]
    assert result.unreadable_count == 1


def test_get_helpers_are_database_read_only(tmp_path):
    path = tmp_path / "read-only.sqlite3"
    _, version_id = _accepted_first(path)
    library = TopicLibrary(path, AllPointsTopicIndexer())
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    topic_id = library.snapshot().topics[0].topic_id
    witness = connect(path)
    try:
        before = int(witness.execute("PRAGMA data_version").fetchone()[0])
        assert read_accepted_detail(path, version_id).kind is AcceptedDetailKind.FOUND
        list_accepted_insights(path)
        search_accepted_insights(path, "narrower")
        list_topic_auxiliary_insights(path, topic_id)
        list_current_accepted_inputs(path)
        after = int(witness.execute("PRAGMA data_version").fetchone()[0])
    finally:
        witness.close()

    assert after == before


def test_one_preestablished_disqualification_is_filtered_from_current_consumers(
    tmp_path,
):
    """A-scope projection only; no production disqualification path is claimed."""
    path = tmp_path / "filtered-disqualification.sqlite3"
    _, version_id = _accepted_first(path)
    with connect(path) as connection:
        event_id = int(
            connection.execute(
                "SELECT produced_event_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, 'refuted', ?, 'Pre-established fixture fact',
                      '2026-08-21T10:01:00+00:00')
            """,
            (version_id, event_id),
        )

    detail = read_accepted_detail(path, version_id).detail
    search = search_accepted_insights(path, "narrower")
    retry = _service_for(path).record_judgment(version_id, "interesting")

    assert detail.current_role is AcceptedRole.HISTORICAL
    assert detail.historical_reason == "refuted"
    assert search.current == ()
    assert [item.insight_version_id for item in search.historical] == [version_id]
    assert list_current_accepted_inputs(path) == ()
    assert retry.kind == "already_recorded"
    assert retry.judgment.accepted_current_role == "historical"
    assert retry.judgment.accepted_historical_reason == "refuted"


def test_current_row_with_same_event_dual_facts_projects_refuted_primary_read_only(
    tmp_path,
):
    path = tmp_path / "current-dual-projection.sqlite3"
    _, version_id = _accepted_first(path)
    with connect(path) as connection:
        event_id = int(
            connection.execute(
                "SELECT produced_event_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
        connection.executemany(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, ?, ?, ?, '2026-08-21T10:01:00+00:00')
            """,
            [
                (version_id, "basis_invalid", event_id, "Additional invalid basis"),
                (version_id, "refuted", event_id, "Primary refutation"),
            ],
        )
    witness = connect(path)
    try:
        before = int(witness.execute("PRAGMA data_version").fetchone()[0])
        detail = read_accepted_detail(path, version_id).detail
        after = int(witness.execute("PRAGMA data_version").fetchone()[0])
    finally:
        witness.close()

    assert detail.current_role is AcceptedRole.HISTORICAL
    assert detail.historical_reason == "refuted"
    assert [fact.fact_kind for fact in detail.additional_exit_facts] == [
        "basis_invalid"
    ]
    assert list_current_accepted_inputs(path) == ()
    assert after == before


def test_stored_replacement_primary_remains_readable_with_later_disqualification(
    tmp_path,
):
    path = tmp_path / "replacement-primary-additional-disqualification.sqlite3"
    insight_id, version_id = _produce_first_version(path)
    _, kr_c = add_formal_knowledge(path, "replacement-primary-c")
    _, kr_d = add_formal_knowledge(path, "replacement-primary-d")
    replacement, _, _ = _service(
        path,
        plan=_core_replacement_plan(insight_id, kr_c, kr_d),
    )
    replacement_event = replacement.start_or_reuse().event_id
    assert replacement.drive(replacement_event).event.status == "succeeded"
    assert _service_for(path).record_judgment(
        version_id, "interesting"
    ).kind == "recorded"
    _, later_source = add_formal_knowledge(path, "later-disqualification")
    later_plan = empty_growth_plan_payload()
    later_plan["new_input_reviews"] = [
        {
            "knowledge_result_id": later_source,
            "outcome": "considered_no_formal_result",
            "reason_text": "Establish a later formal event",
        }
    ]
    later, _, _ = _service(path, plan=later_plan)
    later_event = later.start_or_reuse().event_id
    assert later.drive(later_event).event.status == "succeeded"
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, 'refuted', ?, 'Later fixture refutation',
                      '2026-08-21T10:02:00+00:00')
            """,
            (version_id, later_event),
        )

    detail_result = read_accepted_detail(path, version_id)
    search = search_accepted_insights(path, "narrower")

    assert detail_result.kind is AcceptedDetailKind.FOUND
    detail = detail_result.detail
    assert detail.historical_reason == "identity_replaced"
    assert detail.caused_by_event_id == replacement_event
    assert [fact.fact_kind for fact in detail.additional_exit_facts] == [
        "refuted"
    ]
    assert search.unreadable_count == 0
    assert search.historical[0].historical_reason == "identity_replaced"


def test_whole_collection_sql_failure_is_not_an_empty_result(tmp_path):
    with pytest.raises(AcceptedInsightLibraryError):
        search_accepted_insights(tmp_path, "anything")

    with pytest.raises(AcceptedInsightLibraryError):
        list_accepted_insights(tmp_path)


def test_unsafe_source_handoff_makes_accepted_detail_unreadable(tmp_path):
    path = tmp_path / "unsafe-handoff.sqlite3"
    _, version_id = _accepted_first(path)
    with connect(path) as connection:
        connection.execute(
            "UPDATE knowledge_results SET published_path = '../escape.md'"
        )

    assert read_accepted_detail(path, version_id).kind is AcceptedDetailKind.UNREADABLE
