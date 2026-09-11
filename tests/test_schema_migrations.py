from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import knowledge_distiller.schema_migrations as schema_migrations
from knowledge_distiller.database import initialize_database
from knowledge_distiller.schema_migrations import (
    CREATE_KNOWLEDGE_RESULTS,
    CREATE_SCHEMA_MIGRATIONS,
    CURRENT_BASELINE_CORE_STATEMENTS,
    CURRENT_BASELINE_TOPIC_STATEMENTS,
    MIGRATIONS,
    Migration,
    MigrationManifestError,
    SchemaMigrationError,
    UnsupportedLegacySchema,
    migrate_database,
)


BASELINE_TABLES = (
    "materials",
    "source_facts",
    "knowledge_results",
    "tasks",
    "topics",
    "topic_memberships",
    "topic_index_state",
)

M2A_TABLES = {
    *BASELINE_TABLES,
    "schema_migrations",
    "organization_events",
    "organization_event_source_boundary",
    "organization_event_coverages",
}

M2A_INDEXES = {
    "one_task_per_identified_material",
    "knowledge_result_source_fact_identity",
    "one_running_organization_event",
    "organization_events_status_order",
    "organization_event_source_boundary_role_knowledge",
}

M2A_TRIGGERS = {
    "current_source_fact_must_belong_to_material",
    "current_knowledge_result_must_match_source_fact",
    "source_facts_cannot_be_updated",
    "source_facts_cannot_be_deleted",
    "knowledge_result_content_cannot_be_updated",
    "knowledge_results_cannot_be_deleted",
    "organization_coverage_matches_frozen_source",
    "organization_events_terminal_transition_only",
    "organization_events_cannot_be_deleted",
    "organization_event_source_boundary_cannot_be_updated",
    "organization_event_source_boundary_cannot_be_deleted",
    "organization_event_coverages_cannot_be_updated",
    "organization_event_coverages_cannot_be_deleted",
}

M2B_CORE_TABLES = {
    "relation_identities",
    "relation_versions",
    "relation_current",
    "relation_facts",
    "insight_identities",
    "insight_versions",
    "insight_identity_replacements",
    "insight_version_disqualifications",
    "user_insight_judgments",
    "accepted_insight_versions",
    "organization_event_accepted_boundary",
    "organization_event_relation_boundary",
    "relation_version_participants",
    "relation_version_used_relations",
    "insight_version_participants",
    "insight_version_used_relations",
}

M2B_TABLES = M2A_TABLES | M2B_CORE_TABLES
POST_M2A_TABLES = M2B_CORE_TABLES | {"accepted_insight_publications"}
M2A_MIGRATIONS = MIGRATIONS[:2]
M2B_MIGRATIONS = MIGRATIONS[:3]


@pytest.fixture
def m2a_binary(monkeypatch):
    """Run an exact 0002 binary without weakening the published 0003 binary."""
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2A_MIGRATIONS)
    return M2A_MIGRATIONS


def _execute_statements(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _create_legacy_database(
    database_path: Path,
    *,
    with_topics: bool,
    altered_knowledge_check: bool = False,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        statements = CURRENT_BASELINE_CORE_STATEMENTS
        if altered_knowledge_check:
            altered = CREATE_KNOWLEDGE_RESULTS.replace(
                "published_at IS NULL AND published_path IS NULL",
                "published_at IS NULL OR published_path IS NULL",
            )
            statements = tuple(
                altered if statement == CREATE_KNOWLEDGE_RESULTS else statement
                for statement in statements
            )
        _execute_statements(connection, statements)
        _insert_formal_rows(connection)
        if with_topics:
            _execute_statements(connection, CURRENT_BASELINE_TOPIC_STATEMENTS)
            connection.execute(
                """
                INSERT INTO topics (topic_id, normalized_name, name, scope)
                VALUES (7, 'legacy-topic', '历史主题', '历史范围')
                """
            )
            connection.execute(
                """
                INSERT INTO topic_memberships (
                    topic_id, knowledge_result_id, point_id, position
                ) VALUES (7, 1, 'p1', 0)
                """
            )
            connection.execute(
                """
                INSERT INTO topic_index_state (state_id, source_signature, indexed_at)
                VALUES (1, 'legacy-signature', '2026-08-20T00:00:00+00:00')
                """
            )


def _insert_formal_rows(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO materials (
            material_id, platform, platform_item_id, original_url,
            canonical_url, created_at
        ) VALUES (
            1, 'douyin', 'legacy-1', 'https://example.test/legacy-1',
            'https://example.test/canonical/legacy-1',
            '2026-08-20T00:00:00+00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO source_facts (
            source_fact_id, material_id, metadata_json, content_snapshot,
            uncertainty_json, replaces_source_fact_id, change_reason, created_at
        ) VALUES (
            1, 1, '{"title":"legacy"}', '历史原文', '[]', NULL, NULL,
            '2026-08-20T00:01:00+00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO knowledge_results (
            knowledge_result_id, source_fact_id, payload_json, created_at,
            invalidated_at, invalidation_reason, published_at, published_path
        ) VALUES (
            1, 1, '{"title":"legacy","points":[{"point_id":"p1"}]}',
            '2026-08-20T00:02:00+00:00', NULL, NULL,
            '2026-08-20T00:03:00+00:00', '知识蒸馏器/legacy-1.md'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO tasks (
            task_id, material_id, submitted_url, created_at, updated_at,
            last_failure_boundary, last_failure_reason,
            waiting_boundary, waiting_reason
        ) VALUES (
            1, 1, 'https://example.test/legacy-1',
            '2026-08-20T00:00:00+00:00',
            '2026-08-20T00:03:00+00:00', NULL, NULL, NULL, NULL
        )
        """
    )
    connection.execute(
        """
        UPDATE materials
        SET current_source_fact_id = 1, current_knowledge_result_id = 1
        WHERE material_id = 1
        """
    )


def _table_rows(
    database_path: Path,
    tables: tuple[str, ...],
) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database_path) as connection:
        return {
            table: tuple(
                tuple(row)
                for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')
            )
            for table in tables
        }


def _database_snapshot(database_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(database_path) as connection:
        schema = tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                ORDER BY type, name
                """
            )
        )
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                ORDER BY name
                """
            )
        )
        rows = tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ),
            )
            for table in tables
        )
        return schema, rows, int(connection.execute("PRAGMA user_version").fetchone()[0])


def _create_manifest_prefix(database_path: Path, count: int = 1) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(CREATE_SCHEMA_MIGRATIONS)
        for migration in MIGRATIONS[:count]:
            connection.execute(
                """
                INSERT INTO schema_migrations (
                    migration_id, position, checksum, applied_at
                ) VALUES (?, ?, ?, '2026-08-20T00:00:00+00:00')
                """,
                (
                    migration.migration_id,
                    migration.position,
                    migration.checksum,
                ),
            )


def _manifest(database_path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT migration_id, position, checksum
                FROM schema_migrations
                ORDER BY position
                """
            )
        )


def test_published_migration_checksums_are_frozen_golden_values():
    """Published migrations only append; changing a golden requires explicit review."""
    # Do not derive these values from migration SQL. Future migrations append to
    # MIGRATIONS; editing an existing golden must be a deliberate review event.
    assert tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in MIGRATIONS
    ) == (
        (
            "0001_current_baseline",
            1,
            "9ce368f207b8201dd0a28f7cf4eed10a6fbc095e38cc9c4851a306b9a4a86269",
        ),
        (
            "0002_organization_event_and_coverage",
            2,
            "372ea6041646712c14faa8b21b126a10953d5ec2fc98bdc74e22a28fa41f5868",
        ),
        (
            "0003_relation_insight_and_acceptance_core",
            3,
            "018ffe0fe228f454ef688d8cc81932a389e808f38b63cb3869185e552dc7c9c9",
        ),
        (
            "0004_accepted_insight_publication",
            4,
            "0280f2ec613f5032d0c2fa7e2a037c7e9ff7440a082e3f18e02f46c1c2b81448",
        ),
    )


def test_g2_binary_reopens_0003_without_manifest_or_checksum_changes(
    tmp_path,
    monkeypatch,
):
    """G2 adds behavior on the frozen 0003 schema and no migration of its own."""
    database_path = tmp_path / "g2-reopen.sqlite3"
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2B_MIGRATIONS)
    initialize_database(database_path)
    before = _manifest(database_path)

    # Importing and using the G2 binary must not append or rewrite schema rows.
    from knowledge_distiller.accepted_insight_library import search_accepted_insights

    assert search_accepted_insights(database_path, "no matches").current == ()
    initialize_database(database_path)

    assert _manifest(database_path) == before == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in M2B_MIGRATIONS
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2a_empty_database_builds_exact_prefix_without_later_capability_facts(
    tmp_path,
    m2a_binary,
):
    """M2a: empty database reaches exactly 0001 -> 0002, never 0003/0004."""
    database_path = tmp_path / "empty" / "knowledge.sqlite3"

    migrate_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        foreign_key_check = tuple(connection.execute("PRAGMA foreign_key_check"))
        coverage_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM organization_event_coverages"
            ).fetchone()[0]
        )
        event_count = int(
            connection.execute("SELECT COUNT(*) FROM organization_events").fetchone()[0]
        )

    assert tables == M2A_TABLES
    assert indexes == M2A_INDEXES
    assert triggers == M2A_TRIGGERS
    assert foreign_key_check == ()
    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in m2a_binary
    )
    assert tables.isdisjoint(POST_M2A_TABLES)
    assert coverage_count == 0
    assert event_count == 0


@pytest.mark.parametrize("with_topics", [False, True], ids=["four-table", "seven-table"])
def test_m2a_adopts_only_exact_supported_legacy_and_preserves_rows(
    tmp_path,
    with_topics,
    m2a_binary,
):
    """M2a: supported 4/7-table legacy adoption preserves every existing field."""
    database_path = tmp_path / f"legacy-{int(with_topics)}.sqlite3"
    _create_legacy_database(database_path, with_topics=with_topics)
    existing_tables = BASELINE_TABLES if with_topics else BASELINE_TABLES[:4]
    before = _table_rows(database_path, existing_tables)

    migrate_database(database_path)

    assert _table_rows(database_path, existing_tables) == before
    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in m2a_binary
    )
    with sqlite3.connect(database_path) as connection:
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()
        assert connection.execute("SELECT COUNT(*) FROM organization_events").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM organization_event_source_boundary"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM organization_event_coverages"
            ).fetchone()[0]
            == 0
        )
        if not with_topics:
            assert connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0


def test_m2a_upgrades_an_existing_0001_manifest_prefix_without_rewriting_rows(
    tmp_path,
    m2a_binary,
):
    """M2a: a staged 0001 snapshot advances to 0002 without baseline rewrites."""
    database_path = tmp_path / "staged-0001.sqlite3"
    _create_legacy_database(database_path, with_topics=True)
    _create_manifest_prefix(database_path)
    before = _table_rows(database_path, BASELINE_TABLES)

    migrate_database(database_path)

    assert _table_rows(database_path, BASELINE_TABLES) == before
    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in m2a_binary
    )
    with sqlite3.connect(database_path) as connection:
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()
        assert connection.execute("SELECT COUNT(*) FROM organization_events").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["missing-trigger", "changed-check"])
def test_m2a_rejects_near_but_incompatible_legacy_without_changes(
    tmp_path,
    mutation,
    m2a_binary,
):
    """M2a: table names alone cannot authorize legacy adoption."""
    database_path = tmp_path / f"incompatible-{mutation}.sqlite3"
    _create_legacy_database(
        database_path,
        with_topics=False,
        altered_knowledge_check=mutation == "changed-check",
    )
    if mutation == "missing-trigger":
        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP TRIGGER source_facts_cannot_be_updated")
    before = _database_snapshot(database_path)

    with pytest.raises(UnsupportedLegacySchema, match="unsupported legacy schema"):
        migrate_database(database_path)

    assert _database_snapshot(database_path) == before


@pytest.mark.parametrize(
    "manifest_case",
    ["unknown", "gap", "position", "checksum", "duplicate-position"],
)
def test_m2a_manifest_corruption_fails_before_new_ddl(
    tmp_path,
    manifest_case,
    m2a_binary,
):
    """M2a: unknown/gap/position/checksum/duplicate manifest facts fail closed."""
    database_path = tmp_path / f"manifest-{manifest_case}.sqlite3"
    _create_legacy_database(database_path, with_topics=True)
    with sqlite3.connect(database_path) as connection:
        if manifest_case == "duplicate-position":
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    migration_id TEXT NOT NULL PRIMARY KEY,
                    position INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, 1, ?, 'now')",
                (MIGRATIONS[0].migration_id, MIGRATIONS[0].checksum),
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES ('duplicate', 1, ?, 'now')",
                ("d" * 64,),
            )
        else:
            connection.execute(CREATE_SCHEMA_MIGRATIONS)
            if manifest_case == "unknown":
                values = ("9999_unknown", 1, "u" * 64)
            elif manifest_case == "gap":
                values = (
                    MIGRATIONS[1].migration_id,
                    2,
                    MIGRATIONS[1].checksum,
                )
            elif manifest_case == "position":
                values = (
                    MIGRATIONS[0].migration_id,
                    2,
                    MIGRATIONS[0].checksum,
                )
            else:
                values = (MIGRATIONS[0].migration_id, 1, "x" * 64)
            connection.execute(
                """
                INSERT INTO schema_migrations (
                    migration_id, position, checksum, applied_at
                ) VALUES (?, ?, ?, 'now')
                """,
                values,
            )
    before = _database_snapshot(database_path)

    with pytest.raises(MigrationManifestError):
        migrate_database(database_path)

    assert _database_snapshot(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'organization_events'
                """
            ).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "failure_point",
    ["middle-ddl", "foreign-key-check", "manifest-insert"],
)
def test_m2a_migration_failure_rolls_back_schema_and_manifest(
    tmp_path,
    monkeypatch,
    failure_point,
):
    """M2a: DDL, FK-check, and manifest failures leave no partial migration."""
    database_path = tmp_path / f"rollback-{failure_point}.sqlite3"
    _create_legacy_database(database_path, with_topics=True)
    _create_manifest_prefix(database_path)
    before = _database_snapshot(database_path)
    original = MIGRATIONS[1]

    if failure_point == "middle-ddl":
        statements = (
            original.statements[0],
            "CREATE TABLE migration_rollback_probe (probe_id INTEGER PRIMARY KEY)",
            "CREATE TABLE broken_migration (",
        )
        expected_error = sqlite3.DatabaseError
    elif failure_point == "foreign-key-check":
        statements = original.statements + (
            "CREATE TABLE migration_fk_parent (parent_id INTEGER PRIMARY KEY)",
            """
            CREATE TABLE migration_fk_child (
                parent_id INTEGER,
                FOREIGN KEY (parent_id) REFERENCES migration_fk_parent(parent_id)
                  DEFERRABLE INITIALLY DEFERRED
            )
            """,
            "INSERT INTO migration_fk_child (parent_id) VALUES (999)",
        )
        expected_error = SchemaMigrationError
    else:
        statements = original.statements + (
            """
            CREATE TRIGGER fail_second_manifest_insert
            BEFORE INSERT ON schema_migrations
            WHEN NEW.position = 2
            BEGIN
                SELECT RAISE(ABORT, 'injected manifest failure');
            END
            """,
        )
        expected_error = sqlite3.DatabaseError

    replacement = Migration.create(
        original.migration_id,
        original.position,
        statements,
    )
    monkeypatch.setattr(
        schema_migrations,
        "MIGRATIONS",
        (MIGRATIONS[0], replacement),
    )

    with pytest.raises(expected_error):
        migrate_database(database_path)

    assert _database_snapshot(database_path) == before


def test_m2a_two_threads_serialize_and_apply_manifest_once(
    tmp_path,
    monkeypatch,
    m2a_binary,
):
    """M2a: two independent connections compete for the write lock safely."""
    database_path = tmp_path / "concurrent.sqlite3"
    barrier = threading.Barrier(2)
    original_begin = schema_migrations._begin_immediate

    def synchronized_begin(connection):
        barrier.wait(timeout=5)
        original_begin(connection)

    monkeypatch.setattr(schema_migrations, "_begin_immediate", synchronized_begin)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(migrate_database, database_path) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in m2a_binary
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def _insert_event(connection: sqlite3.Connection, event_id: int) -> None:
    connection.execute(
        """
        INSERT INTO organization_events (
            event_id, status, started_at,
            topic_before_json, topic_before_signature,
            topic_guard_signature, boundary_signature
        ) VALUES (?, 'running', ?, '{}', ?, ?, ?)
        """,
        (
            event_id,
            f"2026-08-20T00:{event_id:02d}:00+00:00",
            "b" * 64,
            "g" * 64,
            "d" * 64,
        ),
    )


def _succeed_event(connection: sqlite3.Connection, event_id: int) -> None:
    connection.execute(
        """
        UPDATE organization_events
        SET status = 'succeeded', completed_at = ?, success_payload_json = '{}'
        WHERE event_id = ?
        """,
        (f"2026-08-20T01:{event_id:02d}:00+00:00", event_id),
    )


def _insert_two_owned_knowledge_results(connection: sqlite3.Connection) -> None:
    for identity in (1, 2):
        connection.execute(
            """
            INSERT INTO materials (
                material_id, platform, platform_item_id, original_url, created_at
            ) VALUES (?, 'douyin', ?, ?, ?)
            """,
            (
                identity,
                f"item-{identity}",
                f"https://example.test/{identity}",
                f"2026-08-20T00:0{identity}:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO source_facts (
                source_fact_id, material_id, metadata_json,
                content_snapshot, uncertainty_json, created_at
            ) VALUES (?, ?, '{}', ?, '[]', ?)
            """,
            (
                identity,
                identity,
                f"来源 {identity}",
                f"2026-08-20T00:1{identity}:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_results (
                knowledge_result_id, source_fact_id, payload_json, created_at
            ) VALUES (?, ?, '{}', ?)
            """,
            (identity, identity, f"2026-08-20T00:2{identity}:00+00:00"),
        )


def test_m2a_ownership_coverage_and_immutability_dml_fences(tmp_path, m2a_binary):
    """M2a: static DML fences protect exact ownership and permanent history."""
    database_path = tmp_path / "dml.sqlite3"
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_two_owned_knowledge_results(connection)

        _insert_event(connection, 1)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO organization_event_source_boundary
                VALUES (1, 1, 2, 'frozen_new', 0, ?)
                """,
                ("q" * 64,),
            )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM organization_event_source_boundary
                WHERE event_id = 1
                """
            ).fetchone()[0]
            == 0
        )
        connection.execute(
            """
            INSERT INTO organization_event_source_boundary
            VALUES (1, 1, 1, 'frozen_new', 0, ?)
            """,
            ("q" * 64,),
        )
        _succeed_event(connection, 1)
        connection.execute(
            """
            INSERT INTO organization_event_coverages
            VALUES (1, 1, 1, '2026-08-20T01:01:01+00:00')
            """
        )

        _insert_event(connection, 2)
        connection.execute(
            """
            INSERT INTO organization_event_source_boundary
            VALUES (2, 2, 2, 'eligible_history', 0, ?)
            """,
            ("q" * 64,),
        )
        _succeed_event(connection, 2)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO organization_event_coverages
                VALUES (2, 2, 2, '2026-08-20T01:02:01+00:00')
                """
            )

        _insert_event(connection, 3)
        connection.execute(
            """
            INSERT INTO organization_event_source_boundary
            VALUES (3, 2, 2, 'frozen_new', 0, ?)
            """,
            ("q" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO organization_event_coverages
                VALUES (3, 2, 2, '2026-08-20T01:03:01+00:00')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, 30)
        connection.execute(
            """
            UPDATE organization_events
            SET status = 'failed', completed_at = ?, failure_code = 'persistence_failed'
            WHERE event_id = 3
            """,
            ("2026-08-20T01:03:00+00:00",),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO organization_event_coverages
                VALUES (3, 2, 2, '2026-08-20T01:03:02+00:00')
                """
            )

        _insert_event(connection, 4)
        connection.execute(
            """
            INSERT INTO organization_event_source_boundary
            VALUES (4, 2, 2, 'frozen_new', 0, ?)
            """,
            ("q" * 64,),
        )
        _succeed_event(connection, 4)
        coverage_count_before_cross_owner = connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0]
        with pytest.raises(
            sqlite3.IntegrityError,
            match="coverage does not match succeeded frozen source",
        ):
            connection.execute(
                """
                INSERT INTO organization_event_coverages
                VALUES (4, 2, 1, '2026-08-20T01:04:01+00:00')
                """
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == coverage_count_before_cross_owner
        connection.execute(
            """
            INSERT INTO organization_event_coverages
            VALUES (4, 2, 2, '2026-08-20T01:04:02+00:00')
            """
        )

        _insert_event(connection, 5)
        connection.execute(
            """
            INSERT INTO organization_event_source_boundary
            VALUES (5, 2, 2, 'frozen_new', 0, ?)
            """,
            ("q" * 64,),
        )
        _succeed_event(connection, 5)
        coverage_count_before_duplicate = connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0]
        with pytest.raises(
            sqlite3.IntegrityError,
            match=(
                "UNIQUE constraint failed: "
                "organization_event_coverages.knowledge_result_id"
            ),
        ):
            connection.execute(
                """
                INSERT INTO organization_event_coverages
                VALUES (5, 2, 2, '2026-08-20T01:05:01+00:00')
                """
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == coverage_count_before_duplicate

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE organization_event_source_boundary
                SET position = 1 WHERE event_id = 1 AND knowledge_result_id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                DELETE FROM organization_event_source_boundary
                WHERE event_id = 1 AND knowledge_result_id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE organization_event_coverages
                SET covered_at = 'later'
                WHERE event_id = 1 AND knowledge_result_id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                DELETE FROM organization_event_coverages
                WHERE event_id = 1 AND knowledge_result_id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE organization_events
                SET success_payload_json = '{"changed":true}'
                WHERE event_id = 1
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM organization_events WHERE event_id = 1")

        assert tuple(
            connection.execute(
                """
                SELECT event_id, knowledge_result_id, source_fact_id
                FROM organization_event_coverages
                ORDER BY event_id, knowledge_result_id
                """
            )
        ) == ((1, 1, 1), (4, 2, 2))
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM organization_event_source_boundary
                WHERE knowledge_result_id = 1 AND source_fact_id = 2
                """
            ).fetchone()[0]
            == 0
        )
        assert tuple(
            connection.execute(
                """
                SELECT status, success_payload_json
                FROM organization_events
                WHERE event_id = 1
                """
            ).fetchone()
        ) == ("succeeded", "{}")
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_events WHERE event_id = 30"
        ).fetchone()[0] == 0
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def _insert_source_boundary(
    connection: sqlite3.Connection,
    event_id: int,
    knowledge_result_id: int,
    role: str,
    position: int,
) -> None:
    connection.execute(
        """
        INSERT INTO organization_event_source_boundary (
            event_id, knowledge_result_id, source_fact_id,
            boundary_role, position, qualification_signature
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            knowledge_result_id,
            knowledge_result_id,
            role,
            position,
            "q" * 64,
        ),
    )


def _insert_relation_identity(
    connection: sqlite3.Connection,
    relation_id: int,
    event_id: int,
) -> None:
    connection.execute(
        "INSERT INTO relation_identities VALUES (?, ?, ?)",
        (relation_id, event_id, f"2026-08-20T02:{relation_id:02d}:00+00:00"),
    )


def _insert_relation_version(
    connection: sqlite3.Connection,
    relation_version_id: int,
    relation_id: int,
    version_no: int,
    previous_version_id: int | None,
    event_id: int,
) -> None:
    connection.execute(
        """
        INSERT INTO relation_versions (
            relation_version_id, relation_id, version_no, previous_version_id,
            produced_event_id, payload_json, semantic_signature,
            dependency_signature, created_at
        ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)
        """,
        (
            relation_version_id,
            relation_id,
            version_no,
            previous_version_id,
            event_id,
            f"r{relation_version_id}".ljust(64, "r")[:64],
            f"d{relation_version_id}".ljust(64, "d")[:64],
            f"2026-08-20T03:{relation_version_id % 60:02d}:00+00:00",
        ),
    )


def _insert_insight_identity(
    connection: sqlite3.Connection,
    insight_id: int,
    event_id: int,
) -> None:
    connection.execute(
        "INSERT INTO insight_identities VALUES (?, ?, ?)",
        (insight_id, event_id, f"2026-08-20T04:{insight_id:02d}:00+00:00"),
    )


def _insert_insight_version(
    connection: sqlite3.Connection,
    insight_version_id: int,
    insight_id: int,
    version_no: int,
    previous_version_id: int | None,
    event_id: int,
) -> None:
    connection.execute(
        """
        INSERT INTO insight_versions (
            insight_version_id, insight_id, version_no, previous_version_id,
            produced_event_id, payload_json, semantic_signature,
            dependency_signature, created_at
        ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)
        """,
        (
            insight_version_id,
            insight_id,
            version_no,
            previous_version_id,
            event_id,
            f"i{insight_version_id}".ljust(64, "i")[:64],
            f"d{insight_version_id}".ljust(64, "d")[:64],
            f"2026-08-20T05:{insight_version_id % 60:02d}:00+00:00",
        ),
    )


def _assert_insert_rejected_without_growth(
    connection: sqlite3.Connection,
    table: str,
    sql: str,
    parameters: tuple[object, ...] = (),
    match: str | None = None,
) -> None:
    before = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    with pytest.raises(sqlite3.IntegrityError, match=match):
        connection.execute(sql, parameters)
    after = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    assert after == before


def _insert_complete_m2b_graph(connection: sqlite3.Connection) -> None:
    """Insert every 0003 child with non-null parents in a disposable database."""
    _insert_two_owned_knowledge_results(connection)

    _insert_event(connection, 1)
    _insert_source_boundary(connection, 1, 1, "frozen_new", 0)
    _insert_source_boundary(connection, 1, 2, "eligible_history", 0)
    _insert_relation_identity(connection, 1, 1)
    _insert_relation_version(connection, 11, 1, 1, None, 1)
    connection.execute(
        "INSERT INTO relation_current VALUES (1, 11, '2026-08-20T06:01:00+00:00')"
    )
    _insert_insight_identity(connection, 1, 1)
    _insert_insight_version(connection, 101, 1, 1, None, 1)
    connection.execute(
        """
        INSERT INTO insight_version_participants VALUES
        (101, 'source-1', 'source_knowledge', 1, 'p1', NULL, 0, 'first source'),
        (101, 'source-2', 'source_knowledge', 2, 'p2', NULL, 1, 'second source')
        """
    )
    connection.execute(
        "INSERT INTO insight_version_used_relations VALUES (101, 11, 0, 'same event')"
    )
    connection.execute(
        """
        INSERT INTO user_insight_judgments
        VALUES (1, 1, 101, 'interesting', NULL, '2026-08-20T06:02:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO accepted_insight_versions (
            insight_version_id, insight_id, judgment_id, judgment_decision,
            initial_role, current_role, accepted_at
        ) VALUES (101, 1, 1, 'interesting', 'current', 'current',
                  '2026-08-20T06:03:00+00:00')
        """
    )
    _succeed_event(connection, 1)

    _insert_event(connection, 2)
    _insert_source_boundary(connection, 2, 1, "eligible_history", 0)
    _insert_source_boundary(connection, 2, 2, "eligible_history", 1)
    connection.execute(
        "INSERT INTO organization_event_accepted_boundary VALUES (2, 101, 0, ?)",
        ("a" * 64,),
    )
    connection.execute(
        """
        INSERT INTO organization_event_relation_boundary
        VALUES (2, 11, 'current_input', 0, ?)
        """,
        ("r" * 64,),
    )
    _insert_relation_identity(connection, 2, 2)
    _insert_relation_version(connection, 21, 2, 1, None, 2)
    connection.execute(
        """
        INSERT INTO relation_version_participants VALUES
        (21, 'source-1', 'source_knowledge', 1, 'p1', NULL, 0, 'source role'),
        (21, 'accepted-101', 'accepted_insight', NULL, NULL, 101, 1, 'accepted role')
        """
    )
    connection.execute(
        "INSERT INTO relation_version_used_relations VALUES (21, 11, 0, 'boundary')"
    )
    connection.execute(
        "INSERT INTO relation_current VALUES (2, 21, '2026-08-20T06:04:00+00:00')"
    )
    _insert_relation_identity(connection, 3, 2)
    _insert_relation_version(connection, 31, 3, 1, None, 2)
    _insert_relation_version(connection, 12, 1, 2, 11, 2)
    connection.execute(
        """
        UPDATE relation_current
        SET relation_version_id = 12, activated_at = '2026-08-20T06:05:00+00:00'
        WHERE relation_id = 1
        """
    )
    connection.execute(
        """
        INSERT INTO relation_facts VALUES
        (1, 2, 1, 11, 'evolved', 12, NULL, 'same identity evolution', '2026-08-20T06:06:00+00:00'),
        (2, 2, 2, 21, 'wrong', NULL, NULL, 'formally wrong', '2026-08-20T06:07:00+00:00'),
        (3, 2, 2, 21, 'replaced', NULL, 3, 'replacement identity', '2026-08-20T06:08:00+00:00'),
        (4, 2, 1, 11, 'basis_invalid', NULL, NULL, 'basis lost', '2026-08-20T06:09:00+00:00'),
        (5, 2, 3, 31, 'attention_activated', NULL, NULL, 'attention on', '2026-08-20T06:10:00+00:00'),
        (6, 2, 3, 31, 'attention_retired', NULL, NULL, 'attention off', '2026-08-20T06:11:00+00:00'),
        (7, 2, 3, 31, 'attention_activated', NULL, NULL, 'attention on again', '2026-08-20T06:12:00+00:00')
        """
    )
    _insert_insight_identity(connection, 2, 2)
    _insert_insight_version(connection, 201, 2, 1, None, 2)
    connection.execute(
        """
        INSERT INTO insight_version_participants VALUES
        (201, 'source-2', 'source_knowledge', 2, 'p2', NULL, 0, 'source role'),
        (201, 'accepted-101', 'accepted_insight', NULL, NULL, 101, 1, 'accepted role')
        """
    )
    connection.execute(
        "INSERT INTO insight_version_used_relations VALUES (201, 21, 0, 'planned stable')"
    )
    connection.execute(
        """
        INSERT INTO user_insight_judgments
        VALUES (2, 2, 201, 'interesting', 'keep', '2026-08-20T06:12:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO accepted_insight_versions (
            insight_version_id, insight_id, judgment_id, judgment_decision,
            initial_role, current_role, accepted_at
        ) VALUES (201, 2, 2, 'interesting', 'current', 'current',
                  '2026-08-20T06:13:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO insight_identity_replacements
        VALUES (1, 2, 2, 'new core identity', '2026-08-20T06:14:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO insight_version_disqualifications
        VALUES (1, 101, 'basis_invalid', 2, 'basis lost', '2026-08-20T06:15:00+00:00')
        """
    )
    connection.execute(
        """
        UPDATE accepted_insight_versions
        SET current_role = 'historical', historical_at = '2026-08-20T06:16:00+00:00',
            historical_reason = 'basis_invalid', caused_by_event_id = 2,
            disqualification_reason = 'basis_invalid'
        WHERE insight_version_id = 101
        """
    )
    _succeed_event(connection, 2)

    _insert_event(connection, 3)
    _insert_source_boundary(connection, 3, 1, "eligible_history", 0)
    connection.execute(
        "INSERT INTO organization_event_accepted_boundary VALUES (3, 201, 0, ?)",
        ("a" * 64,),
    )
    connection.execute(
        """
        INSERT INTO organization_event_relation_boundary VALUES
        (3, 12, 'current_input', 0, ?),
        (3, 31, 'reconsideration_hint', 1, ?)
        """,
        ("r" * 64, "h" * 64),
    )
    _succeed_event(connection, 3)


def test_m2b_empty_database_reaches_exact_0003_without_later_capability_facts(
    tmp_path,
    monkeypatch,
):
    """M2b: latest binary is exact 0003 and all new parent/child tables start empty."""
    database_path = tmp_path / "m2b-empty.sqlite3"

    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2B_MIGRATIONS)
    initialize_database(database_path)
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        assert tables == M2B_TABLES
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in M2B_CORE_TABLES
        )
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'accepted_insight_publications'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_events WHERE status = 'succeeded'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_event_coverages"
        ).fetchone()[0] == 0

    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in M2B_MIGRATIONS
    )


def test_m2b_staged_0002_to_0003_preserves_all_existing_fields(tmp_path, monkeypatch):
    """M2b: exact 0002 source/Topic/event/boundary/coverage rows survive field-for-field."""
    database_path = tmp_path / "m2b-staged.sqlite3"
    _create_legacy_database(database_path, with_topics=True)
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2A_MIGRATIONS)
    migrate_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_event(connection, 1)
        _insert_source_boundary(connection, 1, 1, "frozen_new", 0)
        _succeed_event(connection, 1)
        connection.execute(
            """
            INSERT INTO organization_event_coverages
            VALUES (1, 1, 1, '2026-08-20T07:00:00+00:00')
            """
        )
    before = _table_rows(database_path, tuple(sorted(M2A_TABLES - {"schema_migrations"})))

    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2B_MIGRATIONS)
    migrate_database(database_path)

    assert _table_rows(
        database_path,
        tuple(sorted(M2A_TABLES - {"schema_migrations"})),
    ) == before
    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in M2B_MIGRATIONS
    )
    with sqlite3.connect(database_path) as connection:
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in M2B_CORE_TABLES
        )
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2b_complete_parent_graph_accepts_every_child_and_relation_fact_axis(tmp_path):
    """M2b: every 0003 child has a real non-null parent path and legal DML evidence."""
    database_path = tmp_path / "m2b-positive.sqlite3"
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)

        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
            for table in M2B_CORE_TABLES
        )
        assert tuple(
            connection.execute(
                """
                SELECT fact_kind FROM relation_facts
                WHERE relation_id = 2
                ORDER BY fact_kind
                """
            )
        ) == (("replaced",), ("wrong",))
        assert connection.execute(
            """
            SELECT COUNT(*) FROM relation_facts
            WHERE relation_version_id = 11 AND fact_kind = 'basis_invalid'
            """
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM relation_facts
            WHERE relation_id = 3 AND fact_kind LIKE 'attention_%'
            """
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = 101"
        ).fetchone()[0] == "historical"
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = 201"
        ).fetchone()[0] == "current"
        assert connection.execute("SELECT COUNT(*) FROM organization_event_coverages").fetchone()[0] == 0
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2b_predecessors_are_same_identity_consecutive_and_one_per_event(tmp_path):
    """M2b: relation/insight lineage rejects cross-identity, gaps, and same-event v2."""
    database_path = tmp_path / "m2b-lineage.sqlite3"
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)

        for values in (
            (32, 3, 2, 12, 3),  # relation 3 cannot inherit relation 1 v2
            (14, 1, 4, 12, 3),  # relation 1 cannot skip v3
            (13, 1, 3, 12, 2),  # event 2 already produced relation 1 v2
        ):
            _assert_insert_rejected_without_growth(
                connection,
                "relation_versions",
                """
                INSERT INTO relation_versions (
                    relation_version_id, relation_id, version_no,
                    previous_version_id, produced_event_id, payload_json,
                    semantic_signature, dependency_signature, created_at
                ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, 'now')
                """,
                (*values, "s" * 64, "d" * 64),
            )

        for values in (
            (202, 2, 2, 101, 3),  # insight 2 cannot inherit insight 1 v1
            (103, 1, 3, 101, 3),  # insight 1 cannot skip v2
            (202, 2, 2, 201, 2),  # event 2 already produced insight 2 v1
        ):
            _assert_insert_rejected_without_growth(
                connection,
                "insight_versions",
                """
                INSERT INTO insight_versions (
                    insight_version_id, insight_id, version_no,
                    previous_version_id, produced_event_id, payload_json,
                    semantic_signature, dependency_signature, created_at
                ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, 'now')
                """,
                (*values, "s" * 64, "d" * 64),
            )

        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2b_relation_current_evolution_and_replacement_keep_identity_axes(tmp_path):
    """M2b: current/evolved stay in one identity; replacement must change identity."""
    database_path = tmp_path / "m2b-relation-identity.sqlite3"
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)

        _assert_insert_rejected_without_growth(
            connection,
            "relation_current",
            "INSERT INTO relation_current VALUES (3, 12, 'now')",
        )
        _assert_insert_rejected_without_growth(
            connection,
            "relation_facts",
            """
            INSERT INTO relation_facts VALUES
            (20, 3, 3, 31, 'evolved', 12, NULL, 'cross identity', 'now')
            """,
        )
        _assert_insert_rejected_without_growth(
            connection,
            "relation_facts",
            """
            INSERT INTO relation_facts VALUES
            (21, 3, 3, 31, 'replaced', NULL, 3, 'self replacement', 'now')
            """,
        )

        assert connection.execute(
            """
            SELECT successor_relation_version_id FROM relation_facts
            WHERE relation_id = 1 AND fact_kind = 'evolved'
            """
        ).fetchone()[0] == 12
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2b_event_boundaries_participants_and_used_edges_reject_cross_event_inputs(
    tmp_path,
):
    """M2b: exact current roles and owner-event source/accepted/relation inputs are closed."""
    database_path = tmp_path / "m2b-boundaries.sqlite3"
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)
        _insert_event(connection, 4)
        _insert_source_boundary(connection, 4, 1, "eligible_history", 0)

        _assert_insert_rejected_without_growth(
            connection,
            "organization_event_accepted_boundary",
            "INSERT INTO organization_event_accepted_boundary VALUES (4, 101, 0, ?)",
            ("a" * 64,),
            "accepted boundary requires current exact version",
        )
        _assert_insert_rejected_without_growth(
            connection,
            "organization_event_relation_boundary",
            """
            INSERT INTO organization_event_relation_boundary
            VALUES (4, 11, 'current_input', 0, ?)
            """,
            ("r" * 64,),
            "relation current boundary requires exact current version",
        )
        _assert_insert_rejected_without_growth(
            connection,
            "organization_event_relation_boundary",
            """
            INSERT INTO organization_event_relation_boundary
            VALUES (4, 12, 'reconsideration_hint', 0, ?)
            """,
            ("r" * 64,),
            "relation reconsideration hint cannot already be current",
        )

        connection.execute(
            "INSERT INTO organization_event_accepted_boundary VALUES (4, 201, 0, ?)",
            ("a" * 64,),
        )
        connection.execute(
            """
            INSERT INTO organization_event_relation_boundary
            VALUES (4, 12, 'current_input', 0, ?)
            """,
            ("r" * 64,),
        )
        _insert_relation_identity(connection, 4, 4)
        _insert_relation_version(connection, 41, 4, 1, None, 4)
        _insert_insight_identity(connection, 3, 4)
        _insert_insight_version(connection, 301, 3, 1, None, 4)

        participant_cases = (
            (
                "relation_version_participants",
                """
                INSERT INTO relation_version_participants VALUES
                (41, 'outside-source', 'source_knowledge', 2, 'p2', NULL, 0, 'outside')
                """,
            ),
            (
                "relation_version_participants",
                """
                INSERT INTO relation_version_participants VALUES
                (41, 'outside-accepted', 'accepted_insight', NULL, NULL, 101, 0, 'outside')
                """,
            ),
            (
                "insight_version_participants",
                """
                INSERT INTO insight_version_participants VALUES
                (301, 'outside-source', 'source_knowledge', 2, 'p2', NULL, 0, 'outside')
                """,
            ),
            (
                "insight_version_participants",
                """
                INSERT INTO insight_version_participants VALUES
                (301, 'outside-accepted', 'accepted_insight', NULL, NULL, 101, 0, 'outside')
                """,
            ),
        )
        for table, sql in participant_cases:
            _assert_insert_rejected_without_growth(connection, table, sql)

        _assert_insert_rejected_without_growth(
            connection,
            "relation_version_used_relations",
            """
            INSERT INTO relation_version_used_relations
            VALUES (41, 21, 0, 'outside boundary')
            """,
        )
        _assert_insert_rejected_without_growth(
            connection,
            "insight_version_used_relations",
            """
            INSERT INTO insight_version_used_relations
            VALUES (301, 21, 0, 'outside and not same event')
            """,
        )
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2b_accepted_exact_judgment_historical_causes_and_one_way_roles(tmp_path):
    """M2b: accepted ownership is exact; legal history is causal and never reversible."""
    database_path = tmp_path / "m2b-accepted.sqlite3"
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)

        _insert_event(connection, 4)
        _insert_insight_version(connection, 202, 2, 2, 201, 4)
        _insert_insight_identity(connection, 3, 4)
        _insert_insight_version(connection, 301, 3, 1, None, 4)
        _insert_insight_identity(connection, 4, 4)
        _insert_insight_version(connection, 401, 4, 1, None, 4)
        connection.execute(
            """
            INSERT INTO user_insight_judgments VALUES
            (10, 2, 202, 'interesting', NULL, '2026-08-20T08:00:00+00:00'),
            (11, 3, 301, 'rethink', NULL, '2026-08-20T08:01:00+00:00'),
            (12, 4, 401, 'interesting', NULL, '2026-08-20T08:02:00+00:00')
            """
        )
        _assert_insert_rejected_without_growth(
            connection,
            "insight_identity_replacements",
            """
            INSERT INTO insight_identity_replacements
            VALUES (3, 3, 4, 'self replacement', 'now')
            """,
        )
        connection.execute(
            """
            UPDATE accepted_insight_versions
            SET current_role = 'historical', historical_at = '2026-08-20T08:03:00+00:00',
                historical_reason = 'newer_accepted_current',
                caused_by_judgment_id = 10
            WHERE insight_version_id = 201
            """
        )
        connection.execute(
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id, judgment_decision,
                initial_role, current_role, accepted_at
            ) VALUES (202, 2, 10, 'interesting', 'current', 'current',
                      '2026-08-20T08:04:00+00:00')
            """
        )
        _succeed_event(connection, 4)

        _insert_event(connection, 5)
        _insert_insight_version(connection, 302, 3, 2, 301, 5)
        _insert_insight_version(connection, 402, 4, 2, 401, 5)
        connection.execute(
            """
            INSERT INTO user_insight_judgments VALUES
            (13, 3, 302, 'interesting', NULL, '2026-08-20T08:05:00+00:00'),
            (14, 4, 402, 'interesting', NULL, '2026-08-20T08:06:00+00:00')
            """
        )

        _assert_insert_rejected_without_growth(
            connection,
            "accepted_insight_versions",
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id, judgment_decision,
                initial_role, current_role, accepted_at
            ) VALUES (301, 3, 11, 'interesting', 'current', 'current', 'now')
            """,
        )
        _assert_insert_rejected_without_growth(
            connection,
            "accepted_insight_versions",
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id, judgment_decision,
                initial_role, current_role, accepted_at
            ) VALUES (301, 3, 13, 'interesting', 'current', 'current', 'now')
            """,
        )
        _assert_insert_rejected_without_growth(
            connection,
            "accepted_insight_versions",
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id, judgment_decision,
                initial_role, current_role, accepted_at, historical_at,
                historical_reason, caused_by_judgment_id
            ) VALUES (401, 4, 12, 'interesting', 'current', 'historical',
                      'now', 'now', 'newer_accepted_current', 14)
            """,
            match="initial current accepted row must be inserted current",
        )
        _assert_insert_rejected_without_growth(
            connection,
            "accepted_insight_versions",
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id, judgment_decision,
                initial_role, current_role, accepted_at, historical_at,
                historical_reason, caused_by_judgment_id
            ) VALUES (401, 4, 12, 'interesting', 'historical', 'historical',
                      'now', 'now', 'basis_invalid', 14)
            """,
        )

        connection.execute(
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id, judgment_decision,
                initial_role, current_role, accepted_at
            ) VALUES (402, 4, 14, 'interesting', 'current', 'current',
                      '2026-08-20T08:07:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO accepted_insight_versions (
                insight_version_id, insight_id, judgment_id, judgment_decision,
                initial_role, current_role, accepted_at, historical_at,
                historical_reason, caused_by_judgment_id
            ) VALUES (401, 4, 12, 'interesting', 'historical', 'historical',
                      '2026-08-20T08:08:00+00:00', '2026-08-20T08:08:00+00:00',
                      'born_older_than_current', 14)
            """
        )

        for sql in (
            "UPDATE accepted_insight_versions SET accepted_at = accepted_at WHERE insight_version_id = 202",
            "UPDATE accepted_insight_versions SET current_role = 'current' WHERE insight_version_id = 201",
            "UPDATE accepted_insight_versions SET historical_at = 'later' WHERE insight_version_id = 201",
            "DELETE FROM accepted_insight_versions WHERE insight_version_id = 202",
            "DELETE FROM accepted_insight_versions WHERE insight_version_id = 201",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)

        assert tuple(
            connection.execute(
                """
                SELECT insight_version_id, initial_role, current_role, historical_reason
                FROM accepted_insight_versions
                WHERE insight_version_id IN (201, 202, 401, 402)
                ORDER BY insight_version_id
                """
            )
        ) == (
            (201, "current", "historical", "newer_accepted_current"),
            (202, "current", "current", None),
            (401, "historical", "historical", "born_older_than_current"),
            (402, "current", "current", None),
        )
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2b_all_append_only_rows_reject_update_and_delete(tmp_path):
    """M2b: identity, lineage, edges, facts, judgments, and boundaries are immutable."""
    database_path = tmp_path / "m2b-immutable.sqlite3"
    initialize_database(database_path)
    immutable_rows = (
        ("relation_identities", "relation_id", "relation_id = 1"),
        ("relation_versions", "relation_version_id", "relation_version_id = 11"),
        ("insight_identities", "insight_id", "insight_id = 1"),
        ("insight_versions", "insight_version_id", "insight_version_id = 101"),
        (
            "insight_identity_replacements",
            "replaced_insight_id",
            "replaced_insight_id = 1",
        ),
        (
            "insight_version_disqualifications",
            "disqualification_id",
            "disqualification_id = 1",
        ),
        ("user_insight_judgments", "judgment_id", "judgment_id = 1"),
        (
            "organization_event_accepted_boundary",
            "position",
            "event_id = 2 AND insight_version_id = 101",
        ),
        (
            "organization_event_relation_boundary",
            "position",
            "event_id = 2 AND relation_version_id = 11",
        ),
        (
            "relation_version_participants",
            "position",
            "relation_version_id = 21 AND participant_key = 'source-1'",
        ),
        (
            "relation_version_used_relations",
            "position",
            "relation_version_id = 21 AND used_relation_version_id = 11",
        ),
        (
            "insight_version_participants",
            "position",
            "insight_version_id = 101 AND participant_key = 'source-1'",
        ),
        (
            "insight_version_used_relations",
            "position",
            "insight_version_id = 101 AND relation_version_id = 11",
        ),
        ("relation_facts", "relation_fact_id", "relation_fact_id = 1"),
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)
        for table, column, predicate in immutable_rows:
            before = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE {table} SET {column} = {column} WHERE {predicate}"
                )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"DELETE FROM {table} WHERE {predicate}")
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == before
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_m2b_schema_contains_all_required_named_indexes_and_narrow_triggers(tmp_path):
    """M2b: the published migration installs the complete designed static fence set."""
    database_path = tmp_path / "m2b-named-objects.sqlite3"
    initialize_database(database_path)
    required_indexes = {
        "relation_version_participants_source",
        "relation_version_participants_accepted",
        "insight_version_participants_source",
        "insight_version_participants_accepted",
        "relation_versions_semantic_signature",
        "insight_versions_semantic_signature",
        "user_insight_judgments_decision_order",
        "insight_version_disqualifications_kind",
        "one_current_accepted_version_per_identity",
        "accepted_insight_versions_role_order",
        "one_wrong_fact_per_relation",
        "one_replacement_per_relation",
        "one_evolution_successor_per_version",
        "one_basis_invalid_per_version",
        "relation_facts_event_kind",
    }
    narrow_triggers = {
        "relation_version_lineage_must_be_consecutive",
        "insight_version_lineage_must_be_consecutive",
        "accepted_boundary_requires_current",
        "relation_boundary_matches_role",
        "relation_participant_within_event_boundary",
        "insight_participant_within_event_boundary",
        "relation_used_relation_within_event_boundary",
        "insight_used_relation_is_boundary_or_same_event",
        "accepted_initial_current_must_be_inserted_current",
        "accepted_born_historical_cause_on_insert",
        "accepted_newer_current_cause_on_update",
        "accepted_insight_versions_current_to_historical_only",
        "accepted_insight_versions_cannot_be_deleted",
    }
    immutable_tables = {
        "relation_identities",
        "relation_versions",
        "insight_identities",
        "insight_versions",
        "insight_identity_replacements",
        "insight_version_disqualifications",
        "user_insight_judgments",
        "organization_event_accepted_boundary",
        "organization_event_relation_boundary",
        "relation_version_participants",
        "relation_version_used_relations",
        "insight_version_participants",
        "insight_version_used_relations",
        "relation_facts",
    }

    with sqlite3.connect(database_path) as connection:
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }

    assert required_indexes <= indexes
    assert narrow_triggers <= triggers
    assert {
        f"{table}_cannot_be_{action}"
        for table in immutable_tables
        for action in ("updated", "deleted")
    } <= triggers


def test_m2b_required_identity_fk_enum_and_position_columns_are_not_null(tmp_path):
    """M2b: every always-present identity, FK, enum, and position is explicit NOT NULL."""
    database_path = tmp_path / "m2b-not-null.sqlite3"
    initialize_database(database_path)
    required = {
        "relation_identities": {"relation_id", "created_event_id", "created_at"},
        "relation_versions": {
            "relation_version_id", "relation_id", "version_no", "produced_event_id",
            "payload_json", "semantic_signature", "dependency_signature", "created_at",
        },
        "relation_current": {"relation_id", "relation_version_id", "activated_at"},
        "insight_identities": {"insight_id", "created_event_id", "created_at"},
        "insight_versions": {
            "insight_version_id", "insight_id", "version_no", "produced_event_id",
            "payload_json", "semantic_signature", "dependency_signature", "created_at",
        },
        "insight_identity_replacements": {
            "replaced_insight_id", "replacement_insight_id", "event_id",
            "reason_text", "created_at",
        },
        "insight_version_disqualifications": {
            "disqualification_id", "insight_version_id", "fact_kind", "event_id",
            "reason_text", "created_at",
        },
        "user_insight_judgments": {
            "judgment_id", "insight_id", "insight_version_id", "decision", "decided_at",
        },
        "accepted_insight_versions": {
            "insight_version_id", "insight_id", "judgment_id", "judgment_decision",
            "initial_role", "current_role", "accepted_at",
        },
        "organization_event_accepted_boundary": {
            "event_id", "insight_version_id", "position", "qualification_signature",
        },
        "organization_event_relation_boundary": {
            "event_id", "relation_version_id", "boundary_role", "position",
            "qualification_signature",
        },
        "relation_version_participants": {
            "relation_version_id", "participant_key", "input_kind", "position",
            "contribution_text",
        },
        "relation_version_used_relations": {
            "relation_version_id", "used_relation_version_id", "position", "role_text",
        },
        "insight_version_participants": {
            "insight_version_id", "participant_key", "input_kind", "position",
            "contribution_text",
        },
        "insight_version_used_relations": {
            "insight_version_id", "relation_version_id", "position", "role_text",
        },
        "relation_facts": {
            "relation_fact_id", "event_id", "relation_id", "relation_version_id",
            "fact_kind", "reason_text", "created_at",
        },
    }

    with sqlite3.connect(database_path) as connection:
        for table, columns in required.items():
            signature = {
                str(row[1]): int(row[3])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert {column for column in columns if signature[column] != 1} == set()


@pytest.mark.parametrize(
    "failure_point",
    ["middle-ddl", "foreign-key-check", "manifest-insert"],
)
def test_m2b_migration_failure_rolls_back_all_0003_schema_and_manifest(
    tmp_path,
    monkeypatch,
    failure_point,
):
    """M2b: any 0003 DDL, FK-check, or manifest failure restores exact 0002."""
    database_path = tmp_path / f"m2b-rollback-{failure_point}.sqlite3"
    _create_legacy_database(database_path, with_topics=True)
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2A_MIGRATIONS)
    migrate_database(database_path)
    before = _database_snapshot(database_path)
    original = MIGRATIONS[2]

    if failure_point == "middle-ddl":
        statements = (
            original.statements[0],
            "CREATE TABLE migration_0003_rollback_probe (probe_id INTEGER PRIMARY KEY)",
            "CREATE TABLE broken_0003_migration (",
        )
        expected_error = sqlite3.DatabaseError
    elif failure_point == "foreign-key-check":
        statements = original.statements + (
            "CREATE TABLE migration_0003_fk_parent (parent_id INTEGER PRIMARY KEY)",
            """
            CREATE TABLE migration_0003_fk_child (
                parent_id INTEGER,
                FOREIGN KEY (parent_id) REFERENCES migration_0003_fk_parent(parent_id)
                  DEFERRABLE INITIALLY DEFERRED
            )
            """,
            "INSERT INTO migration_0003_fk_child (parent_id) VALUES (999)",
        )
        expected_error = SchemaMigrationError
    else:
        statements = original.statements + (
            """
            CREATE TRIGGER fail_third_manifest_insert
            BEFORE INSERT ON schema_migrations
            WHEN NEW.position = 3
            BEGIN
                SELECT RAISE(ABORT, 'injected 0003 manifest failure');
            END
            """,
        )
        expected_error = sqlite3.DatabaseError

    replacement = Migration.create(
        original.migration_id,
        original.position,
        statements,
    )
    monkeypatch.setattr(
        schema_migrations,
        "MIGRATIONS",
        (MIGRATIONS[0], MIGRATIONS[1], replacement),
    )

    with pytest.raises(expected_error):
        migrate_database(database_path)

    assert _database_snapshot(database_path) == before


def test_m2b_two_threads_serialize_and_apply_0003_manifest_once(tmp_path, monkeypatch):
    """M2b: two independent latest-binary connections apply one exact 0003 prefix."""
    database_path = tmp_path / "m2b-concurrent.sqlite3"
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2A_MIGRATIONS)
    migrate_database(database_path)
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2B_MIGRATIONS)
    barrier = threading.Barrier(2)
    original_begin = schema_migrations._begin_immediate

    def synchronized_begin(connection):
        barrier.wait(timeout=5)
        original_begin(connection)

    monkeypatch.setattr(schema_migrations, "_begin_immediate", synchronized_begin)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(migrate_database, database_path) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in M2B_MIGRATIONS
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def _insert_publication(
    connection: sqlite3.Connection,
    publication_id: int,
    insight_version_id: int,
    judgment_id: int,
    *,
    relative_path: str,
    machine_identity: str,
) -> None:
    connection.execute(
        """
        INSERT INTO accepted_insight_publications (
            publication_id, insight_version_id, judgment_id, relative_path,
            machine_identity, content_sha256, render_context_signature,
            placement_receipt_json, placed_at, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            publication_id,
            insight_version_id,
            judgment_id,
            relative_path,
            machine_identity,
            f"{publication_id:064x}",
            f"{publication_id + 100:064x}",
            '{"codec":"accepted-placement-receipt-v1"}',
            "2026-08-22T00:00:00+00:00",
            "2026-08-22T00:01:00+00:00",
        ),
    )


def test_g3_staged_0003_to_0004_preserves_rows_and_appends_exact_manifest(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "g3-staged.sqlite3"
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2B_MIGRATIONS)
    migrate_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)
    before = _table_rows(
        database_path,
        tuple(sorted(M2B_TABLES - {"schema_migrations"})),
    )

    monkeypatch.setattr(schema_migrations, "MIGRATIONS", MIGRATIONS)
    migrate_database(database_path)

    assert _table_rows(
        database_path,
        tuple(sorted(M2B_TABLES - {"schema_migrations"})),
    ) == before
    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in MIGRATIONS
    )
    assert tuple(migration.checksum for migration in MIGRATIONS[:3]) == tuple(
        migration.checksum for migration in M2B_MIGRATIONS
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM accepted_insight_publications"
        ).fetchone()[0] == 0
        table_info = tuple(connection.execute(
            "PRAGMA table_info(accepted_insight_publications)"
        ))
        assert tuple(str(row[1]) for row in table_info) == (
            "publication_id",
            "insight_version_id",
            "judgment_id",
            "relative_path",
            "machine_identity",
            "content_sha256",
            "render_context_signature",
            "placement_receipt_json",
            "placed_at",
            "recorded_at",
        )
        assert all(int(row[3]) == 1 for row in table_info)
        assert int(table_info[0][5]) == 1
        publication_foreign_keys = tuple(
            connection.execute(
                "PRAGMA foreign_key_list(accepted_insight_publications)"
            )
        )
        assert {
            (str(row[2]), str(row[3]), str(row[4]))
            for row in publication_foreign_keys
        } == {
            (
                "accepted_insight_versions",
                "insight_version_id",
                "insight_version_id",
            ),
            (
                "accepted_insight_versions",
                "judgment_id",
                "judgment_id",
            ),
        }
        assert len({int(row[0]) for row in publication_foreign_keys}) == 1
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger'
                  AND tbl_name = 'accepted_insight_publications'
                """
            )
        }
        assert triggers == {
            "accepted_insight_publications_cannot_be_updated",
            "accepted_insight_publications_cannot_be_deleted",
        }
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


def test_g3_publication_requires_exact_accepted_pair_and_is_immutable(tmp_path):
    database_path = tmp_path / "g3-publication-fences.sqlite3"
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_complete_m2b_graph(connection)
        _insert_insight_identity(connection, 3, 3)
        _insert_insight_version(connection, 301, 3, 1, None, 3)
        _insert_insight_identity(connection, 4, 3)
        _insert_insight_version(connection, 401, 4, 1, None, 3)
        connection.execute(
            """
            INSERT INTO user_insight_judgments
            VALUES (3, 3, 301, 'rethink', NULL, '2026-08-22T00:00:00+00:00')
            """
        )

        _insert_publication(
            connection,
            1,
            101,
            1,
            relative_path="知识蒸馏器/新知/a.md",
            machine_identity="accepted-insight:1:version:101",
        )
        assert connection.execute(
            "SELECT insight_version_id, judgment_id FROM accepted_insight_publications"
        ).fetchone() == (101, 1)

        invalid = (
            (2, 401, 1, "知识蒸馏器/新知/pending.md", "pending"),
            (2, 301, 3, "知识蒸馏器/新知/rethink.md", "rethink"),
            (2, 101, 2, "知识蒸馏器/新知/cross.md", "cross"),
            (2, 201, 1, "知识蒸馏器/新知/cross-accepted.md", "cross-accepted"),
        )
        for publication_id, version_id, judgment_id, path, identity in invalid:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_publication(
                    connection,
                    publication_id,
                    version_id,
                    judgment_id,
                    relative_path=path,
                    machine_identity=identity,
                )

        duplicate_values = (
            (2, 101, 1, "知识蒸馏器/新知/other.md", "other-version"),
            (2, 201, 2, "知识蒸馏器/新知/a.md", "other-path"),
            (
                2,
                201,
                2,
                "知识蒸馏器/新知/other.md",
                "accepted-insight:1:version:101",
            ),
        )
        for publication_id, version_id, judgment_id, path, identity in duplicate_values:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_publication(
                    connection,
                    publication_id,
                    version_id,
                    judgment_id,
                    relative_path=path,
                    machine_identity=identity,
                )

        _insert_publication(
            connection,
            2,
            201,
            2,
            relative_path="知识蒸馏器/新知/current.md",
            machine_identity="accepted-insight:2:version:201",
        )

        with pytest.raises(sqlite3.IntegrityError, match="publications are immutable"):
            connection.execute(
                "UPDATE accepted_insight_publications SET recorded_at = recorded_at"
            )
        with pytest.raises(sqlite3.IntegrityError, match="publications are immutable"):
            connection.execute("DELETE FROM accepted_insight_publications")
        assert connection.execute(
            "SELECT COUNT(*) FROM accepted_insight_publications"
        ).fetchone()[0] == 2
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()


@pytest.mark.parametrize(
    "failure_point",
    ["middle-ddl", "foreign-key-check", "manifest-insert"],
)
def test_g3_migration_failure_rolls_back_all_0004_schema_and_manifest(
    tmp_path,
    monkeypatch,
    failure_point,
):
    database_path = tmp_path / f"g3-rollback-{failure_point}.sqlite3"
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2B_MIGRATIONS)
    migrate_database(database_path)
    before = _database_snapshot(database_path)
    original = MIGRATIONS[3]

    if failure_point == "middle-ddl":
        statements = (
            original.statements[0],
            "CREATE TABLE migration_0004_rollback_probe (probe_id INTEGER PRIMARY KEY)",
            "CREATE TABLE broken_0004_migration (",
        )
        expected_error = sqlite3.DatabaseError
    elif failure_point == "foreign-key-check":
        statements = original.statements + (
            "CREATE TABLE migration_0004_fk_parent (parent_id INTEGER PRIMARY KEY)",
            """
            CREATE TABLE migration_0004_fk_child (
                parent_id INTEGER,
                FOREIGN KEY (parent_id) REFERENCES migration_0004_fk_parent(parent_id)
                  DEFERRABLE INITIALLY DEFERRED
            )
            """,
            "INSERT INTO migration_0004_fk_child (parent_id) VALUES (999)",
        )
        expected_error = SchemaMigrationError
    else:
        statements = original.statements + (
            """
            CREATE TRIGGER fail_fourth_manifest_insert
            BEFORE INSERT ON schema_migrations
            WHEN NEW.position = 4
            BEGIN
                SELECT RAISE(ABORT, 'injected 0004 manifest failure');
            END
            """,
        )
        expected_error = sqlite3.DatabaseError

    replacement = Migration.create(
        original.migration_id,
        original.position,
        statements,
    )
    monkeypatch.setattr(
        schema_migrations,
        "MIGRATIONS",
        (*M2B_MIGRATIONS, replacement),
    )
    with pytest.raises(expected_error):
        migrate_database(database_path)
    assert _database_snapshot(database_path) == before


def test_g3_two_threads_serialize_and_apply_0004_manifest_once(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "g3-concurrent.sqlite3"
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", M2B_MIGRATIONS)
    migrate_database(database_path)
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", MIGRATIONS)
    barrier = threading.Barrier(2)
    original_begin = schema_migrations._begin_immediate

    def synchronized_begin(connection):
        barrier.wait(timeout=5)
        original_begin(connection)

    monkeypatch.setattr(schema_migrations, "_begin_immediate", synchronized_begin)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(migrate_database, database_path) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    assert _manifest(database_path) == tuple(
        (migration.migration_id, migration.position, migration.checksum)
        for migration in MIGRATIONS
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == 4
        assert tuple(connection.execute("PRAGMA foreign_key_check")) == ()
