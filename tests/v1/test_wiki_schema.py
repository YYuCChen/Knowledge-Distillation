from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from knowledge_distiller.v1 import wiki_schema
from knowledge_distiller.v1.database import SCHEMA_VERSION, INGESTION_COLUMNS, connect, initialize


SCHEMA_21_FIXTURE = Path(__file__).with_name("fixtures") / "wiki-schema21.sql"
SCHEMA_22_FIXTURE = Path(__file__).with_name("fixtures") / "wiki-schema22.sql"


def _create_schema21(path: Path) -> None:
    """Restore the empty schema emitted by the released schema-21 initializer."""
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_21_FIXTURE.read_text(encoding="utf-8"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'wiki_%'"
        ).fetchone() is None


def _create_schema22(path: Path) -> None:
    """Restore the exact stage-2 wiki schema on top of released schema 21."""
    _create_schema21(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_22_FIXTURE.read_text(encoding="utf-8"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        assert {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wiki_%'"
            )
        } == {"wiki_tasks", "wiki_task_batches", "wiki_task_raw"}


def _schema21_binary_version_gate(path: Path) -> None:
    """Preserve the released initializer's relevant version gate as a contract."""
    with sqlite3.connect(path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        # Exact accepted-version set at public commit 85b710cc758fead41846a69f417a764343135009.
        if version not in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21):
            raise RuntimeError(f"unsupported database version: {version}")


def _legacy_snapshot(path: Path, *, normalize_v24=False) -> tuple[dict[str, str], dict[str, list[tuple]]]:
    with sqlite3.connect(path) as connection:
        objects = dict(
            connection.execute(
                """SELECT name,sql FROM sqlite_master
                   WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'wiki_%'
                   ORDER BY type,name"""
            )
        )
        tables = [
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table'
                     AND name NOT LIKE 'sqlite_%'
                     AND name NOT LIKE 'wiki_%'
                   ORDER BY name"""
            )
        ]
        contents = {
            name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in tables
        }
        if normalize_v24:
            # Only the explicitly added A1 objects/columns are projected away.
            # All pre-existing SQL and every original value still compare exact.
            additions = {'ingestion_events', 'ingestion_events_subject',
                         'collection_members_ingestion_contract_match'}
            prefixes = ('ingestion_events_', 'distill_items_ingestion_',
                        'collection_operations_ingestion_', 'source_media_ingestion_',
                        'submitted_sources_ingestion_')
            objects = {name: sql for name, sql in objects.items()
                       if name not in additions and not name.startswith(prefixes)}
            contents.pop('ingestion_events', None)
            for table in ('distill_items', 'collection_operations'):
                for definition in INGESTION_COLUMNS:
                    objects[table] = objects[table].replace(', ' + definition, '')
                added = {definition.split()[0] for definition in INGESTION_COLUMNS}
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info({table})')
                           if row[1] not in added]
                selected = ','.join('"' + name + '"' for name in columns)
                contents[table] = list(connection.execute(f'SELECT {selected} FROM {table} ORDER BY rowid'))
    return objects, contents


def _assert_healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_task(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    vault_key: str = "a" * 64,
    boundary: str = "b" * 64,
    raw_count: int = 0,
    batch_count: int = 0,
) -> None:
    connection.execute(
        """INSERT INTO wiki_tasks (
               task_id, vault_path, vault_key, request_kind, trigger_source,
               backend, model, effort, kit_version, kit_manifest_sha256,
               boundary_sha256, state, raw_count, batch_count,
               completed_batch_count, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            task_id,
            "/synthetic/vault",
            vault_key,
            "all",
            "local_web",
            "codex_cli",
            "gpt-5.6-sol",
            "xhigh",
            "3.0.0",
            "c" * 64,
            boundary,
            "queued",
            raw_count,
            batch_count,
            0,
            "2026-10-01T00:00:00+00:00",
            "2026-10-01T00:00:00+00:00",
        ),
    )


def _insert_frozen_task(connection: sqlite3.Connection) -> str:
    task_id = "1" * 32
    _insert_task(connection, task_id, raw_count=1, batch_count=1)
    connection.execute(
        "INSERT INTO wiki_task_batches VALUES (?,?,?,?,?)",
        (task_id, 1, "queued", 1, None),
    )
    connection.execute(
        """INSERT INTO wiki_task_raw (
               task_id, ordinal, batch_no, raw_id, identity,
               relative_path, byte_count, content_sha256
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (
            task_id,
            1,
            1,
            "R-20261001-0001",
            "本人",
            "raw/自述/2026/10/R-20261001-0001.md",
            18,
            "d" * 64,
        ),
    )
    return task_id


def test_real_schema21_upgrade_is_additive_and_preserves_legacy_database(tmp_path):
    database = tmp_path / "app.sqlite3"
    _create_schema21(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO settings VALUES ('sentinel','unchanged')")
    before = _legacy_snapshot(database)

    initialize(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 24
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wiki_%'"
            )
        } == {"wiki_tasks", "wiki_task_batches", "wiki_task_raw", "wiki_observations", "wiki_outcome_receipts"}
        _assert_healthy(connection)
    assert _legacy_snapshot(database, normalize_v24=True) == before


def test_schema22_to_23_preserves_frozen_task_rows_and_adds_observation(tmp_path):
    database = tmp_path / "app.sqlite3"
    _create_schema22(database)
    with sqlite3.connect(database) as connection:
        task_id = _insert_frozen_task(connection)
    with sqlite3.connect(database) as connection:
        before_task = connection.execute(
            "SELECT * FROM wiki_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        before_batch = connection.execute(
            "SELECT * FROM wiki_task_batches WHERE task_id=?", (task_id,)
        ).fetchone()
        before_raw = connection.execute(
            "SELECT * FROM wiki_task_raw WHERE task_id=?", (task_id,)
        ).fetchone()

    initialize(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        after_task = connection.execute(
            "SELECT * FROM wiki_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        assert after_task[:len(before_task)] == before_task
        assert after_task[len(before_task):] == ('legacy', '{}', None)
        assert connection.execute(
            "SELECT * FROM wiki_task_batches WHERE task_id=?", (task_id,)
        ).fetchone() == before_batch
        assert connection.execute(
            "SELECT * FROM wiki_task_raw WHERE task_id=?", (task_id,)
        ).fetchone() == before_raw
        assert connection.execute(
            "SELECT COUNT(*) FROM wiki_observations"
        ).fetchone()[0] == 0
        _assert_healthy(connection)


def test_schema23_rebuild_failure_restores_schema22_and_frozen_rows(
    tmp_path, monkeypatch
):
    database = tmp_path / "app.sqlite3"
    _create_schema22(database)
    with sqlite3.connect(database) as connection:
        task_id = _insert_frozen_task(connection)

    def fail_after_first_rename(connection):
        connection.execute("DROP INDEX wiki_one_unresolved_task_per_vault")
        connection.execute("ALTER TABLE wiki_task_raw RENAME TO wiki_task_raw_v22")
        connection.execute("CREATE TABLE broken migration syntax")

    monkeypatch.setattr(wiki_schema, "migrate_v23", fail_after_first_rename)
    with pytest.raises(sqlite3.OperationalError):
        initialize(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        assert connection.execute(
            "SELECT COUNT(*) FROM wiki_tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM wiki_task_raw WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='wiki_task_raw_v22'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='wiki_observations'"
        ).fetchone() is None
        _assert_healthy(connection)


def test_schema23_rebuild_rejects_orphaned_v22_child_and_preserves_source(tmp_path):
    database = tmp_path / "app.sqlite3"
    _create_schema22(database)
    orphan_id = "a" * 32
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO wiki_task_batches VALUES (?,?,?,?,?)",
            (orphan_id, 1, "queued", 1, None),
        )

    with pytest.raises(
        RuntimeError, match="database migration found broken wiki task references"
    ):
        initialize(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        assert connection.execute(
            "SELECT task_id FROM wiki_task_batches"
        ).fetchone()[0] == orphan_id
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='wiki_observations'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'wiki_%_v22'"
        ).fetchone() is None


def test_schema22_migration_sql_failure_rolls_back_the_whole_transaction(
    tmp_path, monkeypatch
):
    database = tmp_path / "app.sqlite3"
    _create_schema21(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO settings VALUES ('sentinel','unchanged')")
    before = _legacy_snapshot(database)

    def fail_midway(connection):
        connection.execute(wiki_schema.STATEMENTS[0])
        connection.execute(wiki_schema.STATEMENTS[1])
        connection.execute("CREATE TABLE broken migration syntax")

    monkeypatch.setattr(wiki_schema, "migrate", fail_midway)
    with pytest.raises(sqlite3.OperationalError):
        initialize(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'wiki_%'"
        ).fetchone() is None
        _assert_healthy(connection)
    assert _legacy_snapshot(database) == before


def test_schema21_binary_rejects_schema22_and_opens_restored_backup(tmp_path):
    database = tmp_path / "app.sqlite3"
    backup = tmp_path / "schema21-backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    _create_schema21(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO settings VALUES ('sentinel','backup-value')")
    expected = _legacy_snapshot(database)
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
        source.backup(target)

    initialize(database)
    with pytest.raises(RuntimeError, match="unsupported database version: 24"):
        _schema21_binary_version_gate(database)

    shutil.copy2(backup, restored)
    _schema21_binary_version_gate(restored)
    with sqlite3.connect(restored) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert connection.execute(
            "SELECT value FROM settings WHERE key='sentinel'"
        ).fetchone()[0] == "backup-value"
        _assert_healthy(connection)
    assert _legacy_snapshot(restored) == expected


def test_future_schema_is_rejected_without_downgrade(tmp_path):
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('keep')")
        connection.execute("PRAGMA user_version=25")

    with pytest.raises(RuntimeError, match="unsupported database version: 25"):
        initialize(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "keep"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'wiki_%'"
        ).fetchone() is None


def test_task_and_batch_triggers_reject_state_jumps(tmp_path):
    database = tmp_path / "app.sqlite3"
    initialize(database)
    with connect(database) as connection:
        task_id = _insert_frozen_task(connection)
        with pytest.raises(sqlite3.IntegrityError, match="invalid wiki task transition"):
            connection.execute(
                "UPDATE wiki_tasks SET state='running' WHERE task_id=?", (task_id,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="invalid wiki batch transition"):
            connection.execute(
                "UPDATE wiki_task_batches SET state='running' WHERE task_id=?", (task_id,)
            )
        assert connection.execute(
            "SELECT state FROM wiki_tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0] == "queued"
        assert connection.execute(
            "SELECT state FROM wiki_task_batches WHERE task_id=?", (task_id,)
        ).fetchone()[0] == "queued"
        connection.execute(
            "UPDATE wiki_tasks SET state='preparing' WHERE task_id=?", (task_id,)
        )
        connection.execute(
            "UPDATE wiki_task_batches SET state='preparing' WHERE task_id=?", (task_id,)
        )


def test_frozen_task_batch_and_raw_rows_reject_update_and_delete(tmp_path):
    database = tmp_path / "app.sqlite3"
    initialize(database)
    with connect(database) as connection:
        task_id = _insert_frozen_task(connection)
        statements = (
            (
                "UPDATE wiki_tasks SET vault_path='/synthetic/other' WHERE task_id=?",
                "wiki task boundary is immutable",
            ),
            ("DELETE FROM wiki_tasks WHERE task_id=?", "wiki task is durable"),
            (
                "UPDATE wiki_task_batches SET item_count=2 WHERE task_id=?",
                "wiki task batch identity is immutable",
            ),
            ("DELETE FROM wiki_task_batches WHERE task_id=?", "wiki task batch is durable"),
            (
                "UPDATE wiki_task_raw SET byte_count=19 WHERE task_id=?",
                "wiki task raw boundary is immutable",
            ),
            ("DELETE FROM wiki_task_raw WHERE task_id=?", "wiki task raw boundary is immutable"),
        )
        for statement, message in statements:
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(statement, (task_id,))
        assert connection.execute(
            "SELECT COUNT(*) FROM wiki_tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM wiki_task_batches WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM wiki_task_raw WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 1


def test_only_one_unresolved_task_can_own_a_vault(tmp_path):
    database = tmp_path / "app.sqlite3"
    initialize(database)
    with connect(database) as connection:
        first = "1" * 32
        second = "2" * 32
        third = "3" * 32
        _insert_task(connection, first, boundary="1" * 64)
        with pytest.raises(sqlite3.IntegrityError, match="wiki_tasks.vault_key"):
            _insert_task(connection, second, boundary="2" * 64)

        for state in ("preparing", "running", "validating", "publishing", "succeeded"):
            connection.execute(
                "UPDATE wiki_tasks SET state=? WHERE task_id=?", (state, first)
            )
        _insert_task(connection, second, boundary="2" * 64)
        with pytest.raises(sqlite3.IntegrityError, match="wiki_tasks.vault_key"):
            _insert_task(connection, third, boundary="3" * 64)
        assert connection.execute(
            "SELECT COUNT(*) FROM wiki_tasks WHERE vault_key=?", ("a" * 64,)
        ).fetchone()[0] == 2
