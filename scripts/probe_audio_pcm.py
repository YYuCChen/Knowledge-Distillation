"""Verify standardization and contiguous preview PCM using a real speech fixture.

Target labels/timestamps are controlled test metadata, not ASR word alignment.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
import wave
from knowledge_distiller.media import VerifiedTemporaryMedia
from knowledge_distiller.primary import FFmpegAudioNormalizer, PrimaryChunk, PrimaryRecovery
from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.v1.confirmation import FFmpegConfirmationClipper, locate_concern_audio


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        return stream.readframes(stream.getnframes())


def check_pcm(path, reference):
    with wave.open(str(path), 'rb') as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (1, 2, 16000)
        assert stream.getnframes() * 2 == len(reference)
        assert stream.readframes(stream.getnframes()) == reference


def check_route(source, reference, output):
    normalized = FFmpegAudioNormalizer().normalize(
        VerifiedTemporaryMedia('self_created', 'k04-worded-speech', source,
                               len(reference) / 32000), output / 'normalization')
    assert normalized.audio is not None
    audio = normalized.audio
    check_pcm(audio.path, reference)
    cases = []
    for start, end, label in [(0, 2, 'head'), (7.5, 8.5, 'window8'),
                             (19.5, 20.5, 'worker20'), (299.5, 300.5, 'segment300'),
                             (audio.duration_seconds - 2, audio.duration_seconds, 'tail')]:
        recovery = PrimaryRecovery(label, 'en', (PrimaryChunk(label, start, end, 'en'),),
                                   timeline_status='available')
        concern = ReviewConcern(0, len(label), label, 'controlled PCM interval', True)
        left, right = locate_concern_audio(audio, recovery, label, concern)
        target = output / f'{label}.wav'
        FFmpegConfirmationClipper().clip(audio, recovery, label, concern, target)
        # Production ffmpeg arguments are millisecond rounded. Record this rather
        # than pretend the preview is accurate to a finer timestamp precision.
        begin = round(float(f'{left:.3f}') * 16000)
        count = round(float(f'{right-left:.3f}') * 16000)
        check_pcm(target, reference[begin*2:(begin+count)*2])
        cases.append({'label': label, 'target_seconds': [start, end], 'preview_seconds': [left,right],
                      'start_frame': begin, 'frames': count, 'pcm_equal': True})
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('fixtures', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    reference = pcm(args.fixtures / 'standard.wav')
    check_pcm(args.fixtures / 'standard.wav', reference)
    assert len(reference) == 312 * 32000

    # Bind an independent CLI decoding oracle before invoking the product.
    # Different decoder builds need not round AAC samples identically.
    executable = Path(shutil.which('ffmpeg') or '').resolve()
    assert executable.is_file(), 'ffmpeg executable unavailable'
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    version = subprocess.check_output([str(executable), '-version'], text=True, timeout=30)
    baseline = args.output / 'compressed-baseline.wav'
    command = [str(executable), '-nostdin', '-v', 'error', '-y',
               '-i', str(args.fixtures / 'source.m4a'), '-map', '0:a:0', '-vn',
               '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(baseline)]
    decoded_run = subprocess.run(command, capture_output=True, timeout=120)
    (args.output / 'decoder-stdout.log').write_bytes(decoded_run.stdout)
    (args.output / 'decoder-stderr.log').write_bytes(decoded_run.stderr)
    decoded_run.check_returncode()
    decoded = pcm(baseline)
    check_pcm(baseline, decoded)
    assert len(decoded) == len(reference)

    # Preserve the canonical PCM identity and the collector's existing outputs.
    cases = check_route(args.fixtures / 'standard.wav', reference, args.output)
    compressed_output = args.output / 'compressed'
    compressed_output.mkdir()
    compressed_cases = check_route(args.fixtures / 'source.m4a', decoded, compressed_output)
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == executable_sha256
    compressed_files = {str(path.relative_to(args.output)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in [baseline, *sorted(compressed_output.rglob('*.wav'))]}
    result = {'source_sha256': hashlib.sha256((args.fixtures/'source.m4a').read_bytes()).hexdigest(),
              'standard_pcm_sha256': hashlib.sha256(reference).hexdigest(),
              'standard_frames': len(reference)//2, 'normalization_pcm_equal': True,
              'normalization_input': 'standard.wav', 'cases': cases,
              'compressed': {'input': 'source.m4a',
                             'source_sha256': hashlib.sha256((args.fixtures/'source.m4a').read_bytes()).hexdigest(),
                             'format': {'rate': 16000, 'channels': 1, 'sample_width': 2},
                             'decoder_executable': str(executable), 'decoder_exit_code': decoded_run.returncode,
                             'decoder_sha256': executable_sha256, 'decoder_version': version,
                             'baseline_command': command, 'baseline_frames': len(decoded)//2,
                             'baseline_pcm_sha256': hashlib.sha256(decoded).hexdigest(),
                             'matches_canonical_fixture_pcm': decoded == reference,
                             'normalization_pcm_equal': True, 'cases': compressed_cases,
                             'files_sha256': compressed_files},
              'semantics': 'fixed decoder CLI baseline and exact PCM slices; controlled timestamps; does not establish decoder correctness or actual ASR word timing'}
    (args.output/'result.json').write_text(json.dumps(result, indent=2))
    print('Lossless PCM and fixed-decoder normalization; ten contiguous previews passed')


if __name__ == '__main__':
    main()
