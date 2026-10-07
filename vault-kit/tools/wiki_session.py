#!/usr/bin/env python3
"""Hold the Vault directory lock for an entire manual editing command.

This module is deliberately standard-library-only because it is installed in
the user's Vault.  The lock is the opened Vault directory inode; no PID file,
timeout, or stale-lock deletion heuristic is involved.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading


LOCK_FD_ENV = "KD_WIKI_LOCK_FD"
LOCK_KEY_ENV = "KD_WIKI_LOCK_VAULT_KEY"
LOCK_SOCKET_ENV = "KD_WIKI_LOCK_SOCKET"
LOCK_TOKEN_ENV = "KD_WIKI_LOCK_TOKEN"
_SOCKET_PATH_LIMIT = 104


class SessionError(RuntimeError):
    pass


def _canonical_root(value: str | os.PathLike[str]) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SessionError("vault_symlink")
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise SessionError("vault_path_invalid")
    except SessionError:
        raise
    except OSError as error:
        raise SessionError("vault_path_invalid") from error
    return path


def canonical_root(value: str | os.PathLike[str]) -> Path:
    """Public path guard shared with the read-only protocol."""
    return _canonical_root(value)


def _key(root: Path) -> str:
    return hashlib.sha256(os.fsencode(root)).hexdigest()


def _acquire(root: Path) -> int:
    if os.name != "posix":
        raise SessionError("vault_lock_unsupported")
    import fcntl

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
        opened = os.fstat(descriptor)
        current = root.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise SessionError("vault_path_changed")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(descriptor, True)
        return descriptor
    except SessionError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise SessionError("vault_busy") from error
        raise SessionError("vault_lock_failed") from error


def session_is_locked(root: Path | str) -> bool:
    """Check the inherited fd and ensure it owns the lock for ``root``.

    The descriptor, inode and derived Vault key are all checked. Reapplying
    ``flock`` on a valid inherited open-file description preserves the lock.
    """
    if os.name != "posix":
        return False
    try:
        canonical = _canonical_root(root)
        key = _key(canonical)
        if os.environ.get(LOCK_KEY_ENV) != key:
            return False
        raw_descriptor = os.environ.get(LOCK_FD_ENV)
        if raw_descriptor:
            try:
                descriptor = int(raw_descriptor)
                opened = os.fstat(descriptor)
                current = canonical.stat()
                if stat.S_ISDIR(opened.st_mode) and (
                    opened.st_dev, opened.st_ino
                ) == (current.st_dev, current.st_ino):
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
            except (OSError, ValueError):
                pass
        endpoint = os.environ.get(LOCK_SOCKET_ENV)
        token = os.environ.get(LOCK_TOKEN_ENV)
        if not endpoint or not token:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(endpoint)
            client.sendall((token + "\n" + key + "\n").encode("ascii"))
            response = b""
            while len(response) < 3:
                chunk = client.recv(3 - len(response))
                if not chunk:
                    break
                response += chunk
            return response == b"OK\n"
    except (OSError, ValueError, SessionError):
        return False


def _serve(endpoint: str, token: str, key: str, stop: threading.Event) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(endpoint)
        os.chmod(endpoint, 0o600)
        server.listen(8)
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                continue
            with connection:
                connection.settimeout(1.0)
                try:
                    reader = connection.makefile("rb")
                    token_line = reader.readline(130).rstrip(b"\r\n").decode("ascii")
                    key_line = reader.readline(130).rstrip(b"\r\n").decode("ascii")
                    valid = (hmac.compare_digest(token_line, token)
                             and hmac.compare_digest(key_line, key))
                    connection.sendall(b"OK\n" if valid else b"NO\n")
                except (OSError, UnicodeError):
                    pass


def _socket_runtime() -> tuple[Path, str]:
    """Create a private broker socket without inheriting a long TMPDIR path."""
    candidates = [Path("/private/tmp"), Path("/tmp"), Path(tempfile.gettempdir())]
    attempted: set[str] = set()
    for base in candidates:
        name = os.fspath(base)
        if name in attempted:
            continue
        attempted.add(name)
        runtime: Path | None = None
        try:
            if not base.is_absolute() or not stat.S_ISDIR(base.stat().st_mode):
                continue
            runtime = Path(tempfile.mkdtemp(prefix=".kdws-", dir=base))
            os.chmod(runtime, 0o700)
            info = runtime.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise OSError("private session directory unavailable")
            endpoint = os.fspath(runtime / "s")
            # sockaddr_un.sun_path is 104 bytes on macOS, including NUL.
            if len(os.fsencode(endpoint)) >= _SOCKET_PATH_LIMIT:
                raise OSError("session socket path too long")
            return runtime, endpoint
        except OSError:
            if runtime is not None:
                try:
                    runtime.rmdir()
                except OSError:
                    pass
    raise SessionError("vault_lock_broker_failed")


def _broker(root: Path, descriptor: int, command: list[str]) -> int:
    runtime, endpoint = _socket_runtime()
    token = secrets.token_hex(32)
    key = _key(root)
    stop = threading.Event()
    server = threading.Thread(target=_serve, args=(endpoint, token, key, stop), daemon=True)
    server.start()
    # bind() must complete before the agent can launch a grandchild check.
    for _ in range(100):
        if Path(endpoint).exists():
            break
        stop.wait(0.01)
    else:
        stop.set()
        raise SessionError("vault_lock_broker_failed")

    env = os.environ.copy()
    env[LOCK_FD_ENV] = str(descriptor)
    env[LOCK_KEY_ENV] = key
    env[LOCK_SOCKET_ENV] = endpoint
    env[LOCK_TOKEN_ENV] = token
    child: subprocess.Popen[bytes] | None = None

    def forward(signum, _frame):
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    old_handlers: dict[int, object] = {}
    try:
        child = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            pass_fds=(descriptor,),
        )
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, forward)
        return child.wait()
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            child.wait()
        stop.set()
        server.join(timeout=1)
        try:
            Path(endpoint).unlink(missing_ok=True)
            runtime.rmdir()
        except OSError:
            pass
        os.close(descriptor)


def _run(root: Path, command: list[str]) -> int:
    """Launch a broker child so killing this launcher does not drop the lock."""
    descriptor = _acquire(root)
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            code = _broker(root, descriptor, command)
        except SessionError as error:
            print(str(error), file=sys.stderr)
            code = 2
        except BaseException:
            print("vault_lock_broker_failed", file=sys.stderr)
            code = 2
        os._exit(code)

    os.close(descriptor)
    old_handlers: dict[int, object] = {}

    def forward(signum, _frame):
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, forward)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在完整编辑会话内持有知识库写锁")
    parser.add_argument("--root", default=None, help="Vault 根目录")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    raw_root = args.root or Path(__file__).parent.parent
    try:
        root = _canonical_root(raw_root)
        command = list(args.command)
        if command and command[0] == "--":
            command.pop(0)
        if command == ["status"]:
            return 0 if session_is_locked(root) else 1
        if not command:
            raise SessionError("command_required")
        return _run(root, command)
    except SessionError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
