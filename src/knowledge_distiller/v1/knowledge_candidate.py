"""Unconnected R14 text candidates; structural validity is not source support.

Candidate hashes and generated IDs are diagnostic, never persisted identities.
No model calls, recovery budget, publishing, or source mutation happen here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .domain import Evidence, Knowledge, Point, validate_knowledge
from .knowledge_model import source_segments

CONTRACT_VERSION = "r14-inline-text-v1"
PARSER_VERSION = "r14-candidate-parser-v1"
VALIDATOR_VERSION = "r14-candidate-validator-v1"


class CandidateError(ValueError):
    """Safe machine diagnostics: neither payload values nor source in messages."""

    def __init__(self, category: str, field_path: str):
        self.category = category
        self.field_path = field_path
        super().__init__(f"{category}: {field_path}")


@dataclass(frozen=True)
class KnowledgeCandidate:
    qualified: bool
    knowledge: Knowledge | None
    rejection_reason: str | None
    candidate_hash: str
    snapshot_sha256: str
    contract_version: str = CONTRACT_VERSION
    parser_version: str = PARSER_VERSION
    validator_version: str = VALIDATOR_VERSION
    # No supported/unsupported judgment is inferred from valid numbering.
    support_status: str = "not_reviewed"


def _object(value: object, path: str, required: set[str], optional=frozenset()) -> dict:
    if not isinstance(value, dict):
        raise CandidateError("object_required", path)
    for key in sorted(required):
        if key not in value:
            raise CandidateError("field_missing", f"{path}.{key}")
    if set(value) - required - optional:
        # Do not echo untrusted field names: they may contain source text.
        raise CandidateError("field_unexpected", path)
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateError("text_empty_or_invalid", path)
    return value  # Preserve statements, arguments, and rejection reasons verbatim.


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError("json_duplicate_key", "$")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise CandidateError("json_syntax_invalid", "$")


def parse_knowledge_candidate(
    snapshot: str, text: str, *, source_kind: str = "text"
) -> KnowledgeCandidate:
    """Parse strict JSON against unchanged text, then run domain validation.

    Images, videos, and collections require distinct future contracts. The
    caller must declare their kind; this API does not infer media from text.
    """
    if source_kind != "text":
        raise CandidateError("source_kind_unsupported", "$.source_kind")
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise CandidateError("snapshot_empty_or_invalid", "$.snapshot")
    if not isinstance(text, str):
        raise CandidateError("json_syntax_invalid", "$")
    try:
        payload = json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except CandidateError:
        raise
    except (ValueError, RecursionError):
        # Suppress JSON decoder context, which holds the original response.
        raise CandidateError("json_syntax_invalid", "$") from None
    if not isinstance(payload, dict):
        raise CandidateError("object_required", "$")
    if "qualified" not in payload:
        raise CandidateError("field_missing", "$.qualified")
    if not isinstance(payload["qualified"], bool):
        raise CandidateError("boolean_required", "$.qualified")
    qualified = payload["qualified"]
    if not qualified:
        _object(payload, "$", {"qualified", "rejection_reason"})
        reason = _text(payload["rejection_reason"], "$.rejection_reason")
        knowledge = None
    else:
        _object(payload, "$", {"qualified", "title", "subtitle", "summary", "core_points", "other_points"}, {"rejection_reason"})
        if payload.get("rejection_reason") not in (None, ""):
            raise CandidateError("qualified_rejection_conflict", "$.rejection_reason")
        identity = []
        for key in ("title", "subtitle", "summary"):
            value = _text(payload[key], f"$.{key}")
            if "\n" in value or "\r" in value:
                raise CandidateError("single_line_required", f"$.{key}")
            if value.strip() in [item.strip() for item in identity]:
                raise CandidateError("identity_duplicate", f"$.{key}")
            identity.append(value)
        segments = source_segments(snapshot)
        evidence: list[Evidence] = []
        range_ids: dict[tuple[int, int], str] = {}
        groups = []
        point_count = 0
        for group in ("core_points", "other_points"):
            if not isinstance(payload[group], list):
                raise CandidateError("list_required", f"$.{group}")
            points = []
            for index, item in enumerate(payload[group]):
                path = f"$.{group}[{index}]"
                _object(item, path, {"statement", "argument", "source_ranges"})
                statement = _text(item["statement"], f"{path}.statement")
                argument = _text(item["argument"], f"{path}.argument")
                ranges = item["source_ranges"]
                if not isinstance(ranges, list) or not ranges:
                    raise CandidateError("ranges_empty_or_invalid", f"{path}.source_ranges")
                seen = set()
                ids = []
                for number, selection in enumerate(ranges):
                    range_path = f"{path}.source_ranges[{number}]"
                    _object(selection, range_path, {"start_segment", "end_segment"})
                    for key in ("start_segment", "end_segment"):
                        if not isinstance(selection[key], str) or selection[key] not in segments:
                            raise CandidateError("segment_unknown", f"{range_path}.{key}")
                    first = segments[selection["start_segment"]]
                    last = segments[selection["end_segment"]]
                    if first[0] > last[0]:
                        raise CandidateError("range_reversed", range_path)
                    start, end = first[0], last[1]
                    excerpt = snapshot[start:end]
                    if not excerpt.strip():
                        raise CandidateError("evidence_empty", range_path)
                    if "[听辨不清]" in excerpt:
                        raise CandidateError("range_source_missing", range_path)
                    interval = (start, end)
                    if interval in seen:
                        raise CandidateError("range_duplicate", range_path)
                    seen.add(interval)
                    if interval not in range_ids:
                        evidence_id = f"e{len(evidence) + 1}"
                        range_ids[interval] = evidence_id
                        evidence.append(Evidence(evidence_id, start, end, excerpt))
                    ids.append(range_ids[interval])
                point_count += 1
                points.append(Point(f"p{point_count}", statement, argument, tuple(ids)))
            groups.append(tuple(points))
        if not point_count:
            raise CandidateError("points_empty", "$.core_points")
        knowledge = Knowledge(*identity, *groups, tuple(evidence))
        try:
            validate_knowledge(snapshot, knowledge)
        except (TypeError, ValueError):
            raise CandidateError("domain_invalid", "$.knowledge") from None
        reason = None
    try:
        snapshot_sha = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        canonical = json.dumps(
            {"payload": payload, "snapshot_sha256": snapshot_sha, "contract_version": CONTRACT_VERSION},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        candidate_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise CandidateError("unicode_invalid", "$") from None
    return KnowledgeCandidate(
        qualified, knowledge, reason, candidate_hash, snapshot_sha,
        CONTRACT_VERSION, PARSER_VERSION, VALIDATOR_VERSION,
    )
