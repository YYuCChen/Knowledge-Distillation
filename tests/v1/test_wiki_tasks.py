from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from knowledge_distiller.v1.wiki_kit import verify_source_kit
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.database import initialize
from knowledge_distiller.v1.wiki_tasks import (
    ProtocolRaw,
    WikiTaskError,
    WikiTaskStore,
    _freeze_one,
    _protocol_path,
)


KIT = Path(__file__).resolve().parents[2] / "vault-kit"


def _install(vault: Path):
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


def _raw(vault: Path, number: int, *, identity="第三方", adjacency=(), target=""):
    raw_id = f"R-20261001-{number:04d}"
    folder = "外部" if identity == "第三方" else "自述"
    path = vault / "raw" / folder / "2026" / "10" / f"{raw_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = ""
    if target:
        extra += f"附言对象: {target}\n"
    if adjacency:
        extra += "邻接:\n" + "".join(
            f"  - {{编号: {other}, 间隔秒: 60}}\n" for other in adjacency)
    path.write_text(
        f"---\n编号: {raw_id}\n格式版本: 1\n身份: {identity}\n"
        f"收录于: 2026-10-01T10:{number % 60:02d}:00+08:00\n{extra}---\n\n"
        f"仅用于合成测试的正文 marker-{number}。\n\n^source-1\n",
        encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault)
    database = tmp_path / "app.sqlite3"
    store = WikiTaskStore(database, kit_root=KIT, python_executable=sys.executable)
    return vault, database, store


def test_sixty_plus_raw_are_frozen_once_batched_and_read_after_restart(setup):
    vault, database, store = setup
    for number in range(1, 63):
        _raw(vault, number)
    _raw(vault, 63, identity="本人附言", adjacency=("R-20261001-0001",),
         target="R-20261001-0001")
    task = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-5.6-sol", effort="xhigh")
    assert task.state == "queued"
    assert task.raw_count == 63
    assert task.batch_count == 13
    assert {item.raw_id for item in task.raw} == {f"R-20261001-{n:04d}" for n in range(1, 64)}
    first = {item.raw_id for item in task.raw if item.batch_no == 1}
    assert {"R-20261001-0001", "R-20261001-0063"} <= first
    assert len(first) == 5
    assert all(len(item.content_sha256) == 64 and item.byte_count > 0 for item in task.raw)

    # A late file belongs to the next scan and is never appended to this task.
    _raw(vault, 64)
    restarted = WikiTaskStore(database, kit_root=KIT, python_executable=sys.executable)
    loaded = restarted.get(task.task_id)
    assert loaded == task
    assert "R-20261001-0064" not in {item.raw_id for item in loaded.raw}
    child_code = """import json,sys
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore
s=WikiTaskStore(sys.argv[1],kit_root=sys.argv[2],python_executable=sys.executable)
t=s.get(sys.argv[3])
print(json.dumps({'task_id':t.task_id,'raw_count':t.raw_count,'batch_count':t.batch_count,
 'digests':sorted(x.content_sha256 for x in t.raw)}))
"""
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(database), str(KIT), task.task_id],
        capture_output=True, text=True, check=False)
    assert child.returncode == 0, child.stderr
    reopened = json.loads(child.stdout)
    assert reopened == {
        "task_id": task.task_id,
        "raw_count": task.raw_count,
        "batch_count": task.batch_count,
        "digests": sorted(item.content_sha256 for item in task.raw),
    }

    # The database stores no raw body or title, only frozen inventory metadata.
    assert b"marker-63" not in database.read_bytes()
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(wiki_task_raw)")}
    assert columns == {"task_id", "ordinal", "batch_no", "raw_id", "identity",
                       "relative_path", "byte_count", "content_sha256"}


def test_all_batches_progress_while_task_stays_running_then_task_finalizes(setup):
    vault, _, store = setup
    for number in range(1, 13):
        _raw(vault, number)
    task = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-5.6-sol", effort="high")
    task = store.set_task_state(task.task_id, "preparing")
    task = store.set_task_state(task.task_id, "running")
    for batch in task.batches:
        for state in ("preparing", "running", "validating", "publishing"):
            task = store.set_batch_state(task.task_id, batch.batch_no, state)
            assert task.state == "running"
        task = store.mark_batch_readback_succeeded(task.task_id, batch.batch_no)
        assert task.state == "running"
    assert task.completed_batch_count == task.batch_count
    task = store.set_task_state(task.task_id, "validating")
    task = store.set_task_state(task.task_id, "publishing")
    task = store.set_task_state(task.task_id, "succeeded")
    assert task.state == "succeeded"


def test_duplicate_reuses_boundary_late_boundary_is_busy_and_vaults_are_independent(setup, tmp_path):
    vault, database, store = setup
    _raw(vault, 1)
    first = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-5.6-sol", effort="medium")
    repeated = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-5.6-sol", effort="medium")
    assert repeated.task_id == first.task_id
    _raw(vault, 2)
    with pytest.raises(WikiTaskError, match="vault_busy"):
        store.create_or_reuse(
            vault, request_kind="all", trigger_source="local_web",
            backend="codex_cli", model="gpt-5.6-sol", effort="medium")

    other = tmp_path / "other-vault"
    other.mkdir()
    _install(other)
    _raw(other, 1)
    second = store.create_or_reuse(
        other, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-5.6-sol", effort="medium")
    assert second.task_id != first.task_id


def test_two_processes_create_one_active_task_then_reuse_it(setup):
    vault, database, store = setup
    _raw(vault, 1)
    initialize(database)
    start = vault / "start"
    child_code = """import json,sys,time
from pathlib import Path
from knowledge_distiller.v1.wiki_tasks import WikiTaskError,WikiTaskStore
while not Path(sys.argv[4]).exists(): time.sleep(.01)
s=WikiTaskStore(sys.argv[1],kit_root=sys.argv[2],python_executable=sys.executable)
try:
 t=s.create_or_reuse(sys.argv[3],request_kind='all',trigger_source='local_web',
                     backend='codex_cli',model='gpt-5.6-sol',effort='medium')
 print(json.dumps({'task_id':t.task_id}))
except WikiTaskError as e:
 print(json.dumps({'error':str(e)}))
"""
    command = [sys.executable, "-c", child_code, str(database), str(KIT), str(vault), str(start)]
    children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
    start.write_text("go", encoding="utf-8")
    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert all(row.get("error") in (None, "vault_busy") for row in results)
    task_ids = {row["task_id"] for row in results if "task_id" in row}
    assert len(task_ids) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM wiki_tasks
               WHERE state IN ('queued','preparing','running','validating','publishing','failed')"""
        ).fetchone()[0] == 1
    repeated = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-5.6-sol", effort="medium")
    assert repeated.task_id == task_ids.pop()


def test_illegal_transition_and_failed_recovery_require_explicit_retry(setup):
    vault, _, store = setup
    _raw(vault, 1)
    task = store.create_or_reuse(
        vault, request_kind="one_batch", trigger_source="cli",
        backend="codex_cli", model="gpt-5.6-sol", effort="medium")
    with pytest.raises(WikiTaskError, match="invalid_transition"):
        store.set_task_state(task.task_id, "running")
    store.set_task_state(task.task_id, "preparing")
    store.set_task_state(task.task_id, "running")
    failed = store.fail_task(task.task_id, "publish_interrupted", recovery_phase="publishing")
    assert (failed.state, failed.recovery_state, failed.recovery_phase) == (
        "failed", "required", "publishing")
    with pytest.raises(WikiTaskError, match="recovery_required"):
        store.fail_task(task.task_id, "internal_error", recovery_phase="none")
    with pytest.raises(WikiTaskError, match="invalid_transition"):
        store.set_recovery_state(task.task_id, "succeeded")
    with pytest.raises(WikiTaskError, match="recovery_required"):
        store.retry_failed(task.task_id)
    store.set_recovery_state(task.task_id, "running")
    store.set_recovery_state(task.task_id, "succeeded")
    retried = store.retry_failed(task.task_id)
    assert (retried.state, retried.error_code, retried.recovery_state, retried.recovery_phase) == (
        "queued", None, "not_needed", "none")


def test_older_unresolved_recovery_blocks_matching_or_superseding_later_failure(setup):
    vault, database, store = setup
    _raw(vault, 1)
    older = store.create_or_reuse(
        vault, request_kind="all", trigger_source="local_web",
        backend="codex_cli", model="gpt-old", effort="high")
    older = store.fail_task(
        older.task_id, "publish_interrupted", recovery_phase="publishing")
    later_id = "f" * 32
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO wiki_tasks(
                task_id,vault_path,vault_key,request_kind,trigger_source,
                backend,model,effort,kit_version,kit_manifest_sha256,
                boundary_sha256,state,raw_count,batch_count,
                completed_batch_count,error_code,recovery_state,recovery_phase,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (later_id, older.vault_path, older.vault_key, "all", "local_web",
             "codex_cli", "gpt-later", "high", older.kit_version,
             older.kit_manifest_sha256, "e" * 64, "failed", 0, 0, 0,
             "model_unavailable", "not_needed", "none",
             "2026-10-01T01:00:00+00:00", "2026-10-01T01:00:00+00:00"),
        )

    for model in ("gpt-later", "gpt-new"):
        with pytest.raises(WikiTaskError, match="recovery_required"):
            store.create_or_reuse(
                vault, request_kind="all", trigger_source="local_web",
                backend="codex_cli", model=model, effort="high",
                allow_supersede_failed=True)

    assert store.get(older.task_id).recovery_state == "required"
    assert store.get(later_id).state == "failed"


def test_manual_lock_conflicts_and_raw_path_guards(setup, tmp_path):
    vault, _, store = setup
    _raw(vault, 1)
    with VaultWriteLock.acquire(vault):
        with pytest.raises(WikiTaskError, match="vault_busy"):
            store.create_or_reuse(
                vault, request_kind="all", trigger_source="local_web",
                backend="codex_cli", model="gpt-5.6-sol", effort="low")

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    raw = vault / "raw/外部/2026/10/R-20261001-0001.md"
    raw.unlink()
    raw.symlink_to(outside)
    row = ProtocolRaw("raw/外部/2026/10/R-20261001-0001.md", "R-20261001-0001",
                      "第三方", "", "", (), len(b"outside"), "0" * 64)
    with pytest.raises(WikiTaskError, match="raw_symlink"):
        _freeze_one(vault, row)
    with pytest.raises(WikiTaskError, match="raw_path_invalid"):
        _protocol_path("raw/外部/../../private.md")


def test_local_web_only_accepts_all(setup):
    vault, _, store = setup
    with pytest.raises(WikiTaskError, match="request_kind_invalid"):
        store.create_or_reuse(
            vault, request_kind="one_batch", trigger_source="local_web",
            backend="codex_cli", model="gpt-5.6-sol", effort="low")
