from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping

from .accepted_insight_library import (
    _load_current_accepted_inputs as _load_current_accepted_inputs_shared,
    _load_current_source_inputs as _load_current_source_inputs_shared,
)
from .database import connect, utc_now
from .growth_modeling import (
    GrowthModelFailure,
    HistoricalRecallPlanner,
    RecallSelection,
    RelationInsightPlanner,
    identity_catalog_to_dict,
)
from .growth_qualification import (
    GrowthQualificationError,
    QualificationContext,
    qualify_growth_plan,
)
from .organization_models import (
    AcceptedInsightInput,
    EventStatus,
    GrowthBoundary,
    GrowthIdentityCatalog,
    InsightCatalogState,
    InsightIdentityCatalogEntry,
    InputKind,
    OrganizationEventRead,
    OrganizationFailureCode,
    OrganizationSuccess,
    PendingCandidateList,
    PendingCandidateRead,
    Participant,
    RelationAction,
    RelationBoundaryInput,
    RelationIdentityCatalogEntry,
    RelationRefKind,
    SourceKnowledgeInput,
    SourcePointIdentity,
    UsedRelationInput,
    build_evolution_basis_card,
    decode_insight_payload,
    decode_relation_payload,
    decode_success_payload,
    encode_success_payload,
    canonical_json,
    insight_payload_to_dict,
    semantic_signature,
    sha256_json,
)
from .relation_insight_store import (
    DependencyChanged,
    activate_inserted_relation,
    apply_non_evolved_relation_review,
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
from .topic_indexing import (
    ExistingTopicInput,
    TopicIndexFailure,
    TopicIndexer,
    TopicPlan,
    TopicPointInput,
    TopicPointReference,
)
from .topic_library import (
    build_topic_overwrite_guard,
    compute_source_signature,
    load_topic_planning_input,
    replace_topic_index,
    topic_plan_payload,
)


logger = logging.getLogger(__name__)


class OrganizationStartKind(StrEnum):
    STARTED = "started"
    REUSED = "reused"
    EMPTY = "empty"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class OrganizationStartResult:
    kind: OrganizationStartKind
    event_id: int | None = None


class OrganizationDriveKind(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class OrganizationDriveResult:
    kind: OrganizationDriveKind
    event: OrganizationEventRead

    @property
    def baseline_changed(self) -> bool:
        return self.event.failure_code is OrganizationFailureCode.TOPIC_BASELINE_CHANGED


@dataclass(frozen=True)
class OrganizationEventView:
    event: OrganizationEventRead | None
    driver_active: bool


class OrganizationReadError(Exception):
    pass


class _FrozenInputIneligible(Exception):
    pass


class _TopicBaselineChanged(Exception):
    pass


FailureInjector = Callable[[str, sqlite3.Connection], None]


class OrganizationService:
    def __init__(
        self,
        database_path: Path,
        *,
        topic_indexer: TopicIndexer,
        recall_planner: HistoricalRecallPlanner,
        relation_insight_planner: RelationInsightPlanner,
        failure_injector: FailureInjector | None = None,
        source_loader=None,
        topic_store=None,
    ):
        self.source_loader = source_loader
        self.topic_store = topic_store
        self.database_path = database_path
        self.topic_indexer = topic_indexer
        self.recall_planner = recall_planner
        self.relation_insight_planner = relation_insight_planner
        self.failure_injector = failure_injector
        self._driver_lock = threading.Lock()
        self._driver_condition = threading.Condition()
        self._active_driver_events: set[int] = set()

    def _read_boundary(self, connection, event_id):
        if self.source_loader is None:
            return _load_event_boundary(connection, event_id)
        return _load_event_boundary(connection, event_id, self.source_loader)

    def start_or_reuse(self) -> OrganizationStartResult:
        try:
            with connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                running = connection.execute(
                    """
                    SELECT event_id FROM organization_events
                    WHERE status = 'running'
                    """
                ).fetchone()
                if running is not None:
                    return OrganizationStartResult(
                        OrganizationStartKind.REUSED, int(running["event_id"])
                    )
                sources = (self.source_loader or _load_all_eligible_sources)(connection)
                covered = {
                    int(row["knowledge_result_id"])
                    for row in connection.execute(
                        "SELECT knowledge_result_id FROM organization_event_coverages"
                    ).fetchall()
                }
                frozen = tuple(
                    item for item in sources if item.knowledge_result_id not in covered
                )
                if not frozen:
                    connection.rollback()
                    return OrganizationStartResult(OrganizationStartKind.EMPTY)
                planning = (self.topic_store.planning(connection) if self.topic_store is not None
                            else load_topic_planning_input(connection))
                accepted = _load_current_accepted_inputs(connection, sources)
                current_relations, hints = _load_relation_inputs(
                    connection, sources, accepted
                )
                boundary_payload = _boundary_payload(
                    frozen,
                    tuple(
                        item
                        for item in sources
                        if item.knowledge_result_id not in {
                            value.knowledge_result_id for value in frozen
                        }
                    ),
                    accepted,
                    current_relations,
                    hints,
                )
                started_at = utc_now()
                cursor = connection.execute(
                    """
                    INSERT INTO organization_events (
                        status, started_at, completed_at, failure_code,
                        topic_before_json, topic_before_signature,
                        topic_guard_signature, boundary_signature,
                        success_payload_json
                    ) VALUES (
                        'running', ?, NULL, NULL, ?, ?, ?, ?, NULL
                    )
                    """,
                    (
                        started_at,
                        canonical_json(planning.before_payload),
                        planning.before_signature,
                        planning.guard_signature,
                        sha256_json(boundary_payload),
                    ),
                )
                event_id = int(cursor.lastrowid)
                frozen_ids = {item.knowledge_result_id for item in frozen}
                source_positions = {"frozen_new": 0, "eligible_history": 0}
                for source in sources:
                    role = (
                        "frozen_new"
                        if source.knowledge_result_id in frozen_ids
                        else "eligible_history"
                    )
                    position = source_positions[role]
                    source_positions[role] += 1
                    connection.execute(
                        """
                        INSERT INTO organization_event_source_boundary (
                            event_id, knowledge_result_id, source_fact_id,
                            boundary_role, position, qualification_signature
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            source.knowledge_result_id,
                            source.source_fact_id,
                            role,
                            position,
                            source.qualification_signature,
                        ),
                    )
                for position, item in enumerate(accepted):
                    connection.execute(
                        """
                        INSERT INTO organization_event_accepted_boundary (
                            event_id, insight_version_id, position,
                            qualification_signature
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            item.insight_version_id,
                            position,
                            item.qualification_signature,
                        ),
                    )
                relations = sorted(
                    (*current_relations, *hints),
                    key=lambda item: (
                        0 if item.boundary_role == "current_input" else 1,
                        item.relation_id,
                        item.relation_version_id,
                    ),
                )
                for position, item in enumerate(relations):
                    connection.execute(
                        """
                        INSERT INTO organization_event_relation_boundary (
                            event_id, relation_version_id, boundary_role,
                            position, qualification_signature
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            item.relation_version_id,
                            item.boundary_role,
                            position,
                            item.qualification_signature,
                        ),
                    )
                return OrganizationStartResult(OrganizationStartKind.STARTED, event_id)
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return OrganizationStartResult(OrganizationStartKind.READ_FAILED)

    def drive(self, event_id: int) -> OrganizationDriveResult:
        with self._driver_condition:
            while event_id in self._active_driver_events:
                self._driver_condition.wait()
            self._active_driver_events.add(event_id)
        try:
            return self._drive_owned(event_id)
        finally:
            with self._driver_condition:
                self._active_driver_events.discard(event_id)
                self._driver_condition.notify_all()

    def read_event_view(self, event_id: int) -> OrganizationEventView:
        with self._driver_condition:
            return OrganizationEventView(
                event=read_event(self.database_path, event_id),
                driver_active=event_id in self._active_driver_events,
            )

    def _drive_owned(self, event_id: int) -> OrganizationDriveResult:
        with self._driver_lock:
            current = read_event(self.database_path, event_id)
            if current is None:
                raise LookupError(f"Organization event {event_id} does not exist")
            if current.status is not EventStatus.RUNNING:
                return OrganizationDriveResult(OrganizationDriveKind.TERMINAL, current)

            for formation_cycle in range(2):
                try:
                    with connect(self.database_path) as connection:
                        boundary = self._read_boundary(connection, event_id)
                        before_payload = _read_topic_before(connection, event_id)
                        identity_catalog = _load_identity_catalog(connection)
                    topic_points = _topic_points(boundary)
                    existing_topics = _existing_topics_from_before(before_payload)
                    if not self.topic_indexer.is_available():
                        return self._fail(
                            event_id, OrganizationFailureCode.TOPIC_PLANNING_FAILED
                        )
                    topic_indexing = self.topic_indexer.organize(
                        topic_points, existing_topics
                    )
                    if topic_indexing.plan is None:
                        return self._fail(
                            event_id, OrganizationFailureCode.TOPIC_PLANNING_FAILED
                        )
                    if not self.recall_planner.is_available():
                        return self._fail(event_id, OrganizationFailureCode.RECALL_FAILED)
                    recall = self.recall_planner.recall(boundary)
                    if recall.selection is None:
                        return self._fail(event_id, OrganizationFailureCode.RECALL_FAILED)
                    if not self.relation_insight_planner.is_available():
                        return self._fail(
                            event_id, OrganizationFailureCode.GROWTH_PLANNING_FAILED
                        )
                    growth = self.relation_insight_planner.plan(
                        boundary,
                        recall.selection,
                        identity_catalog=identity_catalog,
                        expanded_inputs=_expanded_inputs(boundary, recall.selection),
                        topic_before=before_payload,
                        topic_plan=topic_plan_payload(topic_indexing.plan),
                    )
                    if growth.plan is None:
                        code = (
                            OrganizationFailureCode.INVALID_MODEL_OUTPUT
                            if growth.failure
                            in {GrowthModelFailure.INCOMPLETE, GrowthModelFailure.INVALID_OUTPUT}
                            else OrganizationFailureCode.GROWTH_PLANNING_FAILED
                        )
                        return self._fail(event_id, code)
                    qualified = qualify_growth_plan(
                        growth.plan,
                        _qualification_context(
                            boundary,
                            identity_catalog,
                            selected_source_knowledge_ids=frozenset(
                                recall.selection.source_knowledge_ids
                            ),
                            selected_accepted_insight_version_ids=frozenset(
                                recall.selection.accepted_insight_version_ids
                            ),
                            selected_current_relation_version_ids=frozenset(
                                recall.selection.current_relation_version_ids
                            ),
                            selected_reconsideration_hint_version_ids=frozenset(
                                recall.selection.reconsideration_hint_version_ids
                            ),
                        ),
                    )
                    _qualify_topic_changes(
                        before_payload,
                        topic_indexing.plan,
                        qualified.plan.topic_change_assessments,
                    )
                    with connect(self.database_path) as connection:
                        fresh_boundary = self._read_boundary(connection, event_id)
                        fresh_identity_catalog = _load_identity_catalog(connection)
                    try:
                        fresh_qualified = qualify_growth_plan(
                            growth.plan,
                            _qualification_context(
                                fresh_boundary,
                                fresh_identity_catalog,
                                selected_source_knowledge_ids=frozenset(
                                    recall.selection.source_knowledge_ids
                                ),
                                selected_accepted_insight_version_ids=frozenset(
                                    recall.selection.accepted_insight_version_ids
                                ),
                                selected_current_relation_version_ids=frozenset(
                                    recall.selection.current_relation_version_ids
                                ),
                                selected_reconsideration_hint_version_ids=frozenset(
                                    recall.selection.reconsideration_hint_version_ids
                                ),
                            ),
                        )
                    except GrowthQualificationError:
                        if formation_cycle == 0:
                            continue
                        raise
                    if fresh_qualified.dependency_signature != qualified.dependency_signature:
                        if formation_cycle == 0:
                            continue
                        return self._fail(
                            event_id, OrganizationFailureCode.DEPENDENCY_CHANGED
                        )
                    return self._commit_success(
                        event_id,
                        fresh_identity_catalog.signature,
                        topic_indexing.plan,
                        fresh_qualified,
                        frozenset(recall.selection.source_knowledge_ids),
                        frozenset(recall.selection.accepted_insight_version_ids),
                        frozenset(recall.selection.current_relation_version_ids),
                        frozenset(recall.selection.reconsideration_hint_version_ids),
                    )
                except _FrozenInputIneligible:
                    return self._fail(
                        event_id, OrganizationFailureCode.FROZEN_INPUT_INELIGIBLE
                    )
                except GrowthQualificationError as error:
                    logger.warning(
                        "Organization qualification failed: "
                        "event_id=%s stage=planning code=%s",
                        event_id,
                        error.code,
                    )
                    return self._fail(
                        event_id, OrganizationFailureCode.QUALIFICATION_FAILED
                    )
                except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
                    return self._fail(
                        event_id, OrganizationFailureCode.INPUT_READ_FAILED
                    )
            return self._fail(event_id, OrganizationFailureCode.DEPENDENCY_CHANGED)

    def _commit_success(
        self,
        event_id: int,
        planned_identity_catalog_signature: str,
        topic_plan: TopicPlan,
        qualified,
        selected_source_knowledge_ids: frozenset[int],
        selected_accepted_insight_version_ids: frozenset[int],
        selected_current_relation_version_ids: frozenset[int],
        selected_reconsideration_hint_version_ids: frozenset[int],
    ) -> OrganizationDriveResult:
        failure_code: OrganizationFailureCode | None = None
        try:
            connection = connect(self.database_path)
            try:
                self._inject("before_begin_immediate", connection)
                connection.execute("BEGIN IMMEDIATE")
                event = connection.execute(
                    "SELECT status, topic_guard_signature FROM organization_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if event is None:
                    raise LookupError(f"Organization event {event_id} does not exist")
                if event["status"] != "running":
                    connection.rollback()
                    terminal = read_event(self.database_path, event_id)
                    assert terminal is not None
                    return OrganizationDriveResult(OrganizationDriveKind.TERMINAL, terminal)
                current_boundary = self._read_boundary(connection, event_id)
                current_identity_catalog = _load_identity_catalog(connection)
                if (
                    current_identity_catalog.signature
                    != planned_identity_catalog_signature
                ):
                    raise DependencyChanged(
                        "DEPENDENCY_CHANGED: identity catalog changed"
                    )
                current_qualified = qualify_growth_plan(
                    qualified.plan,
                    _qualification_context(
                        current_boundary,
                        current_identity_catalog,
                        selected_source_knowledge_ids=(
                            selected_source_knowledge_ids
                        ),
                        selected_accepted_insight_version_ids=(
                            selected_accepted_insight_version_ids
                        ),
                        selected_current_relation_version_ids=(
                            selected_current_relation_version_ids
                        ),
                        selected_reconsideration_hint_version_ids=(
                            selected_reconsideration_hint_version_ids
                        ),
                    ),
                )
                if current_qualified.dependency_signature != qualified.dependency_signature:
                    raise DependencyChanged("DEPENDENCY_CHANGED: final dependency changed")
                current_guard = sha256_json(self.topic_store.guard(connection) if self.topic_store is not None
                                           else build_topic_overwrite_guard(connection))
                if current_guard != str(event["topic_guard_signature"]):
                    raise _TopicBaselineChanged
                valid_points = {point.reference for point in _topic_points(current_boundary)}
                planned_points = {
                    member for topic in topic_plan.topics for member in topic.members
                } | set(topic_plan.unassigned_points)
                if planned_points != valid_points:
                    raise GrowthQualificationError(
                        "topic_point_coverage", "Topic plan no longer matches event source boundary"
                    )
                before_payload = _read_topic_before(connection, event_id)
                topic_changes = _qualify_topic_changes(
                    before_payload,
                    topic_plan,
                    current_qualified.plan.topic_change_assessments,
                )
                # A second no-model qualification under the write lock is the G gate.
                qualified = current_qualified
                self._inject("after_final_qualification", connection)
                now = utc_now()
                reviews_by_id = {
                    review.relation_id: review
                    for review in qualified.plan.relation_reviews
                }
                hint_versions = {
                    item.relation_version_id
                    for item in current_boundary.reconsideration_hints
                }
                # Requalified exact hints become current before any used edge is formed.
                for review in qualified.plan.relation_reviews:
                    if (
                        review.relation_version_id in hint_versions
                        and review.action is RelationAction.ATTENTION
                        and review.attention_state == "activated"
                    ):
                        apply_non_evolved_relation_review(
                            connection,
                            event_id=event_id,
                            review=review,
                            replacement_relation_id=None,
                            created_at=now,
                        )
                relation_map = {}
                for relation in qualified.plan.new_relations:
                    inserted = insert_relation_version(
                        connection,
                        event_id=event_id,
                        plan=relation,
                        dependency_signature=qualified.dependency_signature,
                        created_at=now,
                    )
                    relation_map[relation.new_relation_key] = inserted
                    if relation.target_kind == "evolve_identity":
                        assert relation.previous_relation_version_id is not None
                        subject = read_relation_version(
                            connection, relation.previous_relation_version_id
                        )
                        review = reviews_by_id[inserted.relation_id]
                        insert_evolved_fact(
                            connection,
                            event_id=event_id,
                            subject=subject,
                            successor=inserted,
                            reason_text=review.reason_text,
                            created_at=now,
                        )
                    activate_inserted_relation(
                        connection, inserted, activated_at=now
                    )
                    self._inject("after_relation_version", connection)
                for review in qualified.plan.relation_reviews:
                    if review.action is RelationAction.EVOLVED:
                        continue
                    if (
                        review.relation_version_id in hint_versions
                        and review.action is RelationAction.ATTENTION
                        and review.attention_state == "activated"
                    ):
                        continue
                    replacement_id = None
                    if review.replacement_new_relation_key is not None:
                        replacement_id = relation_map[
                            review.replacement_new_relation_key
                        ].relation_id
                    apply_non_evolved_relation_review(
                        connection,
                        event_id=event_id,
                        review=review,
                        replacement_relation_id=replacement_id,
                        created_at=now,
                    )
                post_h_current_set = frozenset(
                    int(row["relation_version_id"])
                    for row in connection.execute(
                        "SELECT relation_version_id FROM relation_current"
                    ).fetchall()
                )
                for relation in qualified.plan.new_relations:
                    owner = relation_map[relation.new_relation_key]
                    for position, ref in enumerate(relation.used_relations):
                        insert_relation_used_edge(
                            connection,
                            owner=owner,
                            relation_ref=ref,
                            position=position,
                            post_h_current_set=post_h_current_set,
                        )
                self._inject("after_relation_edges", connection)
                insight_map = {}
                for candidate in qualified.plan.candidate_versions:
                    inserted = insert_insight_version(
                        connection,
                        event_id=event_id,
                        plan=candidate,
                        dependency_signature=qualified.dependency_signature,
                        created_at=now,
                    )
                    insight_map[candidate.new_insight_key] = inserted
                    for position, ref in enumerate(candidate.used_relations):
                        resolved_id = (
                            relation_map[ref.new_relation_key].relation_version_id
                            if ref.ref_kind is RelationRefKind.PLANNED_STABLE
                            else ref.relation_version_id
                        )
                        assert resolved_id is not None
                        insert_insight_used_edge(
                            connection,
                            owner=inserted,
                            relation_ref=ref,
                            resolved_relation_version_id=resolved_id,
                            position=position,
                            post_h_current_set=post_h_current_set,
                        )
                    if candidate.replaces_insight_id is not None:
                        insert_insight_replacement(
                            connection,
                            replaced_insight_id=candidate.replaces_insight_id,
                            replacement_insight_id=inserted.insight_id,
                            event_id=event_id,
                            reason_text="Core claim replaced by qualified candidate",
                            created_at=now,
                        )
                        connection.execute(
                            """
                            UPDATE accepted_insight_versions
                            SET current_role = 'historical', historical_at = ?,
                                historical_reason = 'identity_replaced',
                                caused_by_event_id = ?, replacement_insight_id = ?
                            WHERE insight_id = ? AND current_role = 'current'
                            """,
                            (
                                now,
                                event_id,
                                inserted.insight_id,
                                candidate.replaces_insight_id,
                            ),
                        )
                    self._inject("after_candidate", connection)
                disqualifications_by_target = {}
                for action in qualified.plan.accepted_disqualifications:
                    disqualifications_by_target.setdefault(
                        action.insight_version_id, []
                    ).append(action)
                for actions in disqualifications_by_target.values():
                    ordered_actions = tuple(
                        sorted(
                            actions,
                            key=lambda item: (
                                0 if item.fact_kind.value == "basis_invalid" else 1
                            ),
                        )
                    )
                    for action in ordered_actions:
                        insert_accepted_disqualification_fact(
                            connection,
                            event_id=event_id,
                            action=action,
                            created_at=now,
                        )
                        self._inject(
                            "after_accepted_disqualification_fact", connection
                        )
                    retire_accepted_for_disqualification(
                        connection,
                        event_id=event_id,
                        actions=ordered_actions,
                        historical_at=now,
                    )
                    self._inject(
                        "after_accepted_disqualification_retirement",
                        connection,
                    )
                topic_signature = compute_source_signature(
                    _topic_points(current_boundary)
                )
                if self.topic_store is None:
                    replace_topic_index(connection, topic_plan, topic_signature)
                else:
                    self.topic_store.replace(connection, topic_plan, current_boundary)
                self._inject("after_topic_replace", connection)
                success = OrganizationSuccess(
                    n=len(current_boundary.frozen_new),
                    m=len(topic_changes),
                    k=len(insight_map),
                    topic_after={
                        "codec": "topic-after-v1",
                        "source_signature": topic_signature,
                        "plan": topic_plan_payload(topic_plan),
                    },
                    topic_changes=topic_changes,
                    new_input_reviews=qualified.plan.new_input_reviews,
                    relation_reviews=qualified.plan.relation_reviews,
                    accepted_disqualifications=(
                        qualified.plan.accepted_disqualifications
                    ),
                    rejected_counts=tuple(
                        sorted(
                            Counter(
                                item.output_kind
                                for item in qualified.plan.rejected_outputs
                            ).items()
                        )
                    ),
                    dependency_signature=qualified.dependency_signature,
                    relation_local_map=tuple(
                        sorted(
                            (
                                key,
                                inserted.relation_version_id,
                            )
                            for key, inserted in relation_map.items()
                        )
                    ),
                    insight_local_map=tuple(
                        sorted(
                            (key, inserted.insight_version_id)
                            for key, inserted in insight_map.items()
                        )
                    ),
                    final_required_sources=qualified.final_required_source_set,
                    final_required_accepted=qualified.final_required_accepted_set,
                    final_required_boundary_relations=(
                        qualified.final_required_boundary_relation_set
                    ),
                    final_reconsideration_hints=(
                        qualified.final_reconsideration_hint_set
                    ),
                    final_requalified_current=(
                        qualified.final_requalified_current_set
                    ),
                    final_required_planned_relations=(
                        qualified.final_required_planned_relation_set
                    ),
                    recursive_dependency_closure=_accepted_dependency_closure(
                        current_boundary.accepted_current,
                        qualified.final_required_accepted_set,
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE organization_events
                    SET status = 'succeeded', completed_at = ?,
                        success_payload_json = ?
                    WHERE event_id = ? AND status = 'running'
                    """,
                    (now, encode_success_payload(success), event_id),
                )
                if updated.rowcount != 1:
                    raise DependencyChanged("DEPENDENCY_CHANGED: event terminal changed")
                self._inject("after_event_success", connection)
                frozen_rows = connection.execute(
                    """
                    SELECT knowledge_result_id, source_fact_id
                    FROM organization_event_source_boundary
                    WHERE event_id = ? AND boundary_role = 'frozen_new'
                    ORDER BY position
                    """,
                    (event_id,),
                ).fetchall()
                if len(frozen_rows) != len(current_boundary.frozen_new):
                    raise DependencyChanged("DEPENDENCY_CHANGED: frozen coverage changed")
                for row in frozen_rows:
                    connection.execute(
                        """
                        INSERT INTO organization_event_coverages (
                            event_id, knowledge_result_id, source_fact_id, covered_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            int(row["knowledge_result_id"]),
                            int(row["source_fact_id"]),
                            now,
                        ),
                    )
                    self._inject("after_coverage", connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        except _TopicBaselineChanged:
            failure_code = OrganizationFailureCode.TOPIC_BASELINE_CHANGED
        except _FrozenInputIneligible:
            failure_code = OrganizationFailureCode.FROZEN_INPUT_INELIGIBLE
        except DependencyChanged:
            failure_code = OrganizationFailureCode.DEPENDENCY_CHANGED
        except GrowthQualificationError as error:
            logger.warning(
                "Organization qualification failed: "
                "event_id=%s stage=final_lock code=%s",
                event_id,
                error.code,
            )
            failure_code = OrganizationFailureCode.QUALIFICATION_FAILED
        except sqlite3.Error:
            failure_code = OrganizationFailureCode.PERSISTENCE_FAILED
        if failure_code is not None:
            return self._fail(event_id, failure_code)
        event = read_event(self.database_path, event_id)
        assert event is not None
        return OrganizationDriveResult(OrganizationDriveKind.SUCCEEDED, event)

    def _fail(
        self, event_id: int, code: OrganizationFailureCode
    ) -> OrganizationDriveResult:
        fail_event(self.database_path, event_id, code)
        event = read_event(self.database_path, event_id)
        assert event is not None
        kind = (
            OrganizationDriveKind.FAILED
            if event.status is EventStatus.FAILED
            else OrganizationDriveKind.TERMINAL
        )
        return OrganizationDriveResult(kind, event)

    def _inject(self, point: str, connection: sqlite3.Connection) -> None:
        if self.failure_injector is not None:
            self.failure_injector(point, connection)


def fail_event(
    database_path: Path,
    event_id: int,
    code: OrganizationFailureCode,
) -> None:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE organization_events
            SET status = 'failed', completed_at = ?, failure_code = ?
            WHERE event_id = ? AND status = 'running'
            """,
            (utc_now(), code.value, event_id),
        )


def read_event(database_path: Path, event_id: int) -> OrganizationEventRead | None:
    try:
        with connect(database_path) as connection:
            row = connection.execute(
                "SELECT * FROM organization_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            frozen = tuple(
                int(item["knowledge_result_id"])
                for item in connection.execute(
                    """
                    SELECT knowledge_result_id
                    FROM organization_event_source_boundary
                    WHERE event_id = ? AND boundary_role = 'frozen_new'
                    ORDER BY position
                    """,
                    (event_id,),
                ).fetchall()
            )
        status = EventStatus(str(row["status"]))
        success = None
        failure = None
        if status is EventStatus.SUCCEEDED:
            if row["success_payload_json"] is None:
                raise ValueError("Succeeded event has no success payload")
            success = decode_success_payload(str(row["success_payload_json"]))
            if success.n != len(frozen):
                raise ValueError("Succeeded event coverage summary is inconsistent")
        elif status is EventStatus.FAILED:
            failure = OrganizationFailureCode(str(row["failure_code"]))
        return OrganizationEventRead(
            event_id=int(row["event_id"]),
            status=status,
            started_at=str(row["started_at"]),
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
            failure_code=failure,
            frozen_new_ids=frozen,
            success=success,
        )
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise OrganizationReadError("整理事件暂时无法安全读取") from error


def list_pending_candidates(database_path: Path) -> PendingCandidateList:
    try:
        with connect(database_path) as connection:
            rows = connection.execute(
                """
                SELECT iv.*, e.status
                FROM insight_versions AS iv
                JOIN organization_events AS e
                  ON e.event_id = iv.produced_event_id
                LEFT JOIN user_insight_judgments AS j
                  ON j.insight_version_id = iv.insight_version_id
                WHERE e.status = 'succeeded' AND j.judgment_id IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM insight_identity_replacements AS replacement
                    WHERE replacement.replaced_insight_id = iv.insight_id
                  )
                ORDER BY iv.insight_version_id DESC
                """
            ).fetchall()
    except sqlite3.Error as error:
        raise OrganizationReadError("新知候选暂时无法安全读取") from error
    candidates = []
    unreadable = 0
    for row in rows:
        try:
            candidates.append(
                PendingCandidateRead(
                    insight_version_id=int(row["insight_version_id"]),
                    insight_id=int(row["insight_id"]),
                    version_no=int(row["version_no"]),
                    event_id=int(row["produced_event_id"]),
                    payload=decode_insight_payload(str(row["payload_json"])),
                )
            )
        except (TypeError, ValueError):
            unreadable += 1
    return PendingCandidateList(tuple(candidates), unreadable)


def _load_identity_catalog(
    connection: sqlite3.Connection,
) -> GrowthIdentityCatalog:
    relation_entries = []
    for row in connection.execute(
        """
        SELECT v.*,
               v.version_no = (
                 SELECT MAX(latest.version_no)
                 FROM relation_versions AS latest
                 WHERE latest.relation_id = v.relation_id
               ) AS is_latest,
               EXISTS (
                 SELECT 1 FROM relation_current AS c
                 WHERE c.relation_id = v.relation_id
                   AND c.relation_version_id = v.relation_version_id
               ) AS is_current
        FROM relation_versions AS v
        JOIN organization_events AS e ON e.event_id = v.produced_event_id
        WHERE e.status = 'succeeded'
        ORDER BY v.relation_id, v.version_no
        """
    ).fetchall():
        payload = decode_relation_payload(str(row["payload_json"]))
        stored_signature = str(row["semantic_signature"])
        if semantic_signature(payload) != stored_signature:
            raise ValueError("Relation catalog semantic signature is invalid")
        evolution_basis = _load_catalog_evolution_basis(
            connection,
            "relation",
            int(row["relation_version_id"]),
            payload.required_premises,
        )
        facts = tuple(
            str(fact["fact_kind"])
            for fact in connection.execute(
                """
                SELECT fact_kind FROM relation_facts
                WHERE relation_version_id = ?
                ORDER BY relation_fact_id
                """,
                (int(row["relation_version_id"]),),
            ).fetchall()
        )
        relation_entries.append(
            RelationIdentityCatalogEntry(
                int(row["relation_id"]),
                int(row["relation_version_id"]),
                int(row["version_no"]),
                payload,
                stored_signature,
                evolution_basis,
                bool(row["is_latest"]),
                bool(row["is_current"]),
                facts,
            )
        )

    insight_entries = []
    for row in connection.execute(
        """
        SELECT v.*,
               v.version_no = (
                 SELECT MAX(latest.version_no)
                 FROM insight_versions AS latest
                 WHERE latest.insight_id = v.insight_id
               ) AS is_latest,
               j.decision, a.current_role,
               replacement.replacement_insight_id
        FROM insight_versions AS v
        JOIN organization_events AS e ON e.event_id = v.produced_event_id
        LEFT JOIN user_insight_judgments AS j
          ON j.insight_version_id = v.insight_version_id
        LEFT JOIN accepted_insight_versions AS a
          ON a.insight_version_id = v.insight_version_id
         AND a.judgment_id = j.judgment_id
        LEFT JOIN insight_identity_replacements AS replacement
          ON replacement.replaced_insight_id = v.insight_id
        WHERE e.status = 'succeeded'
        ORDER BY v.insight_id, v.version_no
        """
    ).fetchall():
        payload = decode_insight_payload(str(row["payload_json"]))
        stored_signature = str(row["semantic_signature"])
        if semantic_signature(payload) != stored_signature:
            raise ValueError("Insight catalog semantic signature is invalid")
        evolution_basis = _load_catalog_evolution_basis(
            connection,
            "insight",
            int(row["insight_version_id"]),
            payload.required_premises,
        )
        decision = row["decision"]
        current_role = row["current_role"]
        if decision is None:
            if current_role is not None:
                raise ValueError("Pending insight has accepted state")
            state = InsightCatalogState.PENDING
        elif decision == "rethink":
            if current_role is None:
                state = InsightCatalogState.RETHINK
            else:
                from .accepted_insight_library import read_accepted_row
                accepted = read_accepted_row(connection, int(row['insight_version_id']))
                if accepted is None:
                    raise ValueError("Reconsidered insight has no valid grant")
                state = (InsightCatalogState.ACCEPTED_CURRENT if current_role == 'current'
                         else InsightCatalogState.ACCEPTED_HISTORICAL)
        elif decision == "interesting" and current_role == "current":
            state = InsightCatalogState.ACCEPTED_CURRENT
        elif decision == "interesting" and current_role == "historical":
            state = InsightCatalogState.ACCEPTED_HISTORICAL
        else:
            raise ValueError("Interesting insight is missing accepted state")
        permanent_facts = tuple(
            str(fact["fact_kind"])
            for fact in connection.execute(
                """
                SELECT fact_kind FROM insight_version_disqualifications
                WHERE insight_version_id = ?
                ORDER BY disqualification_id
                """,
                (int(row["insight_version_id"]),),
            ).fetchall()
        )
        insight_entries.append(
            InsightIdentityCatalogEntry(
                int(row["insight_id"]),
                int(row["insight_version_id"]),
                int(row["version_no"]),
                payload,
                stored_signature,
                evolution_basis,
                bool(row["is_latest"]),
                state,
                permanent_facts,
                (
                    int(row["replacement_insight_id"])
                    if row["replacement_insight_id"] is not None
                    else None
                ),
            )
        )

    relation_identity_by_signature: dict[str, int] = {}
    for item in relation_entries:
        existing = relation_identity_by_signature.setdefault(
            item.semantic_signature, item.relation_id
        )
        if existing != item.relation_id:
            raise ValueError(
                "Relation catalog contains an exact semantic duplicate across identities"
            )
    insight_identity_by_signature: dict[str, int] = {}
    for item in insight_entries:
        existing = insight_identity_by_signature.setdefault(
            item.semantic_signature, item.insight_id
        )
        if existing != item.insight_id:
            raise ValueError(
                "Insight catalog contains an exact semantic duplicate across identities"
            )
    relation_basis_versions = [
        (
            item.relation_id,
            item.semantic_signature,
            item.evolution_basis.fingerprint,
        )
        for item in relation_entries
    ]
    if len(relation_basis_versions) != len(set(relation_basis_versions)):
        raise ValueError(
            "Relation catalog repeats an exact semantic and formation basis"
        )
    insight_basis_versions = [
        (
            item.insight_id,
            item.semantic_signature,
            item.evolution_basis.fingerprint,
        )
        for item in insight_entries
    ]
    if len(insight_basis_versions) != len(set(insight_basis_versions)):
        raise ValueError(
            "Insight catalog repeats an exact semantic and formation basis"
        )
    unsigned = GrowthIdentityCatalog(
        tuple(relation_entries), tuple(insight_entries), ""
    )
    signature_payload = identity_catalog_to_dict(unsigned)
    signature_payload.pop("signature")
    return GrowthIdentityCatalog(
        unsigned.relation_versions,
        unsigned.insight_versions,
        sha256_json(signature_payload),
    )


def _load_catalog_evolution_basis(
    connection: sqlite3.Connection,
    owner_kind: str,
    owner_version_id: int,
    premises,
):
    if owner_kind == "relation":
        rows = connection.execute(
            """
            SELECT participant_key, input_kind, knowledge_result_id, point_id,
                   accepted_insight_version_id, position, contribution_text
            FROM relation_version_participants
            WHERE relation_version_id = ? ORDER BY position
            """,
            (owner_version_id,),
        ).fetchall()
    elif owner_kind == "insight":
        rows = connection.execute(
            """
            SELECT participant_key, input_kind, knowledge_result_id, point_id,
                   accepted_insight_version_id, position, contribution_text
            FROM insight_version_participants
            WHERE insight_version_id = ? ORDER BY position
            """,
            (owner_version_id,),
        ).fetchall()
    else:
        raise ValueError("Unknown evolution basis owner")
    participants = tuple(
        _participant_from_row(row, position) for position, row in enumerate(rows)
    )
    if len(participants) < 2:
        raise ValueError("Evolution basis has too few persisted participants")
    used_relations = _load_used_relation_inputs(
        connection, owner_kind, owner_version_id
    )
    return build_evolution_basis_card(
        participants,
        premises,
        tuple(
            f"relation_version:{item.relation_version_id}"
            for item in used_relations
        ),
    )


def _load_all_eligible_sources(
    connection: sqlite3.Connection,
) -> tuple[SourceKnowledgeInput, ...]:
    return _load_current_source_inputs_shared(connection)


def _load_current_accepted_inputs(
    connection: sqlite3.Connection,
    sources: tuple[SourceKnowledgeInput, ...],
) -> tuple[AcceptedInsightInput, ...]:
    return _load_current_accepted_inputs_shared(connection, sources)


def _participant_from_row(row, expected_position: int) -> Participant:
    if int(row["position"]) != expected_position:
        raise ValueError("Version participant positions are not dense")
    kind = InputKind(str(row["input_kind"]))
    return Participant(
        participant_key=str(row["participant_key"]),
        input_kind=kind,
        position=expected_position,
        contribution_text=str(row["contribution_text"]),
        knowledge_result_id=(
            int(row["knowledge_result_id"])
            if row["knowledge_result_id"] is not None
            else None
        ),
        point_id=(str(row["point_id"]) if row["point_id"] is not None else None),
        accepted_insight_version_id=(
            int(row["accepted_insight_version_id"])
            if row["accepted_insight_version_id"] is not None
            else None
        ),
    )


def _load_used_relation_inputs(
    connection: sqlite3.Connection,
    owner_kind: str,
    owner_version_id: int,
) -> tuple[UsedRelationInput, ...]:
    if owner_kind == "insight":
        table = "insight_version_used_relations"
        owner_column = "insight_version_id"
        used_column = "relation_version_id"
    elif owner_kind == "relation":
        table = "relation_version_used_relations"
        owner_column = "relation_version_id"
        used_column = "used_relation_version_id"
    else:
        raise ValueError("Unknown used relation owner")
    rows = connection.execute(
        f"""
        SELECT edge.{used_column} AS relation_version_id,
               edge.position, edge.role_text, relation.relation_id,
               relation.version_no, relation.produced_event_id,
               relation.payload_json, relation.semantic_signature
        FROM {table} AS edge
        JOIN relation_versions AS relation
          ON relation.relation_version_id = edge.{used_column}
        JOIN organization_events AS event
          ON event.event_id = relation.produced_event_id
        WHERE edge.{owner_column} = ? AND event.status = 'succeeded'
        ORDER BY edge.position
        """,
        (owner_version_id,),
    ).fetchall()
    expected_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {owner_column} = ?",
            (owner_version_id,),
        ).fetchone()[0]
    )
    if len(rows) != expected_count:
        raise ValueError("Used relation graph is broken")
    result = []
    for position, row in enumerate(rows):
        if int(row["position"]) != position:
            raise ValueError("Used relation positions are not dense")
        payload = decode_relation_payload(str(row["payload_json"]))
        if semantic_signature(payload) != str(row["semantic_signature"]):
            raise ValueError("Used relation semantic signature is invalid")
        result.append(
            UsedRelationInput(
                relation_version_id=int(row["relation_version_id"]),
                relation_id=int(row["relation_id"]),
                version_no=int(row["version_no"]),
                produced_event_id=int(row["produced_event_id"]),
                position=position,
                role_text=str(row["role_text"]),
                payload_json=canonical_json(json.loads(str(row["payload_json"]))),
            )
        )
    return tuple(result)


def _accepted_dependency_closure(
    accepted_inputs: tuple[AcceptedInsightInput, ...],
    insight_version_ids: tuple[int, ...],
) -> tuple[tuple[int, tuple[SourcePointIdentity, ...]], ...]:
    roots = {item.insight_version_id: item for item in accepted_inputs}
    nodes = {}
    for insight_version_id in insight_version_ids:
        root = roots.get(insight_version_id)
        if root is None:
            raise ValueError("Required accepted input is missing from strict boundary")
        for node in root.lineage_nodes:
            nodes[node.insight_version_id] = node.source_leaves
    return tuple(
        (insight_version_id, nodes[insight_version_id])
        for insight_version_id in sorted(nodes)
    )


def _load_relation_inputs(
    connection: sqlite3.Connection,
    sources: tuple[SourceKnowledgeInput, ...],
    accepted: tuple[AcceptedInsightInput, ...],
    *,
    strict: bool = True,
) -> tuple[tuple[RelationBoundaryInput, ...], tuple[RelationBoundaryInput, ...]]:
    current_rows = connection.execute(
        """
        SELECT v.*, 'current_input' AS boundary_role
        FROM relation_current AS c
        JOIN relation_versions AS v
          ON v.relation_id = c.relation_id
         AND v.relation_version_id = c.relation_version_id
        JOIN organization_events AS e ON e.event_id = v.produced_event_id
        WHERE e.status = 'succeeded'
        ORDER BY v.relation_id
        """
    ).fetchall()
    hint_rows = connection.execute(
        """
        SELECT v.*, 'reconsideration_hint' AS boundary_role
        FROM relation_versions AS v
        JOIN organization_events AS e ON e.event_id = v.produced_event_id
        WHERE e.status = 'succeeded'
          AND v.version_no = (
            SELECT MAX(latest.version_no) FROM relation_versions AS latest
            WHERE latest.relation_id = v.relation_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM relation_current AS c WHERE c.relation_id = v.relation_id
          )
          AND (
            SELECT f.fact_kind FROM relation_facts AS f
            WHERE f.relation_id = v.relation_id
            ORDER BY f.relation_fact_id DESC LIMIT 1
          ) = 'attention_retired'
          AND NOT EXISTS (
            SELECT 1 FROM relation_facts AS invalid
            WHERE invalid.relation_id = v.relation_id
              AND invalid.fact_kind IN ('basis_invalid', 'wrong', 'replaced')
          )
        ORDER BY v.relation_id
        """
    ).fetchall()
    allowed_points = {
        point.identity for item in sources for point in item.points
    }
    accepted_ids = {item.insight_version_id for item in accepted}

    def load_rows(rows):
        result = []
        for row in rows:
            try:
                result.append(
                    _relation_input(
                        connection, row, allowed_points, accepted_ids
                    )
                )
            except ValueError:
                if strict:
                    raise
        return tuple(result)

    return load_rows(current_rows), load_rows(hint_rows)


def _relation_input(connection, row, allowed_points, accepted_ids):
    payload = decode_relation_payload(str(row["payload_json"]))
    if semantic_signature(payload) != str(row["semantic_signature"]):
        raise ValueError("Stable relation semantic signature is invalid")
    participant_rows = connection.execute(
        """
        SELECT participant_key, input_kind, knowledge_result_id, point_id,
               accepted_insight_version_id, position, contribution_text
        FROM relation_version_participants
        WHERE relation_version_id = ? ORDER BY position
        """,
        (int(row["relation_version_id"]),),
    ).fetchall()
    if len(participant_rows) < 2:
        raise ValueError("Stable relation has too few participants")
    participants = tuple(
        _participant_from_row(value, position)
        for position, value in enumerate(participant_rows)
    )
    for participant in participants:
        if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
            if SourcePointIdentity(
                participant.knowledge_result_id or 0,
                participant.point_id or "",
            ) not in allowed_points:
                raise ValueError("Stable relation source is no longer eligible")
        elif participant.accepted_insight_version_id not in accepted_ids:
            raise ValueError("Stable relation accepted input is no longer current")
    used_relations = _load_used_relation_inputs(
        connection, "relation", int(row["relation_version_id"])
    )
    signature = sha256_json(
        {
            "codec": "relation-qualification-v2",
            "relation_version_id": int(row["relation_version_id"]),
            "boundary_role": str(row["boundary_role"]),
            "semantic_signature": str(row["semantic_signature"]),
            "dependency_signature": str(row["dependency_signature"]),
            "participants": [
                _participant_identity_payload(value) for value in participants
            ],
            "used_relations": [
                _used_relation_payload(value) for value in used_relations
            ],
        }
    )
    return RelationBoundaryInput(
        int(row["relation_id"]),
        int(row["relation_version_id"]),
        int(row["version_no"]),
        str(row["boundary_role"]),
        canonical_json(json.loads(str(row["payload_json"]))),
        str(row["semantic_signature"]),
        str(row["dependency_signature"]),
        signature,
        participants,
        used_relations,
    )


def _load_event_boundary(connection, event_id: int, source_loader=_load_all_eligible_sources) -> GrowthBoundary:
    source_rows = connection.execute(
        """
        SELECT * FROM organization_event_source_boundary
        WHERE event_id = ?
        ORDER BY CASE boundary_role WHEN 'frozen_new' THEN 0 ELSE 1 END, position
        """,
        (event_id,),
    ).fetchall()
    if not source_rows:
        raise ValueError("Organization event has no source boundary")
    eligible_sources = {
        item.knowledge_result_id: item for item in source_loader(connection)
    }
    frozen = []
    history = []
    for row in source_rows:
        source = eligible_sources.get(int(row["knowledge_result_id"]))
        if source is None or source.source_fact_id != int(row["source_fact_id"]):
            if row["boundary_role"] == "frozen_new":
                raise _FrozenInputIneligible
            continue
        updated = SourceKnowledgeInput(
            source.knowledge_result_id,
            source.source_fact_id,
            source.title,
            source.summary,
            source.points,
            str(row["boundary_role"]),
            source.qualification_signature,
        )
        (frozen if row["boundary_role"] == "frozen_new" else history).append(updated)
    if not frozen:
        raise _FrozenInputIneligible
    all_sources = tuple(frozen + history)
    accepted_all = _load_current_accepted_inputs(connection, all_sources)
    accepted_map = {item.insight_version_id: item for item in accepted_all}
    accepted = tuple(
        accepted_map[int(row["insight_version_id"])]
        for row in connection.execute(
            """
            SELECT insight_version_id FROM organization_event_accepted_boundary
            WHERE event_id = ? ORDER BY position
            """,
            (event_id,),
        ).fetchall()
        if int(row["insight_version_id"]) in accepted_map
    )
    current_all, hints_all = _load_relation_inputs(
        connection, all_sources, accepted, strict=False
    )
    current_map = {item.relation_version_id: item for item in current_all}
    hint_map = {item.relation_version_id: item for item in hints_all}
    current = []
    hints = []
    for row in connection.execute(
        """
        SELECT relation_version_id, boundary_role
        FROM organization_event_relation_boundary
        WHERE event_id = ? ORDER BY position
        """,
        (event_id,),
    ).fetchall():
        version_id = int(row["relation_version_id"])
        if row["boundary_role"] == "current_input" and version_id in current_map:
            current.append(current_map[version_id])
        elif row["boundary_role"] == "reconsideration_hint" and version_id in hint_map:
            hints.append(hint_map[version_id])
    return GrowthBoundary(
        event_id,
        tuple(frozen),
        tuple(history),
        accepted,
        tuple(current),
        tuple(hints),
    )


def _qualification_context(
    boundary,
    identity_catalog,
    *,
    selected_source_knowledge_ids,
    selected_accepted_insight_version_ids,
    selected_current_relation_version_ids,
    selected_reconsideration_hint_version_ids,
):
    latest_relation_entries = {
        item.relation_id: item
        for item in identity_catalog.relation_versions
        if item.is_latest
    }
    latest_insight_entries = {
        item.insight_id: item
        for item in identity_catalog.insight_versions
        if item.is_latest
    }
    return QualificationContext(
        boundary=boundary,
        selected_source_knowledge_ids=selected_source_knowledge_ids,
        selected_accepted_insight_version_ids=(
            selected_accepted_insight_version_ids
        ),
        selected_current_relation_version_ids=(
            selected_current_relation_version_ids
        ),
        selected_reconsideration_hint_version_ids=(
            selected_reconsideration_hint_version_ids
        ),
        known_relation_semantic_signatures={
            identity: item.semantic_signature
            for identity, item in latest_relation_entries.items()
        },
        known_insight_semantic_signatures={
            identity: item.semantic_signature
            for identity, item in latest_insight_entries.items()
        },
        rethink_insight_semantic_signatures={
            item.insight_id: item.semantic_signature
            for item in identity_catalog.insight_versions
            if item.is_latest and item.state is InsightCatalogState.RETHINK
        },
        latest_relation_versions={
            identity: item.relation_version_id
            for identity, item in latest_relation_entries.items()
        },
        latest_insight_versions={
            identity: item.insight_version_id
            for identity, item in latest_insight_entries.items()
        },
        latest_relation_evolution_bases={
            identity: item.evolution_basis
            for identity, item in latest_relation_entries.items()
        },
        latest_insight_evolution_bases={
            identity: item.evolution_basis
            for identity, item in latest_insight_entries.items()
        },
        relation_evolution_history_by_semantic_signature={
            signature: tuple(
                (item.relation_id, item.evolution_basis.fingerprint)
                for item in identity_catalog.relation_versions
                if item.semantic_signature == signature
            )
            for signature in {
                item.semantic_signature
                for item in identity_catalog.relation_versions
            }
        },
        insight_evolution_history_by_semantic_signature={
            signature: tuple(
                (item.insight_id, item.evolution_basis.fingerprint)
                for item in identity_catalog.insight_versions
                if item.semantic_signature == signature
            )
            for signature in {
                item.semantic_signature
                for item in identity_catalog.insight_versions
            }
        },
        relation_identity_by_semantic_signature={
            item.semantic_signature: item.relation_id
            for item in identity_catalog.relation_versions
        },
        insight_identity_by_semantic_signature={
            item.semantic_signature: item.insight_id
            for item in identity_catalog.insight_versions
        },
        replaced_insight_identities={
            item.insight_id: item.replaced_by_insight_id
            for item in identity_catalog.insight_versions
            if item.replaced_by_insight_id is not None
        },
        catalog_basis_invalid_relation_versions={
            item.relation_id: item.relation_version_id
            for item in latest_relation_entries.values()
            if not item.is_current
            and "basis_invalid" in item.permanent_facts
            and not {"wrong", "replaced"} & set(item.permanent_facts)
        },
        identity_catalog_signature=identity_catalog.signature,
    )


def _topic_points(boundary: GrowthBoundary) -> tuple[TopicPointInput, ...]:
    return tuple(
        TopicPointInput(
            source.knowledge_result_id,
            source.source_fact_id,
            point.point_id,
            point.role,
            point.statement,
            point.argument,
            source.title,
            source.summary,
        )
        for source in (*boundary.frozen_new, *boundary.eligible_history)
        for point in source.points
    )


def _read_topic_before(connection, event_id):
    row = connection.execute(
        "SELECT topic_before_json FROM organization_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Organization event {event_id} does not exist")
    value = json.loads(str(row["topic_before_json"]))
    if not isinstance(value, Mapping) or value.get("codec") != "topic-safety-snapshot-v1":
        raise ValueError("Organization event Topic before is invalid")
    return dict(value)


def _existing_topics_from_before(value):
    topics = value.get("topics")
    if not isinstance(topics, list):
        raise ValueError("Topic before topics are invalid")
    result = []
    for topic in topics:
        if not isinstance(topic, Mapping):
            raise ValueError("Topic before topic is invalid")
        members = topic.get("members")
        if not isinstance(members, list):
            raise ValueError("Topic before members are invalid")
        result.append(
            ExistingTopicInput(
                int(topic["topic_id"]),
                str(topic["name"]),
                str(topic["scope"]),
                tuple(
                    TopicPointReference(
                        int(member["knowledge_result_id"]), str(member["point_id"])
                    )
                    for member in members
                ),
            )
        )
    return tuple(result)


def _expanded_inputs(boundary: GrowthBoundary, recall: RecallSelection):
    selected_source_knowledge_ids = set(recall.source_knowledge_ids)
    selected_accepted_insight_version_ids = set(
        recall.accepted_insight_version_ids
    )
    selected_current_relation_version_ids = set(
        recall.current_relation_version_ids
    )
    selected_reconsideration_hint_version_ids = set(
        recall.reconsideration_hint_version_ids
    )
    source_by_id = {
        item.knowledge_result_id: item
        for item in (*boundary.frozen_new, *boundary.eligible_history)
    }
    accepted_by_version = {
        item.insight_version_id: item for item in boundary.accepted_current
    }
    return {
        "frozen_new": [_source_payload(item) for item in boundary.frozen_new],
        "historical_sources": [
            _source_payload(item)
            for item in boundary.eligible_history
            if item.knowledge_result_id in selected_source_knowledge_ids
        ],
        "accepted_current": [
            _accepted_expanded_payload(item, source_by_id)
            for item in boundary.accepted_current
            if item.insight_version_id
            in selected_accepted_insight_version_ids
        ],
        "selected_current_relations": [
            _relation_expanded_payload(
                item,
                source_by_id,
                accepted_by_version,
                authority="review_and_used_only_after_qualification",
            )
            for item in boundary.current_relations
            if item.relation_version_id
            in selected_current_relation_version_ids
        ],
        "selected_reconsideration_hints": [
            _relation_expanded_payload(
                item,
                source_by_id,
                accepted_by_version,
                authority="requalification_only_until_review_and_event_activation",
            )
            for item in boundary.reconsideration_hints
            if item.relation_version_id
            in selected_reconsideration_hint_version_ids
        ],
    }


def _source_payload(item: SourceKnowledgeInput):
    return {
        "knowledge_result_id": item.knowledge_result_id,
        "source_fact_id": item.source_fact_id,
        "title": item.title,
        "summary": item.summary,
        "points": [
            {
                "point_id": point.point_id,
                "role": point.role,
                "statement": point.statement,
                "argument": point.argument,
            }
            for point in item.points
        ],
    }


def _participant_identity_payload(item: Participant):
    value = {
        "participant_key": item.participant_key,
        "input_kind": item.input_kind.value,
        "position": item.position,
        "contribution_text": item.contribution_text,
    }
    if item.input_kind is InputKind.SOURCE_KNOWLEDGE:
        value.update(
            knowledge_result_id=item.knowledge_result_id,
            point_id=item.point_id,
        )
    else:
        value["accepted_insight_version_id"] = item.accepted_insight_version_id
    return value


def _used_relation_payload(item: UsedRelationInput):
    return {
        "relation_version_id": item.relation_version_id,
        "relation_id": item.relation_id,
        "version_no": item.version_no,
        "produced_event_id": item.produced_event_id,
        "position": item.position,
        "role_text": item.role_text,
        "payload": json.loads(item.payload_json),
        "authority": "historical_basis_edge_only",
    }


def _source_point_payload(
    source: SourceKnowledgeInput,
    point_id: str,
):
    point = next((value for value in source.points if value.point_id == point_id), None)
    if point is None:
        raise ValueError("Participant source point is missing")
    return {
        "knowledge_result_id": source.knowledge_result_id,
        "source_fact_id": source.source_fact_id,
        "title": source.title,
        "summary": source.summary,
        "point": {
            "point_id": point.point_id,
            "role": point.role,
            "statement": point.statement,
            "argument": point.argument,
        },
    }


def _expanded_participant_payload(
    participant: Participant,
    source_by_id: Mapping[int, SourceKnowledgeInput],
):
    value = _participant_identity_payload(participant)
    if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
        source = source_by_id.get(participant.knowledge_result_id or 0)
        if source is None:
            raise ValueError("Participant source is outside the frozen boundary")
        value["resolved_source"] = _source_point_payload(
            source, participant.point_id or ""
        )
    else:
        value["resolved_accepted"] = {
            "insight_version_id": participant.accepted_insight_version_id,
            "resolution": "recursive_lineage_node",
        }
    return value


def _accepted_expanded_payload(
    item: AcceptedInsightInput,
    source_by_id: Mapping[int, SourceKnowledgeInput],
):
    return {
        "insight_version_id": item.insight_version_id,
        "insight_id": item.insight_id,
        "version_no": item.version_no,
        "produced_event_id": item.produced_event_id,
        "identity_kind": item.identity_kind,
        "current_role": item.current_role,
        "disqualification_facts": list(item.disqualification_facts),
        "replacement_insight_id": item.replacement_insight_id,
        "payload": insight_payload_to_dict(item.payload),
        "recursive_lineage": [
            {
                "insight_version_id": node.insight_version_id,
                "insight_id": node.insight_id,
                "version_no": node.version_no,
                "produced_event_id": node.produced_event_id,
                "payload": insight_payload_to_dict(node.payload),
                "participants": [
                    _expanded_participant_payload(participant, source_by_id)
                    for participant in node.participants
                ],
                "used_relations": [
                    _used_relation_payload(value) for value in node.used_relations
                ],
                "source_leaves": [
                    {
                        "knowledge_result_id": leaf.knowledge_result_id,
                        "point_id": leaf.point_id,
                    }
                    for leaf in node.source_leaves
                ],
            }
            for node in item.lineage_nodes
        ],
        "source_leaves": [
            {
                "knowledge_result_id": leaf.knowledge_result_id,
                "point_id": leaf.point_id,
            }
            for leaf in item.source_leaves
        ],
        "authority": "accepted_current_formal_input",
    }


def _relation_expanded_payload(
    item: RelationBoundaryInput,
    source_by_id: Mapping[int, SourceKnowledgeInput],
    accepted_by_version: Mapping[int, AcceptedInsightInput],
    *,
    authority: str,
):
    participants = []
    for participant in item.participants:
        value = _expanded_participant_payload(participant, source_by_id)
        if participant.input_kind is InputKind.ACCEPTED_INSIGHT:
            accepted = accepted_by_version.get(
                participant.accepted_insight_version_id or 0
            )
            if accepted is None:
                raise ValueError("Relation accepted participant is not current")
            value["resolved_accepted"] = _accepted_expanded_payload(
                accepted, source_by_id
            )
        participants.append(value)
    return {
        "relation_id": item.relation_id,
        "relation_version_id": item.relation_version_id,
        "version_no": item.version_no,
        "boundary_role": item.boundary_role,
        "payload": json.loads(item.payload_json),
        "participants": participants,
        "used_relations": [
            _used_relation_payload(value) for value in item.used_relations
        ],
        "authority": authority,
    }


def _boundary_payload(frozen, history, accepted, current, hints):
    return {
        "codec": "organization-boundary-v1",
        "frozen_new": [
            [item.knowledge_result_id, item.source_fact_id, item.qualification_signature]
            for item in frozen
        ],
        "eligible_history": [
            [item.knowledge_result_id, item.source_fact_id, item.qualification_signature]
            for item in history
        ],
        "accepted": [
            [item.insight_version_id, item.qualification_signature] for item in accepted
        ],
        "relations": [
            [
                item.relation_version_id,
                item.boundary_role,
                item.qualification_signature,
            ]
            for item in (*current, *hints)
        ],
    }


def _qualify_topic_changes(before, after: TopicPlan, assessments):
    before_topics = {
        int(item["topic_id"]): item
        for item in before.get("topics", [])
        if isinstance(item, Mapping)
    }
    after_existing = {
        item.topic_id: item for item in after.topics if item.topic_id is not None
    }
    label_only_topic_ids = set()
    reasons = []
    for topic_id in sorted(set(before_topics) - set(after_existing)):
        reasons.append(f"主题 {topic_id} 退出当前组织")
    for topic in after.topics:
        if topic.topic_id is None:
            reasons.append(f"新主题 {topic.name} 成立")
            continue
        before_topic = before_topics.get(topic.topic_id)
        if before_topic is None:
            reasons.append(f"主题 {topic.topic_id} 重新进入当前组织")
            continue
        before_members = {
            (int(item["knowledge_result_id"]), str(item["point_id"]))
            for item in before_topic.get("members", [])
        }
        after_members = {
            (item.knowledge_result_id, item.point_id) for item in topic.members
        }
        if before_members != after_members:
            reasons.append(f"主题 {topic.topic_id} 成员发生实质变化")
        elif (
            str(before_topic.get("name")) != topic.name
            or str(before_topic.get("scope")) != topic.scope
        ):
            label_only_topic_ids.add(topic.topic_id)
    assessments_by_topic_id = {}
    seen_refs = set()
    expected_refs = {
        f"topic:{topic_id}" for topic_id in label_only_topic_ids
    }
    for assessment in assessments:
        if assessment.topic_ref in seen_refs:
            raise GrowthQualificationError(
                "duplicate_topic_assessment",
                "a label-only Topic change has duplicate assessments",
            )
        seen_refs.add(assessment.topic_ref)
        if assessment.topic_ref not in expected_refs:
            raise GrowthQualificationError(
                "unknown_topic_assessment",
                "Topic assessment is not an exact label-only diff",
            )
        topic_id = int(assessment.topic_ref.removeprefix("topic:"))
        assessments_by_topic_id[topic_id] = assessment
    if set(assessments_by_topic_id) != label_only_topic_ids:
        raise GrowthQualificationError(
            "missing_topic_assessment",
            "every label-only Topic diff needs one exact assessment",
        )
    for topic in after.topics:
        if topic.topic_id not in label_only_topic_ids:
            continue
        assessment = assessments_by_topic_id[topic.topic_id]
        if assessment.changed:
            reasons.append(assessment.reason_text)
    return tuple(reasons)
