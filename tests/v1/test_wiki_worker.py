from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from knowledge_distiller.v1.wiki_kit import verify_source_kit
from knowledge_distiller.v1.wiki_publish import WikiPublishError, publish_wiki
from knowledge_distiller.v1.wiki_runner import RunnerResult
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_staging import load_staging_snapshot
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore, scan_workflow_protocol
from knowledge_distiller.v1.wiki_worker import WikiWorker


KIT = Path(__file__).resolve().parents[2] / "vault-kit"


def _install(vault: Path) -> None:
    manifest = verify_source_kit(KIT)
    for item in manifest.files:
        target = vault / item.install_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT / item.source_path, target)
    receipt = {
        "kit_version": manifest.kit_version,
        "protocol_version": manifest.protocol_version,
        "manifest_sha256": manifest.manifest_sha256,
        "files": [item.__dict__ for item in manifest.files],
    }
    (vault / ".kd").mkdir(exist_ok=True)
    (vault / ".kd/wiki-kit.json").write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(vault / "tools/wiki_session.py"), "--root", str(vault), "--",
         sys.executable, str(vault / "tools/kb.py"), "init", "--root", str(vault)],
        capture_output=True, text=True, cwd=vault, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def _raw(vault: Path, number: int) -> Path:
    raw_id = f"R-20261001-{number:04d}"
    path = vault / "raw/外部/2026/10" / f"{raw_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    hour = 10 + (number - 1) // 60
    minute = (number - 1) % 60
    path.write_text(
        f"---\n编号: {raw_id}\n格式版本: 1\n身份: 第三方\n"
        f"收录于: 2026-10-01T{hour:02d}:{minute:02d}:00+08:00\n---\n\n合成素材 {number}。\n\n^source-1\n",
        encoding="utf-8")
    return path


def _setup(tmp_path: Path, count: int = 1):
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    vault.mkdir()
    runtime.mkdir(mode=0o700)
    _install(vault)
    raws = [_raw(vault, number) for number in range(1, count + 1)]
    store = WikiTaskStore(tmp_path / "app.sqlite3", kit_root=KIT,
                          python_executable=sys.executable)
    task = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web", backend="codex_cli",
        model="gpt-test", effort="high")
    return vault, runtime, raws, store, task


class FakeRunner:
    def __init__(self, *, error: str | None = None, after_ingest=None):
        self.error = error
        self.after_ingest = after_ingest
        self.batches: list[tuple[str, ...]] = []
        self.health_calls = 0

    def run(self, staging_vault, _runtime_root, **kwargs):
        paths = tuple(kwargs["raw_paths"])
        self.batches.append(paths)
        if self.error:
            return RunnerResult(self.error)
        with open(Path(staging_vault) / "wiki/log.md", "a", encoding="utf-8") as stream:
            stream.write("\n## [2026-10-01] ingest | 合成批次\n")
            for path in paths:
                stream.write(f"- 已处理 {path}\n")
        if self.after_ingest is not None:
            self.after_ingest(len(self.batches))
        return RunnerResult(None)

    def run_health(self, staging_vault, _runtime_root, **_kwargs):
        self.health_calls += 1
        root = Path(staging_vault)
        with open(root / "wiki/log.md", "a", encoding="utf-8") as stream:
            stream.write("\n## [2026-10-01] lint | 合成完整体检\n- 完成。\n")
        (root / "wiki/体检报告.md").write_text(
            "# 体检报告 2026-10-01\n\n合成体检完成。\n", encoding="utf-8")
        return RunnerResult(None)


@pytest.mark.parametrize("count", [6, 63])
def test_worker_runs_multiple_batches_health_publish_and_readback(tmp_path, count):
    vault, runtime, raws, store, task = _setup(tmp_path, count=count)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in raws}
    runner = FakeRunner()
    result = WikiWorker(store, runtime, runner).run_one()
    assert result is not None and result.error_code is None
    finished = store.get(task.task_id)
    assert finished.state == "succeeded"
    expected_batches = [5] * (count // 5) + ([count % 5] if count % 5 else [])
    assert finished.completed_batch_count == finished.batch_count == len(expected_batches)
    assert [len(batch) for batch in runner.batches] == expected_batches
    assert runner.health_calls == 1
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in raws} == before
    assert not (runtime / "wiki-tasks" / task.task_id / "current-attempt").exists()
    protocol = scan_workflow_protocol(vault, sys.executable, vault / "tools/kb.py")
    assert protocol.pending == ()
    assert protocol.health_due is False
    assert protocol.lint_count == 1


def test_worker_failure_is_fixed_and_never_publishes_partial_staging(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path)
    before = (vault / "wiki/log.md").read_bytes()
    result = WikiWorker(store, runtime, FakeRunner(error="agent_failed")).run_one()
    assert result is not None and result.error_code == "agent_failed"
    failed = store.get(task.task_id)
    assert failed.state == "failed"
    assert failed.batches[0].state == "failed"
    assert (vault / "wiki/log.md").read_bytes() == before
    assert not (runtime / "wiki-tasks" / task.task_id / "current-attempt").exists()


def test_late_raw_stays_for_next_task_and_prevents_automatic_health(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path)

    def arrive(_batch_count):
        _raw(vault, 2)

    runner = FakeRunner(after_ingest=arrive)
    result = WikiWorker(store, runtime, runner).run_one()
    assert result is not None and result.error_code is None
    assert runner.health_calls == 0
    finished = store.get(task.task_id)
    assert finished.state == "succeeded"
    protocol = scan_workflow_protocol(vault, sys.executable, vault / "tools/kb.py")
    assert [item.raw_id for item in protocol.pending] == ["R-20261001-0002"]
    assert protocol.health_eligible is False


def test_raw_arriving_after_freeze_before_prepare_stays_out_of_all_batches(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path, count=6)
    late = _raw(vault, 7)
    late_before = late.read_bytes()
    runner = FakeRunner()

    result = WikiWorker(store, runtime, runner).run_one()

    assert result is not None and result.error_code is None
    assert [len(batch) for batch in runner.batches] == [5, 1]
    assert all(late.relative_to(vault).as_posix() not in batch for batch in runner.batches)
    assert runner.health_calls == 0
    assert late.read_bytes() == late_before
    protocol = scan_workflow_protocol(vault, sys.executable, vault / "tools/kb.py")
    assert [item.raw_id for item in protocol.pending] == ["R-20261001-0007"]


def test_agent_modified_kb_is_rejected_before_controller_execution(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path)
    canary = tmp_path / "outside-canary"

    class MaliciousRunner(FakeRunner):
        def run(self, staging_vault, runtime_root, **kwargs):
            result = super().run(staging_vault, runtime_root, **kwargs)
            (Path(staging_vault) / "tools/kb.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(canary)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            return result

    result = WikiWorker(store, runtime, MaliciousRunner()).run_one()

    assert result is not None and result.error_code == "validation_failed"
    assert not canary.exists()
    assert store.get(task.task_id).state == "failed"
    assert not (runtime / "wiki-tasks" / task.task_id / "current-attempt").exists()


def test_busy_first_vault_does_not_starve_second_vault(tmp_path):
    vault, runtime, _raws, store, first = _setup(tmp_path)
    second_vault = tmp_path / "second-vault"
    second_vault.mkdir()
    _install(second_vault)
    _raw(second_vault, 2)
    second = store.create_or_reuse(
        second_vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-test", effort="high")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE wiki_tasks SET created_at='2026-10-01T00:00:00+00:00' WHERE task_id=?",
                           (first.task_id,))
        connection.execute("UPDATE wiki_tasks SET created_at='2026-10-01T00:00:01+00:00' WHERE task_id=?",
                           (second.task_id,))

    with VaultWriteLock.acquire(vault):
        result = WikiWorker(store, runtime, FakeRunner()).run_one()

    assert result is not None and result.task_id == second.task_id and result.error_code is None
    assert store.get(second.task_id).state == "succeeded"
    assert store.get(first.task_id).state == "preparing"


def test_unexpected_runner_exception_becomes_fixed_error_and_next_task_runs(tmp_path):
    vault, runtime, _raws, store, first = _setup(tmp_path)

    class BrokenRunner(FakeRunner):
        def run(self, *_args, **_kwargs):
            raise RuntimeError("arbitrary body must not persist")

    failed = WikiWorker(store, runtime, BrokenRunner()).run_one()
    assert failed is not None and failed.error_code == "internal_error"
    assert store.get(first.task_id).error_code == "internal_error"

    second_vault = tmp_path / "after-error-vault"
    second_vault.mkdir()
    _install(second_vault)
    _raw(second_vault, 2)
    second = store.create_or_reuse(
        second_vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-test", effort="high")
    completed = WikiWorker(store, runtime, FakeRunner()).run_one()
    assert completed is not None and completed.task_id == second.task_id
    assert completed.error_code is None


def test_missing_queued_vault_fails_fixed_and_does_not_stop_next_vault(tmp_path):
    vault, runtime, _raws, store, first = _setup(tmp_path)
    moved = tmp_path / "moved-vault"
    vault.rename(moved)
    second_vault = tmp_path / "valid-vault"
    second_vault.mkdir()
    _install(second_vault)
    _raw(second_vault, 2)
    second = store.create_or_reuse(
        second_vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-test", effort="high")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE wiki_tasks SET created_at='2026-10-01T00:00:00+00:00' WHERE task_id=?",
                           (first.task_id,))
        connection.execute("UPDATE wiki_tasks SET created_at='2026-10-01T00:00:01+00:00' WHERE task_id=?",
                           (second.task_id,))

    result = WikiWorker(store, runtime, FakeRunner()).run_one()

    assert result is not None and result.task_id == second.task_id and result.error_code is None
    failed = store.get(first.task_id)
    assert failed.state == "failed" and failed.error_code == "validation_failed"


def test_committed_publish_is_verified_and_completed_after_worker_restart(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path)

    def crash_after_commit(*args, **kwargs):
        publish_wiki(*args, **kwargs)
        raise SystemExit(91)

    with pytest.raises(SystemExit) as stopped:
        WikiWorker(store, runtime, FakeRunner(), publish=crash_after_commit).run_one()
    assert stopped.value.code == 91
    interrupted = store.get(task.task_id)
    assert interrupted.state == "running"
    assert interrupted.batches[0].state == "publishing"

    resumed = WikiWorker(store, runtime, FakeRunner()).run_one()
    assert resumed is not None and resumed.error_code is None
    assert store.get(task.task_id).state == "succeeded"


def test_committed_publish_never_overwrites_later_user_edit_during_recovery(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path)

    def crash_after_commit(*args, **kwargs):
        publish_wiki(*args, **kwargs)
        raise SystemExit(92)

    with pytest.raises(SystemExit):
        WikiWorker(store, runtime, FakeRunner(), publish=crash_after_commit).run_one()
    log = vault / "wiki/log.md"
    log.write_text(log.read_text(encoding="utf-8") + "\n用户后续编辑。\n", encoding="utf-8")
    user_bytes = log.read_bytes()

    resumed = WikiWorker(store, runtime, FakeRunner()).run_one()
    assert resumed is not None and resumed.error_code == "publish_conflict"
    failed = store.get(task.task_id)
    assert failed.state == "failed"
    assert failed.recovery_state == "failed"
    assert log.read_bytes() == user_bytes


def test_recovery_uses_committed_journal_not_mutable_staging_bytes(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path)

    def crash_after_commit(*args, **kwargs):
        publish_wiki(*args, **kwargs)
        raise SystemExit(93)

    with pytest.raises(SystemExit):
        WikiWorker(store, runtime, FakeRunner(), publish=crash_after_commit).run_one()
    snapshot = load_staging_snapshot(runtime / "wiki-tasks" / task.task_id)
    staging_log = snapshot.workspace / "wiki/log.md"
    staging_log.write_text("Agent后改的非权威内容。\n", encoding="utf-8")

    resumed = WikiWorker(store, runtime, FakeRunner()).run_one()
    assert resumed is not None and resumed.error_code is None
    assert store.get(task.task_id).state == "succeeded"
    assert "Agent后改" not in (vault / "wiki/log.md").read_text(encoding="utf-8")


def test_failed_committed_publish_requires_explicit_recovery_then_retry(tmp_path):
    vault, runtime, _raws, store, task = _setup(tmp_path)

    def report_failure_after_commit(*args, **kwargs):
        publish_wiki(*args, **kwargs)
        raise WikiPublishError("publish_io_failed")

    failed = WikiWorker(
        store, runtime, FakeRunner(), publish=report_failure_after_commit).run_one()
    assert failed is not None and failed.error_code == "publish_interrupted"
    current = store.get(task.task_id)
    assert current.state == "failed" and current.recovery_state == "required"

    recovered = WikiWorker(store, runtime, FakeRunner()).recover_task(task.task_id)
    assert recovered.error_code == "publish_interrupted"
    current = store.get(task.task_id)
    assert current.state == "failed"
    assert current.recovery_state == "succeeded"
    assert current.completed_batch_count == 1

    store.retry_failed(task.task_id)
    resumed_runner = FakeRunner()
    resumed = WikiWorker(store, runtime, resumed_runner).run_one()
    assert resumed is not None and resumed.error_code is None
    assert resumed_runner.batches == []
    assert store.get(task.task_id).state == "succeeded"
