"""Windows x64 optional CPU runtime; downloads happen only after explicit POST."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile
import tarfile
from .adapters.python_policy import RUNTIMES, PYTHON_VERSION

MODEL_ID = 'Qwen/Qwen3-ASR-1.7B-hf'
MODEL_REVISION = 'bcd2b5b7f32b480ab5790554cfa8347f246a14f3'
RUNTIME_VERSION = 'windows-transformers-5.16.1-cpu-python311-v2'
PYTHON_URL = RUNTIMES['windows']['url']
PYTHON_SHA256 = RUNTIMES['windows']['sha256']


def copy_vc_runtime(python):
    if getattr(sys, 'frozen', False):
        # The embedded Python ZIP only contains vcruntime, while PyTorch also
        # imports MSVCP. Reuse the verified runtime shipped in the base app.
        for name in ('msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_atomic_wait.dll',
                     'vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_threads.dll'):
            shutil.copyfile(Path(sys._MEIPASS) / name, python / name)


def download(component, url, path, digest):
    if path.is_file():
        with path.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() == digest:
                return
    temporary = path.with_suffix('.part')
    with urllib.request.urlopen(url, timeout=20) as response, temporary.open('wb') as target:
        while chunk := response.read(1024 * 1024):
            if component._stop.is_set():
                raise RuntimeError('interrupted')
            target.write(chunk)
    with temporary.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
            raise RuntimeError('download_checksum_mismatch')
    temporary.replace(path)


def extract(archive, target):
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            if not (target / member.filename).resolve().is_relative_to(target.resolve()):
                raise ValueError('archive_path_escape')
        source.extractall(target)


def install(component):
    from .qwen_component import ASSETS, _atomic_json
    staging = component.root / 'installing'
    staging.mkdir(exist_ok=True)
    component._prepare_staging(staging)
    cache = component.root / 'downloads'
    cache.mkdir(exist_ok=True)
    archive = cache / 'python-3.11.16.tar.gz'
    download(component, PYTHON_URL, archive, PYTHON_SHA256)
    marker = staging/'python-ready'
    if not marker.is_file() or marker.read_text() != PYTHON_SHA256:
        component._reset_staging_python(staging)
        with tarfile.open(archive) as source:
            source.extractall(staging, filter='data')
        marker.write_text(PYTHON_SHA256)
    python = staging / 'python'
    copy_vc_runtime(python)
    component._probe_python(staging)
    component._state('installing_runtime')
    lock = json.loads((ASSETS / 'qwen-windows-lock.json').read_text(encoding='utf-8'))
    if lock.get('python') != PYTHON_VERSION:
        raise RuntimeError('wheel_lock_python_mismatch')
    site = python / 'Lib/site-packages'
    for item in lock['wheels']:
        archive = cache / item['filename']
        download(component, item['url'], archive, item['sha256'])
        extract(archive, site)
        # Wheel .data/purelib and .data/platlib are installed under site-packages.
        for data in site.glob('*.data'):
            for name in ('purelib', 'platlib'):
                if (data / name).is_dir():
                    shutil.copytree(data / name, site, dirs_exist_ok=True)
    component._state('downloading_model')
    component._run([str(python / 'python.exe'), '-I', str(ASSETS / 'qwen_windows_worker.py'),
                    'install', str(staging / 'model'), MODEL_ID, MODEL_REVISION, RUNTIME_VERSION,
                    str(staging / 'install-result.json')], staging)
    files = {p.relative_to(staging / 'model').as_posix(): p.stat().st_size
             for p in (staging / 'model').rglob('*') if p.is_file() and '.cache' not in p.parts}
    if 'config.json' not in files or not any(n.endswith('.safetensors') for n in files):
        raise RuntimeError('incomplete_model')
    component._run([str(python / 'python.exe'), '-I', '-c',
                    'import torch; from transformers import Qwen3ASRForConditionalGeneration, AutoProcessor; import librosa'], staging)
    component._state('verifying_runtime')
    component._verify_runtime(staging)
    _atomic_json(staging / 'component.json', dict(version=RUNTIME_VERSION, revision=MODEL_REVISION, files=files, self_test_passed=True, python=component._probe_python(staging)))
    if component._stop.is_set():
        raise RuntimeError('interrupted')
    component._activate(staging)
