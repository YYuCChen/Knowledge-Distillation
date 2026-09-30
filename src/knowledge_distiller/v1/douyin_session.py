"""Application-owned Douyin login; routine collection never attaches to daily Chrome."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from .chrome import ChromePage, ChromeSessionError, DouyinConnection, _read_endpoint
from .keychain import KeychainError


# In-page read of the dedicated profile's own account. Douyin answers HTTP 200
# with status_code 8 ("用户未登录") when the profile has no live session.
DOUYIN_LIVE_PROBE = (
    "(async()=>{try{const r=await fetch('/aweme/v1/web/user/profile/self/?device_platform=webapp&aid=6383',"
    "{credentials:'include'});if(r.status===401)return 'logged_out';if(!r.ok)return 'unknown';"
    "const d=await r.json();if(d&&d.status_code===0&&d.user&&d.user.sec_uid)return 'connected';"
    "if(d&&d.status_code===8)return 'logged_out';return 'unknown'}catch(e){return 'unknown'}})()")


class DouyinOwnedSession:
    platform = 'douyin'
    label = '抖音'
    home = 'https://www.douyin.com/user/self'
    cookie_urls = ['https://www.douyin.com']

    def __init__(self, store, root: Path, *, secret_factory=None):
        self.store, self.root = store, root
        from .local_secrets import LocalSecrets
        self.secret_factory = secret_factory or LocalSecrets(store.path.parent / "credentials")
        self._process = None
        self._profile = None
        self._headed = False
        self._lock = threading.RLock()
        atexit.register(self.close)

    def _context(self):
        row = self.store.connection(self.platform)
        if row is None or row['state'] != 'connected':
            raise ChromeSessionError(self.platform + '_login_required')
        context = row['browser_context']
        if not context or not context.startswith('owned:'):
            raise ChromeSessionError(self.platform + '_login_required')
        identifier = context.removeprefix('owned:')
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ChromeSessionError(self.platform + '_login_required')
        return identifier

    def _secret(self, identifier):
        return self.secret_factory(self.platform + '-session-' + identifier)

    def cookies(self):
        try:
            rows = json.loads(self._secret(self._context()).load())
            if not rows or (self.platform == 'youtube' and not isinstance(rows, list)) or (self.platform != 'youtube' and (not isinstance(rows, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in rows.items()))):
                raise ValueError('invalid cookie cache')
            return rows
        except (KeychainError, ValueError, TypeError) as error:
            raise ChromeSessionError(self.platform + '_login_required') from error

    def _launch(self, identifier, *, headed=False):
        profile = self.root / identifier
        if self._process is not None and self._process.poll() is None:
            if self._profile == profile and self._headed == headed:
                return profile / 'DevToolsActivePort'
            self.close()
        from .desktop_paths import chrome_executable
        executable = chrome_executable()
        if not executable.is_file():
            raise ChromeSessionError(self.platform + '_browser_missing')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        self._stop_orphan(profile, executable)
        port = profile / 'DevToolsActivePort'
        port.unlink(missing_ok=True)
        args = [str(executable), '--user-data-dir=' + str(profile), '--remote-debugging-port=0',
                '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check']
        if not headed:
            args.append('--headless=new')
        args.append('about:blank')
        self._process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._profile, self._headed = profile, headed
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            if port.exists():
                return port
            time.sleep(.1)
        self.close()
        raise ChromeSessionError(self.platform + '_browser_unavailable')

    def _stop_orphan(self, profile, executable):
        """Reclaim only an orphan Chrome using this exact application profile."""
        lock = profile / 'SingletonLock'
        if not lock.is_symlink():
            return
        try:
            pid = int(os.readlink(lock).rsplit('-', 1)[1])
        except (OSError, ValueError, IndexError):
            return
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'ppid=,command='],
                                capture_output=True, text=True, check=False)
        fields = result.stdout.strip().split(None, 1)
        if len(fields) != 2 or fields[0] != '1':
            return
        command = fields[1]
        if (not command.startswith(str(executable) + ' ')
                or '--user-data-dir=' + str(profile) + ' --' not in command):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.1)
        # Leave the live process and its lock intact if it cannot quit normally.
        raise ChromeSessionError(self.platform + '_browser_unavailable')

    def _page(self, url, port):
        import websocket
        return ChromePage(url, port, connector=websocket.create_connection)

    def _wait_login(self, page):
        return page.wait_for("location.hostname === 'www.douyin.com' && Boolean(document.querySelector('[data-e2e=\"user-info\"]')?.getClientRects().length)", timeout=180)

    def verify(self):
        # Stage a new identity. Failed login never overwrites the active connection.
        identifier = uuid4().hex
        with self._lock:
            try:
                port = self._launch(identifier, headed=True)
                with self._page(self.home, port) as page:
                    logged_in = self._wait_login(page)
                    if not logged_in:
                        raise ChromeSessionError(self.platform + '_login_required')
                    label = (page.evaluate("document.querySelector('[data-e2e=\"user-title\"]')?.textContent || null") if self.platform == 'douyin' else None)
                    rows = page.cookies(self.cookie_urls)
                    cookies = rows if self.platform == 'youtube' else {r['name']: r['value'] for r in rows if isinstance(r.get('name'), str) and isinstance(r.get('value'), str)}
                    if not cookies:
                        raise ChromeSessionError(self.platform + '_login_required')
                    secret = self._secret(identifier)
                    secret.save(json.dumps(cookies, ensure_ascii=False))
                    from .local_secrets import LocalSecrets
                    if isinstance(self.secret_factory, LocalSecrets):
                        self.secret_factory.mark_validated(self.platform + '-session-' + identifier)
                    try:
                        secret.set_label('知识蒸馏器｜' + self.label + '登录态｜' + identifier[-8:])
                    except KeychainError:
                        pass
                return DouyinConnection(label.strip()[:128] if isinstance(label, str) else None, 'owned:' + identifier)
            except Exception as error:
                self.close()
                self.discard('owned:' + identifier)
                if isinstance(error, ChromeSessionError):
                    raise
                raise ChromeSessionError(self.platform + '_browser_unavailable') from error
            finally:
                self.close()

    def browser_page(self, url):
        self.cookies()  # Never silently import the daily browser or open an interactive login.
        with self._lock:
            return self._page(url, self._launch(self._context()))

    def live_status(self, *, timeout=20):
        """Read the dedicated profile's current login without changing it.

        Returns 'connected', 'logged_out' or 'unknown'. It never saves, replaces
        or clears stored credentials; only a definite logout is 'logged_out'.
        """
        if self.platform != 'douyin':
            raise NotImplementedError(self.platform)
        identifier = self._context()
        with self._lock:
            try:
                with self._page('https://www.douyin.com/', self._launch(identifier)) as page:
                    if not page.wait_for("location.hostname==='www.douyin.com' && "
                                         "document.readyState==='complete'", timeout=timeout):
                        return 'unknown'
                    result = page.call('Runtime.evaluate', {'expression': DOUYIN_LIVE_PROBE,
                        'returnByValue': True, 'awaitPromise': True}, page=True)
            except ChromeSessionError as error:
                if str(error).endswith('_login_required'):
                    return 'logged_out'
                return 'unknown'
            except Exception:
                return 'unknown'
        value = result.get('result', {}).get('value')
        return value if value in {'connected', 'logged_out'} else 'unknown'

    def _quit_gracefully(self, process, profile):
        """Ask Chrome to exit through DevTools so its cookie store is flushed.

        A signal right after login lost the new auth cookies from the profile
        (BUG-20260922-02): later headless launches were logged out.
        """
        if profile is None:
            return False
        try:
            import websocket
            endpoint = _read_endpoint(profile / 'DevToolsActivePort')
            connection = websocket.create_connection(endpoint, timeout=5, suppress_origin=True,
                                                     http_no_proxy=['127.0.0.1'])
            try:
                connection.send(json.dumps({'id': 1, 'method': 'Browser.close'}))
            finally:
                try:
                    connection.close()
                except Exception:
                    pass
            process.wait(timeout=10)
            return True
        except Exception:
            return False

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            if process is not None and process.poll() is None:
                if sys.platform != 'win32' and self._quit_gracefully(process, self._profile):
                    return
                if sys.platform == 'win32':
                    # Only this Popen-owned live process tree, never /IM chrome.
                    subprocess.run(
                        [str(Path(os.environ['SystemRoot']) / 'System32/taskkill.exe'),
                         '/PID', str(process.pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
                    if process.poll() is not None:
                        return
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def discard(self, context):
        if not context or not context.startswith('owned:'):
            return
        identifier = context.removeprefix('owned:')
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            return
        with self._lock:
            if self._profile == self.root / identifier:
                self.close()
            self._secret(identifier).clear()
            if (self.root / identifier).exists():
                deadline = time.monotonic() + (5 if sys.platform == 'win32' else 0)
                while True:
                    try:
                        shutil.rmtree(self.root / identifier)
                        break
                    except PermissionError:
                        # Windows may release terminated Chrome's file handles
                        # after taskkill returns. Retry only our owned profile.
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.1)
