"""Validate captured original-language VTT before choosing it over ASR.

Coverage is a structural eligibility check, not a claim of transcription
accuracy. Raw cues and source identity remain in the material metadata.
"""
import hashlib
import html
import math
import re
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery

_TIME = re.compile(r'(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})')


def _seconds(value):
    match = _TIME.fullmatch(value)
    if not match:
        raise ValueError('invalid_timestamp')
    h, m, s, ms = match.groups()
    if int(m) >= 60 or int(s) >= 60:
        raise ValueError('invalid_timestamp')
    return int(h or 0)*3600 + int(m)*60 + int(s) + int(ms)/1000


def parse_vtt(raw, duration, language):
    if not raw.startswith('WEBVTT') or not math.isfinite(duration) or duration <= 0:
        raise ValueError('invalid_vtt')
    rows = []
    for block in re.split(r'\n\s*\n', raw.replace('\r\n', '\n')):
        lines = block.splitlines()
        if not lines or lines[0].startswith(('NOTE', 'STYLE', 'REGION')):
            continue
        timing = next((i for i,line in enumerate(lines) if '-->' in line), None)
        if timing is None:
            continue
        a, b = lines[timing].split('-->', 1)
        start, end = _seconds(a.strip()), _seconds(b.strip().split()[0])
        text = html.unescape(re.sub(r'<[^>]*>', '', '\n'.join(lines[timing+1:]))).strip()
        if not text or not 0 <= start < end <= duration+1:
            raise ValueError('invalid_cue')
        if rows and (start < rows[-1][0] or start > max(r[1] for r in rows)+5):
            raise ValueError('coverage_gap_or_order')
        rows.append((start, end, text))
    if not rows or rows[0][0] > 5 or max(r[1] for r in rows) < duration-5:
        raise ValueError('incomplete_coverage')
    chunks = []; mapping = []; previous = None; offset = 0
    for index, (start, end, text) in enumerate(rows):
        original = text
        # Only remove a rolling display suffix/prefix while its time overlaps.
        # A repeated statement after a non-overlapping cue is real content.
        if previous and start < previous[1]:
            old = previous[2].splitlines(); new = text.splitlines()
            for size in range(min(len(old),len(new)), 0, -1):
                if old[-size:] == new[:size]:
                    text = '\n'.join(new[size:]); break
        previous = (start,end,original)
        if text:
            chunks.append(PrimaryChunk(text, start, min(end,duration), language))
            mapping.append({'cue': index, 'start': offset, 'end': offset+len(text),
                            'start_seconds': start, 'end_seconds': min(end,duration)})
            offset += len(text)+1
    if not chunks:
        raise ValueError('empty_caption')
    return PrimaryRecovery('\n'.join(c.text for c in chunks), language, tuple(chunks)), mapping


def select_subtitle(captured):
    metadata = captured.metadata
    language = metadata.get('original_language')
    diagnostics = []
    tracks = metadata.get('captions') or []
    if not isinstance(tracks, list):
        return None, {'caption_selection': [{'code': 'invalid_tracks'}]}
    for track in sorted(tracks, key=lambda t: 0 if isinstance(t,dict) and t.get('kind') == 'manual' else 1):
        code = None
        if not isinstance(track, dict):
            diagnostics.append({'code': 'invalid_track'}); continue
        if track.get('source_key') != captured.source_key:
            code = 'source_binding_missing_or_mismatch'
        elif (not language or not isinstance(language,str) or not isinstance(track.get('language'),str)
              or track['language'].split('-')[0] != language.split('-')[0]):
            code = 'original_language_unverified'
        elif track.get('translated') is not False or track.get('kind') not in {'manual','automatic'}:
            code = 'translation_or_provenance_unverified'
        if code:
            diagnostics.append({'language': track.get('language'), 'code': code}); continue
        try:
            recovery, mapping = parse_vtt(track['text'], captured.duration_seconds, track['language'])
        except (KeyError, TypeError, ValueError, IndexError):
            diagnostics.append({'language': track.get('language'), 'code': 'invalid_or_incomplete_vtt'}); continue
        return recovery, {'caption_selection': diagnostics, 'subtitle_baseline': {
            'source_key': captured.source_key, 'language': track['language'], 'kind': track['kind'],
            'raw_sha256': hashlib.sha256(track['text'].encode()).hexdigest(),
            'text': recovery.text, 'cue_map': mapping, 'verification': 'structural_only_not_listened'}}
    return None, {'caption_selection': diagnostics or [{'code': 'no_captions'}]}
