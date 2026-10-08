"""Durable task boundaries for V3 wiki maintenance.

The module freezes only stable raw identities, relative paths, byte counts and
digests. Raw bodies, page bodies, arbitrary exception messages and credentials
are never persisted in the task tables.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import uuid

from .database import connect, initialize
from .wiki_kit import (
    WikiKitError,
    read_receipt,
    verify_installed_kit,
    verify_source_kit,
)
from .wiki_lock import VaultWriteLock, WikiLockError, canonical_vault, vault_key
from .wiki_kit_runtime import WikiKitRuntime, WikiKitRuntimeError
from .wiki_schema import ERROR_CODES


RAW_ID = re.compile(r"R-\d{8}-\d{4}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
IDENTITIES = {"第三方", "本人", "本人附言"}
TASK_STATES = {"queued", "preparing", "running", "validating", "publishing", "succeeded", "failed"}
BATCH_STATES = TASK_STATES


class WikiTaskError(RuntimeError):
    """A fixed-code task error safe to return through application boundaries."""


@dataclass(frozen=True)
class ProtocolRaw:
    relative_path: str
    raw_id: str
    identity: str
    collected_at: str
    addendum_target: str
    adjacent_raw_ids: tuple[str, ...]
    byte_count: int
    content_sha256: str


@dataclass(frozen=True)
class ProtocolScan:
    pending: tuple[ProtocolRaw, ...]
    issue_counts: tuple[tuple[str, int], ...]
    candidate_count: int
    health_eligible: bool
    health_due: bool
    health_due_reason: str
    last_lint_date: str
    lint_count: int


@dataclass(frozen=True)
class FrozenRaw:
    relative_path: str
    raw_id: str
    identity: str
    byte_count: int
    content_sha256: str
    ordinal: int = 0
    batch_no: int = 0


@dataclass(frozen=True)
class WikiBatch:
    batch_no: int
    state: str
    item_count: int
    error_code: str | None


@dataclass(frozen=True)
class WikiTask:
    task_id: str
    vault_path: str
    vault_key: str
    request_kind: str
    trigger_source: str
    backend: str
    model: str
    effort: str
    kit_version: str
    kit_manifest_sha256: str
    boundary_sha256: str
    state: str
    raw_count: int
    batch_count: int
    completed_batch_count: int
    error_code: str | None
    recovery_state: str
    recovery_phase: str
    created_at: str
    updated_at: str
    batches: tuple[WikiBatch, ...] = ()
    raw: tuple[FrozenRaw, ...] = ()
    outcome_contract: str = 'legacy'
    plan_json: str = '{}'
    plan_sha256: str | None = None


@dataclass(frozen=True)
class WikiObservation:
    vault_key: str
    vault_path: str
    task_id: str | None
    pending_count: int | None
    candidate_count: int | None
    observed_at: str
    error_code: str | None


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _protocol_path(value: object) -> str:
    if not isinstance(value, str):
        raise WikiTaskError("protocol_error")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.suffix != ".md" or len(path.parts) < 3
            or path.parts[0] != "raw" or path.parts[1] not in {"外部", "自述"}
            or any(part in ("", ".", "..") for part in path.parts)):
        raise WikiTaskError("raw_path_invalid")
    return path.as_posix()


def scan_workflow_protocol(vault: Path | str, python_executable: Path | str | None = None,
                           kb_path: Path | str | None = None, *,
                           runtime: WikiKitRuntime | None = None) -> ProtocolScan:
    root = canonical_vault(vault)
    try:
        if runtime is not None:
            result = runtime.run("kb", root, ("protocol-scan",))
        else:
            if python_executable is None or kb_path is None:
                raise WikiKitRuntimeError("protocol_error")
            environment = {
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            result = subprocess.run(
                [os.fspath(python_executable), "-E", "-B", os.fspath(kb_path),
                 "protocol-scan", "--root", os.fspath(root)],
                cwd=root, capture_output=True, text=True, timeout=120,
                check=False, env=environment)
    except WikiKitRuntimeError as error:
        code = str(error)
        if code not in {"kit_missing", "kit_drift", "kit_incompatible"}:
            code = "protocol_error"
        raise WikiTaskError(code) from error
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise WikiTaskError("protocol_error") from error
    if result.returncode != 0:
        raise WikiTaskError("protocol_error")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise WikiTaskError("protocol_error") from error
    if not isinstance(data, dict) or set(data) != {
        "protocol_version", "issue_counts", "pending", "candidate_count", "health"
    }:
        raise WikiTaskError("protocol_error")
    if data["protocol_version"] != 2:
        raise WikiTaskError("kit_incompatible")
    counts = data["issue_counts"]
    if (not isinstance(counts, dict) or set(counts) != {"错误", "提醒", "信息"}
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                   for value in counts.values())):
        raise WikiTaskError("protocol_error")
    if counts["错误"]:
        raise WikiTaskError("protocol_error")
    rows = data["pending"]
    if not isinstance(rows, list):
        raise WikiTaskError("protocol_error")
    candidate_count = data["candidate_count"]
    health = data["health"]
    if (not isinstance(candidate_count, int) or isinstance(candidate_count, bool)
            or candidate_count < 0 or not isinstance(health, dict)
            or set(health) != {"eligible", "due", "due_reason", "last_lint_date", "lint_count"}
            or type(health["eligible"]) is not bool or type(health["due"]) is not bool
            or health["due_reason"] not in {
                "not_eligible", "first", "current", "changed_due", "changed_waiting", "unknown"
            }
            or not isinstance(health["last_lint_date"], str)
            or not isinstance(health["lint_count"], int) or isinstance(health["lint_count"], bool)
            or health["lint_count"] < 0 or (health["due"] and not health["eligible"])):
        raise WikiTaskError("protocol_error")
    parsed = []
    seen_paths, seen_ids = set(), set()
    expected = {
        "relative_path", "raw_id", "identity", "collected_at", "addendum_target",
        "adjacent_raw_ids", "byte_count", "content_sha256",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            raise WikiTaskError("protocol_error")
        relative = _protocol_path(row["relative_path"])
        raw_id, identity = row["raw_id"], row["identity"]
        if not isinstance(raw_id, str) or not RAW_ID.fullmatch(raw_id) or identity not in IDENTITIES:
            raise WikiTaskError("protocol_error")
        relative_parts = PurePosixPath(relative).parts
        if (PurePosixPath(relative).stem != raw_id
                or (identity == "第三方") != (relative_parts[1] == "外部")):
            raise WikiTaskError("protocol_error")
        if relative in seen_paths or raw_id in seen_ids:
            raise WikiTaskError("protocol_error")
        seen_paths.add(relative)
        seen_ids.add(raw_id)
        collected = row["collected_at"]
        target = row["addendum_target"]
        adjacent = row["adjacent_raw_ids"]
        count, digest = row["byte_count"], row["content_sha256"]
        if not isinstance(collected, str) or not collected or not isinstance(target, str):
            raise WikiTaskError("protocol_error")
        if target and not RAW_ID.fullmatch(target):
            raise WikiTaskError("protocol_error")
        if (not isinstance(adjacent, list) or any(not isinstance(x, str) or not RAW_ID.fullmatch(x)
                                                  for x in adjacent)):
            raise WikiTaskError("protocol_error")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise WikiTaskError("protocol_error")
        if not isinstance(digest, str) or not HASH.fullmatch(digest):
            raise WikiTaskError("protocol_error")
        parsed.append(ProtocolRaw(relative, raw_id, identity, collected, target,
                                  tuple(dict.fromkeys(adjacent)), count, digest))
    return ProtocolScan(
        tuple(sorted(parsed, key=lambda row: row.relative_path)),
        tuple((level, counts[level]) for level in ("错误", "提醒", "信息")),
        candidate_count,
        health["eligible"],
        health["due"],
        health["due_reason"],
        health["last_lint_date"],
        health["lint_count"],
    )


def scan_protocol(vault: Path | str, python_executable: Path | str,
                  kb_path: Path | str) -> tuple[ProtocolRaw, ...]:
    """Compatibility wrapper for task freezing callers needing pending raw only."""
    return scan_workflow_protocol(vault, python_executable, kb_path).pending


def _freeze_one(root: Path, row: ProtocolRaw) -> FrozenRaw:
    current = root
    try:
        for part in PurePosixPath(row.relative_path).parts:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise WikiTaskError("raw_symlink")
        if not stat.S_ISREG(current.lstat().st_mode):
            raise WikiTaskError("raw_path_invalid")
        descriptor = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise WikiTaskError("raw_path_invalid")
            digest = hashlib.sha256()
            count = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
            after = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise WikiTaskError("raw_changed")
        finally:
            os.close(descriptor)
    except WikiTaskError:
        raise
    except FileNotFoundError as error:
        raise WikiTaskError("raw_changed") from error
    except OSError as error:
        raise WikiTaskError("raw_path_invalid") from error
    value = digest.hexdigest()
    if count != row.byte_count or value != row.content_sha256:
        raise WikiTaskError("raw_changed")
    return FrozenRaw(row.relative_path, row.raw_id, row.identity, count, value)


def plan_batches(rows: tuple[ProtocolRaw, ...], target_size: int = 5) -> tuple[tuple[ProtocolRaw, ...], ...]:
    """Keep explicit relation components together, prioritizing user's raws.

    ``邻接`` contributes only a co-batching candidate edge. It is not stored or
    presented as causal evidence by this planner.
    """
    if target_size < 1:
        raise WikiTaskError("invalid_batch_size")
    by_id = {row.raw_id: row for row in rows}
    links = {row.raw_id: set() for row in rows}
    for row in rows:
        related = set(row.adjacent_raw_ids)
        if row.addendum_target:
            related.add(row.addendum_target)
        for other in related:
            if other in by_id:
                links[row.raw_id].add(other)
                links[other].add(row.raw_id)
    components = []
    visited = set()
    for row in sorted(rows, key=lambda item: item.relative_path):
        if row.raw_id in visited:
            continue
        stack, component = [row.raw_id], []
        visited.add(row.raw_id)
        while stack:
            raw_id = stack.pop()
            component.append(by_id[raw_id])
            for other in sorted(links[raw_id], reverse=True):
                if other not in visited:
                    visited.add(other)
                    stack.append(other)
        component.sort(key=lambda item: item.relative_path)
        components.append(tuple(component))
    components.sort(key=lambda group: (
        0 if any(row.identity in {"本人", "本人附言"} for row in group) else 1,
        group[0].relative_path,
    ))
    batches, current = [], []
    for component in components:
        if current and len(current) + len(component) > target_size:
            batches.append(tuple(current))
            current = []
        if len(component) > target_size:
            batches.append(component)
        else:
            current.extend(component)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _boundary(frozen_batches: tuple[tuple[FrozenRaw, ...], ...]) -> str:
    rows = [[{
        "relative_path": item.relative_path,
        "raw_id": item.raw_id,
        "identity": item.identity,
        "byte_count": item.byte_count,
        "content_sha256": item.content_sha256,
    } for item in batch] for batch in frozen_batches]
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _program_plan(raw, *, boundary, kit_version, kit_sha256, backend, model, effort):
    """Program-only plan. Source_requirement is an obligation, not proof."""
    from . import wiki_typed as t
    from .wiki_support import CONTRACT_VERSION, EXTRACTOR_VERSION, RECOVERY_VERSION
    configuration = dict(backend=backend, model=model, effort=effort,
        provider='knowledge_subscription', transport='typed-exec-schema-final-v1',
        input_policy=t.APPLICATION_UTF8_POLICY, max_application_input_bytes=t.INPUT_LIMIT,
        stdout_limit=t.STDOUT_LIMIT, stderr_limit=t.STDERR_LIMIT, line_limit=t.LINE_LIMIT,
        final_limit=t.FINAL_LIMIT, generation_timeout=t.GENERATION_TIMEOUT,
        check_timeout=t.CHECK_TIMEOUT, max_extra_repairs=2, r14_max_tokens=8192)
    schemas = {name: dict(contract=contract, revision=1, sha256=t.digest(t.encoded(schema)))
        for name, contract, schema in (
            ('proposal', t.CONTRACT, t.PROPOSAL_SCHEMA),
            ('check', t.CHECK_CONTRACT, t.CHECK_SCHEMA),
            ('support', t.SUPPORT_CONTRACT, t.SUPPORT_SCHEMA))}
    batch_numbers = sorted({r.batch_no for r in raw})
    if (not raw or batch_numbers != list(range(1, len(batch_numbers) + 1))
            or [r.ordinal for r in raw] != list(range(1, len(raw) + 1))
            or len({r.raw_id for r in raw}) != len(raw)
            or [r.batch_no for r in raw] != sorted(r.batch_no for r in raw)):
        raise WikiTaskError('plan_binding_invalid')
    groups = tuple(tuple(r for r in raw if r.batch_no == n) for n in batch_numbers)
    if _boundary(groups) != boundary:
        raise WikiTaskError('plan_binding_invalid')
    return dict(contract='r08-task-plan-v1', schema_revision=1, outcome_contract=t.CONTRACT,
        boundary_sha256=boundary,
        source_requirement=dict(contract='wiki-source-proof-v1', scope='finite_retained_sources'),
        kit=dict(version=kit_version, manifest_sha256=kit_sha256), configuration=configuration,
        schemas=schemas, r14_versions=[CONTRACT_VERSION, EXTRACTOR_VERSION, RECOVERY_VERSION],
        raw=[asdict(r) for r in raw],
        batches=[dict(batch_no=n, raw_ids=[r.raw_id for r in raw if r.batch_no == n])
                 for n in batch_numbers])


def execution_policy_sha256(task):
    plan = _validated_plan(task)
    return hashlib.sha256(_canonical({k: plan[k] for k in
                                    ('configuration', 'schemas', 'r14_versions')})).hexdigest()


def _validated_plan(task):
    expected = _program_plan(task.raw, boundary=task.boundary_sha256,
        kit_version=task.kit_version, kit_sha256=task.kit_manifest_sha256,
        backend=task.backend, model=task.model, effort=task.effort)
    content = _canonical(expected)
    if (task.outcome_contract != expected['outcome_contract']
            or task.plan_json != content.decode('utf-8')
            or task.plan_sha256 != hashlib.sha256(content).hexdigest()
            or task.raw_count != len(task.raw) or task.batch_count != len(expected['batches'])
            or [(b.batch_no, b.item_count) for b in task.batches] != [
                (b['batch_no'], len(b['raw_ids'])) for b in expected['batches']]):
        raise WikiTaskError('plan_binding_invalid')
    return expected


def _execution_schema(connection):
    # Frozen schema27 owner API; no migration or runtime catalog adoption here.
    try:
        from .database import _wiki_execution_schema_inventory
    except ImportError:
        raise WikiTaskError('execution_schema_unavailable') from None
    if connection.execute('PRAGMA user_version').fetchone()[0] != 27:
        raise WikiTaskError('execution_schema_unavailable')
    _wiki_execution_schema_inventory(connection)


class WikiTaskStore:
    def __init__(self, database_path: Path | str, *, kit_root: Path | str,
                 python_executable: Path | str, kb_path: Path | str | None = None,
                 runtime: WikiKitRuntime | None = None):
        self.database_path = Path(database_path)
        self.kit_root = Path(kit_root)
        self.python_executable = Path(python_executable)
        self.kb_path = Path(kb_path) if kb_path else self.kit_root / "tools" / "kb.py"
        self.runtime = runtime or WikiKitRuntime(
            self.kit_root, python_executable=self.python_executable)

    def _task(self, connection: sqlite3.Connection, task_id: str) -> WikiTask:
        row = connection.execute("SELECT * FROM wiki_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise WikiTaskError("task_not_found")
        batches = tuple(WikiBatch(r["batch_no"], r["state"], r["item_count"], r["error_code"])
                        for r in connection.execute(
                            "SELECT * FROM wiki_task_batches WHERE task_id=? ORDER BY batch_no", (task_id,)))
        raw = tuple(FrozenRaw(r["relative_path"], r["raw_id"], r["identity"],
                              r["byte_count"], r["content_sha256"],
                              r["ordinal"], r["batch_no"])
                    for r in connection.execute(
                        "SELECT * FROM wiki_task_raw WHERE task_id=? ORDER BY ordinal", (task_id,)))
        return WikiTask(
            row["task_id"], row["vault_path"], row["vault_key"], row["request_kind"],
            row["trigger_source"], row["backend"], row["model"], row["effort"],
            row["kit_version"], row["kit_manifest_sha256"], row["boundary_sha256"],
            row["state"], row["raw_count"], row["batch_count"], row["completed_batch_count"],
            row["error_code"], row["recovery_state"], row["recovery_phase"],
            row["created_at"], row["updated_at"], batches, raw,
            row['outcome_contract'], row['plan_json'], row['plan_sha256'],
        )

    def get(self, task_id: str) -> WikiTask:
        initialize(self.database_path)
        with connect(self.database_path) as connection:
            return self._task(connection, task_id)

    @staticmethod
    def _observation(row: sqlite3.Row | None) -> WikiObservation | None:
        if row is None:
            return None
        return WikiObservation(
            row["vault_key"], row["vault_path"], row["task_id"],
            row["pending_count"], row["candidate_count"],
            row["observed_at"], row["error_code"],
        )

    def latest_task_for_vault(self, vault: Path | str) -> WikiTask | None:
        initialize(self.database_path)
        try:
            key = vault_key(canonical_vault(vault))
        except WikiLockError as error:
            raise WikiTaskError("raw_path_invalid") from error
        with connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT task_id FROM wiki_tasks WHERE vault_key=?
                   ORDER BY rowid DESC LIMIT 1""", (key,)
            ).fetchone()
            return self._task(connection, row["task_id"]) if row is not None else None

    def status_task_for_vault(self, vault: Path | str) -> WikiTask | None:
        """Return the one task which owns the cheap product status surface.

        Active work wins.  With no active work, an unresolved recovery journal
        wins even when a later, safely failed history row exists.  Otherwise
        the newest durable task represents the Vault.
        """
        initialize(self.database_path)
        try:
            key = vault_key(canonical_vault(vault))
        except WikiLockError as error:
            raise WikiTaskError("raw_path_invalid") from error
        with connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT task_id FROM wiki_tasks WHERE vault_key=?
                   ORDER BY CASE
                       WHEN state IN ('queued','preparing','running','validating','publishing')
                           THEN 0
                       WHEN state='failed'
                            AND recovery_state NOT IN ('not_needed','succeeded') THEN 1
                       ELSE 2 END,
                       rowid DESC
                   LIMIT 1""",
                (key,),
            ).fetchone()
            return self._task(connection, row["task_id"]) if row is not None else None

    def observation_for_vault(self, vault: Path | str) -> WikiObservation | None:
        initialize(self.database_path)
        try:
            key = vault_key(canonical_vault(vault))
        except WikiLockError as error:
            raise WikiTaskError("raw_path_invalid") from error
        with connect(self.database_path) as connection:
            return self._observation(connection.execute(
                "SELECT * FROM wiki_observations WHERE vault_key=?", (key,)
            ).fetchone())

    def record_observation(self, vault: Path | str, *, scan: ProtocolScan | None,
                           error_code: str | None = None,
                           task_id: str | None = None) -> WikiObservation:
        if (scan is None) == (error_code is None):
            raise WikiTaskError("protocol_error")
        if error_code is not None and error_code not in ERROR_CODES:
            raise WikiTaskError("error_code_invalid")
        initialize(self.database_path)
        try:
            root = canonical_vault(vault)
            key = vault_key(root)
        except WikiLockError as error:
            raise WikiTaskError("raw_path_invalid") from error
        pending = len(scan.pending) if scan is not None else None
        candidates = scan.candidate_count if scan is not None else None
        now = _timestamp()
        try:
            with connect(self.database_path) as connection:
                connection.execute(
                    """INSERT INTO wiki_observations(
                           vault_key,vault_path,task_id,pending_count,candidate_count,
                           observed_at,error_code
                       ) VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(vault_key) DO UPDATE SET
                           vault_path=excluded.vault_path,
                           task_id=excluded.task_id,
                           pending_count=excluded.pending_count,
                           candidate_count=excluded.candidate_count,
                           observed_at=excluded.observed_at,
                           error_code=excluded.error_code""",
                    (key, os.fspath(root), task_id, pending, candidates, now, error_code),
                )
                return self._observation(connection.execute(
                    "SELECT * FROM wiki_observations WHERE vault_key=?", (key,)
                ).fetchone())
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("protocol_error") from error

    def claim_next_runnable(self, *, exclude_task_ids: frozenset[str] = frozenset()) -> WikiTask | None:
        """Claim one queued task or return an interrupted nonterminal task.

        The Vault inode lock remains the cross-process execution authority. The
        database transaction only prevents two queued tasks being advanced by
        the same claim operation.
        """
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """SELECT task_id,state FROM wiki_tasks
                       WHERE state IN ('queued','preparing','running','validating','publishing')
                       ORDER BY created_at,task_id"""
                ).fetchall()
                row = next((candidate for candidate in rows
                            if candidate["task_id"] not in exclude_task_ids), None)
                if row is None:
                    return None
                if row["state"] == "queued":
                    connection.execute(
                        "UPDATE wiki_tasks SET state='preparing',updated_at=? WHERE task_id=?",
                        (_timestamp(), row["task_id"]),
                    )
                return self._task(connection, row["task_id"])
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def create_or_reuse(self, vault: Path | str, *, request_kind: str,
                        trigger_source: str, backend: str, model: str, effort: str,
                        allow_supersede_failed: bool = False,
                        outcome_contract: str = 'legacy') -> WikiTask:
        if outcome_contract not in {'legacy', 'r08-wiki-outcomes-v1'}:
            raise WikiTaskError('outcome_contract_invalid')
        if request_kind not in {"one_batch", "all"}:
            raise WikiTaskError("request_kind_invalid")
        if trigger_source not in {"local_web", "claudian", "cli"}:
            raise WikiTaskError("trigger_source_invalid")
        if trigger_source == "local_web" and request_kind != "all":
            raise WikiTaskError("request_kind_invalid")
        if backend != "codex_cli":
            raise WikiTaskError("backend_invalid")
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise WikiTaskError("effort_invalid")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model):
            raise WikiTaskError("model_invalid")

        initialize(self.database_path)
        try:
            root = canonical_vault(vault)
            with VaultWriteLock.acquire(root) as held:
                desired = verify_source_kit(self.kit_root)
                receipt = read_receipt(root)
                if receipt is None:
                    raise WikiTaskError("kit_missing")
                verify_installed_kit(root, receipt)
                if (receipt.kit_version != desired.kit_version
                        or receipt.protocol_version != desired.protocol_version
                        or receipt.manifest_sha256 != desired.manifest_sha256
                        or receipt.files != desired.files):
                    raise WikiTaskError("kit_incompatible")
                rows = scan_workflow_protocol(root, runtime=self.runtime).pending
                if not rows:
                    raise WikiTaskError("no_pending_raw")
                planned = plan_batches(rows)
                if request_kind == "one_batch" and outcome_contract == 'legacy':
                    planned = planned[:1]
                frozen_batches = tuple(tuple(_freeze_one(root, item) for item in batch)
                                       for batch in planned)
                boundary = _boundary(frozen_batches)
                frozen_raw = tuple(replace(item, ordinal=ordinal, batch_no=batch_no)
                    for ordinal, (batch_no, item) in enumerate(
                        ((n, r) for n, batch in enumerate(frozen_batches, 1) for r in batch), 1))
                plan_bytes = (_canonical(_program_plan(frozen_raw, boundary=boundary,
                    kit_version=desired.kit_version, kit_sha256=desired.manifest_sha256,
                    backend=backend, model=model, effort=effort))
                    if outcome_contract != 'legacy' else b'{}')
                plan_sha = hashlib.sha256(plan_bytes).hexdigest() if outcome_contract != 'legacy' else None
                now, key = _timestamp(), held.key
                try:
                    with connect(self.database_path) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        if outcome_contract != 'legacy':
                            _execution_schema(connection)
                        unresolved_recovery = connection.execute(
                            """SELECT 1 FROM wiki_tasks WHERE vault_key=? AND state='failed'
                               AND recovery_state NOT IN ('not_needed','succeeded') LIMIT 1""",
                            (key,),
                        ).fetchone()
                        if unresolved_recovery:
                            raise WikiTaskError("recovery_required")
                        existing = connection.execute(
                            """SELECT task_id FROM wiki_tasks
                               WHERE vault_key=? AND boundary_sha256=? AND kit_manifest_sha256=?
                                 AND backend=? AND model=? AND effort=? AND outcome_contract=?
                                 AND (?='legacy' OR plan_sha256=?)""",
                            (key, boundary, desired.manifest_sha256, backend, model, effort,
                             outcome_contract, outcome_contract, plan_sha),
                        ).fetchone()
                        if existing:
                            reused = self._task(connection, existing["task_id"])
                            if outcome_contract != 'legacy':
                                _validated_plan(reused)
                                if reused.raw != frozen_raw:
                                    raise WikiTaskError('plan_binding_invalid')
                            return reused
                        active = connection.execute(
                            """SELECT task_id FROM wiki_tasks WHERE vault_key=?
                               AND state IN ('queued','preparing','running','validating','publishing')""",
                            (key,),
                        ).fetchone()
                        if active:
                            raise WikiTaskError("vault_busy")
                        failed = connection.execute(
                            """SELECT * FROM wiki_tasks WHERE vault_key=? AND state='failed'
                               ORDER BY rowid DESC LIMIT 1""", (key,)
                        ).fetchone()
                        if failed is not None:
                            if failed["recovery_state"] not in {"not_needed", "succeeded"}:
                                raise WikiTaskError("recovery_required")
                            execution_changed = (
                                failed["backend"], failed["model"], failed["effort"],
                                failed["kit_manifest_sha256"],
                            ) != (backend, model, effort, desired.manifest_sha256)
                            if not allow_supersede_failed or not execution_changed:
                                raise WikiTaskError("vault_busy")
                        task_id = uuid.uuid4().hex
                        raw_count = sum(len(batch) for batch in frozen_batches)
                        columns = ('task_id,vault_path,vault_key,request_kind,trigger_source,'
                            'backend,model,effort,kit_version,kit_manifest_sha256,'
                            'boundary_sha256,state,raw_count,batch_count,completed_batch_count,'
                            'error_code,recovery_state,recovery_phase,created_at,updated_at')
                        values = (task_id, os.fspath(root), key, request_kind, trigger_source,
                            backend, model, effort, desired.kit_version, desired.manifest_sha256,
                            boundary, 'queued', raw_count, len(frozen_batches), 0, None,
                            'not_needed', 'none', now, now)
                        if outcome_contract != 'legacy':
                            columns += ',outcome_contract,plan_json,plan_sha256'
                            values += (outcome_contract, plan_bytes.decode('utf-8'), plan_sha)
                        connection.execute(f"INSERT INTO wiki_tasks({columns}) VALUES ({','.join('?' for _ in values)})",
                                           values)
                        ordinal = 0
                        for batch_no, batch in enumerate(frozen_batches, 1):
                            connection.execute(
                                "INSERT INTO wiki_task_batches VALUES (?,?,?,?,?)",
                                (task_id, batch_no, "queued", len(batch), None),
                            )
                            for item in batch:
                                ordinal += 1
                                connection.execute(
                                    """INSERT INTO wiki_task_raw(
                                        task_id,ordinal,batch_no,raw_id,identity,relative_path,
                                        byte_count,content_sha256
                                    ) VALUES (?,?,?,?,?,?,?,?)""",
                                    (task_id, ordinal, batch_no, item.raw_id, item.identity,
                                     item.relative_path, item.byte_count, item.content_sha256),
                                )
                        return self._task(connection, task_id)
                except sqlite3.IntegrityError as error:
                    raise WikiTaskError("vault_busy") from error
        except BlockingIOError as error:
            raise WikiTaskError("vault_busy") from error
        except WikiLockError as error:
            raise WikiTaskError("raw_path_invalid") from error
        except WikiKitError as error:
            code = str(error)
            if code not in {"kit_missing", "kit_drift", "kit_incompatible"}:
                code = "kit_drift"
            raise WikiTaskError(code) from error

    def read_execution_task(self, task_id: str, *, expected_plan_sha256: str,
                            connection=None) -> WikiTask:
        if connection is None:
            with connect(self.database_path) as db:
                return self.read_execution_task(task_id, expected_plan_sha256=expected_plan_sha256,
                                                connection=db)
        _execution_schema(connection)
        task = self._task(connection, task_id)
        _validated_plan(task)
        if task.plan_sha256 != expected_plan_sha256:
            raise WikiTaskError('plan_binding_invalid')
        return task

    def reserve_generation(self, task_id: str, snapshot, runtime_root, *,
                           expected_plan_sha256: str, batch_no: int, lock,
                           source_proof, argv, stdin_bytes, schema_bytes,
                           input_binding, recording_call, timeout_seconds, allow_recovery=False):
        """One host permit, durable before Popen; no queue or state mutation."""
        from .wiki_staging import reserve_generation
        with connect(self.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.read_execution_task(task_id, expected_plan_sha256=expected_plan_sha256,
                                            connection=db)
            batch = next((b for b in task.batches if b.batch_no == batch_no), None)
            if batch is None or batch.state != 'running' or task.state != 'running':
                raise WikiTaskError('invalid_transition')
            return reserve_generation(snapshot, runtime_root, task=task, batch_no=batch_no,
                lock=lock, source_proof=(source_proof.with_connection(db)
                    if hasattr(source_proof, 'with_connection') else source_proof), argv=argv, stdin_bytes=stdin_bytes,
                schema_bytes=schema_bytes, input_binding=input_binding,
                recording_call=recording_call, timeout_seconds=timeout_seconds, allow_recovery=allow_recovery)

    def save_generation_result(self, permit, result, *, lock, source_proof):
        from .wiki_staging import save_generation_result
        with connect(self.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.read_execution_task(permit.task.task_id,
                expected_plan_sha256=permit.task.plan_sha256, connection=db)
            if task.raw != permit.task.raw or task.boundary_sha256 != permit.task.boundary_sha256:
                raise WikiTaskError('plan_binding_invalid')
            return save_generation_result(permit, result, lock=lock, source_proof=(
                source_proof.with_connection(db) if hasattr(source_proof, 'with_connection') else source_proof))

    def load_generation_checkpoint(self, task_id, runtime_root, *, batch_no, lock,
                                   expected_plan_sha256, source_proof, allow_regenerated_graph=False):
        from .wiki_staging import load_generation_checkpoint
        with connect(self.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.read_execution_task(task_id, expected_plan_sha256=expected_plan_sha256,
                                            connection=db)
            return load_generation_checkpoint(Path(runtime_root) / 'wiki-tasks' / task_id,
                runtime_root, task=task, batch_no=batch_no, lock=lock, source_proof=(
                    source_proof.with_connection(db) if hasattr(source_proof, 'with_connection') else source_proof),
                allow_regenerated_graph=allow_regenerated_graph)

    def set_task_state(self, task_id: str, state: str) -> WikiTask:
        if state not in TASK_STATES - {"failed"}:
            raise WikiTaskError("state_invalid")
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute(
                    "UPDATE wiki_tasks SET state=?, updated_at=? WHERE task_id=?",
                    (state, _timestamp(), task_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise WikiTaskError("task_not_found")
                if state == "succeeded":
                    task = self._task(connection, task_id)
                    if task.completed_batch_count != task.batch_count:
                        raise WikiTaskError("readback_incomplete")
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def set_batch_state(self, task_id: str, batch_no: int, state: str) -> WikiTask:
        if state not in BATCH_STATES - {"failed", "succeeded"}:
            raise WikiTaskError("state_invalid")
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute(
                    "UPDATE wiki_task_batches SET state=? WHERE task_id=? AND batch_no=?",
                    (state, task_id, batch_no),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise WikiTaskError("batch_not_found")
                connection.execute("UPDATE wiki_tasks SET updated_at=? WHERE task_id=?",
                                   (_timestamp(), task_id))
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def mark_batch_readback_succeeded(self, task_id: str, batch_no: int) -> WikiTask:
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state FROM wiki_task_batches WHERE task_id=? AND batch_no=?",
                    (task_id, batch_no),
                ).fetchone()
                if row is None:
                    raise WikiTaskError("batch_not_found")
                if row["state"] == "succeeded":
                    return self._task(connection, task_id)
                connection.execute(
                    "UPDATE wiki_task_batches SET state='succeeded', error_code=NULL WHERE task_id=? AND batch_no=?",
                    (task_id, batch_no),
                )
                connection.execute(
                    """UPDATE wiki_tasks SET completed_batch_count=completed_batch_count+1,
                       updated_at=? WHERE task_id=?""",
                    (_timestamp(), task_id),
                )
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def accept_published_batch(self, task_id, batch_no, *, verify, recovered=False):
        """Locked caller proves publication on this transaction, then records success.

        The nine-field schema27 capability is exact and one-use; normal database
        connections retain their default denial. No caller success boolean is used.
        """
        from .wiki_outcomes import CONTRACT
        with connect(self.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._task(db, task_id)
            _validated_plan(task)
            batch = next(b for b in task.batches if b.batch_no == batch_no)
            if batch.state == 'succeeded':
                return task
            if recovered and batch.state == 'failed':
                for state in ('queued', 'preparing', 'running', 'validating', 'publishing'):
                    db.execute('UPDATE wiki_task_batches SET state=?,error_code=NULL WHERE task_id=? AND batch_no=?',
                               (state, task_id, batch_no))
                task = self._task(db, task_id)
            elif batch.state != 'publishing':
                raise WikiTaskError('invalid_transition')
            receipt_id, payload_json = verify(task, db)
            created_at = _timestamp()
            values = (receipt_id, task_id, batch_no, 'accepted', CONTRACT,
                      task.boundary_sha256, task.plan_sha256, payload_json, created_at)
            old = db.execute("SELECT receipt_id,payload_json FROM wiki_outcome_receipts WHERE task_id=? AND batch_no=? AND phase='accepted'",
                             (task_id, batch_no)).fetchone()
            if old is not None:
                if tuple(old) != (receipt_id, payload_json):
                    raise WikiTaskError('readback_failed')
            else:
                available = [True]
                def permission(*actual):
                    if available[0] and actual == values:
                        available[0] = False
                        return 1
                    return 0
                db.create_function('wiki_outcome_accept', 9, permission)
                try:
                    db.execute('INSERT INTO wiki_outcome_receipts VALUES(?,?,?,?,?,?,?,?,?)', values)
                finally:
                    db.create_function('wiki_outcome_accept', 9, lambda *_: 0)
            db.execute("UPDATE wiki_task_batches SET state='succeeded',error_code=NULL WHERE task_id=? AND batch_no=?",
                       (task_id, batch_no))
            db.execute('UPDATE wiki_tasks SET completed_batch_count=completed_batch_count+1,updated_at=? WHERE task_id=?',
                       (_timestamp(), task_id))
            return self._task(db, task_id)

    def mark_recovered_batch_readback_succeeded(self, task_id: str,
                                                batch_no: int) -> WikiTask:
        """Record a journal-proven commit without rerunning the model batch."""
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state FROM wiki_task_batches WHERE task_id=? AND batch_no=?",
                    (task_id, batch_no),
                ).fetchone()
                if row is None:
                    raise WikiTaskError("batch_not_found")
                if row["state"] == "succeeded":
                    return self._task(connection, task_id)
                if row["state"] != "failed":
                    raise WikiTaskError("invalid_transition")
                for state in ("queued", "preparing", "running", "validating", "publishing"):
                    connection.execute(
                        "UPDATE wiki_task_batches SET state=?,error_code=NULL WHERE task_id=? AND batch_no=?",
                        (state, task_id, batch_no),
                    )
                connection.execute(
                    "UPDATE wiki_task_batches SET state='succeeded',error_code=NULL WHERE task_id=? AND batch_no=?",
                    (task_id, batch_no),
                )
                connection.execute(
                    """UPDATE wiki_tasks SET completed_batch_count=completed_batch_count+1,
                       updated_at=? WHERE task_id=?""",
                    (_timestamp(), task_id),
                )
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def fail_task(self, task_id: str, error_code: str, *, recovery_phase: str = "none") -> WikiTask:
        if error_code not in ERROR_CODES:
            raise WikiTaskError("error_code_invalid")
        if recovery_phase not in {"none", "staging", "publishing", "readback"}:
            raise WikiTaskError("recovery_phase_invalid")
        recovery_state = "not_needed" if recovery_phase == "none" else "required"
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT state,recovery_state FROM wiki_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if current is None:
                    raise WikiTaskError("task_not_found")
                if (current["state"] == "failed" and current["recovery_state"] != "not_needed"
                        and recovery_state == "not_needed"):
                    raise WikiTaskError("recovery_required")
                connection.execute(
                    """UPDATE wiki_tasks SET state='failed', error_code=?, recovery_state=?,
                       recovery_phase=?, updated_at=? WHERE task_id=?""",
                    (error_code, recovery_state, recovery_phase, _timestamp(), task_id),
                )
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def fail_batch(self, task_id: str, batch_no: int, error_code: str) -> WikiTask:
        if error_code not in ERROR_CODES:
            raise WikiTaskError("error_code_invalid")
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute(
                    "UPDATE wiki_task_batches SET state='failed', error_code=? WHERE task_id=? AND batch_no=?",
                    (error_code, task_id, batch_no),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise WikiTaskError("batch_not_found")
                connection.execute("UPDATE wiki_tasks SET updated_at=? WHERE task_id=?",
                                   (_timestamp(), task_id))
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def set_recovery_state(self, task_id: str, state: str) -> WikiTask:
        if state not in {"running", "succeeded", "failed"}:
            raise WikiTaskError("recovery_state_invalid")
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute(
                    "UPDATE wiki_tasks SET recovery_state=?, updated_at=? WHERE task_id=?",
                    (state, _timestamp(), task_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise WikiTaskError("task_not_found")
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error

    def retry_failed(self, task_id: str) -> WikiTask:
        return self._resolve_failed(task_id, "queued")

    def _resolve_failed(self, task_id: str, target: str) -> WikiTask:
        initialize(self.database_path)
        try:
            with connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state,recovery_state FROM wiki_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is None:
                    raise WikiTaskError("task_not_found")
                if row["state"] != "failed" or row["recovery_state"] not in {"not_needed", "succeeded"}:
                    raise WikiTaskError("recovery_required")
                if target == "queued":
                    connection.execute(
                        """UPDATE wiki_task_batches SET state='queued', error_code=NULL
                           WHERE task_id=? AND state='failed'""", (task_id,))
                connection.execute(
                    """UPDATE wiki_tasks SET state=?, error_code=NULL, recovery_state='not_needed',
                       recovery_phase='none', updated_at=? WHERE task_id=?""",
                    (target, _timestamp(), task_id),
                )
                return self._task(connection, task_id)
        except sqlite3.IntegrityError as error:
            raise WikiTaskError("invalid_transition") from error
