"""Independent coordinator for durable V3 wiki maintenance tasks."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Callable

from .wiki_lock import VaultWriteLock, WikiLockError
from .wiki_publish import (
    PublishExpectation,
    PublishState,
    WikiPublishError,
    publish_wiki,
    recover_wiki,
)
from .wiki_runner import CodexWikiRunner, RunnerResult, WikiRunnerError
from .wiki_session_broker import WikiSessionBroker
from .wiki_staging import (
    StagingSnapshot,
    ValidatedBatch,
    WikiStagingError,
    accept_validated_batch,
    cleanup_staging,
    load_staging_snapshot,
    prepare_staging,
    validate_staging,
    verify_formal_inputs,
    verify_recovered_publish,
    verify_staging_protected,
)
from .wiki_kit import WikiKitError, verify_source_kit
from .wiki_kit_runtime import WikiKitRuntimeError
from .wiki_tasks import WikiTask, WikiTaskError, WikiTaskStore, scan_workflow_protocol


@dataclass(frozen=True)
class WikiWorkResult:
    task_id: str
    error_code: str | None


class WikiWorker:
    """Run wiki tasks without occupying the source-distillation worker."""

    def __init__(self, store: WikiTaskStore, runtime_root: Path | str,
                 runner: CodexWikiRunner, *, idle_seconds: float = 0.5,
                 publish: Callable = publish_wiki, recover: Callable = recover_wiki):
        self.store = store
        self.runtime_root = Path(runtime_root)
        self.runner = runner
        self.idle_seconds = idle_seconds
        self.publish = publish
        self.recover = recover
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._activity = threading.Lock()
        self._update_reserved = False
        self._observation_guard = threading.Lock()
        self._observation_requests: dict[str, bool] = {}
        self._observation_last: dict[str, float] = {}
        self.observation_interval = 30.0

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        reset = getattr(self.runner, "reset_cancellation", None)
        if reset is not None:
            reset()
        self._stopping.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="wiki-worker", daemon=True)
        self._thread.start()
        return True

    def wake(self) -> None:
        self._wake.set()

    def request_observation(self, vault: Path | str, *, force: bool = False) -> bool:
        value = os.fspath(vault)
        with self._observation_guard:
            last = self._observation_last.get(value, 0.0)
            if (not force and value not in self._observation_requests
                    and time.monotonic() - last < self.observation_interval):
                return False
            self._observation_requests[value] = (
                force or self._observation_requests.get(value, False))
        self.wake()
        return True

    def stop(self, timeout: float = 10) -> bool:
        self._stopping.set()
        self._wake.set()
        self.runner.cancel()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
            self._thread = None
        return True

    def pending_work(self) -> bool:
        from .database import connect
        with connect(self.store.database_path) as connection:
            return connection.execute(
                """SELECT 1 FROM wiki_tasks
                   WHERE state IN ('queued','preparing','running','validating','publishing')
                      OR (state='failed' AND recovery_state IN ('required','running','failed'))
                   LIMIT 1"""
            ).fetchone() is not None

    def reserve_for_update(self) -> bool:
        if not self._activity.acquire(blocking=False):
            return False
        try:
            if self.pending_work():
                return False
            self._update_reserved = True
            return True
        finally:
            self._activity.release()

    def update_ready(self) -> bool:
        return not self._activity.locked() and not self.pending_work()

    def release_update(self) -> None:
        with self._activity:
            self._update_reserved = False
        self.wake()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            result = None
            with self._activity:
                if not self._update_reserved and not self._stopping.is_set():
                    result = self.run_one()
                    if result is not None:
                        try:
                            completed = self.store.get(result.task_id)
                            self.request_observation(completed.vault_path, force=True)
                        except WikiTaskError:
                            pass
                    else:
                        self._refresh_observation()
            if result is None:
                self._wake.wait(self.idle_seconds)
                self._wake.clear()

    def _refresh_observation(self) -> bool:
        with self._observation_guard:
            if not self._observation_requests:
                return False
            vault, _force = self._observation_requests.popitem()
        error_code = None
        scan = None
        try:
            with VaultWriteLock.acquire(vault):
                scan = scan_workflow_protocol(vault, runtime=self.store.runtime)
        except BlockingIOError:
            error_code = "vault_busy"
        except WikiTaskError as error:
            code = str(error)
            error_code = code if code in {
                "protocol_error", "kit_missing", "kit_drift", "kit_incompatible",
                "raw_path_invalid", "raw_symlink", "raw_changed",
            } else "protocol_error"
        except (OSError, WikiLockError):
            error_code = "raw_path_invalid"
        try:
            self.store.record_observation(vault, scan=scan, error_code=error_code)
        except WikiTaskError:
            pass
        with self._observation_guard:
            self._observation_last[vault] = time.monotonic()
        return True

    def run_one(self) -> WikiWorkResult | None:
        skipped: set[str] = set()
        while True:
            task = self.store.claim_next_runnable(exclude_task_ids=frozenset(skipped))
            if task is None:
                return None
            try:
                lock = VaultWriteLock.acquire(task.vault_path)
                break
            except BlockingIOError:
                # Try other Vaults before sleeping; never spin on a busy head task.
                skipped.add(task.task_id)
            except (OSError, WikiLockError):
                self._safe_fail(task.task_id, None, "validation_failed",
                                recovery_phase="none")
                skipped.add(task.task_id)
        try:
            with lock:
                return self._run_locked(self.store.get(task.task_id), lock)
        except Exception:
            # Keep the worker alive without persisting arbitrary exception text.
            # A journal-bearing batch must retain its recovery gate.
            try:
                current = self.store.get(task.task_id)
                phase = ("publishing" if any(batch.state == "publishing"
                                             for batch in current.batches) else "none")
            except Exception:
                phase = "none"
            self._safe_fail(task.task_id, None, "internal_error", recovery_phase=phase)
            return WikiWorkResult(task.task_id, "internal_error")

    def _run_locked(self, task: WikiTask, lock: VaultWriteLock) -> WikiWorkResult:
        recovered = self._recover_interrupted_publish(task, lock)
        if isinstance(recovered, WikiWorkResult):
            return recovered
        task = recovered
        if task.completed_batch_count == task.batch_count:
            try:
                cleanup_staging(load_staging_snapshot(
                    self.runtime_root / "wiki-tasks" / task.task_id))
            except WikiStagingError:
                pass
            return self._finalize(task)

        if task.state == "queued":
            task = self.store.set_task_state(task.task_id, "preparing")
        try:
            snapshot = prepare_staging(
                task.vault_path, self.runtime_root, task.task_id, task.raw,
                python_executable=self.store.python_executable,
                source_kit_root=self.store.kit_root, lock=lock,
                kit_runtime=self.store.runtime)
        except WikiStagingError as error:
            code = self._staging_code(str(error))
            self._safe_fail(task.task_id, None, code, recovery_phase="none")
            return WikiWorkResult(task.task_id, code)
        if task.state == "preparing":
            task = self.store.set_task_state(task.task_id, "running")

        for batch in task.batches:
            if batch.state == "succeeded":
                continue
            if batch.state != "queued":
                task = self._reset_unpublished_batch(task, batch.batch_no)
                if task.state == "queued":
                    task = self.store.set_task_state(task.task_id, "preparing")
                    task = self.store.set_task_state(task.task_id, "running")
            raw_paths = tuple(item.relative_path for item in task.raw
                              if item.batch_no == batch.batch_no)
            self.store.set_batch_state(task.task_id, batch.batch_no, "preparing")
            self.store.set_batch_state(task.task_id, batch.batch_no, "running")
            try:
                result = self.runner.run(
                    snapshot.workspace, self.runtime_root, model=task.model, effort=task.effort,
                    batch_no=batch.batch_no, raw_paths=raw_paths)
            except WikiRunnerError as error:
                code = self._runner_code(str(error))
                return self._failed(task.task_id, batch.batch_no, code, snapshot)
            if not result.succeeded:
                return self._failed(task.task_id, batch.batch_no,
                                    self._runner_code(result.error_code or "agent_failed"), snapshot)
            if not self._trusted_kb(snapshot, task):
                return self._failed(task.task_id, batch.batch_no, "validation_failed", snapshot)
            self.store.set_batch_state(task.task_id, batch.batch_no, "validating")
            try:
                validated = validate_staging(
                    snapshot, batch.batch_no, raw_paths,
                    python_executable=self.store.python_executable,
                    source_kit_root=self.store.kit_root,
                    kit_runtime=self.store.runtime)
                formal_check = verify_formal_inputs(snapshot, task.vault_path, lock=lock)
            except WikiStagingError as error:
                return self._failed(task.task_id, batch.batch_no,
                                    self._staging_code(str(error)), snapshot)

            health_performed = False
            if validated.health_due:
                if formal_check.late_raw_count == 0:
                    before_health = validated
                    try:
                        health_result = self.runner.run_health(
                            snapshot.workspace, self.runtime_root,
                            model=task.model, effort=task.effort)
                    except WikiRunnerError as error:
                        return self._failed(task.task_id, batch.batch_no,
                                            self._runner_code(str(error)), snapshot)
                    if not health_result.succeeded or not self._trusted_kb(snapshot, task):
                        code = self._runner_code(health_result.error_code or "validation_failed")
                        return self._failed(task.task_id, batch.batch_no, code, snapshot)
                    try:
                        validated = validate_staging(
                            snapshot, batch.batch_no, raw_paths,
                            python_executable=self.store.python_executable,
                            source_kit_root=self.store.kit_root,
                            kit_runtime=self.store.runtime)
                    except WikiStagingError as error:
                        return self._failed(task.task_id, batch.batch_no,
                                            self._staging_code(str(error)), snapshot)
                    if not self._health_completed(snapshot, before_health, validated):
                        return self._failed(task.task_id, batch.batch_no,
                                            "validation_failed", snapshot)
                    health_performed = True

            try:
                formal_check = verify_formal_inputs(snapshot, task.vault_path, lock=lock)
            except WikiStagingError as error:
                return self._failed(task.task_id, batch.batch_no,
                                    self._staging_code(str(error)), snapshot)
            if health_performed and formal_check.late_raw_count:
                return self._failed(task.task_id, batch.batch_no, "validation_failed", snapshot)
            result = self._publish_batch(task, snapshot, validated, lock,
                                         health_performed=health_performed)
            if isinstance(result, WikiWorkResult):
                return result
            snapshot, task = result

        cleanup_staging(snapshot)
        return self._finalize(self.store.get(task.task_id))

    def _trusted_kb(self, snapshot: StagingSnapshot, task: WikiTask) -> bool:
        try:
            verify_staging_protected(snapshot)
            manifest = verify_source_kit(self.store.kit_root)
            if manifest.manifest_sha256 != task.kit_manifest_sha256:
                return False
            with WikiSessionBroker(snapshot.workspace, self.runtime_root) as session:
                result = self.store.runtime.run(
                    "kb", snapshot.workspace,
                    session_environment=session.environment())
                return result.returncode == 0
        except (OSError, UnicodeError, WikiStagingError, WikiKitError,
                WikiKitRuntimeError):
            return False

    @staticmethod
    def _health_completed(snapshot: StagingSnapshot, before: ValidatedBatch,
                          after: ValidatedBatch) -> bool:
        baseline = {item.relative_path: item.sha256 for item in snapshot.files}
        before_changes = {item.relative_path: item.after_sha256 for item in before.changes}
        after_changes = {item.relative_path: item.after_sha256 for item in after.changes}
        before_report = before_changes.get("wiki/体检报告.md",
                                           baseline.get("wiki/体检报告.md"))
        after_report = after_changes.get("wiki/体检报告.md",
                                         baseline.get("wiki/体检报告.md"))
        return (
            before.health_due
            and after.health_eligible
            and not after.health_due
            and after.health_due_reason == "current"
            and after.lint_count == before.lint_count + 1
            and before_report is not None
            and after_report is not None
            and before_report != after_report
        )

    def _publish_batch(self, task: WikiTask, snapshot: StagingSnapshot,
                       validated: ValidatedBatch, lock: VaultWriteLock, *,
                       health_performed: bool,
                       ) -> tuple[StagingSnapshot, WikiTask] | WikiWorkResult:
        if not validated.changes:
            return self._failed(task.task_id, validated.batch_no,
                                "validation_failed", snapshot)
        expected = {
            item.relative_path: PublishExpectation(item.before_sha256, item.after_sha256)
            for item in validated.changes
        }
        journal = snapshot.control / f"publish-{validated.batch_no}"
        self.store.set_batch_state(task.task_id, validated.batch_no, "publishing")
        try:
            published = self.publish(
                task.vault_path, snapshot.workspace, journal, expected, lock=lock)
            if published.state != PublishState.COMMITTED:
                raise WikiPublishError("publish_interrupted")
            accepted = accept_validated_batch(snapshot, validated)
            check = verify_formal_inputs(accepted, task.vault_path, lock=lock)
            protocol = scan_workflow_protocol(task.vault_path, runtime=self.store.runtime)
            frozen = {item.relative_path for item in task.raw}
            formal_frozen_pending = {item.relative_path for item in protocol.pending} & frozen
            expected_frozen_pending = set(validated.pending_after) & frozen
            if formal_frozen_pending != expected_frozen_pending:
                raise WikiPublishError("readback_failed")
            if health_performed and check.late_raw_count:
                raise WikiPublishError("readback_failed")
            task = self.store.mark_batch_readback_succeeded(task.task_id, validated.batch_no)
            return accepted, task
        except (WikiPublishError, WikiStagingError, WikiTaskError) as error:
            code = self._publish_code(str(error))
            self._safe_fail(task.task_id, validated.batch_no, code,
                            recovery_phase="publishing")
            return WikiWorkResult(task.task_id, code)

    def _recover_interrupted_publish(self, task: WikiTask,
                                     lock: VaultWriteLock) -> WikiTask | WikiWorkResult:
        publishing = [batch for batch in task.batches if batch.state == "publishing"]
        if not publishing:
            return task
        task_root = self.runtime_root / "wiki-tasks" / task.task_id
        try:
            snapshot = load_staging_snapshot(task_root)
        except WikiStagingError:
            return self._recovery_failed(task, publishing[0].batch_no, "recovery_failed")
        for batch in publishing:
            journal = snapshot.control / f"publish-{batch.batch_no}"
            if not journal.is_dir():
                return self._recovery_failed(task, batch.batch_no, "recovery_failed")
            try:
                result = self.recover(task.vault_path, journal, lock=lock)
            except WikiPublishError:
                return self._recovery_failed(task, batch.batch_no, "recovery_failed")
            if result.state == PublishState.RECOVERY_FAILED:
                return self._recovery_failed(task, batch.batch_no, "recovery_failed")
            if result.state == PublishState.RECOVERED:
                self.store.fail_batch(task.task_id, batch.batch_no, "publish_interrupted")
                self.store.fail_task(task.task_id, "publish_interrupted",
                                     recovery_phase="publishing")
                self.store.set_recovery_state(task.task_id, "running")
                self.store.set_recovery_state(task.task_id, "succeeded")
                return WikiWorkResult(task.task_id, "publish_interrupted")
            if not self._committed_publish_proven(task, batch.batch_no, snapshot,
                                                  journal, result.paths, lock):
                return self._recovery_failed(task, batch.batch_no, "publish_conflict")
            task = self.store.mark_batch_readback_succeeded(task.task_id, batch.batch_no)
        return task

    def recover_task(self, task_id: str) -> WikiWorkResult:
        """Explicitly resolve a failed publish journal; never retry the model."""
        if not self._activity.acquire(blocking=False):
            return WikiWorkResult(task_id, "vault_busy")
        try:
            return self._recover_task(task_id)
        finally:
            self._activity.release()

    def _recover_task(self, task_id: str) -> WikiWorkResult:
        try:
            task = self.store.get(task_id)
            if (task.state != "failed" or task.recovery_phase != "publishing"
                    or task.recovery_state not in {"required", "failed"}):
                return WikiWorkResult(task_id, "recovery_failed")
            lock = VaultWriteLock.acquire(task.vault_path)
        except BlockingIOError:
            return WikiWorkResult(task_id, "vault_busy")
        except (WikiTaskError, OSError):
            return WikiWorkResult(task_id, "recovery_failed")
        with lock:
            self.store.set_recovery_state(task_id, "running")
            task = self.store.get(task_id)
            failed = [batch for batch in task.batches if batch.state == "failed"]
            if len(failed) != 1:
                self.store.set_recovery_state(task_id, "failed")
                return WikiWorkResult(task_id, "recovery_failed")
            batch = failed[0]
            task_root = self.runtime_root / "wiki-tasks" / task_id
            try:
                snapshot = load_staging_snapshot(task_root)
                journal = snapshot.control / f"publish-{batch.batch_no}"
                result = self.recover(task.vault_path, journal, lock=lock)
                if result.state == PublishState.RECOVERY_FAILED:
                    raise WikiPublishError("recovery_failed")
                if result.state == PublishState.COMMITTED:
                    if not self._committed_publish_proven(
                        task, batch.batch_no, snapshot, journal, result.paths, lock
                    ):
                        raise WikiPublishError("publish_conflict")
                    self.store.mark_recovered_batch_readback_succeeded(task_id, batch.batch_no)
                self.store.set_recovery_state(task_id, "succeeded")
                return WikiWorkResult(task_id, "publish_interrupted")
            except (WikiPublishError, WikiStagingError, WikiTaskError):
                self.store.set_recovery_state(task_id, "failed")
                return WikiWorkResult(task_id, "recovery_failed")

    def _recovery_failed(self, task: WikiTask, batch_no: int,
                         code: str) -> WikiWorkResult:
        self._safe_fail(task.task_id, batch_no, code, recovery_phase="publishing")
        try:
            current = self.store.get(task.task_id)
            if current.recovery_state == "required":
                self.store.set_recovery_state(task.task_id, "running")
                self.store.set_recovery_state(task.task_id, "failed")
        except WikiTaskError:
            pass
        return WikiWorkResult(task.task_id, code)

    def _committed_publish_proven(self, task: WikiTask, batch_no: int,
                                  snapshot: StagingSnapshot, journal: Path,
                                  paths: tuple[str, ...], lock: VaultWriteLock) -> bool:
        try:
            after = self._journal_after(journal, paths)
            verify_recovered_publish(snapshot, task.vault_path, after, lock=lock)
            manifest = verify_source_kit(self.store.kit_root)
            if manifest.manifest_sha256 != task.kit_manifest_sha256:
                return False
            protocol = scan_workflow_protocol(task.vault_path, runtime=self.store.runtime)
            frozen = {item.relative_path for item in task.raw}
            expected = {
                item.relative_path for item in task.raw
                if item.batch_no != batch_no
                and next(batch.state for batch in task.batches
                         if batch.batch_no == item.batch_no) != "succeeded"
            }
            return ({item.relative_path for item in protocol.pending} & frozen) == expected
        except (OSError, UnicodeError, ValueError, WikiKitError,
                WikiStagingError, WikiTaskError):
            return False

    @staticmethod
    def _journal_after(journal: Path, paths: tuple[str, ...]) -> dict[str, str]:
        target = journal / "journal.json"
        info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("journal")
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("state") != "committed":
            raise ValueError("journal")
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("journal")
        after: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("journal")
            relative, digest = item.get("path"), item.get("after")
            if (not isinstance(relative, str) or relative in after
                    or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                raise ValueError("journal")
            after[relative] = digest
        if set(after) != set(paths):
            raise ValueError("journal")
        return after

    def _reset_unpublished_batch(self, task: WikiTask, batch_no: int) -> WikiTask:
        self.store.fail_batch(task.task_id, batch_no, "interrupted")
        self.store.fail_task(task.task_id, "interrupted", recovery_phase="none")
        return self.store.retry_failed(task.task_id)

    def _failed(self, task_id: str, batch_no: int, code: str,
                snapshot: StagingSnapshot) -> WikiWorkResult:
        self._safe_fail(task_id, batch_no, code, recovery_phase="none")
        try:
            cleanup_staging(snapshot)
        except WikiStagingError:
            pass
        return WikiWorkResult(task_id, code)

    def _safe_fail(self, task_id: str, batch_no: int | None, code: str, *,
                   recovery_phase: str) -> None:
        try:
            if batch_no is not None:
                current = self.store.get(task_id)
                row = next((batch for batch in current.batches if batch.batch_no == batch_no), None)
                if row is not None and row.state != "failed":
                    self.store.fail_batch(task_id, batch_no, code)
            current = self.store.get(task_id)
            if current.state != "failed":
                self.store.fail_task(task_id, code, recovery_phase=recovery_phase)
        except WikiTaskError:
            pass

    def _finalize(self, task: WikiTask) -> WikiWorkResult:
        try:
            if task.state == "queued":
                task = self.store.set_task_state(task.task_id, "preparing")
            if task.state == "preparing":
                task = self.store.set_task_state(task.task_id, "running")
            if task.state == "running":
                task = self.store.set_task_state(task.task_id, "validating")
            if task.state == "validating":
                task = self.store.set_task_state(task.task_id, "publishing")
            if task.state == "publishing":
                task = self.store.set_task_state(task.task_id, "succeeded")
            if task.state != "succeeded":
                raise WikiTaskError("invalid_transition")
            return WikiWorkResult(task.task_id, None)
        except WikiTaskError:
            self._safe_fail(task.task_id, None, "readback_failed", recovery_phase="readback")
            return WikiWorkResult(task.task_id, "readback_failed")

    @staticmethod
    def _runner_code(code: str) -> str:
        return code if code in {
            "config_required", "runner_unavailable", "runner_timeout", "model_unavailable",
            "network_error", "agent_failed", "vault_busy", "interrupted",
        } else "agent_failed"

    @staticmethod
    def _staging_code(code: str) -> str:
        if code == "raw_changed":
            return code
        if code == "publish_conflict":
            return code
        return "validation_failed"

    @staticmethod
    def _publish_code(code: str) -> str:
        if code in {"target_digest_changed", "publish_path_changed", "publish_conflict"}:
            return "publish_conflict"
        if code == "readback_failed":
            return code
        return "publish_interrupted"
