"""Private staging Vault construction and validation for V3 wiki tasks."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Iterable
import uuid

from .wiki_kit import WikiKitError, read_receipt, verify_installed_kit, verify_source_kit
from .wiki_kit_runtime import WikiKitRuntime
from .wiki_lock import WikiLockError, VaultWriteLock, canonical_vault, session_fd_holds_lock
from .wiki_tasks import FrozenRaw, WikiTaskError, scan_workflow_protocol


REGENERABLE_GRAPH = frozenset({
    ".graph/graph.json",
    ".graph/state.json",
    ".graph/检查结果.md",
})
_TASK_ID = frozenset("0123456789abcdef")
_SHA256 = frozenset("0123456789abcdef")


class WikiStagingError(RuntimeError):
    """A fixed-code staging failure safe to persist."""


@dataclass(frozen=True)
class SnapshotFile:
    relative_path: str
    role: str
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class StagingSnapshot:
    task_id: str
    task_root: Path
    workspace: Path
    control: Path
    backup: Path
    files: tuple[SnapshotFile, ...]
    pending_before: tuple[str, ...]


@dataclass(frozen=True)
class StagedChange:
    relative_path: str
    before_sha256: str | None
    after_sha256: str


@dataclass(frozen=True)
class ValidatedBatch:
    task_id: str
    batch_no: int
    staging_vault: Path
    changes: tuple[StagedChange, ...]
    pending_after: tuple[str, ...]
    candidate_count: int
    health_eligible: bool
    health_due: bool
    health_due_reason: str
    last_lint_date: str
    lint_count: int


@dataclass(frozen=True)
class FormalInputCheck:
    late_raw_count: int


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise WikiStagingError("path_invalid")
    return path.as_posix()


def _require_lock(root: Path, lock: VaultWriteLock) -> None:
    try:
        if lock.descriptor < 0 or lock.vault != root or not session_fd_holds_lock(root, lock.descriptor):
            raise WikiStagingError("vault_lock_required")
    except OSError as error:
        raise WikiStagingError("vault_lock_required") from error


def _stable_bytes(path: Path, *, changed_code: str) -> bytes:
    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise WikiStagingError("path_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise WikiStagingError(changed_code)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except WikiStagingError:
        raise
    except OSError as error:
        raise WikiStagingError(changed_code) from error


def _tree_files(root: Path, relative_root: str) -> tuple[str, ...]:
    base = root.joinpath(*PurePosixPath(relative_root).parts)
    if not base.exists() and not base.is_symlink():
        return ()
    result: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise WikiStagingError("path_invalid") from error
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise WikiStagingError("path_invalid") from error
            if stat.S_ISLNK(info.st_mode):
                raise WikiStagingError("path_symlink")
            path = Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                result.append(path.relative_to(root).as_posix())
            else:
                raise WikiStagingError("path_invalid")

    if base.is_symlink() or not base.is_dir():
        raise WikiStagingError("path_symlink" if base.is_symlink() else "path_invalid")
    visit(base)
    return tuple(result)


def _all_files(root: Path) -> tuple[str, ...]:
    result: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise WikiStagingError("path_invalid") from error
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise WikiStagingError("path_invalid") from error
            if stat.S_ISLNK(info.st_mode):
                raise WikiStagingError("path_symlink")
            path = Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                result.append(path.relative_to(root).as_posix())
            else:
                raise WikiStagingError("path_invalid")

    visit(root)
    return tuple(result)


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with open(path, "xb") as stream:
        os.chmod(path, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _role(relative: str) -> str:
    if relative.startswith("raw/"):
        return "raw"
    if relative.startswith("wiki/"):
        return "wiki"
    if relative in REGENERABLE_GRAPH:
        return "graph_regenerable"
    if relative.startswith(".graph/"):
        return "graph_protected"
    return "kit"


def _manifest(snapshot: StagingSnapshot) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": snapshot.task_id,
        "files": [file.__dict__ for file in snapshot.files],
        "pending_before": list(snapshot.pending_before),
    }


def _write_manifest(snapshot: StagingSnapshot) -> None:
    target = snapshot.control / "snapshot.json"
    content = json.dumps(_manifest(snapshot), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    temporary = snapshot.control / ".snapshot.json.tmp"
    if temporary.exists():
        temporary.unlink()
    _write_private(temporary, content)
    os.replace(temporary, target)
    os.chmod(target, 0o600)


def _write_current_attempt(task_container: Path, attempt_id: str) -> None:
    target = task_container / "current-attempt"
    temporary = task_container / f".current-attempt.{uuid.uuid4().hex}.tmp"
    _write_private(temporary, (attempt_id + "\n").encode("ascii"))
    os.replace(temporary, target)
    os.chmod(target, 0o600)
    descriptor = os.open(task_container, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_staging_snapshot(task_root: Path | str) -> StagingSnapshot:
    """Load the controller-owned snapshot after a worker restart."""
    task_container = canonical_vault(task_root)
    task_id = task_container.name
    if (task_container.parent.name != "wiki-tasks" or len(task_id) != 32
            or any(char not in _TASK_ID for char in task_id)):
        raise WikiStagingError("staging_layout_invalid")
    try:
        attempt_id = _stable_bytes(
            task_container / "current-attempt", changed_code="validation_failed"
        ).decode("ascii").strip()
    except UnicodeError as error:
        raise WikiStagingError("validation_failed") from error
    if len(attempt_id) != 32 or any(char not in _TASK_ID for char in attempt_id):
        raise WikiStagingError("validation_failed")
    root = canonical_vault(task_container / "attempts" / attempt_id)
    workspace, control, backup = root / "workspace", root / "control", root / "backup"
    try:
        for directory in (workspace, control, backup):
            if directory.is_symlink() or not directory.is_dir():
                raise WikiStagingError("staging_layout_invalid")
        raw = _stable_bytes(control / "snapshot.json", changed_code="validation_failed")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WikiStagingError("validation_failed") from error
    if not isinstance(data, dict) or set(data) != {"version", "task_id", "files", "pending_before"}:
        raise WikiStagingError("validation_failed")
    if data["version"] != 1 or data["task_id"] != task_id:
        raise WikiStagingError("validation_failed")
    rows, pending = data["files"], data["pending_before"]
    if not isinstance(rows, list) or not isinstance(pending, list):
        raise WikiStagingError("validation_failed")
    files: list[SnapshotFile] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"relative_path", "role", "byte_count", "sha256"}:
            raise WikiStagingError("validation_failed")
        relative = _relative(row["relative_path"])
        role, count, digest = row["role"], row["byte_count"], row["sha256"]
        if (relative in seen or role != _role(relative)
                or not isinstance(count, int) or isinstance(count, bool) or count < 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in _SHA256 for char in digest)):
            raise WikiStagingError("validation_failed")
        seen.add(relative)
        files.append(SnapshotFile(relative, role, count, digest))
    if any(not isinstance(value, str) or _relative(value) not in seen for value in pending):
        raise WikiStagingError("validation_failed")
    return StagingSnapshot(task_id, root, workspace, control, backup,
                           tuple(files), tuple(pending))


def prepare_staging(formal_vault: Path | str, runtime_root: Path | str, task_id: str,
                    frozen_raw: Iterable[FrozenRaw], *, python_executable: Path | str,
                    source_kit_root: Path | str, lock: VaultWriteLock,
                    kit_runtime: WikiKitRuntime | None = None) -> StagingSnapshot:
    """Copy the task-start Vault inputs into a new private staging workspace."""
    if len(task_id) != 32 or any(char not in _TASK_ID for char in task_id):
        raise WikiStagingError("task_id_invalid")
    try:
        root = canonical_vault(formal_vault)
        runtime = canonical_vault(runtime_root)
    except WikiLockError as error:
        raise WikiStagingError("staging_layout_invalid") from error
    _require_lock(root, lock)
    try:
        root.relative_to(runtime)
    except ValueError:
        pass
    else:
        raise WikiStagingError("staging_layout_invalid")
    try:
        runtime.relative_to(root)
    except ValueError:
        pass
    else:
        raise WikiStagingError("staging_layout_invalid")

    tasks_root = runtime / "wiki-tasks"
    task_container = tasks_root / task_id
    attempt_id = uuid.uuid4().hex
    attempts_root = task_container / "attempts"
    task_root = attempts_root / attempt_id
    workspace = task_root / "workspace"
    control = task_root / "control"
    backup = task_root / "backup"
    try:
        tasks_root.mkdir(mode=0o700, exist_ok=True)
        if tasks_root.is_symlink() or not tasks_root.is_dir():
            raise WikiStagingError("staging_layout_invalid")
        os.chmod(tasks_root, 0o700)
        task_container.mkdir(mode=0o700, exist_ok=True)
        if task_container.is_symlink() or not task_container.is_dir():
            raise WikiStagingError("staging_layout_invalid")
        os.chmod(task_container, 0o700)
        attempts_root.mkdir(mode=0o700, exist_ok=True)
        if attempts_root.is_symlink() or not attempts_root.is_dir():
            raise WikiStagingError("staging_layout_invalid")
        os.chmod(attempts_root, 0o700)
        task_root.mkdir(mode=0o700)
        os.chmod(task_root, 0o700)
        for directory in (workspace, control, backup):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        try:
            receipt = read_receipt(root)
            if receipt is None:
                raise WikiStagingError("kit_missing")
            verify_installed_kit(root, receipt)
            source_kit = verify_source_kit(source_kit_root)
            if source_kit != receipt:
                raise WikiStagingError("kit_drift")
        except WikiKitError as error:
            raise WikiStagingError("kit_drift") from error
        relative_paths: set[str] = set()
        for prefix in ("raw", "wiki", ".graph"):
            relative_paths.update(_tree_files(root, prefix))
        relative_paths.update(item.install_path for item in receipt.files)
        relative_paths.add(".kd/wiki-kit.json")

        files = []
        for relative in sorted(relative_paths):
            safe = _relative(relative)
            content = _stable_bytes(root.joinpath(*PurePosixPath(safe).parts),
                                    changed_code="input_changed")
            _write_private(workspace.joinpath(*PurePosixPath(safe).parts), content)
            files.append(SnapshotFile(safe, _role(safe), len(content),
                                      hashlib.sha256(content).hexdigest()))

        by_path = {item.relative_path: item for item in files}
        for frozen in frozen_raw:
            item = by_path.get(_relative(frozen.relative_path))
            if (item is None or item.role != "raw" or item.byte_count != frozen.byte_count
                    or item.sha256 != frozen.content_sha256):
                raise WikiStagingError("raw_changed")
        try:
            protocol = scan_workflow_protocol(
                workspace, runtime=(kit_runtime or WikiKitRuntime(
                    source_kit_root, python_executable=python_executable)))
        except WikiTaskError as error:
            raise WikiStagingError("validation_failed") from error
        snapshot = StagingSnapshot(
            task_id, task_root, workspace, control, backup, tuple(files),
            tuple(item.relative_path for item in protocol.pending),
        )
        _write_manifest(snapshot)
        _write_current_attempt(task_container, attempt_id)
        return snapshot
    except BaseException:
        shutil.rmtree(task_root, ignore_errors=True)
        raise


def _current_files(workspace: Path) -> dict[str, SnapshotFile]:
    result = {}
    for relative in _all_files(workspace):
        content = _stable_bytes(workspace.joinpath(*PurePosixPath(relative).parts),
                                changed_code="validation_failed")
        result[relative] = SnapshotFile(relative, _role(relative), len(content),
                                        hashlib.sha256(content).hexdigest())
    return result


def _verified_current_files(snapshot: StagingSnapshot) -> dict[str, SnapshotFile]:
    """Reject any change outside Agent-owned wiki/regenerable outputs."""
    before = {item.relative_path: item for item in snapshot.files}
    current = _current_files(snapshot.workspace)
    if set(before) - set(current):
        raise WikiStagingError("validation_failed")
    for relative, item in current.items():
        previous = before.get(relative)
        allowed = relative.startswith("wiki/") or relative in REGENERABLE_GRAPH
        if previous is None and not allowed:
            raise WikiStagingError("validation_failed")
        if previous is not None and (
            previous.sha256 != item.sha256 or previous.byte_count != item.byte_count
        ):
            if previous.role == "raw":
                raise WikiStagingError("raw_changed")
            if not allowed:
                raise WikiStagingError("validation_failed")
    return current


def verify_staging_protected(snapshot: StagingSnapshot) -> None:
    """Perform the controller-side tree check before any trusted tool execution."""
    _verified_current_files(snapshot)


def validate_staging(snapshot: StagingSnapshot, batch_no: int,
                     batch_raw_paths: Iterable[str], *,
                     python_executable: Path | str,
                     source_kit_root: Path | str,
                     kit_runtime: WikiKitRuntime | None = None) -> ValidatedBatch:
    """Validate one Agent batch and prove it consumed exactly that pending set."""
    if batch_no < 1:
        raise WikiStagingError("batch_invalid")
    batch = {_relative(value) for value in batch_raw_paths}
    if not batch or not batch <= set(snapshot.pending_before):
        raise WikiStagingError("batch_boundary_invalid")
    before = {item.relative_path: item for item in snapshot.files}
    current = _verified_current_files(snapshot)
    changes = []
    for relative, item in current.items():
        previous = before.get(relative)
        allowed = relative.startswith("wiki/") or relative in REGENERABLE_GRAPH
        if previous is None:
            if not allowed:
                raise WikiStagingError("validation_failed")
            changes.append(StagedChange(relative, None, item.sha256))
        elif previous.sha256 != item.sha256 or previous.byte_count != item.byte_count:
            changes.append(StagedChange(relative, previous.sha256, item.sha256))
    try:
        verify_source_kit(source_kit_root)
        protocol = scan_workflow_protocol(
            snapshot.workspace, runtime=(kit_runtime or WikiKitRuntime(
                source_kit_root, python_executable=python_executable)))
    except WikiTaskError as error:
        raise WikiStagingError("validation_failed") from error
    pending_after = {item.relative_path for item in protocol.pending}
    if pending_after != set(snapshot.pending_before) - batch:
        raise WikiStagingError("batch_boundary_invalid")
    return ValidatedBatch(snapshot.task_id, batch_no, snapshot.workspace,
                          tuple(sorted(changes, key=lambda item: item.relative_path)),
                          tuple(sorted(pending_after)), protocol.candidate_count,
                          protocol.health_eligible, protocol.health_due,
                          protocol.health_due_reason, protocol.last_lint_date,
                          protocol.lint_count)


def accept_validated_batch(snapshot: StagingSnapshot,
                           batch: ValidatedBatch) -> StagingSnapshot:
    """Advance the staging baseline only after formal publish and readback."""
    if batch.task_id != snapshot.task_id or batch.staging_vault != snapshot.workspace:
        raise WikiStagingError("batch_invalid")
    current = _current_files(snapshot.workspace)
    accepted = replace(snapshot, files=tuple(sorted(current.values(),
                                                    key=lambda item: item.relative_path)),
                       pending_before=batch.pending_after)
    _write_manifest(accepted)
    return accepted


def verify_formal_inputs(snapshot: StagingSnapshot, formal_vault: Path | str, *,
                         lock: VaultWriteLock) -> FormalInputCheck:
    """Reject stale wiki/graph inputs and changed task-start raw before publish."""
    root = canonical_vault(formal_vault)
    _require_lock(root, lock)
    baseline = {item.relative_path: item for item in snapshot.files}
    current_raw = set(_tree_files(root, "raw"))
    baseline_raw = {path for path, item in baseline.items() if item.role == "raw"}
    for relative in baseline_raw:
        item = baseline[relative]
        content = _stable_bytes(root.joinpath(*PurePosixPath(relative).parts),
                                changed_code="raw_changed")
        if len(content) != item.byte_count or hashlib.sha256(content).hexdigest() != item.sha256:
            raise WikiStagingError("raw_changed")

    for relative, item in baseline.items():
        if item.role != "kit":
            continue
        content = _stable_bytes(root.joinpath(*PurePosixPath(relative).parts),
                                changed_code="publish_conflict")
        if len(content) != item.byte_count or hashlib.sha256(content).hexdigest() != item.sha256:
            raise WikiStagingError("publish_conflict")

    for prefix, roles in (("wiki", {"wiki"}),
                          (".graph", {"graph_regenerable", "graph_protected"})):
        current_paths = set(_tree_files(root, prefix))
        baseline_paths = {path for path, item in baseline.items() if item.role in roles}
        if current_paths != baseline_paths:
            raise WikiStagingError("publish_conflict")
        for relative in baseline_paths:
            item = baseline[relative]
            content = _stable_bytes(root.joinpath(*PurePosixPath(relative).parts),
                                    changed_code="publish_conflict")
            if len(content) != item.byte_count or hashlib.sha256(content).hexdigest() != item.sha256:
                raise WikiStagingError("publish_conflict")
    return FormalInputCheck(late_raw_count=len(current_raw - baseline_raw))


def verify_recovered_publish(snapshot: StagingSnapshot, formal_vault: Path | str,
                             published_after: dict[str, str], *,
                             lock: VaultWriteLock) -> FormalInputCheck:
    """Verify committed journal bytes plus every unchanged inference input."""
    root = canonical_vault(formal_vault)
    _require_lock(root, lock)
    baseline = {item.relative_path: item for item in snapshot.files}
    if not published_after or any(
        path not in baseline and not (path.startswith("wiki/") or path in REGENERABLE_GRAPH)
        for path in published_after
    ):
        raise WikiStagingError("publish_conflict")
    current_raw = set(_tree_files(root, "raw"))
    baseline_raw = {path for path, item in baseline.items() if item.role == "raw"}
    for relative in baseline_raw:
        item = baseline[relative]
        content = _stable_bytes(root.joinpath(*PurePosixPath(relative).parts),
                                changed_code="raw_changed")
        if len(content) != item.byte_count or hashlib.sha256(content).hexdigest() != item.sha256:
            raise WikiStagingError("raw_changed")
    for relative, item in baseline.items():
        if item.role != "kit":
            continue
        content = _stable_bytes(root.joinpath(*PurePosixPath(relative).parts),
                                changed_code="publish_conflict")
        if len(content) != item.byte_count or hashlib.sha256(content).hexdigest() != item.sha256:
            raise WikiStagingError("publish_conflict")
    for prefix, roles in (("wiki", {"wiki"}),
                          (".graph", {"graph_regenerable", "graph_protected"})):
        actual = set(_tree_files(root, prefix))
        expected = {path for path, item in baseline.items() if item.role in roles}
        expected.update(path for path in published_after
                        if path.startswith(prefix + "/"))
        if actual != expected:
            raise WikiStagingError("publish_conflict")
        for relative in expected:
            digest = published_after.get(relative)
            if digest is None:
                digest = baseline[relative].sha256
            content = _stable_bytes(root.joinpath(*PurePosixPath(relative).parts),
                                    changed_code="publish_conflict")
            if hashlib.sha256(content).hexdigest() != digest:
                raise WikiStagingError("publish_conflict")
    return FormalInputCheck(late_raw_count=len(current_raw - baseline_raw))


def cleanup_staging(snapshot: StagingSnapshot) -> None:
    """Remove only this completed attempt, never an older possibly-live attempt."""
    try:
        if (snapshot.task_root.parent.name != "attempts"
                or snapshot.task_root.parent.parent.name != snapshot.task_id
                or len(snapshot.task_root.name) != 32
                or any(char not in _TASK_ID for char in snapshot.task_root.name)):
            raise WikiStagingError("staging_layout_invalid")
        shutil.rmtree(snapshot.task_root)
        task_container = snapshot.task_root.parent.parent
        pointer = task_container / "current-attempt"
        if pointer.exists() and not pointer.is_symlink():
            current = pointer.read_text(encoding="ascii").strip()
            if current == snapshot.task_root.name:
                pointer.unlink()
    except WikiStagingError:
        raise
    except OSError as error:
        raise WikiStagingError("staging_cleanup_failed") from error


# Host evidence only; separate from application stdin/schema and model budgets.
CHECKPOINT_LIMIT = 32 * 1024 * 1024
_PERMIT_KEY = object()


def _checkpoint_bytes(value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(content) > CHECKPOINT_LIMIT:
        raise WikiStagingError('checkpoint_limit')
    return content


def _checkpoint_read(path):
    from .wiki_typed import read_final
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise WikiStagingError('checkpoint_invalid')
            result[key] = value
        return result
    def invalid(_value):
        raise WikiStagingError('checkpoint_invalid')
    content = read_final(path.parent, path.name, limit=CHECKPOINT_LIMIT)
    try:
        value = json.loads(content, object_pairs_hook=pairs, parse_constant=invalid)
        def finite(node, key=None):
            if type(node) is float and (key != 'timeout_seconds' or not math.isfinite(node) or node <= 0):
                raise WikiStagingError('checkpoint_invalid')
            if type(node) is dict:
                for name, child in node.items():
                    finite(child, name)
            elif type(node) is list:
                for child in node:
                    finite(child)
        finite(value)
        if _checkpoint_bytes(value) != content:
            raise WikiStagingError('checkpoint_invalid')
        return value
    except (ValueError, UnicodeError):
        raise WikiStagingError('checkpoint_invalid') from None


def _checkpoint_dir(path):
    from .wiki_typed import _directory
    if not path.exists():
        path.mkdir(mode=0o700)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    _directory(path)
    info = path.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WikiStagingError('checkpoint_path_invalid')
    return path


def _checkpoint_write(path, content):
    from .wiki_typed import _directory, _file_key, read_final
    root = _directory(path.parent)
    identity = (root.stat().st_dev, root.stat().st_ino, root.stat().st_mode)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise WikiStagingError('checkpoint_invalid')
            view = view[written:]
        os.fsync(fd)
        if _file_key(os.fstat(fd)) != _file_key(path.lstat()):
            raise WikiStagingError('checkpoint_path_invalid')
    finally:
        os.close(fd)
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        held = os.fstat(parent)
        if identity != (held.st_dev, held.st_ino, held.st_mode):
            raise WikiStagingError('checkpoint_path_invalid')
        os.fsync(parent)
    finally:
        os.close(parent)
    if read_final(root, path.name, limit=max(CHECKPOINT_LIMIT, len(content))) != content:
        raise WikiStagingError('checkpoint_invalid')


def _checkpoint_tree(snapshot):
    from .wiki_typed import _input_digest
    rows = []
    for path in _all_files(snapshot.workspace):
        sha, size = _input_digest(snapshot.workspace, path)
        rows.append(dict(relative_path=path, role=_role(path), byte_count=size, sha256=sha))
    return rows


def _checkpoint_sources(task, snapshot):
    from .wiki_typed import _input_digest
    for raw in task.raw:
        if _input_digest(snapshot.workspace, raw.relative_path) != (raw.content_sha256, raw.byte_count):
            raise WikiStagingError('raw_changed')


def _copy_checkpoint_before(source, target, expected):
    """Stream original bytes without a Vault-sized allocation or input cap."""
    from .wiki_typed import _directory, _file_key, _input_digest
    parents = [(p, (p.stat().st_dev, p.stat().st_ino, p.stat().st_mode))
               for p in (_directory(source.parent), *source.parent.parents)]
    before = source.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.getuid()):
        raise WikiStagingError('checkpoint_path_invalid')
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    dst = None
    try:
        if _file_key(os.fstat(src)) != _file_key(before):
            raise WikiStagingError('input_changed')
        dst = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        hasher, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(src, min(65536, before.st_size + 1 - size))
            if not chunk:
                break
            size += len(chunk); hasher.update(chunk)
            if size > before.st_size:
                raise WikiStagingError('input_changed')
            view = memoryview(chunk)
            while view:
                written = os.write(dst, view)
                if written <= 0:
                    raise WikiStagingError('checkpoint_invalid')
                view = view[written:]
        if ((hasher.hexdigest(), size) != expected
                or _file_key(os.fstat(src)) != _file_key(before)
                or _file_key(source.lstat()) != _file_key(before)):
            raise WikiStagingError('input_changed')
        os.fsync(dst)
        if _file_key(os.fstat(dst)) != _file_key(target.lstat()):
            raise WikiStagingError('checkpoint_path_invalid')
        for path, key in parents:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or key != (info.st_dev, info.st_ino, info.st_mode):
                raise WikiStagingError('checkpoint_path_invalid')
    finally:
        os.close(src)
        if dst is not None:
            os.close(dst)
    if _input_digest(target.parent, target.name) != expected:
        raise WikiStagingError('input_changed')
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _execution_location(snapshot, runtime_root, task, lock):
    from .wiki_typed import validate_layout
    from .wiki_tasks import _validated_plan
    _require_lock(canonical_vault(task.vault_path), lock)
    _validated_plan(task)
    validate_layout(snapshot, runtime_root, dict(task_id=task.task_id,
        attempt_id=snapshot.task_root.name, batch_no=1,
        boundary_sha256=task.boundary_sha256, input_sha256='0' * 64))
    container = snapshot.task_root.parent.parent
    pointer = _checkpoint_read_pointer(container)
    if pointer != snapshot.task_root.name:
        raise WikiStagingError('checkpoint_attempt_changed')
    return container


def _checkpoint_read_pointer(container):
    from .wiki_typed import read_final
    value = read_final(container, 'current-attempt', limit=33)
    try:
        attempt = value.decode('ascii')
    except UnicodeError:
        raise WikiStagingError('checkpoint_invalid') from None
    if len(attempt) != 33 or attempt[-1] != '\n' or any(c not in _TASK_ID for c in attempt[:-1]):
        raise WikiStagingError('checkpoint_invalid')
    return attempt[:-1]


def bind_task_execution(snapshot, runtime_root, *, task, lock):
    from .wiki_tasks import execution_policy_sha256
    container = _execution_location(snapshot, runtime_root, task, lock)
    root = _checkpoint_dir(container / 'execution')
    record = dict(contract='wiki-task-execution-v1', task_id=task.task_id,
        plan_sha256=task.plan_sha256, execution_policy_sha256=execution_policy_sha256(task),
        boundary_sha256=task.boundary_sha256, kit_manifest_sha256=task.kit_manifest_sha256,
        first_attempt=snapshot.task_root.name)
    path = root / 'binding.json'
    if path.exists() or path.is_symlink():
        if _checkpoint_read(path) != record:
            raise WikiStagingError('checkpoint_binding_changed')
    else:
        _checkpoint_write(path, _checkpoint_bytes(record))
    return root


def preserve_batch_baseline(snapshot, runtime_root, *, task, batch_no, lock):
    root = bind_task_execution(snapshot, runtime_root, task=task, lock=lock)
    if type(batch_no) is not int or not any(b.batch_no == batch_no for b in task.batches):
        raise WikiStagingError('batch_invalid')
    root = _checkpoint_dir(root / f'batch-{batch_no}')
    manifest = _manifest(snapshot)
    path = root / 'baseline.json'
    if path.exists() or path.is_symlink():
        if _checkpoint_read(path) != manifest:
            raise WikiStagingError('checkpoint_baseline_changed')
    else:
        if _checkpoint_tree(snapshot) != manifest['files']:
            raise WikiStagingError('input_changed')
        before_root = _checkpoint_dir(root / 'before')
        for item in snapshot.files:
            target = before_root / item.relative_path
            current = before_root
            for part in PurePosixPath(item.relative_path).parts[:-1]:
                current = _checkpoint_dir(current / part)
            _copy_checkpoint_before(snapshot.workspace / item.relative_path, target,
                                    (item.sha256, item.byte_count))
        _checkpoint_write(path, _checkpoint_bytes(manifest))
    from .wiki_typed import _input_digest
    for item in snapshot.files:
        if _input_digest(root / 'before', item.relative_path) != (item.sha256, item.byte_count):
            raise WikiStagingError('checkpoint_baseline_changed')
    return root


class GenerationPermit:
    """Controller capability, not a certificate against malicious same-UID code."""
    def __init__(self, key, root, reservation, task, snapshot):
        if key is not _PERMIT_KEY:
            raise WikiStagingError('checkpoint_invalid')
        self.root, self.reservation, self.task, self.snapshot = root, reservation, task, snapshot
        self._pid, self._key, self._claimed, self._saved = os.getpid(), key, False, False

    def claim_spawn(self):
        if self._pid != os.getpid() or self._key is not _PERMIT_KEY or self._claimed:
            raise WikiStagingError('generation_already_reserved')
        self._claimed = True


def reserve_generation(snapshot, runtime_root, *, task, batch_no, lock, source_proof,
                       argv, stdin_bytes, schema_bytes, input_binding,
                       recording_call, timeout_seconds, allow_recovery=False):
    from dataclasses import asdict
    from . import wiki_typed as t
    root = preserve_batch_baseline(snapshot, runtime_root, task=task, batch_no=batch_no, lock=lock)
    path = root / 'reservation.json'
    if path.exists() or path.is_symlink():
        if not allow_recovery:
            raise WikiStagingError('generation_already_reserved')
        for number in (2, 3):
            path = root / f'reservation-{number}.json'
            if not path.exists() and not path.is_symlink():
                break
        else:
            raise WikiStagingError('generation_already_reserved')
    if (type(argv) is not tuple or not argv or any(type(a) is not str or '\x00' in a for a in argv)
            or argv.count('-o') != 1 or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= t.GENERATION_TIMEOUT
            or not isinstance(input_binding, t.InputBinding)):
        raise WikiStagingError('checkpoint_invalid')
    index = argv.index('-o')
    if index + 1 >= len(argv):
        raise WikiStagingError('checkpoint_invalid')
    final = Path(argv[index + 1])
    try:
        relative = final.relative_to(snapshot.control).as_posix()
        if relative == '.' or any(p in {'', '.', '..'} for p in relative.split('/')):
            raise ValueError()
        t._directory(final.parent)
    except (ValueError, OSError):
        raise WikiStagingError('checkpoint_path_invalid') from None
    if final.exists() or final.is_symlink() or schema_bytes != t.encoded(t.PROPOSAL_SCHEMA):
        raise WikiStagingError('checkpoint_invalid')
    if type(stdin_bytes) is not bytes or type(schema_bytes) is not bytes:
        raise WikiStagingError('checkpoint_invalid')
    measured = t.admit_input(stdin_bytes, t.PROPOSAL_SCHEMA, None,
        input_policy=t.APPLICATION_UTF8_POLICY, max_application_input_bytes=t.INPUT_LIMIT,
        schema_bytes=schema_bytes)
    if measured != input_binding:
        raise WikiStagingError('checkpoint_binding_changed')
    binding, rows, payload = t.freeze_input(task, snapshot, batch_no, source_proof, runtime_root=runtime_root)
    if not stdin_bytes.endswith(t.encoded(dict(binding=binding, input=payload))):
        raise WikiStagingError('checkpoint_binding_changed')
    _checkpoint_sources(task, snapshot)
    from .wiki_typed import read_final
    recording_call = Path(recording_call)
    recorded = (read_final(recording_call, 'argv.json', limit=65536),
                read_final(recording_call, 'stdin.utf8', limit=t.INPUT_LIMIT),
                read_final(recording_call, 'schema.json', limit=t.INPUT_LIMIT))
    if recorded != (t.encoded(argv), stdin_bytes, schema_bytes):
        raise WikiStagingError('checkpoint_binding_changed')
    record = dict(contract='wiki-generation-reservation-v1', task_id=task.task_id,
        batch_no=batch_no, plan_sha256=task.plan_sha256,
        first_attempt=snapshot.task_root.name, binding=binding, input_binding=asdict(input_binding),
        argv=list(argv), stdin_sha256=t.digest(stdin_bytes), schema_sha256=t.digest(schema_bytes),
        final_relative_path=relative, recording_call=str(recording_call),
        timeout_seconds=timeout_seconds)
    if path.name != 'reservation.json':
        record['generation_attempt'] = number
    try:
        _checkpoint_write(path, _checkpoint_bytes(record))
    except FileExistsError:
        raise WikiStagingError('generation_already_reserved') from None
    return GenerationPermit(_PERMIT_KEY, root, record, task, snapshot)


def save_generation_result(permit, result, *, lock, source_proof):
    from dataclasses import asdict
    from . import wiki_typed as t
    if (not isinstance(permit, GenerationPermit) or permit._key is not _PERMIT_KEY
            or permit._pid != os.getpid() or not permit._claimed or permit._saved
            or not isinstance(result, t.TypedRunnerResult) or not result.succeeded):
        raise WikiStagingError('checkpoint_invalid')
    task, snapshot, record = permit.task, permit.snapshot, permit.reservation
    _require_lock(canonical_vault(task.vault_path), lock)
    runtime_root = snapshot.task_root.parent.parent.parent.parent
    if preserve_batch_baseline(snapshot, runtime_root, task=task, batch_no=record['batch_no'], lock=lock) != permit.root:
        raise WikiStagingError('checkpoint_binding_changed')
    suffix = '' if record.get('generation_attempt', 1) == 1 else '-' + str(record['generation_attempt'])
    if _checkpoint_read(permit.root / f'reservation{suffix}.json') != record:
        raise WikiStagingError('checkpoint_binding_changed')
    if result.input_binding is None or asdict(result.input_binding) != record['input_binding']:
        raise WikiStagingError('checkpoint_binding_changed')
    terminal_root = Path(record['recording_call'])
    terminal = _checkpoint_read(terminal_root / 'terminal.json')
    _verify_generation_terminal(terminal, terminal_root, record)
    if dict(result.usage) != terminal['usage']:
        raise WikiStagingError('checkpoint_binding_changed')
    final = snapshot.control / record['final_relative_path']
    content = t.read_final(final.parent, final.name)
    if result.final_bytes != content or result.final_sha256 != t.digest(content):
        raise WikiStagingError('checkpoint_binding_changed')
    rows = tuple((r, b'') for r in task.raw if r.batch_no == record['batch_no'])
    t.parse_proposal(content, record['binding'], rows)
    _checkpoint_sources(task, snapshot)
    current_binding, _rows, _payload = t.freeze_input(task, snapshot, record['batch_no'],
        source_proof, runtime_root=runtime_root)
    if current_binding != record['binding']:
        raise WikiStagingError('checkpoint_binding_changed')
    tree = _checkpoint_tree(snapshot)
    baseline = {f.relative_path: f for f in snapshot.files}
    for entry in tree:
        if entry['role'] not in {'wiki', 'graph_regenerable'}:
            previous = baseline.get(entry['relative_path'])
            if previous is None or previous.__dict__ != entry:
                raise WikiStagingError('input_changed')
    if set(baseline) - {r['relative_path'] for r in tree}:
        raise WikiStagingError('input_changed')
    _checkpoint_write(permit.root / f'proposal{suffix}.json', content)
    receipt = dict(contract='wiki-generation-result-v1',
        reservation_sha256=t.digest(_checkpoint_bytes(record)), terminal_sha256=t.digest(_checkpoint_bytes(terminal)),
        proposal_sha256=t.digest(content), proposal_bytes=len(content), output_tree=tree,
        input_binding=record['input_binding'])
    _checkpoint_write(permit.root / f'result{suffix}.json', _checkpoint_bytes(receipt))
    permit._saved = True
    return receipt


def _verify_generation_terminal(terminal, root, record, *, schema_definition='proposal',
                                expected_error=None):
    from . import wiki_typed as t
    if (type(terminal) is not dict or terminal.get('contract') != 'g3-exec-recording-v1'
            or terminal.get('reserved') is not True or terminal.get('actual_spawned') is not True
            or type(terminal.get('pid')) is not int or terminal['pid'] <= 0
            or type(terminal.get('returncode')) is not int or terminal['returncode'] != 0
            or terminal.get('error_code') != expected_error
            or any(terminal.get(k) is not False for k in
                   ('cancelled', 'timed_out', 'not_observed_tail', 'truncated_due_to_overflow'))
            or any(terminal.get(k) is not True for k in ('complete_stdout_eof', 'complete_stderr_eof'))
            or type(terminal.get('stdin_size')) is not int
            or type(terminal.get('stdin_written')) is not int
            or terminal['stdin_size'] != record['input_binding']['prompt_bytes']
            or terminal['stdin_written'] != terminal['stdin_size']
            or terminal.get('timeout_seconds') != record['timeout_seconds']):
        raise WikiStagingError('generation_interrupted')
    if (t.read_final(root, 'argv.json', limit=65536) != t.encoded(record['argv'])
            or t.digest(t.read_final(root, 'stdin.utf8', limit=t.INPUT_LIMIT)) != record['stdin_sha256']
            or t.digest(t.read_final(root, 'schema.json', limit=t.INPUT_LIMIT)) != record['schema_sha256']):
        raise WikiStagingError('checkpoint_binding_changed')
    stdin = t.read_final(root, 'stdin.utf8', limit=t.INPUT_LIMIT)
    schema = t.read_final(root, 'schema.json', limit=t.INPUT_LIMIT)
    expected_schema = t.PROPOSAL_SCHEMA if schema_definition == 'proposal' else schema_definition
    measured = t.admit_input(stdin, expected_schema, None,
        input_policy=t.APPLICATION_UTF8_POLICY, schema_bytes=schema, schema_absent=expected_schema is None)
    if schema != (b'' if expected_schema is None else t.encoded(expected_schema)) or measured.__dict__ != record['input_binding']:
        raise WikiStagingError('checkpoint_binding_changed')
    if type(terminal.get('call_id')) is not int or root.name != f"exec-{terminal['call_id']:04d}":
        raise WikiStagingError('checkpoint_invalid')
    usage = terminal.get('usage')
    if (type(usage) is not dict or set(usage) - {'input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'}
            or any(type(v) is not int or v < 0 for v in usage.values())):
        raise WikiStagingError('checkpoint_invalid')
    for tag, filename, limit in (('out', 'stdout.jsonl.raw', t.STDOUT_LIMIT), ('err', 'stderr.raw', t.STDERR_LIMIT)):
        data = t.read_final(root, filename, limit=limit)
        observed, retained = terminal.get('observed_bytes'), terminal.get('retained_bytes')
        if (type(observed) is not dict or type(retained) is not dict
                or type(observed.get(tag)) is not int or type(retained.get(tag)) is not int
                or observed[tag] != len(data) or retained[tag] != len(data)):
            raise WikiStagingError('generation_interrupted')


def load_bound_staging(task_root, runtime_root, *, task, lock):
    from .wiki_typed import _directory
    container = _directory(task_root)
    record = _checkpoint_read(container / 'execution/binding.json')
    if record.get('first_attempt') != _checkpoint_read_pointer(container):
        raise WikiStagingError('checkpoint_attempt_changed')
    snapshot = load_staging_snapshot(container)
    bind_task_execution(snapshot, runtime_root, task=task, lock=lock)
    _checkpoint_sources(task, snapshot)
    return snapshot


def load_generation_checkpoint(task_root, runtime_root, *, task, batch_no, lock, source_proof,
                               allow_regenerated_graph=False):
    from . import wiki_typed as t
    if type(batch_no) is not int or not any(b.batch_no == batch_no for b in task.batches):
        raise WikiStagingError('batch_invalid')
    snapshot = load_bound_staging(task_root, runtime_root, task=task, lock=lock)
    root = snapshot.task_root.parent.parent / 'execution' / f'batch-{batch_no}'
    baseline = _checkpoint_read(root / 'baseline.json')
    if (type(baseline) is not dict or set(baseline) != {'version', 'task_id', 'files', 'pending_before'}
            or type(baseline['version']) is not int or baseline['version'] != 1
            or baseline['task_id'] != task.task_id or type(baseline['files']) is not list):
        raise WikiStagingError('checkpoint_baseline_changed')
    files = []
    for row in baseline['files']:
        if type(row) is not dict or set(row) != {'relative_path', 'role', 'byte_count', 'sha256'}:
            raise WikiStagingError('checkpoint_baseline_changed')
        path = _relative(row['relative_path'])
        if (row['role'] != _role(path) or type(row['byte_count']) is not int
                or row['byte_count'] < 0 or type(row['sha256']) is not str
                or len(row['sha256']) != 64 or any(c not in _SHA256 for c in row['sha256'])
                or t._input_digest(root / 'before', path) != (row['sha256'], row['byte_count'])):
            raise WikiStagingError('checkpoint_baseline_changed')
        files.append(SnapshotFile(**row))
    if (len({f.relative_path for f in files}) != len(files)
            or type(baseline['pending_before']) is not list
            or any(p not in {f.relative_path for f in files} for p in baseline['pending_before'])):
        raise WikiStagingError('checkpoint_baseline_changed')
    snapshot = replace(snapshot, files=tuple(files), pending_before=tuple(baseline['pending_before']))
    suffix = next(('-' + str(n) for n in (3, 2) if (root / f'reservation-{n}.json').exists()), '')
    reservation = _checkpoint_read(root / f'reservation{suffix}.json')
    expected = (task.task_id, task.plan_sha256, snapshot.task_root.name, batch_no)
    if tuple(reservation.get(k) for k in ('task_id', 'plan_sha256', 'first_attempt', 'batch_no')) != expected:
        raise WikiStagingError('checkpoint_binding_changed')
    current_binding, _rows, _payload = t.freeze_input(task, snapshot, batch_no, source_proof,
                                                      runtime_root=runtime_root)
    if current_binding != reservation['binding']:
        raise WikiStagingError('checkpoint_binding_changed')
    if not (root / f'result{suffix}.json').exists():
        raise WikiStagingError('generation_interrupted')
    receipt = _checkpoint_read(root / f'result{suffix}.json')
    terminal_root = Path(reservation['recording_call'])
    terminal = _checkpoint_read(terminal_root / 'terminal.json')
    _verify_generation_terminal(terminal, terminal_root, reservation)
    proposal = t.read_final(root, f'proposal{suffix}.json')
    current_tree = _checkpoint_tree(snapshot)
    if receipt.get('output_tree') != current_tree:
        old = {r['relative_path']: r for r in receipt.get('output_tree', [])}
        current = {r['relative_path']: r for r in current_tree}
        if (not allow_regenerated_graph or not {
                p for p in set(old) | set(current) if old.get(p) != current.get(p)} <= set(REGENERABLE_GRAPH)):
            raise WikiStagingError('checkpoint_binding_changed')
    if (receipt != dict(contract='wiki-generation-result-v1',
            reservation_sha256=t.digest(_checkpoint_bytes(reservation)),
            terminal_sha256=t.digest(_checkpoint_bytes(terminal)), proposal_sha256=t.digest(proposal),
            proposal_bytes=len(proposal), output_tree=receipt['output_tree'],
            input_binding=reservation['input_binding'])):
        raise WikiStagingError('checkpoint_binding_changed')
    rows = tuple((r, b'') for r in task.raw if r.batch_no == batch_no)
    t.parse_proposal(proposal, reservation['binding'], rows)
    return snapshot, proposal, receipt
