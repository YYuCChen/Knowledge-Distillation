from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .database import list_recent_formal_knowledge, list_searchable_formal_knowledge
from .knowledge_derivation import knowledge_candidate_from_payload


@dataclass(frozen=True)
class FormalPointCard:
    knowledge_result_id: int
    point_id: str
    role: str
    statement: str
    knowledge_title: str
    source_label: str
    platform: str
    published_path: str


@dataclass(frozen=True)
class FormalKnowledgePoint:
    card: FormalPointCard
    argument: str
    point_order: int


@dataclass(frozen=True)
class FormalKnowledgeDocument:
    knowledge_result_id: int
    source_fact_id: int
    title: str
    summary: str
    created_timestamp: float
    points: tuple[FormalKnowledgePoint, ...]
    normalized_context: tuple[str, ...]


class KnowledgeLibraryError(Exception):
    pass


@dataclass(frozen=True)
class FormalPointSearchResult:
    results: tuple[FormalPointCard, ...]
    unreadable_count: int


@dataclass(frozen=True)
class RecentKnowledgeRecord:
    knowledge_result_id: int
    title: str
    summary: str
    core_point_statements: tuple[str, ...]
    source_label: str
    platform: str
    published_at: str
    published_path: str


@dataclass(frozen=True)
class RecentKnowledgeReadResult:
    records: tuple[RecentKnowledgeRecord, ...]
    unreadable_count: int


@dataclass(frozen=True)
class _RankedPoint:
    card: FormalPointCard
    rank: tuple[int, int, int, int, int]
    created_timestamp: float
    point_order: int


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def search_formal_points(
    database_path: Path,
    query: str,
    *,
    limit: int = 50,
) -> FormalPointSearchResult:
    normalized_query = normalize_search_text(query)
    if not normalized_query or limit <= 0:
        return FormalPointSearchResult((), 0)
    terms = tuple(dict.fromkeys(normalized_query.split()))
    ranked: list[_RankedPoint] = []
    try:
        rows = list_searchable_formal_knowledge(database_path)
    except sqlite3.Error as error:
        raise KnowledgeLibraryError("正式知识库暂时无法安全读取") from error

    unreadable_count = 0
    for row in rows:
        try:
            document = decode_formal_knowledge_row(row)
            ranked.extend(_matching_points(document, normalized_query, terms))
        except (json.JSONDecodeError, TypeError, ValueError):
            unreadable_count += 1

    ranked.sort(
        key=lambda item: (
            *(-part for part in item.rank),
            -int(item.card.role == "core"),
            -item.created_timestamp,
            item.point_order,
            item.card.knowledge_result_id,
            item.card.point_id,
        )
    )
    unique: list[FormalPointCard] = []
    seen: set[tuple[int, str]] = set()
    for item in ranked:
        identity = (item.card.knowledge_result_id, item.card.point_id)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item.card)
        if len(unique) == min(limit, 50):
            break
    return FormalPointSearchResult(tuple(unique), unreadable_count)


def read_recent_formal_knowledge(
    database_path: Path,
    *,
    limit: int = 6,
) -> RecentKnowledgeReadResult:
    """Project newest eligible formal results into lightweight page records."""
    visible_limit = min(max(limit, 0), 6)
    result = _read_formal_knowledge_records(
        database_path,
        error_message="最近沉淀暂时无法安全读取",
    )
    return RecentKnowledgeReadResult(
        result.records[:visible_limit],
        result.unreadable_count,
    )


def read_all_formal_knowledge(
    database_path: Path,
) -> RecentKnowledgeReadResult:
    """Project every readable formal result in publication order."""
    return _read_formal_knowledge_records(
        database_path,
        error_message="来源型知识暂时无法安全读取",
    )


def _read_formal_knowledge_records(
    database_path: Path,
    *,
    error_message: str,
) -> RecentKnowledgeReadResult:
    try:
        rows = list_recent_formal_knowledge(database_path)
    except sqlite3.Error as error:
        raise KnowledgeLibraryError(error_message) from error

    records: list[RecentKnowledgeRecord] = []
    unreadable_count = 0
    for row in rows:
        try:
            document = decode_formal_knowledge_row(row)
            if not document.points:
                raise ValueError("Formal knowledge has no readable points")
            first_card = document.points[0].card
            record = RecentKnowledgeRecord(
                knowledge_result_id=document.knowledge_result_id,
                title=document.title,
                summary=document.summary,
                core_point_statements=tuple(
                    point.card.statement
                    for point in document.points
                    if point.card.role == "core"
                ),
                source_label=first_card.source_label,
                platform=first_card.platform,
                published_at=str(row["published_at"]),
                published_path=first_card.published_path,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            unreadable_count += 1
            continue
        records.append(record)
    return RecentKnowledgeReadResult(tuple(records), unreadable_count)


def decode_formal_knowledge_row(row) -> FormalKnowledgeDocument:
    """Decode one eligible formal result for every knowledge-library entrypoint."""
    payload = json.loads(row["payload_json"])
    metadata = json.loads(row["metadata_json"])
    if not isinstance(payload, Mapping) or not isinstance(metadata, Mapping):
        raise ValueError("Formal knowledge payload is invalid")
    candidate = knowledge_candidate_from_payload(
        int(row["source_fact_id"]),
        str(row["content_snapshot"]),
        payload,
    )
    source_label, source_context = _source_context(metadata)
    platform = str(row["platform"])
    context = tuple(
        normalize_search_text(value)
        for value in (
            candidate.title,
            candidate.summary,
            platform,
            *source_context,
        )
        if value
    )
    created_timestamp = datetime.fromisoformat(str(row["created_at"])).timestamp()
    points = (
        *(("core", point) for point in candidate.core_points),
        *(("other", point) for point in candidate.other_points),
    )
    formal_points = tuple(
        FormalKnowledgePoint(
            FormalPointCard(
                int(row["knowledge_result_id"]),
                point.point_id,
                role,
                point.statement,
                candidate.title,
                source_label,
                platform,
                str(row["published_path"]),
            ),
            point.argument,
            point_order,
        )
        for point_order, (role, point) in enumerate(points)
    )
    return FormalKnowledgeDocument(
        int(row["knowledge_result_id"]),
        int(row["source_fact_id"]),
        candidate.title,
        candidate.summary,
        created_timestamp,
        formal_points,
        context,
    )


def _matching_points(
    document: FormalKnowledgeDocument,
    normalized_query: str,
    terms: tuple[str, ...],
) -> list[_RankedPoint]:
    matches: list[_RankedPoint] = []
    for point in document.points:
        statement = normalize_search_text(point.card.statement)
        argument = normalize_search_text(point.argument)
        statement_hits = sum(term in statement for term in terms)
        argument_hits = sum(term in argument for term in terms)
        point_hits = tuple(term in statement or term in argument for term in terms)
        context_hits = tuple(
            any(term in value for value in document.normalized_context)
            for term in terms
        )
        if not any(point_hits) or not all(
            point_hit or context_hit
            for point_hit, context_hit in zip(point_hits, context_hits, strict=True)
        ):
            continue
        matches.append(
            _RankedPoint(
                point.card,
                (
                    int(statement == normalized_query),
                    int(normalized_query in statement),
                    statement_hits,
                    argument_hits,
                    sum(context_hits),
                ),
                document.created_timestamp,
                point.point_order,
            )
        )
    return matches


def _source_context(metadata: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    author = metadata.get("author")
    display_name: str | None = None
    account_id: str | None = None
    if author is not None:
        if not isinstance(author, Mapping):
            raise ValueError("SourceFact metadata is invalid")
        display_name = _optional_metadata_text(author.get("display_name"))
        account_id = _optional_metadata_text(author.get("platform_account_id"))
    description = _optional_metadata_text(metadata.get("original_description"))
    source_label = display_name or account_id or "来源信息未标注"
    values = tuple(
        value for value in (display_name, account_id, description) if value is not None
    )
    return source_label, values


def _optional_metadata_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("SourceFact metadata is invalid")
    return value
