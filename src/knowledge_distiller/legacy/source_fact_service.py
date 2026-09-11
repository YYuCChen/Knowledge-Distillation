from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    establish_source_fact,
    get_task,
    get_task_current_source_fact,
    get_task_material_identity,
    record_task_source_failure,
    record_task_waiting_for_source_condition,
    utc_now,
)
from ..faithful_review import FaithfulReviewCandidate, ReviewConcern
from ..media import VerifiedTemporaryMedia
from ..primary import PrimaryChunk, PrimaryRecovery, StandardAudio
from ..secondary import (
    LocalAudioClipper,
    SecondaryAudio,
    SecondaryAudioError,
    SecondaryFailure,
    SecondaryResolver,
)
from .snapshot_acceptance import (
    SnapshotAcceptanceKind,
    accept_snapshot_candidate,
)


class SourceFactProcessingKind(StrEnum):
    ESTABLISHED = "established"
    NEEDS_LOCAL_RESOLUTION = "needs_local_resolution"
    NEEDS_HUMAN_CONFIRMATION = "needs_human_confirmation"
    WAITING_FOR_EXTERNAL_CONDITION = "waiting_for_external_condition"
    FAILED = "failed"


@dataclass(frozen=True)
class HumanResolutionRequest:
    candidate: FaithfulReviewCandidate
    concern: ReviewConcern
    choices: tuple[str, ...]
    context_before: str
    context_after: str
    audio: SecondaryAudio


@dataclass(frozen=True)
class SourceFactProcessingResult:
    task_id: int
    kind: SourceFactProcessingKind
    source_fact_id: int | None = None
    created: bool = False
    accepted_with_uncertainty: bool = False
    human_resolution: HumanResolutionRequest | None = None


def produce_task_source_fact(
    database_path: Path,
    task_id: int,
    media: VerifiedTemporaryMedia,
    recovery: PrimaryRecovery,
    candidate: FaithfulReviewCandidate,
    *,
    standard_audio: StandardAudio | None = None,
    secondary_resolver: SecondaryResolver | None = None,
    audio_clipper: LocalAudioClipper | None = None,
) -> SourceFactProcessingResult:
    task = get_task(database_path, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} does not exist")
    identity = get_task_material_identity(database_path, task_id)
    if identity is None:
        raise ValueError("Task has no confirmed material identity")
    current_source_fact = get_task_current_source_fact(database_path, task_id)
    if current_source_fact is not None:
        uncertainties = json.loads(current_source_fact["uncertainty_json"])
        return SourceFactProcessingResult(
            task_id,
            SourceFactProcessingKind.ESTABLISHED,
            int(current_source_fact["source_fact_id"]),
            False,
            bool(uncertainties),
        )
    if (
        media.platform != identity.platform
        or media.platform_item_id != identity.platform_item_id
    ):
        raise ValueError("Verified media does not belong to task material")
    if not math.isfinite(media.duration_seconds) or media.duration_seconds <= 0:
        raise ValueError("Verified media duration is invalid")

    acceptance = accept_snapshot_candidate(recovery, candidate)
    while acceptance.kind is SnapshotAcceptanceKind.NEEDS_LOCAL_RESOLUTION:
        path_two = _resolve_next_local_concern(
            candidate,
            recovery,
            standard_audio,
            secondary_resolver,
            audio_clipper,
        )
        if path_two.kind is _LocalResolutionKind.UNAVAILABLE:
            record_task_waiting_for_source_condition(
                database_path,
                task_id,
                "secondary_unavailable",
            )
            return SourceFactProcessingResult(
                task_id,
                SourceFactProcessingKind.WAITING_FOR_EXTERNAL_CONDITION,
            )
        if path_two.kind is _LocalResolutionKind.UNRESOLVED:
            if path_two.human_resolution is not None:
                record_task_waiting_for_source_condition(
                    database_path,
                    task_id,
                    "human_source_confirmation_required",
                )
                return SourceFactProcessingResult(
                    task_id,
                    SourceFactProcessingKind.NEEDS_HUMAN_CONFIRMATION,
                    human_resolution=path_two.human_resolution,
                )
            record_task_source_failure(
                database_path,
                task_id,
                "snapshot_needs_local_resolution",
            )
            return SourceFactProcessingResult(
                task_id,
                SourceFactProcessingKind.NEEDS_LOCAL_RESOLUTION,
            )
        if path_two.kind is _LocalResolutionKind.FAILED:
            record_task_source_failure(
                database_path,
                task_id,
                "secondary_resolution_failed",
            )
            return SourceFactProcessingResult(task_id, SourceFactProcessingKind.FAILED)
        if path_two.candidate is None:
            raise AssertionError("Resolved local concerns have no candidate")
        candidate = path_two.candidate
        acceptance = accept_snapshot_candidate(recovery, candidate)
        if acceptance.kind not in {
            SnapshotAcceptanceKind.PASSED,
            SnapshotAcceptanceKind.PASSED_WITH_UNCERTAINTY,
            SnapshotAcceptanceKind.NEEDS_LOCAL_RESOLUTION,
        }:
            record_task_source_failure(
                database_path,
                task_id,
                "snapshot_rejected_after_secondary",
            )
            return SourceFactProcessingResult(task_id, SourceFactProcessingKind.FAILED)
    if acceptance.kind is SnapshotAcceptanceKind.REJECTED:
        record_task_source_failure(database_path, task_id, "snapshot_rejected")
        return SourceFactProcessingResult(task_id, SourceFactProcessingKind.FAILED)
    if acceptance.snapshot is None:
        raise AssertionError("Accepted snapshot has no content")

    metadata = {
        "platform": identity.platform,
        "platform_item_id": identity.platform_item_id,
        "original_url": identity.original_url,
        "canonical_url": identity.canonical_url,
        "content_type": "video",
        "content_extracted_at": utc_now(),
        "media_duration_seconds": media.duration_seconds,
        "author": (
            {
                "display_name": media.author_name,
                "platform_account_id": media.author_platform_id,
            }
            if media.author_name is not None
            or media.author_platform_id is not None
            else None
        ),
        "source_title": None,
        "original_description": media.original_description,
        "published_at": media.published_at,
        "source_modified_at": None,
    }
    uncertainties = [
        _serialize_uncertainty(concern)
        for concern in acceptance.snapshot.uncertainties
    ]
    creation = establish_source_fact(
        database_path,
        task_id,
        metadata,
        acceptance.snapshot.text,
        uncertainties,
    )
    accepted_with_uncertainty = (
        acceptance.kind is SnapshotAcceptanceKind.PASSED_WITH_UNCERTAINTY
    )
    if not creation.created:
        current_source_fact = get_task_current_source_fact(database_path, task_id)
        if current_source_fact is None:
            raise AssertionError("Reused SourceFact is not current")
        accepted_with_uncertainty = bool(
            json.loads(current_source_fact["uncertainty_json"])
        )
    return SourceFactProcessingResult(
        task_id,
        SourceFactProcessingKind.ESTABLISHED,
        creation.source_fact_id,
        creation.created,
        accepted_with_uncertainty,
    )


def _serialize_uncertainty(concern: ReviewConcern) -> dict[str, object]:
    return {
        "start": concern.start_offset,
        "end": concern.end_offset,
        "text": concern.text,
        "reason": concern.reason,
        "meaning_may_change": concern.meaning_may_change,
        "candidate_readings": list(concern.candidate_readings),
    }


class _LocalResolutionKind(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class _LocalResolution:
    kind: _LocalResolutionKind
    candidate: FaithfulReviewCandidate | None = None
    human_resolution: HumanResolutionRequest | None = None


def _resolve_next_local_concern(
    candidate: FaithfulReviewCandidate,
    recovery: PrimaryRecovery,
    standard_audio: StandardAudio | None,
    resolver: SecondaryResolver | None,
    clipper: LocalAudioClipper | None,
) -> _LocalResolution:
    concern = next(
        (
            concern
            for concern in candidate.concerns
            if concern.meaning_may_change
        ),
        None,
    )
    if concern is None:
        return _LocalResolution(_LocalResolutionKind.FAILED)
    if standard_audio is None or clipper is None:
        return _LocalResolution(_LocalResolutionKind.UNAVAILABLE)

    output_dir = standard_audio.path.parent / "secondary"
    time_range = _locate_concern_audio(
        candidate,
        recovery,
        concern,
        standard_audio,
    )
    if time_range is None:
        return _LocalResolution(_LocalResolutionKind.UNRESOLVED)
    start_seconds, end_seconds = time_range
    try:
        local_audio = clipper.clip(
            standard_audio,
            start_seconds,
            end_seconds,
            output_dir / "concern-1.wav",
        )
    except SecondaryAudioError:
        return _LocalResolution(_LocalResolutionKind.FAILED)
    human_resolution = _build_human_resolution(candidate, concern, local_audio)

    if resolver is None or not resolver.available:
        return (
            _LocalResolution(
                _LocalResolutionKind.UNRESOLVED,
                human_resolution=human_resolution,
            )
            if human_resolution is not None
            else _LocalResolution(_LocalResolutionKind.UNAVAILABLE)
        )

    resolution = resolver.resolve(local_audio)
    if resolution.failure is SecondaryFailure.RUNTIME_UNAVAILABLE:
        return (
            _LocalResolution(
                _LocalResolutionKind.UNRESOLVED,
                human_resolution=human_resolution,
            )
            if human_resolution is not None
            else _LocalResolution(_LocalResolutionKind.UNAVAILABLE)
        )
    if resolution.failure is not None or resolution.transcript is None:
        return (
            _LocalResolution(
                _LocalResolutionKind.UNRESOLVED,
                human_resolution=human_resolution,
            )
            if human_resolution is not None
            else _LocalResolution(_LocalResolutionKind.FAILED)
        )
    reading = _uniquely_supported_reading(resolution.transcript, concern)
    if reading is None or reading == concern.text:
        return _LocalResolution(
            _LocalResolutionKind.UNRESOLVED,
            human_resolution=human_resolution,
        )

    return _LocalResolution(
        _LocalResolutionKind.RESOLVED,
        _apply_local_corrections(candidate, [(concern, reading)]),
    )


def apply_human_resolution(
    request: HumanResolutionRequest,
    choice: str,
) -> FaithfulReviewCandidate:
    if not isinstance(choice, str) or choice not in request.choices:
        raise ValueError("Human resolution choice is not allowed")
    return _apply_local_corrections(
        request.candidate,
        [(request.concern, choice)],
    )


def _build_human_resolution(
    candidate: FaithfulReviewCandidate,
    concern: ReviewConcern,
    audio: SecondaryAudio,
) -> HumanResolutionRequest | None:
    choices = tuple(dict.fromkeys((concern.text, *concern.candidate_readings)))
    if (
        len(choices) < 2
        or len(choices) > 5
        or any(
            not choice.strip()
            or len(choice) > 80
            or any(unicodedata.category(character) in {"Cc", "Cs"} for character in choice)
            for choice in choices
        )
    ):
        return None
    context_start = max(0, concern.start_offset - 36)
    context_end = min(len(candidate.text), concern.end_offset + 36)
    return HumanResolutionRequest(
        candidate,
        concern,
        choices,
        candidate.text[context_start : concern.start_offset],
        candidate.text[concern.end_offset : context_end],
        audio,
    )


def _locate_concern_audio(
    candidate: FaithfulReviewCandidate,
    recovery: PrimaryRecovery,
    concern: ReviewConcern,
    audio: StandardAudio,
) -> tuple[float, float] | None:
    if not _chunk_timeline_is_usable(recovery, audio):
        return None
    matches: list[tuple[PrimaryChunk, int, str]] = []
    for chunk in recovery.chunks:
        offset = chunk.text.find(concern.text)
        while offset >= 0:
            matches.append((chunk, offset, concern.text))
            offset = chunk.text.find(concern.text, offset + 1)
    occurrence = candidate.text[: concern.start_offset].count(concern.text)
    if occurrence < len(matches):
        chunk, offset, reading = matches[occurrence]
    else:
        alternative_matches: list[tuple[PrimaryChunk, int, str]] = []
        for chunk in recovery.chunks:
            for reading in concern.candidate_readings:
                offset = chunk.text.find(reading)
                while offset >= 0:
                    alternative_matches.append((chunk, offset, reading))
                    offset = chunk.text.find(reading, offset + 1)
        if len(alternative_matches) != 1:
            return None
        chunk, offset, reading = alternative_matches[0]
    if not chunk.text or chunk.end_seconds > audio.duration_seconds + 0.25:
        return None
    seconds_per_character = (chunk.end_seconds - chunk.start_seconds) / len(chunk.text)
    target_start = chunk.start_seconds + offset * seconds_per_character
    target_end = chunk.start_seconds + (offset + len(reading)) * seconds_per_character
    return (
        max(0.0, target_start - 8.0),
        min(audio.duration_seconds, target_end + 8.0),
    )


def _chunk_timeline_is_usable(
    recovery: PrimaryRecovery,
    audio: StandardAudio,
) -> bool:
    if not recovery.chunks:
        return False
    previous_end = 0.0
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
            or chunk.end_seconds > audio.duration_seconds + 0.25
            or (
                chunk.start_seconds < previous_end
                and not math.isclose(
                    chunk.start_seconds,
                    previous_end,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            )
        ):
            return False
        previous_end = chunk.end_seconds
    return True


def _uniquely_supported_reading(
    transcript: str,
    concern: ReviewConcern,
) -> str | None:
    readings = tuple(dict.fromkeys((concern.text, *concern.candidate_readings)))
    if len(readings) < 2:
        return None
    normalized_transcript = _normalize_acoustic_text(transcript)
    supported = [
        reading
        for reading in readings
        if _normalize_acoustic_text(reading)
        and _normalize_acoustic_text(reading) in normalized_transcript
    ]
    return supported[0] if len(supported) == 1 else None


def _normalize_acoustic_text(value: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _apply_local_corrections(
    candidate: FaithfulReviewCandidate,
    replacements: list[tuple[ReviewConcern, str]],
) -> FaithfulReviewCandidate:
    replacement_by_start = {
        concern.start_offset: (concern, reading)
        for concern, reading in replacements
    }
    text_parts: list[str] = []
    cursor = 0
    shift = 0
    retained: list[ReviewConcern] = []
    for concern in candidate.concerns:
        replacement = replacement_by_start.get(concern.start_offset)
        if replacement is not None:
            _, reading = replacement
            text_parts.append(candidate.text[cursor : concern.start_offset])
            text_parts.append(reading)
            cursor = concern.end_offset
            shift += len(reading) - (concern.end_offset - concern.start_offset)
            continue
        retained.append(
            ReviewConcern(
                concern.start_offset + shift,
                concern.end_offset + shift,
                concern.text,
                concern.reason,
                concern.meaning_may_change,
                concern.candidate_readings,
            )
        )
    text_parts.append(candidate.text[cursor:])
    return FaithfulReviewCandidate("".join(text_parts), tuple(retained))
