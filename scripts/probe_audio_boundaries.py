"""Opt-in K04 real-word speech probe; isolated output and read-only local models.

Requires a 312s standard.wav and 24s short.wav, both mono 16k signed PCM16.
The supplied fixtures must be worded speech. This probe never downloads models.
Root incompleteness in the recovery test is injected; child ASR remains real.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

from knowledge_distiller.primary import (StandardAudio, QwenPrimaryAdapter,
    QwenRuntimeResult, PrimaryRecognition, PrimaryFailure, PrimaryRecovery)
from knowledge_distiller.v1.primary_cache import recognize_segmented
from knowledge_distiller.v1.asr_recovery import recognize_resumable
from knowledge_distiller.v1.audio_location_recovery import recover_locations


def pcm(path):
    with wave.open(str(path), 'rb') as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 16000)
        return source.readframes(source.getnframes())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class Binding:
    def __init__(self, args):
        self.args = args
        self.calls = 0
        self.model = str(args.model)
        self.cache_identity = ['k04-real-worker-v1', args.engine, str(args.worker)]

    def transcribe(self, path):
        self.calls += 1
        run = self.args.output / 'calls' / f'{self.calls:03d}'
        run.mkdir(parents=True)
        runtime = ('0.3.5' if self.args.engine == 'mlx' else
                   'windows-transformers-5.16.1-cpu-python311-v2')
        model_id, revision = (('Qwen/Qwen3-ASR-1.7B', '7278e1e70fe206f11671096ffdd38061171dd6e5')
            if self.args.engine == 'mlx' else
            ('Qwen/Qwen3-ASR-1.7B-hf', 'bcd2b5b7f32b480ab5790554cfa8347f246a14f3'))
        env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'HF_HOME': str(self.args.output / 'hf-cache'),
               'XDG_CACHE_HOME': str(self.args.output / 'cache'),
               'TMPDIR': str(self.args.output / 'tmp'), 'PYTHONUTF8': '1'}
        command = [str(self.args.python), '-I', '-B', str(self.args.worker), 'transcribe',
                   str(self.args.model), model_id, revision, runtime, str(path), str(run / 'result.json')]
        started = time.time()
        with (run / 'stderr.txt').open('wb') as errors:
            process = subprocess.run(command, env=env, stdout=errors, stderr=errors)
        save(run / 'input.json', {'source': str(path), 'pcm_sha256': hashlib.sha256(pcm(path)).hexdigest(),
             'frames': len(pcm(path)) // 2, 'elapsed_seconds': time.time() - started,
             'returncode': process.returncode, 'engine': self.args.engine})
        if process.returncode:
            raise RuntimeError(f'actual_worker_failed:{run}')
        data = json.loads((run / 'result.json').read_text(encoding='utf-8'))
        return QwenRuntimeResult(**{k: data[k] for k in ('text', 'language', 'finish_reason', 'truncated', 'chunks')})


class ForcedRootIncomplete:
    """Deterministic failure injection only; both recovery children use real ASR."""
    cache_identity = 'k04-root-incomplete-injection-v1'
    def __init__(self, primary, root):
        self.primary, self.root = primary, root
    def recognize(self, audio):
        if audio.path == self.root:
            return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
        return self.primary.recognize(audio)


def main():
    parser = argparse.ArgumentParser()
    for name in ('python', 'worker', 'model', 'fixtures', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--engine', choices=('mlx', 'transformers'), required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'tmp').mkdir()
    save(args.output / 'runtime.json', {'driver_python': sys.version, 'engine': args.engine,
        'worker_sha256': hashlib.sha256(args.worker.read_bytes()).hexdigest(),
        'component_python': subprocess.check_output([str(args.python), '-I', '-B', '-c',
            'import sys; print(sys.version)'], text=True).strip(),
        'evidence_scope': 'self-created worded speech; PCM equality separate from recognition semantics'})
    binding = Binding(args)
    primary = QwenPrimaryAdapter(binding)
    long_path, short_path = args.fixtures / 'standard.wav', args.fixtures / 'short.wav'
    long = StandardAudio(long_path, len(pcm(long_path))/32000)
    short = StandardAudio(short_path, len(pcm(short_path))/32000)
    (args.output / 'long').mkdir()
    result = recognize_segmented(primary, long, args.output / 'long')
    save(args.output / 'long-result.json', asdict(result))
    segments = sorted((args.output / 'long' / 'asr-segments').glob('*/audio.wav'))
    assert b''.join(pcm(p) for p in segments) == pcm(long_path)
    calls = binding.calls
    cached = recognize_segmented(primary, long, args.output / 'long')
    assert binding.calls == calls and cached == result
    save(args.output / 'long-pcm-cache.json', {'contiguous_pcm_equal': True,
         'segments': [len(pcm(p))//2 for p in segments], 'cache_added_calls': 0})
    original = PrimaryRecovery('Retained source text; independent replay anchors only.', 'en', ())
    recovered = recover_locations(primary, short, original, args.output / 'locations')
    save(args.output / 'locations-result.json', asdict(recovered))
    windows = sorted((args.output / 'locations' / 'location-recovery').glob('*/audio.wav'))
    assert b''.join(pcm(p) for p in windows) == pcm(short_path)
    assert recovered.text == original.text
    injected = ForcedRootIncomplete(primary, short_path)
    directory = args.output / 'recovery'
    directory.mkdir()
    first = recognize_resumable(injected, short, directory)
    assert first.failure == PrimaryFailure.INCOMPLETE
    second = recognize_resumable(injected, short, directory)
    save(args.output / 'recovery-result.json', asdict(second))
    children = sorted((directory / 'asr-recovery').glob('*/audio.wav'))
    assert b''.join(pcm(p) for p in children) == pcm(short_path)
    for child in children:
        assert pcm(child.parent / 'decoder.wav')[8000:-8000] == pcm(child)
    save(args.output / 'summary.json', {'actual_worker_calls': binding.calls,
        'long_success': result.recovery is not None,
        'location_status': recovered.timeline_status, 'location_text_unchanged': True,
        'location_pcm_equal': True, 'recovery_pcm_equal': True,
        'recovery_success': second.recovery is not None,
        'recovery_root_failure': 'injected; child recognition real',
        'original_user_discontinuity': 'not_reproduced_by_this_probe'})
    print(json.dumps({'complete': True, 'calls': binding.calls}))


if __name__ == '__main__':
    main()
