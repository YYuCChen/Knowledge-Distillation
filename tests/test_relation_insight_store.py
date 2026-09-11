from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from knowledge_distiller.database import connect, utc_now
from knowledge_distiller.organization_models import (
    AcceptedDisqualification,
    InsightDisqualificationKind,
    RelationReference,
    RelationRefKind,
    parse_growth_plan,
)
from knowledge_distiller.relation_insight_store import (
    DependencyChanged,
    ExistingRelationVersion,
    InsertedRelationVersion,
    activate_inserted_relation,
    activate_requalified_relation,
    insert_evolved_fact,
    insert_accepted_disqualification_fact,
    insert_insight_replacement,
    insert_insight_used_edge,
    insert_insight_version,
    insert_relation_used_edge,
    insert_relation_version,
    read_relation_version,
    retire_accepted_for_disqualification,
)
from tests.fixtures.growth import (
    add_formal_knowledge,
    empty_growth_plan_payload,
    insight_payload,
    relation_payload,
    source_participant,
)
from tests.test_insight_judgment_service import (
    _produce_first_version,
    _service_for,
)
from tests.test_organization_service import _service


def _insert_event(connection, source_rows, *, status="running"):
    now = utc_now()
    cursor = connection.execute(
        """
        INSERT INTO organization_events (
          status, started_at, completed_at, failure_code,
          topic_before_json, topic_before_signature, topic_guard_signature,
          boundary_signature, success_payload_json
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            status,
            now,
            now if status == "succeeded" else None,
            '{"codec":"topic-safety-snapshot-v1","state":"no_index","topics":[],"uncovered_count":2}',
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "{}" if status == "succeeded" else None,
        ),
    )
    event_id = int(cursor.lastrowid)
    for position, (knowledge_result_id, source_fact_id) in enumerate(source_rows):
        connection.execute(
            """
            INSERT INTO organization_event_source_boundary (
              event_id, knowledge_result_id, source_fact_id, boundary_role,
              position, qualification_signature
            ) VALUES (?, ?, ?, 'frozen_new', ?, ?)
            """,
            (
                event_id,
                knowledge_result_id,
                source_fact_id,
                position,
                f"{knowledge_result_id:064x}",
            ),
        )
    return event_id


def _relation_plan(kr1, kr2, *, key="r", evolve=None, used=()):
    payload = empty_growth_plan_payload()
    relation = {
        "new_relation_key": key,
        "target_kind": "create_identity" if evolve is None else "evolve_identity",
        "payload": relation_payload(statement=f"Relation {key}"),
        "participants": [
            source_participant("a", kr1, position=0),
            source_participant("b", kr2, position=1),
        ],
        "used_relations": list(used),
    }
    if evolve is not None:
        relation.update(
            relation_id=evolve.relation_id,
            previous_relation_version_id=evolve.relation_version_id,
        )
    payload["new_relations"] = [relation]
    return parse_growth_plan(payload).new_relations[0]


def _candidate_plan(kr1, kr2, used):
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [
        {
            "new_insight_key": "i",
            "target_kind": "create_identity",
            "payload": insight_payload(),
            "participants": [
                source_participant("a", kr1, position=0),
                source_participant("b", kr2, position=1),
            ],
            "used_relations": [used],
        }
    ]
    return parse_growth_plan(payload).candidate_versions[0]


@pytest.fixture
def store_graph(tmp_path):
    path = tmp_path / "growth.sqlite3"
    sf1, kr1 = add_formal_knowledge(path, "a")
    sf2, kr2 = add_formal_knowledge(path, "b")
    return path, ((kr1, sf1), (kr2, sf2)), kr1, kr2


def test_insert_evolved_fact_only_accepts_just_inserted_direct_successor(store_graph):
    """E2E-25: typed successor is same identity, consecutive and same event."""
    path, source_rows, kr1, kr2 = store_graph
    with connect(path) as connection:
        event1 = _insert_event(connection, source_rows)
        first = insert_relation_version(
            connection,
            event_id=event1,
            plan=_relation_plan(kr1, kr2),
            dependency_signature="a" * 64,
            created_at=utc_now(),
        )
        activate_inserted_relation(connection, first, activated_at=utc_now())
        connection.execute(
            "UPDATE organization_events SET status='succeeded', completed_at=?, success_payload_json='{}' WHERE event_id=?",
            (utc_now(), event1),
        )
        event2 = _insert_event(connection, source_rows)
        connection.execute(
            "INSERT INTO organization_event_relation_boundary VALUES (?, ?, 'current_input', 0, ?)",
            (event2, first.relation_version_id, "b" * 64),
        )
        successor = insert_relation_version(
            connection,
            event_id=event2,
            plan=_relation_plan(
                kr1,
                kr2,
                key="r-next",
                evolve=ExistingRelationVersion(
                    first.relation_id,
                    first.relation_version_id,
                    first.version_no,
                    first.produced_event_id,
                ),
            ),
            dependency_signature="c" * 64,
            created_at=utc_now(),
        )
        insert_evolved_fact(
            connection,
            event_id=event2,
            subject=read_relation_version(connection, first.relation_version_id),
            successor=successor,
            reason_text="Narrower boundary",
            created_at=utc_now(),
        )

        with pytest.raises(DependencyChanged, match="direct"):
            insert_evolved_fact(
                connection,
                event_id=event2,
                subject=read_relation_version(connection, first.relation_version_id),
                successor=replace(successor, produced_event_id=event1),
                reason_text="wrong event",
                created_at=utc_now(),
            )


@pytest.mark.parametrize(
    "fact_kind",
    [
        InsightDisqualificationKind.BASIS_INVALID,
        InsightDisqualificationKind.REFUTED,
    ],
)
def test_accepted_disqualification_store_inserts_fact_before_exact_retirement(
    tmp_path, fact_kind
):
    path = tmp_path / f"accepted-{fact_kind.value}.sqlite3"
    _, version_id = _produce_first_version(path)
    assert _service_for(path).record_judgment(version_id, "interesting").kind == (
        "recorded"
    )
    add_formal_knowledge(path, f"new-{fact_kind.value}")
    event_id = _service(path)[0].start_or_reuse().event_id
    action = AcceptedDisqualification(
        version_id,
        fact_kind,
        "Formal review established the exact current exit cause",
    )

    with connect(path) as connection:
        insert_accepted_disqualification_fact(
            connection,
            event_id=event_id,
            action=action,
            created_at=utc_now(),
        )
        assert connection.execute(
            "SELECT fact_kind FROM insight_version_disqualifications WHERE insight_version_id = ?",
            (version_id,),
        ).fetchone()[0] == fact_kind.value
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (version_id,),
        ).fetchone()[0] == "current"

        retire_accepted_for_disqualification(
            connection,
            event_id=event_id,
            actions=(action,),
            historical_at=utc_now(),
        )

        assert tuple(
            connection.execute(
                """
                SELECT current_role, historical_reason,
                       caused_by_event_id, disqualification_reason
                FROM accepted_insight_versions
                WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchone()
        ) == ("historical", fact_kind.value, event_id, fact_kind.value)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_accepted_disqualification_store_accepts_two_distinct_same_event_facts(
    tmp_path,
):
    path = tmp_path / "accepted-dual-store.sqlite3"
    _, version_id = _produce_first_version(path)
    assert _service_for(path).record_judgment(version_id, "interesting").kind == (
        "recorded"
    )
    add_formal_knowledge(path, "dual-store-boundary")
    event_id = _service(path)[0].start_or_reuse().event_id
    actions = tuple(
        AcceptedDisqualification(
            version_id,
            fact_kind,
            f"Formal review established {fact_kind.value}",
        )
        for fact_kind in (
            InsightDisqualificationKind.BASIS_INVALID,
            InsightDisqualificationKind.REFUTED,
        )
    )

    with connect(path) as connection:
        insert_accepted_disqualification_fact(
            connection,
            event_id=event_id,
            action=actions[0],
            created_at=utc_now(),
        )
        with pytest.raises(DependencyChanged, match="target changed"):
            insert_accepted_disqualification_fact(
                connection,
                event_id=event_id,
                action=actions[0],
                created_at=utc_now(),
            )
        insert_accepted_disqualification_fact(
            connection,
            event_id=event_id,
            action=actions[1],
            created_at=utc_now(),
        )
        retire_accepted_for_disqualification(
            connection,
            event_id=event_id,
            actions=actions,
            historical_at=utc_now(),
        )

        assert [
            row[0]
            for row in connection.execute(
                """
                SELECT fact_kind FROM insight_version_disqualifications
                WHERE insight_version_id = ? ORDER BY disqualification_id
                """,
                (version_id,),
            ).fetchall()
        ] == ["basis_invalid", "refuted"]
        assert tuple(
            connection.execute(
                """
                SELECT current_role, historical_reason
                FROM accepted_insight_versions WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchone()
        ) == ("historical", "refuted")


def test_accepted_disqualification_store_rejects_other_event_preexisting_cause(
    tmp_path,
):
    path = tmp_path / "accepted-existing-cause.sqlite3"
    _, version_id = _produce_first_version(path)
    assert _service_for(path).record_judgment(version_id, "interesting").kind == (
        "recorded"
    )
    with connect(path) as connection:
        produced_event_id = int(
            connection.execute(
                "SELECT produced_event_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id, reason_text, created_at
            ) VALUES (?, 'basis_invalid', ?, 'Existing cause', ?)
            """,
            (version_id, produced_event_id, utc_now()),
        )
    add_formal_knowledge(path, "new-existing-cause")
    event_id = _service(path)[0].start_or_reuse().event_id

    with connect(path) as connection:
        with pytest.raises(DependencyChanged, match="target changed"):
            insert_accepted_disqualification_fact(
                connection,
                event_id=event_id,
                action=AcceptedDisqualification(
                    version_id,
                    InsightDisqualificationKind.REFUTED,
                    "A second cause is forbidden",
                ),
                created_at=utc_now(),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_disqualifications WHERE insight_version_id = ?",
            (version_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (version_id,),
        ).fetchone()[0] == "current"


def test_insight_replacement_store_allows_preexisting_disqualification(tmp_path):
    path = tmp_path / "replacement-disqualification-drift.sqlite3"
    old_insight_id, old_version_id = _produce_first_version(path)
    with connect(path) as connection:
        produced_event_id = int(
            connection.execute(
                "SELECT produced_event_id FROM insight_versions WHERE insight_version_id = ?",
                (old_version_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id, reason_text, created_at
            ) VALUES (?, 'refuted', ?, 'Existing formal refutation', ?)
            """,
            (old_version_id, produced_event_id, utc_now()),
        )
    sf_c, kr_c = add_formal_knowledge(path, "replacement-c")
    sf_d, kr_d = add_formal_knowledge(path, "replacement-d")
    with connect(path) as connection:
        event_id = _insert_event(connection, ((kr_c, sf_c), (kr_d, sf_d)))

    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [
        {
            "new_insight_key": "replacement",
            "target_kind": "create_identity",
            "replaces_insight_id": old_insight_id,
            "payload": insight_payload(claim="A replacement core claim"),
            "participants": [
                source_participant("a", kr_c, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [],
        }
    ]
    candidate = parse_growth_plan(payload).candidate_versions[0]

    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        inserted = insert_insight_version(
            connection,
            event_id=event_id,
            plan=candidate,
            dependency_signature="a" * 64,
            created_at=utc_now(),
        )
        insert_insight_replacement(
            connection,
            replaced_insight_id=old_insight_id,
            replacement_insight_id=inserted.insight_id,
            event_id=event_id,
            reason_text="Later replacement is an additional formal fact",
            created_at=utc_now(),
        )
        connection.commit()
    finally:
        connection.close()

    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_identity_replacements"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_versions WHERE produced_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_disqualifications WHERE insight_version_id = ?",
            (old_version_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM organization_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == "running"


def test_accepted_disqualification_store_rejects_same_transaction_replacement(
    tmp_path,
):
    path = tmp_path / "disqualification-replacement-drift.sqlite3"
    old_insight_id, old_version_id = _produce_first_version(path)
    assert _service_for(path).record_judgment(
        old_version_id, "interesting"
    ).kind == "recorded"
    _, kr_c = add_formal_knowledge(path, "replacement-drift-c")
    _, kr_d = add_formal_knowledge(path, "replacement-drift-d")
    event_id = _service(path)[0].start_or_reuse().event_id
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [
        {
            "new_insight_key": "replacement",
            "target_kind": "create_identity",
            "replaces_insight_id": old_insight_id,
            "payload": insight_payload(claim="Replacement drift claim"),
            "participants": [
                source_participant("a", kr_c, position=0),
                source_participant("b", kr_d, position=1),
            ],
            "used_relations": [],
        }
    ]
    candidate = parse_growth_plan(payload).candidate_versions[0]
    action = AcceptedDisqualification(
        old_version_id,
        InsightDisqualificationKind.REFUTED,
        "Conflicting same-transaction refutation",
    )

    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        inserted = insert_insight_version(
            connection,
            event_id=event_id,
            plan=candidate,
            dependency_signature="a" * 64,
            created_at=utc_now(),
        )
        insert_insight_replacement(
            connection,
            replaced_insight_id=old_insight_id,
            replacement_insight_id=inserted.insight_id,
            event_id=event_id,
            reason_text="Same transaction replacement",
            created_at=utc_now(),
        )
        with pytest.raises(DependencyChanged, match="target changed"):
            insert_accepted_disqualification_fact(
                connection,
                event_id=event_id,
                action=action,
                created_at=utc_now(),
            )
        connection.rollback()
    finally:
        connection.close()

    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_identity_replacements"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM insight_version_disqualifications"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (old_version_id,),
        ).fetchone()[0] == "current"
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_boundary_current_edge_uses_insert_select_rowcount_and_rolls_back(store_graph):
    """E2E-43/§13.4B: exact current is rechecked by the same connection."""
    path, source_rows, kr1, kr2 = store_graph
    with connect(path) as connection:
        event1 = _insert_event(connection, source_rows)
        used = insert_relation_version(
            connection,
            event_id=event1,
            plan=_relation_plan(kr1, kr2, key="used"),
            dependency_signature="a" * 64,
            created_at=utc_now(),
        )
        activate_inserted_relation(connection, used, activated_at=utc_now())
        connection.execute(
            "UPDATE organization_events SET status='succeeded', completed_at=?, success_payload_json='{}' WHERE event_id=?",
            (utc_now(), event1),
        )
        event2 = _insert_event(connection, source_rows)
        connection.execute(
            "INSERT INTO organization_event_relation_boundary VALUES (?, ?, 'current_input', 0, ?)",
            (event2, used.relation_version_id, "b" * 64),
        )
        owner = insert_relation_version(
            connection,
            event_id=event2,
            plan=_relation_plan(kr1, kr2, key="owner"),
            dependency_signature="c" * 64,
            created_at=utc_now(),
        )
        activate_inserted_relation(connection, owner, activated_at=utc_now())
        reference = RelationReference(
            RelationRefKind.BOUNDARY_CURRENT,
            "Used current connection",
            relation_version_id=used.relation_version_id,
        )
        insert_relation_used_edge(
            connection,
            owner=owner,
            relation_ref=reference,
            position=0,
            post_h_current_set=frozenset(
                {used.relation_version_id, owner.relation_version_id}
            ),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM relation_version_used_relations"
        ).fetchone()[0] == 1

        connection.execute(
            "DELETE FROM relation_current WHERE relation_id = ?", (used.relation_id,)
        )
        another = insert_relation_version(
            connection,
            event_id=event2,
            plan=_relation_plan(kr1, kr2, key="another"),
            dependency_signature="d" * 64,
            created_at=utc_now(),
        )
        with pytest.raises(DependencyChanged, match="edge qualification"):
            insert_relation_used_edge(
                connection,
                owner=another,
                relation_ref=reference,
                position=0,
                post_h_current_set=frozenset({used.relation_version_id}),
            )


def test_requalified_hint_requires_same_event_activation_fact_and_current(store_graph):
    """E2E-45: hint boundary alone has no used-edge permission."""
    path, source_rows, kr1, kr2 = store_graph
    with connect(path) as connection:
        event1 = _insert_event(connection, source_rows)
        hint = insert_relation_version(
            connection,
            event_id=event1,
            plan=_relation_plan(kr1, kr2, key="hint"),
            dependency_signature="a" * 64,
            created_at=utc_now(),
        )
        connection.execute(
            "UPDATE organization_events SET status='succeeded', completed_at=?, success_payload_json='{}' WHERE event_id=?",
            (utc_now(), event1),
        )
        event2 = _insert_event(connection, source_rows)
        connection.execute(
            "INSERT INTO organization_event_relation_boundary VALUES (?, ?, 'reconsideration_hint', 0, ?)",
            (event2, hint.relation_version_id, "b" * 64),
        )
        owner = insert_relation_version(
            connection,
            event_id=event2,
            plan=_relation_plan(kr1, kr2, key="owner"),
            dependency_signature="c" * 64,
            created_at=utc_now(),
        )
        reference = RelationReference(
            RelationRefKind.REQUALIFIED_CURRENT,
            "Requalified exact connection",
            relation_version_id=hint.relation_version_id,
        )
        with pytest.raises(DependencyChanged):
            insert_relation_used_edge(
                connection,
                owner=owner,
                relation_ref=reference,
                position=0,
                post_h_current_set=frozenset({hint.relation_version_id}),
            )

        activate_requalified_relation(
            connection,
            event_id=event2,
            subject=ExistingRelationVersion(
                hint.relation_id,
                hint.relation_version_id,
                hint.version_no,
                hint.produced_event_id,
            ),
            reason_text="Requalified from scratch",
            created_at=utc_now(),
        )
        insert_relation_used_edge(
            connection,
            owner=owner,
            relation_ref=reference,
            position=0,
            post_h_current_set=frozenset({hint.relation_version_id}),
        )


def test_planned_stable_candidate_edge_requires_same_event_current(store_graph):
    """E2E-43: same-event relation is current before candidate edge is inserted."""
    path, source_rows, kr1, kr2 = store_graph
    with connect(path) as connection:
        event = _insert_event(connection, source_rows)
        relation = insert_relation_version(
            connection,
            event_id=event,
            plan=_relation_plan(kr1, kr2, key="planned"),
            dependency_signature="a" * 64,
            created_at=utc_now(),
        )
        activate_inserted_relation(connection, relation, activated_at=utc_now())
        reference = {
            "ref_kind": "planned_stable",
            "new_relation_key": "planned",
            "role_text": "Same-event connection",
        }
        candidate_plan = _candidate_plan(kr1, kr2, reference)
        insight = insert_insight_version(
            connection,
            event_id=event,
            plan=candidate_plan,
            dependency_signature="b" * 64,
            created_at=utc_now(),
        )
        insert_insight_used_edge(
            connection,
            owner=insight,
            relation_ref=candidate_plan.used_relations[0],
            resolved_relation_version_id=relation.relation_version_id,
            position=0,
            post_h_current_set=frozenset({relation.relation_version_id}),
        )
        assert connection.execute(
            "SELECT relation_version_id FROM insight_version_used_relations"
        ).fetchone()[0] == relation.relation_version_id

        connection.execute(
            "DELETE FROM relation_current WHERE relation_id = ?", (relation.relation_id,)
        )
        another = insert_insight_version(
            connection,
            event_id=event,
            plan=replace(candidate_plan, new_insight_key="i2"),
            dependency_signature="c" * 64,
            created_at=utc_now(),
        )
        with pytest.raises(DependencyChanged):
            insert_insight_used_edge(
                connection,
                owner=another,
                relation_ref=candidate_plan.used_relations[0],
                resolved_relation_version_id=relation.relation_version_id,
                position=0,
                post_h_current_set=frozenset({relation.relation_version_id}),
            )
