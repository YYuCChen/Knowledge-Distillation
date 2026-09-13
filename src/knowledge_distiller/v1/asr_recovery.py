"""Subdivide persistently incomplete ASR without accepting truncated output."""
from dataclasses import asdict, replace
import array
import json
import os
from pathlib import Path
import wave
import sys

from knowledge_distiller.primary import PrimaryFailure, PrimaryRecognition, StandardAudio
from .local_records import write_record
from .windows_platform import filesystem_path, is_link_or_reparse

# A retry may perform bounded extra inference. Successful children do not spend
# this budget, so later retries can always reach the remaining failed leaves.
MAX_RECOVERY_CALLS = 16
MIN_CHILD_SECONDS = 1
DECODER_PADDING_SECONDS = 0.25


def _record(path, identity):
    from .primary_cache import _checksum
    if is_link_or_reparse(path):
        raise OSError('asr_recovery_checkpoint_link')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            return {}
        checksum = value.pop('checksum')
        if (value.get('version') == 1 and value.get('identity') == identity
                and checksum == _checksum(value)):
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def _save(path, value):
    from .primary_cache import _checksum
    write_record(path, {**value, 'checksum': _checksum(value)})


def _cut(original):
    """Prefer a low-energy gap near the middle; never omit PCM frames.

    Quantization noise makes a spoken pause rarely all-zero. Energy only
    chooses a boundary; it does not certify transcription completeness.
    """
    count, rate = original.getnframes(), original.getframerate()
    minimum = max(1, int(rate * MIN_CHILD_SECONDS))
    if count < 2 * minimum:
        return None
    middle = count // 2
    if original.getsampwidth() == 2:
        window = max(1, rate // 50)
        candidates = range(max(minimum, count // 4),
                           min(count - minimum, 3 * count // 4) + 1, window)
        energies = []
        for frame in candidates:
            original.setpos(max(0, frame - window // 2))
            pcm = original.readframes(window)
            if len(pcm) != window * 2 * original.getnchannels():
                continue
            samples = array.array('h', pcm)
            if sys.byteorder != 'little':
                samples.byteswap()
            energies.append((frame, sum(x*x for x in samples) / len(samples)))
        if energies:
            # A 40 dB drop in RMS relative to the loudest candidate window.
            threshold = max(energy for _, energy in energies) * 0.0001
            quiet = [frame for frame, energy in energies if energy <= threshold]
            if quiet:
                return min(quiet, key=lambda frame: abs(frame - middle))
    return middle


def recognize_resumable(recognizer, audio, directory):
    directory = filesystem_path(directory)
    if is_link_or_reparse(directory):
        raise OSError('asr_recovery_directory_link')
    return _recognize(recognizer, audio, directory, [MAX_RECOVERY_CALLS], False)


def _decode_child(recognizer, audio, directory):
    """Give the decoder boundary context without duplicating source speech."""
    from .primary_cache import recognize_cached, qualify_timeline
    padded_path = directory / 'decoder.wav'
    decoder_cache = directory / 'decoder-cache'
    if is_link_or_reparse(padded_path) or is_link_or_reparse(decoder_cache):
        raise OSError('asr_decoder_path_link')
    decoder_cache.mkdir(exist_ok=True)
    with wave.open(str(audio.path), 'rb') as original:
        rate = original.getframerate()
        padding_frames = int(rate * DECODER_PADDING_SECONDS)
        pad = bytes(padding_frames * original.getnchannels() * original.getsampwidth())
        pcm = original.readframes(original.getnframes())
        with wave.open(str(padded_path), 'wb') as output:
            output.setparams(original.getparams())
            output.writeframes(pad + pcm + pad)
    padding = padding_frames / rate
    result = recognize_cached(recognizer, replace(audio, path=padded_path,
        duration_seconds=audio.duration_seconds + 2 * padding), decoder_cache)
    if result.recovery is None:
        return result
    def source_time(t):
        return max(0.0, min(audio.duration_seconds, t - padding))
    recovery = replace(result.recovery, chunks=tuple(
        replace(c, start_seconds=source_time(c.start_seconds),
                end_seconds=source_time(c.end_seconds)) for c in result.recovery.chunks))
    return PrimaryRecognition.succeeded(qualify_timeline(recovery, audio))


def _recognize(recognizer, audio, directory, budget, subdivision):
    from .primary_cache import (_identity, _read_recovery, _checksum,
                                recognize_cached, _merge_recognitions)
    identity = [*_identity(recognizer, audio), 'subdivision-low-energy-pad-v3']
    cache = directory / 'primary-subdivision.json'
    cached = _read_recovery(cache, identity, audio)
    if cached is not None:
        return cached
    plan_path = directory / 'asr-recovery-plan.json'
    plan = _record(plan_path, identity)
    if not plan:
        if budget[0] <= 0:
            return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
        budget[0] -= 1
        result = (_decode_child(recognizer, audio, directory) if subdivision
                  else recognize_cached(recognizer, audio, directory))
        if result.failure != PrimaryFailure.INCOMPLETE:
            if result.recovery is not None:
                data = asdict(result.recovery)
                write_record(cache, {'version': 1, 'identity': identity,
                    'recovery': data, 'checksum': _checksum(data)})
            return result
        previous = _record(directory / 'asr-incomplete-attempt.json', identity)
        _save(directory / 'asr-incomplete-attempt.json',
              {'version': 1, 'identity': identity, 'incomplete': True})
        # Give a transient incomplete response one ordinary retry. Persistent
        # failures then subdivide; children may immediately subdivide further.
        if not subdivision and not previous.get('incomplete'):
            return result
        with wave.open(str(audio.path), 'rb') as original:
            cut = _cut(original)
            if cut is None:
                return result
            plan = {'version': 1, 'identity': identity, 'split_frame': cut,
                    'frames': original.getnframes(), 'rate': original.getframerate()}
        _save(plan_path, plan)

    results = []
    failures = []
    with wave.open(str(audio.path), 'rb') as original:
        count, rate = original.getnframes(), original.getframerate()
        cut = plan.get('split_frame')
        if (type(cut) is not int or not 0 < cut < count
                or plan.get('frames') != count or plan.get('rate') != rate):
            raise OSError('asr_recovery_plan_invalid')
        for index, (start, end) in enumerate(((0, cut), (cut, count))):
            children = directory / 'asr-recovery'
            child = children / f'{index:05d}'
            if is_link_or_reparse(children) or is_link_or_reparse(child):
                raise OSError('asr_recovery_directory_link')
            child.mkdir(parents=True, exist_ok=True)
            path = child / 'audio.wav'
            temporary = child / 'audio.tmp'
            if is_link_or_reparse(path) or is_link_or_reparse(temporary):
                raise OSError('asr_recovery_audio_link')
            original.setpos(start)
            pcm = original.readframes(end - start)
            if len(pcm) != (end - start) * original.getsampwidth() * original.getnchannels():
                raise OSError('asr_recovery_audio_truncated')
            with wave.open(str(temporary), 'wb') as output:
                output.setparams(original.getparams())
                output.writeframes(pcm)
            os.replace(temporary, path)
            piece = StandardAudio(path, (end - start) / rate, rate,
                                  original.getnchannels(), original.getsampwidth())
            result = _recognize(recognizer, piece, child, budget, True)
            if result.recovery is None:
                failures.append({'start_frame': start, 'end_frame': end,
                                 'failure': str(result.failure)})
            else:
                results.append((start / rate, result.recovery))
    write_record(directory / 'asr-recovery-status.json',
                 {'identity': identity, 'failures': failures,
                  'remaining_calls': budget[0], 'completed_children': len(results)})
    if failures:
        return PrimaryRecognition.failed(PrimaryFailure(failures[0]['failure']))
    result = _merge_recognitions(results, audio)
    data = asdict(result.recovery)
    write_record(cache, {'version': 1, 'identity': identity, 'recovery': data,
                         'checksum': _checksum(data)})
    return result
