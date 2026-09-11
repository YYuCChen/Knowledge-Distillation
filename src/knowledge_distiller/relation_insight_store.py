from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from .organization_models import (
    AcceptedDisqualification,
    CandidateVersionPlan,
    InputKind,
    NewRelationPlan,
    Participant,
    RelationAction,
    RelationReference,
    RelationRefKind,
    RelationReview,
    encode_insight_payload,
    encode_relation_payload,
    semantic_signature,
)


class DependencyChanged(sqlite3.IntegrityError):
    pass


@dataclass(frozen=True)
class ExistingRelationVersion:
    relation_id: int
    relation_version_id: int
    version_no: int
    produced_event_id: int


@dataclass(frozen=True)
class InsertedRelationVersion:
    relation_id: int
    relation_version_id: int
    version_no: int
    previous_version_id: int | None
    produced_event_id: int


@dataclass(frozen=True)
class InsertedInsightVersion:
    insight_id: int
    insight_version_id: int
    version_no: int
    previous_version_id: int | None
    produced_event_id: int


def insert_accepted_disqualification_fact(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    action: AcceptedDisqualification,
    created_at: str,
) -> None:
    cursor = connection.execute(
        """
        INSERT INTO insight_version_disqualifications (
            insight_version_id, fact_kind, event_id,
            reason_text, created_at
        )
        SELECT accepted.insight_version_id, ?, owner.event_id, ?, ?
        FROM accepted_insight_versions AS accepted
        JOIN insight_versions AS version
          ON version.insight_version_id = accepted.insight_version_id
         AND version.insight_id = accepted.insight_id
        JOIN organization_events AS produced
          ON produced.event_id = version.produced_event_id
         AND produced.status = 'succeeded'
        JOIN organization_event_accepted_boundary AS boundary
          ON boundary.insight_version_id = accepted.insight_version_id
         AND boundary.event_id = ?
        JOIN organization_events AS owner
          ON owner.event_id = boundary.event_id
         AND owner.status = 'running'
        WHERE accepted.insight_version_id = ?
          AND accepted.current_role = 'current'
          AND NOT EXISTS (
            SELECT 1 FROM insight_version_disqualifications AS existing
            WHERE existing.insight_version_id = accepted.insight_version_id
              AND (existing.event_id != owner.event_id OR existing.fact_kind = ?)
          )
          AND NOT EXISTS (
            SELECT 1 FROM insight_identity_replacements AS replacement
            WHERE replacement.replaced_insight_id = accepted.insight_id
          )
        """,
        (
            action.fact_kind.value,
            action.reason_text,
            created_at,
            event_id,
            action.insight_version_id,
            action.fact_kind.value,
        ),
    )
    if cursor.rowcount != 1:
        raise DependencyChanged(
            "DEPENDENCY_CHANGED: accepted disqualification target changed"
        )


def retire_accepted_for_disqualification(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    actions: tuple[AcceptedDisqualification, ...],
    historical_at: str,
) -> None:
    if not actions or len({action.fact_kind for action in actions}) != len(actions):
        raise ValueError("accepted disqualification group must contain distinct kinds")
    target_ids = {action.insight_version_id for action in actions}
    if len(target_ids) != 1:
        raise ValueError("accepted disqualification group must have one exact target")
    primary = next(
        (action for action in actions if action.fact_kind.value == "refuted"),
        actions[0],
    )
    target_id = primary.insight_version_id
    expected_count = len(actions)
    cursor = connection.execute(
        """
        UPDATE accepted_insight_versions AS accepted
        SET current_role = 'historical', historical_at = ?,
            historical_reason = ?, caused_by_event_id = ?,
            disqualification_reason = ?
        WHERE accepted.insight_version_id = ?
          AND accepted.current_role = 'current'
          AND EXISTS (
            SELECT 1 FROM insight_version_disqualifications AS fact
            WHERE fact.insight_version_id = accepted.insight_version_id
              AND fact.event_id = ? AND fact.fact_kind = ?
          )
          AND ? = (
            SELECT COUNT(*) FROM insight_version_disqualifications AS facts
            WHERE facts.insight_version_id = accepted.insight_version_id
          )
          AND ? = (
            SELECT COUNT(*) FROM insight_version_disqualifications AS facts
            WHERE facts.insight_version_id = accepted.insight_version_id
              AND facts.event_id = ?
          )
          AND NOT EXISTS (
            SELECT 1 FROM insight_identity_replacements AS replacement
            WHERE replacement.replaced_insight_id = accepted.insight_id
          )
        """,
        (
            historical_at,
            primary.fact_kind.value,
            event_id,
            primary.fact_kind.value,
            target_id,
            event_id,
            primary.fact_kind.value,
            expected_count,
            expected_count,
            event_id,
        ),
    )
    if cursor.rowcount != 1:
        raise DependencyChanged(
            "DEPENDENCY_CHANGED: accepted retirement target changed"
        )


def read_relation_version(
    connection: sqlite3.Connection, relation_version_id: int
) -> ExistingRelationVersion:
    row = connection.execute(
        """
        SELECT relation_id, relation_version_id, version_no, produced_event_id
        FROM relation_versions
        WHERE relation_version_id = ?
        """,
        (relation_version_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Relation version {relation_version_id} does not exist")
    return ExistingRelationVersion(
        int(row["relation_id"]),
        int(row["relation_version_id"]),
        int(row["version_no"]),
        int(row["produced_event_id"]),
    )


def insert_relation_version(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    plan: NewRelationPlan,
    dependency_signature: str,
    created_at: str,
) -> InsertedRelationVersion:
    if plan.target_kind == "create_identity":
        relation_id = int(
            connection.execute(
                """
                INSERT INTO relation_identities (created_event_id, created_at)
                VALUES (?, ?)
                """,
                (event_id, created_at),
            ).lastrowid
        )
        version_no = 1
        previous_version_id = None
    else:
        if plan.relation_id is None or plan.previous_relation_version_id is None:
            raise ValueError("Evolved relation target is incomplete")
        predecessor = read_relation_version(
            connection, plan.previous_relation_version_id
        )
        if predecessor.relation_id != plan.relation_id:
            raise DependencyChanged("DEPENDENCY_CHANGED: predecessor identity changed")
        relation_id = plan.relation_id
        version_no = predecessor.version_no + 1
        previous_version_id = predecessor.relation_version_id
    cursor = connection.execute(
        """
        INSERT INTO relation_versions (
            relation_id, version_no, previous_version_id, produced_event_id,
            payload_json, semantic_signature, dependency_signature, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            relation_id,
            version_no,
            previous_version_id,
            event_id,
            encode_relation_payload(plan.payload),
            semantic_signature(plan.payload),
            dependency_signature,
            created_at,
        ),
    )
    inserted = InsertedRelationVersion(
        relation_id,
        int(cursor.lastrowid),
        version_no,
        previous_version_id,
        event_id,
    )
    _insert_participants(
        connection,
        table="relation_version_participants",
        owner_column="relation_version_id",
        owner_id=inserted.relation_version_id,
        participants=plan.participants,
    )
    return inserted


def insert_insight_version(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    plan: CandidateVersionPlan,
    dependency_signature: str,
    created_at: str,
) -> InsertedInsightVersion:
    if plan.target_kind == "create_identity":
        insight_id = int(
            connection.execute(
                """
                INSERT INTO insight_identities (created_event_id, created_at)
                VALUES (?, ?)
                """,
                (event_id, created_at),
            ).lastrowid
        )
        version_no = 1
        previous_version_id = None
    else:
        if plan.insight_id is None or plan.previous_insight_version_id is None:
            raise ValueError("Evolved insight target is incomplete")
        row = connection.execute(
            """
            SELECT insight_id, version_no
            FROM insight_versions
            WHERE insight_version_id = ?
            """,
            (plan.previous_insight_version_id,),
        ).fetchone()
        if row is None or int(row["insight_id"]) != plan.insight_id:
            raise DependencyChanged("DEPENDENCY_CHANGED: insight predecessor changed")
        insight_id = plan.insight_id
        version_no = int(row["version_no"]) + 1
        previous_version_id = plan.previous_insight_version_id
    cursor = connection.execute(
        """
        INSERT INTO insight_versions (
            insight_id, version_no, previous_version_id, produced_event_id,
            payload_json, semantic_signature, dependency_signature, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            insight_id,
            version_no,
            previous_version_id,
            event_id,
            encode_insight_payload(plan.payload),
            semantic_signature(plan.payload),
            dependency_signature,
            created_at,
        ),
    )
    inserted = InsertedInsightVersion(
        insight_id,
        int(cursor.lastrowid),
        version_no,
        previous_version_id,
        event_id,
    )
    _insert_participants(
        connection,
        table="insight_version_participants",
        owner_column="insight_version_id",
        owner_id=inserted.insight_version_id,
        participants=plan.participants,
    )
    return inserted


def insert_evolved_fact(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    subject: ExistingRelationVersion,
    successor: InsertedRelationVersion,
    reason_text: str,
    created_at: str,
) -> None:
    if (
        successor.relation_id != subject.relation_id
        or successor.previous_version_id != subject.relation_version_id
        or successor.version_no != subject.version_no + 1
        or successor.produced_event_id != event_id
    ):
        raise DependencyChanged(
            "DEPENDENCY_CHANGED: evolved successor is not exact direct same-event successor"
        )
    connection.execute(
        """
        INSERT INTO relation_facts (
            event_id, relation_id, relation_version_id, fact_kind,
            successor_relation_version_id, replacement_relation_id,
            reason_text, created_at
        ) VALUES (?, ?, ?, 'evolved', ?, NULL, ?, ?)
        """,
        (
            event_id,
            subject.relation_id,
            subject.relation_version_id,
            successor.relation_version_id,
            reason_text,
            created_at,
        ),
    )


def activate_requalified_relation(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    subject: ExistingRelationVersion,
    reason_text: str,
    created_at: str,
) -> None:
    boundary = connection.execute(
        """
        SELECT 1
        FROM organization_event_relation_boundary
        WHERE event_id = ? AND relation_version_id = ?
          AND boundary_role = 'reconsideration_hint'
        """,
        (event_id, subject.relation_version_id),
    ).fetchone()
    if boundary is None:
        raise DependencyChanged("DEPENDENCY_CHANGED: relation is not an exact hint")
    current = connection.execute(
        "SELECT 1 FROM relation_current WHERE relation_id = ?",
        (subject.relation_id,),
    ).fetchone()
    if current is not None:
        raise DependencyChanged("DEPENDENCY_CHANGED: hint is already current")
    connection.execute(
        """
        INSERT INTO relation_facts (
            event_id, relation_id, relation_version_id, fact_kind,
            successor_relation_version_id, replacement_relation_id,
            reason_text, created_at
        ) VALUES (?, ?, ?, 'attention_activated', NULL, NULL, ?, ?)
        """,
        (
            event_id,
            subject.relation_id,
            subject.relation_version_id,
            reason_text,
            created_at,
        ),
    )
    cursor = connection.execute(
        """
        INSERT INTO relation_current (
            relation_id, relation_version_id, activated_at
        )
        SELECT ?, ?, ?
        WHERE EXISTS (
          SELECT 1 FROM organization_event_relation_boundary
          WHERE event_id = ? AND relation_version_id = ?
            AND boundary_role = 'reconsideration_hint'
        )
        """,
        (
            subject.relation_id,
            subject.relation_version_id,
            created_at,
            event_id,
            subject.relation_version_id,
        ),
    )
    if cursor.rowcount != 1:
        raise DependencyChanged("DEPENDENCY_CHANGED: hint activation failed")


def activate_inserted_relation(
    connection: sqlite3.Connection,
    inserted: InsertedRelationVersion,
    *,
    activated_at: str,
) -> None:
    cursor = connection.execute(
        """
        INSERT INTO relation_current (
            relation_id, relation_version_id, activated_at
        ) VALUES (?, ?, ?)
        ON CONFLICT(relation_id) DO UPDATE SET
          relation_version_id = excluded.relation_version_id,
          activated_at = excluded.activated_at
        WHERE relation_current.relation_version_id = ?
        """,
        (
            inserted.relation_id,
            inserted.relation_version_id,
            activated_at,
            inserted.previous_version_id,
        ),
    )
    if cursor.rowcount != 1:
        raise DependencyChanged("DEPENDENCY_CHANGED: relation current changed")


def apply_non_evolved_relation_review(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    review: RelationReview,
    replacement_relation_id: int | None,
    created_at: str,
) -> None:
    if review.action is RelationAction.UNCHANGED:
        return
    if review.action is RelationAction.ATTENTION:
        if review.attention_state == "activated":
            # Hint activation has a stricter helper and current-input activation is
            # already current, so this branch only records a current input no-op.
            row = connection.execute(
                "SELECT relation_id, version_no, produced_event_id FROM relation_versions WHERE relation_version_id = ?",
                (review.relation_version_id,),
            ).fetchone()
            if row is None:
                raise DependencyChanged("DEPENDENCY_CHANGED: relation disappeared")
            boundary = connection.execute(
                """
                SELECT boundary_role FROM organization_event_relation_boundary
                WHERE event_id = ? AND relation_version_id = ?
                """,
                (event_id, review.relation_version_id),
            ).fetchone()
            if boundary is not None and boundary["boundary_role"] == "reconsideration_hint":
                activate_requalified_relation(
                    connection,
                    event_id=event_id,
                    subject=ExistingRelationVersion(
                        int(row["relation_id"]),
                        review.relation_version_id,
                        int(row["version_no"]),
                        int(row["produced_event_id"]),
                    ),
                    reason_text=review.reason_text,
                    created_at=created_at,
                )
            return
        fact_kind = "attention_retired"
    elif review.action is RelationAction.BASIS_INVALID:
        fact_kind = "basis_invalid"
    elif review.action in {RelationAction.WRONG, RelationAction.WRONG_AND_REPLACED}:
        fact_kind = "wrong"
    elif review.action is RelationAction.REPLACED:
        fact_kind = "replaced"
    else:
        raise ValueError("Evolved review must use insert_evolved_fact")

    # wrong+replaced produces two independent facts but deletes current once below.
    fact_kinds = [fact_kind]
    if review.action is RelationAction.WRONG_AND_REPLACED:
        fact_kinds.append("replaced")
    for kind in fact_kinds:
        connection.execute(
            """
            INSERT INTO relation_facts (
                event_id, relation_id, relation_version_id, fact_kind,
                successor_relation_version_id, replacement_relation_id,
                reason_text, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                event_id,
                review.relation_id,
                review.relation_version_id,
                kind,
                replacement_relation_id if kind == "replaced" else None,
                review.reason_text,
                created_at,
            ),
        )
    connection.execute(
        """
        DELETE FROM relation_current
        WHERE relation_id = ? AND relation_version_id = ?
        """,
        (review.relation_id, review.relation_version_id),
    )


def insert_relation_used_edge(
    connection: sqlite3.Connection,
    *,
    owner: InsertedRelationVersion,
    relation_ref: RelationReference,
    position: int,
    post_h_current_set: frozenset[int],
) -> None:
    used_id = relation_ref.relation_version_id
    if used_id is None or used_id not in post_h_current_set:
        raise DependencyChanged("DEPENDENCY_CHANGED: relation ref is not post-H current")
    if relation_ref.ref_kind is RelationRefKind.BOUNDARY_CURRENT:
        predicate = """
          EXISTS (
            SELECT 1 FROM organization_event_relation_boundary AS b
            WHERE b.event_id = owner.produced_event_id
              AND b.relation_version_id = ?
              AND b.boundary_role = 'current_input'
          )
        """
        parameters = (used_id,)
    elif relation_ref.ref_kind is RelationRefKind.REQUALIFIED_CURRENT:
        predicate = """
          EXISTS (
            SELECT 1
            FROM organization_event_relation_boundary AS b
            JOIN relation_facts AS f
              ON f.event_id = b.event_id
             AND f.relation_version_id = b.relation_version_id
             AND f.fact_kind = 'attention_activated'
            WHERE b.event_id = owner.produced_event_id
              AND b.relation_version_id = ?
              AND b.boundary_role = 'reconsideration_hint'
          )
        """
        parameters = (used_id,)
    else:
        raise DependencyChanged("DEPENDENCY_CHANGED: relation owner cannot use planned ref")
    sql = f"""
        INSERT INTO relation_version_used_relations (
            relation_version_id, used_relation_version_id, position, role_text
        )
        SELECT owner.relation_version_id, current.relation_version_id, ?, ?
        FROM relation_versions AS owner
        JOIN relation_current AS current ON current.relation_version_id = ?
        WHERE owner.relation_version_id = ?
          AND {predicate}
    """
    cursor = connection.execute(
        sql,
        (position, relation_ref.role_text, used_id, owner.relation_version_id, *parameters),
    )
    if cursor.rowcount != 1:
        raise DependencyChanged("DEPENDENCY_CHANGED: relation edge qualification changed")


def insert_insight_used_edge(
    connection: sqlite3.Connection,
    *,
    owner: InsertedInsightVersion,
    relation_ref: RelationReference,
    resolved_relation_version_id: int,
    position: int,
    post_h_current_set: frozenset[int],
) -> None:
    if resolved_relation_version_id not in post_h_current_set:
        raise DependencyChanged("DEPENDENCY_CHANGED: insight relation is not post-H current")
    if relation_ref.ref_kind is RelationRefKind.BOUNDARY_CURRENT:
        predicate = """
          EXISTS (
            SELECT 1 FROM organization_event_relation_boundary AS b
            WHERE b.event_id = owner.produced_event_id
              AND b.relation_version_id = current.relation_version_id
              AND b.boundary_role = 'current_input'
          )
        """
    elif relation_ref.ref_kind is RelationRefKind.REQUALIFIED_CURRENT:
        predicate = """
          EXISTS (
            SELECT 1
            FROM organization_event_relation_boundary AS b
            JOIN relation_facts AS f
              ON f.event_id = b.event_id
             AND f.relation_version_id = b.relation_version_id
             AND f.fact_kind = 'attention_activated'
            WHERE b.event_id = owner.produced_event_id
              AND b.relation_version_id = current.relation_version_id
              AND b.boundary_role = 'reconsideration_hint'
          )
        """
    else:
        predicate = """
          EXISTS (
            SELECT 1 FROM relation_versions AS planned
            WHERE planned.relation_version_id = current.relation_version_id
              AND planned.produced_event_id = owner.produced_event_id
          )
        """
    cursor = connection.execute(
        f"""
        INSERT INTO insight_version_used_relations (
            insight_version_id, relation_version_id, position, role_text
        )
        SELECT owner.insight_version_id, current.relation_version_id, ?, ?
        FROM insight_versions AS owner
        JOIN relation_current AS current ON current.relation_version_id = ?
        WHERE owner.insight_version_id = ?
          AND {predicate}
        """,
        (
            position,
            relation_ref.role_text,
            resolved_relation_version_id,
            owner.insight_version_id,
        ),
    )
    if cursor.rowcount != 1:
        raise DependencyChanged("DEPENDENCY_CHANGED: insight edge qualification changed")


def insert_insight_replacement(
    connection: sqlite3.Connection,
    *,
    replaced_insight_id: int,
    replacement_insight_id: int,
    event_id: int,
    reason_text: str,
    created_at: str,
) -> None:
    cursor = connection.execute(
        """
        INSERT INTO insight_identity_replacements (
            replaced_insight_id, replacement_insight_id, event_id,
            reason_text, created_at
        )
        SELECT replaced.insight_id, replacement.insight_id,
               event.event_id, ?, ?
        FROM insight_identities AS replaced
        JOIN insight_identities AS replacement
          ON replacement.insight_id = ?
        JOIN organization_events AS event
          ON event.event_id = ? AND event.status = 'running'
        WHERE replaced.insight_id = ?
          AND replacement.created_event_id = event.event_id
          AND replaced.insight_id != replacement.insight_id
          AND NOT EXISTS (
            SELECT 1 FROM insight_identity_replacements AS existing
            WHERE existing.replaced_insight_id = replaced.insight_id
          )
        """,
        (
            reason_text,
            created_at,
            replacement_insight_id,
            event_id,
            replaced_insight_id,
        ),
    )
    if cursor.rowcount != 1:
        raise DependencyChanged(
            "DEPENDENCY_CHANGED: insight replacement target changed"
        )


def _insert_participants(
    connection: sqlite3.Connection,
    *,
    table: str,
    owner_column: str,
    owner_id: int,
    participants: tuple[Participant, ...],
) -> None:
    if (table, owner_column) not in {
        ("relation_version_participants", "relation_version_id"),
        ("insight_version_participants", "insight_version_id"),
    }:
        raise ValueError("Unsupported participant table")
    connection.executemany(
        f"""
        INSERT INTO {table} (
            {owner_column}, participant_key, input_kind,
            knowledge_result_id, point_id, accepted_insight_version_id,
            position, contribution_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                owner_id,
                item.participant_key,
                item.input_kind.value,
                item.knowledge_result_id,
                item.point_id,
                item.accepted_insight_version_id,
                item.position,
                item.contribution_text,
            )
            for item in participants
        ],
    )
