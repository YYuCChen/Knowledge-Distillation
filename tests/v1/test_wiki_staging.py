from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from knowledge_distiller.v1.wiki_kit import verify_source_kit
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_session_broker import WikiSessionBroker
from knowledge_distiller.v1.wiki_staging import (
    WikiStagingError,
    accept_validated_batch,
    prepare_staging,
    validate_staging,
    verify_formal_inputs,
)
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore


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
    path.write_text(
        f"---\n编号: {raw_id}\n格式版本: 1\n身份: 第三方\n"
        f"收录于: 2026-10-01T10:{number:02d}:00+08:00\n---\n\n合成素材 {number}。\n\n^source-1\n",
        encoding="utf-8")
    return path


def _setup(tmp_path: Path):
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    vault.mkdir()
    runtime.mkdir(mode=0o700)
    _install(vault)
    _raw(vault, 1)
    _raw(vault, 2)
    (vault / ".graph/queries.jsonl").write_text(
        '{"日期":"2026-10-01","页面":[]}\n', encoding="utf-8")
    store = WikiTaskStore(tmp_path / "app.sqlite3", kit_root=KIT,
                          python_executable=sys.executable)
    task = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web", backend="codex_cli",
        model="gpt-5.6-sol", effort="xhigh")
    with VaultWriteLock.acquire(vault) as lock:
        snapshot = prepare_staging(vault, runtime, task.task_id, task.raw,
                                   python_executable=sys.executable,
                                   source_kit_root=KIT, lock=lock)
    return vault, runtime, task, snapshot


def _process(staging: Path, runtime: Path, paths: list[str]) -> None:
    with open(staging / "wiki/log.md", "a", encoding="utf-8") as stream:
        for path in paths:
            stream.write(f"\n## [2026-10-01] ingest | 合成\n- 已处理 {path}\n")
    with WikiSessionBroker(staging, runtime) as broker:
        environment = os.environ.copy()
        environment.update(broker.environment())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, str(staging / "tools/kb.py"), "--root", str(staging)],
            cwd=staging, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_batch_validation_consumes_only_batch_and_advances_baseline(tmp_path):
    vault, runtime, task, snapshot = _setup(tmp_path)
    first, second = (item.relative_path for item in task.raw)
    _process(snapshot.workspace, runtime, [first])
    validated = validate_staging(snapshot, 1, [first], python_executable=sys.executable,
                                 source_kit_root=KIT)
    assert validated.pending_after == (second,)
    assert {
        "wiki/log.md", "wiki/index.md", "wiki/待确认.md", ".graph/graph.json",
        ".graph/state.json", ".graph/检查结果.md",
    } <= {change.relative_path for change in validated.changes}
    accepted = accept_validated_batch(snapshot, validated)
    assert accepted.pending_before == (second,)

    _raw(vault, 3)  # Late raw is counted, not appended to the frozen staging input.
    with VaultWriteLock.acquire(vault) as lock:
        check = verify_formal_inputs(snapshot, vault, lock=lock)
        assert check.late_raw_count == 1
        topic = vault / "wiki/主题/商业.md"
        topic.write_text(topic.read_text(encoding="utf-8") + "\n用户同时修改。\n", encoding="utf-8")
        with pytest.raises(WikiStagingError, match="publish_conflict"):
            verify_formal_inputs(snapshot, vault, lock=lock)


def test_validation_rejects_raw_change_future_batch_and_protected_graph(tmp_path):
    _vault, runtime, task, snapshot = _setup(tmp_path)
    first, second = (item.relative_path for item in task.raw)
    raw = snapshot.workspace.joinpath(*Path(first).parts)
    raw.write_text(raw.read_text(encoding="utf-8") + "changed", encoding="utf-8")
    with pytest.raises(WikiStagingError, match="raw_changed"):
        validate_staging(snapshot, 1, [first], python_executable=sys.executable,
                         source_kit_root=KIT)

    # Restore exact raw, then prove consuming a future batch is also rejected.
    original = next(item for item in snapshot.files if item.relative_path == first)
    formal_raw = _vault.joinpath(*Path(first).parts)
    raw.write_bytes(formal_raw.read_bytes())
    assert len(raw.read_bytes()) == original.byte_count
    _process(snapshot.workspace, runtime, [first, second])
    with pytest.raises(WikiStagingError, match="batch_boundary_invalid"):
        validate_staging(snapshot, 1, [first], python_executable=sys.executable,
                         source_kit_root=KIT)

    # Restore a fresh setup for the protected user query history check.
    other = tmp_path / "other"
    other.mkdir()
    _vault2, _runtime2, task2, snapshot2 = _setup(other)
    query = snapshot2.workspace / ".graph/queries.jsonl"
    query.write_text(query.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(WikiStagingError, match="validation_failed"):
        validate_staging(snapshot2, 1, [task2.raw[0].relative_path],
                         python_executable=sys.executable, source_kit_root=KIT)


def test_validation_rejects_symlink_and_unexpected_path(tmp_path):
    _vault, _runtime, task, snapshot = _setup(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (snapshot.workspace / "wiki/link.md").symlink_to(outside)
    with pytest.raises(WikiStagingError, match="path_symlink"):
        validate_staging(snapshot, 1, [task.raw[0].relative_path],
                         python_executable=sys.executable, source_kit_root=KIT)


def test_controller_exit_never_reuses_workspace_still_written_by_old_agent(tmp_path):
    vault, runtime, task, _initial = _setup(tmp_path)
    ready = tmp_path / "old-workspace"
    done = tmp_path / "old-done"
    controller = r'''
import os, subprocess, sys
from pathlib import Path
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_staging import prepare_staging
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore
database, kit, vault, runtime, task_id, ready, done = map(Path, sys.argv[1:])
store = WikiTaskStore(database, kit_root=kit, python_executable=sys.executable)
task = store.get(task_id.name)
with VaultWriteLock.acquire(vault) as lock:
    snapshot = prepare_staging(vault, runtime, task.task_id, task.raw,
        python_executable=sys.executable, source_kit_root=kit, lock=lock)
    code = "import sys,time; from pathlib import Path; time.sleep(1); Path(sys.argv[1]).write_text('old'); Path(sys.argv[2]).write_text('done')"
    subprocess.Popen([sys.executable, "-c", code,
        str(snapshot.workspace / "old-agent.txt"), str(done)],
        close_fds=True, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ready.write_text(str(snapshot.workspace))
    os._exit(0)
'''
    result = subprocess.run(
        [sys.executable, "-c", controller, str(store_path := tmp_path / "app.sqlite3"),
         str(KIT), str(vault), str(runtime), task.task_id, str(ready), str(done)],
        check=False, timeout=10)
    assert result.returncode == 0
    old_workspace = Path(ready.read_text())

    with VaultWriteLock.acquire(vault) as lock:
        replacement = prepare_staging(
            vault, runtime, task.task_id, task.raw,
            python_executable=sys.executable, source_kit_root=KIT, lock=lock)
    assert replacement.workspace != old_workspace
    assert not (replacement.workspace / "old-agent.txt").exists()
    deadline = time.monotonic() + 5
    while not done.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert done.exists()
    assert (old_workspace / "old-agent.txt").read_text() == "old"
    assert not (replacement.workspace / "old-agent.txt").exists()
