from __future__ import annotations

import logging
import atexit
import threading

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


logger = logging.getLogger(__name__)


class ChromeSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DouyinConnection:
    account_label: str | None
    browser_context: str | None = None


class _BrowserConnection:
    """One authorized browser transport, with serialized request/response pairs."""
    def __init__(self, endpoint, connector):
        self.lock = threading.RLock()
        self.socket = connector(endpoint, timeout=60, suppress_origin=True,
                                http_no_proxy=["127.0.0.1"])
        self.next_id = 0

    def call(self, method, params=None, session=None):
        import json
        import time
        with self.lock:
            if self.socket is None:
                raise ChromeSessionError("chrome_connection_failed")
            self.next_id += 1
            message = {"id": self.next_id, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            try:
                self.socket.send(json.dumps(message))
                deadline = time.monotonic() + 20
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    self.socket.settimeout(remaining)
                    response = json.loads(self.socket.recv())
                    if response.get("id") == self.next_id:
                        break
            except Exception as error:
                self.close()
                raise ChromeSessionError("chrome_connection_failed") from error
            if "error" in response:
                raise ChromeSessionError("chrome_connection_failed")
            return response.get("result", {})

    def close(self):
        with self.lock:
            if self.socket is not None:
                _quietly(self.socket.close)
                self.socket = None


_connections = {}
_connections_lock = threading.RLock()


def close_browser_connections():
    with _connections_lock:
        for connection in _connections.values():
            connection.close()
        _connections.clear()


def _browser_connection(endpoint):
    import websocket
    with _connections_lock:
        # Chrome restarts change the endpoint; never retain a stale browser.
        for old in list(_connections):
            if old != endpoint:
                _connections.pop(old).close()
        connection = _connections.get(endpoint)
        if connection is None or connection.socket is None:
            connection = _BrowserConnection(endpoint, websocket.create_connection)
            _connections[endpoint] = connection
        return connection


atexit.register(close_browser_connections)


class ChromePage:
    """A task-owned tab on the application's reusable Chrome connection."""
    def __init__(self, url: str, active_port_file: Path | None = None, *, connector=None):
        self._connection = None
        self._target = None
        self._session = None
        # Explicit connectors retain isolated ownership for embedded callers/tests.
        self._private_connection = connector is not None
        endpoint = _read_endpoint(active_port_file or _default_active_port_file())
        try:
            self._connection = (_BrowserConnection(endpoint, connector) if connector
                                else _browser_connection(endpoint))
            self._target = self.call("Target.createTarget", {"url": url})["targetId"]
            self._session = self.call("Target.attachToTarget", {
                "targetId": self._target, "flatten": True,
            })["sessionId"]
            self.call("Runtime.enable", page=True)
        except Exception as error:
            self.close()
            raise ChromeSessionError("chrome_connection_failed") from error

    def call(self, method: str, params=None, *, page: bool = False):
        if self._connection is None:
            raise ChromeSessionError("chrome_connection_failed")
        return self._connection.call(method, params, self._session if page else None)

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
        }, page=True)
        if "exceptionDetails" in result:
            raise ChromeSessionError("chrome_connection_failed")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, *, timeout: float = 20):
        import time
        deadline = time.monotonic() + timeout
        while True:
            value = self.evaluate(expression)
            if value:
                return value
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    def cookies(self, urls: list[str]):
        return self.call("Network.getCookies", {"urls": urls}, page=True).get("cookies", [])

    def close(self):
        if self._connection is not None:
            try:
                if self._target is not None:
                    self.call("Target.closeTarget", {"targetId": self._target})
            except Exception:
                logger.warning("Could not close the temporary Chrome page")
            finally:
                if self._private_connection:
                    self._connection.close()
                self._connection = None
                self._target = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class DouyinChromeSession:
    def __init__(self, page_factory=None, active_port_file: Path | None = None):
        self._page_factory = page_factory or ChromePage
        self._active_port_file = active_port_file

    def verify(self) -> DouyinConnection:
        try:
            with self._page_factory("https://www.douyin.com/user/self", self._active_port_file) as page:
                found = page.wait_for("""location.hostname === 'www.douyin.com' &&
                    Boolean(document.querySelector('[data-e2e="user-info"]')?.getClientRects().length)""")
                if not found:
                    raise ChromeSessionError("douyin_login_required")
                label = page.evaluate("""document.querySelector('[data-e2e="user-title"]')?.textContent || null""")
                return DouyinConnection(label.strip()[:128] if isinstance(label, str) and label.strip() else None)
        except ChromeSessionError:
            raise
        except Exception as error:
            raise ChromeSessionError("chrome_connection_failed") from error

    def cookies(self) -> Mapping[str, str]:
        try:
            with self._page_factory("about:blank", self._active_port_file) as page:
                rows = page.cookies(["https://www.douyin.com"])
                values = {
                    row["name"]: row["value"] for row in rows
                    if isinstance(row, dict) and isinstance(row.get("name"), str)
                    and isinstance(row.get("value"), str) and row["name"] and row["value"]
                }
                if not values:
                    raise ChromeSessionError("douyin_login_required")
                return values
        except ChromeSessionError:
            raise
        except Exception as error:
            raise ChromeSessionError("chrome_connection_failed") from error


def _default_active_port_file() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Google"
        / "Chrome"
        / "DevToolsActivePort"
    )


def _read_endpoint(path: Path) -> str:
    try:
        lines = [line.strip() for line in path.read_text(encoding="ascii").splitlines()]
        port_text, websocket_path = [line for line in lines if line][:2]
        port = int(port_text)
    except (OSError, UnicodeError, ValueError) as error:
        raise ChromeSessionError("chrome_remote_debugging_disabled") from error
    if not 0 < port <= 65_535 or not websocket_path.startswith("/devtools/browser/"):
        raise ChromeSessionError("chrome_remote_debugging_disabled")
    return f"ws://127.0.0.1:{port}{websocket_path}"


def _quietly(action: Callable[[], object]) -> None:
    try:
        action()
    except Exception:
        pass
