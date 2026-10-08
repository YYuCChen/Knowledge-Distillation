from __future__ import annotations

import sqlite3
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 26
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
    # Normal connections have no filesystem authority. A2 installs exact,
    # transaction-local predicates only after locked readback and binding CAS.
    connection.create_function('ingestion_proof', 3, lambda *_: 0)
    connection.create_function('ingestion_release', 4, lambda *_: 0)
    connection.create_function('ingestion_raw_terminal', 4, lambda *_: 0)
    connection.create_function('local_intake_insert', 7, lambda *_: 0)
    connection.create_function('local_intake_event', 7, lambda *_: 0)
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
        if version < 26:
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
        elif version not in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, SCHEMA_VERSION):
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

        if version < 25:
            if not connection.in_transaction:
                connection.execute('BEGIN IMMEDIATE')
            migrate_v25(connection)
            connection.execute('PRAGMA user_version = 25')
        if version < 26:
            if not connection.in_transaction:
                connection.execute('BEGIN IMMEDIATE')
            migrate_v26(connection)
            connection.execute('PRAGMA user_version = 26')
            connection.commit()
            connection.execute('PRAGMA foreign_keys = ON')
            if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise RuntimeError('local intake migration committed; FK re-enable failed')


RAW_OWNER_COLUMNS = (
    'item_id', 'submitted_url', 'state', 'phase', 'material_id', 'error_code',
    'rejection_reason', 'dismissed_at', 'confirmation_json', 'queued_at',
    'created_at', 'updated_at', 'platform_authority_json', 'submitted_title',
    'review_revision', 'ingestion_contract', 'source_binding_sha256', 'relation_binding_sha256',
)
# Source-audited parent dependents; no guessed SQL dependency rewriting.
RAW_PARENT_TRIGGERS = frozenset((
    'distill_review_revision', 'distill_items_ingestion_binding_immutable',
    'distill_items_ingestion_binding_required', 'distill_items_ingestion_owner_immutable',
    'distill_items_ingestion_no_delete', 'collection_members_ingestion_contract_match',
    'ingestion_events_observation_typed', 'source_media_no_update',
    'source_media_ingestion_no_update', 'source_media_ingestion_no_delete',
    'submitted_sources_ingestion_no_release', 'submitted_sources_ingestion_no_delete',
    'submitted_sources_ingestion_owner_immutable', 'source_media_ingestion_capture_binding',
    'source_media_ingestion_capture_release', 'source_media_ingestion_capture_no_delete',
))
RAW_PARENT_CHILDREN = frozenset((
    'submitted_sources', 'confirmation_decisions', 'source_review_results',
    'collection_members', 'manual_cards', 'group_decisions', 'capture_state',
    'feishu_parts', 'ingestion_events',
))
RAW_TERMINAL_TRIGGERS = (
    """CREATE TRIGGER distill_items_raw_terminal_no_insert
        BEFORE INSERT ON distill_items WHEN NEW.state='raw_saved'
        BEGIN SELECT RAISE(ABORT,'raw terminal writer required'); END""",
    """CREATE TRIGGER distill_items_raw_terminal_proof
        BEFORE UPDATE ON distill_items WHEN NEW.state='raw_saved' AND OLD.state!='raw_saved'
        AND (OLD.state!='working' OR OLD.phase NOT IN ('collecting','reviewing')
            OR NEW.phase!='done' OR NEW.ingestion_contract!='raw-verified-v1'
            OR NEW.confirmation_json IS NOT NULL OR NEW.dismissed_at IS NOT NULL
            OR NEW.material_id IS NULL OR NEW.source_binding_sha256 IS NULL
            OR NEW.relation_binding_sha256 IS NULL
            OR ingestion_raw_terminal(OLD.item_id,OLD.review_revision,NEW.state,NEW.phase)!=1)
        BEGIN SELECT RAISE(ABORT,'raw terminal proof unavailable'); END""",
    """CREATE TRIGGER distill_items_raw_terminal_no_reopen
        BEFORE UPDATE OF state,phase ON distill_items WHEN OLD.state='raw_saved'
        AND (NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase)
        BEGIN SELECT RAISE(ABORT,'raw terminal is durable'); END""",
)


def _quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _migration_inventory(db):
    """Full typed-row/FK inventory, including wiki/observation/ownership domains."""
    result = {}
    for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall():
        table = row[0]
        columns = tuple(tuple(r) for r in db.execute(f'PRAGMA table_xinfo({_quoted(table)})'))
        names = ','.join(_quoted(c[1]) for c in columns)
        types = ','.join(f'typeof({_quoted(c[1])})' for c in columns)
        digest = hashlib.sha256(b'raw-terminal-migration-v1\0')
        count = 0
        for values in db.execute(f'SELECT rowid,{names},{types} FROM {_quoted(table)} ORDER BY rowid'):
            payload = repr(tuple(values)).encode('utf-8')
            digest.update(len(payload).to_bytes(8, 'big'))
            digest.update(payload)
            count += 1
        fks = tuple(tuple(r) for r in db.execute(f'PRAGMA foreign_key_list({_quoted(table)})'))
        indexes = tuple(tuple(r) for r in db.execute(f'PRAGMA index_list({_quoted(table)})'))
        result[table] = (columns, count, digest.hexdigest(), fks, indexes)
    return result


def _check_v25_preservation(db, before, catalog, parent_sql):
    if _migration_inventory(db) != before:
        raise RuntimeError('raw terminal migration changed old data or references')
    after = {tuple(r[:2]): tuple(r[2:]) for r in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')}
    for key, value in catalog.items():
        if key != ('table', 'distill_items') and after.get(key) != value:
            raise RuntimeError('raw terminal migration changed old schema object')
    actual = after[('table', 'distill_items')][1]
    # SQLite quotes the final name during ALTER TABLE RENAME; no other rewrite allowed.
    expected = parent_sql.replace('CREATE TABLE distill_items', 'CREATE TABLE "distill_items"', 1)
    if actual != expected or set(after) - set(catalog) != {
            ('trigger', statement.split()[2]) for statement in RAW_TERMINAL_TRIGGERS}:
        raise RuntimeError('raw terminal migration unexpected DDL')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RuntimeError('raw terminal migration broken references')
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError('raw terminal migration integrity failed')


def migrate_v25(db):
    """Atomic parent rebuild only; preserve legacy rows, guards and all source bytes."""
    if not db.in_transaction or db.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
        raise RuntimeError('raw terminal migration connection required')
    catalog = {tuple(r[:2]): tuple(r[2:]) for r in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')}
    if any(name == 'distill_items_v25' for _, name in catalog):
        raise RuntimeError('raw terminal migration reserved name collision')
    before = _migration_inventory(db)
    columns = before['distill_items'][0]
    if len(columns) != 18 or {c[1] for c in columns} != set(RAW_OWNER_COLUMNS) or any(c[6] for c in columns):
        raise RuntimeError('raw terminal migration unsupported parent columns')
    parent = catalog[('table', 'distill_items')][1]
    if not parent.startswith('CREATE TABLE distill_items (') or not parent.rstrip().endswith(')'):
        raise RuntimeError('raw terminal migration unsupported parent DDL')
    state_check = re.compile(r"state IN \(\s*'queued',\s*'working',\s*'waiting_user',\s*'succeeded',\s*'failed'\s*\)")
    parent, changes = state_check.subn("state IN ('queued','working','waiting_user','succeeded','failed','raw_saved')", parent)
    if changes != 1:
        raise RuntimeError('raw terminal migration unsupported state CHECK')
    parent = parent.rstrip()[:-1] + ", CHECK(state!='raw_saved' OR (phase='done' AND ingestion_contract='raw-verified-v1')))"
    dependents = {}
    incoming = set()
    for table, (_, _, _, fks, _) in before.items():
        for fk in fks:
            if fk[2] == 'distill_items':
                if fk[3:7] != ('item_id', 'item_id', 'NO ACTION', 'NO ACTION'):
                    raise RuntimeError('raw terminal migration unsupported incoming FK')
                incoming.add(table)
    if incoming != RAW_PARENT_CHILDREN or before['distill_items'][4]:
        raise RuntimeError('raw terminal migration unsupported parent references or indexes')
    if before['distill_items'][3] != ((0, 0, 'materials', 'material_id', 'material_id',
                                      'NO ACTION', 'NO ACTION', 'NONE'),):
        raise RuntimeError('raw terminal migration unsupported outgoing FK')
    for (kind, name), (table, sql) in catalog.items():
        if kind in ('trigger', 'view', 'index') and (table == 'distill_items' or
                (sql and re.search(r'\bdistill_items\b', sql, re.IGNORECASE))):
            if kind != 'trigger' or name not in RAW_PARENT_TRIGGERS:
                raise RuntimeError('raw terminal migration unsupported dependent object')
            dependents[name] = sql
    if set(dependents) != RAW_PARENT_TRIGGERS:
        raise RuntimeError('raw terminal migration missing parent guards')
    db.execute(parent.replace('CREATE TABLE distill_items', 'CREATE TABLE distill_items_v25', 1))
    names = ','.join(_quoted(c[1]) for c in columns)
    db.execute(f'INSERT INTO distill_items_v25({names}) SELECT {names} FROM distill_items')
    # Only this migration connection has FK enforcement disabled. No ordinary guard bypass.
    for name in sorted(dependents):
        db.execute(f'DROP TRIGGER {_quoted(name)}')
    db.execute('DROP TABLE distill_items')
    db.execute('ALTER TABLE distill_items_v25 RENAME TO distill_items')
    for name in sorted(dependents):
        db.execute(dependents[name])
    for statement in RAW_TERMINAL_TRIGGERS:
        db.execute(statement)
    _check_v25_preservation(db, before, catalog, parent)


OBSERVATION_V24_SQL = """CREATE TRIGGER IF NOT EXISTS ingestion_events_observation_typed
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
        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END"""


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
    connection.execute(_SOURCE_GUARDS['distill_items_ingestion_owner_immutable'][2].replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))
    connection.execute(_SOURCE_GUARDS['distill_items_ingestion_no_delete'][2].replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))
    connection.execute("""CREATE TRIGGER IF NOT EXISTS collection_members_ingestion_contract_match
        BEFORE INSERT ON collection_members
        WHEN (SELECT ingestion_contract FROM collection_operations WHERE operation_id=NEW.operation_id)
          IS NOT (SELECT ingestion_contract FROM distill_items WHERE item_id=NEW.item_id)
        BEGIN SELECT RAISE(ABORT,'collection ingestion contract mismatch'); END""")
    connection.execute(_SOURCE_GUARDS['ingestion_events'][2].replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
    connection.execute(_SOURCE_GUARDS['ingestion_events_subject'][2].replace('CREATE INDEX ', 'CREATE INDEX IF NOT EXISTS ', 1))
    connection.execute(OBSERVATION_V24_SQL)
    for action in ('UPDATE', 'DELETE'):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS ingestion_events_no_{action.lower()}
            BEFORE {action} ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END""")
    # This DDL is for fresh/22/23 synthetic candidate roots only. initialize(24)
    # intentionally does not replace an earlier candidate's fail-closed guard.
    connection.execute(_SOURCE_GUARDS['ingestion_events_proof_unavailable'][2].replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))
    from .wiki_schema import migrate_outcome_storage
    migrate_outcome_storage(connection)
    from .media_lifecycle import protect_ingestion
    protect_ingestion(connection)


# Historical SUBMITTED_SCHEMA remains the input for earlier migrations.
SUBMITTED_SCHEMA_V26 = SUBMITTED_SCHEMA.replace("'epub'", "'epub', 'image'").replace(
    '    UNIQUE(input_kind, input_key)',
    "    binding_scope TEXT NOT NULL DEFAULT 'legacy' CHECK(typeof(binding_scope)='text'),\n"
    '    UNIQUE(input_kind, input_key, binding_scope)')
LOCAL_EVENT_INDEX = """CREATE UNIQUE INDEX ingestion_events_local_owner ON ingestion_events(item_id)
    WHERE kind='raw_pending' AND json_extract(detail_json,'$.code')='intake_frozen'"""
_LOCAL_OBSERVATION = """NEW.kind='raw_pending' AND NEW.subject_kind='item'
            AND NEW.subject_id=NEW.item_id AND json_extract(NEW.detail_json,'$.code')='intake_frozen'
            AND (SELECT count(*) FROM json_each(NEW.detail_json))=4
            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json))=4
            AND NOT EXISTS(SELECT 1 FROM json_each(NEW.detail_json)
                WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256'))
            AND json_type(NEW.detail_json,'$.manifest')='object'
            AND (SELECT count(*) FROM json_each(NEW.detail_json,'$.manifest'))=1
            AND json_type(NEW.detail_json,'$.manifest.intake_envelope_json')='text'
            AND local_intake_event(NEW.event_key,NEW.contract,NEW.subject_kind,NEW.subject_id,
                NEW.item_id,NEW.binding_sha256,NEW.detail_json)=1"""
OBSERVATION_V26_SQL = OBSERVATION_V24_SQL.replace(
    'CREATE TRIGGER IF NOT EXISTS', 'CREATE TRIGGER', 1).replace(
    'AND COALESCE(NOT (', 'AND COALESCE(NOT ((', 1).replace(
    '          ),1)', '          ) OR (' + _LOCAL_OBSERVATION + ')),1)', 1)


def _stored_ddl(statement):
    return statement.strip().rstrip(';')


SUBMITTED_BINDING_COLUMNS = ('item_id', 'input_kind', 'input_key', 'input_label',
                             'input_metadata', 'content', 'retain_until', 'retryable')


def _source_ddl(statement):
    """Only SQLite's documented CREATE prefix/storage spelling differences."""
    sql = _stored_ddl(statement)
    for kind in ('TABLE', 'INDEX', 'TRIGGER'):
        prefix = 'CREATE ' + kind + ' IF NOT EXISTS '
        if sql.startswith(prefix):
            return 'CREATE ' + kind + ' ' + sql[len(prefix):]
    return sql


def source_schema_inventory(db):
    """Finite catalog/ABI read only; missing guards never grant capability.

    No initialization, migration, callbacks, row/body hashing or byte loading.
    A known damaged guard is returned as a gap; unknown structure is rejected.
    """
    from .media_lifecycle import OLD_INPUT_GUARDS, LOCAL_INPUT_GUARDS, RELEASABLE
    from .capture_schema import STATEMENTS as CAPTURE_STATEMENTS
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (25, 26):
        raise ValueError('source_schema_unsupported')
    catalog = {r[1]: (r[0], r[2], r[3]) for r in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema')}
    expected = dict(_SOURCE_GUARDS)
    for statement in RAW_STATEMENTS:
        sql = _source_ddl(statement)
        kind, name = sql.split()[1:3]
        parent = 'raw_records' if name != 'raw_counters' else name
        expected[name] = (kind.lower(), parent, sql)
    submitted = SUBMITTED_SCHEMA_V26 if version == 26 else SUBMITTED_SCHEMA.replace("'epub'", "'epub', 'image'")
    expected['submitted_sources'] = ('table', 'submitted_sources', _source_ddl(submitted))
    for statement in (*OLD_INPUT_GUARDS, *(LOCAL_INPUT_GUARDS if version == 26 else ())):
        sql = _source_ddl(statement)
        expected[sql.split()[2]] = ('trigger', 'submitted_sources', sql)
    observation = OBSERVATION_V26_SQL if version == 26 else OBSERVATION_V24_SQL
    expected['ingestion_events_observation_typed'] = ('trigger', 'ingestion_events', _source_ddl(observation))
    if version == 26:
        expected['ingestion_events_local_owner'] = ('index', 'ingestion_events', _source_ddl(LOCAL_EVENT_INDEX))
    for statement in RAW_TERMINAL_TRIGGERS:
        expected[statement.split()[2]] = ('trigger', 'distill_items', statement)
    for statement in CAPTURE_STATEMENTS:
        sql = _source_ddl(statement)
        kind, name = sql.split()[1:3]
        table = name if kind == 'TABLE' else name.rsplit('_no_', 1)[0]
        expected[name] = (kind.lower(), table, sql)
    start = SCHEMA.index('CREATE TABLE distill_items (')
    parent = SCHEMA[start:SCHEMA.index('\n);', start) + 2]
    for definition in ("platform_authority_json TEXT NOT NULL DEFAULT '{}'",
                       "submitted_title TEXT NOT NULL DEFAULT ''",
                       'review_revision INTEGER NOT NULL DEFAULT 0', *INGESTION_COLUMNS):
        parent = parent[:-1] + ', ' + definition + ')'
    old_state = "state IN ('queued', 'working', 'waiting_user', 'succeeded', 'failed')"
    if parent.count(old_state) != 1:
        raise ValueError('source_schema_unsupported')
    parent = parent.replace(old_state,
                            "state IN ('queued','working','waiting_user','succeeded','failed','raw_saved')", 1)
    parent = parent[:-1] + ", CHECK(state!='raw_saved' OR (phase='done' AND ingestion_contract='raw-verified-v1')))"
    expected['distill_items'] = ('table', 'distill_items', parent)
    start = SCHEMA.index('CREATE TABLE source_facts (')
    expected['source_facts'] = ('table', 'source_facts', SCHEMA[start:SCHEMA.index('\n);', start) + 2])
    for name in ('source_facts_no_update', 'source_facts_no_delete'):
        start = SCHEMA.index('CREATE TRIGGER ' + name)
        expected[name] = ('trigger', 'source_facts', SCHEMA[start:SCHEMA.index('\nEND;', start) + 4])
    expected['source_media'] = ('table', 'source_media', '''CREATE TABLE source_media (
                material_id INTEGER NOT NULL REFERENCES materials(material_id),
                member_id TEXT NOT NULL, position INTEGER NOT NULL,
                mime_type TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY(material_id, member_id), UNIQUE(material_id, position)
            )''')
    expected['source_media_no_delete'] = ('trigger', 'source_media', '''CREATE TRIGGER source_media_no_delete
                    BEFORE DELETE ON source_media
                    WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = OLD.material_id) BEGIN
                    SELECT RAISE(ABORT, 'SourceFact media is immutable'); END''')
    expected['source_media_no_insert'] = ('trigger', 'source_media', '''CREATE TRIGGER source_media_no_insert
                BEFORE INSERT ON source_media
                WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = NEW.material_id) BEGIN
                SELECT RAISE(ABORT, 'SourceFact media is immutable'); END''')
    expected['source_media_no_update'] = ('trigger', 'source_media', f'''CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media
        WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id=OLD.material_id)
        AND NOT (NEW.material_id=OLD.material_id AND NEW.member_id=OLD.member_id
            AND NEW.position=OLD.position AND NEW.mime_type=OLD.mime_type
            AND NEW.sha256=OLD.sha256 AND length(OLD.content)>0
            AND typeof(NEW.content)='blob' AND length(NEW.content)=0
            AND EXISTS (SELECT 1 FROM materials m WHERE m.material_id=OLD.material_id AND {RELEASABLE}))
        BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END''')
    gaps = []
    for name, (kind, table, sql) in expected.items():
        actual = catalog.get(name)
        variants = {_source_ddl(sql)}
        if kind == 'table':
            variants.add(_source_ddl(sql).replace('CREATE TABLE ' + name, 'CREATE TABLE "' + name + '"', 1))
        matches = (actual is not None and actual[:2] == (kind, table)
                   and isinstance(actual[2], str) and _source_ddl(actual[2]) in variants)
        if not matches:
            if kind == 'table':
                raise ValueError('source_schema_unsupported')
            gaps.append(name)
    # Exact table DDL fixes CHECK/FK/column ABI. Explicit indexes fix uniqueness.
    # Extra guards/indexes on these source tables are not silently adopted.
    tables = {'raw_records', 'raw_counters', 'source_facts', 'source_media',
              'submitted_sources', 'ingestion_events', 'captures', 'capture_state',
              'capture_transcripts', 'capture_identity_events', 'delivery_adjacency'}
    if any(table in tables and name not in expected and not (kind == 'index' and sql is None)
           for name, (kind, table, sql) in catalog.items()):
        raise ValueError('source_schema_unsupported')
    columns = tuple(tuple(r) for r in db.execute('PRAGMA table_xinfo(distill_items)'))
    if (len(columns) != len(RAW_OWNER_COLUMNS) or {r[1] for r in columns} != set(RAW_OWNER_COLUMNS)
            or any(r[6] or r[2] != ('INTEGER' if r[1] in {'item_id', 'material_id', 'review_revision'} else 'TEXT')
                   for r in columns)):
        raise ValueError('source_schema_unsupported')
    if tuple(tuple(r) for r in db.execute('PRAGMA foreign_key_list(distill_items)')) != (
            (0, 0, 'materials', 'material_id', 'material_id', 'NO ACTION', 'NO ACTION', 'NONE'),):
        raise ValueError('source_schema_unsupported')
    for table, wanted in (('submitted_sources', ('input_kind', 'input_key', 'binding_scope') if version == 26 else ('input_kind', 'input_key')),
                          ('source_media', ('material_id', 'member_id'))):
        indexes = db.execute('PRAGMA index_list(' + table + ')').fetchall()
        unique = []
        for row in indexes:
            if row[2] and not row[4]:
                keys = tuple(r[2] for r in db.execute('PRAGMA index_xinfo(' + _quoted(row[1]) + ')') if r[5])
                unique.append(keys)
        required = {wanted} if table == 'submitted_sources' else {wanted, ('material_id', 'position')}
        if set(unique) != required or len(unique) != len(required):
            raise ValueError('source_schema_unsupported')
    return version, tuple(sorted(gaps))


def migrate_v26(db):
    """One input-table rebuild; immutable old evidence is never rebound."""
    from .media_lifecycle import OLD_INPUT_GUARDS, LOCAL_INPUT_GUARDS
    if not db.in_transaction or db.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
        raise RuntimeError('local intake migration connection required')
    catalog = {tuple(r[:2]): tuple(r[2:]) for r in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')}
    old_sql = catalog.get(('table', 'submitted_sources'), (None, None))[1]
    historical = _stored_ddl(SUBMITTED_SCHEMA.replace("'epub'", "'epub', 'image'"))
    if old_sql not in (historical, historical.replace('CREATE TABLE submitted_sources',
                                                     'CREATE TABLE "submitted_sources"', 1)):
        raise RuntimeError('local intake migration unsupported input DDL')
    before = _migration_inventory(db)
    columns = before['submitted_sources'][0]
    names = tuple(c[1] for c in columns)
    if names != ('item_id', 'input_kind', 'input_key', 'input_label', 'input_metadata',
                 'content', 'retain_until', 'retryable') or any(c[6] for c in columns):
        raise RuntimeError('local intake migration unsupported input columns')
    if before['submitted_sources'][3] != ((0, 0, 'distill_items', 'item_id', 'item_id',
                                         'NO ACTION', 'NO ACTION', 'NONE'),):
        raise RuntimeError('local intake migration unsupported input FK')
    if before['submitted_sources'][4] != ((0, 'sqlite_autoindex_submitted_sources_1', 1, 'u', 0),):
        raise RuntimeError('local intake migration unsupported input indexes')
    if any(fk[2] == 'submitted_sources' for value in before.values() for fk in value[3]):
        raise RuntimeError('local intake migration unsupported incoming FK')
    expected_guards = {s.split()[5]: _stored_ddl(s).replace('CREATE TRIGGER IF NOT EXISTS',
                                                          'CREATE TRIGGER', 1)
                       for s in OLD_INPUT_GUARDS}
    dependent = {}
    for (kind, name), (table, sql) in catalog.items():
        if kind in ('trigger', 'view', 'index') and (table == 'submitted_sources' or
                (sql and re.search(r'\bsubmitted_sources\b', sql, re.IGNORECASE))):
            if kind == 'index' and sql is None and name == 'sqlite_autoindex_submitted_sources_1':
                continue
            if kind != 'trigger' or name not in expected_guards or sql != expected_guards[name]:
                raise RuntimeError('local intake migration unsupported dependent object')
            dependent[name] = sql
    if set(dependent) != set(expected_guards):
        raise RuntimeError('local intake migration missing input guards')
    if catalog.get(('trigger', 'ingestion_events_observation_typed'), (None, None))[1] != (
            _stored_ddl(OBSERVATION_V24_SQL).replace('CREATE TRIGGER IF NOT EXISTS', 'CREATE TRIGGER', 1)):
        raise RuntimeError('local intake migration unsupported observation guard')
    additions = {('trigger', s.split()[2]) for s in LOCAL_INPUT_GUARDS} | {
        ('index', 'ingestion_events_local_owner')}
    if any(key in catalog for key in additions) or ('table', 'submitted_sources_v26') in catalog:
        raise RuntimeError('local intake migration reserved name collision')
    if db.execute("""SELECT 1 FROM submitted_sources WHERE typeof(item_id)!='integer'
        OR typeof(input_kind)!='text' OR typeof(input_key)!='text' OR typeof(input_label)!='text'
        OR typeof(input_metadata)!='text' OR typeof(content) NOT IN ('blob','null')
        OR typeof(retain_until) NOT IN ('text','null') OR typeof(retryable)!='integer'
        OR retryable NOT IN (0,1) LIMIT 1""").fetchone():
        raise RuntimeError('local intake migration invalid input types')
    quoted_names = ','.join(_quoted(n) for n in names)
    old_rows = [tuple(r) for r in db.execute(f'SELECT {quoted_names} FROM submitted_sources ORDER BY item_id')]
    db.execute(SUBMITTED_SCHEMA_V26.replace('CREATE TABLE submitted_sources',
                                            'CREATE TABLE submitted_sources_v26', 1))
    db.execute(f'INSERT INTO submitted_sources_v26({quoted_names}) SELECT {quoted_names} FROM submitted_sources')
    for name in dependent:
        db.execute(f'DROP TRIGGER {_quoted(name)}')
    db.execute('DROP TABLE submitted_sources')
    db.execute('ALTER TABLE submitted_sources_v26 RENAME TO submitted_sources')
    for sql in dependent.values():
        db.execute(sql)
    for sql in LOCAL_INPUT_GUARDS:
        db.execute(sql)
    db.execute('DROP TRIGGER ingestion_events_observation_typed')
    db.execute(OBSERVATION_V26_SQL)
    db.execute(LOCAL_EVENT_INDEX)
    after = _migration_inventory(db)
    for table, prior in before.items():
        if table == 'submitted_sources':
            continue
        current_inventory = after[table]
        if table == 'ingestion_events':
            prior_indexes = {r[1:] for r in prior[4]}
            current_indexes = {r[1:] for r in current_inventory[4]}
            if (current_inventory[:4] != prior[:4] or current_indexes != prior_indexes | {
                    ('ingestion_events_local_owner', 1, 'c', 1)}):
                raise RuntimeError('local intake migration changed old events')
        elif current_inventory != prior:
            raise RuntimeError('local intake migration changed another domain')
    if old_rows != [tuple(r) for r in db.execute(f'SELECT {quoted_names} FROM submitted_sources ORDER BY item_id')]:
        raise RuntimeError('local intake migration changed old inputs')
    if db.execute("SELECT 1 FROM submitted_sources WHERE binding_scope!='legacy' LIMIT 1").fetchone():
        raise RuntimeError('local intake migration rebound old inputs')
    current = {tuple(r[:2]): tuple(r[2:]) for r in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')}
    for key, value in catalog.items():
        if key not in {('table', 'submitted_sources'), ('trigger', 'ingestion_events_observation_typed')} and current.get(key) != value:
            raise RuntimeError('local intake migration changed old DDL')
    expected_table = _stored_ddl(SUBMITTED_SCHEMA_V26).replace(
        'CREATE TABLE submitted_sources', 'CREATE TABLE "submitted_sources"', 1)
    if current[('table', 'submitted_sources')][1] != expected_table or set(current)-set(catalog) != additions:
        raise RuntimeError('local intake migration unexpected DDL')
    for statement in (*LOCAL_INPUT_GUARDS, OBSERVATION_V26_SQL):
        name = statement.split()[2]
        if current[('trigger', name)][1] != _stored_ddl(statement):
            raise RuntimeError('local intake migration unexpected guard')
    if current[('index', 'ingestion_events_local_owner')][1] != _stored_ddl(LOCAL_EVENT_INDEX):
        raise RuntimeError('local intake migration unexpected unique index')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None or db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError('local intake migration integrity failed')


# Finite source catalog from the accepted schema25 DDL; no upgrade probe
# imports or full-domain row inventory on the read path.
_SOURCE_GUARDS = {
    'ingestion_events': ('table', 'ingestion_events',
        """CREATE TABLE ingestion_events (
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
    )"""),
    'ingestion_events_subject': ('index', 'ingestion_events',
        """CREATE INDEX ingestion_events_subject ON ingestion_events(subject_kind,subject_id,event_id)"""),
    'ingestion_events_no_update': ('trigger', 'ingestion_events',
        """CREATE TRIGGER ingestion_events_no_update
            BEFORE UPDATE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END"""),
    'ingestion_events_no_delete': ('trigger', 'ingestion_events',
        """CREATE TRIGGER ingestion_events_no_delete
            BEFORE DELETE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END"""),
    'ingestion_events_proof_unavailable': ('trigger', 'ingestion_events',
        """CREATE TRIGGER ingestion_events_proof_unavailable
        BEFORE INSERT ON ingestion_events
        WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')
          AND (ingestion_proof(NEW.kind,NEW.binding_sha256,NEW.detail_json)!=1
               OR NEW.kind IS NOT json_extract(NEW.detail_json,'$.code')
               OR NEW.subject_kind IS NOT json_extract(NEW.detail_json,'$.manifest.subject_kind')
               OR NEW.subject_id IS NOT json_extract(NEW.detail_json,'$.manifest.subject_id')
               OR NEW.item_id IS NOT json_extract(NEW.detail_json,'$.manifest.owner_item_id')
               OR NEW.binding_sha256 IS NOT json_extract(NEW.detail_json,'$.final_binding_sha256'))
        BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END"""),
    'distill_items_ingestion_binding_immutable': ('trigger', 'distill_items',
        """CREATE TRIGGER distill_items_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON distill_items
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END"""),
    'distill_items_ingestion_binding_required': ('trigger', 'distill_items',
        """CREATE TRIGGER distill_items_ingestion_binding_required
            BEFORE INSERT ON distill_items WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END"""),
    'distill_items_ingestion_owner_immutable': ('trigger', 'distill_items',
        """CREATE TRIGGER distill_items_ingestion_owner_immutable
        BEFORE UPDATE OF material_id ON distill_items
        WHEN OLD.ingestion_contract!='legacy' AND OLD.material_id IS NOT NULL
          AND NEW.material_id IS NOT OLD.material_id
        BEGIN SELECT RAISE(ABORT,'ingestion owner is immutable'); END"""),
    'distill_items_ingestion_no_delete': ('trigger', 'distill_items',
        """CREATE TRIGGER distill_items_ingestion_no_delete
        BEFORE DELETE ON distill_items WHEN OLD.ingestion_contract!='legacy'
        BEGIN SELECT RAISE(ABORT,'ingestion owner is durable'); END"""),
    'source_media_ingestion_no_update': ('trigger', 'source_media',
        """CREATE TRIGGER source_media_ingestion_no_update
            BEFORE UPDATE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
              AND ingestion_release(OLD.material_id,OLD.member_id,OLD.sha256,NEW.content)!=1
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END"""),
    'source_media_ingestion_no_delete': ('trigger', 'source_media',
        """CREATE TRIGGER source_media_ingestion_no_delete
            BEFORE DELETE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END"""),
    'source_media_ingestion_capture_binding': ('trigger', 'capture_state',
        """CREATE TRIGGER source_media_ingestion_capture_binding
        BEFORE UPDATE OF item_id,audio_path ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND (NEW.item_id IS NOT OLD.item_id
            OR (OLD.audio_path IS NOT NULL AND NEW.audio_path IS NOT OLD.audio_path))
        BEGIN SELECT RAISE(ABORT,'ingestion capture binding is immutable'); END"""),
    'source_media_ingestion_capture_release': ('trigger', 'capture_state',
        """CREATE TRIGGER source_media_ingestion_capture_release
        BEFORE UPDATE OF audio_released_at ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND NEW.audio_released_at IS NOT OLD.audio_released_at
          AND ingestion_release('capture',OLD.capture_id,OLD.audio_path,NEW.audio_released_at)!=1
        BEGIN SELECT RAISE(ABORT,'ingestion audio is retained'); END"""),
    'source_media_ingestion_capture_no_delete': ('trigger', 'capture_state',
        """CREATE TRIGGER source_media_ingestion_capture_no_delete
        BEFORE DELETE ON capture_state WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1'))
        BEGIN SELECT RAISE(ABORT,'ingestion capture owner is durable'); END"""),
}
