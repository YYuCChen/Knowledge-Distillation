"""Recoverable, per-file publication from an isolated staging Vault."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping
import uuid

from .wiki_lock import (
    VaultWriteLock,
    WikiLockError,
    canonical_vault,
    session_fd_holds_lock,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_JOURNAL_NAME = "journal.json"
_JOURNAL_VERSION = 1
_TEMPORARY_NAME = re.compile(r"\.kd-publish-[0-9a-f]{32}\.tmp")


class WikiPublishError(RuntimeError):
    """A publication failure carrying a stable, non-content error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PublishExpectation:
    before_sha256: str | None
    after_sha256: str


class PublishState(StrEnum):
    COMMITTED = "committed"
    RECOVERED = "recovered"
    RECOVERY_FAILED = "recovery_failed"


@dataclass(frozen=True)
class PublishResult:
    state: PublishState
    paths: tuple[str, ...]
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FileSnapshot:
    content: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class _PreparedItem:
    relative: PurePosixPath
    before: _FileSnapshot | None
    after: _FileSnapshot


def _event(_name: str, _relative_path: str) -> None:
    """Fault-injection seam used by deterministic interruption tests."""


def _validate_digest(value: str | None, *, optional: bool) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WikiPublishError("manifest_digest_invalid")


def _relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise WikiPublishError("publish_path_invalid")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise WikiPublishError("publish_path_invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WikiPublishError("publish_path_invalid")
    relative = PurePosixPath(value)
    allowed = (
        (len(relative.parts) >= 2 and relative.parts[0] == "wiki")
        or value in {
            ".graph/graph.json",
            ".graph/state.json",
            ".graph/检查结果.md",
        }
    )
    if not allowed:
        raise WikiPublishError("publish_path_not_allowed")
    return relative


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_parent(
    root: Path,
    relative: PurePosixPath,
    *,
    create: bool,
) -> int:
    descriptor = os.open(root, _directory_flags())
    try:
        for part in relative.parts[:-1]:
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_snapshot(root: Path, relative: PurePosixPath) -> _FileSnapshot | None:
    try:
        parent = _open_parent(root, relative, create=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WikiPublishError("publish_path_unsafe") from error
    try:
        return _read_snapshot_at(parent, relative.name)
    except OSError as error:
        raise WikiPublishError("publish_path_unsafe") from error
    finally:
        os.close(parent)


def _read_snapshot_at(parent: int, name: str) -> _FileSnapshot | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WikiPublishError("publish_path_unsafe")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise WikiPublishError("publish_path_changed")
        return _FileSnapshot(b"".join(chunks), stat.S_IMODE(before.st_mode))
    finally:
        os.close(descriptor)


def _current_digest(root: Path, relative: PurePosixPath) -> str | None:
    snapshot = _read_snapshot(root, relative)
    return None if snapshot is None else snapshot.sha256


def _missing_parent_directories(root: Path, relative: PurePosixPath) -> tuple[str, ...]:
    missing: list[str] = []
    descriptor = os.open(root, _directory_flags())
    prefix: list[str] = []
    absent = False
    try:
        for part in relative.parts[:-1]:
            prefix.append(part)
            if absent:
                missing.append("/".join(prefix))
                continue
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                absent = True
                missing.append("/".join(prefix))
                continue
            os.close(descriptor)
            descriptor = child
        return tuple(missing)
    except OSError as error:
        raise WikiPublishError("publish_path_unsafe") from error
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, _directory_flags())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(root: Path, payload: dict[str, object]) -> None:
    temporary = root / f".{_JOURNAL_NAME}.{uuid.uuid4().hex}.tmp"
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        _write_private_file(temporary, content)
        os.replace(temporary, root / _JOURNAL_NAME)
        os.chmod(root / _JOURNAL_NAME, 0o600, follow_symlinks=False)
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_journal_root(value: Path | str) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise WikiPublishError("journal_path_unsafe")
    else:
        parent_descriptor = -1
        try:
            parent = canonical_vault(path.parent)
            parent_descriptor = os.open(parent, _directory_flags())
            os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
        except (OSError, WikiLockError) as error:
            raise WikiPublishError("journal_path_unsafe") from error
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
    try:
        root = canonical_vault(path)
        os.chmod(root, 0o700, follow_symlinks=False)
    except (OSError, WikiLockError) as error:
        raise WikiPublishError("journal_path_unsafe") from error
    return root


def _existing_journal_root(value: Path | str) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise WikiPublishError("journal_path_unsafe")
        return canonical_vault(path)
    except (OSError, WikiLockError) as error:
        raise WikiPublishError("journal_path_unsafe") from error


def _vault_root(value: Path | str, code: str) -> Path:
    try:
        return canonical_vault(value)
    except WikiLockError as error:
        raise WikiPublishError(code) from error


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_roots(target: Path, staging: Path, journal: Path) -> None:
    roots = (target, staging, journal)
    if any(_paths_overlap(roots[index], roots[other])
           for index in range(len(roots)) for other in range(index + 1, len(roots))):
        raise WikiPublishError("publish_roots_overlap")


def _validate_lock(target: Path, lock: VaultWriteLock) -> None:
    if (
        not isinstance(lock, VaultWriteLock)
        or lock.descriptor < 0
        or lock.vault != target
        or not session_fd_holds_lock(target, lock.descriptor)
    ):
        raise WikiPublishError("vault_lock_invalid")


def _atomic_install(
    root: Path,
    relative: PurePosixPath,
    content: bytes,
    *,
    mode: int,
    must_be_absent: bool,
    expected_current: str | None,
    temporary: str,
    event_prefix: str | None,
) -> None:
    parent = _open_parent(root, relative, create=True)
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if event_prefix is not None:
            _event(f"{event_prefix}after_temp_fsync", relative.as_posix())
            _event(f"{event_prefix}before_replace", relative.as_posix())
        current = _read_snapshot_at(parent, relative.name)
        current_digest = None if current is None else current.sha256
        if current_digest != expected_current:
            raise WikiPublishError("target_digest_changed")
        if must_be_absent:
            os.link(
                temporary,
                relative.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            if event_prefix is not None:
                _event(f"{event_prefix}after_replace", relative.as_posix())
            os.unlink(temporary, dir_fd=parent)
        else:
            os.replace(
                temporary,
                relative.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            if event_prefix is not None:
                _event(f"{event_prefix}after_replace", relative.as_posix())
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _remove_if_digest(root: Path, relative: PurePosixPath, expected: str) -> bool:
    parent = _open_parent(root, relative, create=False)
    try:
        current = _read_snapshot_at(parent, relative.name)
        if current is None or current.sha256 != expected:
            return False
        os.unlink(relative.name, dir_fd=parent)
        os.fsync(parent)
        return True
    finally:
        os.close(parent)


def _manifest(
    staging: Path,
    target: Path,
    expected: Mapping[str, PublishExpectation],
) -> tuple[tuple[_PreparedItem, ...], tuple[str, ...]]:
    if not isinstance(expected, Mapping) or not expected:
        raise WikiPublishError("manifest_empty")
    prepared: list[_PreparedItem] = []
    created: set[str] = set()
    normalized: set[str] = set()
    supplied_items = list(expected.items())
    if any(not isinstance(item[0], str) for item in supplied_items):
        raise WikiPublishError("manifest_invalid")
    for supplied, expectation in sorted(supplied_items, key=lambda item: item[0]):
        relative = _relative_path(supplied)
        key = relative.as_posix()
        if key in normalized or not isinstance(expectation, PublishExpectation):
            raise WikiPublishError("manifest_invalid")
        normalized.add(key)
        _validate_digest(expectation.before_sha256, optional=True)
        _validate_digest(expectation.after_sha256, optional=False)
        staged = _read_snapshot(staging, relative)
        if staged is None:
            raise WikiPublishError("deletion_not_supported")
        if staged.sha256 != expectation.after_sha256:
            raise WikiPublishError("staging_digest_changed")
        current = _read_snapshot(target, relative)
        current_digest = None if current is None else current.sha256
        if current_digest != expectation.before_sha256:
            raise WikiPublishError("target_digest_changed")
        prepared.append(_PreparedItem(relative, current, staged))
        created.update(_missing_parent_directories(target, relative))
    ordered_dirs = tuple(sorted(created, key=lambda value: (value.count("/"), value)))
    return tuple(prepared), ordered_dirs


def _journal_payload(
    target: Path,
    prepared: tuple[_PreparedItem, ...],
    created_dirs: tuple[str, ...],
) -> dict[str, object]:
    return {
        "version": _JOURNAL_VERSION,
        "target_key": hashlib.sha256(os.fsencode(target)).hexdigest(),
        "state": "publishing",
        "created_dirs": list(created_dirs),
        "items": [
            {
                "path": item.relative.as_posix(),
                "before": None if item.before is None else item.before.sha256,
                "after": item.after.sha256,
                "before_mode": None if item.before is None else item.before.mode,
                "backup": None if item.before is None else f"{index:04d}.before",
                "temporary": f".kd-publish-{uuid.uuid4().hex}.tmp",
                "status": "pending",
            }
            for index, item in enumerate(prepared)
        ],
    }


def _create_backups(root: Path, prepared: tuple[_PreparedItem, ...]) -> None:
    if not any(item.before is not None for item in prepared):
        return
    backups = root / "backups"
    backups.mkdir(mode=0o700)
    os.chmod(backups, 0o700, follow_symlinks=False)
    for index, item in enumerate(prepared):
        if item.before is not None:
            _write_private_file(backups / f"{index:04d}.before", item.before.content)
    _fsync_directory(backups)
    _fsync_directory(root)


def publish_wiki(
    target_vault: Path | str,
    staging_vault: Path | str,
    journal_root: Path | str,
    expected: Mapping[str, PublishExpectation],
    *,
    lock: VaultWriteLock,
) -> PublishResult:
    """Publish a pre-hashed staging manifest and durably record every file."""

    target = _vault_root(target_vault, "target_vault_unsafe")
    staging = _vault_root(staging_vault, "staging_vault_unsafe")
    journal_path = Path(os.path.abspath(os.fspath(journal_root)))
    if _paths_overlap(target, staging) or any(
        _paths_overlap(root, journal_path) for root in (target, staging)
    ):
        raise WikiPublishError("publish_roots_overlap")
    _validate_lock(target, lock)
    journal = _prepare_journal_root(journal_path)
    _validate_roots(target, staging, journal)
    if any(journal.iterdir()):
        raise WikiPublishError("journal_exists")

    prepared, created_dirs = _manifest(staging, target, expected)
    payload = _journal_payload(target, prepared, created_dirs)
    try:
        _create_backups(journal, prepared)
        _write_journal(journal, payload)
        for index, item in enumerate(prepared):
            path = item.relative.as_posix()
            entry = payload["items"][index]
            assert isinstance(entry, dict)
            entry["status"] = "replacing"
            _write_journal(journal, payload)
            before = None if item.before is None else item.before.sha256
            if _current_digest(target, item.relative) != before:
                raise WikiPublishError("target_digest_changed")
            _atomic_install(
                target,
                item.relative,
                item.after.content,
                mode=0o600 if item.before is None else item.before.mode,
                must_be_absent=item.before is None,
                expected_current=before,
                temporary=entry["temporary"],
                event_prefix="",
            )
            entry["status"] = "replaced"
            _write_journal(journal, payload)

        payload["state"] = "verifying"
        _write_journal(journal, payload)
        for index, item in enumerate(prepared):
            path = item.relative.as_posix()
            _event("before_readback", path)
            if _current_digest(target, item.relative) != item.after.sha256:
                raise WikiPublishError("readback_failed")
            entry = payload["items"][index]
            assert isinstance(entry, dict)
            entry["status"] = "verified"
            _write_journal(journal, payload)
        payload["state"] = PublishState.COMMITTED.value
        _write_journal(journal, payload)
    except WikiPublishError:
        raise
    except OSError as error:
        raise WikiPublishError("publish_io_failed") from error
    return PublishResult(
        PublishState.COMMITTED,
        tuple(item.relative.as_posix() for item in prepared),
    )


def _load_journal(root: Path, target: Path) -> dict[str, object]:
    journal_path = root / _JOURNAL_NAME
    try:
        info = journal_path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise WikiPublishError("journal_invalid")
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise WikiPublishError("journal_invalid") from error
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _JOURNAL_VERSION
        or payload.get("target_key") != hashlib.sha256(os.fsencode(target)).hexdigest()
        or not isinstance(payload.get("items"), list)
        or not isinstance(payload.get("created_dirs"), list)
    ):
        raise WikiPublishError("journal_invalid")
    for entry in payload["items"]:
        if not isinstance(entry, dict):
            raise WikiPublishError("journal_invalid")
        _relative_path(entry.get("path"))
        _validate_digest(entry.get("before"), optional=True)
        _validate_digest(entry.get("after"), optional=False)
        backup = entry.get("backup")
        if backup is not None and (
            not isinstance(backup, str) or PurePosixPath(backup).name != backup
        ):
            raise WikiPublishError("journal_invalid")
        temporary = entry.get("temporary")
        if not isinstance(temporary, str) or _TEMPORARY_NAME.fullmatch(temporary) is None:
            raise WikiPublishError("journal_invalid")
    return payload


def _backup(root: Path, entry: dict[str, object]) -> _FileSnapshot:
    name = entry.get("backup")
    mode = entry.get("before_mode")
    if not isinstance(name, str) or not isinstance(mode, int):
        raise WikiPublishError("journal_invalid")
    path = root / "backups" / name
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise WikiPublishError("journal_invalid")
        content = path.read_bytes()
    except OSError as error:
        raise WikiPublishError("journal_invalid") from error
    snapshot = _FileSnapshot(content, mode)
    if snapshot.sha256 != entry["before"]:
        raise WikiPublishError("journal_invalid")
    return snapshot


def _cleanup_temporary(
    target: Path,
    relative: PurePosixPath,
    temporary: str,
    allowed_digests: set[str],
) -> bool:
    try:
        parent = _open_parent(target, relative, create=False)
    except FileNotFoundError:
        return True
    try:
        snapshot = _read_snapshot_at(parent, temporary)
        if snapshot is None:
            return True
        if snapshot.sha256 not in allowed_digests:
            return False
        os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
        return True
    except (OSError, WikiPublishError):
        return False
    finally:
        os.close(parent)


def _cleanup_created_dirs(target: Path, values: list[object]) -> tuple[str, ...]:
    conflicts: list[str] = []
    directories: list[PurePosixPath] = []
    for value in values:
        if (
            not isinstance(value, str)
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or not (value == ".graph" or value == "wiki" or value.startswith("wiki/"))
        ):
            raise WikiPublishError("journal_invalid")
        directories.append(PurePosixPath(value))
    for relative in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        parent_relative = PurePosixPath(*relative.parts[:-1], ".kd-placeholder")
        try:
            parent = _open_parent(target, parent_relative, create=False)
        except FileNotFoundError:
            continue
        try:
            os.rmdir(relative.name, dir_fd=parent)
            os.fsync(parent)
        except FileNotFoundError:
            pass
        except OSError:
            conflicts.append(relative.as_posix())
        finally:
            os.close(parent)
    return tuple(conflicts)


def recover_wiki(
    target_vault: Path | str,
    journal_root: Path | str,
    *,
    lock: VaultWriteLock,
) -> PublishResult:
    """Rollback an interrupted publication without overwriting later edits."""

    target = _vault_root(target_vault, "target_vault_unsafe")
    journal_path = Path(os.path.abspath(os.fspath(journal_root)))
    if _paths_overlap(target, journal_path):
        raise WikiPublishError("publish_roots_overlap")
    _validate_lock(target, lock)
    journal = _existing_journal_root(journal_path)
    if _paths_overlap(target, journal):
        raise WikiPublishError("publish_roots_overlap")
    payload = _load_journal(journal, target)
    items = payload["items"]
    assert isinstance(items, list)
    paths = tuple(str(entry["path"]) for entry in items)
    if payload.get("state") == PublishState.COMMITTED.value:
        return PublishResult(PublishState.COMMITTED, paths)
    if payload.get("state") == PublishState.RECOVERED.value:
        return PublishResult(PublishState.RECOVERED, paths)

    conflicts: list[str] = []
    try:
        for entry in reversed(items):
            assert isinstance(entry, dict)
            relative = _relative_path(entry["path"])
            before = entry["before"]
            after = entry["after"]
            temporary = entry["temporary"]
            assert isinstance(after, str) and isinstance(temporary, str)
            temporary_digests = {after}
            if isinstance(before, str):
                temporary_digests.add(before)
            temporary_conflict = not _cleanup_temporary(
                target, relative, temporary, temporary_digests
            )
            if temporary_conflict:
                temporary_path = (relative.parent / temporary).as_posix()
                conflicts.append(temporary_path)
            try:
                current = _current_digest(target, relative)
            except WikiPublishError:
                conflicts.append(relative.as_posix())
                entry["status"] = "recovery_conflict"
                _write_journal(journal, payload)
                continue
            if current == before:
                entry["status"] = (
                    "recovery_conflict" if temporary_conflict else "rolled_back"
                )
                _write_journal(journal, payload)
                continue
            if current != after:
                conflicts.append(relative.as_posix())
                entry["status"] = "recovery_conflict"
                _write_journal(journal, payload)
                continue

            _event("before_recover", relative.as_posix())
            if _current_digest(target, relative) != after:
                conflicts.append(relative.as_posix())
                entry["status"] = "recovery_conflict"
                _write_journal(journal, payload)
                continue
            if before is None:
                if not _remove_if_digest(target, relative, after):
                    conflicts.append(relative.as_posix())
                    entry["status"] = "recovery_conflict"
                    _write_journal(journal, payload)
                    continue
            else:
                backup = _backup(journal, entry)
                try:
                    _atomic_install(
                        target,
                        relative,
                        backup.content,
                        mode=backup.mode,
                        must_be_absent=False,
                        expected_current=after,
                        temporary=temporary,
                        event_prefix="recovery_",
                    )
                except WikiPublishError as error:
                    if error.code != "target_digest_changed":
                        raise
                    conflicts.append(relative.as_posix())
                    entry["status"] = "recovery_conflict"
                    _write_journal(journal, payload)
                    continue
            entry["status"] = (
                "recovery_conflict" if temporary_conflict else "rolled_back"
            )
            _write_journal(journal, payload)

        if not conflicts:
            created = payload["created_dirs"]
            assert isinstance(created, list)
            conflicts.extend(_cleanup_created_dirs(target, created))
        payload["state"] = (
            PublishState.RECOVERY_FAILED.value
            if conflicts
            else PublishState.RECOVERED.value
        )
        _write_journal(journal, payload)
    except WikiPublishError:
        raise
    except OSError as error:
        raise WikiPublishError("recovery_io_failed") from error
    return PublishResult(
        PublishState.RECOVERY_FAILED if conflicts else PublishState.RECOVERED,
        paths,
        tuple(conflicts),
    )
