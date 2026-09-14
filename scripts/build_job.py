"""Durable, single-host candidate build worker. No publishing or installation."""
from __future__ import annotations
import argparse
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile


def write_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 2
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                # Windows readers briefly open without FILE_SHARE_DELETE.
                # Retry only native sharing/access races; persistent errors fail.
                if getattr(error, 'winerror', None) not in {5, 32, 33} or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path, timeout=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0'); stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise BlockingIOError('Build lock is busy: ' + str(path)) from error
                time.sleep(.05)
        try:
            yield stream
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def read_status_json(path):
    # Windows may reject a reader briefly while the writer replaces status.
    # Persistent ACL errors still surface; malformed JSON is never retried.
    deadline = time.monotonic() + 2
    while True:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except PermissionError:
            if sys.platform != 'win32' or time.monotonic() >= deadline:
                raise
            time.sleep(.01)


def status(job):
    state = read_status_json(job / 'status.json') if (job / 'status.json').exists() else {'status': 'interrupted' if any(job.glob('attempt-*')) else 'not-started'}
    if state['status'] == 'running':
        try:
            with locked(job / 'worker.lock'):
                state = dict(state, status='interrupted')
        except BlockingIOError:
            pass
    return state


def stop_child(process):
    if os.name == 'nt':
        # The worker's kill-on-close Job owns every descendant. Terminate the
        # Popen-owned launcher by its existing handle; unwinding this worker
        # closes the Job and reaps remaining children without WMI/taskkill.
        process.terminate()
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            process.kill()
    finally:
        if os.name != 'nt':
            # A child may ignore TERM after its parent has already exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait(timeout=10)


def collect_artifacts(job, attempt, patterns):
    artifacts = {}
    for pattern in patterns:
        check_cancel(job)
        matched = sorted(attempt.glob(pattern))
        if not matched:
            raise RuntimeError('Missing artifact: ' + pattern)
        for path in matched:
            path.resolve().relative_to(attempt.resolve())
            with path.open('rb') as stream:
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    check_cancel(job)
                    digest.update(chunk)
            relative = path.relative_to(job).as_posix()
            artifacts[relative] = {'path': relative, 'size': path.stat().st_size, 'sha256': digest.hexdigest()}
    return [artifacts[key] for key in sorted(artifacts)]


def checked_success(job, state, request):
    if state.get('status') != 'succeeded':
        return state
    try:
        actual = collect_artifacts(job, job / ('attempt-' + str(state['attempt'])), request.get('artifacts', []))
        if actual != state.get('artifacts'):
            raise RuntimeError('Artifact size or SHA-256 changed')
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        return dict(state, status='invalid-artifacts', error=str(error))
    return state


def check_cancel(job):
    if (job / 'cancel').exists():
        raise InterruptedError('Cancellation requested')


def cancel(job):
    # A completed success wins over a later cancel; an earlier cancel wins
    # over finalization. Share the same short lock with the successful commit.
    with locked(job / 'submit.lock', timeout=20):
        with locked(job / 'control.lock', timeout=20):
            state = status(job)
            if state['status'] not in {'succeeded', 'failed', 'cancelled'}:
                (job / 'cancel').touch()
            return status(job)


def run(job, retry=False):
    job = job.resolve()
    if os.name == 'nt':
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        from knowledge_distiller.v1.windows_job import own_children
        own_children()
    with locked(job / 'worker.lock'):
        old = status_without_lock(job)
        request = json.loads((job / 'request.json').read_text(encoding='utf-8'))
        old = checked_success(job, old, request)
        if old.get('status') == 'succeeded': return
        if (old or any(job.glob('attempt-*'))) and not retry: return
        with locked(job / 'control.lock', timeout=20):
            if retry:
                (job / 'cancel').unlink(missing_ok=True)
        number = max([old.get('attempt', 0)] + [int(p.name[8:]) for p in job.glob('attempt-*') if p.name[8:].isdigit()]) + 1
        attempt = job / ('attempt-' + str(number))
        attempt.mkdir()
        state = {'status': 'running', 'attempt': number, 'pid': os.getpid(),
                 'source_commit': request['source_commit'], 'version': request['version'], 'started': time.time()}
        def save(**values):
            state.update(values, updated=time.time()); write_json(job / 'status.json', state)
        save()
        try:
            # One native build per host, including jobs submitted under other versions.
            with locked(Path(request.get('host_lock', job.parent / 'host.lock'))) as host_lock:
                env = dict(os.environ, **request.get('env', {}))
                for key in ('PYTHONPATH',):
                    if key in env: env[key] = env[key].replace('{source}', request['source'])
                for index, step in enumerate(request['steps']):
                    check_cancel(job)
                    command = [part.replace('{attempt}', attempt.as_posix()).replace('{source}', request['source']) for part in step['command']]
                    save(step=step['name'])
                    with (attempt / f'{index:02d}-{step["name"]}.log').open('wb') as log:
                        process = subprocess.Popen(command, cwd=request['source'], env=env, stdout=log, stderr=subprocess.STDOUT,
                                                   start_new_session=os.name != 'nt',
                                                   **({'pass_fds': (host_lock.fileno(),)} if os.name != 'nt' else {}))
                        try:
                            while process.poll() is None:
                                check_cancel(job)
                                save(child_pid=process.pid); time.sleep(2)
                            check_cancel(job)
                            if process.returncode: raise RuntimeError(f'{step["name"]} exited {process.returncode}')
                        finally:
                            if process.poll() is None: stop_child(process)
                check_cancel(job)
                artifacts = collect_artifacts(job, attempt, request.get('artifacts', []))
                with locked(job / 'control.lock', timeout=20):
                    check_cancel(job)
                    save(status='succeeded', artifacts=artifacts, finished=time.time())
        except BaseException as error:
            save(status='cancelled' if isinstance(error, InterruptedError) else 'failed', error=f'{type(error).__name__}: {error}', finished=time.time())
            raise


def status_without_lock(job):
    return read_status_json(job / 'status.json') if (job / 'status.json').exists() else {}


def submit(job, request, retry=False, *, supervised=False):
    """Launch a worker; supervised jobs remain subject to the caller/CI host."""
    job.mkdir(parents=True, exist_ok=True)
    with locked(job / 'submit.lock', timeout=20):
        path = job / 'request.json'
        if path.exists():
            if json.loads(path.read_text(encoding='utf-8')) != request: raise ValueError('Job identity already has different inputs')
        else: write_json(path, request)
        old = checked_success(job, status(job), request)
        if old['status'] in {'running', 'succeeded'}: return old
        if old['status'] != 'not-started' and not retry: return old
        flags = (0x00000008 | 0x00000200) if os.name == 'nt' else 0
        if os.name == 'nt' and not supervised:
            flags |= 0x01000000  # CREATE_BREAKAWAY_FROM_JOB; never silently fall back
        with (job / 'worker.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run', str(job)] + (['--retry'] if retry else []),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                             creationflags=flags, start_new_session=os.name != 'nt' and not supervised)
        for _ in range(100):
            new = status(job)
            if new.get('attempt', 0) > old.get('attempt', 0): return new
            time.sleep(.1)
        raise RuntimeError('Worker did not acknowledge startup; inspect worker.log')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['submit','run','status','cancel'])
    parser.add_argument('job', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--supervised', action='store_true',
                        help='Keep submitted workers under the calling host; they may end with its job')
    args = parser.parse_args()
    if args.action == 'run': run(args.job, args.retry)
    elif args.action == 'submit': print(json.dumps(submit(args.job, json.loads(args.request.read_text(encoding='utf-8')), args.retry, supervised=args.supervised)))
    elif args.action == 'status': print(json.dumps(status(args.job)))
    else:
        print(json.dumps(cancel(args.job)))


if __name__ == '__main__': main()
