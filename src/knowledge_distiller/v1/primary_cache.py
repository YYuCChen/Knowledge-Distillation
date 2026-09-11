"""Reuse successful primary ASR for the same normalized audio and recognizer."""
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery, PrimaryRecognition


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
    previous = 0.0
    for chunk in recovery.chunks:
        if (not isinstance(chunk.text, str) or not chunk.text.strip()
            or not all(isinstance(t, (int,float)) and math.isfinite(t) for t in (chunk.start_seconds, chunk.end_seconds))
            or chunk.start_seconds < previous or chunk.end_seconds <= chunk.start_seconds
            or chunk.end_seconds > audio.duration_seconds + 0.5):
            return False
        previous = chunk.end_seconds
    return bool(recovery.chunks)


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
                recovery = PrimaryRecovery(**{**data, 'chunks': tuple(PrimaryChunk(**chunk) for chunk in data['chunks'])})
                if _valid(recovery, audio): return PrimaryRecognition.succeeded(recovery)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Invalid checkpoint is recomputed, never promoted to a source fact.
    result = recognizer.recognize(audio)
    if result.recovery is not None and _valid(result.recovery, audio):
        temporary = path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'version':1, 'identity':identity, 'recovery':asdict(result.recovery), 'checksum':_checksum(asdict(result.recovery))}, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return result
