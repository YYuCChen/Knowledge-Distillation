"""Opt-in worker probe: record input PCM reads without changing their bytes.

Usage: component-python -I -B trace_qwen_pcm_reads.py WORKER transcribe MODEL
       MODEL_ID REVISION RUNTIME INPUT.wav OUTPUT.json
Trace is written alongside OUTPUT.json, including on worker failure.
"""
import hashlib
import json
from pathlib import Path
import runpy
import sys
import wave

worker = sys.argv.pop(1)
source = Path(sys.argv[6]).resolve()
output = Path(sys.argv[-1]).with_suffix('.pcm-reads.json')
reads = []
original_open = wave.open


def traced_open(file, mode=None):
    stream = original_open(file, mode)
    if mode == 'rb' and isinstance(file, (str, Path)) and Path(file).resolve() == source:
        original_read = stream.readframes
        def readframes(count):
            start = stream.tell()
            data = original_read(count)
            width = stream.getnchannels() * stream.getsampwidth()
            reads.append({'start_frame': start, 'requested_frames': count,
                          'frames': len(data) // width,
                          'pcm_sha256': hashlib.sha256(data).hexdigest()})
            return data
        stream.readframes = readframes
    return stream


wave.open = traced_open
try:
    runpy.run_path(worker, run_name='__main__')
finally:
    output.write_text(json.dumps({'source': str(source), 'reads': reads}, indent=2))
