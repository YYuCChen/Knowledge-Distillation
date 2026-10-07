from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 24
INGESTION_CONTRACT = 'raw-verified-v1'
# These are process bindings, not filesystem verification certificates.
INGESTION_COLUMNS = (
    "ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1'))",
    "source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'))",
    "relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*'))",
)
TOPIC_STATEMENTS = (
    """CREATE TABLE topic_entries (
        topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, scope TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE topic_members (
        topic_id INTEGER NOT NULL REFERENCES topic_entries(topic_id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        knowledge_result_id INTEGER NOT NULL REFERENCES knowledge_results(knowledge_result_id),
        point_id TEXT NOT NULL,
        PRIMARY KEY(topic_id, position), UNIQUE(topic_id, knowledge_result_id, point_id)
    )""",
    """CREATE TABLE topic_snapshot (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        knowledge_count INTEGER NOT NULL CHECK(knowledge_count >= 0),
        input_signature TEXT NOT NULL
    )""",
)

RAW_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS raw_records (
        raw_id TEXT PRIMARY KEY CHECK (raw_id GLOB 'R-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'),
        subject_kind TEXT NOT NULL CHECK (subject_kind IN ('material', 'capture')),
        subject_id INTEGER NOT NULL,
        identity TEXT NOT NULL CHECK (identity IN ('第三方', '本人', '本人附言')),
        relative_path TEXT NOT NULL UNIQUE,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
        attachments_json TEXT NOT NULL DEFAULT '[]',
        supersedes TEXT UNIQUE,
        origin TEXT NOT NULL CHECK (origin IN ('app', 'migration', 'vault')),
        created_at TEXT NOT NULL,
        written_at TEXT,
        written_vault TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS raw_records_subject ON raw_records(subject_kind, subject_id)",
    """CREATE TRIGGER IF NOT EXISTS raw_records_content_no_update
    BEFORE UPDATE OF raw_id, subject_kind, subject_id, identity, relative_path, content, content_sha256,
        attachments_json, supersedes, origin, created_at ON raw_records BEGIN
        SELECT RAISE(ABORT, 'raw record is immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS raw_records_written_once
    BEFORE UPDATE OF written_at, written_vault ON raw_records WHEN OLD.written_at IS NOT NULL BEGIN
        SELECT RAISE(ABORT, 'raw record was already written');
    END""",
    """CREATE TRIGGER IF NOT EXISTS raw_records_no_delete
    BEFORE DELETE ON raw_records BEGIN
        SELECT RAISE(ABORT, 'raw record is immutable');
    END""",
    """CREATE TABLE IF NOT EXISTS raw_counters (
        day TEXT PRIMARY KEY CHECK (length(day) = 8),
        last INTEGER NOT NULL CHECK (last BETWEEN 0 AND 9999)
    )""",
)

SUBMITTED_SCHEMA = """
CREATE TABLE submitted_sources (
    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),
    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', 'markdown', 'pdf', 'epub')),
    input_key TEXT NOT NULL,
    input_label TEXT NOT NULL,
    input_metadata TEXT NOT NULL,
    content BLOB,
    retain_until TEXT,
    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),
    UNIQUE(input_kind, input_key)
);
"""
SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE source_connections (
    platform TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('connected', 'relogin_required', 'unconfigured')
    ),
    generation INTEGER NOT NULL CHECK (generation > 0),
    account_label TEXT,
    connected_at TEXT NOT NULL
);

CREATE TABLE materials (
    material_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    submitted_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_kind, source_key)
);

CREATE TABLE distill_items (
    item_id INTEGER PRIMARY KEY,
    submitted_url TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'working', 'waiting_user', 'succeeded', 'failed')
    ),
    phase TEXT NOT NULL CHECK (
        phase IN ('collecting', 'reviewing', 'distilling', 'publishing', 'done')
    ),
    material_id INTEGER REFERENCES materials(material_id),
    error_code TEXT,
    rejection_reason TEXT,
    dismissed_at TEXT,
    confirmation_json TEXT,
    queued_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_facts (
    source_fact_id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL UNIQUE REFERENCES materials(material_id),
    snapshot TEXT NOT NULL CHECK (TRIM(snapshot) != ''),
    uncertainties_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TRIGGER source_facts_no_update
BEFORE UPDATE ON source_facts BEGIN
    SELECT RAISE(ABORT, 'SourceFact is immutable');
END;

CREATE TRIGGER source_facts_no_delete
BEFORE DELETE ON source_facts BEGIN
    SELECT RAISE(ABORT, 'SourceFact is immutable');
END;

CREATE TABLE knowledge_results (
    knowledge_result_id INTEGER PRIMARY KEY,
    source_fact_id INTEGER NOT NULL UNIQUE REFERENCES source_facts(source_fact_id),
    payload_json TEXT NOT NULL,
    published_path TEXT,
    published_vault TEXT,
    published_at TEXT,
    created_at TEXT NOT NULL,
    CHECK ((published_path IS NULL) = (published_at IS NULL))
);

CREATE TRIGGER knowledge_results_content_no_update
BEFORE UPDATE OF source_fact_id, payload_json, created_at ON knowledge_results BEGIN
    SELECT RAISE(ABORT, 'KnowledgeResult content is immutable');
END;

CREATE TRIGGER knowledge_results_no_delete
BEFORE DELETE ON knowledge_results BEGIN
    SELECT RAISE(ABORT, 'KnowledgeResult is immutable');
END;
"""


@contextmanager
def connect(path: Path, *, timeout: float = 5) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 10 or version == 22:
            # A parent-table rebuild preserves all ids. Disable enforcement only
            # for this migration connection; check every FK before atomic commit.
            connection.execute("PRAGMA foreign_keys = OFF")
        if version == 0:
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + SUBMITTED_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version == 4:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(SUBMITTED_SCHEMA.replace("submitted_sources (", "submitted_sources_v5 (", 1))
            connection.execute("INSERT INTO submitted_sources_v5 SELECT * FROM submitted_sources")
            connection.execute("DROP TABLE submitted_sources")
            connection.execute("ALTER TABLE submitted_sources_v5 RENAME TO submitted_sources")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version in (1, 2, 3):
            connection.execute("BEGIN IMMEDIATE")
            if version == 1:
                connection.execute(
                    "ALTER TABLE distill_items ADD COLUMN queued_at TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "UPDATE distill_items SET queued_at = created_at WHERE queued_at = ''"
                )
            # Old test publications did not record a destination. Do not infer
            # it from today's setting; their handoff remains unavailable.
            if version < 3:
                connection.execute(
                    "ALTER TABLE knowledge_results ADD COLUMN published_vault TEXT"
                )
            connection.execute(SUBMITTED_SCHEMA)
            connection.execute("ALTER TABLE source_facts ADD COLUMN lineage_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version not in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, SCHEMA_VERSION):
            raise RuntimeError(f"unsupported database version: {version}")

        if version < 6:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            for statement in TOPIC_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 7:
            from knowledge_distiller.schema_migrations import (
                ORGANIZATION_EVENT_AND_COVERAGE_STATEMENTS,
                RELATION_INSIGHT_AND_ACCEPTANCE_CORE_STATEMENTS,
            )
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .insight_schema import extend_accepted_table, statements
            for statement in (*ORGANIZATION_EVENT_AND_COVERAGE_STATEMENTS,
                              *RELATION_INSIGHT_AND_ACCEPTANCE_CORE_STATEMENTS):
                connection.execute(extend_accepted_table(statement))
            for statement in statements():
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 8:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE distill_items ADD COLUMN platform_authority_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 9:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE source_connections ADD COLUMN browser_context TEXT")
            connection.execute("""CREATE TABLE source_media (
                material_id INTEGER NOT NULL REFERENCES materials(material_id),
                member_id TEXT NOT NULL, position INTEGER NOT NULL,
                mime_type TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY(material_id, member_id), UNIQUE(material_id, position)
            )""")
            for action in ('UPDATE', 'DELETE'):
                connection.execute(f"""CREATE TRIGGER source_media_no_{action.lower()}
                    BEFORE {action} ON source_media
                    WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = OLD.material_id) BEGIN
                    SELECT RAISE(ABORT, 'SourceFact media is immutable'); END""")
            connection.execute("""CREATE TRIGGER source_media_no_insert
                BEFORE INSERT ON source_media
                WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = NEW.material_id) BEGIN
                SELECT RAISE(ABORT, 'SourceFact media is immutable'); END""")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 10:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .source_versions import migrate_material_snapshots
            migrate_material_snapshots(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("database migration found broken source references")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 11:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .collection_schema import STATEMENTS
            for statement in STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 12:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(distill_items)')}
            if 'rejection_reason' not in columns:
                connection.execute('ALTER TABLE distill_items ADD COLUMN rejection_reason TEXT')
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 13:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(distill_items)')}
            if 'dismissed_at' not in columns:
                connection.execute('ALTER TABLE distill_items ADD COLUMN dismissed_at TEXT')
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 14:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(distill_items)')}
            if 'submitted_title' not in columns:
                connection.execute("ALTER TABLE distill_items ADD COLUMN submitted_title TEXT NOT NULL DEFAULT ''")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 15:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .feishu_schema import STATEMENTS
            for statement in STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 16:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .media_lifecycle import migrate
            migrate(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 17:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS confirmation_decisions (
                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
                revision TEXT NOT NULL, action TEXT NOT NULL, value TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(item_id, revision))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS feishu_action_queue (
                id INTEGER PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
                app_id TEXT NOT NULL, message_id TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT)""")
            submitted_sql=connection.execute("SELECT sql FROM sqlite_master WHERE name='submitted_sources'").fetchone()[0]
            if "'image'" not in submitted_sql:
                connection.execute(submitted_sql.replace('submitted_sources', 'submitted_sources_image').replace("'epub'", "'epub', 'image'"))
                connection.execute('INSERT INTO submitted_sources_image SELECT * FROM submitted_sources')
                connection.execute('DROP TABLE submitted_sources')
                connection.execute(submitted_sql.replace("'epub'", "'epub', 'image'"))
                connection.execute('INSERT INTO submitted_sources SELECT * FROM submitted_sources_image')
                connection.execute('DROP TABLE submitted_sources_image')
            trigger=connection.execute("SELECT sql FROM sqlite_master WHERE name='source_media_no_update'").fetchone()
            if trigger and "'image'" not in trigger[0]:
                connection.execute('DROP TRIGGER source_media_no_update')
                connection.execute(trigger[0].replace("'bilibili'", "'bilibili','image'"))
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 18:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(distill_items)')}
            if 'review_revision' not in columns:
                connection.execute("ALTER TABLE distill_items ADD COLUMN review_revision INTEGER NOT NULL DEFAULT 0")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS distill_review_revision AFTER UPDATE ON distill_items
                WHEN NEW.review_revision = OLD.review_revision AND (
                    NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase
                    OR NEW.material_id IS NOT OLD.material_id
                    OR NEW.submitted_url IS NOT OLD.submitted_url
                    OR NEW.confirmation_json IS NOT OLD.confirmation_json
                    OR NEW.platform_authority_json IS NOT OLD.platform_authority_json
                ) BEGIN
                UPDATE distill_items SET review_revision = OLD.review_revision + 1
                WHERE item_id = NEW.item_id;
            END""")
            connection.execute("""CREATE TABLE IF NOT EXISTS source_review_results (
                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
                revision INTEGER NOT NULL, identity TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('complete','failed')),
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(item_id, revision))""")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 19:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .confirmation_schema import migrate
            migrate(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 20:
            # raw/ index (docs/engineering/raw-interface.md). A V1.3 binary cannot
            # open schema 20; that rule is unchanged from earlier upgrades.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            for statement in RAW_STATEMENTS:
                connection.execute(statement)
            from .media_lifecycle import require_raw
            require_raw(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 21:
            # Feishu quick notes: append-only captures, transcripts, adjacency and
            # identity events (docs/roadmap/handoff-feishu-capture.md).
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .capture_schema import STATEMENTS as CAPTURE_STATEMENTS
            for statement in CAPTURE_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 22:
            # V3 wiki maintenance is a separate durable domain. It does not
            # reuse or rewrite the legacy organization event tables.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .wiki_schema import migrate
            migrate(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 23:
            # Cheap Local Web status is a last explicit machine observation,
            # not a replacement for the authoritative freeze at submission.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .wiki_schema import migrate_observations, migrate_v23
            if version == 22:
                migrate_v23(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("database migration found broken wiki task references")
            else:
                migrate_observations(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if version < 24:
            if not connection.in_transaction:
                connection.execute('BEGIN IMMEDIATE')
            migrate_v24(connection)
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RuntimeError('database migration found broken ingestion references')
            connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')


def migrate_v24(connection: sqlite3.Connection) -> None:
    """Add inert process storage; do not backfill proof or touch original bytes."""
    for table in ('distill_items', 'collection_operations'):
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        for definition in INGESTION_COLUMNS:
            if definition.split()[0] not in columns:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON {table}
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END""")
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_ingestion_binding_required
            BEFORE INSERT ON {table} WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS distill_items_ingestion_owner_immutable
        BEFORE UPDATE OF material_id ON distill_items
        WHEN OLD.ingestion_contract!='legacy' AND OLD.material_id IS NOT NULL
          AND NEW.material_id IS NOT OLD.material_id
        BEGIN SELECT RAISE(ABORT,'ingestion owner is immutable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS distill_items_ingestion_no_delete
        BEFORE DELETE ON distill_items WHEN OLD.ingestion_contract!='legacy'
        BEGIN SELECT RAISE(ABORT,'ingestion owner is durable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS collection_members_ingestion_contract_match
        BEFORE INSERT ON collection_members
        WHEN (SELECT ingestion_contract FROM collection_operations WHERE operation_id=NEW.operation_id)
          IS NOT (SELECT ingestion_contract FROM distill_items WHERE item_id=NEW.item_id)
        BEGIN SELECT RAISE(ABORT,'collection ingestion contract mismatch'); END""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ingestion_events (
        event_id INTEGER PRIMARY KEY,
        event_key TEXT NOT NULL UNIQUE CHECK(length(event_key)=64 AND event_key NOT GLOB '*[^0-9a-f]*'),
        contract TEXT NOT NULL CHECK(contract='raw-verified-v1'),
        subject_kind TEXT NOT NULL CHECK(subject_kind IN ('item','material','capture')),
        subject_id INTEGER NOT NULL CHECK(subject_id>0),
        item_id INTEGER REFERENCES distill_items(item_id),
        kind TEXT NOT NULL CHECK(kind IN ('source_ready','raw_pending','raw_verified','release_authorized','media_released')),
        binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
        detail_json TEXT NOT NULL CHECK(json_valid(detail_json) AND json_type(detail_json)='object'),
        created_at TEXT NOT NULL CHECK(trim(created_at)!='')
    )""")
    connection.execute('CREATE INDEX IF NOT EXISTS ingestion_events_subject ON ingestion_events(subject_kind,subject_id,event_id)')
    connection.execute("""CREATE TRIGGER IF NOT EXISTS ingestion_events_observation_typed
        BEFORE INSERT ON ingestion_events WHEN NEW.kind IN ('source_ready','raw_pending')
          AND COALESCE(NOT (
            NEW.subject_kind='item' AND NEW.subject_id=NEW.item_id
            AND EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=NEW.item_id
                        AND i.ingestion_contract=NEW.contract
                        AND i.source_binding_sha256=json_extract(NEW.detail_json,'$.source_binding_sha256')
                        AND i.relation_binding_sha256=json_extract(NEW.detail_json,'$.relation_binding_sha256'))
            AND (SELECT count(*) FROM json_each(NEW.detail_json))=4
            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json))=4
            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json)
                            WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256'))
            AND json_type(NEW.detail_json,'$.manifest')='object'
            AND (SELECT count(*) FROM json_each(NEW.detail_json,'$.manifest'))=2
            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json,'$.manifest'))=2
            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json,'$.manifest')
                            WHERE key NOT IN ('source_fact_id','snapshot_sha256'))
            AND ((NEW.kind='source_ready' AND json_extract(NEW.detail_json,'$.code')='source_fact_ready')
                 OR (NEW.kind='raw_pending' AND json_extract(NEW.detail_json,'$.code')
                     IN ('context_pending','readback_pending','writer_pending')))
            AND ((json_type(NEW.detail_json,'$.manifest.source_fact_id')='null'
                  AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='null'
                  AND NEW.kind='raw_pending')
                 OR (json_type(NEW.detail_json,'$.manifest.source_fact_id')='integer'
                     AND json_extract(NEW.detail_json,'$.manifest.source_fact_id')>0
                     AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='text'
                     AND length(json_extract(NEW.detail_json,'$.manifest.snapshot_sha256'))=64
                     AND json_extract(NEW.detail_json,'$.manifest.snapshot_sha256') NOT GLOB '*[^0-9a-f]*'))
          ),1)
        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END""")
    for action in ('UPDATE', 'DELETE'):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS ingestion_events_no_{action.lower()}
            BEFORE {action} ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END""")
    # A1 has no filesystem-proof producer. Neither an API nor direct SQL can
    # manufacture a verified/release event until the reviewed A2 boundary exists.
    connection.execute("""CREATE TRIGGER IF NOT EXISTS ingestion_events_proof_unavailable
        BEFORE INSERT ON ingestion_events
        WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')
        BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END""")
    from .wiki_schema import migrate_outcome_storage
    migrate_outcome_storage(connection)
    from .media_lifecycle import protect_ingestion
    protect_ingestion(connection)
