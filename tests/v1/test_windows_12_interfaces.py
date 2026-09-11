"""Windows 2026.09.09.12 boundary regressions; disposable files and processes only."""
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from knowledge_distiller.v1 import bilibili, vault_access

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows native interfaces')


def test_vault_registration_and_open_preserve_note(tmp_path, monkeypatch):
    roaming = tmp_path / 'roaming'
    registry = roaming / 'obsidian/obsidian.json'
    registry.parent.mkdir(parents=True)
    vault = tmp_path / '中文 空格'
    vault.mkdir()
    note = vault / '笔记.md'
    note.write_text('原有笔记', encoding='utf-8')
    registry.write_text(json.dumps({'vaults': {'windows-vault': {'path': str(vault)}}}), encoding='utf-8')
    monkeypatch.setenv('APPDATA', str(roaming))
    assert vault_access.publication_status(str(vault), note.name)['url'].startswith('obsidian://open?vault=windows-vault&')
    opened = []
    monkeypatch.setattr(vault_access.os, 'startfile', lambda path: opened.append(path))
    monkeypatch.setattr(vault_access.subprocess, 'Popen', lambda args, **kwargs: opened.append(args))
    vault_access.open_saved_location(str(vault))
    vault_access.open_saved_location(str(vault), note.name, reveal=True)
    assert opened == [str(vault), ['explorer.exe', '/select,', str(note)]]
    assert note.read_text(encoding='utf-8') == '原有笔记'
    assert vault_access.publication_status(str(vault), '../outside.md')['state'] == 'invalid_path'


def test_bilibili_timeout_terminates_owned_descendants(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    owned = []
    pid_file = tmp_path / 'child.pid'
    child_code = 'import time;time.sleep(60)'
    code = ('import subprocess,sys,time,pathlib;'
            f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}]);'
            f'pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));time.sleep(60)')
    def substitute(command, **kwargs):
        if '--bilibili-worker' in command or (len(command) > 2 and command[1] == '-c'):
            command = [sys.executable, '-c', code]
            process = real_popen(command, **kwargs)
            owned.append(process)
            return process
        return real_popen(command, **kwargs)
    monkeypatch.setattr(bilibili.subprocess, 'Popen', substitute)
    try:
        with pytest.raises(bilibili.BilibiliSourceError, match='bilibili_timeout'):
            bilibili._run_worker({}, 2)
        assert owned[0].poll() is not None
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
        if handle:
            try:
                assert kernel.WaitForSingleObject(handle, 2000) == 0
            finally:
                kernel.CloseHandle(handle)
    finally:
        for process in owned:
            if process.poll() is None:
                subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], capture_output=True)


def test_windowed_python_restores_redirected_worker_pipes():
    entry = Path(__file__).resolve().parents[2] / 'packaging/windows_entry.py'
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    assert pythonw.is_file()
    code = (f'import runpy,sys;entry=runpy.run_path({str(entry)!r});'
            'entry["restore_worker_streams"]();'
            'print("worker:"+sys.stdin.readline().strip(),flush=True)')
    result = subprocess.run([str(pythonw), '-c', code], input='中文 pipe\n',
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'worker:中文 pipe'


@pytest.mark.parametrize('argument,module,function', [
    ('--feishu-worker', 'knowledge_distiller.v1.feishu_socket', 'worker_main'),
    ('--bilibili-worker', 'knowledge_distiller.v1.bilibili', '_worker_main'),
])
def test_frozen_worker_dispatch_precedes_desktop(argument, module, function, monkeypatch):
    import importlib
    entry = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'packaging/windows_entry.py'))
    called = []
    monkeypatch.setattr(sys, 'argv', ['KnowledgeDistiller.exe', argument])
    monkeypatch.setattr(importlib.import_module(module), function, lambda: called.append(argument))
    entry['main']()
    assert called == [argument]
