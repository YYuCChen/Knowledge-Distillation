from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from .accepted_insight_renderer import (
    AcceptedEvolutionReference,
    AcceptedInsightRenderContext,
    AcceptedPublicationLink,
    AcceptedRenderEvidence,
    AcceptedRenderExitFact,
    AcceptedRenderFormationEvent,
    AcceptedRenderJudgment,
    AcceptedRenderLineageNode,
    AcceptedRenderSourceLeaf,
    AcceptedRenderUsedRelation,
    accepted_insight_machine_identity,
    accepted_insight_relative_path,
    decode_accepted_placement_receipt,
    validate_accepted_insight_render_context,
)
from .database import connect, query_searchable_formal_knowledge
from .knowledge_derivation import KnowledgeCandidate, knowledge_candidate_from_payload
from .knowledge_library import (
    FormalKnowledgeDocument,
    decode_formal_knowledge_row,
    normalize_search_text,
)
from .organization_models import (
    AcceptedInsightInput,
    AcceptedInsightLineageNode,
    InputKind,
    InsightPayload,
    Limitation,
    Participant,
    SourceKnowledgeInput,
    SourcePointIdentity,
    SourcePointInput,
    UsedRelationInput,
    canonical_json,
    decode_insight_payload,
    decode_relation_payload,
    insight_payload_to_dict,
    semantic_signature,
    sha256_json,
)
from .topic_library import TopicLibraryError, load_topic_planning_input


class AcceptedInsightLibraryError(Exception):
    """The accepted collection could not be read as a trustworthy whole."""


class AcceptedDetailKind(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"


class AcceptedRole(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"


class AcceptedRenderContextKind(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class AcceptedFormationEvent:
    event_id: int
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class AcceptedJudgmentRead:
    judgment_id: int
    decision: str
    meaning: str
    annotation_text: str | None
    decided_at: str


@dataclass(frozen=True)
class AcceptedSourceLeaf:
    knowledge_result_id: int
    source_fact_id: int
    point_id: str
    role: str
    statement: str
    argument: str
    evidence_ids: tuple[str, ...]
    title: str
    source_label: str
    platform: str
    published_path: str


@dataclass(frozen=True)
class AcceptedExitFact:
    fact_kind: str
    event_id: int
    reason_text: str
    created_at: str
    replacement_insight_id: int | None = None


@dataclass(frozen=True)
class AcceptedInsightDetail:
    insight_version_id: int
    insight_id: int
    version_no: int
    identity_kind: str
    payload: InsightPayload
    judgment: AcceptedJudgmentRead
    initial_role: AcceptedRole
    current_role: AcceptedRole
    historical_reason: str | None
    historical_at: str | None
    caused_by_event_id: int | None
    caused_by_judgment_id: int | None
    replacement_insight_id: int | None
    additional_exit_facts: tuple[AcceptedExitFact, ...]
    formation_event: AcceptedFormationEvent
    top_level_participants: tuple[Participant, ...]
    used_relations: tuple[UsedRelationInput, ...]
    recursive_lineage: tuple[AcceptedInsightLineageNode, ...]
    source_leaves: tuple[AcceptedSourceLeaf, ...]
    publication: AcceptedPublicationLink | None


@dataclass(frozen=True)
class AcceptedDetailResult:
    kind: AcceptedDetailKind
    detail: AcceptedInsightDetail | None = None


@dataclass(frozen=True)
class AcceptedRenderContextResult:
    kind: AcceptedRenderContextKind
    context: AcceptedInsightRenderContext | None = None


@dataclass(frozen=True)
class AcceptedInsightSearchCard:
    insight_version_id: int
    insight_id: int
    version_no: int
    identity_kind: str
    role: AcceptedRole
    claim: str
    short_discussion: str
    connection_reasons: tuple[str, ...]
    limitations: tuple[Limitation, ...]
    accepted_at: str
    historical_reason: str | None


@dataclass(frozen=True)
class AcceptedInsightSearchResult:
    current: tuple[AcceptedInsightSearchCard, ...]
    historical: tuple[AcceptedInsightSearchCard, ...]
    unreadable_count: int


@dataclass(frozen=True)
class TopicAuxiliaryInsight:
    topic_id: int
    insight_version_id: int
    insight_id: int
    version_no: int
    claim: str
    matching_source_leaves: tuple[SourcePointIdentity, ...]


@dataclass(frozen=True)
class TopicAuxiliaryResult:
    topic_found: bool
    insights: tuple[TopicAuxiliaryInsight, ...]
    unreadable_count: int


@dataclass(frozen=True)
class _ExactSource:
    document: FormalKnowledgeDocument
    evidence_ids_by_point: Mapping[str, tuple[str, ...]]
    row: sqlite3.Row
    candidate: KnowledgeCandidate


@dataclass(frozen=True)
class AcceptedInsightRow:
    """Validated acceptance facts and effective role, within the caller transaction."""

    row: sqlite3.Row
    effective_role: AcceptedRole
    historical_reason: str | None
    historical_at: str | None
    caused_by_event_id: int | None
    caused_by_judgment_id: int | None
    replacement_insight_id: int | None
    additional_exit_facts: tuple[AcceptedExitFact, ...]


_INTERESTING_MEANING = (
    "This AI-derived insight is worth retaining as a long-term asset; "
    "the judgment does not certify truth or raise its evidence level."
)


def read_accepted_detail(
    database_path: Path,
    insight_version_id: int,
) -> AcceptedDetailResult:
    try:
        with connect(database_path) as connection:
            try:
                row = read_accepted_row(connection, insight_version_id)
                if row is None:
                    return AcceptedDetailResult(AcceptedDetailKind.NOT_FOUND)
                detail = _decode_detail(connection, row)
            except (json.JSONDecodeError, TypeError, ValueError):
                return AcceptedDetailResult(AcceptedDetailKind.UNREADABLE)
    except sqlite3.Error as error:
        raise AcceptedInsightLibraryError(
            "Accepted insight detail could not be read safely"
        ) from error
    return AcceptedDetailResult(AcceptedDetailKind.FOUND, detail)


def read_accepted_render_context(
    database_path: Path,
    insight_version_id: int,
) -> AcceptedRenderContextResult:
    """Read one exact accepted publication snapshot without touching the Vault."""
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN")
            try:
                context = load_accepted_render_context(
                    connection,
                    insight_version_id,
                )
                if context is None:
                    return AcceptedRenderContextResult(
                        AcceptedRenderContextKind.NOT_FOUND
                    )
            except (json.JSONDecodeError, TypeError, ValueError):
                return AcceptedRenderContextResult(
                    AcceptedRenderContextKind.UNREADABLE
                )
    except sqlite3.Error as error:
        raise AcceptedInsightLibraryError(
            "Accepted insight render context could not be read safely"
        ) from error
    return AcceptedRenderContextResult(AcceptedRenderContextKind.FOUND, context)


def load_accepted_render_context(
    connection: sqlite3.Connection,
    insight_version_id: int,
) -> AcceptedInsightRenderContext | None:
    """Strictly load within the caller's existing read or write transaction."""
    accepted = read_accepted_row(connection, insight_version_id)
    if accepted is None:
        return None
    context = _decode_render_context(connection, accepted)
    validate_accepted_insight_render_context(context)
    return context


def validate_accepted_receipt_snapshot_anchors(
    connection: sqlite3.Connection,
    context: AcceptedInsightRenderContext,
    *,
    placed_at: str,
) -> None:
    """Prove a receipt snapshot against immutable formal memory.

    Current roles, later exit facts, and later publication navigation may have
    advanced since placement.  Those changes may extend the snapshot, but they
    may not rewrite any anchor that the receipt actually contains.
    """
    validate_accepted_insight_render_context(context)
    placed_time = _snapshot_time(placed_at, "Accepted placement time")

    for node in context.lineage_nodes:
        accepted = read_accepted_row(connection, node.insight_version_id)
        if accepted is None:
            raise ValueError("Accepted receipt version anchor is missing")
        row = accepted.row
        payload = decode_insight_payload(str(row["payload_json"]))
        if (
            int(row["insight_id"]) != node.insight_id
            or int(row["version_no"]) != node.version_no
            or str(row["semantic_signature"]) != node.semantic_signature
            or str(row["dependency_signature"]) != node.dependency_signature
            or str(row["insight_created_at"]) != node.created_at
            or payload != node.payload
            or int(row["produced_event_id"]) != node.formation_event.event_id
            or str(row["started_at"]) != node.formation_event.started_at
            or str(row["completed_at"]) != node.formation_event.completed_at
            or str(row["decision"]) != node.judgment.decision
            or int(row["judgment_id"]) != node.judgment.judgment_id
            or (
                str(row["annotation_text"])
                if row["annotation_text"] is not None
                else None
            )
            != node.judgment.annotation_text
            or str(row["decided_at"]) != node.judgment.decided_at
        ):
            raise ValueError("Accepted receipt version anchor changed")
        identity = connection.execute(
            """
            SELECT created_event_id FROM insight_identities
            WHERE insight_id = ?
            """,
            (node.insight_id,),
        ).fetchone()
        if identity is None or (
            node.version_no == 1
            and int(identity["created_event_id"]) != node.formation_event.event_id
        ):
            raise ValueError("Accepted receipt identity anchor is invalid")
        _validate_snapshot_predecessor(
            connection,
            node,
            row,
            placed_time,
        )
        _validate_snapshot_role(connection, node, accepted, placed_time)
        _validate_snapshot_participants(connection, node)
        _validate_snapshot_used_relations(connection, node)
        _validate_snapshot_publication(
            connection,
            node.insight_version_id,
            node.existing_publication,
            placed_time,
        )

    for leaf in context.source_leaves:
        exact = _load_exact_source(connection, leaf.knowledge_result_id)
        if exact is None:
            raise ValueError("Accepted receipt source anchor is missing")
        current = _render_source_leaf(
            {leaf.knowledge_result_id: exact},
            SourcePointIdentity(leaf.knowledge_result_id, leaf.point_id),
        )
        if replace(
            current,
            invalidated_at=leaf.invalidated_at,
            invalidation_reason=leaf.invalidation_reason,
        ) != leaf:
            raise ValueError("Accepted receipt source anchor changed")
        _validate_snapshot_invalidation(leaf, current, placed_time)

    for reference in context.evolution_references:
        _validate_snapshot_reference(
            connection,
            reference,
            placed_time,
            replacement_target_insight_id=context.insight_id,
        )


def _validate_snapshot_predecessor(
    connection: sqlite3.Connection,
    node: AcceptedRenderLineageNode,
    row: sqlite3.Row,
    placed_time: datetime,
) -> None:
    stored = (
        int(row["previous_version_id"])
        if row["previous_version_id"] is not None
        else None
    )
    if node.previous_version_id is not None:
        if node.previous_version_id != stored:
            raise ValueError("Accepted receipt predecessor anchor changed")
        return
    if stored is None:
        return
    later_accepted = connection.execute(
        """
        SELECT accepted_at FROM accepted_insight_versions
        WHERE insight_version_id = ?
        """,
        (stored,),
    ).fetchone()
    if later_accepted is not None and _snapshot_time(
        str(later_accepted["accepted_at"]),
        "Accepted predecessor time",
    ) < placed_time:
        raise ValueError("Accepted receipt omitted an existing predecessor")


def _validate_snapshot_role(
    connection: sqlite3.Connection,
    node: AcceptedRenderLineageNode,
    accepted: AcceptedInsightRow,
    placed_time: datetime,
) -> None:
    row = accepted.row
    if str(row["initial_role"]) != node.initial_role:
        raise ValueError("Accepted receipt initial role changed")
    if node.current_role == "current":
        if accepted.effective_role is AcceptedRole.CURRENT:
            return
        if node.initial_role != "current" or accepted.historical_at is None:
            raise ValueError("Accepted receipt current role is impossible")
        if placed_time > _snapshot_time(
            accepted.historical_at,
            "Accepted historical time",
        ):
            raise ValueError("Accepted receipt uses a stale current role")
        return

    if accepted.effective_role is not AcceptedRole.HISTORICAL:
        raise ValueError("Accepted receipt historical role is not formal")
    if accepted.historical_at is None or _snapshot_time(
        accepted.historical_at,
        "Accepted historical time",
    ) > placed_time:
        raise ValueError("Accepted receipt historical role did not yet exist")
    if (
        node.historical_reason != accepted.historical_reason
        or node.historical_at != accepted.historical_at
        or node.caused_by_event_id != accepted.caused_by_event_id
        or node.caused_by_judgment_id != accepted.caused_by_judgment_id
        or node.replacement_insight_id != accepted.replacement_insight_id
    ):
        raise ValueError("Accepted receipt historical primary changed")

    primary = _render_primary_exit_fact(
        connection,
        node.insight_id,
        node.insight_version_id,
        accepted,
    )
    if node.primary_exit_fact != primary:
        raise ValueError("Accepted receipt event primary changed")
    cause = _load_judgment_cause_reference(connection, accepted)
    if node.primary_cause_accepted is None:
        if cause is not None:
            raise ValueError("Accepted receipt judgment primary is missing")
    else:
        _validate_snapshot_reference(
            connection,
            node.primary_cause_accepted,
            placed_time,
        )
        if cause is None or not _same_snapshot_reference_anchor(
            node.primary_cause_accepted,
            cause,
        ):
            raise ValueError("Accepted receipt judgment primary changed")

    current_additional = tuple(
        AcceptedRenderExitFact(
            fact_kind=fact.fact_kind,
            event_id=fact.event_id,
            reason_text=fact.reason_text,
            created_at=fact.created_at,
            replacement_insight_id=fact.replacement_insight_id,
        )
        for fact in accepted.additional_exit_facts
    )
    count = len(node.additional_exit_facts)
    if current_additional[:count] != node.additional_exit_facts:
        raise ValueError("Accepted receipt additional exit prefix changed")
    if any(
        _snapshot_time(fact.created_at, "Accepted exit fact time") <= placed_time
        for fact in current_additional[count:]
    ):
        raise ValueError("Accepted receipt omitted an existing exit fact")


def _validate_snapshot_participants(
    connection: sqlite3.Connection,
    node: AcceptedRenderLineageNode,
) -> None:
    rows = connection.execute(
        """
        SELECT participant_key, input_kind, knowledge_result_id, point_id,
               accepted_insight_version_id, position, contribution_text
        FROM insight_version_participants
        WHERE insight_version_id = ?
        ORDER BY position
        """,
        (node.insight_version_id,),
    ).fetchall()
    current = tuple(
        _participant_from_row(row, position)
        for position, row in enumerate(rows)
    )
    if current != node.participants:
        raise ValueError("Accepted receipt participant anchor changed")


def _validate_snapshot_used_relations(
    connection: sqlite3.Connection,
    node: AcceptedRenderLineageNode,
) -> None:
    current = tuple(
        _render_used_relation(connection, relation)
        for relation in _load_used_relations(
            connection,
            node.insight_version_id,
        )
    )
    if current != node.used_relations:
        raise ValueError("Accepted receipt used-relation anchor changed")
    for relation in node.used_relations:
        identity = connection.execute(
            """
            SELECT 1 FROM relation_identities WHERE relation_id = ?
            """,
            (relation.relation_id,),
        ).fetchone()
        if identity is None:
            raise ValueError("Accepted receipt relation identity is missing")


def _validate_snapshot_invalidation(
    receipt: AcceptedRenderSourceLeaf,
    current: AcceptedRenderSourceLeaf,
    placed_time: datetime,
) -> None:
    if receipt.invalidated_at is None:
        if current.invalidated_at is not None and _snapshot_time(
            current.invalidated_at,
            "KnowledgeResult invalidation time",
        ) < placed_time:
            raise ValueError("Accepted receipt omitted an existing invalidation")
        return
    if (
        current.invalidated_at != receipt.invalidated_at
        or current.invalidation_reason != receipt.invalidation_reason
        or _snapshot_time(
            receipt.invalidated_at,
            "KnowledgeResult invalidation time",
        ) > placed_time
    ):
        raise ValueError("Accepted receipt invalidation anchor changed")


def _validate_snapshot_reference(
    connection: sqlite3.Connection,
    reference: AcceptedEvolutionReference,
    placed_time: datetime,
    *,
    replacement_target_insight_id: int | None = None,
) -> None:
    accepted = read_accepted_row(connection, reference.insight_version_id)
    if accepted is None:
        raise ValueError("Accepted receipt evolution anchor is missing")
    row = accepted.row
    payload = decode_insight_payload(str(row["payload_json"]))
    if (
        int(row["insight_id"]) != reference.insight_id
        or int(row["version_no"]) != reference.version_no
        or int(row["judgment_id"]) != reference.judgment_id
        or str(row["accepted_at"]) != reference.accepted_at
        or str(row["initial_role"]) != reference.initial_role
        or payload.claim != reference.claim
    ):
        raise ValueError("Accepted receipt evolution anchor changed")
    if reference.current_role == "current":
        if accepted.effective_role is AcceptedRole.HISTORICAL:
            if reference.initial_role != "current" or accepted.historical_at is None:
                raise ValueError("Accepted receipt evolution role is impossible")
            if placed_time > _snapshot_time(
                accepted.historical_at,
                "Accepted evolution historical time",
            ):
                raise ValueError("Accepted receipt evolution role is stale")
    elif (
        accepted.effective_role is not AcceptedRole.HISTORICAL
        or accepted.historical_at is None
        or _snapshot_time(
            accepted.historical_at,
            "Accepted evolution historical time",
        ) > placed_time
    ):
        raise ValueError("Accepted receipt evolution history did not yet exist")

    if reference.relation_kind == "replaced_identity":
        if replacement_target_insight_id is None:
            raise ValueError("Accepted receipt replacement target is missing")
        replacement = connection.execute(
            """
            SELECT reason_text, created_at
            FROM insight_identity_replacements
            WHERE replaced_insight_id = ? AND replacement_insight_id = ?
              AND event_id = ?
            """,
            (
                reference.insight_id,
                replacement_target_insight_id,
                reference.relation_event_id,
            ),
        ).fetchone()
        if (
            replacement is None
            or str(replacement["reason_text"]) != reference.relation_reason_text
            or _snapshot_time(
                str(replacement["created_at"]),
                "Accepted replacement time",
            ) > placed_time
        ):
            raise ValueError("Accepted receipt replacement anchor changed")
    elif reference.relation_event_id is not None:
        raise ValueError("Accepted receipt evolution event is invalid")
    _validate_snapshot_publication(
        connection,
        reference.insight_version_id,
        reference.publication,
        placed_time,
    )


def _validate_snapshot_publication(
    connection: sqlite3.Connection,
    insight_version_id: int,
    publication: AcceptedPublicationLink | None,
    placed_time: datetime,
) -> None:
    current = _load_publication_link(connection, insight_version_id)
    if publication is not None:
        if current != publication:
            raise ValueError("Accepted receipt publication anchor changed")
        return
    if current is None:
        return
    row = connection.execute(
        """
        SELECT recorded_at FROM accepted_insight_publications
        WHERE insight_version_id = ?
        """,
        (insight_version_id,),
    ).fetchone()
    if row is None or _snapshot_time(
        str(row["recorded_at"]),
        "Accepted publication recording time",
    ) < placed_time:
        raise ValueError("Accepted receipt omitted an existing publication")


def _same_snapshot_reference_anchor(
    receipt: AcceptedEvolutionReference,
    current: AcceptedEvolutionReference,
) -> bool:
    return (
        receipt.relation_kind == current.relation_kind
        and receipt.relation_event_id == current.relation_event_id
        and receipt.relation_reason_text == current.relation_reason_text
        and receipt.insight_version_id == current.insight_version_id
        and receipt.insight_id == current.insight_id
        and receipt.version_no == current.version_no
        and receipt.judgment_id == current.judgment_id
        and receipt.accepted_at == current.accepted_at
        and receipt.initial_role == current.initial_role
        and receipt.claim == current.claim
    )


def _snapshot_time(value: str, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if result.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return result


def list_accepted_insights(
    database_path: Path,
    *,
    limit: int = 50,
) -> AcceptedInsightSearchResult:
    if limit <= 0:
        return AcceptedInsightSearchResult((), (), 0)
    cards, unreadable = _read_accepted_cards(
        database_path,
        failure_message="Accepted insight list could not be read safely",
    )
    current = tuple(
        card
        for detail, card in cards
        if detail.current_role is AcceptedRole.CURRENT
    )[: min(limit, 50)]
    historical = tuple(
        card
        for detail, card in cards
        if detail.current_role is AcceptedRole.HISTORICAL
    )[: min(limit, 50)]
    return AcceptedInsightSearchResult(current, historical, unreadable)


def search_accepted_insights(
    database_path: Path,
    query: str,
    *,
    limit: int = 50,
) -> AcceptedInsightSearchResult:
    normalized = normalize_search_text(query)
    if not normalized or limit <= 0:
        return AcceptedInsightSearchResult((), (), 0)
    terms = tuple(dict.fromkeys(normalized.split()))
    cards, unreadable = _read_accepted_cards(
        database_path,
        failure_message="Accepted insight search could not be read safely",
    )
    current: list[tuple[tuple[int, ...], AcceptedInsightSearchCard]] = []
    historical: list[tuple[tuple[int, ...], AcceptedInsightSearchCard]] = []
    for detail, card in cards:
        rank = _search_rank(detail, normalized, terms)
        if rank is None:
            continue
        target = (
            current
            if detail.current_role is AcceptedRole.CURRENT
            else historical
        )
        target.append((rank, card))

    def ordered(items):
        items.sort(
            key=lambda item: (
                *(-part for part in item[0]),
                -item[1].version_no,
                -item[1].insight_version_id,
            ),
            reverse=False,
        )
        return tuple(card for _, card in items[: min(limit, 50)])

    return AcceptedInsightSearchResult(
        ordered(current),
        ordered(historical),
        unreadable,
    )


def _read_accepted_cards(
    database_path: Path,
    *,
    failure_message: str,
) -> tuple[
    tuple[tuple[AcceptedInsightDetail, AcceptedInsightSearchCard], ...],
    int,
]:
    try:
        with connect(database_path) as connection:
            version_ids = tuple(
                int(row["insight_version_id"])
                for row in connection.execute(
                    """
                    SELECT insight_version_id
                    FROM accepted_insight_versions
                    ORDER BY accepted_at DESC, insight_version_id DESC
                    """
                ).fetchall()
            )
            cards = []
            unreadable = 0
            for insight_version_id in version_ids:
                try:
                    accepted = read_accepted_row(connection, insight_version_id)
                    if accepted is None:
                        raise ValueError("Accepted row disappeared")
                    detail = _decode_detail(connection, accepted)
                    cards.append(
                        (
                            detail,
                            AcceptedInsightSearchCard(
                                insight_version_id=detail.insight_version_id,
                                insight_id=detail.insight_id,
                                version_no=detail.version_no,
                                identity_kind=detail.identity_kind,
                                role=detail.current_role,
                                claim=detail.payload.claim,
                                short_discussion=detail.payload.short_discussion,
                                connection_reasons=detail.payload.connection_reasons,
                                limitations=detail.payload.limitations,
                                accepted_at=str(accepted.row["accepted_at"]),
                                historical_reason=detail.historical_reason,
                            ),
                        )
                    )
                except (json.JSONDecodeError, TypeError, ValueError):
                    unreadable += 1
    except sqlite3.Error as error:
        raise AcceptedInsightLibraryError(failure_message) from error
    return tuple(cards), unreadable


def list_topic_auxiliary_insights(
    database_path: Path,
    topic_id: int,
) -> TopicAuxiliaryResult:
    try:
        with connect(database_path) as connection:
            planning = load_topic_planning_input(connection)
            topic = next(
                (
                    item
                    for item in planning.existing_topics
                    if item.topic_id == topic_id and len(item.members) >= 2
                ),
                None,
            )
            if topic is None:
                return TopicAuxiliaryResult(False, (), 0)
            members = {
                SourcePointIdentity(item.knowledge_result_id, item.point_id)
                for item in topic.members
            }
            version_ids = tuple(
                int(row["insight_version_id"])
                for row in connection.execute(
                    """
                    SELECT insight_version_id
                    FROM accepted_insight_versions
                    WHERE current_role = 'current'
                    ORDER BY accepted_at DESC, insight_version_id DESC
                    """
                ).fetchall()
            )
            result = []
            unreadable = 0
            for version_id in version_ids:
                try:
                    accepted = read_accepted_row(connection, version_id)
                    if (
                        accepted is None
                        or accepted.effective_role is not AcceptedRole.CURRENT
                    ):
                        continue
                    detail = _decode_detail(connection, accepted)
                    matching = tuple(
                        sorted(
                            SourcePointIdentity(
                                item.knowledge_result_id, item.point_id
                            )
                            for item in detail.source_leaves
                            if SourcePointIdentity(
                                item.knowledge_result_id, item.point_id
                            )
                            in members
                        )
                    )
                    if matching:
                        result.append(
                            TopicAuxiliaryInsight(
                                topic_id,
                                detail.insight_version_id,
                                detail.insight_id,
                                detail.version_no,
                                detail.payload.claim,
                                matching,
                            )
                        )
                except (json.JSONDecodeError, TypeError, ValueError):
                    unreadable += 1
    except (
        sqlite3.Error,
        TopicLibraryError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise AcceptedInsightLibraryError(
            "Topic auxiliary insights could not be read safely"
        ) from error
    return TopicAuxiliaryResult(True, tuple(result), unreadable)


def list_current_accepted_inputs(
    database_path: Path,
) -> tuple[AcceptedInsightInput, ...]:
    try:
        with connect(database_path) as connection:
            sources = _load_current_source_inputs(connection)
            return _load_current_accepted_inputs(connection, sources)
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as error:
        raise AcceptedInsightLibraryError(
            "Current accepted inputs could not be read safely"
        ) from error


def _load_current_accepted_inputs(
    connection: sqlite3.Connection,
    sources: tuple[SourceKnowledgeInput, ...],
) -> tuple[AcceptedInsightInput, ...]:
    """Strict connection-level loader shared with OrganizationService."""
    source_points = {
        point.identity: source
        for source in sources
        for point in source.points
    }
    version_ids = tuple(
        int(row["insight_version_id"])
        for row in connection.execute(
            """
            SELECT insight_version_id
            FROM accepted_insight_versions
            WHERE current_role = 'current'
            ORDER BY insight_id, insight_version_id
            """
        ).fetchall()
    )
    result = []
    for insight_version_id in version_ids:
        accepted = read_accepted_row(connection, insight_version_id)
        if accepted is None:
            raise ValueError("Accepted current disappeared during strict read")
        if accepted.effective_role is not AcceptedRole.CURRENT:
            continue
        row = accepted.row
        payload = decode_insight_payload(str(row["payload_json"]))
        if semantic_signature(payload) != str(row["semantic_signature"]):
            raise ValueError("Accepted current semantic signature is invalid")
        lineage_nodes = load_insight_lineage_nodes(
            connection,
            insight_version_id,
            source_points.__contains__,
            root_requires_accepted=True,
        )
        leaves = lineage_nodes[0].source_leaves
        allowed = set(source_points)
        if not leaves or not set(leaves) <= allowed:
            raise ValueError("Accepted current has invalid source lineage")
        signature = sha256_json(
            {
                "codec": "accepted-qualification-v2",
                "insight_version_id": insight_version_id,
                "semantic_signature": str(row["semantic_signature"]),
                "dependency_signature": str(row["dependency_signature"]),
                "lineage": [
                    _lineage_node_payload(node) for node in lineage_nodes
                ],
                "source_leaves": [
                    [leaf.knowledge_result_id, leaf.point_id] for leaf in leaves
                ],
            }
        )
        result.append(
            AcceptedInsightInput(
                insight_version_id,
                int(row["insight_id"]),
                int(row["version_no"]),
                int(row["produced_event_id"]),
                payload,
                lineage_nodes,
                leaves,
                signature,
            )
        )
    return tuple(result)


def load_insight_lineage_nodes(
    connection: sqlite3.Connection,
    root_insight_version_id: int,
    source_is_readable: Callable[[SourcePointIdentity], bool],
    *,
    root_requires_accepted: bool,
) -> tuple[AcceptedInsightLineageNode, ...]:
    """Read complete, validated lineage using the caller's source-readability rule."""
    nodes: dict[int, AcceptedInsightLineageNode] = {}
    order: list[int] = []
    visiting: set[int] = set()

    def load(
        insight_version_id: int,
        *,
        requires_accepted: bool,
    ) -> tuple[SourcePointIdentity, ...]:
        if insight_version_id in visiting:
            raise ValueError("Accepted insight lineage has a cycle")
        existing = nodes.get(insight_version_id)
        if existing is not None:
            return existing.source_leaves
        accepted_join = (
            "JOIN accepted_insight_versions AS a "
            "ON a.insight_version_id = iv.insight_version_id "
            "AND a.insight_id = iv.insight_id"
            if requires_accepted
            else ""
        )
        row = connection.execute(
            f"""
            SELECT iv.insight_version_id, iv.insight_id, iv.version_no,
                   iv.produced_event_id, iv.payload_json, iv.semantic_signature
            FROM insight_versions AS iv
            {accepted_join}
            JOIN organization_events AS e ON e.event_id = iv.produced_event_id
            WHERE iv.insight_version_id = ? AND e.status = 'succeeded'
            """,
            (insight_version_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Accepted insight lineage is broken")
        payload = decode_insight_payload(str(row["payload_json"]))
        if semantic_signature(payload) != str(row["semantic_signature"]):
            raise ValueError("Accepted insight lineage semantic signature is invalid")
        participant_rows = connection.execute(
            """
            SELECT participant_key, input_kind, knowledge_result_id, point_id,
                   accepted_insight_version_id, position, contribution_text
            FROM insight_version_participants
            WHERE insight_version_id = ?
            ORDER BY position
            """,
            (insight_version_id,),
        ).fetchall()
        if len(participant_rows) < 2:
            raise ValueError("Accepted insight lineage is incomplete")
        participants = tuple(
            _participant_from_row(value, position)
            for position, value in enumerate(participant_rows)
        )
        visiting.add(insight_version_id)
        order.append(insight_version_id)
        leaves: set[SourcePointIdentity] = set()
        for participant in participants:
            if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
                identity = SourcePointIdentity(
                    participant.knowledge_result_id or 0,
                    participant.point_id or "",
                )
                if not source_is_readable(identity):
                    raise ValueError(
                        "Accepted insight lineage source is not safely readable"
                    )
                leaves.add(identity)
            else:
                leaves.update(
                    load(
                        participant.accepted_insight_version_id or 0,
                        requires_accepted=True,
                    )
                )
        visiting.remove(insight_version_id)
        if not leaves:
            raise ValueError("Accepted insight lineage has no source leaves")
        node = AcceptedInsightLineageNode(
            insight_version_id=int(row["insight_version_id"]),
            insight_id=int(row["insight_id"]),
            version_no=int(row["version_no"]),
            produced_event_id=int(row["produced_event_id"]),
            payload=payload,
            participants=participants,
            used_relations=_load_used_relations(
                connection, insight_version_id
            ),
            source_leaves=tuple(sorted(leaves)),
        )
        nodes[insight_version_id] = node
        return node.source_leaves

    load(root_insight_version_id, requires_accepted=root_requires_accepted)
    return tuple(nodes[value] for value in order)


def _load_judgment_target_lineage(
    connection: sqlite3.Connection,
    insight_version_id: int,
) -> tuple[AcceptedInsightLineageNode, ...]:
    """Strictly validate an established candidate before its first judgment."""
    exact_sources: dict[int, _ExactSource] = {}

    def source_is_readable(identity: SourcePointIdentity) -> bool:
        source = exact_sources.get(identity.knowledge_result_id)
        if source is None:
            source = _load_exact_source(connection, identity.knowledge_result_id)
            if source is None:
                return False
            exact_sources[identity.knowledge_result_id] = source
        return any(
            point.card.point_id == identity.point_id
            for point in source.document.points
        )

    return load_insight_lineage_nodes(
        connection,
        insight_version_id,
        source_is_readable,
        root_requires_accepted=False,
    )


def read_accepted_row(
    connection: sqlite3.Connection,
    insight_version_id: int,
) -> AcceptedInsightRow | None:
    """Read a validated grant; missing grants return None, corrupt facts raise."""
    row = connection.execute(
        """
        SELECT a.*, iv.version_no, iv.previous_version_id, iv.payload_json,
               iv.semantic_signature, iv.dependency_signature,
               iv.created_at AS insight_created_at, iv.produced_event_id,
               j.decision, j.annotation_text, j.decided_at,
               e.status AS produced_event_status, e.started_at,
               e.completed_at
        FROM accepted_insight_versions AS a
        JOIN insight_versions AS iv
          ON iv.insight_version_id = a.insight_version_id
         AND iv.insight_id = a.insight_id
        JOIN user_insight_judgments AS j
          ON j.judgment_id = a.judgment_id
         AND j.insight_version_id = a.insight_version_id
         AND j.insight_id = a.insight_id
         AND j.decision = a.judgment_decision
        JOIN organization_events AS e ON e.event_id = iv.produced_event_id
        WHERE a.insight_version_id = ?
        """,
        (insight_version_id,),
    ).fetchone()
    if row is None:
        accepted_exists = connection.execute(
            """
            SELECT 1 FROM accepted_insight_versions
            WHERE insight_version_id = ?
            """,
            (insight_version_id,),
        ).fetchone()
        if accepted_exists is not None:
            raise ValueError("Accepted version has a broken required reference")
        return None
    if not _valid_accepted_grant(connection, row) or row["produced_event_status"] != "succeeded":
        raise ValueError("Accepted version does not have a valid interesting origin")
    causes = _load_accepted_exit_facts(
        connection,
        int(row["insight_id"]),
        insight_version_id,
    )
    stored_role = AcceptedRole(str(row["current_role"]))
    if stored_role is AcceptedRole.HISTORICAL:
        reason = str(row["historical_reason"])
        primary = _stored_primary_exit_fact(row, causes)
        return AcceptedInsightRow(
            row,
            stored_role,
            reason,
            str(row["historical_at"]),
            (
                int(row["caused_by_event_id"])
                if row["caused_by_event_id"] is not None
                else None
            ),
            (
                int(row["caused_by_judgment_id"])
                if row["caused_by_judgment_id"] is not None
                else None
            ),
            (
                int(row["replacement_insight_id"])
                if row["replacement_insight_id"] is not None
                else None
            ),
            tuple(fact for fact in causes if fact is not primary),
        )
    if not causes:
        return AcceptedInsightRow(row, stored_role, None, None, None, None, None, ())
    primary = _select_primary_exit_fact(causes)
    return AcceptedInsightRow(
        row,
        AcceptedRole.HISTORICAL,
        primary.fact_kind,
        primary.created_at,
        primary.event_id,
        None,
        primary.replacement_insight_id,
        tuple(fact for fact in causes if fact is not primary),
    )


def _load_accepted_exit_facts(
    connection: sqlite3.Connection,
    insight_id: int,
    insight_version_id: int,
) -> tuple[AcceptedExitFact, ...]:
    facts = []
    replacement = connection.execute(
        """
        SELECT replacement_insight_id, event_id, reason_text, created_at
        FROM insight_identity_replacements
        WHERE replaced_insight_id = ?
        """,
        (insight_id,),
    ).fetchone()
    if replacement is not None:
        facts.append(
            AcceptedExitFact(
                fact_kind="identity_replaced",
                event_id=int(replacement["event_id"]),
                reason_text=str(replacement["reason_text"]),
                created_at=str(replacement["created_at"]),
                replacement_insight_id=int(replacement["replacement_insight_id"]),
            )
        )
    facts.extend(
        AcceptedExitFact(
            fact_kind=str(item["fact_kind"]),
            event_id=int(item["event_id"]),
            reason_text=str(item["reason_text"]),
            created_at=str(item["created_at"]),
        )
        for item in connection.execute(
            """
            SELECT fact_kind, event_id, reason_text, created_at
            FROM insight_version_disqualifications
            WHERE insight_version_id = ?
            ORDER BY disqualification_id
            """,
            (insight_version_id,),
        ).fetchall()
    )
    return tuple(
        sorted(
            facts,
            key=lambda fact: (
                fact.event_id,
                {"refuted": 0, "basis_invalid": 1, "identity_replaced": 2}[
                    fact.fact_kind
                ],
            ),
        )
    )


def _select_primary_exit_fact(
    facts: tuple[AcceptedExitFact, ...],
) -> AcceptedExitFact:
    if not facts:
        raise ValueError("Accepted exit fact set is empty")
    first_event_id = min(fact.event_id for fact in facts)
    first_event = tuple(fact for fact in facts if fact.event_id == first_event_id)
    kinds = {fact.fact_kind for fact in first_event}
    if "identity_replaced" in kinds and len(kinds) > 1:
        raise ValueError("Accepted version has ambiguous same-event exit causes")
    if "refuted" in kinds:
        return next(fact for fact in first_event if fact.fact_kind == "refuted")
    return first_event[0]


def _stored_primary_exit_fact(
    row: sqlite3.Row,
    facts: tuple[AcceptedExitFact, ...],
) -> AcceptedExitFact | None:
    reason = str(row["historical_reason"])
    if reason not in {"basis_invalid", "refuted", "identity_replaced"}:
        return None
    if row["caused_by_event_id"] is None:
        raise ValueError("Accepted historical primary is missing its event")
    event_id = int(row["caused_by_event_id"])
    replacement_id = (
        int(row["replacement_insight_id"])
        if row["replacement_insight_id"] is not None
        else None
    )
    matches = tuple(
        fact
        for fact in facts
        if fact.fact_kind == reason
        and fact.event_id == event_id
        and (
            reason != "identity_replaced"
            or fact.replacement_insight_id == replacement_id
        )
    )
    if len(matches) != 1:
        raise ValueError("Accepted historical primary does not match an exact fact")
    return matches[0]


def _decode_detail(
    connection: sqlite3.Connection,
    accepted: AcceptedInsightRow,
) -> AcceptedInsightDetail:
    row = accepted.row
    payload = decode_insight_payload(str(row["payload_json"]))
    if semantic_signature(payload) != str(row["semantic_signature"]):
        raise ValueError("Accepted insight semantic signature is invalid")
    exact_sources: dict[int, _ExactSource] = {}

    def source_is_readable(identity: SourcePointIdentity) -> bool:
        source = exact_sources.get(identity.knowledge_result_id)
        if source is None:
            source = _load_exact_source(connection, identity.knowledge_result_id)
            if source is None:
                return False
            exact_sources[identity.knowledge_result_id] = source
        return any(
            point.card.point_id == identity.point_id
            for point in source.document.points
        )

    lineage = load_insight_lineage_nodes(
        connection,
        int(row["insight_version_id"]),
        source_is_readable,
        root_requires_accepted=True,
    )
    leaves = tuple(
        _source_leaf(exact_sources, identity)
        for identity in lineage[0].source_leaves
    )
    completed_at = row["completed_at"]
    if completed_at is None:
        raise ValueError("Accepted formation event is incomplete")
    return AcceptedInsightDetail(
        insight_version_id=int(row["insight_version_id"]),
        insight_id=int(row["insight_id"]),
        version_no=int(row["version_no"]),
        identity_kind="ai_derived_insight",
        payload=payload,
        judgment=AcceptedJudgmentRead(
            int(row["judgment_id"]),
            str(row["decision"]),
            _INTERESTING_MEANING,
            (
                str(row["annotation_text"])
                if row["annotation_text"] is not None
                else None
            ),
            str(row["decided_at"]),
        ),
        initial_role=AcceptedRole(str(row["initial_role"])),
        current_role=accepted.effective_role,
        historical_reason=accepted.historical_reason,
        historical_at=accepted.historical_at,
        caused_by_event_id=accepted.caused_by_event_id,
        caused_by_judgment_id=accepted.caused_by_judgment_id,
        replacement_insight_id=accepted.replacement_insight_id,
        additional_exit_facts=accepted.additional_exit_facts,
        formation_event=AcceptedFormationEvent(
            int(row["produced_event_id"]),
            str(row["started_at"]),
            str(completed_at),
        ),
        top_level_participants=lineage[0].participants,
        used_relations=lineage[0].used_relations,
        recursive_lineage=lineage,
        source_leaves=leaves,
        publication=_load_publication_link(
            connection,
            int(row["insight_version_id"]),
        ),
    )


def _decode_render_context(
    connection: sqlite3.Connection,
    accepted: AcceptedInsightRow,
) -> AcceptedInsightRenderContext:
    root_row = accepted.row
    exact_sources: dict[int, _ExactSource] = {}

    def source_is_readable(identity: SourcePointIdentity) -> bool:
        source = exact_sources.get(identity.knowledge_result_id)
        if source is None:
            source = _load_exact_source(connection, identity.knowledge_result_id)
            if source is None:
                return False
            exact_sources[identity.knowledge_result_id] = source
        return any(
            point.point_id == identity.point_id
            for point in source.candidate.core_points + source.candidate.other_points
        )

    lineage = load_insight_lineage_nodes(
        connection,
        int(root_row["insight_version_id"]),
        source_is_readable,
        root_requires_accepted=True,
    )
    render_nodes = []
    for node in lineage:
        node_accepted = read_accepted_row(connection, node.insight_version_id)
        if node_accepted is None:
            raise ValueError("Accepted render lineage lost its accepted root")
        render_nodes.append(
            _render_lineage_node(
                connection,
                node,
                node_accepted,
                root_insight_version_id=int(root_row["insight_version_id"]),
            )
        )
    sources = tuple(
        _render_source_leaf(exact_sources, identity)
        for identity in lineage[0].source_leaves
    )
    evolution_references = _load_evolution_references(
        connection,
        int(root_row["insight_version_id"]),
        int(root_row["insight_id"]),
    )
    root = render_nodes[0]
    snapshot_fact_ids = _render_snapshot_fact_ids(
        tuple(render_nodes),
        sources,
        evolution_references,
    )
    return AcceptedInsightRenderContext(
        insight_version_id=root.insight_version_id,
        insight_id=root.insight_id,
        version_no=root.version_no,
        root=root,
        lineage_nodes=tuple(render_nodes),
        source_leaves=sources,
        evolution_references=evolution_references,
        snapshot_fact_ids=snapshot_fact_ids,
    )


def _render_lineage_node(
    connection: sqlite3.Connection,
    node: AcceptedInsightLineageNode,
    accepted: AcceptedInsightRow,
    *,
    root_insight_version_id: int,
) -> AcceptedRenderLineageNode:
    row = accepted.row
    completed_at = row["completed_at"]
    if completed_at is None:
        raise ValueError("Accepted formation event is incomplete")
    existing_publication = None
    if node.insight_version_id != root_insight_version_id:
        existing_publication = _load_publication_link(
            connection,
            node.insight_version_id,
        )
    accepted_previous_version_id = None
    if row["previous_version_id"] is not None:
        previous_version_id = int(row["previous_version_id"])
        if read_accepted_row(connection, previous_version_id) is not None:
            accepted_previous_version_id = previous_version_id
    return AcceptedRenderLineageNode(
        insight_version_id=node.insight_version_id,
        insight_id=node.insight_id,
        version_no=node.version_no,
        previous_version_id=accepted_previous_version_id,
        semantic_signature=str(row["semantic_signature"]),
        dependency_signature=str(row["dependency_signature"]),
        created_at=str(row["insight_created_at"]),
        payload=node.payload,
        judgment=AcceptedRenderJudgment(
            judgment_id=int(row["judgment_id"]),
            decision=str(row["decision"]),
            annotation_text=(
                str(row["annotation_text"])
                if row["annotation_text"] is not None
                else None
            ),
            decided_at=str(row["decided_at"]),
        ),
        initial_role=str(row["initial_role"]),
        current_role=accepted.effective_role.value,
        historical_reason=accepted.historical_reason,
        historical_at=accepted.historical_at,
        caused_by_event_id=accepted.caused_by_event_id,
        caused_by_judgment_id=accepted.caused_by_judgment_id,
        replacement_insight_id=accepted.replacement_insight_id,
        primary_cause_accepted=_load_judgment_cause_reference(
            connection,
            accepted,
        ),
        primary_exit_fact=_render_primary_exit_fact(
            connection,
            node.insight_id,
            node.insight_version_id,
            accepted,
        ),
        additional_exit_facts=tuple(
            AcceptedRenderExitFact(
                fact_kind=fact.fact_kind,
                event_id=fact.event_id,
                reason_text=fact.reason_text,
                created_at=fact.created_at,
                replacement_insight_id=fact.replacement_insight_id,
            )
            for fact in accepted.additional_exit_facts
        ),
        formation_event=AcceptedRenderFormationEvent(
            event_id=int(row["produced_event_id"]),
            started_at=str(row["started_at"]),
            completed_at=str(completed_at),
        ),
        participants=node.participants,
        used_relations=tuple(
            _render_used_relation(connection, value)
            for value in node.used_relations
        ),
        source_leaf_identities=tuple(
            (value.knowledge_result_id, value.point_id)
            for value in node.source_leaves
        ),
        existing_publication=existing_publication,
    )


def _render_primary_exit_fact(
    connection: sqlite3.Connection,
    insight_id: int,
    insight_version_id: int,
    accepted: AcceptedInsightRow,
) -> AcceptedRenderExitFact | None:
    if accepted.caused_by_event_id is None:
        return None
    matches = tuple(
        fact
        for fact in _load_accepted_exit_facts(
            connection,
            insight_id,
            insight_version_id,
        )
        if fact.fact_kind == accepted.historical_reason
        and fact.event_id == accepted.caused_by_event_id
        and (
            fact.fact_kind != "identity_replaced"
            or fact.replacement_insight_id == accepted.replacement_insight_id
        )
    )
    if len(matches) != 1:
        raise ValueError("Accepted render primary exit fact is invalid")
    fact = matches[0]
    return AcceptedRenderExitFact(
        fact_kind=fact.fact_kind,
        event_id=fact.event_id,
        reason_text=fact.reason_text,
        created_at=fact.created_at,
        replacement_insight_id=fact.replacement_insight_id,
    )


def _render_used_relation(
    connection: sqlite3.Connection,
    value: UsedRelationInput,
) -> AcceptedRenderUsedRelation:
    payload = decode_relation_payload(value.payload_json)
    row = connection.execute(
        """
        SELECT semantic_signature, dependency_signature
        FROM relation_versions
        WHERE relation_version_id = ? AND relation_id = ? AND version_no = ?
        """,
        (value.relation_version_id, value.relation_id, value.version_no),
    ).fetchone()
    if row is None or semantic_signature(payload) != str(row["semantic_signature"]):
        raise ValueError("Accepted used relation exact version is invalid")
    return AcceptedRenderUsedRelation(
        relation_version_id=value.relation_version_id,
        relation_id=value.relation_id,
        version_no=value.version_no,
        produced_event_id=value.produced_event_id,
        position=value.position,
        role_text=value.role_text,
        payload=payload,
        semantic_signature=str(row["semantic_signature"]),
        dependency_signature=str(row["dependency_signature"]),
    )


def _render_source_leaf(
    sources: Mapping[int, _ExactSource],
    identity: SourcePointIdentity,
) -> AcceptedRenderSourceLeaf:
    source = sources.get(identity.knowledge_result_id)
    if source is None:
        raise ValueError("Accepted render source leaf is missing")
    points = source.candidate.core_points + source.candidate.other_points
    point = next((item for item in points if item.point_id == identity.point_id), None)
    if point is None:
        raise ValueError("Accepted render source point is missing")
    role = (
        "core"
        if any(item.point_id == identity.point_id for item in source.candidate.core_points)
        else "other"
    )
    evidence_by_id = {
        item.evidence_id: item for item in source.candidate.evidence_registry
    }
    try:
        evidences = tuple(
            AcceptedRenderEvidence(
                evidence_id=evidence_by_id[evidence_id].evidence_id,
                source_fact_id=evidence_by_id[evidence_id].source_fact_id,
                start_offset=evidence_by_id[evidence_id].start_offset,
                end_offset=evidence_by_id[evidence_id].end_offset,
                evidence_text=evidence_by_id[evidence_id].evidence_text,
            )
            for evidence_id in point.evidence_ids
        )
    except KeyError as error:
        raise ValueError("Accepted render source evidence is missing") from error
    row = source.row
    card = next(
        item.card
        for item in source.document.points
        if item.card.point_id == identity.point_id
    )
    return AcceptedRenderSourceLeaf(
        material_id=int(row["material_id"]),
        platform=str(row["platform"]),
        platform_item_id=str(row["platform_item_id"]),
        original_url=str(row["original_url"]),
        canonical_url=(
            str(row["canonical_url"])
            if row["canonical_url"] is not None
            else None
        ),
        material_created_at=str(row["material_created_at"]),
        knowledge_result_id=int(row["knowledge_result_id"]),
        knowledge_result_created_at=str(row["knowledge_result_created_at"]),
        knowledge_payload_json=str(row["payload_json"]),
        invalidated_at=(
            str(row["invalidated_at"])
            if row["invalidated_at"] is not None
            else None
        ),
        invalidation_reason=(
            str(row["invalidation_reason"])
            if row["invalidation_reason"] is not None
            else None
        ),
        published_at=str(row["published_at"]),
        published_path=str(row["published_path"]),
        source_fact_id=int(row["source_fact_id"]),
        source_metadata_json=str(row["metadata_json"]),
        content_snapshot=str(row["content_snapshot"]),
        uncertainty_json=str(row["uncertainty_json"]),
        replaces_source_fact_id=(
            int(row["replaces_source_fact_id"])
            if row["replaces_source_fact_id"] is not None
            else None
        ),
        source_change_reason=(
            str(row["change_reason"])
            if row["change_reason"] is not None
            else None
        ),
        source_fact_created_at=str(row["source_fact_created_at"]),
        point_id=identity.point_id,
        point_role=role,
        point_statement=point.statement,
        point_argument=point.argument,
        evidences=evidences,
        knowledge_title=source.candidate.title,
        knowledge_summary=source.candidate.summary,
        source_label=card.source_label,
    )


def _load_evolution_references(
    connection: sqlite3.Connection,
    root_insight_version_id: int,
    root_insight_id: int,
) -> tuple[AcceptedEvolutionReference, ...]:
    values: dict[tuple[str, int, int], AcceptedEvolutionReference] = {}
    predecessor = connection.execute(
        """
        SELECT previous_version_id
        FROM insight_versions
        WHERE insight_version_id = ? AND insight_id = ?
        """,
        (root_insight_version_id, root_insight_id),
    ).fetchone()
    if predecessor is None:
        raise ValueError("Accepted render root disappeared")
    if predecessor["previous_version_id"] is not None:
        reference = _load_accepted_reference(
            connection,
            int(predecessor["previous_version_id"]),
            relation_kind="direct_predecessor",
        )
        if reference is not None:
            values[
                (
                    reference.relation_kind,
                    reference.insight_id,
                    reference.insight_version_id,
                )
            ] = reference

    replaced_versions = connection.execute(
        """
        SELECT replacement.event_id, replacement.reason_text,
               a.insight_version_id
        FROM insight_identity_replacements AS replacement
        JOIN accepted_insight_versions AS a
          ON a.insight_id = replacement.replaced_insight_id
        JOIN insight_versions AS iv
          ON iv.insight_version_id = a.insight_version_id
         AND iv.insight_id = a.insight_id
        WHERE replacement.replacement_insight_id = ?
        ORDER BY replacement.event_id, iv.version_no, a.insight_version_id
        """,
        (root_insight_id,),
    ).fetchall()
    for row in replaced_versions:
        reference = _load_accepted_reference(
            connection,
            int(row["insight_version_id"]),
            relation_kind="replaced_identity",
            relation_event_id=int(row["event_id"]),
            relation_reason_text=str(row["reason_text"]),
        )
        if reference is None:
            raise ValueError("Accepted replacement reference disappeared")
        values.setdefault(
            (
                reference.relation_kind,
                reference.insight_id,
                reference.insight_version_id,
            ),
            reference,
        )
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (
                item.relation_kind,
                item.insight_id,
                item.version_no,
                item.insight_version_id,
            ),
        )
    )


def _load_judgment_cause_reference(
    connection: sqlite3.Connection,
    accepted: AcceptedInsightRow,
) -> AcceptedEvolutionReference | None:
    judgment_id = accepted.caused_by_judgment_id
    if judgment_id is None:
        return None
    row = connection.execute(
        """
        SELECT insight_version_id
        FROM user_insight_judgments
        WHERE judgment_id = ?
        """,
        (judgment_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Accepted historical judgment cause is broken")
    reference = _load_accepted_reference(
        connection,
        int(row["insight_version_id"]),
        relation_kind="historical_cause",
    )
    if reference is None or reference.judgment_id != judgment_id:
        raise ValueError("Accepted historical judgment cause is not accepted")
    return reference


def _load_accepted_reference(
    connection: sqlite3.Connection,
    insight_version_id: int,
    *,
    relation_kind: str,
    relation_event_id: int | None = None,
    relation_reason_text: str | None = None,
) -> AcceptedEvolutionReference | None:
    accepted = read_accepted_row(connection, insight_version_id)
    if accepted is None:
        return None
    row = accepted.row
    payload = decode_insight_payload(str(row["payload_json"]))
    if semantic_signature(payload) != str(row["semantic_signature"]):
        raise ValueError("Accepted evolution reference semantic is invalid")
    return AcceptedEvolutionReference(
        relation_kind=relation_kind,
        relation_event_id=relation_event_id,
        relation_reason_text=relation_reason_text,
        insight_version_id=insight_version_id,
        insight_id=int(row["insight_id"]),
        version_no=int(row["version_no"]),
        judgment_id=int(row["judgment_id"]),
        accepted_at=str(row["accepted_at"]),
        initial_role=str(row["initial_role"]),
        current_role=accepted.effective_role.value,
        claim=payload.claim,
        publication=_load_publication_link(connection, insight_version_id),
    )


def _load_publication_link(
    connection: sqlite3.Connection,
    insight_version_id: int,
) -> AcceptedPublicationLink | None:
    row = connection.execute(
        """
        SELECT p.*, a.insight_id, a.judgment_id AS accepted_judgment_id,
               iv.version_no, iv.payload_json, iv.semantic_signature
        FROM accepted_insight_publications AS p
        JOIN accepted_insight_versions AS a
          ON a.insight_version_id = p.insight_version_id
         AND a.judgment_id = p.judgment_id
        JOIN insight_versions AS iv
          ON iv.insight_version_id = a.insight_version_id
         AND iv.insight_id = a.insight_id
        WHERE p.insight_version_id = ?
        """,
        (insight_version_id,),
    ).fetchone()
    if row is None:
        exists = connection.execute(
            """
            SELECT 1 FROM accepted_insight_publications
            WHERE insight_version_id = ?
            """,
            (insight_version_id,),
        ).fetchone()
        if exists is not None:
            raise ValueError("Accepted publication navigation is broken")
        return None
    payload = decode_insight_payload(str(row["payload_json"]))
    if semantic_signature(payload) != str(row["semantic_signature"]):
        raise ValueError("Accepted publication navigation semantic is invalid")
    relative_path = str(row["relative_path"])
    machine_identity = str(row["machine_identity"])
    insight_id = int(row["insight_id"])
    version_no = int(row["version_no"])
    if relative_path != accepted_insight_relative_path(insight_id, version_no):
        raise ValueError("Accepted publication navigation path is invalid")
    if machine_identity != accepted_insight_machine_identity(
        insight_id,
        insight_version_id,
    ):
        raise ValueError("Accepted publication navigation identity is invalid")
    for key in ("content_sha256", "render_context_signature"):
        value = str(row[key])
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Accepted publication navigation checksum is invalid")
    receipt = decode_accepted_placement_receipt(str(row["placement_receipt_json"]))
    if (
        receipt.insight_version_id != insight_version_id
        or receipt.judgment_id != int(row["accepted_judgment_id"])
        or receipt.relative_path != relative_path
        or receipt.machine_identity != machine_identity
        or receipt.render_context_signature != str(row["render_context_signature"])
    ):
        raise ValueError("Accepted publication navigation receipt is invalid")
    return AcceptedPublicationLink(
        publication_id=int(row["publication_id"]),
        insight_version_id=insight_version_id,
        insight_id=insight_id,
        version_no=version_no,
        claim=payload.claim,
        relative_path=relative_path,
        machine_identity=machine_identity,
    )


def _render_snapshot_fact_ids(
    nodes: tuple[AcceptedRenderLineageNode, ...],
    sources: tuple[AcceptedRenderSourceLeaf, ...],
    evolution_references: tuple[AcceptedEvolutionReference, ...],
) -> tuple[str, ...]:
    values: set[str] = set()
    for node in nodes:
        values.update(
            {
                f"accepted:{node.insight_version_id}",
                f"insight:{node.insight_id}",
                f"insight-version:{node.insight_version_id}",
                f"judgment:{node.judgment.judgment_id}",
                f"organization-event:{node.formation_event.event_id}",
            }
        )
        values.update(
            f"insight-participant:{node.insight_version_id}:{item.position}"
            for item in node.participants
        )
        for relation in node.used_relations:
            values.add(f"relation:{relation.relation_id}")
            values.add(f"relation-version:{relation.relation_version_id}")
        for fact in node.additional_exit_facts:
            values.add(
                f"accepted-exit:{node.insight_version_id}:{fact.event_id}:"
                f"{fact.fact_kind}"
            )
        if node.primary_exit_fact is not None:
            values.add(
                f"accepted-exit:{node.insight_version_id}:"
                f"{node.primary_exit_fact.event_id}:"
                f"{node.primary_exit_fact.fact_kind}"
            )
        if node.caused_by_judgment_id is not None:
            values.add(f"judgment:{node.caused_by_judgment_id}")
        if node.primary_cause_accepted is not None:
            _add_evolution_reference_fact_ids(
                values,
                node.primary_cause_accepted,
                replacement_insight_id=None,
            )
        if node.replacement_insight_id is not None:
            values.add(f"insight:{node.replacement_insight_id}")
        if node.existing_publication is not None:
            values.add(f"publication:{node.existing_publication.publication_id}")
    for source in sources:
        values.update(
            {
                f"material:{source.material_id}",
                f"source-fact:{source.source_fact_id}",
                f"knowledge-result:{source.knowledge_result_id}",
                f"source-point:{source.knowledge_result_id}:{source.point_id}",
            }
        )
        values.update(
            f"evidence:{source.knowledge_result_id}:{source.point_id}:"
            f"{evidence.evidence_id}"
            for evidence in source.evidences
        )
    for reference in evolution_references:
        _add_evolution_reference_fact_ids(
            values,
            reference,
            replacement_insight_id=nodes[0].insight_id,
        )
    return tuple(sorted(values))


def _add_evolution_reference_fact_ids(
    values: set[str],
    reference: AcceptedEvolutionReference,
    *,
    replacement_insight_id: int | None,
) -> None:
    values.update(
        {
            f"accepted:{reference.insight_version_id}",
            f"insight:{reference.insight_id}",
            f"insight-version:{reference.insight_version_id}",
            f"judgment:{reference.judgment_id}",
        }
    )
    if reference.relation_event_id is not None:
        values.add(f"organization-event:{reference.relation_event_id}")
        if replacement_insight_id is None:
            raise ValueError("Accepted replacement snapshot target is missing")
        values.add(
            f"identity-replacement:{reference.insight_id}:"
            f"{replacement_insight_id}:{reference.relation_event_id}"
        )
    if reference.publication is not None:
        values.add(f"publication:{reference.publication.publication_id}")


def _search_rank(detail, normalized: str, terms: tuple[str, ...]):
    primary_fields = (
        detail.payload.claim,
        detail.payload.short_discussion,
        *detail.payload.connection_reasons,
    )
    primary = tuple(normalize_search_text(value) for value in primary_fields)
    auxiliary = tuple(
        normalize_search_text(value.text) for value in detail.payload.limitations
    )
    term_primary = tuple(any(term in value for value in primary) for term in terms)
    term_any = tuple(
        primary_hit or any(term in value for value in auxiliary)
        for term, primary_hit in zip(terms, term_primary, strict=True)
    )
    if not all(term_any) or not any(term_primary):
        return None
    claim = primary[0]
    discussion = primary[1]
    reasons = primary[2:]
    return (
        int(claim == normalized),
        int(normalized in claim),
        sum(term in claim for term in terms),
        sum(term in discussion for term in terms),
        sum(any(term in reason for reason in reasons) for term in terms),
        sum(term_primary),
    )


def _load_exact_source(
    connection: sqlite3.Connection,
    knowledge_result_id: int,
) -> _ExactSource | None:
    row = connection.execute(
        """
        SELECT m.material_id, m.platform, m.platform_item_id, m.original_url,
               m.canonical_url, m.created_at AS material_created_at,
               kr.knowledge_result_id, kr.source_fact_id, kr.payload_json,
               kr.created_at, kr.created_at AS knowledge_result_created_at,
               kr.invalidated_at, kr.invalidation_reason, kr.published_at,
               kr.published_path, sf.metadata_json, sf.content_snapshot,
               sf.uncertainty_json, sf.replaces_source_fact_id,
               sf.change_reason, sf.created_at AS source_fact_created_at
        FROM knowledge_results AS kr
        JOIN source_facts AS sf ON sf.source_fact_id = kr.source_fact_id
        JOIN materials AS m ON m.material_id = sf.material_id
        WHERE kr.published_at IS NOT NULL
          AND kr.published_path IS NOT NULL
          AND TRIM(kr.published_path) != ''
          AND kr.knowledge_result_id = ?
        """,
        (knowledge_result_id,),
    ).fetchone()
    if row is None:
        return None
    _validate_handoff_path(str(row["published_path"]))
    payload = json.loads(str(row["payload_json"]))
    metadata = json.loads(str(row["metadata_json"]))
    uncertainties = json.loads(str(row["uncertainty_json"]))
    if (
        not isinstance(payload, Mapping)
        or not isinstance(metadata, Mapping)
        or not isinstance(uncertainties, list)
    ):
        raise ValueError("Accepted source formal facts are invalid")
    candidate = knowledge_candidate_from_payload(
        int(row["source_fact_id"]),
        str(row["content_snapshot"]),
        payload,
    )
    document = decode_formal_knowledge_row(row)
    evidence_ids = {
        item.point_id: item.evidence_ids
        for item in candidate.core_points + candidate.other_points
    }
    return _ExactSource(document, evidence_ids, row, candidate)


def _source_leaf(
    sources: Mapping[int, _ExactSource],
    identity: SourcePointIdentity,
) -> AcceptedSourceLeaf:
    source = sources.get(identity.knowledge_result_id)
    if source is None:
        raise ValueError("Accepted source leaf is missing")
    point = next(
        (item for item in source.document.points if item.card.point_id == identity.point_id),
        None,
    )
    if point is None:
        raise ValueError("Accepted source point is missing")
    return AcceptedSourceLeaf(
        identity.knowledge_result_id,
        source.document.source_fact_id,
        identity.point_id,
        point.card.role,
        point.card.statement,
        point.argument,
        source.evidence_ids_by_point[identity.point_id],
        point.card.knowledge_title,
        point.card.source_label,
        point.card.platform,
        point.card.published_path,
    )


def _load_current_source_inputs(
    connection: sqlite3.Connection,
) -> tuple[SourceKnowledgeInput, ...]:
    sources = []
    for row in query_searchable_formal_knowledge(connection):
        document = decode_formal_knowledge_row(row)
        points = tuple(
            SourcePointInput(
                document.knowledge_result_id,
                point.card.point_id,
                point.card.role,
                point.card.statement,
                point.argument,
            )
            for point in document.points
        )
        if not points:
            raise ValueError("Formal knowledge has no points")
        sources.append(
            SourceKnowledgeInput(
                document.knowledge_result_id,
                document.source_fact_id,
                document.title,
                document.summary,
                points,
                "eligible_history",
                sha256_json(
                    {
                        "codec": "source-qualification-v1",
                        "knowledge_result_id": document.knowledge_result_id,
                        "source_fact_id": document.source_fact_id,
                        "payload_json": str(row["payload_json"]),
                        "published_at": str(row["published_at"]),
                        "published_path": str(row["published_path"]),
                    }
                ),
            )
        )
    return tuple(sorted(sources, key=lambda item: item.knowledge_result_id))


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
        point_id=str(row["point_id"]) if row["point_id"] is not None else None,
        accepted_insight_version_id=(
            int(row["accepted_insight_version_id"])
            if row["accepted_insight_version_id"] is not None
            else None
        ),
    )


def _load_used_relations(
    connection: sqlite3.Connection,
    insight_version_id: int,
) -> tuple[UsedRelationInput, ...]:
    rows = connection.execute(
        """
        SELECT edge.relation_version_id, edge.position, edge.role_text,
               relation.relation_id, relation.version_no,
               relation.produced_event_id, relation.payload_json,
               relation.semantic_signature
        FROM insight_version_used_relations AS edge
        JOIN relation_versions AS relation
          ON relation.relation_version_id = edge.relation_version_id
        JOIN organization_events AS event
          ON event.event_id = relation.produced_event_id
        WHERE edge.insight_version_id = ? AND event.status = 'succeeded'
        ORDER BY edge.position
        """,
        (insight_version_id,),
    ).fetchall()
    expected = int(
        connection.execute(
            "SELECT COUNT(*) FROM insight_version_used_relations WHERE insight_version_id = ?",
            (insight_version_id,),
        ).fetchone()[0]
    )
    if len(rows) != expected:
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
                int(row["relation_version_id"]),
                int(row["relation_id"]),
                int(row["version_no"]),
                int(row["produced_event_id"]),
                position,
                str(row["role_text"]),
                canonical_json(json.loads(str(row["payload_json"]))),
            )
        )
    return tuple(result)


def _lineage_node_payload(node: AcceptedInsightLineageNode):
    return {
        "insight_version_id": node.insight_version_id,
        "insight_id": node.insight_id,
        "version_no": node.version_no,
        "produced_event_id": node.produced_event_id,
        "payload": insight_payload_to_dict(node.payload),
        "participants": [
            ({
                "participant_key": item.participant_key,
                "input_kind": item.input_kind.value,
                "position": item.position,
                "contribution_text": item.contribution_text,
                "knowledge_result_id": item.knowledge_result_id,
                "point_id": item.point_id,
            } if item.input_kind is InputKind.SOURCE_KNOWLEDGE else {
                "participant_key": item.participant_key,
                "input_kind": item.input_kind.value,
                "position": item.position,
                "contribution_text": item.contribution_text,
                "accepted_insight_version_id": item.accepted_insight_version_id,
            })
            for item in node.participants
        ],
        "used_relations": [
            {
                "relation_version_id": item.relation_version_id,
                "relation_id": item.relation_id,
                "version_no": item.version_no,
                "produced_event_id": item.produced_event_id,
                "position": item.position,
                "role_text": item.role_text,
                "payload": json.loads(item.payload_json),
                "authority": "historical_basis_edge_only",
            }
            for item in node.used_relations
        ],
        "source_leaves": [
            [leaf.knowledge_result_id, leaf.point_id] for leaf in node.source_leaves
        ],
    }


def _validate_handoff_path(value: str) -> None:
    if value != value.strip() or "\\" in value or "\x00" in value:
        raise ValueError("Published source handoff path is unsafe")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.suffix.lower() != ".md"
        or not value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Published source handoff path is unsafe")


def _valid_accepted_grant(connection, row):
    if row['decision'] == 'interesting':
        return True
    if row['decision'] != 'rethink' or 'reconsideration_id' not in row.keys() or row['reconsideration_id'] is None:
        return False
    return connection.execute("""SELECT 1 FROM insight_reconsiderations
        WHERE reconsideration_id=? AND insight_version_id=? AND judgment_id=?
        AND decision='interesting_after_rethink'""", (row['reconsideration_id'],row['insight_version_id'],row['judgment_id'])).fetchone() is not None
