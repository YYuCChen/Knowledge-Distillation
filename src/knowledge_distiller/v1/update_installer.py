"""Owned helper stays alive while Sparkle replaces and relaunches the desktop app."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import os
import fcntl
import signal
import shutil
import sqlite3
import stat
import tempfile
import time
import httpx
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .updates import Updates, UpdateError, parse_feed, validate_install_paths


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _database_image(path):
    """Read a quiescent image without initializing or upgrading its schema."""
    path = Path(path)
    digest = _file_sha256(path)
    with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as db:
        if (db.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                or db.execute('PRAGMA foreign_key_check').fetchall()):
            raise OSError('database restore image is invalid')
        version = db.execute('PRAGMA user_version').fetchone()[0]
    if _file_sha256(path) != digest:
        raise OSError('database restore image changed during validation')
    return digest, version


def _restore_database(backup, database):
    """Publish a verified same-volume copy before exposing the old bundle.

    This is one database replacement, not a crash-atomic bundle/DB transaction.
    On failure the caller retains both bundles and the original backup.
    """
    backup, database = Path(backup), Path(database)
    expected = _database_image(backup)
    descriptor, name = tempfile.mkstemp(prefix='.kd-update-restore-', dir=database.parent)
    stage = Path(name)
    owned = os.fstat(descriptor)
    os.close(descriptor)
    try:
        shutil.copy2(backup, stage)
        current = stage.lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or stat.S_IMODE(current.st_mode) != 0o600
                or (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino)):
            raise OSError('database restore stage identity changed')
        if _database_image(stage) != expected:
            raise OSError('database restore stage differs from backup')
        with stage.open('rb') as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                raise OSError('database restore stage identity changed')
            os.fsync(stream.fileno())
        current = stage.lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or stat.S_IMODE(current.st_mode) != 0o600
                or (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino)):
            raise OSError('database restore stage identity changed')
        # The candidate has stopped; none of its SQLite sidecars may accompany
        # this older image. The original backup is never consumed or removed.
        for suffix in ('-wal', '-shm'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        os.replace(stage, database)
        directory = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if _database_image(database) != expected:
            raise OSError('database restore readback differs from backup')
    finally:
        # Clean only the exact file created by this invocation. A substituted
        # path is not ours to remove, and a published stage no longer exists.
        try:
            current = stage.lstat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                stage.unlink()


def run(plan_path):
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_text())
    validate_install_paths(plan['data_root'], plan['info']['bundle'])
    updates = Updates(plan['data_root'], info=plan['info'])
    feed = (updates.root/'appcast.xml').read_bytes()
    release = parse_feed(feed, updates.info['public_key'], updates.info['version'])
    if not release or release['version'] != plan['version']:
        raise UpdateError('安装目标已变化，请重新检查更新。')
    if plan.get('asset_name') == release['full']['name']:
        release['selected']=release['full']
    elif plan.get('asset_name') not in (None,release['selected']['name']):
        raise UpdateError('安装文件已变化，请重新检查更新。')
    token = secrets.token_urlsafe(32)
    assets = {a['name']: a for a in (release['full'], release['selected'])}
    # Serialize fallback fetches, including duplicate HTTP requests.
    fetch_lock = threading.Lock()
    full_refused = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            prefix = '/'+token+'/'
            if not self.path.startswith(prefix):
                self.send_error(404); return
            name = self.path[len(prefix):]
            try:
                if name == 'appcast.xml':
                    self.send_response(200); self.send_header('Content-Length', str(len(feed))); self.end_headers()
                    self.wfile.write(feed)
                elif name in assets:
                    if name == release['full']['name'] and release['selected'] != release['full']:
                        # Sparkle may request a full fallback after a delta fails.
                        # Persist the reason before restoring/relaunching the old app.
                        (updates.root/'full-update-required.json').write_text(json.dumps({
                            'version':release['version'],'reason':'delta_not_applicable'}))
                        full_refused.set()
                        self.send_error(409, 'Full update requires explicit confirmation')
                        return
                    with fetch_lock:
                        path = updates.download(assets[name])
                    self.send_response(200); self.send_header('Content-Length', str(path.stat().st_size)); self.end_headers()
                    with path.open('rb') as stream:
                        while chunk := stream.read(1024*1024):
                            self.wfile.write(chunk)
                else:
                    self.send_error(404)
            except Exception:
                self.send_error(503)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    result = 1
    target = Path(updates.info['bundle'])
    previous = target.with_name('.knowledge-distiller-update-backup.app')
    backup_db = updates.root/'before-install.sqlite3'
    handshake = updates.root/'startup-handshake'
    stopped = False
    owns_previous = False
    accepted = False
    installation_lock = (Path(plan['data_root'])/'.update.lock').open('a')
    snapshot_ready = False
    candidate = None
    def wait_exit(pid, timeout=60):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try: os.kill(pid, 0)
            except ProcessLookupError: return
            time.sleep(.2)
        raise UpdateError('应用尚未退出，安装已停止。')
    try:
        fcntl.flock(installation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A frozen onefile helper has a bootloader parent, not the requesting app.
        # Bind the request to the running instance of this exact data directory.
        instance = json.loads((Path(plan['data_root'])/'.desktop-instance.json').read_text())
        if plan['parent_pid'] != instance['pid']:
            raise UpdateError('安装请求的应用进程已变化。')
        if previous.exists():
            raise UpdateError('发现尚未处理的更新回退副本，本次未覆盖。')
        updates.download(release['selected'])
        previous.mkdir()
        owns_previous = True
        subprocess.run(['ditto', str(target), str(previous)], check=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(previous)], check=True)
        os.kill(plan['parent_pid'], signal.SIGTERM)
        wait_exit(plan['parent_pid'])
        stopped = True
        database = Path(plan['data_root'])/'knowledge.sqlite3'
        # No worker or web writer remains; the candidate is also held before workers.
        with sqlite3.connect(database) as source, sqlite3.connect(backup_db) as destination:
            source.backup(destination)
        os.chmod(backup_db, 0o600)
        snapshot_ready = True
        cli = Path(updates.info['bundle'])/'Contents/Helpers/Updater.app/Contents/MacOS/update-cli'
        with (updates.root/'install.log').open('w') as log:
            result = subprocess.run([str(cli), updates.info['bundle'], '--application', updates.info['bundle'],
                '--feed-url', f'http://127.0.0.1:{server.server_port}/{token}/appcast.xml',
                '--check-immediately', '--interactive', '--user-agent-name', 'KnowledgeDistiller', '--verbose'],
                stdout=log, stderr=subprocess.STDOUT).returncode
        if result:
            if full_refused.is_set():
                raise UpdateError('差量更新未能应用，当前版本已保留。请在设置中确认后再下载完整包。')
            raise UpdateError('Sparkle 未完成安装。')
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(target)], check=True)
        executable = target/'Contents/MacOS/KnowledgeDistiller'
        probe = updates.root/'candidate-runtime.json'
        subprocess.run([str(executable), '--data-dir', plan['data_root'], '--check-runtime', str(probe)],
                       check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        handshake.unlink(missing_ok=True)
        arguments = [str(executable), '--data-dir', plan['data_root'], '--update-handshake', str(handshake)]
        if plan.get('no_open'): arguments.append('--no-open')
        candidate = subprocess.Popen(arguments, start_new_session=True)
        deadline = time.monotonic()+90
        ready = False
        while time.monotonic() < deadline and candidate.poll() is None:
            try:
                state = json.loads((Path(plan['data_root'])/'.desktop-instance.json').read_text())
                if state['pid'] == candidate.pid:
                    url = f"http://127.0.0.1:{state['port']}"
                    status = httpx.get(url+'/settings/updates/status', timeout=2).json()
                    home = httpx.get(url+'/', timeout=2)
                    if status['version'] == release['version'] and status['phase'] == 'installing' and home.status_code == 200:
                        ready = True
                        break
            except (OSError, ValueError, httpx.HTTPError):
                pass
            time.sleep(.25)
        if not ready:
            raise UpdateError('新版本启动检查未通过。')
        # This is a paused startup check, not an attempt to roll back live user work.
        decision = updates.root/'startup-accept.tmp'
        decision.write_text('accepted')
        # After publication might have happened, never roll back potentially live work.
        accepted = True
        decision.replace(handshake)
        result = 0
        try:
            shutil.rmtree(previous)
            backup_db.unlink(missing_ok=True)
            for name in assets:
                (updates.root/name).unlink(missing_ok=True)
        except OSError as error:
            # Work is already live: cleanup failure must never roll user data back.
            (updates.root/'cleanup-error.txt').write_text(str(error))
    except Exception as error:
        result = 1
        if accepted:
            # Keep the previous app and snapshot if cleanup/acknowledgment is uncertain.
            # The candidate may already be processing new user work.
            raise
        if candidate is not None and candidate.poll() is None:
            candidate.terminate()
            candidate.wait(timeout=60)
        if stopped and owns_previous and previous.exists():
            if snapshot_ready:
                database = Path(plan['data_root'])/'knowledge.sqlite3'
                _restore_database(backup_db, database)
            # Never expose a launchable old program with a new/partial DB.
            # If restoration above fails, target/previous/backup stay intact.
            if target.exists(): shutil.rmtree(target)
            previous.rename(target)
            arguments = [str(target/'Contents/MacOS/KnowledgeDistiller'), '--data-dir', plan['data_root']]
            if plan.get('no_open'): arguments.append('--no-open')
            fcntl.flock(installation_lock, fcntl.LOCK_UN)
            subprocess.Popen(arguments, start_new_session=True)
        elif not stopped and owns_previous and previous.exists():
            shutil.rmtree(previous)
        (updates.root/'install-error.txt').write_text(str(error))
    finally:
        server.shutdown(); server.server_close()
        installation_lock.close()
        (updates.root/'install-result.json').write_text(json.dumps({'version': plan['version'], 'exit_code': result}))
    return result
