"""Crash-safe installation of the manifest-owned Vault kit."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Callable, Iterable

from .wiki_kit import (
    RECEIPT_PATH,
    WikiKitError,
    plan_upgrade,
    receipt_bytes,
    source_file_bytes,
    install_path_allowed,
    verify_installed_kit,
    verify_source_kit,
)
from .wiki_lock import VaultWriteLock, WikiLockError, canonical_vault, session_fd_holds_lock, vault_key
from .worker_lifecycle import AdmissionError


class WikiKitInstallError(RuntimeError):
    """A fixed-code installation or recovery failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class KitStatus:
    state: str
    kit_version: str | None
    action: str | None
    error_code: str | None = None


@dataclass(frozen=True)
class KitInstallResult:
    state: str
    kit_version: str | None
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileMutation:
    relative: str
    content: bytes
    mode: int = 0o644
    expected_before: str | None | object = None


_event = lambda _name, _path: None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: str) -> str:
    if not isinstance(value, str):
        raise WikiKitInstallError("install_path_invalid")
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts
            or "\\" in value or "\x00" in value
            or any(part in ("", ".", "..") for part in path.parts)):
        raise WikiKitInstallError("install_path_invalid")
    return path.as_posix()


def _path(root: Path, relative: str, *, create_parents: bool = False) -> Path:
    current = root
    parts = PurePosixPath(_relative(relative)).parts
    for index, part in enumerate(parts[:-1]):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create_parents:
                return current.joinpath(*parts[index + 1:])
            current.mkdir(mode=0o755)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WikiKitInstallError("install_symlink")
    target = current / parts[-1]
    try:
        if stat.S_ISLNK(target.lstat().st_mode):
            raise WikiKitInstallError("install_symlink")
    except FileNotFoundError:
        pass
    return target


def _read(root: Path, relative: str) -> tuple[bytes, int] | None:
    target = _path(root, relative)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WikiKitInstallError("install_read_failed") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise WikiKitInstallError("install_target_invalid")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise WikiKitInstallError("install_read_changed")
        return b"".join(chunks), stat.S_IMODE(before.st_mode)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(root: Path, relative: str, content: bytes, mode: int) -> None:
    target = _path(root, relative, create_parents=True)
    fd, name = tempfile.mkstemp(prefix=".kd-write-", dir=target.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_dir(target.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _private_root(value: Path | str) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    current = Path(path.anchor)
    missing = []
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            continue
        if missing or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WikiKitInstallError("journal_invalid")
    try:
        for directory in missing:
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        os.chmod(path, 0o700)
    except OSError as error:
        raise WikiKitInstallError("journal_invalid") from error
    try:
        return canonical_vault(path)
    except WikiLockError as error:
        raise WikiKitInstallError("journal_invalid") from error


def _journal_file(journal_root: Path) -> Path:
    return journal_root / "journal.json"


def journal_pending(journal_root: Path | str) -> bool:
    root = Path(journal_root)
    try:
        info = root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return True
        return any(root.iterdir())
    except FileNotFoundError:
        return False


def _write_private(path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_journal(journal_root: Path, allowed_kinds: frozenset[str]) -> dict:
    target = _journal_file(journal_root)
    try:
        if stat.S_ISLNK(target.lstat().st_mode):
            raise WikiKitInstallError("journal_invalid")
        data = json.loads(target.read_bytes())
    except WikiKitInstallError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WikiKitInstallError("journal_invalid") from error
    if (not isinstance(data, dict)
            or set(data) != {"version", "kind", "state", "vault_key", "items"}
            or data.get("version") != 1
            or data.get("state") not in {"backing_up", "prepared", "committed"}
            or data.get("kind") not in allowed_kinds):
        raise WikiKitInstallError("journal_invalid")
    if not isinstance(data.get("items"), list) or not isinstance(data.get("vault_key"), str):
        raise WikiKitInstallError("journal_invalid")
    seen_paths = set()
    for row in data["items"]:
        if (not isinstance(row, dict) or set(row) != {
                "path", "before", "before_mode", "after", "after_mode", "backup"}):
            raise WikiKitInstallError("journal_invalid")
        relative = _relative(row.get("path"))
        if relative in seen_paths:
            raise WikiKitInstallError("journal_invalid")
        seen_paths.add(relative)
        valid_hash = lambda value: (isinstance(value, str) and len(value) == 64
                                    and all(char in "0123456789abcdef" for char in value))
        if ((row["before"] is not None and not valid_hash(row["before"]))
                or not valid_hash(row["after"])
                or (row["before_mode"] is not None and not isinstance(row["before_mode"], int))
                or not isinstance(row["after_mode"], int)
                or not 0 <= row["after_mode"] <= 0o777
                or (row["before_mode"] is not None and not 0 <= row["before_mode"] <= 0o777)):
            raise WikiKitInstallError("journal_invalid")
        backup = row["backup"]
        if backup is not None and (not isinstance(backup, str)
                                   or not backup.startswith("backup-")
                                   or not backup[7:].isdigit()):
            raise WikiKitInstallError("journal_invalid")
        if ((row["before"] is None) != (backup is None)
                or (row["before"] is None) != (row["before_mode"] is None)):
            raise WikiKitInstallError("journal_invalid")
        if data["kind"] == "wiki-style-install" and relative not in {
                ".obsidian/snippets/kd-wiki.css", ".kd/wiki-style.json"}:
            raise WikiKitInstallError("journal_invalid")
        if data["kind"] == "wiki-style-appearance" and relative != ".obsidian/appearance.json":
            raise WikiKitInstallError("journal_invalid")
        if (data["kind"] == "wiki-kit" and relative != RECEIPT_PATH.as_posix()
                and not install_path_allowed(relative)):
            raise WikiKitInstallError("journal_invalid")
    return data


def _cleanup(journal_root: Path, data: dict) -> None:
    allowed = {"journal.json"}
    for row in data["items"]:
        backup = row.get("backup")
        if backup:
            allowed.add(backup)
    actual = {p.name for p in journal_root.iterdir()}
    if not all(name in allowed or name.startswith(".journal-") for name in actual):
        raise WikiKitInstallError("journal_invalid")
    for name in actual:
        target = journal_root / name
        try:
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WikiKitInstallError("journal_invalid")
            target.unlink()
        except WikiKitInstallError:
            raise
        except OSError as error:
            raise WikiKitInstallError("journal_invalid") from error
    _fsync_dir(journal_root)


def _read_backup(path: Path, expected: str) -> bytes:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WikiKitInstallError("journal_invalid")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise WikiKitInstallError("journal_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            content = b""
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                content += chunk
        finally:
            os.close(fd)
    except WikiKitInstallError:
        raise
    except OSError as error:
        raise WikiKitInstallError("journal_invalid") from error
    if _sha(content) != expected:
        raise WikiKitInstallError("journal_invalid")
    return content


def _assert_lock(vault: Path, lock: VaultWriteLock) -> None:
    if lock.vault != vault or not session_fd_holds_lock(vault, lock.descriptor):
        raise WikiKitInstallError("vault_lock_required")


def apply_transaction(vault: Path | str, journal_root: Path | str,
                      mutations: Iterable[FileMutation], *, lock: VaultWriteLock,
                      kind: str, verify: Callable[[], None] | None = None) -> tuple[str, ...]:
    root = canonical_vault(vault)
    _assert_lock(root, lock)
    private = _private_root(journal_root)
    if journal_pending(private):
        raise WikiKitInstallError("recovery_required")
    rows, prepared = [], list(mutations)
    seen = set()
    for index, item in enumerate(prepared):
        relative = _relative(item.relative)
        if relative in seen:
            raise WikiKitInstallError("install_path_invalid")
        seen.add(relative)
        existing = _read(root, relative)
        before = existing[0] if existing else None
        before_mode = existing[1] if existing else None
        before_sha = _sha(before) if before is not None else None
        if before_sha != item.expected_before:
            raise WikiKitInstallError("install_conflict")
        if before == item.content and before_mode == item.mode:
            continue
        backup = None
        if before is not None:
            backup = f"backup-{index:04d}"
        rows.append({"path": relative, "before": before_sha,
                     "before_mode": before_mode, "after": _sha(item.content),
                     "after_mode": item.mode, "backup": backup})
    if not rows:
        return ()
    data = {"version": 1, "kind": kind, "state": "backing_up", "vault_key": vault_key(root),
            "items": rows}
    _write_private(_journal_file(private),
                   (json.dumps(data, sort_keys=True) + "\n").encode())
    for index, item in enumerate(prepared):
        relative = _relative(item.relative)
        row = next((candidate for candidate in rows if candidate["path"] == relative), None)
        if row is not None and row["backup"] is not None:
            current = _read(root, relative)
            if current is None or _sha(current[0]) != row["before"]:
                raise WikiKitInstallError("install_conflict")
            _write_private(private / row["backup"], current[0])
    data["state"] = "prepared"
    _write_private(_journal_file(private),
                   (json.dumps(data, sort_keys=True) + "\n").encode())
    content_by_path = {_relative(item.relative): item.content for item in prepared}
    try:
        for row in rows:
            current = _read(root, row["path"])
            current_sha = _sha(current[0]) if current else None
            if current_sha != row["before"]:
                raise WikiKitInstallError("install_conflict")
            _atomic_write(root, row["path"], content_by_path[row["path"]], row["after_mode"])
            _event("after_replace", row["path"])
        for row in rows:
            current = _read(root, row["path"])
            if current is None or _sha(current[0]) != row["after"]:
                raise WikiKitInstallError("install_verify_failed")
        if verify is not None:
            verify()
        data["state"] = "committed"
        _write_private(_journal_file(private),
                       (json.dumps(data, sort_keys=True) + "\n").encode())
        _event("after_commit", "")
        _cleanup(private, data)
        return tuple(row["path"] for row in rows)
    except Exception as error:
        try:
            recover_transaction(root, private, lock=lock, allowed_kinds={kind})
        except Exception:
            raise WikiKitInstallError("recovery_required") from error
        if isinstance(error, WikiKitInstallError):
            raise
        raise WikiKitInstallError("install_failed") from error


def recover_transaction(vault: Path | str, journal_root: Path | str, *,
                        lock: VaultWriteLock,
                        allowed_kinds: Iterable[str] = ("wiki-kit", "wiki-style-install",
                                                        "wiki-style-appearance")) -> str:
    root = canonical_vault(vault)
    _assert_lock(root, lock)
    private = _private_root(journal_root)
    if not _journal_file(private).exists() and not _journal_file(private).is_symlink():
        names = tuple(path.name for path in private.iterdir())
        if names and all(name.startswith(".journal-") for name in names):
            for name in names:
                target = private / name
                info = target.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise WikiKitInstallError("journal_invalid")
                target.unlink()
            _fsync_dir(private)
            return "recovered"
        raise WikiKitInstallError("journal_invalid")
    data = _load_journal(private, frozenset(allowed_kinds))
    if data["vault_key"] != vault_key(root):
        raise WikiKitInstallError("journal_invalid")
    if data["state"] == "backing_up":
        _cleanup(private, data)
        return "recovered"
    currents = []
    for row in data["items"]:
        relative = _relative(row.get("path"))
        current = _read(root, relative)
        digest = _sha(current[0]) if current else None
        if digest not in {row.get("before"), row.get("after")}:
            raise WikiKitInstallError("recovery_conflict")
        currents.append((row, digest))
    if data["state"] == "committed":
        if any(digest != row["after"] for row, digest in currents):
            raise WikiKitInstallError("recovery_conflict")
        _cleanup(private, data)
        return "committed"
    for row, digest in reversed(currents):
        now = _read(root, row["path"])
        now_digest = _sha(now[0]) if now else None
        if now_digest != digest:
            raise WikiKitInstallError("recovery_conflict")
        if digest == row["before"]:
            continue
        if row["before"] is None:
            target = _path(root, row["path"])
            target.unlink()
            _fsync_dir(target.parent)
        else:
            backup = private / row["backup"]
            content = _read_backup(backup, row["before"])
            _atomic_write(root, row["path"], content, row["before_mode"])
    _cleanup(private, data)
    return "recovered"


class _MutationService:
    def __init__(self, runtime_root, gate, coordinator, namespace: str):
        self.runtime_root = _private_root(Path(runtime_root) / namespace)
        self.gate = gate
        self.coordinator = coordinator

    def journal_root(self, vault: Path | str) -> Path:
        return self.runtime_root / vault_key(vault)

    def mutate(self, vault, operation):
        try:
            formal = canonical_vault(vault)
            if (self.runtime_root == formal or self.runtime_root in formal.parents
                    or formal in self.runtime_root.parents):
                raise WikiKitInstallError("runtime_overlap")
            with self.gate.enter():
                if not self.coordinator.reserve_for_update():
                    raise WikiKitInstallError("operation_busy")
                try:
                    with VaultWriteLock.acquire(vault) as lock:
                        return operation(lock)
                finally:
                    self.coordinator.release_update()
        except WikiKitInstallError:
            raise
        except WikiKitError as error:
            raise WikiKitInstallError(str(error)) from error
        except AdmissionError as error:
            raise WikiKitInstallError("update_reserved") from error
        except BlockingIOError as error:
            raise WikiKitInstallError("vault_busy") from error
        except (WikiLockError, OSError) as error:
            raise WikiKitInstallError("install_failed") from error


class WikiKitInstaller(_MutationService):
    def __init__(self, kit_root, runtime_root, gate, coordinator):
        super().__init__(runtime_root, gate, coordinator, "wiki-kit-install")
        self.kit_root = Path(kit_root)

    def status(self, vault) -> KitStatus:
        try:
            if journal_pending(self.journal_root(vault)):
                return KitStatus("recovery_required", None, "recover", "recovery_required")
            desired = verify_source_kit(self.kit_root)
            try:
                current, actions = plan_upgrade(vault, self.kit_root)
            except WikiKitError as error:
                return KitStatus("conflict", desired.kit_version, None, str(error))
            from .wiki_kit import read_receipt
            receipt = read_receipt(vault)
            if receipt is None:
                if any(action.expected_before is not None for action in actions):
                    return KitStatus("update_available", current.kit_version, "repair")
                return KitStatus("missing", current.kit_version, "install")
            if actions or receipt.manifest_sha256 != current.manifest_sha256:
                return KitStatus("update_available", current.kit_version, "repair")
            return KitStatus("ready", current.kit_version, None)
        except (WikiKitError, WikiKitInstallError, WikiLockError) as error:
            return KitStatus("conflict", None, None, str(error))
        except OSError:
            return KitStatus("conflict", None, None, "kit_read_failed")

    def install(self, vault) -> KitInstallResult:
        def operation(lock):
            desired, actions = plan_upgrade(vault, self.kit_root)
            from .wiki_kit import read_receipt
            receipt = read_receipt(vault)
            adopting_unreceipted = (
                receipt is None
                and any(action.expected_before is not None for action in actions)
            )
            mutations = [FileMutation(
                x.install_path,
                source_file_bytes(self.kit_root,
                                  next(i for i in desired.files if i.install_path == x.install_path)),
                0o644,
                x.expected_before)
                for x in actions]
            wanted_receipt = receipt_bytes(desired)
            current_receipt = _read(canonical_vault(vault), RECEIPT_PATH.as_posix())
            if current_receipt is None or current_receipt[0] != wanted_receipt:
                mutations.append(FileMutation(
                    RECEIPT_PATH.as_posix(), wanted_receipt, 0o600,
                    _sha(current_receipt[0]) if current_receipt else None))
            changed = apply_transaction(
                vault, self.journal_root(vault), mutations, lock=lock, kind="wiki-kit",
                verify=lambda: verify_installed_kit(vault, desired))
            state = ("unchanged" if not changed else
                     "updated" if receipt is not None or adopting_unreceipted else "installed")
            return KitInstallResult(state, desired.kit_version, changed)
        return self.mutate(vault, operation)

    def recover(self, vault) -> KitInstallResult:
        def operation(lock):
            state = recover_transaction(vault, self.journal_root(vault), lock=lock,
                                        allowed_kinds={"wiki-kit"})
            from .wiki_kit import read_receipt
            receipt = read_receipt(vault)
            return KitInstallResult(state, receipt.kit_version if receipt else None)
        return self.mutate(vault, operation)
