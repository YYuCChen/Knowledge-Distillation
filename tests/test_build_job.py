"""Real disposable build jobs; no real application data or build dependencies."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_job.py'
spec = importlib.util.spec_from_file_location('build_job', SCRIPT)
build_job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_job)


def prepare(tmp_path, code=None, name='job', artifacts=None):
    job = tmp_path / name
    job.mkdir()
    request = {'source': str(tmp_path), 'source_commit': 'synthetic-commit', 'version': 'synthetic-version',
               'steps': [{'name': 'synthetic', 'command': [sys.executable, '-c', code or 'pass']}],
               'artifacts': artifacts or []}
    build_job.write_json(job / 'request.json', request)
    return job, request


def run(job, retry=False):
    return subprocess.run([sys.executable, str(SCRIPT), 'run', str(job)] + (['--retry'] if retry else []),
                          capture_output=True, timeout=25)


def wait_state(job, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = build_job.status(job)
        if predicate(state):
            return state
        time.sleep(.05)
    raise AssertionError(f'Job did not reach expected state: {state}')


def test_atomic_utf8_write_and_replace_failure_preserves_previous(tmp_path, monkeypatch):
    target = tmp_path / 'status.json'
    build_job.write_json(target, {'error': '中文故障'})
    assert json.loads(target.read_text(encoding='utf-8')) == {'error': '中文故障'}
    def denied(*args):
        raise PermissionError('synthetic failure')
    monkeypatch.setattr(build_job.os, 'replace', denied)
    with pytest.raises(PermissionError):
        build_job.write_json(target, {'error': 'new'})
    assert json.loads(target.read_text(encoding='utf-8')) == {'error': '中文故障'}
    assert not list(tmp_path.glob('*.tmp'))


def test_status_does_not_hide_unexpected_lock_error(tmp_path, monkeypatch):
    build_job.write_json(tmp_path / 'status.json', {'status': 'running'})
    def denied(*args, **kwargs):
        raise PermissionError('unreadable lock')
    monkeypatch.setattr(build_job, 'locked', denied)
    with pytest.raises(PermissionError):
        build_job.status(tmp_path)


def test_duplicate_submit_and_completed_reuse(tmp_path):
    job, request = prepare(tmp_path, 'import time; time.sleep(.2)')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: build_job.submit(job, request), range(2)))
    assert all(state['attempt'] == 1 for state in states)
    completed = wait_state(job, lambda state: state['status'] == 'succeeded')
    assert build_job.submit(job, request) == completed
    assert run(job).returncode == 0
    assert len(list(job.glob('attempt-*'))) == 1
    with pytest.raises(ValueError, match='different inputs'):
        build_job.submit(job, dict(request, version='different'))


def test_failed_run_requires_explicit_retry(tmp_path):
    job, request = prepare(tmp_path, 'raise RuntimeError("合成失败")')
    assert run(job).returncode != 0
    assert build_job.status(job)['status'] == 'failed'
    assert run(job).returncode == 0
    assert build_job.submit(job, request)['attempt'] == 1
    assert run(job, retry=True).returncode != 0
    assert build_job.status(job)['attempt'] == 2


def test_attempt_creation_crash_requires_retry_and_uses_new_attempt(tmp_path):
    job, request = prepare(tmp_path)
    (job / 'attempt-1').mkdir()
    assert build_job.status(job)['status'] == 'interrupted'
    assert run(job).returncode == 0
    assert not (job / 'status.json').exists()
    assert run(job, retry=True).returncode == 0
    assert build_job.status(job)['attempt'] == 2


def test_success_artifact_hash_is_checked_before_reuse_and_retry_rebuilds(tmp_path):
    code = "from pathlib import Path; Path('{attempt}/candidate.zip').write_bytes(b'original')"
    job, request = prepare(tmp_path, code, artifacts=['*.zip'])
    assert run(job).returncode == 0
    (job / 'attempt-1/candidate.zip').write_bytes(b'modified')  # Same size, different hash.
    state = build_job.submit(job, request)
    assert state['status'] == 'invalid-artifacts'
    assert len(list(job.glob('attempt-*'))) == 1
    started = build_job.submit(job, request, retry=True)
    assert started['attempt'] == 2
    done = wait_state(job, lambda state: state['status'] == 'succeeded')
    assert done['attempt'] == 2
    assert build_job.submit(job, request)['status'] == 'succeeded'
    (job / 'attempt-2/candidate.zip').unlink()
    assert build_job.submit(job, request)['status'] == 'invalid-artifacts'


def test_final_step_cancel_cannot_be_reported_as_success(tmp_path):
    job, request = prepare(tmp_path)
    request['steps'][0]['command'][-1] = f"import pathlib,time; time.sleep(.2); pathlib.Path({str(job / 'cancel')!r}).touch()"
    build_job.write_json(job / 'request.json', request)
    assert run(job).returncode != 0
    assert build_job.status(job)['status'] == 'cancelled'


def test_cancel_after_success_does_not_poison_reuse(tmp_path):
    job, request = prepare(tmp_path)
    assert run(job).returncode == 0
    assert build_job.cancel(job)['status'] == 'succeeded'
    assert not (job / 'cancel').exists()
    assert build_job.submit(job, request)['status'] == 'succeeded'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited host lock; Windows uses native Job ownership')
def test_killed_worker_child_retains_host_lock_until_it_exits(tmp_path):
    job, _ = prepare(tmp_path, 'import time; time.sleep(60)')
    other, _ = prepare(tmp_path, name='other')
    with (tmp_path / 'worker.log').open('wb') as output:
        worker = subprocess.Popen([sys.executable, str(SCRIPT), 'run', str(job)], stdout=output, stderr=output)
        child = None
        try:
            child = wait_state(job, lambda state: bool(state.get('child_pid')))['child_pid']
            worker.kill(); worker.wait(timeout=5)
            assert build_job.status(job)['status'] == 'interrupted'
            os.kill(child, 0)
            with pytest.raises(BlockingIOError):
                with build_job.locked(tmp_path / 'host.lock'):
                    pass
            assert run(other).returncode != 0
            assert build_job.status(other)['status'] == 'failed'
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)
            if child:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while True:
            try:
                with build_job.locked(tmp_path / 'host.lock'):
                    break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
    assert run(other, retry=True).returncode == 0


def test_cancel_stops_live_build_and_releases_host_lock(tmp_path):
    job, _ = prepare(tmp_path, 'import time; time.sleep(60)')
    with (tmp_path / 'cancel-worker.log').open('wb') as output:
        worker = subprocess.Popen([sys.executable, str(SCRIPT), 'run', str(job)], stdout=output, stderr=output)
        try:
            wait_state(job, lambda state: bool(state.get('child_pid')))
            build_job.cancel(job)
            assert worker.wait(timeout=20) != 0
            assert build_job.status(job)['status'] == 'cancelled'
            with build_job.locked(tmp_path / 'host.lock'):
                pass
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='Windows kill-on-close Job')
def test_windows_killed_worker_terminates_owned_child(tmp_path):
    import ctypes
    from ctypes import wintypes
    job, _ = prepare(tmp_path, 'import time; time.sleep(60)')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    with (tmp_path / 'killed-worker.log').open('wb') as output:
        worker = subprocess.Popen([sys.executable, str(SCRIPT), 'run', str(job)], stdout=output, stderr=output)
        handle = None
        try:
            child = wait_state(job, lambda state: bool(state.get('child_pid')))['child_pid']
            handle = kernel.OpenProcess(0x100000, False, child)
            assert handle
            worker.kill(); worker.wait(timeout=5)
            assert kernel.WaitForSingleObject(handle, 10000) == 0
            assert build_job.status(job)['status'] == 'interrupted'
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)
            if handle:
                kernel.CloseHandle(handle)


def test_windows_transient_reader_sharing_retries_atomic_replace(tmp_path, monkeypatch):
    target = tmp_path / 'status.json'
    real = build_job.os.replace
    calls = []
    def transient(source, destination):
        calls.append(1)
        if len(calls) == 1:
            error = PermissionError('synthetic sharing violation')
            error.winerror = 32
            raise error
        return real(source, destination)
    monkeypatch.setattr(build_job.os, 'replace', transient)
    build_job.write_json(target, {'status': 'running'})
    assert len(calls) == 2
    assert json.loads(target.read_text())['status'] == 'running'
