"""Exact erasure of registered synthetic copies, never of a product Store/Vault.

The manifest and journal contain identifiers, hashes and states only. Private
directories and a cooperative lock are not a defence against a malicious uid.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable
import uuid

from .reddit_source import DeletionNotice, SourceRef


KINDS = frozenset({"original", "cache", "candidate", "model_response", "staging", "backup", "ai_derived"})
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_CONTROL = ".reddit-control.json"


class RedditErasureError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _event(name: str, artifact_id: str) -> None:
    """Deterministic crash injection seam; does not receive content or paths."""


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise RedditErasureError("identifier_invalid")
    return value


def _ref(ref: SourceRef) -> list[str]:
    _token(ref.version)
    return [ref.source_id, ref.version]


def _refs(items) -> frozenset[SourceRef]:
    try:
        return frozenset(SourceRef(*item) for item in items)
    except (ValueError, TypeError):
        raise RedditErasureError("manifest_refs_invalid") from None


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _digest(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _private(info) -> bool:
    return stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid()


def _named_private_file(parent: int, name: str, fd: int, *, mode: int | None = None,
                        code: str = "file_unsafe"):
    info = os.fstat(fd)
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    for current in (info, named):
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or current.st_uid != os.getuid()
                or mode is not None and stat.S_IMODE(current.st_mode) != mode):
            raise RedditErasureError(code)
    if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
        raise RedditErasureError(code)
    return info


def _validate_lock(parent: int, fd: int):
    return _named_private_file(parent, ".reddit-lock", fd, mode=0o600, code="lock_changed_or_unsafe")


def _read_file(parent: int, name: str, *, required_mode: int | None = None):
    """No links, devices, shared hardlinks or ownership ambiguity."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = _named_private_file(parent, name, fd, mode=required_mode)
        if required_mode is not None:
            _event("journal_before_read", "control")
            _named_private_file(parent, name, fd, mode=required_mode)
        content = bytearray()
        while part := os.read(fd, 65536):
            content.extend(part)
        if required_mode is not None:
            _event("journal_after_read", "control")
        after = _named_private_file(parent, name, fd, mode=required_mode)
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink", "st_mode", "st_uid")
        if any(getattr(before, k) != getattr(after, k) for k in keys):
            raise RedditErasureError("file_changed_during_read")
        return {"dev": after.st_dev, "ino": after.st_ino, "size": after.st_size,
                "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns,
                "sha256": hashlib.sha256(content).hexdigest()}, bytes(content)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class PlanItem:
    artifact_id: str
    action: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ErasurePlan:
    operation_id: str
    plan_sha256: str
    source_refs: frozenset[SourceRef]
    items: tuple[PlanItem, ...]
    coverage_gaps: tuple[str, ...]
    invalidated_refs: tuple[str, ...]


@dataclass(frozen=True)
class ErasureResult:
    operation_id: str
    state: str
    cleared_artifact_ids: tuple[str, ...]
    manual_artifact_ids: tuple[str, ...]
    invalidated_refs: tuple[str, ...]
    coverage_gaps: tuple[str, ...]
    product_complete: bool = False


@dataclass(frozen=True)
class DeletionStop:
    source_refs: frozenset[SourceRef]
    stopped_node_ids: frozenset[str]
    invalidated_refs: tuple[str, ...]
    coverage_gaps: tuple[str, ...]


class SyntheticRoot:
    """One explicitly created disposable root with a recoverable private journal."""

    def __init__(self, path: Path):
        self.path = Path(path)
        # Do not resolve away a symlink supplied by a caller.
        if not self.path.is_absolute():
            raise RedditErasureError("root_absolute_required")
        for ancestor in (self.path,) + tuple(self.path.parents):
            if ancestor.is_symlink():
                raise RedditErasureError("root_symlink")
        try:
            info = self.path.lstat()
            if not _private(info):
                raise RedditErasureError("root_not_private")
            self.identity = (info.st_dev, info.st_ino)
            with self._locked() as (fd, data, lock):
                if data.get("root_identity") != list(self.identity) or data.get("synthetic") is not True:
                    raise RedditErasureError("root_marker_invalid")
        except OSError:
            raise RedditErasureError("root_unavailable") from None

    @classmethod
    def create(cls, path: Path) -> SyntheticRoot:
        path = Path(path)
        if not path.is_absolute() or path.exists() or path.is_symlink():
            raise RedditErasureError("root_must_be_new")
        for ancestor in path.parents:
            if ancestor.is_symlink():
                raise RedditErasureError("root_symlink")
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)
        fd = os.open(path, _directory_flags())
        try:
            info = os.fstat(fd)
            os.mkdir("copies", 0o700, dir_fd=fd)
            lock = os.open(".reddit-lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            lock_info = os.fstat(lock)
            os.close(lock)
            data = {"format": 1, "synthetic": True, "root_id": uuid.uuid4().hex,
                    "root_identity": [info.st_dev, info.st_ino],
                    "lock_identity": [lock_info.st_dev, lock_info.st_ino], "artifacts": {},
                    "stopped_refs": [], "stopped_node_ids": [], "operations": {}}
            cls._save(fd, data)
        finally:
            os.close(fd)
        return cls(path)

    @staticmethod
    def _save(fd: int, data: dict, *, lock_fd: int | None = None) -> None:
        if lock_fd is not None:
            _validate_lock(fd, lock_fd)
        temporary = ".reddit-write-" + uuid.uuid4().hex
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(output, "wb") as stream:
                stream.write(_json(data))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _CONTROL, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
            if lock_fd is not None:
                _validate_lock(fd, lock_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass

    @contextmanager
    def _locked(self):
        fd = lock = None
        try:
            fd = os.open(self.path, _directory_flags())
            info = os.fstat(fd)
            if not _private(info) or (info.st_dev, info.st_ino) != self.identity:
                raise RedditErasureError("root_changed")
            lock = os.open(".reddit-lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            li = _validate_lock(fd, lock)
            _event("lock_before_flock", "control")
            _validate_lock(fd, lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            _event("lock_after_flock", "control")
            _validate_lock(fd, lock)
            _, content = _read_file(fd, _CONTROL, required_mode=0o600)
            data = json.loads(content)
            if data.get("format") != 1 or data.get("synthetic") is not True or data.get("root_identity") != list(self.identity):
                raise RedditErasureError("root_marker_invalid")
            if data.get("lock_identity") != [li.st_dev, li.st_ino]:
                raise RedditErasureError("lock_identity_changed")
            _validate_lock(fd, lock)
            yield fd, data, lock
            _validate_lock(fd, lock)
        except OSError:
            raise RedditErasureError("filesystem_unsafe") from None
        except (ValueError, KeyError, TypeError):
            raise RedditErasureError("journal_invalid") from None
        finally:
            if lock is not None:
                os.close(lock)
            if fd is not None:
                os.close(fd)

    @staticmethod
    @contextmanager
    def _copies(fd: int):
        child = os.open("copies", _directory_flags(), dir_fd=fd)
        try:
            if not _private(os.fstat(child)):
                raise RedditErasureError("copies_not_private")
            yield child
        finally:
            os.close(child)

    def stopped_refs(self) -> frozenset[SourceRef]:
        with self._locked() as (_, data, _lock):
            return _refs(data["stopped_refs"])

    def stopped_node_ids(self) -> frozenset[str]:
        with self._locked() as (_, data, _lock):
            return frozenset(data["stopped_node_ids"])

    def register_artifact(self, *, artifact_id: str, relative_path: str, kind: str,
                          source_refs: frozenset[SourceRef], parent_artifact_ids: tuple[str, ...] = (),
                          owner: str = "unknown", synthetic: bool = False, managed: bool = False,
                          contains_user_content: bool = True, lineage_complete: bool = False,
                          deletion_bound_refs: frozenset[SourceRef] | None = None) -> None:
        artifact_id = _token(artifact_id)
        # Restrict even stored paths to identifiers; receipt cannot leak a title/URL.
        if relative_path != f"copies/{artifact_id}.bin":
            raise RedditErasureError("artifact_path_invalid")
        if kind not in KINDS or owner not in {"machine", "user", "unknown"}:
            raise RedditErasureError("artifact_kind_or_owner_invalid")
        if any(type(x) is not bool for x in (synthetic, managed, contains_user_content, lineage_complete)):
            raise RedditErasureError("artifact_flags_invalid")
        parents = sorted({_token(x) for x in parent_artifact_ids})
        refs = sorted(_ref(x) for x in source_refs)
        if deletion_bound_refs is not None and not deletion_bound_refs <= source_refs:
            raise RedditErasureError("deletion_scope_invalid")
        bound = None if deletion_bound_refs is None else sorted(_ref(x) for x in deletion_bound_refs)
        with self._locked() as (fd, data, lock):
            if data["operations"]:
                # Frozen inventory prevents adding descendants while erasure is in flight.
                raise RedditErasureError("inventory_frozen")
            if (source_refs & _refs(data["stopped_refs"])
                    or any(x.source_id in data["stopped_node_ids"] for x in source_refs)):
                raise RedditErasureError("source_stopped")
            with self._copies(fd) as copies:
                fingerprint, _ = _read_file(copies, artifact_id + ".bin")
                parent = os.fstat(copies)
            entry = {"id": artifact_id, "kind": kind, "refs": refs, "parents": parents,
                     "owner": owner, "synthetic": synthetic, "managed": managed,
                     "contains_user_content": contains_user_content, "lineage_complete": lineage_complete,
                     "deletion_bound_refs": bound,
                     "fingerprint": fingerprint, "copies_identity": [parent.st_dev, parent.st_ino]}
            previous = data["artifacts"].get(artifact_id)
            if previous is not None and previous != entry:
                raise RedditErasureError("artifact_registration_conflict")
            data["artifacts"][artifact_id] = entry
            self._save(fd, data, lock_fd=lock)

    def stop_sources(self, *, operation_id: str, source_refs: frozenset[SourceRef], reason: str) -> None:
        _token(operation_id)
        if not source_refs or reason not in {"authorization_revoked", "authorization_expired", "retention_expired"}:
            raise RedditErasureError("stop_request_invalid")
        refs = sorted(_ref(x) for x in source_refs)
        with self._locked() as (fd, data, lock):
            previous = data["operations"].get(operation_id)
            if previous:
                if (previous["targets"] != refs or previous["reason"] != reason
                        or previous.get("scope", "exact_versions") != "exact_versions"):
                    raise RedditErasureError("operation_conflict")
                return
            data["stopped_refs"] = sorted(_ref(x) for x in _refs(data["stopped_refs"]) | source_refs)
            effective, _ = self._lineage(data["artifacts"])
            invalidated = sorted(key for key, refs in effective.items() if refs & source_refs)
            data["operations"][operation_id] = {"targets": refs, "reason": reason, "scope": "exact_versions", "state": "stopped",
                                                 "at": _stamp(), "invalidated": invalidated, "progress": {}}
            self._save(fd, data, lock_fd=lock)  # Durable stop precedes any planning/content removal.

    def stop_deletion_notices(self, *, operation_id: str, notices: tuple[DeletionNotice, ...],
                              inventory_complete: bool, covered_kinds: frozenset[str]) -> DeletionStop:
        """Stop stable nodes first, expand only proven bound versions in this inventory.

        observed_version is evidence of the notice, never an erasure scope. This
        method does not plan or unlink; unknown historical coverage remains a gap.
        """
        _token(operation_id)
        if not notices or any(not isinstance(x, DeletionNotice) for x in notices):
            raise RedditErasureError("deletion_notice_required")
        notice_values = sorted([x.source_id, _token(x.observed_version), x.reason] for x in set(notices))
        node_ids = frozenset(x.source_id for x in notices)
        with self._locked() as (fd, data, lock):
            previous = data["operations"].get(operation_id)
            if previous is not None:
                if previous.get("scope") != "node_history" or previous.get("notices") != notice_values:
                    raise RedditErasureError("operation_conflict")
                return DeletionStop(_refs(previous["targets"]), node_ids,
                                    tuple(previous["invalidated"]), tuple(previous["initial_gaps"]))
            effective, lineage_problems = self._lineage(data["artifacts"])
            bounds, bound_problems = self._deletion_bounds(data["artifacts"])
            gaps, targets, invalidated = set(), set(), []
            if inventory_complete is not True:
                gaps.add("historical_inventory_not_complete")
            if covered_kinds != KINDS:
                gaps.add("historical_copy_kinds_not_covered")
            for key, refs in effective.items():
                if any(x.source_id in node_ids for x in refs):
                    invalidated.append(key)
                    if lineage_problems[key] or bound_problems[key]:
                        gaps.add("historical_version_scope_unproven")
                    entry = data["artifacts"][key]
                    independent_user = (entry["deletion_bound_refs"] == [] and not entry["parents"]
                                        and entry["owner"] == "user" and entry["contains_user_content"])
                    if not any(x.source_id in node_ids for x in bounds[key]) and not independent_user:
                        gaps.add("historical_version_scope_unproven")
                    targets.update(x for x in bounds[key] if x.source_id in node_ids)
            if not targets:
                gaps.add("historical_bound_versions_missing")
            data["stopped_node_ids"] = sorted(set(data["stopped_node_ids"]) | node_ids)
            data["stopped_refs"] = sorted(_ref(x) for x in _refs(data["stopped_refs"]) | targets)
            data["operations"][operation_id] = {
                "scope": "node_history", "notices": notice_values, "targets": sorted(_ref(x) for x in targets),
                "reason": "node_deleted", "state": "stopped", "at": _stamp(),
                "initial_gaps": sorted(gaps), "invalidated": sorted(invalidated), "progress": {}}
            self._save(fd, data, lock_fd=lock)
            return DeletionStop(frozenset(targets), node_ids, tuple(sorted(invalidated)), tuple(sorted(gaps)))

    @staticmethod
    def _deletion_bounds(artifacts):
        """Independently prove restricted ref inheritance, not just general ancestry."""
        bounds = {key: _refs(entry["deletion_bound_refs"] or []) for key, entry in artifacts.items()}
        problems = {key: set() for key in artifacts}
        for key, entry in artifacts.items():
            if entry["deletion_bound_refs"] is None:
                problems[key].add("deletion_constraint_unknown")
        for _ in range(len(artifacts)):
            changed = False
            for key, entry in artifacts.items():
                merged = bounds[key] | frozenset().union(*(bounds.get(p, frozenset()) for p in entry["parents"]))
                if merged != bounds[key]:
                    bounds[key], changed = merged, True
            if not changed:
                break
        pending, resolved = set(artifacts), set()
        while pending:
            ready = {key for key in pending if set(artifacts[key]["parents"]) <= resolved}
            if not ready:
                for key in pending:
                    problems[key].add("deletion_parent_unresolved")
                break
            for key in ready:
                entry = artifacts[key]
                inherited = frozenset().union(*(bounds[p] for p in entry["parents"]))
                if entry["parents"] and _refs(entry["deletion_bound_refs"] or []) != inherited:
                    problems[key].add("deletion_parent_refs_mismatch")
                if any(problems[p] for p in entry["parents"]):
                    problems[key].add("deletion_parent_unknown")
            pending -= ready
            resolved.update(ready)
        return bounds, problems

    @staticmethod
    def _lineage(artifacts):
        """Compute parent union, rather than trusting a completeness checkbox."""
        effective = {key: _refs(entry["refs"]) for key, entry in artifacts.items()}
        problems = {key: set() for key in artifacts}
        for key, entry in artifacts.items():
            if not entry["lineage_complete"] or not effective[key]:
                problems[key].add("lineage_unknown")
            if entry["kind"] != "original" and not entry["parents"]:
                problems[key].add("parents_required")
            if entry["kind"] == "original" and entry["parents"]:
                problems[key].add("original_has_parents")
            if any(p not in artifacts for p in entry["parents"]):
                problems[key].add("parent_missing")
        # Fixed-point closure also propagates refs through cyclic/malformed graphs.
        for _ in range(len(artifacts)):
            changed = False
            for key, entry in artifacts.items():
                inherited = frozenset().union(*(effective.get(p, frozenset()) for p in entry["parents"]))
                merged = effective[key] | inherited
                if merged != effective[key]:
                    effective[key], changed = merged, True
            if not changed:
                break
        pending, resolved = set(artifacts), set()
        while pending:
            ready = {key for key in pending if set(artifacts[key]["parents"]) <= resolved}
            if not ready:
                for key in pending:
                    problems[key].add("parent_cycle_or_unresolved")
                break
            for key in ready:
                entry = artifacts[key]
                inherited = frozenset().union(*(effective[p] for p in entry["parents"]))
                if entry["parents"] and _refs(entry["refs"]) != inherited:
                    problems[key].add("parent_refs_mismatch")
                for parent in entry["parents"]:
                    if problems[parent]:
                        problems[key].add("parent_lineage_invalid")
            resolved.update(ready)
            pending -= ready
        return effective, problems

    def plan_erasure(self, *, operation_id: str, inventory_complete: bool,
                     covered_kinds: frozenset[str]) -> ErasurePlan:
        _token(operation_id)
        with self._locked() as (fd, data, lock):
            op = data["operations"].get(operation_id)
            if op is None:
                raise RedditErasureError("stop_required")
            if "plan" in op:
                return self._plan_value(operation_id, op)
            artifacts = data["artifacts"]
            targets = _refs(op["targets"])
            effective, problems = self._lineage(artifacts)
            bounds, bound_problems = self._deletion_bounds(artifacts)
            deleted_ids = {x[0] for x in op.get("notices", [])}
            gaps = set(op.get("initial_gaps", []))
            if inventory_complete is not True:
                gaps.add("inventory_not_complete")
            if covered_kinds != KINDS:
                gaps.add("copy_kinds_not_covered")
            with self._copies(fd) as copies:
                expected_names = {key + ".bin" for key in artifacts}
                if set(os.listdir(copies)) - expected_names:
                    gaps.add("unregistered_copy")
                current_parent = os.fstat(copies)
                items = []
                for key in sorted(artifacts):
                    entry = artifacts[key]
                    if problems[key]:
                        gaps.add("lineage_gap")
                    node_affected = any(x.source_id in deleted_ids for x in effective[key])
                    independent_user = (entry["deletion_bound_refs"] == [] and not entry["parents"]
                                        and entry["owner"] == "user" and entry["contains_user_content"])
                    if deleted_ids and node_affected and independent_user:
                        continue  # Explicit independent words are not constrained copies.
                    if not (effective[key] & targets) and not node_affected:
                        continue
                    reasons = set(problems[key])
                    if node_affected:
                        reasons.update(bound_problems[key])
                        if not any(x.source_id in deleted_ids for x in bounds[key]):
                            reasons.add("deletion_constraint_unproven")
                        if bound_problems[key] or "deletion_constraint_unproven" in reasons:
                            gaps.add("historical_version_scope_unproven")
                    if (entry["owner"] != "machine" or entry["contains_user_content"]
                            or entry["synthetic"] is not True or entry["managed"] is not True):
                        reasons.add("user_or_unmanaged_protected")
                    if entry["copies_identity"] != [current_parent.st_dev, current_parent.st_ino]:
                        reasons.add("copies_directory_changed")
                    try:
                        fingerprint, _ = _read_file(copies, key + ".bin")
                        if fingerprint != entry["fingerprint"]:
                            reasons.add("file_modified")
                    except FileNotFoundError:
                        reasons.add("unexplained_missing")
                    except (RedditErasureError, OSError):
                        reasons.add("file_unsafe")
                    items.append({"id": key, "action": "manual" if reasons else "erase",
                                  "reasons": sorted(reasons)})
            if not items:
                gaps.add("target_inventory_missing")
            # All dependent references are invalidated even when protected/manual.
            invalidated = sorted(set(op["invalidated"]) | {item["id"] for item in items})
            plan = {"targets": op["targets"], "items": items, "gaps": sorted(gaps),
                    "invalidated": invalidated, "inventory_sha256": _digest(artifacts)}
            op.update(plan=plan, plan_sha256=_digest(plan), invalidated=invalidated, state="planned")
            self._save(fd, data, lock_fd=lock)
            return self._plan_value(operation_id, op)

    @staticmethod
    def _plan_value(operation_id, op):
        plan = op["plan"]
        return ErasurePlan(operation_id, op["plan_sha256"], _refs(plan["targets"]),
                           tuple(PlanItem(x["id"], x["action"], tuple(x["reasons"])) for x in plan["items"]),
                           tuple(plan["gaps"]), tuple(plan["invalidated"]))

    def execute_erasure(self, plan: ErasurePlan, *, synthetic_only: bool = True,
                        cancel_check: Callable[[], bool] | None = None) -> ErasureResult:
        if synthetic_only is not True:
            raise RedditErasureError("synthetic_only_required")
        with self._locked() as (fd, data, lock):
            op = data["operations"].get(plan.operation_id)
            if (op is None or "plan" not in op or op["plan_sha256"] != plan.plan_sha256
                    or _digest(op["plan"]) != plan.plan_sha256 or self._plan_value(plan.operation_id, op) != plan):
                raise RedditErasureError("plan_changed")
            if not plan.source_refs <= _refs(data["stopped_refs"]):
                raise RedditErasureError("stop_required")
            if not {x[0] for x in op.get("notices", [])} <= set(data["stopped_node_ids"]):
                raise RedditErasureError("stop_required")
            if op["plan"]["inventory_sha256"] != _digest(data["artifacts"]):
                raise RedditErasureError("inventory_changed")
            # Plans may only run in their own registered scope, never product-wide.
            op["state"] = "executing"
            self._save(fd, data, lock_fd=lock)
            cancelled = False
            with self._copies(fd) as copies:
                current_parent = os.fstat(copies)
                for item in plan.items:
                    if cancel_check is not None and cancel_check():
                        cancelled = True
                        break
                    progress = op["progress"].get(item.artifact_id)
                    if item.action != "erase":
                        op["progress"][item.artifact_id] = "manual"
                        continue
                    if progress is not None and progress.startswith("manual"):
                        continue  # A later restoration is not permission to erase a hand edit.
                    entry = data["artifacts"][item.artifact_id]
                    if entry["copies_identity"] != [current_parent.st_dev, current_parent.st_ino]:
                        op["progress"][item.artifact_id] = "manual_directory_changed"
                        continue
                    try:
                        fingerprint, _ = _read_file(copies, item.artifact_id + ".bin")
                    except FileNotFoundError:
                        op["progress"][item.artifact_id] = ("absent_after_prepared" if progress in
                            {"prepared", "cleared", "absent_after_prepared"} else "manual_unexplained_missing")
                        self._save(fd, data, lock_fd=lock)
                        continue
                    except (RedditErasureError, OSError):
                        op["progress"][item.artifact_id] = "manual_file_unsafe"
                        self._save(fd, data, lock_fd=lock)
                        continue
                    if progress in {"cleared", "absent_after_prepared"} or fingerprint != entry["fingerprint"]:
                        op["progress"][item.artifact_id] = "manual_file_changed"
                        self._save(fd, data, lock_fd=lock)
                        continue
                    op["progress"][item.artifact_id] = "prepared"
                    self._save(fd, data, lock_fd=lock)
                    _event("prepared", item.artifact_id)
                    # Recheck after durable preparation and before exact unlink.
                    verify, _ = _read_file(copies, item.artifact_id + ".bin")
                    if verify != fingerprint:
                        op["progress"][item.artifact_id] = "manual_file_changed"
                        self._save(fd, data, lock_fd=lock)
                        continue
                    _event("before_unlink", item.artifact_id)
                    _validate_lock(fd, lock)
                    if cancel_check is not None and cancel_check():
                        cancelled = True
                        break
                    root_now = self.path.lstat()
                    if (root_now.st_dev, root_now.st_ino) != self.identity or not _private(root_now):
                        raise RedditErasureError("root_changed")
                    parent_now = os.stat("copies", dir_fd=fd, follow_symlinks=False)
                    if ((parent_now.st_dev, parent_now.st_ino) != (current_parent.st_dev, current_parent.st_ino)
                            or not _private(parent_now)):
                        raise RedditErasureError("copies_directory_changed")
                    # Check content again after the seam, including in-place same-size edits.
                    last_fingerprint, _ = _read_file(copies, item.artifact_id + ".bin")
                    if last_fingerprint != fingerprint:
                        op["progress"][item.artifact_id] = "manual_file_changed"
                        self._save(fd, data, lock_fd=lock)
                        continue
                    final = os.stat(item.artifact_id + ".bin", dir_fd=copies, follow_symlinks=False)
                    if (final.st_dev, final.st_ino, final.st_size, final.st_nlink) != (
                            fingerprint["dev"], fingerprint["ino"], fingerprint["size"], 1) or not stat.S_ISREG(final.st_mode):
                        op["progress"][item.artifact_id] = "manual_file_changed"
                        self._save(fd, data, lock_fd=lock)
                        continue
                    os.unlink(item.artifact_id + ".bin", dir_fd=copies)
                    _event("unlinked", item.artifact_id)
                    os.fsync(copies)
                    _event("directory_synced", item.artifact_id)
                    op["progress"][item.artifact_id] = "cleared"
                    self._save(fd, data, lock_fd=lock)
                    _event("receipt_synced", item.artifact_id)
                gaps = set(plan.coverage_gaps)
                if set(os.listdir(copies)) - {key + ".bin" for key in data["artifacts"]}:
                    gaps.add("unregistered_copy")
                cleared, manual = [], []
                for item in plan.items:
                    status = op["progress"].get(item.artifact_id, "pending")
                    if status in {"cleared", "absent_after_prepared"}:
                        # No success if a pathname has been recreated after removal.
                        try:
                            os.stat(item.artifact_id + ".bin", dir_fd=copies, follow_symlinks=False)
                        except FileNotFoundError:
                            cleared.append(item.artifact_id)
                        else:
                            op["progress"][item.artifact_id] = "manual_file_recreated"
                            manual.append(item.artifact_id)
                    else:
                        manual.append(item.artifact_id)
            state = ("cancelled" if cancelled else "partial_manual" if manual or gaps
                     else "cleared_registered_synthetic_scope")
            op.update(state=state, at=_stamp(), gaps=sorted(gaps))
            self._save(fd, data, lock_fd=lock)
            return ErasureResult(plan.operation_id, state, tuple(cleared), tuple(manual),
                                 plan.invalidated_refs, tuple(sorted(gaps)))

    def resume_erasure(self, operation_id: str, *, cancel_check=None) -> ErasureResult:
        _token(operation_id)
        with self._locked() as (_, data, _lock):
            op = data["operations"].get(operation_id)
            if op is None or "plan" not in op:
                raise RedditErasureError("plan_required")
            plan = self._plan_value(operation_id, op)
        return self.execute_erasure(plan, cancel_check=cancel_check)
