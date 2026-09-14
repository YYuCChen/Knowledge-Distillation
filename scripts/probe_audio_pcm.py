"""Verify standardization and contiguous preview PCM using a real speech fixture.

Target labels/timestamps are controlled test metadata, not ASR word alignment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import wave
from knowledge_distiller.media import VerifiedTemporaryMedia
from knowledge_distiller.primary import FFmpegAudioNormalizer, PrimaryChunk, PrimaryRecovery
from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.v1.confirmation import FFmpegConfirmationClipper, locate_concern_audio


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        return stream.readframes(stream.getnframes())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('fixtures', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    reference = pcm(args.fixtures / 'standard.wav')
    normalized = FFmpegAudioNormalizer().normalize(
        VerifiedTemporaryMedia('self_created', 'k04-worded-speech', args.fixtures / 'source.m4a',
                               len(reference) / 32000), args.output / 'normalization')
    assert normalized.audio is not None
    audio = normalized.audio
    assert pcm(audio.path) == reference
    cases = []
    for start, end, label in [(0, 2, 'head'), (7.5, 8.5, 'window8'),
                             (19.5, 20.5, 'worker20'), (299.5, 300.5, 'segment300'),
                             (audio.duration_seconds - 2, audio.duration_seconds, 'tail')]:
        recovery = PrimaryRecovery(label, 'en', (PrimaryChunk(label, start, end, 'en'),),
                                   timeline_status='available')
        concern = ReviewConcern(0, len(label), label, 'controlled PCM interval', True)
        left, right = locate_concern_audio(audio, recovery, label, concern)
        target = args.output / f'{label}.wav'
        FFmpegConfirmationClipper().clip(audio, recovery, label, concern, target)
        # Production ffmpeg arguments are millisecond rounded. Record this rather
        # than pretend the preview is accurate to a finer timestamp precision.
        begin = round(float(f'{left:.3f}') * 16000)
        count = round(float(f'{right-left:.3f}') * 16000)
        assert pcm(target) == reference[begin*2:(begin+count)*2]
        cases.append({'label': label, 'target_seconds': [start, end], 'preview_seconds': [left,right],
                      'start_frame': begin, 'frames': count, 'pcm_equal': True})
    result = {'source_sha256': hashlib.sha256((args.fixtures/'source.m4a').read_bytes()).hexdigest(),
              'standard_pcm_sha256': hashlib.sha256(reference).hexdigest(),
              'standard_frames': len(reference)//2, 'normalization_pcm_equal': True,
              'cases': cases, 'semantics': 'controlled timestamps; does not establish actual ASR word timing'}
    (args.output/'result.json').write_text(json.dumps(result, indent=2))
    print('PCM normalization and five contiguous previews passed')


if __name__ == '__main__':
    main()
