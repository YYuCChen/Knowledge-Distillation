"""Private staging Vault construction and validation for V3 wiki tasks."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
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
