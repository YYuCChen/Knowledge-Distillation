from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from knowledge_distiller.v1 import data_upgrade_probe as probe
from knowledge_distiller.v1 import mac_app
from knowledge_distiller.v1.paths import AppPaths


SCHEMA_21 = Path(__file__).with_name("fixtures") / "wiki-schema21.sql"
SENTINEL = "private-setting-and-body-must-not-enter-report"


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _prepare(root: Path) -> tuple[Path, Path]:
    _private_directory(root)
    vault = _private_directory(root / probe.VAULT_RELPATH)
    (vault / "raw").mkdir(mode=0o700)
    (vault / "raw/example.md").write_text("synthetic vault only", encoding="utf-8")
    database = root / probe.DATABASE_NAME
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_21.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO settings(key,value) VALUES('vault_path',?)", (str(vault),))
        connection.execute("INSERT INTO settings(key,value) VALUES('private_sentinel',?)", (SENTINEL,))
        connection.execute(
            """INSERT INTO materials(material_id,source_kind,source_key,submitted_url,
                    canonical_url,metadata_json,created_at)
               VALUES(1,'synthetic','source-1','https://example.invalid/submitted',
                    'https://example.invalid/canonical',?,'2026-10-01T00:00:00Z')""",
            (json.dumps({"private": SENTINEL}),),
        )
        body = "synthetic raw " + SENTINEL
        connection.execute(
            """INSERT INTO raw_records(
                    raw_id,subject_kind,subject_id,identity,relative_path,content,
                    content_sha256,attachments_json,supersedes,origin,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("R-20261001-0001", "material", 1, "第三方",
             "raw/外部/2026/10/R-20261001-0001.md", body,
             hashlib.sha256(body.encode()).hexdigest(), "[]", None, "app",
             "2026-10-01T00:00:00Z"),
        )
    database.chmod(0o600)
    marker = {
        "schema_version": 1,
        "purpose": probe.MARKER_PURPOSE,
        "database_name": probe.DATABASE_NAME,
        "database_before_sha256": probe._digest_file(database),
        "expected_schema": 21,
        "vault_relpath": probe.VAULT_RELPATH,
        "vault_tree_sha256": probe._digest_tree(vault),
    }
    marker_path = root / probe.MARKER_NAME
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    marker_path.chmod(0o600)
    return database, vault


def _report_parent(tmp_path: Path, name: str = "evidence") -> tuple[Path, Path]:
    parent = _private_directory(tmp_path / name)
    return parent, parent / "report.json"


def _formal(tmp_path: Path) -> Path:
    return tmp_path / "formal-never-opened"


def test_schema21_copy_upgrades_to_24_without_changing_legacy_rows_or_vault(tmp_path):
    database, vault = _prepare(tmp_path / "attempt")
    _, report = _report_parent(tmp_path)
    vault_before = probe._digest_tree(vault)
    assert probe.run(database.parent, report, formal_root=_formal(tmp_path)) == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["ok"] is True and result["error_code"] is None
    assert result["database"]["schema_before"] == 21
    assert result["database"]["schema_after"] == 24
    assert result["database"]["quick_check_before"] is True
    assert result["database"]["quick_check_after"] is True
    assert result["database"]["foreign_key_violations_before"] == 0
    assert result["database"]["foreign_key_violations_after"] == 0
    assert result["legacy"]["table_counts_before"] == result["legacy"]["table_counts_after"]
    assert result["legacy"]["table_counts_after"]["materials"] == 1
    assert result["legacy"]["table_counts_after"]["raw_records"] == 1
    assert result["legacy"]["digest_before"] == result["legacy"]["digest_after"]
    assert result["legacy"]["unchanged"] is True
    assert result["vault"] == {
        "sha256_before": vault_before, "sha256_after": vault_before, "unchanged": True}
    assert SENTINEL not in report.read_text(encoding="utf-8")
    assert report.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 24
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wiki_%'"
        )} == {"wiki_tasks", "wiki_task_batches", "wiki_task_raw", "wiki_observations", "wiki_outcome_receipts"}


def test_mac_dispatch_is_early_and_accepts_equals_data_dir(tmp_path, monkeypatch):
    database, _ = _prepare(tmp_path / "attempt")
    _, report = _report_parent(tmp_path)
    monkeypatch.setattr(mac_app, "create_application", lambda *a, **k: pytest.fail("app started"))
    monkeypatch.setattr(mac_app.subprocess, "Popen", lambda *a, **k: pytest.fail("process started"))
    with pytest.raises(SystemExit) as stopped:
        mac_app.main([
            f"--data-dir={database.parent}",
            "--check-data-upgrade", str(report),
        ])
    assert stopped.value.code == 0
    assert json.loads(report.read_text())["ok"] is True


def test_mac_dispatch_requires_explicit_data_dir_before_default_root_is_touched(tmp_path, monkeypatch):
    formal = tmp_path / "formal"
    monkeypatch.setattr(mac_app.AppPaths, "mac_default", classmethod(lambda cls: AppPaths(formal)))
    _, report = _report_parent(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        mac_app.main(["--check-data-upgrade", str(report)])
    assert stopped.value.code == 2
    assert not formal.exists() and not report.exists()


def test_marker_binds_database_bytes_and_schema21(tmp_path):
    database, _ = _prepare(tmp_path / "attempt")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE settings SET value='changed' WHERE key='private_sentinel'")
    _, changed_report = _report_parent(tmp_path, "changed-evidence")
    assert probe.run(database.parent, changed_report, formal_root=_formal(tmp_path)) == 1
    assert json.loads(changed_report.read_text()) == {
        "schema_version": 1, "ok": False, "error_code": "database_changed"}
    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone()[0] == 21

    database, _ = _prepare(tmp_path / "schema-attempt")
    marker_path = database.parent / probe.MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["expected_schema"] = 22  # Real schema21 remains unchanged; only the declaration is wrong.
    marker_path.write_text(json.dumps(marker));marker_path.chmod(0o600)
    _, schema_report = _report_parent(tmp_path, "schema-evidence")
    assert probe.run(database.parent, schema_report, formal_root=_formal(tmp_path)) == 1
    assert json.loads(schema_report.read_text())["error_code"] == "unsupported_schema"


def test_symlinked_data_or_vault_and_formal_relation_are_rejected(tmp_path):
    database, vault = _prepare(tmp_path / "attempt")
    alias = tmp_path / "attempt-link"
    alias.symlink_to(database.parent, target_is_directory=True)
    _, report = _report_parent(tmp_path, "alias-evidence")
    assert probe.run(alias, report, formal_root=_formal(tmp_path)) == 1
    assert json.loads(report.read_text())["error_code"] == "fixture_invalid"

    moved = tmp_path / "real-vault"
    vault.rename(moved)
    vault.symlink_to(moved, target_is_directory=True)
    _, report = _report_parent(tmp_path, "vault-evidence")
    assert probe.run(database.parent, report, formal_root=_formal(tmp_path)) == 1
    assert json.loads(report.read_text())["error_code"] == "fixture_invalid"

    database, _ = _prepare(tmp_path / "formal-attempt")
    _, report = _report_parent(tmp_path, "formal-evidence")
    assert probe.run(database.parent, report, formal_root=database.parent) == 1
    assert json.loads(report.read_text())["error_code"] == "fixture_invalid"


def test_busy_instance_lock_and_existing_report_fail_closed(tmp_path):
    database, _ = _prepare(tmp_path / "attempt")
    _, report = _report_parent(tmp_path)
    descriptor = os.open(database.parent / ".instance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert probe.run(database.parent, report, formal_root=_formal(tmp_path)) == 1
    finally:
        os.close(descriptor)
    assert json.loads(report.read_text())["error_code"] == "fixture_busy"
    before = report.read_bytes()
    assert probe.run(database.parent, report, formal_root=_formal(tmp_path)) == 1
    assert report.read_bytes() == before


def test_report_inside_data_root_and_hardlinked_database_are_rejected(tmp_path):
    database, vault = _prepare(tmp_path / "attempt")
    vault_before = probe._digest_tree(vault)
    for inside in (database.parent / "report.json", vault / "report.json"):
        assert probe.run(database.parent, inside, formal_root=_formal(tmp_path)) == 1
        assert not inside.exists()
    assert probe._digest_tree(vault) == vault_before
    linked = tmp_path / "database-hardlink"
    os.link(database, linked)
    _, report = _report_parent(tmp_path)
    assert probe.run(database.parent, report, formal_root=_formal(tmp_path)) == 1
    assert json.loads(report.read_text())["error_code"] == "fixture_invalid"


def test_migration_failure_is_fixed_code_and_keeps_schema21_bytes(tmp_path, monkeypatch):
    database, _ = _prepare(tmp_path / "attempt")
    before = probe._digest_file(database)
    _, report = _report_parent(tmp_path)

    def fail(path):
        with sqlite3.connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE wiki_should_rollback(value TEXT)")
            raise RuntimeError(SENTINEL)

    monkeypatch.setattr(probe, "initialize", fail)
    assert probe.run(database.parent, report, formal_root=_formal(tmp_path)) == 1
    assert json.loads(report.read_text()) == {
        "schema_version": 1, "ok": False, "error_code": "migration_failed"}
    assert SENTINEL not in report.read_text()
    assert probe._digest_file(database) == before
    assert sqlite3.connect(database).execute("PRAGMA user_version").fetchone()[0] == 21


@pytest.mark.parametrize("target", ["marker", "database"])
def test_linked_marker_or_database_is_rejected_before_migration(tmp_path, target):
    database, _ = _prepare(tmp_path / "attempt")
    path = database.parent / (probe.MARKER_NAME if target == "marker" else probe.DATABASE_NAME)
    moved = tmp_path / (target + "-real")
    path.rename(moved)
    path.symlink_to(moved)
    _, report = _report_parent(tmp_path)
    assert probe.run(database.parent, report, formal_root=_formal(tmp_path)) == 1
    assert json.loads(report.read_text())["error_code"] == "fixture_invalid"
