from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys

from knowledge_distiller.v1.wiki_tasks import (
    ProtocolRaw,
    ProtocolScan,
    WikiTaskStore,
)
from knowledge_distiller.v1.wiki_workflow import WikiWorkflow
from knowledge_distiller.v1.wiki_worker import WikiWorker
from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate

from .test_wiki_tasks import KIT, _install, _raw


class Worker:
    def __init__(self):
        self.wakes = 0
        self.refreshes = []

    def wake(self):
        self.wakes += 1

    def request_observation(self, vault, *, force=False):
        self.refreshes.append((str(vault), force))
        return True

    def recover_task(self, task_id):
        raise AssertionError("recovery was not expected: " + task_id)


def _settings(vault: Path, *, model="gpt-old", state="configured"):
    return {
        "vault_path": str(vault),
        "llm_provider": "codex",
        "llm_model": model,
        "llm_effort": "high",
        "llm_state": state,
    }


def _workflow(tmp_path: Path, vault: Path, settings: dict):
    task_store = WikiTaskStore(
        tmp_path / "app.sqlite3", kit_root=KIT, python_executable=sys.executable)
    worker = Worker()
    workflow = WikiWorkflow(task_store, worker, lambda: dict(settings), WorkAdmissionGate())
    return task_store, worker, workflow


def test_snapshot_reads_persisted_observation_without_running_protocol(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    store, _worker, workflow = _workflow(tmp_path, vault, _settings(vault))
    scan = ProtocolScan((ProtocolRaw(
        "raw/外部/2026/10/R-20261001-0001.md", "R-20261001-0001", "第三方",
        "2026-10-01T00:00:00+08:00", "", (), 12, "a" * 64),),
        (("错误", 0), ("提醒", 0), ("信息", 0)), 3,
        False, False, "not_eligible", "", 0)
    store.record_observation(vault, scan=scan)
    monkeypatch.setattr(store.runtime, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("GET must not scan the Vault")))

    snapshot = workflow.snapshot()
    assert snapshot["state"] == "ready"
    assert snapshot["raw_count"] == 1
    assert snapshot["candidate_count"] == 3
    assert snapshot["actions"] == ["submit", "refresh"]


def test_submit_kit_error_is_persisted_for_redirected_snapshot(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store, _worker, workflow = _workflow(tmp_path, vault, _settings(vault))

    submitted = workflow.submit_all()
    assert submitted["error_code"] == "kit_missing"
    assert submitted["actions"] == ["settings"]
    after_redirect = workflow.snapshot()
    assert after_redirect["error_code"] == "kit_missing"
    assert after_redirect["actions"] == ["settings"]
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT pending_count,candidate_count,error_code FROM wiki_observations"
        ).fetchone()
    assert row == (None, None, "kit_missing")


def test_changed_model_creates_new_task_and_preserves_failed_history(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault)
    _raw(vault, 1)
    settings = _settings(vault)
    store, worker, workflow = _workflow(tmp_path, vault, settings)
    old = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web", backend="codex_cli",
        model="gpt-old", effort="high")
    old = store.fail_task(old.task_id, "model_unavailable")
    settings["llm_model"] = "gpt-new"

    ready = workflow.snapshot()
    assert ready["state"] == "ready"
    assert ready["actions"] == ["submit"]
    created = workflow.submit_all()
    assert created["task_id"] != old.task_id
    assert created["state"] == "queued"
    typed = store.get(created['task_id'])
    assert typed.outcome_contract == 'r08-wiki-outcomes-v1'
    assert typed.plan_sha256 and typed.plan_json != '{}'
    assert worker.wakes == 1
    assert store.get(old.task_id) == old
    with sqlite3.connect(store.database_path) as connection:
        rows = connection.execute(
            "SELECT task_id,model,state FROM wiki_tasks ORDER BY rowid"
        ).fetchall()
    assert rows == [(old.task_id, "gpt-old", "failed"),
                    (created["task_id"], "gpt-new", "queued")]


def test_changed_model_cannot_bypass_required_publish_recovery(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault)
    _raw(vault, 1)
    settings = _settings(vault)
    store, _worker, workflow = _workflow(tmp_path, vault, settings)
    old = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web", backend="codex_cli",
        model="gpt-old", effort="high")
    store.fail_task(old.task_id, "publish_interrupted", recovery_phase="publishing")
    settings["llm_model"] = "gpt-new"

    result = workflow.submit_all()
    assert result["task_id"] == old.task_id
    assert result["state"] == "failed"
    assert result["recovery_state"] == "required"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM wiki_tasks").fetchone()[0] == 1


def test_older_unresolved_recovery_owns_status_over_later_safe_failure(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault)
    _raw(vault, 1)
    settings = _settings(vault, model="gpt-new")
    store, _worker, workflow = _workflow(tmp_path, vault, settings)
    older = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web", backend="codex_cli",
        model="gpt-old", effort="high")
    older = store.fail_task(
        older.task_id, "publish_interrupted", recovery_phase="publishing")
    later_id = "f" * 32
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """INSERT INTO wiki_tasks(
                task_id,vault_path,vault_key,request_kind,trigger_source,
                backend,model,effort,kit_version,kit_manifest_sha256,
                boundary_sha256,state,raw_count,batch_count,
                completed_batch_count,error_code,recovery_state,recovery_phase,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (later_id, older.vault_path, older.vault_key, "all", "local_web",
             "codex_cli", "gpt-new", "high", older.kit_version,
             older.kit_manifest_sha256, "e" * 64, "failed", 0, 0, 0,
             "model_unavailable", "not_needed", "none",
             "2026-10-01T01:00:00+00:00", "2026-10-01T01:00:00+00:00"),
        )

    snapshot = workflow.snapshot()
    assert snapshot["task_id"] == older.task_id
    assert snapshot["recovery_state"] == "required"
    assert snapshot["actions"] == ["retry"]
    submitted = workflow.submit_all()
    assert submitted["task_id"] == older.task_id
    assert submitted["recovery_state"] == "required"


def test_manual_session_lock_persists_conflict_then_background_scan_recovers(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault)
    _raw(vault, 1)
    settings = _settings(vault)
    store = WikiTaskStore(
        tmp_path / "app.sqlite3", kit_root=KIT, python_executable=sys.executable)
    worker = WikiWorker(store, tmp_path / "runtime", object())
    workflow = WikiWorkflow(store, worker, lambda: dict(settings), WorkAdmissionGate())
    release = tmp_path / "release-lock"
    holder_code = """from pathlib import Path
import sys,time
print('locked',flush=True)
p=Path(sys.argv[1])
while not p.exists(): time.sleep(.01)
"""
    holder = subprocess.Popen(
        [sys.executable, str(vault / "tools/wiki_session.py"), "--root", str(vault),
         "--", sys.executable, "-c", holder_code, str(release)],
        cwd=vault, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        submitted = workflow.submit_all()
        assert submitted["error_code"] == "vault_busy"
        assert submitted["actions"] == ["refresh"]
        polled = workflow.snapshot()
        assert polled["error_code"] == "vault_busy"
        with sqlite3.connect(store.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM wiki_tasks").fetchone()[0] == 0
    finally:
        release.write_text("release", encoding="utf-8")
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, stdout + stderr

    workflow.request_refresh(force=True)
    assert worker._refresh_observation() is True
    recovered = workflow.snapshot()
    assert recovered["state"] == "ready"
    assert recovered["error_code"] is None
    assert recovered["raw_count"] == 1

    task = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-old", effort="high")
    store.fail_task(task.task_id, "model_unavailable")
    settings["llm_model"] = "gpt-new"
    release2 = tmp_path / "release-lock-2"
    holder2 = subprocess.Popen(
        [sys.executable, str(vault / "tools/wiki_session.py"), "--root", str(vault),
         "--", sys.executable, "-c", holder_code, str(release2)],
        cwd=vault, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder2.stdout is not None
        assert holder2.stdout.readline().strip() == "locked"
        conflicted = workflow.submit_all()
        assert conflicted["task_id"] is None
        assert conflicted["error_code"] == "vault_busy"
        assert workflow.snapshot()["error_code"] == "vault_busy"
    finally:
        release2.write_text("release", encoding="utf-8")
        stdout, stderr = holder2.communicate(timeout=10)
        assert holder2.returncode == 0, stdout + stderr

    workflow.request_refresh(force=True)
    assert worker._refresh_observation() is True
    assert workflow.snapshot()["state"] == "ready"
    active = workflow.submit_all()
    assert active["state"] == "queued"
    assert active["task_id"] != task.task_id

    release3 = tmp_path / "release-lock-3"
    holder3 = subprocess.Popen(
        [sys.executable, str(vault / "tools/wiki_session.py"), "--root", str(vault),
         "--", sys.executable, "-c", holder_code, str(release3)],
        cwd=vault, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder3.stdout is not None
        assert holder3.stdout.readline().strip() == "locked"
        repeated = workflow.submit_all()
        assert repeated["task_id"] == active["task_id"]
        assert repeated["state"] == "queued"
        assert repeated["error_code"] is None
    finally:
        release3.write_text("release", encoding="utf-8")
        stdout, stderr = holder3.communicate(timeout=10)
        assert holder3.returncode == 0, stdout + stderr


def test_retry_lock_conflict_is_persisted_until_background_scan_recovers(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault)
    _raw(vault, 1)
    settings = _settings(vault)
    store = WikiTaskStore(
        tmp_path / "app.sqlite3", kit_root=KIT, python_executable=sys.executable)
    worker = WikiWorker(store, tmp_path / "runtime", object())
    workflow = WikiWorkflow(store, worker, lambda: dict(settings), WorkAdmissionGate())
    task = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-old", effort="high")
    store.fail_task(task.task_id, "publish_interrupted", recovery_phase="publishing")
    release = tmp_path / "release-retry-lock"
    holder_code = """from pathlib import Path
import sys,time
print('locked',flush=True)
p=Path(sys.argv[1])
while not p.exists(): time.sleep(.01)
"""
    holder = subprocess.Popen(
        [sys.executable, str(vault / "tools/wiki_session.py"), "--root", str(vault),
         "--", sys.executable, "-c", holder_code, str(release)],
        cwd=vault, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        retried = workflow.retry(task.task_id)
        assert retried["error_code"] == "vault_busy"
        assert workflow.snapshot()["error_code"] == "vault_busy"
    finally:
        release.write_text("release", encoding="utf-8")
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, stdout + stderr

    workflow.request_refresh(force=True)
    assert worker._refresh_observation() is True
    recovered = workflow.snapshot()
    assert recovered["task_id"] == task.task_id
    assert recovered["recovery_state"] == "required"
    assert recovered["actions"] == ["retry"]
