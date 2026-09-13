"""Reuse successful primary ASR for the same normalized audio and recognizer."""
from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery, PrimaryRecognition, qualify_primary_timeline as qualify_timeline


def _identity(recognizer, audio):
    with audio.path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    binding = getattr(recognizer, 'binding', None)
    # Public model identity only: never serialize callable credential providers.
    return [digest, audio.duration_seconds, audio.sample_rate_hz, audio.channels, audio.sample_width_bytes, type(recognizer).__module__, type(recognizer).__qualname__,
            getattr(recognizer, 'model', None), getattr(binding, 'model', None),
            getattr(recognizer, 'cache_identity', None)]


def _valid(recovery, audio):
    if not isinstance(recovery.text, str) or not recovery.text.strip() or recovery.truncated is not False or recovery.completed_normally is not True:
        return False
    return True




def _checksum(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def recognize_cached(recognizer, audio, directory):
    identity = _identity(recognizer, audio)
    path = Path(directory) / 'primary-recovery.json'
    if path.is_file() and not path.is_symlink():
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if payload.get('version') == 1 and payload.get('identity') == identity:
                data = payload['recovery']
                if payload.get('checksum') != _checksum(data): raise ValueError('checkpoint corrupt')
                recovery = PrimaryRecovery(**{**data, 'chunks': tuple(PrimaryChunk(**chunk) for chunk in data['chunks']),
                    'timeline_diagnostics':tuple(data.get('timeline_diagnostics',()))})
                if _valid(recovery, audio): return PrimaryRecognition.succeeded(qualify_timeline(recovery,audio))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Invalid checkpoint is recomputed, never promoted to a source fact.
    result = recognizer.recognize(audio)
    if result.recovery is not None and not _valid(result.recovery, audio):
        from knowledge_distiller.primary import PrimaryFailure
        from .local_records import write_record
        recovery = result.recovery
        reason = ('empty_text' if not isinstance(recovery.text,str) or not recovery.text.strip() else
                  'truncated' if recovery.truncated else
                  'abnormal_ending' if not recovery.completed_normally else 'invalid_timeline')
        write_record(path.with_name('primary-failure.json'), {'identity': identity,
            'code': reason, 'recovery': asdict(recovery)})
        return PrimaryRecognition.failed(PrimaryFailure.EMPTY_OUTPUT if reason=='empty_text' else PrimaryFailure.INCOMPLETE)
    if result.recovery is not None:
        from .local_records import write_record
        qualified=qualify_timeline(result.recovery,audio)
        if qualified.timeline_status=='needs_recovery' and result.recovery.chunks:
            write_record(path.with_name('primary-timeline.json'), {'identity':identity,
                'recovery':asdict(result.recovery),'status':'needs_recovery'})
        result=PrimaryRecognition.succeeded(qualified)
        write_record(path, {'version':1, 'identity':identity, 'recovery':asdict(result.recovery),
                            'checksum':_checksum(asdict(result.recovery))})
    return result


def recognize_segmented(recognizer, audio, directory, *, segment_seconds=300):
    """Persist successful long-audio pieces and retry only missing/failed pieces.

    Each checkpoint is bound to actual PCM bytes and recognizer identity. No
    partial text is promoted when any segment fails or claims truncation.
    """
    if audio.duration_seconds <= segment_seconds:
        return recognize_cached(recognizer, audio, directory)
    import wave
    from .local_records import write_record
    from knowledge_distiller.primary import StandardAudio
    root = Path(directory) / 'asr-segments'
    root.mkdir(parents=True, exist_ok=True)
    results = []; failures = []
    with wave.open(str(audio.path), 'rb') as original:
        rate = original.getframerate()
        frames_per_segment = int(segment_seconds * rate)
        if frames_per_segment <= 0:
            raise ValueError('invalid_segment_size')
        for index, frame in enumerate(range(0, original.getnframes(), frames_per_segment)):
            target = root / f'{index:05d}'
            target.mkdir(exist_ok=True)
            path = target / 'audio.wav'
            original.setpos(frame)
            pcm = original.readframes(frames_per_segment)
            count = len(pcm) // (original.getsampwidth()*original.getnchannels())
            with wave.open(str(path), 'wb') as output:
                output.setparams(original.getparams()); output.writeframes(pcm)
            piece = StandardAudio(path, count/rate, rate, original.getnchannels(), original.getsampwidth())
            result = recognize_cached(recognizer, piece, target)
            if result.recovery is None:
                failures.append({'segment': index, 'start_seconds': frame/rate,
                                 'end_seconds': (frame+count)/rate, 'code': str(result.failure)})
            else:
                results.append((frame/rate, result.recovery))
    write_record(root / 'status.json', {'segments_completed': len(results), 'failures': failures})
    if failures:
        from knowledge_distiller.primary import PrimaryFailure
        return PrimaryRecognition.failed(PrimaryFailure(failures[0]['code']))
    chunks = tuple(PrimaryChunk(c.text, c.start_seconds+offset, c.end_seconds+offset, c.language)
                   for offset, recovery in results for c in recovery.chunks)
    merged=PrimaryRecovery(
        '\n'.join(recovery.text for _,recovery in results),
        results[0][1].language if results else None, chunks)
    if any(recovery.timeline_status!='available' for _,recovery in results):
        merged=replace(merged,chunks=(),timeline_status='needs_recovery',
                       timeline_diagnostics=('segment_timeline_incomplete',))
    else:merged=qualify_timeline(merged,audio)
    return PrimaryRecognition.succeeded(merged)
