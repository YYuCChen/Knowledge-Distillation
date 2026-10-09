"""Real process lifecycle tests using only disposable application data."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import pytest


windows_only = pytest.mark.skipif(sys.platform != 'win32', reason='Windows process lifecycle')
PROJECT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)


def _wait_file(path, process, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return path.read_text(encoding='utf-8')
        if process.poll() is not None:
            raise AssertionError(f'owned test process exited early: {process.returncode}')
        time.sleep(.05)
    raise AssertionError('owned process did not publish state')


@windows_only
def test_real_app_single_instance_four_pages_and_lock_release(tmp_path):
    data = tmp_path / '中文 用户数据'
    marker = tmp_path / 'worker-starts.txt'
    script = (
        'import pathlib,sys; '
        'from knowledge_distiller.v1.worker import SingleWorker; '
        'from knowledge_distiller.v1 import windows_job; '
        'original=SingleWorker.start; '
        'SingleWorker.start=lambda self: (windows_job._job or sys.exit("worker started before job"),pathlib.Path(sys.argv[1]).open("a",encoding="utf-8").write("start\\n"),original(self))[-1]; '
        'from knowledge_distiller.v1.windows_app import main; '
        'raise SystemExit(main(sys.argv[2:]))'
    )
    command = [str(PYTHON), '-c', script, str(marker), '--data-dir', str(data),
               '--port', '0', '--no-open', '--smoke-seconds', '15']
    with (tmp_path / 'app-process.log').open('wb') as log:
        first = subprocess.Popen(command, stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            state_path = data / '.desktop-instance.json'
            state = json.loads(_wait_file(state_path, first))
            # Windows venv python.exe can be a redirector owning a real Python child.
            assert isinstance(state['pid'], int) and state['pid'] > 0
            base = f"http://127.0.0.1:{state['port']}"
            for route in ('/', '/topics', '/insights', '/settings'):
                with urllib.request.urlopen(base + route, timeout=5) as response:
                    assert response.status == 200
            second = subprocess.run(command, stdout=log, stderr=log, timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            assert second.returncode == 0
            assert first.poll() is None
            assert json.loads(state_path.read_text(encoding='utf-8')) == state
            assert marker.read_text(encoding='utf-8').splitlines() == ['start']
            assert first.wait(timeout=25) == 0
            assert not state_path.exists()
            from knowledge_distiller.v1.file_lock import acquire
            acquire(data / '.instance.lock').close()
        finally:
            if first.poll() is None:
                first.terminate()
                first.wait(timeout=10)


@windows_only
def test_forced_owned_parent_exit_kills_child_but_not_sibling(tmp_path):
    pid_path = tmp_path / 'owned-child.pid'
    script = (
        'import pathlib,subprocess,sys,time; '
        'from knowledge_distiller.v1.windows_job import own_children; '
        'own_children(); '
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(90)"]); '
        'pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding="utf-8"); '
        'time.sleep(90)'
    )
    sibling = subprocess.Popen([str(PYTHON), '-c', 'import time; time.sleep(90)'],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    parent = subprocess.Popen([str(PYTHON), '-c', script, str(pid_path)],
                              creationflags=subprocess.CREATE_NO_WINDOW)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    child_handle = None
    try:
        child_pid = int(_wait_file(pid_path, parent))
        child_handle = kernel.OpenProcess(0x100000, False, child_pid)
        assert child_handle
        assert kernel.WaitForSingleObject(child_handle, 0) == 258
        parent.kill()  # Only this Popen-owned test parent; no process enumeration.
        parent.wait(timeout=10)
        assert kernel.WaitForSingleObject(child_handle, 10000) == 0
        assert sibling.poll() is None
    finally:
        if child_handle:
            kernel.CloseHandle(child_handle)
        for process in (parent, sibling):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_windows_serve_saved_address_explicit_zero_and_boundary(tmp_path, monkeypatch):
    from flask import Flask
    from knowledge_distiller.v1 import windows_app
    from knowledge_distiller.v1.local_address import LocalAddress, save
    from knowledge_distiller.v1.app import AppPaths
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        chosen = probe.getsockname()[1]
    save(tmp_path, LocalAddress('test-desktop', chosen))
    monkeypatch.setattr(windows_app, 'create_application', lambda paths, **kwargs: Flask(__name__))
    for requested in (None, 0):
        app, server, thread = windows_app.serve(AppPaths(tmp_path), requested)
        try:
            if requested is None:
                assert server.server_port == chosen
            assert server.server_port > 0
            client = app.test_client()
            base = f'http://test-desktop.localhost:{server.server_port}'
            assert client.get('/', base_url=base).status_code == 404
            assert client.get('/', base_url='http://evil.example:57740').status_code == 403
            assert client.post('/', base_url=base, headers={'Origin': 'http://evil.example'}).status_code == 403
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_restart_command_reloads_saved_port_and_preserves_isolation(tmp_path):
    from argparse import Namespace
    from knowledge_distiller.v1.windows_app import restart_command
    command = restart_command(Namespace(no_open=True, smoke_seconds=12, port=0), tmp_path)
    assert '--port' not in command
    assert command[command.index('--data-dir') + 1] == str(tmp_path)
    assert '--no-open' in command
    assert command[command.index('--smoke-seconds') + 1] == '12'
    assert '--restart-wait-pid' in command


@windows_only
def test_settings_restart_survives_old_job_and_uses_saved_address(tmp_path):
    import socket
    import urllib.parse
    data = tmp_path / 'restart-data'
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        chosen = probe.getsockname()[1]
    command = [str(PYTHON), '-m', 'knowledge_distiller.v1.windows_app',
               '--data-dir', str(data), '--port', '0', '--no-open', '--smoke-seconds', '15']
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    restarted_handle = None
    with (tmp_path / 'restart-process.log').open('wb') as output:
        parent = subprocess.Popen(command, stdout=output, stderr=output,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            state_path = data / '.desktop-instance.json'
            initial = json.loads(_wait_file(state_path, parent))
            form = urllib.parse.urlencode({'name': 'restart-test', 'port': chosen}).encode()
            request = urllib.request.Request(f"http://127.0.0.1:{initial['port']}/settings/local-address", data=form)
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200
                assert f'restart-test.localhost:{chosen}'.encode() in response.read()
            assert parent.wait(timeout=25) == 0
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                try:
                    state = json.loads(state_path.read_text(encoding='utf-8'))
                    if state['pid'] != initial['pid']:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            else:
                raise AssertionError('Restarted application did not publish its new state')
            restarted_handle = kernel.OpenProcess(0x100001, False, state['pid'])
            assert restarted_handle
            assert state['port'] == chosen
            assert kernel.WaitForSingleObject(restarted_handle, 0) == 258
            request = urllib.request.Request(f'http://127.0.0.1:{chosen}/settings',
                                             headers={'Host': f'restart-test.localhost:{chosen}'})
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
            assert kernel.WaitForSingleObject(restarted_handle, 25000) == 0
            assert not state_path.exists()
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=10)
            if restarted_handle:
                if kernel.WaitForSingleObject(restarted_handle, 0) == 258:
                    kernel.TerminateProcess(restarted_handle, 1)
                    kernel.WaitForSingleObject(restarted_handle, 10000)
                kernel.CloseHandle(restarted_handle)


@pytest.mark.parametrize('failure', ['busy', 'spawn', 'save', None])
def test_restart_reservation_rollback_and_duplicate_guard(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from knowledge_distiller.v1 import windows_app
    from knowledge_distiller.v1.local_address import LocalAddress, LocalAddressError, load, save
    old, new = LocalAddress(), LocalAddress('next-address', 57741)
    save(tmp_path, old)
    worker = SimpleNamespace(reserve_for_update=Mock(return_value=True), release_update=Mock())
    app = SimpleNamespace(config={'KNOWLEDGE_DISTILLER_WORKER': worker},
                          extensions={'qwen_component': SimpleNamespace(status=lambda: {'busy': failure == 'busy'})})
    spawned = Mock(side_effect=OSError('spawn failed') if failure == 'spawn' else None)
    monkeypatch.setattr(windows_app.subprocess, 'Popen', spawned)
    monkeypatch.setattr(windows_app.subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0x01000000, raising=False)
    monkeypatch.setattr(windows_app.subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    timer = Mock()
    monkeypatch.setattr(windows_app.threading, 'Timer', Mock(return_value=timer))
    if failure == 'save':
        monkeypatch.setattr(windows_app, 'save', Mock(side_effect=OSError('save failed')))
    windows_app.install_restart(app, SimpleNamespace(no_open=True, smoke_seconds=2),
                                tmp_path, tmp_path / 'restart.log', Mock())
    callback = app.config['KNOWLEDGE_DISTILLER_RESTART']
    if failure:
        with pytest.raises(LocalAddressError if failure == 'busy' else OSError):
            callback(new)
        assert load(tmp_path) == old
        timer.start.assert_not_called()
        if failure != 'busy':
            worker.release_update.assert_called_once()
        if failure != 'spawn':
            spawned.assert_not_called()
    else:
        callback(new)
        assert load(tmp_path) == new
        timer.start.assert_called_once()
        with pytest.raises(LocalAddressError, match='local_address_busy'):
            callback(old)
        spawned.assert_called_once()
        assert spawned.call_args.kwargs['creationflags'] & 0x01000000
        worker.release_update.assert_not_called()


def test_busy_port_never_constructs_application(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import windows_app
    from knowledge_distiller.v1.local_address import LocalAddressBindError
    from knowledge_distiller.v1.app import AppPaths
    from unittest.mock import Mock
    import socket
    create = Mock()
    monkeypatch.setattr(windows_app, 'create_application', create)
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        with pytest.raises(LocalAddressBindError):
            windows_app.serve(AppPaths(tmp_path), occupied.getsockname()[1])
    create.assert_not_called()


@windows_only
def test_windows_ui_metadata_does_not_use_wmi(monkeypatch):
    import platform
    from knowledge_distiller.v1.windows_platform import machine, system_label
    def forbidden(*args, **kwargs):
        raise AssertionError('WMI-based platform calls must not run in HTTP threads')
    monkeypatch.setattr(platform, 'machine', forbidden)
    monkeypatch.setattr(platform, 'win32_ver', forbidden)
    assert machine() in {'AMD64', 'ARM64', 'x86'}
    assert system_label().startswith('Windows ')
