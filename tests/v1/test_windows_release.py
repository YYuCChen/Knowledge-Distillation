import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from knowledge_distiller.v1.file_lock import acquire
from knowledge_distiller.v1.qwen_windows import extract, download


def test_lock_excludes_other_process_and_releases_after_exit(tmp_path):
    path = tmp_path / '中文 空格.lock'
    command = [sys.executable, '-c',
               'from knowledge_distiller.v1.file_lock import acquire; import sys; acquire(sys.argv[1])', str(path)]
    lock = acquire(path)
    try:
        assert subprocess.run(command, capture_output=True).returncode != 0
    finally:
        lock.close()
    assert subprocess.run(command, capture_output=True).returncode == 0


def test_windows_archive_rejects_escape(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr('../escape.txt', 'bad')
    with pytest.raises(ValueError, match='escape'):
        extract(archive, tmp_path / 'runtime')
    assert not (tmp_path / 'escape.txt').exists()


def test_windows_download_rejects_wrong_hash(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import threading
    from knowledge_distiller.v1 import qwen_windows
    monkeypatch.setattr(qwen_windows.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'wrong'))
    path = tmp_path / 'runtime.zip'
    with pytest.raises(RuntimeError, match='checksum'):
        download(SimpleNamespace(_stop=threading.Event()), 'https://example.invalid', path, '0' * 64)
    assert not path.exists()


def test_windows_transcript_reads_utf8(tmp_path, monkeypatch):
    from knowledge_distiller.v1.qwen_component import QwenComponent, ComponentQwenRuntime
    component = QwenComponent(tmp_path)
    monkeypatch.setattr(QwenComponent, 'supported', property(lambda self: True))
    monkeypatch.setattr(component, 'ready', lambda: True)
    def run(command, *args, **kwargs):
        Path(command[-1]).write_text(json.dumps(dict(text='中文转写', language='Chinese',
            finish_reason='eos', truncated=False, chunks=[]), ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(component, '_run', run)
    assert ComponentQwenRuntime(component).transcribe(tmp_path / 'audio.wav').text == '中文转写'
