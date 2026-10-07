"""Local Web service boundary for the durable wiki workflow."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .wiki_tasks import (
    WikiTask, WikiTaskError, WikiTaskStore, scan_workflow_protocol,
)
from .wiki_worker import WikiWorker
from .wiki_lock import WikiLockError, canonical_vault
from .worker_lifecycle import AdmissionError, WorkAdmissionGate


ACTIVE_STATES = {"queued", "preparing", "running", "validating", "publishing"}
RESULT_PATHS = ("wiki/index.md", "wiki/待确认.md")


class WikiWorkflow:
    def __init__(self, store: WikiTaskStore, worker: WikiWorker,
                 settings: Callable[[], dict[str, str]], gate: WorkAdmissionGate):
        self.store = store
        self.worker = worker
        self.settings = settings
        self.gate = gate

    @staticmethod
    def _empty(*, state: str = "unknown", error_code: str | None = None,
               actions: tuple[str, ...] = ()) -> dict:
        return {
            "state": state,
            "task_id": None,
            "raw_count": None,
            "batch_count": None,
            "completed_batch_count": None,
            "candidate_count": None,
            "error_code": error_code,
            "recovery_state": None,
            "actions": list(actions),
            "result_relpaths": [],
        }

    def _vault(self) -> Path:
        values = self.settings()
        raw = values.get("vault_path")
        if not raw:
            raise WikiTaskError("config_required")
        return Path(raw)

    @staticmethod
    def _configured(values: dict[str, str]) -> bool:
        return bool(
            values.get("vault_path") and values.get("llm_provider") == "codex"
            and values.get("llm_model") and values.get("llm_effort")
        )

    def _record_error(self, vault: Path, code: str) -> None:
        if code not in {"vault_busy", "kit_missing", "kit_drift", "kit_incompatible", "protocol_error",
                        "raw_path_invalid", "raw_symlink", "raw_changed"}:
            return
        try:
            self.store.record_observation(vault, scan=None, error_code=code)
        except WikiTaskError:
            pass

    def _observe_now(self, vault: Path, task_id: str | None = None) -> None:
        try:
            scan = scan_workflow_protocol(vault, runtime=self.store.runtime)
            self.store.record_observation(vault, scan=scan, task_id=task_id)
        except WikiTaskError as error:
            self._record_error(vault, str(error))

    def _task_status(self, task: WikiTask, candidate_count: int | None) -> dict:
        if task.state in ACTIVE_STATES:
            state, actions = task.state, ()
        elif task.state == "failed":
            state = "failed"
            actions = (("settings",) if task.error_code in {
                "config_required", "runner_unavailable", "model_unavailable",
                "kit_missing", "kit_drift", "kit_incompatible",
            } else ("retry",))
        else:
            state, actions = "succeeded", ("open_index", "open_pending")
        return {
            "state": state,
            "task_id": task.task_id,
            "raw_count": task.raw_count,
            "batch_count": task.batch_count,
            "completed_batch_count": task.completed_batch_count,
            "candidate_count": candidate_count,
            "error_code": task.error_code,
            "recovery_state": task.recovery_state,
            "actions": list(actions),
            "result_relpaths": list(RESULT_PATHS) if task.state == "succeeded" else [],
        }

    def snapshot(self) -> dict:
        values = self.settings()
        if not self._configured(values):
            return self._empty(state="config_required", error_code="config_required",
                               actions=("settings",))
        try:
            vault = self._vault()
            task = self.store.status_task_for_vault(vault)
            observation = self.store.observation_for_vault(vault)
        except WikiTaskError as error:
            code = str(error)
            if code in {"config_required", "raw_path_invalid"}:
                return self._empty(state="config_required", error_code="config_required",
                                   actions=("settings",))
            return self._empty(state="failed", error_code="internal_error",
                               actions=("refresh",))
        candidate = observation.candidate_count if observation is not None else None
        if task is not None and task.state in ACTIVE_STATES:
            return self._task_status(task, candidate)
        if observation is not None and observation.error_code == "vault_busy":
            return self._empty(state="failed", error_code="vault_busy",
                               actions=("refresh",))
        if task is not None and task.state == "failed":
            try:
                desired_manifest = self.store.runtime.verify().manifest_sha256
            except Exception:
                desired_manifest = task.kit_manifest_sha256
            execution_changed = (
                task.backend, task.model, task.effort, task.kit_manifest_sha256,
            ) != (
                "codex_cli", values.get("llm_model"), values.get("llm_effort"),
                desired_manifest,
            )
            if (execution_changed
                    and task.recovery_state in {"not_needed", "succeeded"}):
                result = self._empty(state="ready", actions=("submit",))
                result["raw_count"] = (
                    observation.pending_count if observation is not None else None)
                result["candidate_count"] = candidate
                return result
            result = self._task_status(task, candidate)
            if (task.error_code in {"config_required", "runner_unavailable", "model_unavailable"}
                    and values.get("llm_state") == "configured"
                    and task.recovery_state in {"not_needed", "succeeded"}):
                result["actions"] = ["retry"]
            return result
        if observation is not None and observation.error_code is not None:
            actions = (("settings",) if observation.error_code in {
                "config_required", "kit_missing", "kit_drift", "kit_incompatible",
            } else ("refresh",))
            return self._empty(state="failed", error_code=observation.error_code,
                               actions=actions)
        if observation is not None and observation.pending_count:
            result = self._empty(state="ready", actions=("submit", "refresh"))
            result["raw_count"] = observation.pending_count
            result["candidate_count"] = observation.candidate_count
            return result
        if task is not None:
            return self._task_status(task, candidate)
        result = self._empty(state="idle", actions=("refresh",))
        if observation is not None:
            result["raw_count"] = observation.pending_count
            result["candidate_count"] = observation.candidate_count
        return result

    def submit_all(self) -> dict:
        try:
            with self.gate.enter():
                values = self.settings()
                vault = self._vault()
                if values.get("llm_provider") != "codex":
                    raise WikiTaskError("config_required")
                model, effort = values.get("llm_model", ""), values.get("llm_effort", "")
                if not model or not effort:
                    raise WikiTaskError("config_required")
                task = self.store.create_or_reuse(
                    vault, request_kind="all", trigger_source="local_web",
                    backend="codex_cli", model=model, effort=effort,
                    allow_supersede_failed=True)
                self._observe_now(vault, task.task_id)
                self.worker.wake()
                self.worker.request_observation(vault, force=True)
                return self._task_status(task, None)
        except AdmissionError:
            return self._empty(state="failed", error_code="update_reserved")
        except WikiTaskError as error:
            code = str(error)
            if code == "no_pending_raw":
                try:
                    self._observe_now(self._vault())
                except WikiTaskError:
                    pass
                result = self.snapshot()
                result["error_code"] = "no_pending_raw"
                return result
            if code == "config_required":
                return self._empty(state="config_required", error_code=code,
                                   actions=("settings",))
            if code == "vault_busy":
                current = self.snapshot()
                if (current.get("task_id") is not None
                        and current.get("state") in ACTIVE_STATES):
                    return current
                try:
                    self._record_error(self._vault(), code)
                except WikiTaskError:
                    pass
                return self.snapshot()
            if code == "recovery_required":
                return self.snapshot()
            allowed = {
                "vault_busy", "kit_missing", "kit_drift", "kit_incompatible",
                "protocol_error", "raw_path_invalid", "raw_symlink", "raw_changed",
                "model_unavailable",
            }
            fixed = code if code in allowed else "internal_error"
            try:
                self._record_error(self._vault(), fixed)
            except WikiTaskError:
                pass
            actions = (("settings",) if fixed in {
                "kit_missing", "kit_drift", "kit_incompatible", "model_unavailable",
            } else ("refresh",))
            return self._empty(state="failed", error_code=fixed, actions=actions)

    def retry(self, task_id: str) -> dict:
        try:
            with self.gate.enter():
                task = self.store.get(task_id)
                try:
                    selected_vault = canonical_vault(self._vault())
                except WikiLockError as error:
                    raise WikiTaskError("task_not_found") from error
                if task.vault_path != str(selected_vault):
                    raise WikiTaskError("task_not_found")
                if task.state != "failed":
                    raise WikiTaskError("invalid_transition")
                if task.recovery_state in {"required", "failed"}:
                    recovered = self.worker.recover_task(task_id)
                    if recovered.error_code == "vault_busy":
                        raise WikiTaskError("vault_busy")
                    if recovered.error_code == "recovery_failed":
                        return self.snapshot()
                task = self.store.retry_failed(task_id)
                self.worker.wake()
                return self._task_status(task, None)
        except AdmissionError:
            return self._empty(state="failed", error_code="update_reserved")
        except (OSError, WikiTaskError) as error:
            code = str(error)
            if code == "vault_busy":
                try:
                    self._record_error(self._vault(), code)
                except WikiTaskError:
                    pass
                return self.snapshot()
            allowed = {"task_not_found", "invalid_transition", "recovery_required", "vault_busy"}
            return self._empty(state="failed", error_code=code if code in allowed else "internal_error")

    def request_refresh(self, *, force: bool = False) -> dict:
        try:
            vault = self._vault()
            self.worker.request_observation(vault, force=force)
            return self.snapshot()
        except WikiTaskError:
            return self._empty(state="config_required", error_code="config_required",
                               actions=("settings",))
