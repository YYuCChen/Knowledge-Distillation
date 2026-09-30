"""Redacted local diagnostics for concern-audio recovery (BUG-20260917-01).

The card keeps its finalized wording; this record only says which step of the
recovery chain failed, so a real report can be mapped to one branch. Entries
hold a stable stage code, the concern's internal audio name and exception class
names; never source text, candidates, paths or audio.
"""
from datetime import UTC, datetime
import json
import re

STAGES = frozenset({
    'initial_clip_failed',   # First clip at review time; the card offers recovery.
    'timeline_missing',      # No ASR timeline to locate the concern.
    'source_audio_missing',  # Standard audio missing, unreadable or not a file.
    'relocation_failed',     # Replay ASR could not locate the concern.
    'clip_failed',           # FFmpeg did not produce a matching WAV.
    'write_failed',          # The WAV could not be written in place.
    'state_save_failed',     # The recovered clip could not be recorded on the task.
    'serve_failed',          # The HTTP request found no readable clip.
    'playback_failed',       # The browser reported a media error.
    'recovered',
})
KEEP = 20
NAME = 'audio-recovery-diagnostic.json'


def record(runtime_root, item_id, concern_id, stage, *, error=None, **detail):
    if stage not in STAGES:
        raise ValueError(stage)
    from .local_records import write_record
    directory = runtime_root / 'items' / str(item_id)
    path = directory / NAME
    try:
        entries = json.loads(path.read_text(encoding='utf-8'))['entries'] if path.is_file() else []
        if not isinstance(entries, list):
            entries = []
    except (OSError, ValueError, KeyError, TypeError):
        entries = []
    entry = {'at': datetime.now(UTC).isoformat(timespec='seconds'), 'stage': stage,
             'concern_id': concern_id if _concern_name(concern_id) else None}
    if error is not None:
        entry['exception_type'] = type(error).__name__
        clip_stage = getattr(error, 'stage', None)
        if isinstance(clip_stage, str):
            entry['clip_stage'] = clip_stage
    entry.update({key: value for key, value in detail.items() if isinstance(value, (int, str, bool))})
    try:
        directory.mkdir(parents=True, exist_ok=True)
        write_record(path, {'schema': 1, 'entries': (entries + [entry])[-KEEP:]})
    except OSError:
        pass  # Diagnostics never change the outcome the user sees.


def entries(runtime_root, item_id):
    path = runtime_root / 'items' / str(item_id) / NAME
    try:
        return json.loads(path.read_text(encoding='utf-8'))['entries']
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _concern_name(value):
    return isinstance(value, str) and re.fullmatch(
        r'concern-[1-9][0-9]*(?:-[0-9a-f]{32})?\.wav', value) is not None
