from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Sequence

from .database import connect, query_searchable_formal_knowledge, utc_now
from .knowledge_library import (
    FormalPointCard,
    decode_formal_knowledge_row,
)
from .topic_indexing import (
    ExistingTopicInput,
    TopicDraft,
    TopicIndexFailure,
    TopicIndexer,
    TopicPlan,
    TopicPointInput,
    TopicPointReference,
    normalize_topic_name,
)


TOPIC_INPUT_VERSION = "topic-input-v1"


class TopicRefreshKind(StrEnum):
    CURRENT = "current"
    REFRESHED = "refreshed"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    KNOWLEDGE_CHANGED = "knowledge_changed"
    BASELINE_CHANGED = "baseline_changed"


@dataclass(frozen=True)
class TopicRefreshResult:
    kind: TopicRefreshKind
    failure: TopicIndexFailure | None = None


@dataclass(frozen=True)
class TopicPointProjection:
    card: FormalPointCard
    knowledge_summary: str


@dataclass(frozen=True)
class TopicCard:
    topic_id: int
    name: str
    scope: str
    points: tuple[TopicPointProjection, ...]

    @property
    def cards(self) -> tuple[FormalPointCard, ...]:
        return tuple(point.card for point in self.points)

    @property
    def representative_cards(self) -> tuple[FormalPointCard, ...]:
        return self.cards[:3]


@dataclass(frozen=True)
class TopicLibrarySnapshot:
    topics: tuple[TopicCard, ...]
    has_index: bool
    current: bool
    empty_knowledge: bool
    unreadable_count: int
    uncovered_count: int

    @property
    def unassigned_count(self) -> int:
        return self.uncovered_count if self.current else 0


class TopicLibraryError(Exception):
    pass


@dataclass(frozen=True)
class _LoadedPoints:
    points: tuple[TopicPointInput, ...]
    projections: dict[TopicPointReference, TopicPointProjection]
    unreadable_count: int


@dataclass(frozen=True)
class TopicPlanningInput:
    points: tuple[TopicPointInput, ...]
    existing_topics: tuple[ExistingTopicInput, ...]
    source_signature: str
    snapshot: TopicLibrarySnapshot
    before_payload: dict[str, object]
    before_signature: str
    guard_payload: dict[str, object]
    guard_signature: str


class TopicLibrary:
    def __init__(self, database_path: Path, indexer: TopicIndexer):
        self.database_path = database_path
        self.indexer = indexer
        self._refresh_lock = threading.Lock()

    def indexer_available(self) -> bool:
        return self.indexer.is_available()

    def snapshot(self) -> TopicLibrarySnapshot:
        try:
            with connect(self.database_path) as connection:
                rows = query_searchable_formal_knowledge(connection)
                loaded = _load_points(rows, strict=False)
                state = connection.execute(
                    "SELECT source_signature FROM topic_index_state WHERE state_id = 1"
                ).fetchone()
                topics = _read_topic_cards(connection, loaded.projections)
        except sqlite3.Error as error:
            raise TopicLibraryError("主题索引暂时无法安全读取") from error

        if not loaded.points and not loaded.unreadable_count:
            return TopicLibrarySnapshot((), state is not None, False, True, 0, 0)
        source_signature = (
            compute_source_signature(loaded.points)
            if loaded.unreadable_count == 0
            else None
        )
        current = bool(
            state is not None
            and source_signature is not None
            and state["source_signature"] == source_signature
        )
        member_ids = {
            TopicPointReference(card.knowledge_result_id, card.point_id)
            for topic in topics
            for card in topic.cards
        }
        point_ids = {point.reference for point in loaded.points}
        return TopicLibrarySnapshot(
            topics,
            state is not None,
            current,
            False,
            loaded.unreadable_count,
            len(point_ids - member_ids),
        )

    def topic(self, topic_id: int) -> tuple[TopicCard | None, TopicLibrarySnapshot]:
        snapshot = self.snapshot()
        return next(
            (topic for topic in snapshot.topics if topic.topic_id == topic_id),
            None,
        ), snapshot

    def refresh(self, *, force: bool = False) -> TopicRefreshResult:
        with self._refresh_lock:
            try:
                with connect(self.database_path) as connection:
                    planning_input = load_topic_planning_input(connection)
                    if not planning_input.points:
                        return TopicRefreshResult(TopicRefreshKind.EMPTY)
                    signature = planning_input.source_signature
                    state = connection.execute(
                        "SELECT source_signature FROM topic_index_state WHERE state_id = 1"
                    ).fetchone()
                    if (
                        not force
                        and state is not None
                        and state["source_signature"] == signature
                    ):
                        return TopicRefreshResult(TopicRefreshKind.CURRENT)
                    existing_topics = planning_input.existing_topics
                    starting_guard = planning_input.guard_signature
            except (sqlite3.Error, TopicLibraryError):
                return TopicRefreshResult(TopicRefreshKind.FAILED)

            if not self.indexer.is_available():
                return TopicRefreshResult(TopicRefreshKind.UNAVAILABLE)
            indexing = self.indexer.organize(planning_input.points, existing_topics)
            if indexing.plan is None:
                kind = (
                    TopicRefreshKind.UNAVAILABLE
                    if indexing.failure is TopicIndexFailure.RUNTIME_UNAVAILABLE
                    else TopicRefreshKind.FAILED
                )
                return TopicRefreshResult(kind, indexing.failure)

            try:
                with connect(self.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = load_topic_planning_input(connection)
                    if current.source_signature != signature:
                        connection.rollback()
                        return TopicRefreshResult(TopicRefreshKind.KNOWLEDGE_CHANGED)
                    if current.guard_signature != starting_guard:
                        connection.rollback()
                        return TopicRefreshResult(TopicRefreshKind.BASELINE_CHANGED)
                    _replace_topic_index(connection, indexing.plan, signature)
            except (sqlite3.Error, TopicLibraryError):
                return TopicRefreshResult(TopicRefreshKind.FAILED)
            return TopicRefreshResult(TopicRefreshKind.REFRESHED)


def compute_source_signature(
    points: Sequence[TopicPointInput],
    *,
    version: str = TOPIC_INPUT_VERSION,
) -> str:
    canonical = {
        "version": version,
        "points": [
            {
                "knowledge_result_id": point.knowledge_result_id,
                "source_fact_id": point.source_fact_id,
                "point_id": point.point_id,
                "role": point.role,
                "statement": point.statement,
                "argument": point.argument,
                "title": point.title,
                "summary": point.summary,
            }
            for point in sorted(
                points,
                key=lambda item: (item.knowledge_result_id, item.point_id),
            )
        ],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_topic_planning_input(
    connection: sqlite3.Connection,
) -> TopicPlanningInput:
    loaded = _load_points(query_searchable_formal_knowledge(connection), strict=True)
    source_signature = compute_source_signature(loaded.points)
    state = connection.execute(
        "SELECT source_signature FROM topic_index_state WHERE state_id = 1"
    ).fetchone()
    raw_topic_count = int(
        connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    )
    safe_topics = tuple(
        topic
        for topic in _read_existing_topics(
            connection, {point.reference for point in loaded.points}
        )
        if len(topic.members) >= 2
    )
    member_ids = {member for topic in safe_topics for member in topic.members}
    point_ids = {point.reference for point in loaded.points}
    current = bool(state is not None and state["source_signature"] == source_signature)
    snapshot = TopicLibrarySnapshot(
        topics=tuple(
            TopicCard(topic.topic_id, topic.name, topic.scope, ())
            for topic in safe_topics
        ),
        has_index=state is not None,
        current=current,
        empty_knowledge=not loaded.points,
        unreadable_count=0,
        uncovered_count=len(point_ids - member_ids),
    )
    if not loaded.points:
        before_state = "empty_knowledge"
    elif state is None:
        before_state = "no_index"
    elif current:
        before_state = "legal_empty_index" if raw_topic_count == 0 else "current"
    else:
        before_state = "stale_safe_subset"
    before_payload: dict[str, object] = {
        "codec": "topic-safety-snapshot-v1",
        "state": before_state,
        "topics": [_existing_topic_payload(topic) for topic in safe_topics],
        "uncovered_count": len(point_ids - member_ids),
    }
    guard_payload = build_topic_overwrite_guard(
        connection,
        valid_points=point_ids,
        state=state,
        raw_topic_count=raw_topic_count,
    )
    return TopicPlanningInput(
        points=loaded.points,
        existing_topics=safe_topics,
        source_signature=source_signature,
        snapshot=snapshot,
        before_payload=before_payload,
        before_signature=_signature(before_payload),
        guard_payload=guard_payload,
        guard_signature=_signature(guard_payload),
    )


def build_topic_overwrite_guard(
    connection: sqlite3.Connection,
    *,
    valid_points: set[TopicPointReference] | None = None,
    state: sqlite3.Row | None | object = ...,
    raw_topic_count: int | None = None,
) -> dict[str, object]:
    if valid_points is None:
        loaded = _load_points(query_searchable_formal_knowledge(connection), strict=True)
        valid_points = {point.reference for point in loaded.points}
    if state is ...:
        state = connection.execute(
            "SELECT source_signature FROM topic_index_state WHERE state_id = 1"
        ).fetchone()
    if raw_topic_count is None:
        raw_topic_count = int(
            connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        )
    safe_topics = tuple(
        topic
        for topic in _read_existing_topics(connection, valid_points)
        if len(topic.members) >= 2
    )
    return {
        "codec": "topic-overwrite-guard-v2",
        "index_presence": "absent" if state is None else "present",
        "indexed_source_signature": (
            None if state is None else str(state["source_signature"])
        ),
        "committed_shape": (
            "none"
            if state is None
            else "empty"
            if raw_topic_count == 0
            else "has_topics"
        ),
        "safe_topics": [
            {
                "normalized_name": normalize_topic_name(topic.name),
                "name": topic.name,
                "scope": topic.scope,
                "members": [
                    {
                        "knowledge_result_id": member.knowledge_result_id,
                        "point_id": member.point_id,
                        "position": position,
                    }
                    for position, member in enumerate(topic.members)
                ],
            }
            for topic in sorted(
                safe_topics,
                key=lambda item: (
                    normalize_topic_name(item.name),
                    item.topic_id,
                ),
            )
        ],
    }


def topic_plan_payload(plan: TopicPlan) -> dict[str, object]:
    return {
        "topics": [
            {
                **(
                    {"topic_id": topic.topic_id}
                    if topic.topic_id is not None
                    else {"new_topic_key": topic.new_topic_key}
                ),
                "name": topic.name,
                "scope": topic.scope,
                "members": [
                    {
                        "knowledge_result_id": member.knowledge_result_id,
                        "point_id": member.point_id,
                    }
                    for member in topic.members
                ],
            }
            for topic in plan.topics
        ],
        "unassigned_points": [
            {
                "knowledge_result_id": member.knowledge_result_id,
                "point_id": member.point_id,
            }
            for member in plan.unassigned_points
        ],
    }


def replace_topic_index(
    connection: sqlite3.Connection,
    plan: TopicPlan,
    source_signature: str,
) -> None:
    _replace_topic_index(connection, plan, source_signature)


def _signature(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _existing_topic_payload(topic: ExistingTopicInput) -> dict[str, object]:
    return {
        "topic_id": topic.topic_id,
        "name": topic.name,
        "scope": topic.scope,
        "members": [
            {
                "knowledge_result_id": member.knowledge_result_id,
                "point_id": member.point_id,
                "position": position,
            }
            for position, member in enumerate(topic.members)
        ],
    }


def _load_points(rows: Iterable[sqlite3.Row], *, strict: bool) -> _LoadedPoints:
    points: list[TopicPointInput] = []
    projections: dict[TopicPointReference, TopicPointProjection] = {}
    unreadable_count = 0
    for row in rows:
        try:
            document = decode_formal_knowledge_row(row)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            if strict:
                raise TopicLibraryError("当前正式知识无法完整读取") from error
            unreadable_count += 1
            continue
        for point in document.points:
            topic_point = TopicPointInput(
                document.knowledge_result_id,
                document.source_fact_id,
                point.card.point_id,
                point.card.role,
                point.card.statement,
                point.argument,
                document.title,
                document.summary,
            )
            points.append(topic_point)
            projections[topic_point.reference] = TopicPointProjection(
                point.card,
                document.summary,
            )
    return _LoadedPoints(tuple(points), projections, unreadable_count)


def _read_existing_topics(
    connection: sqlite3.Connection,
    valid_points: set[TopicPointReference],
) -> tuple[ExistingTopicInput, ...]:
    rows = connection.execute(
        """
        SELECT t.topic_id, t.name, t.scope,
               tm.knowledge_result_id, tm.point_id, tm.position
        FROM topics AS t
        LEFT JOIN topic_memberships AS tm ON tm.topic_id = t.topic_id
        ORDER BY t.topic_id, tm.position
        """
    ).fetchall()
    grouped: dict[int, tuple[str, str, list[TopicPointReference]]] = {}
    for row in rows:
        topic_id = int(row["topic_id"])
        grouped.setdefault(topic_id, (str(row["name"]), str(row["scope"]), []))
        if row["knowledge_result_id"] is not None:
            reference = TopicPointReference(
                int(row["knowledge_result_id"]), str(row["point_id"])
            )
            if reference in valid_points:
                grouped[topic_id][2].append(reference)
    return tuple(
        ExistingTopicInput(topic_id, name, scope, tuple(members))
        for topic_id, (name, scope, members) in grouped.items()
    )


def _read_topic_cards(
    connection: sqlite3.Connection,
    projections: dict[TopicPointReference, TopicPointProjection],
) -> tuple[TopicCard, ...]:
    existing = _read_existing_topics(connection, set(projections))
    return tuple(
        TopicCard(
            topic.topic_id,
            topic.name,
            topic.scope,
            tuple(projections[reference] for reference in topic.members),
        )
        for topic in existing
        if len(topic.members) >= 2
    )


def _replace_topic_index(
    connection: sqlite3.Connection,
    plan: TopicPlan,
    source_signature: str,
) -> None:
    reused_ids = {topic.topic_id for topic in plan.topics if topic.topic_id is not None}
    connection.execute("DELETE FROM topic_memberships")
    if reused_ids:
        placeholders = ",".join("?" for _ in reused_ids)
        connection.execute(
            f"DELETE FROM topics WHERE topic_id NOT IN ({placeholders})",
            tuple(sorted(reused_ids)),
        )
        for topic_id in sorted(reused_ids):
            connection.execute(
                "UPDATE topics SET normalized_name = ? WHERE topic_id = ?",
                (f"__topic_refresh__{topic_id}", topic_id),
            )
    else:
        connection.execute("DELETE FROM topics")

    resolved: list[tuple[int, TopicDraft]] = []
    for topic in plan.topics:
        normalized_name = normalize_topic_name(topic.name)
        if topic.topic_id is not None:
            connection.execute(
                """
                UPDATE topics SET normalized_name = ?, name = ?, scope = ?
                WHERE topic_id = ?
                """,
                (normalized_name, topic.name, topic.scope, topic.topic_id),
            )
            topic_id = topic.topic_id
        else:
            topic_id = int(
                connection.execute(
                    """
                    INSERT INTO topics (normalized_name, name, scope)
                    VALUES (?, ?, ?)
                    """,
                    (normalized_name, topic.name, topic.scope),
                ).lastrowid
            )
        resolved.append((topic_id, topic))

    for topic_id, topic in resolved:
        connection.executemany(
            """
            INSERT INTO topic_memberships (
                topic_id, knowledge_result_id, point_id, position
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (
                    topic_id,
                    member.knowledge_result_id,
                    member.point_id,
                    position,
                )
                for position, member in enumerate(topic.members)
            ],
        )
    connection.execute(
        """
        INSERT INTO topic_index_state (state_id, source_signature, indexed_at)
        VALUES (1, ?, ?)
        ON CONFLICT(state_id) DO UPDATE SET
            source_signature = excluded.source_signature,
            indexed_at = excluded.indexed_at
        """,
        (source_signature, utc_now()),
    )
