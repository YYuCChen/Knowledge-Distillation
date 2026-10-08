"""Confirmation JSON contract and bounded read-only WAV verification.

No Store, pipeline, model, credentials, writer or clipper dependency. The JSON
helpers are pure; the explicitly named readback helpers only read item files.
This is a trusted application proof boundary, not protection from same-uid
arbitrary Python/SQLite or uncooperative filesystem mutation.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import wave

from .confirmation_display import english_assistance


PROTOCOL = 'confirmation-presentation-v1'
FAILURE_CODES = frozenset({'confirmation_audio_unavailable', 'review_incomplete',
                           'llm_request_failed', 'llm_request_timeout',
                           'processing_unexpected_failure'})
_TOP_DERIVED = frozenset({'token', 'presentation_preparation', 'audio_alignment'})
_MEMBER_DERIVED = frozenset({'candidate_explanations', 'audio_file',
    'audio_recovery_required', 'audio_range', 'audio_revision',
    'sentence_span', 'candidate_translations', 'candidate_basis'})


class PreparationError(ValueError):
    def __init__(self, code='review_incomplete'):
        super().__init__(code if code in FAILURE_CODES else 'review_incomplete')
        self.code = str(self)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def text_sha256(value):
    if not isinstance(value, str):
        raise PreparationError()
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _positive(value):
    return type(value) is int and value > 0


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def required(pending, source_descriptor):
    """English assistance and unknown/audio origins cannot silently bypass prep."""
    if not isinstance(pending, dict) or not isinstance(pending.get('concerns'), list):
        raise PreparationError()
    if pending.get('kind') == 'image' or not pending['concerns']:
        return False
    return (audio_required(pending, source_descriptor)
        or any(needs_sentence_fields(pending, c) for c in pending['concerns'] if isinstance(c, dict)))


def audio_required(pending, source_descriptor):
    return ('audio_timeline' in pending or source_descriptor.get('source_kind') == 'feishu_voice'
        or any(isinstance(c, dict) and isinstance(c.get('audio_name'), str)
               and c['audio_name'].endswith('.wav') for c in pending['concerns'])
        or source_descriptor.get('source_modality') != 'text')


def _members(pending):
    if not isinstance(pending, dict):
        raise PreparationError()
    snapshot = pending.get('snapshot')
    if not isinstance(snapshot, str) or not snapshot or not isinstance(pending.get('concerns'), list):
        raise PreparationError()
    result = []
    for c in pending['concerns']:
        if (not isinstance(c, dict) or not isinstance(c.get('concern_uid'), str)
                or not c['concern_uid'] or type(c.get('start')) is not int
                or type(c.get('end')) is not int or not 0 <= c['start'] < c['end'] <= len(snapshot)
                or snapshot[c['start']:c['end']] != c.get('text')
                or not isinstance(c.get('audio_name'), str)
                or not isinstance(c.get('source_version_id'), str)
                or not isinstance(c.get('candidates'), list) or not c['candidates']
                or any(not isinstance(v, str) or not v for v in c['candidates'])
                or len(set(c['candidates'])) != len(c['candidates'])
                or c['text'] not in c['candidates']):
            raise PreparationError()
        result.append(c)
    if len({c['concern_uid'] for c in result}) != len(result):
        raise PreparationError()
    return result


def protected_pending(pending):
    """Strip only explicit derived slots; all unknown business fields remain."""
    result = deepcopy(pending)
    for key in _TOP_DERIVED:
        result.pop(key, None)
    for c in result.get('concerns', []):
        for key in _MEMBER_DERIVED:
            c.pop(key, None)
    # Recovery chunks are playback output; source ASR text/duration stay bound.
    timeline = result.get('audio_timeline')
    if isinstance(timeline, dict):
        timeline.pop('chunks', None)
        timeline.pop('timeline_status', None)
    return result


def input_binding(pending, *, source_descriptor):
    _members(pending)
    if (not isinstance(source_descriptor, dict)
            or not {'item_id', 'material_id', 'review_revision', 'source_kind', 'source_key', 'source_sha256'} <= set(source_descriptor)
            or not _positive(source_descriptor.get('item_id'))
            or not _positive(source_descriptor.get('material_id'))
            or not (source_descriptor.get('review_revision') is None
                    or type(source_descriptor['review_revision']) is int
                    and source_descriptor['review_revision'] >= 0)
            or not isinstance(source_descriptor.get('source_kind'), str) or not source_descriptor['source_kind']
            or not isinstance(source_descriptor.get('source_key'), str) or not source_descriptor['source_key']
            or not _hash(source_descriptor.get('source_sha256'))
            or any(not isinstance(pending.get(k), str) or not pending[k]
                   for k in ('review_identity', 'review_round_id', 'source_version_id', 'original_review_hash'))):
        raise PreparationError()
    return digest({'protocol': PROTOCOL, 'source': source_descriptor,
                   'protected_pending': protected_pending(pending)})


def validate_change(before, after):
    _members(before)
    _members(after)
    if protected_pending(before) != protected_pending(after):
        raise PreparationError()
    return True


def output_sha256(pending):
    result = deepcopy(pending)
    result.pop('presentation_preparation', None)
    result.pop('token', None)
    return digest(result)


def member_identity(c):
    return {k: c.get(k) for k in ('concern_uid', 'source_version_id', 'audio_name', 'member_id')}


def manifest_sha256(evidence):
    result = deepcopy(evidence)
    result.pop('manifest_sha256', None)
    return digest(result)


def _key(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns,
            s.st_mode, s.st_nlink)


def _directory_key(s):
    return (s.st_dev, s.st_ino, s.st_mode)


@contextmanager
def _regular(root, relative):
    """Read-only held-fd check; no links, colon, escape or device/FIFO reads."""
    if (not isinstance(relative, str) or not relative or any(v in relative for v in ('\\', ':', '\x00'))
            or relative.startswith('/') or any(p in ('', '.', '..') for p in relative.split('/'))):
        raise PreparationError('confirmation_audio_unavailable')
    root = Path(root).absolute()
    path = root / relative
    parents = list(dict.fromkeys([root, *root.parents, *path.parents[:len(Path(relative).parts) - 1]]))
    try:
        if any(p.is_symlink() or not p.is_dir() for p in parents):
            raise PreparationError('confirmation_audio_unavailable')
        parent_keys = [(p, _directory_key(p.lstat())) for p in parents]
        named = path.lstat()
        if not stat.S_ISREG(named.st_mode):
            raise PreparationError('confirmation_audio_unavailable')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as held:
            opened = os.fstat(held.fileno())
            if not stat.S_ISREG(opened.st_mode) or _key(named) != _key(opened):
                raise PreparationError('confirmation_audio_unavailable')
            yield held
            if (_key(os.fstat(held.fileno())) != _key(opened) or _key(path.lstat()) != _key(opened)
                    or any(p.is_symlink() or not p.is_dir()
                           or _directory_key(p.lstat()) != k for p, k in parent_keys)):
                raise PreparationError('confirmation_audio_unavailable')
    except (OSError, ValueError, EOFError, wave.Error, struct.error) as error:
        if isinstance(error, PreparationError):
            raise
        raise PreparationError('confirmation_audio_unavailable') from None


def read_wav(root, relative):
    """I/O: actual bytes hash and full PCM payload length, without repair."""
    with _regular(root, relative) as held:
        sha = hashlib.sha256()
        size = 0
        while data := held.read(1024 * 1024):
            sha.update(data)
            size += len(data)
        held.seek(0)
        with wave.open(held, 'rb') as wav:
            channels, width, rate, frames = (wav.getnchannels(), wav.getsampwidth(),
                                           wav.getframerate(), wav.getnframes())
            if channels != 1 or width != 2 or rate != 16000 or frames <= 0 or wav.getcomptype() != 'NONE':
                raise PreparationError('confirmation_audio_unavailable')
            count = 0
            while data := wav.readframes(65536):
                count += len(data)
            if count != frames * channels * width:
                raise PreparationError('confirmation_audio_unavailable')
    return {'path': relative, 'sha256': sha.hexdigest(), 'size': size,
            'duration_seconds': frames / rate, 'sample_rate_hz': rate,
            'channels': channels, 'sample_width_bytes': width}


def _range(value, duration):
    if (not isinstance(value, list) or len(value) != 2 or not all(_number(v) for v in value)
            or not 0 <= value[0] < value[1] <= duration or value[1] - value[0] > 10.25):
        raise PreparationError('confirmation_audio_unavailable')
    return value


def _same_pcm(root, clip):
    # The original clipper uses ffmpeg -ss/-t rounded to three decimals. Verify
    # the actual preview is this source segment, not an unrelated valid WAV.
    with _regular(root, 'audio/standard.wav') as original, _regular(root, clip['path']) as part:
        with wave.open(original, 'rb') as source, wave.open(part, 'rb') as preview:
            start = round(round(clip['range_seconds'][0], 3) * source.getframerate())
            source.setpos(start)
            actual = preview.readframes(preview.getnframes())
            if source.readframes(preview.getnframes()) != actual:
                raise PreparationError('confirmation_audio_unavailable')


def _audio_types(value):
    return (isinstance(value, dict) and isinstance(value.get('path'), str)
            and _hash(value.get('sha256')) and _positive(value.get('size'))
            and _number(value.get('duration_seconds')) and value['duration_seconds'] > 0
            and all(type(value.get(k)) is int for k in ('sample_rate_hz', 'channels', 'sample_width_bytes')))


def evidence_context(binding, source_audio, model):
    return digest({'protocol': PROTOCOL, 'input_binding': binding,
                   'source_audio': source_audio, 'model': model})


def needs_sentence_fields(pending, c):
    return english_assistance(pending['snapshot']) or english_assistance(c['text'])


def _sentence_fields(pending, c):
    """Structural full-sentence/option coverage, not translation semantic proof."""
    span = c.get('sentence_span')
    translations = c.get('candidate_translations', {})
    basis = c.get('candidate_basis', {})
    sentence_hash = None
    alternatives_hash = None
    if needs_sentence_fields(pending, c):
        if (not isinstance(span, dict) or set(span) != {'start', 'end'}
                or type(span['start']) is not int or type(span['end']) is not int
                or not 0 <= span['start'] <= c['start'] < c['end'] <= span['end'] <= len(pending['snapshot'])
                or not isinstance(translations, dict) or not isinstance(basis, dict)
                or set(translations) != set(c['candidates']) or set(basis) != set(c['candidates'])):
            raise PreparationError()
        for candidate in c['candidates']:
            meaning, reason = translations[candidate], basis[candidate]
            if (not isinstance(meaning, str) or not meaning.strip()
                    or not re.search('[\u3400-\u9fff]', meaning)
                    or not isinstance(reason, str) or not reason.strip()
                    or reason == c.get('reason') or reason == meaning):
                raise PreparationError()
        sentence = pending['snapshot'][span['start']:span['end']]
        sentence_hash = text_sha256(sentence)
        alternatives_hash = digest({candidate:
            pending['snapshot'][span['start']:c['start']] + candidate +
            pending['snapshot'][c['end']:span['end']] for candidate in c['candidates']})
    return {'sentence_span': deepcopy(span), 'sentence_sha256': sentence_hash,
            'candidate_sentences_sha256': alternatives_hash,
            'candidate_translations': deepcopy(translations),
            'candidate_translations_sha256': digest(translations),
            'candidate_basis': deepcopy(basis), 'candidate_basis_sha256': digest(basis)}


def build_evidence(pending, item_runtime_root, *, source_descriptor, model, ranges):
    """I/O: assemble evidence from current files and producer's real locator ranges.

    Does not locate, cut, explain or repair. The producer must normalize the
    complete pending after forming groups and supply ranges keyed by real UID.
    """
    members = _members(pending)
    if not isinstance(ranges, dict) or set(ranges) != {c['concern_uid'] for c in members}:
        raise PreparationError()
    audio = audio_required(pending, source_descriptor)
    source = read_wav(item_runtime_root, 'audio/standard.wav') if audio else None
    binding = input_binding(pending, source_descriptor=source_descriptor)
    evidence = {'schema': 1, 'protocol': PROTOCOL, 'input_binding': binding,
        'source_audio': source, 'model': deepcopy(model),
        'context_sha256': evidence_context(binding, source, model),
        'review_identity_sha256': digest(pending['review_identity']),
        'snapshot_sha256': text_sha256(pending['snapshot']),
        'output_sha256': output_sha256(pending), 'members': []}
    for ordinal, c in enumerate(members):
        meanings = deepcopy(c.get('candidate_explanations', {}))
        clip = read_wav(item_runtime_root, 'confirmation/' + c['audio_file']) if audio else None
        evidence['members'].append({'identity': member_identity(c), 'ordinal': ordinal,
            'start': c['start'], 'end': c['end'], 'text_sha256': text_sha256(c['text']),
            'candidates_sha256': digest(c['candidates']), 'explanations': meanings,
            'explanations_sha256': digest(meanings),
            **_sentence_fields(pending, c),
            'clip': {**clip, 'range_seconds': deepcopy(ranges[c['concern_uid']])} if audio else None,
            'anchor': {'source_audio_sha256': source['sha256'],
                'snapshot_sha256': evidence['snapshot_sha256'],
                'method': pending.get('audio_alignment'),
                'timeline_sha256': digest(pending.get('audio_timeline'))} if audio else None})
    evidence['manifest_sha256'] = manifest_sha256(evidence)
    validate_evidence(pending, evidence, item_runtime_root, source_descriptor=source_descriptor)
    return evidence


def validate_evidence(pending, evidence, item_runtime_root, *, source_descriptor):
    """I/O validator: source, complete members, output and manifest readback.

    Provenance is cache identity, not current settings: later legal model
    changes do not invalidate previously prepared real user cards.
    """
    try:
        members = _members(pending)
        binding = input_binding(pending, source_descriptor=source_descriptor)
        if (not isinstance(evidence, dict) or type(evidence.get('schema')) is not int
                or evidence['schema'] != 1 or evidence.get('protocol') != PROTOCOL
                or evidence.get('input_binding') != binding
                or evidence.get('manifest_sha256') != manifest_sha256(evidence)
                or evidence.get('output_sha256') != output_sha256(pending)
                or evidence.get('snapshot_sha256') != text_sha256(pending['snapshot'])
                or evidence.get('review_identity_sha256') != digest(pending['review_identity'])
                or evidence.get('context_sha256') != evidence_context(binding, evidence.get('source_audio'), evidence.get('model'))
                or not isinstance(evidence.get('model'), dict)
                or set(evidence['model']) != {'reviewer_type', 'model', 'config_sha256', 'recognizer_sha256'}
                or not isinstance(evidence['model']['reviewer_type'], str)
                or not (evidence['model']['model'] is None or isinstance(evidence['model']['model'], str))
                or not _hash(evidence['model']['config_sha256'])
                or not _hash(evidence['model']['recognizer_sha256'])
                or not isinstance(evidence.get('members'), list) or len(evidence['members']) != len(members)):
            raise PreparationError()
        audio = audio_required(pending, source_descriptor)
        source = read_wav(item_runtime_root, 'audio/standard.wav') if audio else None
        if ((audio and not _audio_types(evidence.get('source_audio')))
                or evidence.get('source_audio') != source):
            raise PreparationError('confirmation_audio_unavailable')
        timeline = pending.get('audio_timeline')
        if audio and (not isinstance(timeline, dict) or not isinstance(timeline.get('text'), str)
                or not isinstance(timeline.get('chunks'), list) or not timeline['chunks']
                or timeline.get('timeline_status') not in {'available', 'recovered_windows', 'unverified'}
                or not _number(timeline.get('duration_seconds'))
                or abs(timeline['duration_seconds'] - source['duration_seconds']) > .25):
            raise PreparationError('confirmation_audio_unavailable')
        previous_end = 0
        for chunk in timeline['chunks'] if audio else []:
            if (not isinstance(chunk, dict) or not isinstance(chunk.get('text'), str) or not chunk['text']
                    or not _number(chunk.get('start_seconds')) or not _number(chunk.get('end_seconds'))
                    or chunk['start_seconds'] < previous_end - 1e-6
                    or not 0 <= chunk['start_seconds'] < chunk['end_seconds'] <= source['duration_seconds'] + .25):
                raise PreparationError('confirmation_audio_unavailable')
            previous_end = chunk['end_seconds']
        for ordinal, (c, m) in enumerate(zip(members, evidence['members'])):
            if (not isinstance(m, dict) or m.get('identity') != member_identity(c)
                    or type(m.get('ordinal')) is not int or m['ordinal'] != ordinal
                    or type(m.get('start')) is not int or type(m.get('end')) is not int
                    or (m.get('start'), m.get('end')) != (c['start'], c['end'])
                    or m.get('text_sha256') != text_sha256(c['text'])
                    or m.get('candidates_sha256') != digest(c['candidates'])
                    or m.get('explanations') != c.get('candidate_explanations', {})
                    or m.get('explanations_sha256') != digest(m.get('explanations'))):
                raise PreparationError()
            meanings = m['explanations']
            if not isinstance(meanings, dict):
                raise PreparationError()
            sentence = _sentence_fields(pending, c)
            if any(m.get(k) != v for k, v in sentence.items()):
                raise PreparationError()
            if not audio:
                if m.get('clip') is not None or m.get('anchor') is not None:
                    raise PreparationError()
                continue
            clip = m.get('clip')
            if (not _audio_types(clip)
                    or not re.fullmatch(r'confirmation/concern-[1-9][0-9]*(?:-[0-9a-f]{32})?\.wav', clip['path'])
                    or c.get('audio_file') != clip['path'].split('/')[-1]):
                raise PreparationError('confirmation_audio_unavailable')
            span = _range(clip.get('range_seconds'), source['duration_seconds'])
            if abs((span[1] - span[0]) - min(10.0, source['duration_seconds'])) > .002:
                raise PreparationError('confirmation_audio_unavailable')
            observed = read_wav(item_runtime_root, clip['path'])
            if (clip != {**observed, 'range_seconds': span}
                    or abs(observed['duration_seconds'] - (span[1] - span[0])) > .25
                    or m.get('anchor') != {'source_audio_sha256': source['sha256'],
                        'snapshot_sha256': text_sha256(pending['snapshot']),
                        'method': pending.get('audio_alignment'), 'timeline_sha256': digest(timeline)}
                    or pending.get('audio_alignment') not in {'asr_chunk_v2', 'local_preview_10s_v3'}):
                raise PreparationError('confirmation_audio_unavailable')
            _same_pcm(item_runtime_root, clip)
        if audio and read_wav(item_runtime_root, 'audio/standard.wav') != source:
            raise PreparationError('confirmation_audio_unavailable')
        return True
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        if isinstance(error, PreparationError):
            raise
        raise PreparationError() from None


def presentation_state(pending, item_runtime_root, *, source_descriptor):
    """Read-only classification using DB evidence, never private cache status."""
    try:
        if not required(pending, source_descriptor):
            return {'outcome': 'not-required', 'code': None}
        marker = pending['presentation_preparation']
        if (marker['protocol'] != PROTOCOL or not _positive(marker['attempt'])
                or marker['input_binding'] != input_binding(pending, source_descriptor=source_descriptor)):
            raise PreparationError()
        if marker['outcome'] == 'failed' and marker.get('code') in FAILURE_CODES:
            return {'outcome': 'failed', 'code': marker['code']}
        if marker['outcome'] != 'prepared':
            raise PreparationError()
        validate_evidence(pending, marker['evidence'], item_runtime_root, source_descriptor=source_descriptor)
        return {'outcome': 'prepared', 'code': None}
    except (KeyError, TypeError, ValueError) as error:
        return {'outcome': 'not-ready', 'code': error.code if isinstance(error, PreparationError) else 'review_incomplete'}


def ready(pending, item_runtime_root, *, source_descriptor):
    return presentation_state(pending, item_runtime_root, source_descriptor=source_descriptor)['outcome'] in {'prepared', 'not-required'}


def prepared_audio(pending, item_runtime_root, member_uid, *, source_descriptor):
    """Return verified bytes, so serving does not reopen an unchecked path."""
    if not required(pending, source_descriptor) or not ready(pending, item_runtime_root,
                                                           source_descriptor=source_descriptor):
        return None
    evidence = pending['presentation_preparation']['evidence']
    matches = [m for m in evidence['members'] if m['identity']['concern_uid'] == member_uid]
    if len(matches) != 1:
        return None
    clip = matches[0]['clip']
    if clip is None:
        return None
    with _regular(item_runtime_root, clip['path']) as held:
        data = held.read()
    if hashlib.sha256(data).hexdigest() != clip['sha256']:
        return None
    return data
