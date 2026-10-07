"""Application-owned write session for an isolated staging Vault.

The controller keeps the staging directory inode locked and exposes the same
nonce-authenticated local socket understood by the installed ``wiki_session``
module.  Codex and its grandchildren receive no lock file descriptor.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import shutil
import socket
import tempfile
import threading

from .wiki_lock import VaultWriteLock, canonical_vault


LOCK_KEY_ENV = "KD_WIKI_LOCK_VAULT_KEY"
LOCK_SOCKET_ENV = "KD_WIKI_LOCK_SOCKET"
LOCK_TOKEN_ENV = "KD_WIKI_LOCK_TOKEN"


class WikiSessionBrokerError(RuntimeError):
    """A fixed-code staging session failure."""


class WikiSessionBroker:
    """Hold a staging lock and answer live-session checks over a Unix socket."""

    def __init__(self, staging_vault: Path | str, runtime_root: Path | str):
        self.staging_vault = canonical_vault(staging_vault)
        self.runtime_root = canonical_vault(runtime_root)
        self._lock: VaultWriteLock | None = None
        self._directory: Path | None = None
        self._endpoint: Path | None = None
        self._token: str | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return bool(self._lock is not None and self._thread is not None
                    and self._thread.is_alive() and self._endpoint is not None
                    and self._endpoint.exists())

    def start(self) -> "WikiSessionBroker":
        if self._lock is not None:
            raise WikiSessionBrokerError("session_already_started")
        try:
            self._lock = VaultWriteLock.acquire(self.staging_vault)
            # Keep sockaddr_un short even when the application data directory or
            # staging task path is long. This directory contains only a socket
            # and a random proof, never Vault content.
            system_temporary = Path("/private/tmp")
            if not system_temporary.is_dir():
                system_temporary = Path(tempfile.gettempdir()).resolve()
            directory = Path(tempfile.mkdtemp(prefix=".kdws-", dir=system_temporary))
            os.chmod(directory, 0o700)
            endpoint = directory / "s"
            # sockaddr_un.sun_path is 104 bytes on macOS, including NUL.
            if len(os.fsencode(endpoint)) >= 104:
                raise WikiSessionBrokerError("session_path_too_long")
            self._directory = directory
            self._endpoint = endpoint
            self._token = secrets.token_hex(32)
            self._thread = threading.Thread(
                target=self._serve, name="wiki-staging-session", daemon=True)
            self._thread.start()
            if not self._ready.wait(2) or not self.active:
                raise WikiSessionBrokerError("session_unavailable")
            return self
        except BlockingIOError as error:
            self.close()
            raise WikiSessionBrokerError("vault_busy") from error
        except WikiSessionBrokerError:
            self.close()
            raise
        except OSError as error:
            self.close()
            raise WikiSessionBrokerError("session_unavailable") from error

    def environment(self) -> dict[str, str]:
        if not self.active or self._endpoint is None or self._token is None:
            raise WikiSessionBrokerError("session_unavailable")
        key = hashlib.sha256(os.fsencode(self.staging_vault)).hexdigest()
        return {
            LOCK_KEY_ENV: key,
            LOCK_SOCKET_ENV: os.fspath(self._endpoint),
            LOCK_TOKEN_ENV: self._token,
        }

    def _serve(self) -> None:
        assert self._endpoint is not None and self._token is not None
        key = hashlib.sha256(os.fsencode(self.staging_vault)).hexdigest()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(os.fspath(self._endpoint))
                os.chmod(self._endpoint, 0o600)
                server.listen(8)
                server.settimeout(0.2)
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        connection, _ = server.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        if self._stop.is_set():
                            return
                        continue
                    with connection:
                        connection.settimeout(1.0)
                        try:
                            reader = connection.makefile("rb")
                            token = reader.readline(130).rstrip(b"\r\n").decode("ascii")
                            supplied_key = reader.readline(130).rstrip(b"\r\n").decode("ascii")
                            valid = (hmac.compare_digest(token, self._token)
                                     and hmac.compare_digest(supplied_key, key))
                            connection.sendall(b"OK\n" if valid else b"NO\n")
                        except (OSError, UnicodeError):
                            pass
        except OSError:
            self._ready.set()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        if self._endpoint is not None:
            try:
                self._endpoint.unlink(missing_ok=True)
            except OSError:
                pass
            self._endpoint = None
        if self._directory is not None:
            try:
                shutil.rmtree(self._directory)
            except OSError:
                pass
            self._directory = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        self._token = None

    def __enter__(self) -> "WikiSessionBroker":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()
