"""Cross-process write lock shared by application and manual Vault sessions."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat


class WikiLockError(RuntimeError):
    """A fixed-code Vault lock failure."""


def canonical_vault(vault: Path | str) -> Path:
    path = Path(os.path.abspath(os.fspath(vault)))
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise WikiLockError("vault_symlink")
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise WikiLockError("vault_path_invalid")
    except WikiLockError:
        raise
    except OSError as error:
        raise WikiLockError("vault_path_invalid") from error
    return path


def vault_key(vault: Path | str) -> str:
    return hashlib.sha256(os.fsencode(canonical_vault(vault))).hexdigest()


@dataclass
class VaultWriteLock:
    vault: Path
    descriptor: int

    @classmethod
    def acquire(cls, vault: Path | str) -> VaultWriteLock:
        if os.name != "posix":
            raise WikiLockError("vault_lock_unsupported")
        import fcntl

        path = canonical_vault(vault)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise WikiLockError("vault_lock_failed") from error
        try:
            opened = os.fstat(descriptor)
            current = path.stat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise WikiLockError("vault_path_changed")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError("vault_busy") from error
            raise WikiLockError("vault_lock_failed") from error
        except BaseException:
            os.close(descriptor)
            raise
        return cls(path, descriptor)

    @property
    def key(self) -> str:
        return hashlib.sha256(os.fsencode(self.vault)).hexdigest()

    def make_inheritable(self) -> int:
        if self.descriptor < 0:
            raise WikiLockError("vault_lock_closed")
        os.set_inheritable(self.descriptor, True)
        return self.descriptor

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> VaultWriteLock:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def session_fd_holds_lock(vault: Path | str, descriptor: int) -> bool:
    """Validate the directory fd and ensure this process holds its Vault lock.

    Reapplying ``flock`` succeeds for the inherited open-file description. If a
    caller supplies a newly opened fd while the Vault is unlocked, this call
    acquires the lock on that fd; callers must therefore keep the fd open for
    the complete editing session. Environment variables alone are never proof.
    """
    if os.name != "posix" or descriptor < 0:
        return False
    import fcntl

    try:
        path = canonical_vault(vault)
        opened = os.fstat(descriptor)
        current = path.stat()
        if not stat.S_ISDIR(opened.st_mode):
            return False
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            return False
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, ValueError, WikiLockError):
        return False


# Backward-compatible internal name while the phase-1 modules are landing.
inherited_session_is_locked = session_fd_holds_lock
