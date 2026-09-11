from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class CapturedMaterial:
    source_kind: str
    source_key: str
    submitted_url: str
    canonical_url: str
    metadata: Mapping[str, object]
    media_path: Path
    duration_seconds: float

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                self.source_kind,
                self.source_key,
                self.submitted_url,
                self.canonical_url,
            )
        ):
            raise ValueError("captured material identity is incomplete")
        if not self.media_path.is_file() or self.duration_seconds <= 0:
            raise ValueError("captured material has no verified media")


@dataclass(frozen=True)
class SourceFact:
    snapshot: str
    uncertainties: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not self.snapshot.strip():
            raise ValueError("source fact snapshot is empty")


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    start: int
    end: int
    text: str
    member_id: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None


@dataclass(frozen=True)
class Point:
    point_id: str
    statement: str
    argument: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Knowledge:
    title: str
    subtitle: str
    summary: str
    core_points: tuple[Point, ...]
    other_points: tuple[Point, ...]
    evidence: tuple[Evidence, ...]


def validate_knowledge(snapshot: str, knowledge: Knowledge) -> None:
    identity_text = (knowledge.title, knowledge.subtitle, knowledge.summary)
    normalized_identity = tuple(value.strip() for value in identity_text)
    if not snapshot.strip() or any(not value for value in normalized_identity):
        raise ValueError("knowledge title, subtitle, summary, and source are required")
    if (
        any("\n" in value or "\r" in value for value in identity_text)
        or len(set(normalized_identity)) != len(normalized_identity)
        or not (knowledge.core_points or knowledge.other_points)
        or not knowledge.evidence
    ):
        raise ValueError("knowledge structure is incomplete")

    points = knowledge.core_points + knowledge.other_points
    point_ids = [point.point_id for point in points]
    evidence_ids = [item.evidence_id for item in knowledge.evidence]
    if len(point_ids) != len(set(point_ids)) or len(evidence_ids) != len(
        set(evidence_ids)
    ):
        raise ValueError("knowledge ids must be unique")

    available = set(evidence_ids)
    referenced: set[str] = set()
    unknown_ranges = []
    offset = snapshot.find("[听辨不清]")
    while offset >= 0:
        unknown_ranges.append((offset, offset + len("[听辨不清]")))
        offset = snapshot.find("[听辨不清]", offset + len("[听辨不清]"))
    for item in knowledge.evidence:
        if snapshot == '[原始图片来源]' and item.member_id is None:
            raise ValueError('image-only source has no textual evidence')
        if item.member_id != 'video-1' and (item.start_seconds is not None or item.end_seconds is not None):
            raise ValueError('only video evidence has a time range')
        if item.member_id is not None and item.member_id != 'video-1':
            if (not isinstance(item.member_id, str) or not item.member_id.startswith('image-')
                    or not item.member_id[6:].isdigit() or int(item.member_id[6:]) < 1
                    or item.start != 0 or item.end != 0 or not item.text.strip() or not item.evidence_id.strip()):
                raise ValueError('image evidence is invalid')
            continue
        if item.member_id == 'video-1':
            import math
            if (not isinstance(item.start_seconds, (int, float)) or not isinstance(item.end_seconds, (int, float))
                    or isinstance(item.start_seconds, bool) or isinstance(item.end_seconds, bool)
                    or not math.isfinite(item.start_seconds) or not math.isfinite(item.end_seconds)
                    or not 0 <= item.start_seconds < item.end_seconds):
                raise ValueError('video evidence time range is invalid')
        if any(item.start < end and item.end > start for start, end in unknown_ranges):
            raise ValueError("knowledge evidence contains unrecognized source")
        if (
            not item.evidence_id.strip()
            or not item.text.strip()
            or not 0 <= item.start < item.end <= len(snapshot)
            or snapshot[item.start : item.end] != item.text
        ):
            raise ValueError("knowledge evidence does not match source")
    for point in points:
        if (
            not point.point_id.strip()
            or not point.statement.strip()
            or not point.argument.strip()
            or not point.evidence_ids
            or len(point.evidence_ids) != len(set(point.evidence_ids))
            or any(item not in available for item in point.evidence_ids)
        ):
            raise ValueError("knowledge point is incomplete")
        referenced.update(point.evidence_ids)
    if referenced != available:
        raise ValueError("every evidence item must support a point")


def knowledge_to_dict(knowledge: Knowledge) -> dict[str, object]:
    return {
        "title": knowledge.title,
        "subtitle": knowledge.subtitle,
        "summary": knowledge.summary,
        "core_points": [_point_to_dict(point) for point in knowledge.core_points],
        "other_points": [_point_to_dict(point) for point in knowledge.other_points],
        "evidence": [
            {
                "id": item.evidence_id,
                "start": item.start,
                "end": item.end,
                "text": item.text,
                **({"member_id": item.member_id} if item.member_id is not None else {}),
                **({'start_seconds': item.start_seconds, 'end_seconds': item.end_seconds} if item.member_id == 'video-1' else {}),
            }
            for item in knowledge.evidence
        ],
    }


def knowledge_from_dict(snapshot: str, value: object) -> Knowledge:
    if not isinstance(value, Mapping):
        raise ValueError("knowledge payload is not an object")
    try:
        core = _points_from_value(value["core_points"])
        other = _points_from_value(value["other_points"])
        evidence = _evidence_from_value(value["evidence"])
        knowledge = Knowledge(
            title=_text(value["title"]),
            subtitle=_text(value["subtitle"]),
            summary=_text(value["summary"]),
            core_points=core,
            other_points=other,
            evidence=evidence,
        )
    except (KeyError, TypeError) as error:
        raise ValueError("knowledge payload is incomplete") from error
    validate_knowledge(snapshot, knowledge)
    return knowledge


def _point_to_dict(point: Point) -> dict[str, object]:
    return {
        "id": point.point_id,
        "statement": point.statement,
        "argument": point.argument,
        "evidence_ids": list(point.evidence_ids),
    }


def _points_from_value(value: object) -> tuple[Point, ...]:
    if not isinstance(value, list):
        raise ValueError("knowledge points are not a list")
    points: list[Point] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(
            item.get("evidence_ids"), list
        ):
            raise ValueError("knowledge point is invalid")
        ids = item["evidence_ids"]
        if any(not isinstance(candidate, str) for candidate in ids):
            raise ValueError("knowledge evidence ids are invalid")
        points.append(
            Point(
                point_id=_text(item.get("id")),
                statement=_text(item.get("statement")),
                argument=_text(item.get("argument")),
                evidence_ids=tuple(ids),
            )
        )
    return tuple(points)


def _evidence_from_value(value: object) -> tuple[Evidence, ...]:
    if not isinstance(value, list):
        raise ValueError("knowledge evidence is not a list")
    evidence: list[Evidence] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("knowledge evidence is invalid")
        start, end = item.get("start"), item.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            raise ValueError("knowledge evidence offsets are invalid")
        evidence.append(
            Evidence(_text(item.get("id")), start, end, _text(item.get("text")), item.get('member_id'),
                     item.get('start_seconds'), item.get('end_seconds'))
        )
    return tuple(evidence)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("knowledge text is empty")
    return value.strip()
