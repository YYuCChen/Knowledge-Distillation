from __future__ import annotations

import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import StrEnum

from ..faithful_review import (
    FaithfulReviewCandidate,
    ReviewConcern,
    preserves_primary_content,
)
from ..primary import PrimaryRecovery


class SnapshotAcceptanceKind(StrEnum):
    PASSED = "passed"
    PASSED_WITH_UNCERTAINTY = "passed_with_uncertainty"
    NEEDS_LOCAL_RESOLUTION = "needs_local_resolution"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AcceptedSnapshot:
    text: str
    uncertainties: tuple[ReviewConcern, ...]


@dataclass(frozen=True)
class SnapshotAcceptance:
    kind: SnapshotAcceptanceKind
    snapshot: AcceptedSnapshot | None = None

    def __post_init__(self) -> None:
        accepted = self.kind in {
            SnapshotAcceptanceKind.PASSED,
            SnapshotAcceptanceKind.PASSED_WITH_UNCERTAINTY,
        }
        if accepted != (self.snapshot is not None):
            raise ValueError("Snapshot acceptance contains an invalid result")


_LONG_SINGLE_PARAGRAPH_LENGTH = 600
_TARGET_PARAGRAPH_LENGTH = 300
_MIN_PARAGRAPH_LENGTH = 160
_MAX_PARAGRAPH_LENGTH = 460
_SENTENCE_ENDINGS = frozenset("。！？!?")


def accept_snapshot_candidate(
    recovery: PrimaryRecovery,
    candidate: FaithfulReviewCandidate,
) -> SnapshotAcceptance:
    original_text_length = len(candidate.text)
    if not _primary_is_complete(recovery):
        return SnapshotAcceptance(SnapshotAcceptanceKind.REJECTED)
    if not _candidate_structure_is_valid(candidate):
        return SnapshotAcceptance(SnapshotAcceptanceKind.REJECTED)
    if not preserves_primary_content(recovery.text.strip(), candidate.text):
        return SnapshotAcceptance(SnapshotAcceptanceKind.REJECTED)
    if candidate.concerns and not _uncertainties_are_few_and_local(candidate):
        return SnapshotAcceptance(SnapshotAcceptanceKind.REJECTED)
    if any(concern.meaning_may_change for concern in candidate.concerns):
        return SnapshotAcceptance(SnapshotAcceptanceKind.NEEDS_LOCAL_RESOLUTION)

    readable_candidate = make_snapshot_candidate_readable(candidate)
    if readable_candidate is None:
        return SnapshotAcceptance(SnapshotAcceptanceKind.REJECTED)
    if (
        not _primary_is_complete(recovery)
        or not _candidate_structure_is_valid(readable_candidate)
        or not preserves_primary_content(recovery.text.strip(), readable_candidate.text)
        or (
            readable_candidate.concerns
            and not _uncertainties_are_few_and_local(
                readable_candidate,
                text_length=original_text_length,
            )
        )
        or any(
            concern.meaning_may_change
            for concern in readable_candidate.concerns
        )
    ):
        return SnapshotAcceptance(SnapshotAcceptanceKind.REJECTED)

    kind = (
        SnapshotAcceptanceKind.PASSED_WITH_UNCERTAINTY
        if readable_candidate.concerns
        else SnapshotAcceptanceKind.PASSED
    )
    return SnapshotAcceptance(
        kind,
        AcceptedSnapshot(readable_candidate.text, readable_candidate.concerns),
    )


def make_snapshot_candidate_readable(
    candidate: FaithfulReviewCandidate,
) -> FaithfulReviewCandidate | None:
    text = candidate.text
    if (
        len(text) < _LONG_SINGLE_PARAGRAPH_LENGTH
        or re.search(r"\n(?:[ \t]*\n)+", text)
    ):
        return candidate

    break_positions = _paragraph_break_positions(text, candidate.concerns)
    if not break_positions:
        return candidate

    text_parts: list[str] = []
    cursor = 0
    for position in break_positions:
        text_parts.extend((text[cursor:position], "\n\n"))
        cursor = position
    text_parts.append(text[cursor:])
    readable_text = "".join(text_parts)
    if not _only_expected_breaks_were_inserted(
        text,
        readable_text,
        break_positions,
    ):
        return None

    shifted_concerns = tuple(
        ReviewConcern(
            concern.start_offset
            + 2 * bisect_right(break_positions, concern.start_offset),
            concern.end_offset
            + 2 * bisect_left(break_positions, concern.end_offset),
            concern.text,
            concern.reason,
            concern.meaning_may_change,
            concern.candidate_readings,
        )
        for concern in candidate.concerns
    )
    if any(
        readable_text[concern.start_offset : concern.end_offset] != concern.text
        for concern in shifted_concerns
    ):
        return None
    return FaithfulReviewCandidate(readable_text, shifted_concerns)


def _paragraph_break_positions(
    text: str,
    concerns: tuple[ReviewConcern, ...],
) -> tuple[int, ...]:
    sentence_boundaries = tuple(
        index + 1
        for index, character in enumerate(text)
        if character in _SENTENCE_ENDINGS
        and index + 1 < len(text)
        and not any(
            concern.start_offset < index + 1 < concern.end_offset
            for concern in concerns
        )
    )
    breaks: list[int] = []
    paragraph_start = 0
    while len(text) - paragraph_start > _MAX_PARAGRAPH_LENGTH:
        candidates = [
            position
            for position in sentence_boundaries
            if paragraph_start + _MIN_PARAGRAPH_LENGTH <= position
            <= paragraph_start + _MAX_PARAGRAPH_LENGTH
            and len(text) - position >= _MIN_PARAGRAPH_LENGTH
        ]
        if not candidates:
            return tuple(breaks)
        target = paragraph_start + _TARGET_PARAGRAPH_LENGTH
        paragraph_end = min(candidates, key=lambda position: (abs(position - target), position))
        breaks.append(paragraph_end)
        paragraph_start = paragraph_end
    return tuple(breaks)


def _only_expected_breaks_were_inserted(
    original: str,
    readable: str,
    break_positions: tuple[int, ...],
) -> bool:
    restored = readable
    for insertion_index, original_position in reversed(
        tuple(enumerate(break_positions))
    ):
        readable_position = original_position + insertion_index * 2
        if restored[readable_position : readable_position + 2] != "\n\n":
            return False
        restored = (
            restored[:readable_position] + restored[readable_position + 2 :]
        )
    return restored == original


def _primary_is_complete(recovery: PrimaryRecovery) -> bool:
    if (
        not isinstance(recovery.text, str)
        or recovery.truncated
        or not recovery.completed_normally
        or not recovery.text.strip()
    ):
        return False
    if not recovery.chunks:
        return False
    for chunk in recovery.chunks:
        if (
            not isinstance(chunk.start_seconds, (int, float))
            or isinstance(chunk.start_seconds, bool)
            or not isinstance(chunk.end_seconds, (int, float))
            or isinstance(chunk.end_seconds, bool)
            or not math.isfinite(chunk.start_seconds)
            or not math.isfinite(chunk.end_seconds)
            or chunk.start_seconds < 0
            or chunk.end_seconds <= chunk.start_seconds
        ):
            return False
    return True


def _candidate_structure_is_valid(candidate: FaithfulReviewCandidate) -> bool:
    if (
        not isinstance(candidate.text, str)
        or not candidate.text
        or candidate.text != candidate.text.strip()
    ):
        return False
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        and character not in {"\n", "\t"}
        for character in candidate.text
    ):
        return False

    if not isinstance(candidate.concerns, tuple):
        return False
    if any(not isinstance(concern, ReviewConcern) for concern in candidate.concerns):
        return False
    if any(
        not isinstance(concern.start_offset, int)
        or isinstance(concern.start_offset, bool)
        or not isinstance(concern.end_offset, int)
        or isinstance(concern.end_offset, bool)
        for concern in candidate.concerns
    ):
        return False
    ordered_concerns = tuple(
        sorted(candidate.concerns, key=lambda item: item.start_offset)
    )
    if ordered_concerns != candidate.concerns:
        return False

    previous_end = 0
    for concern in candidate.concerns:
        if (
            concern.start_offset < previous_end
            or concern.start_offset < 0
            or concern.end_offset <= concern.start_offset
            or concern.end_offset > len(candidate.text)
            or not isinstance(concern.text, str)
            or not concern.text.strip()
            or candidate.text[concern.start_offset : concern.end_offset]
            != concern.text
            or not isinstance(concern.reason, str)
            or not concern.reason.strip()
            or not isinstance(concern.meaning_may_change, bool)
            or not isinstance(concern.candidate_readings, tuple)
            or any(
                not isinstance(reading, str) or not reading.strip()
                for reading in concern.candidate_readings
            )
        ):
            return False
        previous_end = concern.end_offset
    return True


def _uncertainties_are_few_and_local(
    candidate: FaithfulReviewCandidate,
    *,
    text_length: int | None = None,
) -> bool:
    if len(candidate.concerns) > 3:
        return False
    uncertain_characters = sum(
        concern.end_offset - concern.start_offset for concern in candidate.concerns
    )
    allowed_characters = max(
        1,
        math.ceil((text_length if text_length is not None else len(candidate.text)) * 0.05),
    )
    return uncertain_characters <= allowed_characters
