"""Recover replay anchors without replacing the completed source transcript."""
from dataclasses import replace
from pathlib import Path
import hashlib
import wave

from knowledge_distiller.primary import PrimaryChunk, StandardAudio
from .local_records import write_record
from .primary_cache import recognize_cached
from .confirmation_preparation import read_wav, digest


def recover_locations(recognizer, audio, recovery, directory):
    root = Path(directory) / 'location-recovery'
    if root.is_symlink() or Path(directory).is_symlink():
        raise OSError('source_audio_unsafe')
    root.mkdir(parents=True, exist_ok=True)
    chunks, failures = [], []
    observed = read_wav(audio.path.parent, audio.path.name)
    source_sha256 = observed['sha256']
    if abs(observed['duration_seconds'] - audio.duration_seconds) > .25:
        raise OSError('source_audio_changed')
    # Source-byte namespace prevents overwriting successful windows of another
    # source. recognize_cached still binds each window to the recognizer.
    windows = root / digest({'source_audio': observed, 'protocol': 'replay-windows-8s-v1'})
    if windows.is_symlink():
        raise OSError('source_audio_unsafe')
    windows.mkdir(exist_ok=True)
    # These windows bound playback, not source completion or semantic judgment.
    with wave.open(str(audio.path), 'rb') as source:
        rate = source.getframerate()
        width = source.getsampwidth() * source.getnchannels()
        for index, start in enumerate(range(0, source.getnframes(), rate * 8)):
            source.setpos(start)
            pcm = source.readframes(rate * 8)
            if not any(pcm):
                continue
            target = windows / f'{index:06d}'
            if target.is_symlink() or (target / 'audio.wav').is_symlink():
                raise OSError('source_audio_unsafe')
            target.mkdir(exist_ok=True)
            path = target / 'audio.wav'
            with wave.open(str(path), 'wb') as output:
                output.setparams(source.getparams())
                output.writeframes(pcm)
            duration = len(pcm) / width / rate
            result = recognize_cached(recognizer, StandardAudio(path, duration, rate,
                source.getnchannels(), source.getsampwidth()), target)
            if result.recovery is None:
                failures.append({'window': index, 'failure': 'location_window_incomplete'})
                continue
            chunks.append(PrimaryChunk(result.recovery.text, start / rate,
                start / rate + duration, result.recovery.language))
    if read_wav(audio.path.parent, audio.path.name) != observed:
        raise OSError('source_audio_changed')
    write_record(root / 'status.json', {'source_text_unchanged': True,
        'source_audio_sha256': source_sha256,
        'source_text_sha256': hashlib.sha256(recovery.text.encode()).hexdigest(),
        'protocol': 'independent-replay-windows-8s-v1',
        'windows': len(chunks), 'failures': failures})
    # Missing windows can contain another occurrence, so do not claim a unique
    # location when the recovery pass is incomplete. Successful windows persist.
    if failures or not chunks:
        return recovery
    return replace(recovery, chunks=tuple(chunks), timeline_status='recovered_windows',
                   timeline_diagnostics=(*recovery.timeline_diagnostics, 'independent_replay_only_asr'))
