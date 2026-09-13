"""Recover replay anchors without replacing the completed source transcript."""
from dataclasses import replace
from pathlib import Path
import wave

from knowledge_distiller.primary import PrimaryChunk, StandardAudio
from .local_records import write_record
from .primary_cache import recognize_cached


def recover_locations(recognizer, audio, recovery, directory):
    root = Path(directory) / 'location-recovery'
    root.mkdir(parents=True, exist_ok=True)
    chunks, failures = [], []
    # These windows bound playback, not source completion or semantic judgment.
    with wave.open(str(audio.path), 'rb') as source:
        rate = source.getframerate()
        width = source.getsampwidth() * source.getnchannels()
        for index, start in enumerate(range(0, source.getnframes(), rate * 8)):
            source.setpos(start)
            pcm = source.readframes(rate * 8)
            if not any(pcm):
                continue
            target = root / f'{index:06d}'
            target.mkdir(exist_ok=True)
            path = target / 'audio.wav'
            with wave.open(str(path), 'wb') as output:
                output.setparams(source.getparams())
                output.writeframes(pcm)
            duration = len(pcm) / width / rate
            result = recognize_cached(recognizer, StandardAudio(path, duration, rate,
                source.getnchannels(), source.getsampwidth()), target)
            if result.recovery is None:
                failures.append({'window': index, 'failure': str(result.failure)})
                continue
            chunks.append(PrimaryChunk(result.recovery.text, start / rate,
                start / rate + duration, result.recovery.language))
    write_record(root / 'status.json', {'source_text_unchanged': True,
        'windows': len(chunks), 'failures': failures})
    # Missing windows can contain another occurrence, so do not claim a unique
    # location when the recovery pass is incomplete. Successful windows persist.
    if failures or not chunks:
        return recovery
    return replace(recovery, chunks=tuple(chunks), timeline_status='recovered_windows',
                   timeline_diagnostics=(*recovery.timeline_diagnostics, 'independent_replay_only_asr'))
