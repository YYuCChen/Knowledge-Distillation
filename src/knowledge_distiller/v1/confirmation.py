from __future__ import annotations

import math
import os
import re
import threading
import wave
from difflib import Match, SequenceMatcher
from pathlib import Path
from uuid import uuid4

from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.media import CommandRunner, SubprocessCommandRunner
from knowledge_distiller.primary import PrimaryRecovery, StandardAudio


def alignment_blocks(original, candidate):
    """Align words, retaining exact character anchors for replay positions.

    Character matching makes repeated spaces/letters in long English recordings
    quadratic. Whitespace is not an anchor; equal gaps are merged afterwards.
    Chinese characters and punctuation remain individual anchors.
    """
    if original == candidate:
        return [Match(0, 0, len(original)), Match(len(original), len(candidate), 0)]
    pattern = r"[A-Za-z0-9]+|[^\s]"
    left = list(re.finditer(pattern, original))
    right = list(re.finditer(pattern, candidate))
    left_words, right_words = [m.group() for m in left], [m.group() for m in right]
    prefix = 0
    while prefix < min(len(left), len(right)) and left_words[prefix] == right_words[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(left), len(right)) - prefix and left_words[-suffix-1] == right_words[-suffix-1]:
        suffix += 1
    left_end, right_end = len(left) - suffix, len(right) - suffix
    matches = [Match(0, 0, prefix)]
    matches += [Match(b.a + prefix, b.b + prefix, b.size) for b in
                SequenceMatcher(None, left_words[prefix:left_end], right_words[prefix:right_end],
                                autojunk=False).get_matching_blocks()]
    matches.append(Match(left_end, right_end, suffix))
    result = []
    for block in matches:
        for offset in range(block.size):
            a, b = left[block.a + offset], right[block.b + offset]
            current = Match(a.start(), b.start(), len(a.group()))
            if result:
                last = result[-1]
                gap_a = original[last.a + last.size:current.a]
                gap_b = candidate[last.b + last.size:current.b]
                if gap_a == gap_b:
                    result[-1] = Match(last.a, last.b, current.a + current.size - last.a)
                    continue
            result.append(current)
    result.append(Match(len(original), len(candidate), 0))
    return result


class ConfirmationAudioError(RuntimeError):
    pass


class FFmpegConfirmationClipper:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or SubprocessCommandRunner()
        self._alignment_lock = threading.Lock()
        self._alignment_key = None
        self._alignment_blocks = None

    def clip(
        self,
        audio: StandardAudio,
        recovery: PrimaryRecovery,
        candidate_text: str,
        concern: ReviewConcern,
        output_path: Path,
    ) -> Path:
        key = (recovery, candidate_text)
        with self._alignment_lock:
            if self._alignment_key != key:
                original = "".join(chunk.text for chunk in recovery.chunks)
                self._alignment_blocks = alignment_blocks(original, candidate_text)
                self._alignment_key = key
            blocks = self._alignment_blocks
        time_range = locate_concern_audio(
            audio, recovery, candidate_text, concern, blocks=blocks
        )
        if time_range is None:
            raise ConfirmationAudioError("confirmation_audio_unavailable")
        start, end = time_range
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f'.{output_path.stem}-{uuid4().hex}.tmp.wav')
        try:
            result = self.runner.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-i",
                    str(audio.path),
                    "-t",
                    f"{end - start:.3f}",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(temporary),
                ]
            )
            if result.returncode != 0:
                raise ConfirmationAudioError("confirmation_audio_unavailable")
            duration = _wav_duration(temporary)
            if abs(duration - (end - start)) > 0.25:
                raise ConfirmationAudioError("confirmation_audio_unavailable")
            os.replace(temporary, output_path)
            return output_path
        except (OSError, ValueError, wave.Error):
            raise ConfirmationAudioError("confirmation_audio_unavailable") from None
        finally:
            temporary.unlink(missing_ok=True)


def locate_concern_audio(
    audio: StandardAudio,
    recovery: PrimaryRecovery,
    candidate_text: str,
    concern: ReviewConcern,
    *, blocks=None,
) -> tuple[float, float] | None:
    if not _timeline_is_usable(audio, recovery):
        return None
    # Review and human corrections can remove earlier occurrences. Align in the
    # complete context before considering an isolated reading; ordinal counts in
    # the edited text do not identify an occurrence in the original audio.
    aligned = _aligned_chunk_range(audio, recovery, candidate_text, concern, blocks)
    if aligned is not None:
        return aligned
    matches: list[tuple[object, int, str]] = []
    for chunk in recovery.chunks:
        for reading in (concern.text, *concern.candidate_readings):
            offset = chunk.text.find(reading)
            while offset >= 0:
                matches.append((chunk, offset, reading))
                offset = chunk.text.find(reading, offset + 1)

    if len(matches) == 1:
        chunk, offset, reading = matches[0]
    else:
        return None
    if recovery.timeline_status == 'recovered_windows':
        return _preview_window(chunk.start_seconds, chunk.end_seconds, audio.duration_seconds)
    return _preview_window(_estimate_time(chunk, offset), _estimate_time(chunk, offset + len(reading)), audio.duration_seconds)


def _aligned_chunk_range(audio, recovery, candidate_text, concern, blocks=None):
    # Review may remove fillers or add punctuation inside a concern. Anchor both
    # ends in unchanged text, preserving the occurrence even after edits.
    original = "".join(chunk.text for chunk in recovery.chunks)
    if blocks is None:
        blocks = alignment_blocks(original, candidate_text)
    start, end = None, None
    for block in blocks:
        if block.b <= concern.start_offset < block.b + block.size:
            start = block.a + concern.start_offset - block.b
        if block.b < concern.end_offset <= block.b + block.size:
            end = block.a + concern.end_offset - block.b
    # A cleaned phrase may start/end in a replacement (punctuation, filler,
    # corrected reading). Use its surrounding unchanged anchors, never invent
    # word timestamps or map an entirely rewritten transcript.
    if start is None or end is None:
        before = [b for b in blocks if b.size and b.b + b.size <= concern.start_offset]
        after = [b for b in blocks if b.size and b.b >= concern.end_offset]
        if start is None and before:
            start = before[-1].a + before[-1].size
        if end is None and after:
            end = after[0].a
    if start is None or end is None or start >= end:
        return None
    offset = 0
    selected = []
    for chunk in recovery.chunks:
        next_offset = offset + len(chunk.text)
        if offset < end and next_offset > start:
            selected.append((chunk, offset))
        offset = next_offset
    if not selected:
        return None
    first, first_offset = selected[0]
    last, last_offset = selected[-1]
    if recovery.timeline_status == 'recovered_windows':
        # The independent recognizer supplies text for a real bounded window,
        # not word timestamps. Include that entire window; never interpolate.
        if last.end_seconds - first.start_seconds > 10:
            return None
        return _preview_window(first.start_seconds, last.end_seconds, audio.duration_seconds)
    return _preview_window(
        _estimate_time(first, start - first_offset),
        _estimate_time(last, end - last_offset),
        audio.duration_seconds,
    )


def _estimate_time(chunk, offset: int) -> float:
    # ASR supplies segment times, not word times. Estimate only within the
    # context-matched segment; Latin words count as units rather than letters.
    units = list(re.finditer(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*|[^\W_]", chunk.text))
    if not units:
        return chunk.start_seconds
    position = sum(max(0.0, min(1.0, (offset - unit.start()) / (unit.end() - unit.start()))) for unit in units)
    return chunk.start_seconds + (chunk.end_seconds - chunk.start_seconds) * position / len(units)


def _preview_window(start: float, end: float, duration: float) -> tuple[float, float]:
    # Text context length never controls playback length. Keep a ten-second
    # listening window around the concern, shifting at the source boundaries.
    length = min(10.0, duration)
    left = max(0.0, min((start + end) / 2 - length / 2, duration - length))
    return left, left + length


def _timeline_is_usable(audio: StandardAudio, recovery: PrimaryRecovery) -> bool:
    if not recovery.chunks or not audio.path.is_file():
        return False
    previous_end = 0.0
    for chunk in recovery.chunks:
        if (
            not chunk.text
            or not math.isfinite(chunk.start_seconds)
            or not math.isfinite(chunk.end_seconds)
            or (
                chunk.start_seconds < previous_end
                and not math.isclose(
                    chunk.start_seconds,
                    previous_end,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            )
            or chunk.end_seconds <= chunk.start_seconds
            or chunk.end_seconds > audio.duration_seconds + 0.25
        ):
            return False
        previous_end = chunk.end_seconds
    return True


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getnchannels() != 1
            or audio.getframerate() != 16_000
            or audio.getsampwidth() != 2
            or audio.getnframes() <= 0
        ):
            raise ValueError("invalid confirmation audio")
        return audio.getnframes() / audio.getframerate()
