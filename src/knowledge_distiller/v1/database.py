from __future__ import annotations

import sqlite3
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 27
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
    connection.create_function('wiki_outcome_accept', 9, lambda *_: 0)
    connection.create_function('source_identity_append', 2, lambda *_: 0)
    try:
        yield connection
        if connection.in_transaction:
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
        _schema27_preflight(connection, version)
        if version == 27:
            _schema27_integrity(connection)
            return
        if version < 27:
            # A parent-table rebuild preserves all ids. Disable enforcement only
            # for this migration connection; check every FK before atomic commit.
            connection.execute("PRAGMA foreign_keys = OFF")
            if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
                raise RuntimeError('schema27 cannot disable foreign keys')
            if version:
                connection.execute('BEGIN IMMEDIATE')
                if connection.execute('PRAGMA user_version').fetchone()[0] != version:
                    raise RuntimeError('schema27 starting version changed')
                _schema27_preflight(connection, version)
        if version == 0:
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + SUBMITTED_SCHEMA)
            connection.execute("PRAGMA user_version = 5")
        elif version == 4:
            connection.execute(SUBMITTED_SCHEMA.replace("submitted_sources (", "submitted_sources_v5 (", 1))
            connection.execute("INSERT INTO submitted_sources_v5 SELECT * FROM submitted_sources")
            connection.execute("DROP TABLE submitted_sources")
            connection.execute("ALTER TABLE submitted_sources_v5 RENAME TO submitted_sources")
            connection.execute("PRAGMA user_version = 5")
        elif version in (1, 2, 3):
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
            connection.execute("PRAGMA user_version = 5")
        elif version not in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26):
            raise RuntimeError(f"unsupported database version: {version}")

        if version < 6:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            for statement in TOPIC_STATEMENTS:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 6")

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
            connection.execute("PRAGMA user_version = 7")

        if version < 8:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE distill_items ADD COLUMN platform_authority_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("PRAGMA user_version = 8")

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
            connection.execute("PRAGMA user_version = 9")

        if version < 10:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .source_versions import migrate_material_snapshots
            migrate_material_snapshots(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("database migration found broken source references")
            connection.execute("PRAGMA user_version = 10")

        if version < 11:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .collection_schema import STATEMENTS
            for statement in STATEMENTS:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 11")

        if version < 12:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(distill_items)')}
            if 'rejection_reason' not in columns:
                connection.execute('ALTER TABLE distill_items ADD COLUMN rejection_reason TEXT')
            connection.execute("PRAGMA user_version = 12")

        if version < 13:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(distill_items)')}
            if 'dismissed_at' not in columns:
                connection.execute('ALTER TABLE distill_items ADD COLUMN dismissed_at TEXT')
            connection.execute("PRAGMA user_version = 13")

        if version < 14:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(distill_items)')}
            if 'submitted_title' not in columns:
                connection.execute("ALTER TABLE distill_items ADD COLUMN submitted_title TEXT NOT NULL DEFAULT ''")
            connection.execute("PRAGMA user_version = 14")

        if version < 15:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .feishu_schema import STATEMENTS
            for statement in STATEMENTS:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 15")

        if version < 16:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .media_lifecycle import migrate
            migrate(connection)
            connection.execute("PRAGMA user_version = 16")

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
            connection.execute("PRAGMA user_version = 17")

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
            connection.execute("PRAGMA user_version = 18")

        if version < 19:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .confirmation_schema import migrate
            migrate(connection)
            connection.execute("PRAGMA user_version = 19")

        if version < 20:
            # raw/ index (docs/engineering/raw-interface.md). A V1.3 binary cannot
            # open schema 20; that rule is unchanged from earlier upgrades.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            for statement in RAW_STATEMENTS:
                connection.execute(statement)
            from .media_lifecycle import require_raw
            require_raw(connection)
            connection.execute("PRAGMA user_version = 20")

        if version < 21:
            # Feishu quick notes: append-only captures, transcripts, adjacency and
            # identity events (docs/roadmap/handoff-feishu-capture.md).
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .capture_schema import STATEMENTS as CAPTURE_STATEMENTS
            for statement in CAPTURE_STATEMENTS:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 21")

        if version < 22:
            # V3 wiki maintenance is a separate durable domain. It does not
            # reuse or rewrite the legacy organization event tables.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            from .wiki_schema import migrate
            migrate(connection)
            connection.execute("PRAGMA user_version = 22")

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
            connection.execute("PRAGMA user_version = 23")

        if version < 24:
            if not connection.in_transaction:
                connection.execute('BEGIN IMMEDIATE')
            migrate_v24(connection)
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RuntimeError('database migration found broken ingestion references')
            connection.execute('PRAGMA user_version = 24')

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
        if version in range(1, 21):
            # The schema27 freeze covers fresh and actual21--26 only. Preserve
            # the pre-existing 1--20 route and its26 endpoint; certifying those
            # historical DDL/column-order variants for27 needs separate proof.
            connection.commit()
            connection.execute('PRAGMA foreign_keys = ON')
            if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise RuntimeError('local intake migration committed; FK re-enable failed')
            return
        if not connection.in_transaction:
            connection.execute('BEGIN IMMEDIATE')
        migrate_v27(connection)
        connection.execute('PRAGMA user_version = 27')
        _schema27_integrity(connection)
        try:
            connection.commit()
        except Exception as exc:
            if not connection.in_transaction and connection.execute('PRAGMA user_version').fetchone()[0] == 27:
                raise RuntimeError('schema27 committed; commit acknowledgement failed') from exc
            raise
        try:
            _schema27_enable_foreign_keys(connection)
            _schema27_integrity(connection)
        except Exception as exc:
            raise RuntimeError('schema27 committed; postcommit verification failed') from exc


def _schema27_expected(version):
    from .wiki_schema import SCHEMA26_WIKI_DDL, SCHEMA27_WIKI_DDL
    ddl = {**_SCHEMA26_BASE_DDL, **SCHEMA26_WIKI_DDL}
    tables = dict(_SCHEMA26_TABLE_ABI)
    indexes = dict(_SCHEMA26_INDEX_ABI)
    if version in (21, 22, 23, 25):
        delta = _SCHEMA27_LEGACY_DELTAS[version]
        for name in delta['remove']:
            ddl.pop(name)
        ddl.update(delta['ddl'])
        for name in delta['abi_remove']:
            tables.pop(name)
        tables.update(delta['abi'])
        for name in delta['index_remove']:
            indexes.pop(name)
        indexes.update(delta['index'])
    elif version == 24:
        ddl, tables, indexes = _schema27_expected(25)
        parent = ddl['distill_items'][2]
        terminal = ", CHECK(state!='raw_saved' OR (phase='done' AND ingestion_contract='raw-verified-v1')))"
        check = "state IN ('queued','working','waiting_user','succeeded','failed','raw_saved')"
        # Use the independently frozen pre25 parent to restore the exact CHECK,
        # not guessed indentation or an actual input database's DDL.
        prior = _SCHEMA27_LEGACY_DELTAS[23]['ddl']['distill_items'][2]
        import re as _re
        match = _re.search(r"state IN \(\s*'queued',\s*'working',\s*'waiting_user',\s*'succeeded',\s*'failed'\s*\)", prior)
        if parent.count(terminal) != 1 or parent.count(check) != 1 or match is None:
            raise RuntimeError('schema27 historical24 literal invalid')
        parent = parent.replace(terminal, ')', 1).replace(check, match[0], 1)
        ddl['distill_items'] = ('table', 'distill_items', parent)
        for statement in RAW_TERMINAL_TRIGGERS:
            ddl.pop(statement.split()[2])
    elif version == 27:
        ddl.update(SCHEMA27_WIKI_DDL)
        ddl.update(_SCHEMA27_IDENTITY_DDL)
        tables.update(_SCHEMA27_TABLE_DELTA)
        for name in _SCHEMA27_INDEX_REMOVE:
            indexes.pop(name)
        indexes.update(_SCHEMA27_INDEX_DELTA)
    elif version != 26:
        raise RuntimeError(f'unsupported frozen migration source: {version}')
    for name, (table, _, origin, _, _) in indexes.items():
        if origin != 'c':
            ddl[name] = ('index', table, None)
    return ddl, tables, indexes


def _schema27_catalog(db):
    rows = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema').fetchall()
    result = {r[1]: (r[0], r[2], r[3]) for r in rows}
    if len(result) != len(rows):
        raise RuntimeError('schema27 duplicate catalog name')
    return result


def _schema27_check_catalog(db, version):
    ddl, tables, indexes = _schema27_expected(version)
    actual = _schema27_catalog(db)
    if actual.keys() != ddl.keys():
        extra = sorted(actual.keys() - ddl.keys())
        missing = sorted(ddl.keys() - actual.keys())
        raise RuntimeError(f'schema27 catalog closure: extra={extra!r}; missing={missing!r}')
    for name, (kind, table, sql) in ddl.items():
        current = actual[name]
        variants = {sql}
        if kind == 'table' and sql is not None:
            # Only the fixed final table-name spelling introduced by RENAME.
            for spelling in (f'CREATE TABLE {name} (', f'CREATE TABLE "{name}" ('):
                if sql.startswith(spelling):
                    variants.add(sql.replace(spelling, f'CREATE TABLE {name} (', 1))
                    variants.add(sql.replace(spelling, f'CREATE TABLE "{name}" (', 1))
        if current[:2] != (kind, table) or current[2] not in variants:
            raise RuntimeError(f'schema27 object DDL mismatch: {name}')
    found_indexes = {}
    for name, (columns, fks) in tables.items():
        if tuple(tuple(r) for r in db.execute(f'PRAGMA table_xinfo({_quoted(name)})')) != columns:
            raise RuntimeError(f'schema27 column ABI mismatch: {name}')
        if tuple(tuple(r) for r in db.execute(f'PRAGMA foreign_key_list({_quoted(name)})')) != fks:
            raise RuntimeError(f'schema27 FK ABI mismatch: {name}')
        for row in db.execute(f'PRAGMA index_list({_quoted(name)})'):
            index = row[1]
            if index in found_indexes:
                raise RuntimeError('schema27 duplicate index ABI')
            xinfo = tuple(tuple(r) for r in db.execute(f'PRAGMA index_xinfo({_quoted(index)})'))
            found_indexes[index] = (name, row[2], row[3], row[4], xinfo)
    if found_indexes != indexes:
        differing = sorted(k for k in found_indexes.keys() | indexes.keys()
                           if found_indexes.get(k) != indexes.get(k))
        raise RuntimeError(f'schema27 index ABI mismatch: {differing!r}')


def _schema27_preflight(db, version):
    if version in range(1, 21):
        # Do not silently tighten or widen the original historical initializer.
        # This lane stops at26 under its original migration admission rules.
        return
    if version == 0:
        if _schema27_catalog(db):
            raise RuntimeError('schema27 nonempty version-zero database')
        return
    _schema27_check_catalog(db, version)
    _schema27_integrity(db)


def _schema27_integrity(db):
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise RuntimeError('schema27 broken foreign keys')
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError('schema27 integrity check failed')


def _schema27_enable_foreign_keys(db):
    db.execute('PRAGMA foreign_keys = ON')
    if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise RuntimeError('schema27 foreign key enable failed')


def _schema27_rows(db, tables):
    """All old values, storage classes and rowids; includes sqlite_sequence."""
    result = {}
    for name, (columns, _) in tables.items():
        names = ','.join(_quoted(c[1]) for c in columns)
        types = ','.join(f'typeof({_quoted(c[1])})' for c in columns)
        digest = hashlib.sha256(b'schema27-typed-rows-v1\0')
        count = 0
        for row in db.execute(f'SELECT rowid,{names},{types} FROM {_quoted(name)} ORDER BY rowid'):
            payload = repr(tuple(row)).encode('utf-8')
            digest.update(len(payload).to_bytes(8, 'big'))
            digest.update(payload)
            count += 1
        result[name] = (count, digest.hexdigest())
    return result


def _schema27_check_preservation(db, before, catalog, tables):
    if _schema27_rows(db, tables) != before:
        raise RuntimeError('schema27 changed old rowid, values or storage types')
    current = _schema27_catalog(db)
    replaced = {'wiki_tasks', 'wiki_task_outcome_binding_required',
                'wiki_outcome_receipts_publish_unavailable'}
    removed = {'sqlite_autoindex_wiki_tasks_2'}
    for name, value in catalog.items():
        if name not in replaced | removed and current.get(name) != value:
            raise RuntimeError(f'schema27 changed old object: {name}')
    from .wiki_schema import SCHEMA26_WIKI_DDL, SCHEMA27_WIKI_DDL
    new = (SCHEMA27_WIKI_DDL.keys() - SCHEMA26_WIKI_DDL.keys()) | _SCHEMA27_IDENTITY_DDL.keys()
    new |= {'sqlite_autoindex_source_identity_events_1'}
    if current.keys() - catalog.keys() != new or catalog.keys() - current.keys() != removed:
        raise RuntimeError('schema27 unexpected object delta')
    if db.execute('SELECT 1 FROM source_identity_events LIMIT 1').fetchone() is not None:
        raise RuntimeError('schema27 invented identity evidence')
    _schema27_check_catalog(db, 27)
    _schema27_integrity(db)


def migrate_v27(db):
    """Only the frozen26 catalog may enter the unified finite migration."""
    if not db.in_transaction or db.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
        raise RuntimeError('schema27 migration connection required')
    if db.execute('PRAGMA user_version').fetchone()[0] != 26:
        raise RuntimeError('schema27 migration requires version26')
    _schema27_preflight(db, 26)
    _, tables, _ = _schema27_expected(26)
    before = _schema27_rows(db, tables)
    catalog = _schema27_catalog(db)
    from .wiki_schema import migrate_v27 as migrate_wiki_v27
    migrate_wiki_v27(db)
    for _, _, sql in _SCHEMA27_IDENTITY_DDL.values():
        db.execute(sql)
    _schema27_check_preservation(db, before, catalog, tables)


def _wiki_execution_schema_inventory(db):
    """Catalog qualification only: never grants publication authority."""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != 27:
        raise ValueError('wiki_execution_schema_unsupported')
    try:
        _schema27_check_catalog(db, 27)
    except RuntimeError as exc:
        raise ValueError('wiki_execution_schema_unsupported') from exc
    return version


def _source_identity_schema_inventory(db):
    """Fixed identity and all primary material/review dependency definitions."""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != 27:
        raise ValueError('source_identity_schema_unsupported')
    try:
        _schema27_check_catalog(db, 27)
    except RuntimeError as exc:
        raise ValueError('source_identity_schema_unsupported') from exc
    return version


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
    if version not in (25, 26, 27):
        raise ValueError('source_schema_unsupported')
    catalog = {r[1]: (r[0], r[2], r[3]) for r in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema')}
    expected = dict(_SOURCE_GUARDS)
    for statement in RAW_STATEMENTS:
        sql = _source_ddl(statement)
        kind, name = sql.split()[1:3]
        parent = 'raw_records' if name != 'raw_counters' else name
        expected[name] = (kind.lower(), parent, sql)
    submitted = SUBMITTED_SCHEMA_V26 if version in (26, 27) else SUBMITTED_SCHEMA.replace("'epub'", "'epub', 'image'")
    expected['submitted_sources'] = ('table', 'submitted_sources', _source_ddl(submitted))
    for statement in (*OLD_INPUT_GUARDS, *(LOCAL_INPUT_GUARDS if version in (26, 27) else ())):
        sql = _source_ddl(statement)
        expected[sql.split()[2]] = ('trigger', 'submitted_sources', sql)
    observation = OBSERVATION_V26_SQL if version in (26, 27) else OBSERVATION_V24_SQL
    expected['ingestion_events_observation_typed'] = ('trigger', 'ingestion_events', _source_ddl(observation))
    if version in (26, 27):
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
    for table, wanted in (('submitted_sources', ('input_kind', 'input_key', 'binding_scope') if version in (26, 27) else ('input_kind', 'input_key')),
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
    if version == 27:
        _wiki_execution_schema_inventory(db)
        _source_identity_schema_inventory(db)
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


# Finite full catalog from historical primary 58bc8ee, genuine 21/22/23
# fixtures and exact 24/25/26 primary migrations. No live-schema adoption.
_SCHEMA26_BASE_DDL = {
    'accepted_insight_versions': ('table', 'accepted_insight_versions', """CREATE TABLE accepted_insight_versions (
    insight_version_id INTEGER NOT NULL PRIMARY KEY,
    insight_id INTEGER NOT NULL,
    judgment_id INTEGER NOT NULL UNIQUE,
    judgment_decision TEXT NOT NULL CHECK (judgment_decision IN ('interesting','rethink')),
    reconsideration_id INTEGER,
    initial_role TEXT NOT NULL CHECK (initial_role IN ('current', 'historical')),
    current_role TEXT NOT NULL CHECK (current_role IN ('current', 'historical')),
    accepted_at TEXT NOT NULL CHECK (TRIM(accepted_at) != ''),
    historical_at TEXT,
    historical_reason TEXT CHECK (
        historical_reason IS NULL OR historical_reason IN (
            'newer_accepted_current', 'born_older_than_current',
            'born_after_newer_ever_current', 'basis_invalid',
            'refuted', 'identity_replaced'
        )
    ),
    caused_by_event_id INTEGER,
    caused_by_judgment_id INTEGER,
    replacement_insight_id INTEGER,
    disqualification_reason TEXT CHECK (
        disqualification_reason IS NULL
        OR disqualification_reason IN ('basis_invalid', 'refuted')
    ),
    CHECK (initial_role != 'historical' OR current_role = 'historical'),
    CHECK (
        (current_role = 'current'
          AND historical_at IS NULL AND historical_reason IS NULL
          AND caused_by_event_id IS NULL AND caused_by_judgment_id IS NULL
          AND replacement_insight_id IS NULL
          AND disqualification_reason IS NULL)
        OR
        (current_role = 'historical'
          AND historical_at IS NOT NULL
          AND historical_reason IS NOT NULL
          AND (
            (historical_reason IN (
                'newer_accepted_current', 'born_older_than_current',
                'born_after_newer_ever_current'
             )
             AND (
               (historical_reason = 'newer_accepted_current'
                AND initial_role = 'current')
               OR
               (historical_reason IN (
                  'born_older_than_current',
                  'born_after_newer_ever_current'
                ) AND initial_role = 'historical')
             )
             AND caused_by_event_id IS NULL
             AND caused_by_judgment_id IS NOT NULL
             AND replacement_insight_id IS NULL
             AND disqualification_reason IS NULL)
            OR
            (historical_reason IN ('basis_invalid', 'refuted')
             AND caused_by_event_id IS NOT NULL
             AND caused_by_judgment_id IS NULL
             AND replacement_insight_id IS NULL
             AND disqualification_reason = historical_reason)
            OR
            (historical_reason = 'identity_replaced'
             AND caused_by_event_id IS NOT NULL
             AND caused_by_judgment_id IS NULL
             AND replacement_insight_id IS NOT NULL
             AND replacement_insight_id != insight_id
             AND disqualification_reason IS NULL)
          ))
    ),
    CHECK ((judgment_decision='interesting' AND reconsideration_id IS NULL)
        OR (judgment_decision='rethink' AND reconsideration_id IS NOT NULL)),
    FOREIGN KEY(insight_version_id,judgment_id,reconsideration_id)
        REFERENCES insight_reconsiderations(insight_version_id,judgment_id,reconsideration_id),
    UNIQUE (insight_version_id, judgment_id),
    FOREIGN KEY (insight_id, insight_version_id)
      REFERENCES insight_versions(insight_id, insight_version_id),
    FOREIGN KEY (insight_version_id, judgment_id, judgment_decision)
      REFERENCES user_insight_judgments(
        insight_version_id, judgment_id, decision
      ),
    FOREIGN KEY (insight_id, caused_by_judgment_id)
      REFERENCES user_insight_judgments(insight_id, judgment_id),
    FOREIGN KEY (
      insight_version_id, caused_by_event_id, disqualification_reason
    ) REFERENCES insight_version_disqualifications(
      insight_version_id, event_id, fact_kind
    ),
    FOREIGN KEY (insight_id, replacement_insight_id, caused_by_event_id)
      REFERENCES insight_identity_replacements(
        replaced_insight_id, replacement_insight_id, event_id
      )
)"""),
    'capture_identity_events': ('table', 'capture_identity_events', """CREATE TABLE capture_identity_events (
        event_id INTEGER PRIMARY KEY,
        capture_id INTEGER NOT NULL REFERENCES captures(capture_id),
        result TEXT NOT NULL CHECK (result IN ('my_thought', 'third_party', 'annotation', 'pending')),
        basis TEXT NOT NULL, confidence REAL, target_message_id TEXT,
        created_at TEXT NOT NULL
    )"""),
    'capture_state': ('table', 'capture_state', """CREATE TABLE capture_state (
        capture_id INTEGER PRIMARY KEY REFERENCES captures(capture_id),
        item_id INTEGER REFERENCES distill_items(item_id),
        audio_path TEXT, audio_released_at TEXT
    )"""),
    'capture_transcripts': ('table', 'capture_transcripts', """CREATE TABLE capture_transcripts (
        capture_id INTEGER PRIMARY KEY REFERENCES captures(capture_id),
        text TEXT NOT NULL, engine TEXT NOT NULL, model TEXT NOT NULL, version TEXT NOT NULL,
        chunks_json TEXT NOT NULL, created_at TEXT NOT NULL
    )"""),
    'captures': ('table', 'captures', """CREATE TABLE captures (
        capture_id INTEGER PRIMARY KEY,
        app_id TEXT NOT NULL, message_id TEXT NOT NULL,
        message_type TEXT NOT NULL CHECK (message_type IN ('text', 'audio')),
        created_ms INTEGER NOT NULL CHECK (created_ms >= 0),
        received_ms INTEGER NOT NULL CHECK (received_ms >= 0),
        text TEXT, file_key TEXT, duration_ms INTEGER,
        raw_id TEXT NOT NULL UNIQUE,
        CHECK ((message_type = 'text' AND text IS NOT NULL AND file_key IS NULL)
            OR (message_type = 'audio' AND text IS NULL AND file_key IS NOT NULL)),
        UNIQUE (app_id, message_id)
    )"""),
    'collection_confirmations': ('table', 'collection_confirmations', """CREATE TABLE collection_confirmations (
        token TEXT NOT NULL, ordinal INTEGER NOT NULL, expected_count INTEGER NOT NULL,
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        PRIMARY KEY(token,ordinal)
    )"""),
    'collection_events': ('table', 'collection_events', """CREATE TABLE collection_events (
        event_id INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        kind TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
    )"""),
    'collection_members': ('table', 'collection_members', """CREATE TABLE collection_members (
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        ordinal INTEGER NOT NULL,
        native_id TEXT NOT NULL, native_version TEXT NOT NULL,
        item_id INTEGER NOT NULL UNIQUE REFERENCES distill_items(item_id),
        known_unsupported INTEGER NOT NULL CHECK(known_unsupported IN (0,1)),
        source_fact_id INTEGER REFERENCES source_facts(source_fact_id),
        knowledge_result_id INTEGER REFERENCES knowledge_results(knowledge_result_id),
        PRIMARY KEY(operation_id,ordinal), UNIQUE(operation_id,native_id)
    )"""),
    'collection_operations': ('table', 'collection_operations', """CREATE TABLE collection_operations (
        operation_id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL, source_key TEXT NOT NULL, title TEXT NOT NULL,
        manifest_json TEXT NOT NULL, signature TEXT NOT NULL, content_signature TEXT NOT NULL,
        authority_json TEXT NOT NULL, confirmation_token TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('queued','working','waiting_user','partial','failed','succeeded','cancelled')),
        consequence TEXT CHECK(consequence IN ('complete','partial','failed')),
        error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1,
        queued_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1')), source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*')), relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*')),
        UNIQUE(kind,source_key,signature,content_signature)
    )"""),
    'collection_previews': ('table', 'collection_previews', """CREATE TABLE collection_previews (
        token TEXT PRIMARY KEY, preview_json TEXT NOT NULL,
        expires_at REAL
    )"""),
    'collection_results': ('table', 'collection_results', """CREATE TABLE collection_results (
        result_id INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL UNIQUE REFERENCES collection_operations(operation_id),
        payload_json TEXT NOT NULL, lineage_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )"""),
    'confirmation_decisions': ('table', 'confirmation_decisions', """CREATE TABLE confirmation_decisions (
                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
                revision TEXT NOT NULL, action TEXT NOT NULL, value TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(item_id, revision))"""),
    'delivery_adjacency': ('table', 'delivery_adjacency', """CREATE TABLE delivery_adjacency (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, earlier_message_id TEXT NOT NULL,
        gap_seconds INTEGER NOT NULL CHECK (gap_seconds >= 0),
        PRIMARY KEY (app_id, message_id, earlier_message_id)
    )"""),
    'distill_items': ('table', 'distill_items', """CREATE TABLE "distill_items" (
    item_id INTEGER PRIMARY KEY,
    submitted_url TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued','working','waiting_user','succeeded','failed','raw_saved')
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
, platform_authority_json TEXT NOT NULL DEFAULT '{}', submitted_title TEXT NOT NULL DEFAULT '', review_revision INTEGER NOT NULL DEFAULT 0, ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1')), source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*')), relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*')), CHECK(state!='raw_saved' OR (phase='done' AND ingestion_contract='raw-verified-v1')))"""),
    'feishu_action_queue': ('table', 'feishu_action_queue', """CREATE TABLE feishu_action_queue (
                id INTEGER PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
                app_id TEXT NOT NULL, message_id TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT)"""),
    'feishu_binding': ('table', 'feishu_binding', """CREATE TABLE feishu_binding (
        app_id TEXT PRIMARY KEY, bot_open_id TEXT NOT NULL,
        user_open_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
        history_until_ms INTEGER NOT NULL CHECK(history_until_ms >= start_ms)
    )"""),
    'feishu_parts': ('table', 'feishu_parts', """CREATE TABLE feishu_parts (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, position INTEGER NOT NULL,
        item_id INTEGER REFERENCES distill_items(item_id),
        error TEXT, preview_json TEXT,
        PRIMARY KEY(app_id,message_id,position),
        FOREIGN KEY(app_id,message_id) REFERENCES feishu_receipts(app_id,message_id)
    )"""),
    'feishu_receipts': ('table', 'feishu_receipts', """CREATE TABLE feishu_receipts (
        app_id TEXT NOT NULL REFERENCES feishu_binding(app_id),
        message_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
        raw_json TEXT NOT NULL, text TEXT NOT NULL,
        same_topic INTEGER NOT NULL CHECK(same_topic IN (0,1)),
        content_kind TEXT CHECK(content_kind IN ('text','links')),
        state TEXT NOT NULL CHECK(state IN ('received','waiting_input','accepted','rejected','needs_desktop')),
        error TEXT, card_id TEXT, card_signature TEXT, card_attempted_ms INTEGER,
        PRIMARY KEY(app_id,message_id)
    )"""),
    'group_decisions': ('table', 'group_decisions', """CREATE TABLE group_decisions (
        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
        request_id TEXT NOT NULL, group_id TEXT NOT NULL,
        submitted_revision TEXT NOT NULL, selection_digest TEXT NOT NULL,
        payload_digest TEXT NOT NULL, result_json TEXT NOT NULL,
        audit_json TEXT NOT NULL, committed_at TEXT NOT NULL,
        PRIMARY KEY(item_id,request_id),
        UNIQUE(item_id,submitted_revision,selection_digest))"""),
    'insight_identities': ('table', 'insight_identities', """CREATE TABLE insight_identities (
    insight_id INTEGER NOT NULL PRIMARY KEY,
    created_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    FOREIGN KEY (created_event_id) REFERENCES organization_events(event_id)
)"""),
    'insight_identity_replacements': ('table', 'insight_identity_replacements', """CREATE TABLE insight_identity_replacements (
    replaced_insight_id INTEGER NOT NULL PRIMARY KEY,
    replacement_insight_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    reason_text TEXT NOT NULL CHECK (TRIM(reason_text) != ''),
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    CHECK (replaced_insight_id != replacement_insight_id),
    UNIQUE (replaced_insight_id, replacement_insight_id, event_id),
    FOREIGN KEY (replaced_insight_id) REFERENCES insight_identities(insight_id),
    FOREIGN KEY (replacement_insight_id) REFERENCES insight_identities(insight_id),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id)
)"""),
    'insight_reconsiderations': ('table', 'insight_reconsiderations', """CREATE TABLE insight_reconsiderations (
    reconsideration_id INTEGER PRIMARY KEY,
    insight_version_id INTEGER NOT NULL UNIQUE REFERENCES insight_versions(insight_version_id),
    judgment_id INTEGER NOT NULL UNIQUE REFERENCES user_insight_judgments(judgment_id),
    operation_id TEXT NOT NULL UNIQUE CHECK(TRIM(operation_id) != ''),
    decision TEXT NOT NULL CHECK(decision='interesting_after_rethink'),
    reconsidered_at TEXT NOT NULL,
    UNIQUE(insight_version_id,judgment_id,reconsideration_id)
)"""),
    'insight_version_disqualifications': ('table', 'insight_version_disqualifications', """CREATE TABLE insight_version_disqualifications (
    disqualification_id INTEGER NOT NULL PRIMARY KEY,
    insight_version_id INTEGER NOT NULL,
    fact_kind TEXT NOT NULL CHECK (fact_kind IN ('basis_invalid', 'refuted')),
    event_id INTEGER NOT NULL,
    reason_text TEXT NOT NULL CHECK (TRIM(reason_text) != ''),
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    UNIQUE (insight_version_id, fact_kind),
    UNIQUE (insight_version_id, event_id, fact_kind),
    FOREIGN KEY (insight_version_id) REFERENCES insight_versions(insight_version_id),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id)
)"""),
    'insight_version_participants': ('table', 'insight_version_participants', """CREATE TABLE insight_version_participants (
    insight_version_id INTEGER NOT NULL,
    participant_key TEXT NOT NULL CHECK (TRIM(participant_key) != ''),
    input_kind TEXT NOT NULL CHECK (input_kind IN ('source_knowledge', 'accepted_insight')),
    knowledge_result_id INTEGER,
    point_id TEXT,
    accepted_insight_version_id INTEGER,
    position INTEGER NOT NULL CHECK (position >= 0),
    contribution_text TEXT NOT NULL CHECK (TRIM(contribution_text) != ''),
    PRIMARY KEY (insight_version_id, participant_key),
    UNIQUE (insight_version_id, position),
    CHECK (
        (input_kind = 'source_knowledge'
          AND knowledge_result_id IS NOT NULL
          AND point_id IS NOT NULL AND TRIM(point_id) != ''
          AND accepted_insight_version_id IS NULL)
        OR
        (input_kind = 'accepted_insight'
          AND knowledge_result_id IS NULL AND point_id IS NULL
          AND accepted_insight_version_id IS NOT NULL)
    ),
    FOREIGN KEY (insight_version_id) REFERENCES insight_versions(insight_version_id),
    FOREIGN KEY (knowledge_result_id) REFERENCES knowledge_results(knowledge_result_id),
    FOREIGN KEY (accepted_insight_version_id)
      REFERENCES accepted_insight_versions(insight_version_id)
)"""),
    'insight_version_used_relations': ('table', 'insight_version_used_relations', """CREATE TABLE insight_version_used_relations (
    insight_version_id INTEGER NOT NULL,
    relation_version_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    role_text TEXT NOT NULL CHECK (TRIM(role_text) != ''),
    PRIMARY KEY (insight_version_id, relation_version_id),
    UNIQUE (insight_version_id, position),
    FOREIGN KEY (insight_version_id) REFERENCES insight_versions(insight_version_id),
    FOREIGN KEY (relation_version_id) REFERENCES relation_versions(relation_version_id)
)"""),
    'insight_versions': ('table', 'insight_versions', """CREATE TABLE insight_versions (
    insight_version_id INTEGER NOT NULL PRIMARY KEY,
    insight_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    previous_version_id INTEGER,
    produced_event_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK (TRIM(payload_json) != ''),
    semantic_signature TEXT NOT NULL CHECK (LENGTH(semantic_signature) = 64),
    dependency_signature TEXT NOT NULL CHECK (LENGTH(dependency_signature) = 64),
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    UNIQUE (insight_id, version_no),
    UNIQUE (insight_id, insight_version_id),
    UNIQUE (previous_version_id),
    UNIQUE (produced_event_id, insight_id),
    CHECK (
        (version_no = 1 AND previous_version_id IS NULL)
        OR (version_no > 1 AND previous_version_id IS NOT NULL)
    ),
    FOREIGN KEY (insight_id) REFERENCES insight_identities(insight_id),
    FOREIGN KEY (insight_id, previous_version_id)
      REFERENCES insight_versions(insight_id, insight_version_id),
    FOREIGN KEY (produced_event_id) REFERENCES organization_events(event_id)
)"""),
    'knowledge_results': ('table', 'knowledge_results', """CREATE TABLE knowledge_results (
    knowledge_result_id INTEGER PRIMARY KEY,
    source_fact_id INTEGER NOT NULL UNIQUE REFERENCES source_facts(source_fact_id),
    payload_json TEXT NOT NULL,
    published_path TEXT,
    published_vault TEXT,
    published_at TEXT,
    created_at TEXT NOT NULL,
    CHECK ((published_path IS NULL) = (published_at IS NULL))
)"""),
    'manual_cards': ('table', 'manual_cards', """CREATE TABLE manual_cards (
        enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
        review_round_id TEXT NOT NULL, group_id TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','suspended','resolved','superseded')),
        ordering_basis TEXT NOT NULL CHECK(ordering_basis IN ('observed','migration_inferred')),
        ordering_reason TEXT NOT NULL, entered_at TEXT NOT NULL,
        mapping_json TEXT NOT NULL,
        UNIQUE(item_id,review_round_id,group_id))"""),
    'materials': ('table', 'materials', """CREATE TABLE "materials" (
        material_id INTEGER PRIMARY KEY,
        source_kind TEXT NOT NULL, source_key TEXT NOT NULL,
        submitted_url TEXT NOT NULL, canonical_url TEXT NOT NULL,
        metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
        snapshot_key TEXT NOT NULL DEFAULT 'legacy',
        UNIQUE (source_kind, source_key, snapshot_key)
    )"""),
    'media_lifecycle': ('table', 'media_lifecycle', """CREATE TABLE media_lifecycle (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        legacy_material_id INTEGER NOT NULL,
        released_bytes INTEGER NOT NULL DEFAULT 0,
        compacted_bytes INTEGER NOT NULL DEFAULT 0)"""),
    'organization_event_accepted_boundary': ('table', 'organization_event_accepted_boundary', """CREATE TABLE organization_event_accepted_boundary (
    event_id INTEGER NOT NULL,
    insight_version_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    qualification_signature TEXT NOT NULL CHECK (LENGTH(qualification_signature) = 64),
    PRIMARY KEY (event_id, insight_version_id),
    UNIQUE (event_id, position),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id),
    FOREIGN KEY (insight_version_id)
      REFERENCES accepted_insight_versions(insight_version_id)
)"""),
    'organization_event_coverages': ('table', 'organization_event_coverages', """CREATE TABLE organization_event_coverages (
    event_id INTEGER NOT NULL,
    knowledge_result_id INTEGER NOT NULL,
    source_fact_id INTEGER NOT NULL,
    covered_at TEXT NOT NULL CHECK (TRIM(covered_at) != ''),
    PRIMARY KEY (event_id, knowledge_result_id),
    UNIQUE (knowledge_result_id),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id),
    FOREIGN KEY (knowledge_result_id, source_fact_id)
      REFERENCES knowledge_results(knowledge_result_id, source_fact_id)
)"""),
    'organization_event_relation_boundary': ('table', 'organization_event_relation_boundary', """CREATE TABLE organization_event_relation_boundary (
    event_id INTEGER NOT NULL,
    relation_version_id INTEGER NOT NULL,
    boundary_role TEXT NOT NULL CHECK (
        boundary_role IN ('current_input', 'reconsideration_hint')
    ),
    position INTEGER NOT NULL CHECK (position >= 0),
    qualification_signature TEXT NOT NULL CHECK (LENGTH(qualification_signature) = 64),
    PRIMARY KEY (event_id, relation_version_id),
    UNIQUE (event_id, position),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id),
    FOREIGN KEY (relation_version_id) REFERENCES relation_versions(relation_version_id)
)"""),
    'organization_event_source_boundary': ('table', 'organization_event_source_boundary', """CREATE TABLE organization_event_source_boundary (
    event_id INTEGER NOT NULL,
    knowledge_result_id INTEGER NOT NULL,
    source_fact_id INTEGER NOT NULL,
    boundary_role TEXT NOT NULL CHECK (boundary_role IN ('frozen_new', 'eligible_history')),
    position INTEGER NOT NULL CHECK (position >= 0),
    qualification_signature TEXT NOT NULL CHECK (LENGTH(qualification_signature) = 64),
    PRIMARY KEY (event_id, knowledge_result_id),
    UNIQUE (event_id, boundary_role, position),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id),
    FOREIGN KEY (knowledge_result_id, source_fact_id)
      REFERENCES knowledge_results(knowledge_result_id, source_fact_id)
)"""),
    'organization_events': ('table', 'organization_events', """CREATE TABLE organization_events (
    event_id INTEGER NOT NULL PRIMARY KEY,
    memory_scope INTEGER NOT NULL DEFAULT 1 CHECK (memory_scope = 1),
    status TEXT NOT NULL CHECK (status IN ('running', 'failed', 'succeeded')),
    started_at TEXT NOT NULL CHECK (TRIM(started_at) != ''),
    completed_at TEXT,
    failure_code TEXT CHECK (
        failure_code IS NULL OR failure_code IN (
            'input_read_failed', 'topic_planning_failed', 'recall_failed',
            'growth_planning_failed', 'invalid_model_output',
            'qualification_failed', 'frozen_input_ineligible',
            'dependency_changed', 'topic_baseline_changed',
            'persistence_failed'
        )
    ),
    topic_before_json TEXT NOT NULL CHECK (TRIM(topic_before_json) != ''),
    topic_before_signature TEXT NOT NULL CHECK (LENGTH(topic_before_signature) = 64),
    topic_guard_signature TEXT NOT NULL CHECK (LENGTH(topic_guard_signature) = 64),
    boundary_signature TEXT NOT NULL CHECK (LENGTH(boundary_signature) = 64),
    success_payload_json TEXT,
    CHECK (
        (status = 'running' AND completed_at IS NULL
          AND failure_code IS NULL AND success_payload_json IS NULL)
        OR
        (status = 'failed' AND completed_at IS NOT NULL
          AND failure_code IS NOT NULL AND TRIM(failure_code) != ''
          AND success_payload_json IS NULL)
        OR
        (status = 'succeeded' AND completed_at IS NOT NULL
          AND failure_code IS NULL AND success_payload_json IS NOT NULL
          AND TRIM(success_payload_json) != '')
    )
)"""),
    'personal_cognition_entries': ('table', 'personal_cognition_entries', """CREATE TABLE personal_cognition_entries (
    entry_id INTEGER PRIMARY KEY,
    insight_version_id INTEGER NOT NULL REFERENCES insight_versions(insight_version_id),
    judgment_id INTEGER NOT NULL REFERENCES user_insight_judgments(judgment_id),
    reconsideration_id INTEGER REFERENCES insight_reconsiderations(reconsideration_id),
    entry_kind TEXT NOT NULL CHECK(entry_kind IN ('new_idea','new_view')),
    text TEXT NOT NULL CHECK(TRIM(text) != ''),
    operation_id TEXT NOT NULL UNIQUE CHECK(TRIM(operation_id) != ''),
    created_at TEXT NOT NULL,
    CHECK(entry_kind != 'new_view' OR reconsideration_id IS NOT NULL)
)"""),
    'raw_counters': ('table', 'raw_counters', """CREATE TABLE raw_counters (
        day TEXT PRIMARY KEY CHECK (length(day) = 8),
        last INTEGER NOT NULL CHECK (last BETWEEN 0 AND 9999)
    )"""),
    'raw_records': ('table', 'raw_records', """CREATE TABLE raw_records (
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
    )"""),
    'relation_current': ('table', 'relation_current', """CREATE TABLE relation_current (
    relation_id INTEGER NOT NULL PRIMARY KEY,
    relation_version_id INTEGER NOT NULL UNIQUE,
    activated_at TEXT NOT NULL CHECK (TRIM(activated_at) != ''),
    FOREIGN KEY (relation_id, relation_version_id)
      REFERENCES relation_versions(relation_id, relation_version_id)
)"""),
    'relation_facts': ('table', 'relation_facts', """CREATE TABLE relation_facts (
    relation_fact_id INTEGER NOT NULL PRIMARY KEY,
    event_id INTEGER NOT NULL,
    relation_id INTEGER NOT NULL,
    relation_version_id INTEGER NOT NULL,
    fact_kind TEXT NOT NULL CHECK (fact_kind IN (
        'attention_activated', 'attention_retired', 'evolved',
        'basis_invalid', 'wrong', 'replaced'
    )),
    successor_relation_version_id INTEGER,
    replacement_relation_id INTEGER,
    reason_text TEXT NOT NULL CHECK (TRIM(reason_text) != ''),
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    CHECK (
        (fact_kind = 'evolved'
          AND successor_relation_version_id IS NOT NULL
          AND replacement_relation_id IS NULL)
        OR
        (fact_kind = 'replaced'
          AND successor_relation_version_id IS NULL
          AND replacement_relation_id IS NOT NULL)
        OR
        (fact_kind NOT IN ('evolved', 'replaced')
          AND successor_relation_version_id IS NULL
          AND replacement_relation_id IS NULL)
    ),
    CHECK (replacement_relation_id IS NULL OR replacement_relation_id != relation_id),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id),
    FOREIGN KEY (relation_id, relation_version_id)
      REFERENCES relation_versions(relation_id, relation_version_id),
    FOREIGN KEY (relation_id, successor_relation_version_id)
      REFERENCES relation_versions(relation_id, relation_version_id),
    FOREIGN KEY (replacement_relation_id) REFERENCES relation_identities(relation_id)
)"""),
    'relation_identities': ('table', 'relation_identities', """CREATE TABLE relation_identities (
    relation_id INTEGER NOT NULL PRIMARY KEY,
    created_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    FOREIGN KEY (created_event_id) REFERENCES organization_events(event_id)
)"""),
    'relation_version_participants': ('table', 'relation_version_participants', """CREATE TABLE relation_version_participants (
    relation_version_id INTEGER NOT NULL,
    participant_key TEXT NOT NULL CHECK (TRIM(participant_key) != ''),
    input_kind TEXT NOT NULL CHECK (input_kind IN ('source_knowledge', 'accepted_insight')),
    knowledge_result_id INTEGER,
    point_id TEXT,
    accepted_insight_version_id INTEGER,
    position INTEGER NOT NULL CHECK (position >= 0),
    contribution_text TEXT NOT NULL CHECK (TRIM(contribution_text) != ''),
    PRIMARY KEY (relation_version_id, participant_key),
    UNIQUE (relation_version_id, position),
    CHECK (
        (input_kind = 'source_knowledge'
          AND knowledge_result_id IS NOT NULL
          AND point_id IS NOT NULL AND TRIM(point_id) != ''
          AND accepted_insight_version_id IS NULL)
        OR
        (input_kind = 'accepted_insight'
          AND knowledge_result_id IS NULL AND point_id IS NULL
          AND accepted_insight_version_id IS NOT NULL)
    ),
    FOREIGN KEY (relation_version_id) REFERENCES relation_versions(relation_version_id),
    FOREIGN KEY (knowledge_result_id) REFERENCES knowledge_results(knowledge_result_id),
    FOREIGN KEY (accepted_insight_version_id)
      REFERENCES accepted_insight_versions(insight_version_id)
)"""),
    'relation_version_used_relations': ('table', 'relation_version_used_relations', """CREATE TABLE relation_version_used_relations (
    relation_version_id INTEGER NOT NULL,
    used_relation_version_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    role_text TEXT NOT NULL CHECK (TRIM(role_text) != ''),
    PRIMARY KEY (relation_version_id, used_relation_version_id),
    UNIQUE (relation_version_id, position),
    CHECK (relation_version_id != used_relation_version_id),
    FOREIGN KEY (relation_version_id) REFERENCES relation_versions(relation_version_id),
    FOREIGN KEY (used_relation_version_id) REFERENCES relation_versions(relation_version_id)
)"""),
    'relation_versions': ('table', 'relation_versions', """CREATE TABLE relation_versions (
    relation_version_id INTEGER NOT NULL PRIMARY KEY,
    relation_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    previous_version_id INTEGER,
    produced_event_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK (TRIM(payload_json) != ''),
    semantic_signature TEXT NOT NULL CHECK (LENGTH(semantic_signature) = 64),
    dependency_signature TEXT NOT NULL CHECK (LENGTH(dependency_signature) = 64),
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    UNIQUE (relation_id, version_no),
    UNIQUE (relation_id, relation_version_id),
    UNIQUE (previous_version_id),
    UNIQUE (produced_event_id, relation_id),
    CHECK (
        (version_no = 1 AND previous_version_id IS NULL)
        OR (version_no > 1 AND previous_version_id IS NOT NULL)
    ),
    FOREIGN KEY (relation_id) REFERENCES relation_identities(relation_id),
    FOREIGN KEY (relation_id, previous_version_id)
      REFERENCES relation_versions(relation_id, relation_version_id),
    FOREIGN KEY (produced_event_id) REFERENCES organization_events(event_id)
)"""),
    'settings': ('table', 'settings', """CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"""),
    'source_connections': ('table', 'source_connections', """CREATE TABLE source_connections (
    platform TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('connected', 'relogin_required', 'unconfigured')
    ),
    generation INTEGER NOT NULL CHECK (generation > 0),
    account_label TEXT,
    connected_at TEXT NOT NULL
, browser_context TEXT)"""),
    'source_facts': ('table', 'source_facts', """CREATE TABLE source_facts (
    source_fact_id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL UNIQUE REFERENCES materials(material_id),
    snapshot TEXT NOT NULL CHECK (TRIM(snapshot) != ''),
    uncertainties_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
)"""),
    'source_media': ('table', 'source_media', """CREATE TABLE source_media (
                material_id INTEGER NOT NULL REFERENCES materials(material_id),
                member_id TEXT NOT NULL, position INTEGER NOT NULL,
                mime_type TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY(material_id, member_id), UNIQUE(material_id, position)
            )"""),
    'source_review_results': ('table', 'source_review_results', """CREATE TABLE source_review_results (
                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
                revision INTEGER NOT NULL, identity TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('complete','failed')),
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(item_id, revision))"""),
    'submitted_sources': ('table', 'submitted_sources', """CREATE TABLE "submitted_sources" (
    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),
    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', 'markdown', 'pdf', 'epub', 'image')),
    input_key TEXT NOT NULL,
    input_label TEXT NOT NULL,
    input_metadata TEXT NOT NULL,
    content BLOB,
    retain_until TEXT,
    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),
    binding_scope TEXT NOT NULL DEFAULT 'legacy' CHECK(typeof(binding_scope)='text'),
    UNIQUE(input_kind, input_key, binding_scope)
)"""),
    'topic_entries': ('table', 'topic_entries', """CREATE TABLE topic_entries (
        topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, scope TEXT NOT NULL, updated_at TEXT NOT NULL
    )"""),
    'topic_members': ('table', 'topic_members', """CREATE TABLE topic_members (
        topic_id INTEGER NOT NULL REFERENCES topic_entries(topic_id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        knowledge_result_id INTEGER NOT NULL REFERENCES knowledge_results(knowledge_result_id),
        point_id TEXT NOT NULL,
        PRIMARY KEY(topic_id, position), UNIQUE(topic_id, knowledge_result_id, point_id)
    )"""),
    'topic_snapshot': ('table', 'topic_snapshot', """CREATE TABLE topic_snapshot (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        knowledge_count INTEGER NOT NULL CHECK(knowledge_count >= 0),
        input_signature TEXT NOT NULL
    )"""),
    'user_insight_judgments': ('table', 'user_insight_judgments', """CREATE TABLE user_insight_judgments (
    judgment_id INTEGER NOT NULL PRIMARY KEY,
    insight_id INTEGER NOT NULL,
    insight_version_id INTEGER NOT NULL UNIQUE,
    decision TEXT NOT NULL CHECK (decision IN ('interesting', 'rethink')),
    annotation_text TEXT,
    decided_at TEXT NOT NULL CHECK (TRIM(decided_at) != ''),
    CHECK (annotation_text IS NULL OR TRIM(annotation_text) != ''),
    UNIQUE (insight_id, judgment_id),
    UNIQUE (insight_version_id, judgment_id, decision),
    FOREIGN KEY (insight_id, insight_version_id)
      REFERENCES insight_versions(insight_id, insight_version_id)
)"""),
    'accepted_insight_versions_role_order': ('index', 'accepted_insight_versions', """CREATE INDEX accepted_insight_versions_role_order
ON accepted_insight_versions(current_role, accepted_at DESC, insight_version_id DESC)
"""),
    'insight_version_disqualifications_kind': ('index', 'insight_version_disqualifications', """CREATE INDEX insight_version_disqualifications_kind
ON insight_version_disqualifications(insight_version_id, fact_kind)
"""),
    'insight_version_participants_accepted': ('index', 'insight_version_participants', """CREATE INDEX insight_version_participants_accepted
ON insight_version_participants(accepted_insight_version_id)
"""),
    'insight_version_participants_source': ('index', 'insight_version_participants', """CREATE INDEX insight_version_participants_source
ON insight_version_participants(knowledge_result_id, point_id)
"""),
    'insight_versions_semantic_signature': ('index', 'insight_versions', """CREATE INDEX insight_versions_semantic_signature
ON insight_versions(semantic_signature)
"""),
    'knowledge_result_source_fact_identity': ('index', 'knowledge_results', """CREATE UNIQUE INDEX knowledge_result_source_fact_identity
ON knowledge_results(knowledge_result_id, source_fact_id)
"""),
    'one_basis_invalid_per_version': ('index', 'relation_facts', """CREATE UNIQUE INDEX one_basis_invalid_per_version
ON relation_facts(relation_version_id) WHERE fact_kind = 'basis_invalid'
"""),
    'one_current_accepted_version_per_identity': ('index', 'accepted_insight_versions', """CREATE UNIQUE INDEX one_current_accepted_version_per_identity
ON accepted_insight_versions(insight_id) WHERE current_role = 'current'
"""),
    'one_evolution_successor_per_version': ('index', 'relation_facts', """CREATE UNIQUE INDEX one_evolution_successor_per_version
ON relation_facts(relation_version_id) WHERE fact_kind = 'evolved'
"""),
    'one_replacement_per_relation': ('index', 'relation_facts', """CREATE UNIQUE INDEX one_replacement_per_relation
ON relation_facts(relation_id) WHERE fact_kind = 'replaced'
"""),
    'one_running_organization_event': ('index', 'organization_events', """CREATE UNIQUE INDEX one_running_organization_event
ON organization_events(memory_scope) WHERE status = 'running'
"""),
    'one_wrong_fact_per_relation': ('index', 'relation_facts', """CREATE UNIQUE INDEX one_wrong_fact_per_relation
ON relation_facts(relation_id) WHERE fact_kind = 'wrong'
"""),
    'organization_event_source_boundary_role_knowledge': ('index', 'organization_event_source_boundary', """CREATE INDEX organization_event_source_boundary_role_knowledge
ON organization_event_source_boundary(boundary_role, knowledge_result_id)
"""),
    'organization_events_status_order': ('index', 'organization_events', """CREATE INDEX organization_events_status_order
ON organization_events(status, event_id DESC)
"""),
    'raw_records_subject': ('index', 'raw_records', """CREATE INDEX raw_records_subject ON raw_records(subject_kind, subject_id)"""),
    'relation_facts_event_kind': ('index', 'relation_facts', """CREATE INDEX relation_facts_event_kind
ON relation_facts(event_id, fact_kind)
"""),
    'relation_version_participants_accepted': ('index', 'relation_version_participants', """CREATE INDEX relation_version_participants_accepted
ON relation_version_participants(accepted_insight_version_id)
"""),
    'relation_version_participants_source': ('index', 'relation_version_participants', """CREATE INDEX relation_version_participants_source
ON relation_version_participants(knowledge_result_id, point_id)
"""),
    'relation_versions_semantic_signature': ('index', 'relation_versions', """CREATE INDEX relation_versions_semantic_signature
ON relation_versions(semantic_signature)
"""),
    'user_insight_judgments_decision_order': ('index', 'user_insight_judgments', """CREATE INDEX user_insight_judgments_decision_order
ON user_insight_judgments(decision, decided_at)
"""),
    'accepted_born_historical_cause_on_insert': ('trigger', 'accepted_insight_versions', """CREATE TRIGGER accepted_born_historical_cause_on_insert
BEFORE INSERT ON accepted_insight_versions
WHEN NEW.current_role = 'historical'
 AND NEW.historical_reason IN (
   'born_older_than_current', 'born_after_newer_ever_current'
 )
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM user_insight_judgments AS j
        JOIN insight_versions AS cause_v
          ON cause_v.insight_version_id = j.insight_version_id
        JOIN insight_versions AS target_v
          ON target_v.insight_version_id = NEW.insight_version_id
        WHERE j.judgment_id = NEW.caused_by_judgment_id
          AND j.insight_id = NEW.insight_id
          AND (j.decision = 'interesting' OR EXISTS (SELECT 1 FROM insight_reconsiderations r WHERE r.judgment_id=j.judgment_id AND r.insight_version_id=j.insight_version_id))
          AND cause_v.version_no > target_v.version_no
    ) THEN RAISE(ABORT, 'accepted historical judgment cause must be newer interesting') END;
    SELECT CASE WHEN NEW.historical_reason = 'born_older_than_current'
      AND NOT EXISTS (
        SELECT 1 FROM accepted_insight_versions AS a
        WHERE a.judgment_id = NEW.caused_by_judgment_id
          AND a.insight_id = NEW.insight_id
          AND a.current_role = 'current'
      ) THEN RAISE(ABORT, 'born older cause must be current') END;
    SELECT CASE WHEN NEW.historical_reason = 'born_after_newer_ever_current'
      AND NOT EXISTS (
        SELECT 1 FROM accepted_insight_versions AS a
        WHERE a.judgment_id = NEW.caused_by_judgment_id
          AND a.insight_id = NEW.insight_id
          AND a.initial_role = 'current'
          AND a.current_role = 'historical'
      ) THEN RAISE(ABORT, 'born after newer cause must have been current') END;
END"""),
    'accepted_boundary_requires_current': ('trigger', 'organization_event_accepted_boundary', """CREATE TRIGGER accepted_boundary_requires_current
BEFORE INSERT ON organization_event_accepted_boundary
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM accepted_insight_versions AS a
        WHERE a.insight_version_id = NEW.insight_version_id
          AND a.current_role = 'current'
    ) THEN RAISE(ABORT, 'accepted boundary requires current exact version') END;
END"""),
    'accepted_grant_no_change': ('trigger', 'accepted_insight_versions', """CREATE TRIGGER accepted_grant_no_change BEFORE UPDATE OF reconsideration_id ON accepted_insight_versions
        BEGIN SELECT RAISE(ABORT,'Accepted grant is immutable'); END"""),
    'accepted_initial_current_must_be_inserted_current': ('trigger', 'accepted_insight_versions', """CREATE TRIGGER accepted_initial_current_must_be_inserted_current
BEFORE INSERT ON accepted_insight_versions
WHEN NEW.initial_role = 'current' AND NEW.current_role != 'current'
BEGIN
    SELECT RAISE(ABORT, 'initial current accepted row must be inserted current');
END"""),
    'accepted_insight_versions_cannot_be_deleted': ('trigger', 'accepted_insight_versions', """CREATE TRIGGER accepted_insight_versions_cannot_be_deleted
BEFORE DELETE ON accepted_insight_versions
BEGIN
    SELECT RAISE(ABORT, 'accepted insight history is immutable');
END"""),
    'accepted_insight_versions_current_to_historical_only': ('trigger', 'accepted_insight_versions', """CREATE TRIGGER accepted_insight_versions_current_to_historical_only
BEFORE UPDATE ON accepted_insight_versions
WHEN NOT (
    OLD.current_role = 'current'
    AND NEW.current_role = 'historical'
    AND NEW.insight_version_id IS OLD.insight_version_id
    AND NEW.insight_id IS OLD.insight_id
    AND NEW.judgment_id IS OLD.judgment_id
    AND NEW.judgment_decision IS OLD.judgment_decision
    AND NEW.initial_role IS OLD.initial_role
    AND NEW.accepted_at IS OLD.accepted_at
)
BEGIN
    SELECT RAISE(ABORT, 'accepted insight only allows current to historical transition');
END"""),
    'accepted_newer_current_cause_on_update': ('trigger', 'accepted_insight_versions', """CREATE TRIGGER accepted_newer_current_cause_on_update
BEFORE UPDATE OF current_role, historical_reason, caused_by_judgment_id
ON accepted_insight_versions
WHEN NEW.current_role = 'historical'
 AND NEW.historical_reason = 'newer_accepted_current'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM user_insight_judgments AS j
        JOIN insight_versions AS cause_v
          ON cause_v.insight_version_id = j.insight_version_id
        JOIN insight_versions AS target_v
          ON target_v.insight_version_id = NEW.insight_version_id
        WHERE j.judgment_id = NEW.caused_by_judgment_id
          AND j.insight_id = NEW.insight_id
          AND (j.decision = 'interesting' OR EXISTS (SELECT 1 FROM insight_reconsiderations r WHERE r.judgment_id=j.judgment_id AND r.insight_version_id=j.insight_version_id))
          AND cause_v.version_no > target_v.version_no
    ) THEN RAISE(ABORT, 'accepted historical judgment cause must be newer interesting') END;
END"""),
    'capture_identity_events_no_delete': ('trigger', 'capture_identity_events', """CREATE TRIGGER capture_identity_events_no_delete BEFORE DELETE ON capture_identity_events BEGIN
                SELECT RAISE(ABORT, 'capture_identity_events is immutable'); END"""),
    'capture_identity_events_no_update': ('trigger', 'capture_identity_events', """CREATE TRIGGER capture_identity_events_no_update BEFORE UPDATE ON capture_identity_events BEGIN
                SELECT RAISE(ABORT, 'capture_identity_events is immutable'); END"""),
    'capture_transcripts_no_delete': ('trigger', 'capture_transcripts', """CREATE TRIGGER capture_transcripts_no_delete BEFORE DELETE ON capture_transcripts BEGIN
                SELECT RAISE(ABORT, 'capture_transcripts is immutable'); END"""),
    'capture_transcripts_no_update': ('trigger', 'capture_transcripts', """CREATE TRIGGER capture_transcripts_no_update BEFORE UPDATE ON capture_transcripts BEGIN
                SELECT RAISE(ABORT, 'capture_transcripts is immutable'); END"""),
    'captures_no_delete': ('trigger', 'captures', """CREATE TRIGGER captures_no_delete BEFORE DELETE ON captures BEGIN
                SELECT RAISE(ABORT, 'captures is immutable'); END"""),
    'captures_no_update': ('trigger', 'captures', """CREATE TRIGGER captures_no_update BEFORE UPDATE ON captures BEGIN
                SELECT RAISE(ABORT, 'captures is immutable'); END"""),
    'collection_confirmations_no_delete': ('trigger', 'collection_confirmations', """CREATE TRIGGER collection_confirmations_no_delete BEFORE DELETE ON collection_confirmations
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'collection_confirmations_no_update': ('trigger', 'collection_confirmations', """CREATE TRIGGER collection_confirmations_no_update BEFORE UPDATE ON collection_confirmations
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'collection_events_no_delete': ('trigger', 'collection_events', """CREATE TRIGGER collection_events_no_delete BEFORE DELETE ON collection_events
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'collection_events_no_update': ('trigger', 'collection_events', """CREATE TRIGGER collection_events_no_update BEFORE UPDATE ON collection_events
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'collection_manifest_no_update': ('trigger', 'collection_operations', """CREATE TRIGGER collection_manifest_no_update
        BEFORE UPDATE OF kind,source_key,manifest_json,signature,content_signature,authority_json,confirmation_token ON collection_operations
        BEGIN SELECT RAISE(ABORT,'Collection manifest is immutable'); END"""),
    'collection_member_binding_no_replace': ('trigger', 'collection_members', """CREATE TRIGGER collection_member_binding_no_replace
        BEFORE UPDATE OF source_fact_id,knowledge_result_id ON collection_members
        WHEN (OLD.source_fact_id IS NOT NULL AND NEW.source_fact_id IS NOT OLD.source_fact_id)
          OR (OLD.knowledge_result_id IS NOT NULL AND NEW.knowledge_result_id IS NOT OLD.knowledge_result_id)
        BEGIN SELECT RAISE(ABORT,'Collection result binding is immutable'); END"""),
    'collection_members_no_delete': ('trigger', 'collection_members', """CREATE TRIGGER collection_members_no_delete BEFORE DELETE ON collection_members
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'collection_membership_no_update': ('trigger', 'collection_members', """CREATE TRIGGER collection_membership_no_update
        BEFORE UPDATE OF operation_id,ordinal,native_id,native_version,item_id,known_unsupported ON collection_members
        BEGIN SELECT RAISE(ABORT,'Collection membership is immutable'); END"""),
    'collection_operations_no_delete': ('trigger', 'collection_operations', """CREATE TRIGGER collection_operations_no_delete BEFORE DELETE ON collection_operations
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'collection_results_no_delete': ('trigger', 'collection_results', """CREATE TRIGGER collection_results_no_delete BEFORE DELETE ON collection_results
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'collection_results_no_update': ('trigger', 'collection_results', """CREATE TRIGGER collection_results_no_update BEFORE UPDATE ON collection_results
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END"""),
    'delivery_adjacency_no_delete': ('trigger', 'delivery_adjacency', """CREATE TRIGGER delivery_adjacency_no_delete BEFORE DELETE ON delivery_adjacency BEGIN
                SELECT RAISE(ABORT, 'delivery_adjacency is immutable'); END"""),
    'delivery_adjacency_no_update': ('trigger', 'delivery_adjacency', """CREATE TRIGGER delivery_adjacency_no_update BEFORE UPDATE ON delivery_adjacency BEGIN
                SELECT RAISE(ABORT, 'delivery_adjacency is immutable'); END"""),
    'distill_review_revision': ('trigger', 'distill_items', """CREATE TRIGGER distill_review_revision AFTER UPDATE ON distill_items
                WHEN NEW.review_revision = OLD.review_revision AND (
                    NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase
                    OR NEW.material_id IS NOT OLD.material_id
                    OR NEW.submitted_url IS NOT OLD.submitted_url
                    OR NEW.confirmation_json IS NOT OLD.confirmation_json
                    OR NEW.platform_authority_json IS NOT OLD.platform_authority_json
                ) BEGIN
                UPDATE distill_items SET review_revision = OLD.review_revision + 1
                WHERE item_id = NEW.item_id;
            END"""),
    'insight_identities_cannot_be_deleted': ('trigger', 'insight_identities', """CREATE TRIGGER insight_identities_cannot_be_deleted
BEFORE DELETE ON insight_identities
BEGIN
    SELECT RAISE(ABORT, 'insight identities are immutable');
END"""),
    'insight_identities_cannot_be_updated': ('trigger', 'insight_identities', """CREATE TRIGGER insight_identities_cannot_be_updated
BEFORE UPDATE ON insight_identities
BEGIN
    SELECT RAISE(ABORT, 'insight identities are immutable');
END"""),
    'insight_identity_replacements_cannot_be_deleted': ('trigger', 'insight_identity_replacements', """CREATE TRIGGER insight_identity_replacements_cannot_be_deleted
BEFORE DELETE ON insight_identity_replacements
BEGIN
    SELECT RAISE(ABORT, 'insight replacements are immutable');
END"""),
    'insight_identity_replacements_cannot_be_updated': ('trigger', 'insight_identity_replacements', """CREATE TRIGGER insight_identity_replacements_cannot_be_updated
BEFORE UPDATE ON insight_identity_replacements
BEGIN
    SELECT RAISE(ABORT, 'insight replacements are immutable');
END"""),
    'insight_participant_within_event_boundary': ('trigger', 'insight_version_participants', """CREATE TRIGGER insight_participant_within_event_boundary
BEFORE INSERT ON insight_version_participants
BEGIN
    SELECT CASE
      WHEN NEW.input_kind = 'source_knowledge' AND NOT EXISTS (
        SELECT 1
        FROM insight_versions AS v
        JOIN organization_event_source_boundary AS b
          ON b.event_id = v.produced_event_id
        WHERE v.insight_version_id = NEW.insight_version_id
          AND b.knowledge_result_id = NEW.knowledge_result_id
      ) THEN RAISE(ABORT, 'insight source participant is outside event boundary')
      WHEN NEW.input_kind = 'accepted_insight' AND NOT EXISTS (
        SELECT 1
        FROM insight_versions AS v
        JOIN organization_event_accepted_boundary AS b
          ON b.event_id = v.produced_event_id
        WHERE v.insight_version_id = NEW.insight_version_id
          AND b.insight_version_id = NEW.accepted_insight_version_id
      ) THEN RAISE(ABORT, 'insight accepted participant is outside event boundary')
    END;
END"""),
    'insight_reconsiderations_no_delete': ('trigger', 'insight_reconsiderations', """CREATE TRIGGER insight_reconsiderations_no_delete BEFORE DELETE ON insight_reconsiderations BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END"""),
    'insight_reconsiderations_no_update': ('trigger', 'insight_reconsiderations', """CREATE TRIGGER insight_reconsiderations_no_update BEFORE UPDATE ON insight_reconsiderations BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END"""),
    'insight_used_relation_is_boundary_or_same_event': ('trigger', 'insight_version_used_relations', """CREATE TRIGGER insight_used_relation_is_boundary_or_same_event
BEFORE INSERT ON insight_version_used_relations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM insight_versions AS owner
        WHERE owner.insight_version_id = NEW.insight_version_id
          AND (
            EXISTS (
              SELECT 1 FROM organization_event_relation_boundary AS b
              WHERE b.event_id = owner.produced_event_id
                AND b.relation_version_id = NEW.relation_version_id
            )
            OR EXISTS (
              SELECT 1 FROM relation_versions AS planned
              WHERE planned.relation_version_id = NEW.relation_version_id
                AND planned.produced_event_id = owner.produced_event_id
            )
          )
    ) THEN RAISE(ABORT, 'insight used relation is neither boundary nor same event') END;
END"""),
    'insight_version_disqualifications_cannot_be_deleted': ('trigger', 'insight_version_disqualifications', """CREATE TRIGGER insight_version_disqualifications_cannot_be_deleted
BEFORE DELETE ON insight_version_disqualifications
BEGIN
    SELECT RAISE(ABORT, 'insight disqualifications are immutable');
END"""),
    'insight_version_disqualifications_cannot_be_updated': ('trigger', 'insight_version_disqualifications', """CREATE TRIGGER insight_version_disqualifications_cannot_be_updated
BEFORE UPDATE ON insight_version_disqualifications
BEGIN
    SELECT RAISE(ABORT, 'insight disqualifications are immutable');
END"""),
    'insight_version_lineage_must_be_consecutive': ('trigger', 'insight_versions', """CREATE TRIGGER insight_version_lineage_must_be_consecutive
BEFORE INSERT ON insight_versions
BEGIN
    SELECT CASE
      WHEN NEW.version_no = 1 AND NOT EXISTS (
        SELECT 1 FROM insight_identities AS i
        WHERE i.insight_id = NEW.insight_id
          AND i.created_event_id = NEW.produced_event_id
      ) THEN RAISE(ABORT, 'first insight version must match identity event')
      WHEN NEW.version_no > 1 AND NOT EXISTS (
        SELECT 1 FROM insight_versions AS p
        WHERE p.insight_id = NEW.insight_id
          AND p.insight_version_id = NEW.previous_version_id
          AND p.version_no = NEW.version_no - 1
      ) THEN RAISE(ABORT, 'insight predecessor must be same identity and consecutive')
    END;
END"""),
    'insight_version_participants_cannot_be_deleted': ('trigger', 'insight_version_participants', """CREATE TRIGGER insight_version_participants_cannot_be_deleted
BEFORE DELETE ON insight_version_participants
BEGIN
    SELECT RAISE(ABORT, 'insight participants are immutable');
END"""),
    'insight_version_participants_cannot_be_updated': ('trigger', 'insight_version_participants', """CREATE TRIGGER insight_version_participants_cannot_be_updated
BEFORE UPDATE ON insight_version_participants
BEGIN
    SELECT RAISE(ABORT, 'insight participants are immutable');
END"""),
    'insight_version_used_relations_cannot_be_deleted': ('trigger', 'insight_version_used_relations', """CREATE TRIGGER insight_version_used_relations_cannot_be_deleted
BEFORE DELETE ON insight_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'insight used edges are immutable');
END"""),
    'insight_version_used_relations_cannot_be_updated': ('trigger', 'insight_version_used_relations', """CREATE TRIGGER insight_version_used_relations_cannot_be_updated
BEFORE UPDATE ON insight_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'insight used edges are immutable');
END"""),
    'insight_versions_cannot_be_deleted': ('trigger', 'insight_versions', """CREATE TRIGGER insight_versions_cannot_be_deleted
BEFORE DELETE ON insight_versions
BEGIN
    SELECT RAISE(ABORT, 'insight versions are immutable');
END"""),
    'insight_versions_cannot_be_updated': ('trigger', 'insight_versions', """CREATE TRIGGER insight_versions_cannot_be_updated
BEFORE UPDATE ON insight_versions
BEGIN
    SELECT RAISE(ABORT, 'insight versions are immutable');
END"""),
    'knowledge_results_content_no_update': ('trigger', 'knowledge_results', """CREATE TRIGGER knowledge_results_content_no_update
BEFORE UPDATE OF source_fact_id, payload_json, created_at ON knowledge_results BEGIN
    SELECT RAISE(ABORT, 'KnowledgeResult content is immutable');
END"""),
    'knowledge_results_no_delete': ('trigger', 'knowledge_results', """CREATE TRIGGER knowledge_results_no_delete
BEFORE DELETE ON knowledge_results BEGIN
    SELECT RAISE(ABORT, 'KnowledgeResult is immutable');
END"""),
    'material_snapshot_identity_no_update': ('trigger', 'materials', """CREATE TRIGGER material_snapshot_identity_no_update
        BEFORE UPDATE OF source_kind, source_key, snapshot_key ON materials BEGIN
        SELECT RAISE(ABORT, 'Material snapshot identity is immutable'); END"""),
    'material_snapshot_metadata_no_update': ('trigger', 'materials', """CREATE TRIGGER material_snapshot_metadata_no_update
        BEFORE UPDATE OF metadata_json, submitted_url, canonical_url ON materials
        WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = OLD.material_id) BEGIN
        SELECT RAISE(ABORT, 'SourceFact metadata is immutable'); END"""),
    'organization_coverage_matches_frozen_source': ('trigger', 'organization_event_coverages', """CREATE TRIGGER organization_coverage_matches_frozen_source
BEFORE INSERT ON organization_event_coverages
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM organization_event_source_boundary AS b
        JOIN organization_events AS e ON e.event_id = b.event_id
        WHERE b.event_id = NEW.event_id
          AND b.knowledge_result_id = NEW.knowledge_result_id
          AND b.source_fact_id = NEW.source_fact_id
          AND b.boundary_role = 'frozen_new'
          AND e.status = 'succeeded'
    ) THEN RAISE(ABORT, 'coverage does not match succeeded frozen source') END;
END"""),
    'organization_event_accepted_boundary_cannot_be_deleted': ('trigger', 'organization_event_accepted_boundary', """CREATE TRIGGER organization_event_accepted_boundary_cannot_be_deleted
BEFORE DELETE ON organization_event_accepted_boundary
BEGIN
    SELECT RAISE(ABORT, 'accepted event boundary is immutable');
END"""),
    'organization_event_accepted_boundary_cannot_be_updated': ('trigger', 'organization_event_accepted_boundary', """CREATE TRIGGER organization_event_accepted_boundary_cannot_be_updated
BEFORE UPDATE ON organization_event_accepted_boundary
BEGIN
    SELECT RAISE(ABORT, 'accepted event boundary is immutable');
END"""),
    'organization_event_coverages_cannot_be_deleted': ('trigger', 'organization_event_coverages', """CREATE TRIGGER organization_event_coverages_cannot_be_deleted
BEFORE DELETE ON organization_event_coverages
BEGIN
    SELECT RAISE(ABORT, 'organization event coverage is immutable');
END"""),
    'organization_event_coverages_cannot_be_updated': ('trigger', 'organization_event_coverages', """CREATE TRIGGER organization_event_coverages_cannot_be_updated
BEFORE UPDATE ON organization_event_coverages
BEGIN
    SELECT RAISE(ABORT, 'organization event coverage is immutable');
END"""),
    'organization_event_relation_boundary_cannot_be_deleted': ('trigger', 'organization_event_relation_boundary', """CREATE TRIGGER organization_event_relation_boundary_cannot_be_deleted
BEFORE DELETE ON organization_event_relation_boundary
BEGIN
    SELECT RAISE(ABORT, 'relation event boundary is immutable');
END"""),
    'organization_event_relation_boundary_cannot_be_updated': ('trigger', 'organization_event_relation_boundary', """CREATE TRIGGER organization_event_relation_boundary_cannot_be_updated
BEFORE UPDATE ON organization_event_relation_boundary
BEGIN
    SELECT RAISE(ABORT, 'relation event boundary is immutable');
END"""),
    'organization_event_source_boundary_cannot_be_deleted': ('trigger', 'organization_event_source_boundary', """CREATE TRIGGER organization_event_source_boundary_cannot_be_deleted
BEFORE DELETE ON organization_event_source_boundary
BEGIN
    SELECT RAISE(ABORT, 'organization event source boundary is immutable');
END"""),
    'organization_event_source_boundary_cannot_be_updated': ('trigger', 'organization_event_source_boundary', """CREATE TRIGGER organization_event_source_boundary_cannot_be_updated
BEFORE UPDATE ON organization_event_source_boundary
BEGIN
    SELECT RAISE(ABORT, 'organization event source boundary is immutable');
END"""),
    'organization_events_cannot_be_deleted': ('trigger', 'organization_events', """CREATE TRIGGER organization_events_cannot_be_deleted
BEFORE DELETE ON organization_events
BEGIN
    SELECT RAISE(ABORT, 'organization events are immutable history');
END"""),
    'organization_events_terminal_transition_only': ('trigger', 'organization_events', """CREATE TRIGGER organization_events_terminal_transition_only
BEFORE UPDATE ON organization_events
WHEN NOT (
    OLD.status = 'running'
    AND NEW.status IN ('failed', 'succeeded')
    AND NEW.event_id IS OLD.event_id
    AND NEW.memory_scope IS OLD.memory_scope
    AND NEW.started_at IS OLD.started_at
    AND NEW.topic_before_json IS OLD.topic_before_json
    AND NEW.topic_before_signature IS OLD.topic_before_signature
    AND NEW.topic_guard_signature IS OLD.topic_guard_signature
    AND NEW.boundary_signature IS OLD.boundary_signature
)
BEGIN
    SELECT RAISE(ABORT, 'organization event only allows one running to terminal transition');
END"""),
    'personal_cognition_entries_no_delete': ('trigger', 'personal_cognition_entries', """CREATE TRIGGER personal_cognition_entries_no_delete BEFORE DELETE ON personal_cognition_entries BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END"""),
    'personal_cognition_entries_no_update': ('trigger', 'personal_cognition_entries', """CREATE TRIGGER personal_cognition_entries_no_update BEFORE UPDATE ON personal_cognition_entries BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END"""),
    'raw_records_content_no_update': ('trigger', 'raw_records', """CREATE TRIGGER raw_records_content_no_update
    BEFORE UPDATE OF raw_id, subject_kind, subject_id, identity, relative_path, content, content_sha256,
        attachments_json, supersedes, origin, created_at ON raw_records BEGIN
        SELECT RAISE(ABORT, 'raw record is immutable');
    END"""),
    'raw_records_no_delete': ('trigger', 'raw_records', """CREATE TRIGGER raw_records_no_delete
    BEFORE DELETE ON raw_records BEGIN
        SELECT RAISE(ABORT, 'raw record is immutable');
    END"""),
    'raw_records_written_once': ('trigger', 'raw_records', """CREATE TRIGGER raw_records_written_once
    BEFORE UPDATE OF written_at, written_vault ON raw_records WHEN OLD.written_at IS NOT NULL BEGIN
        SELECT RAISE(ABORT, 'raw record was already written');
    END"""),
    'relation_boundary_matches_role': ('trigger', 'organization_event_relation_boundary', """CREATE TRIGGER relation_boundary_matches_role
BEFORE INSERT ON organization_event_relation_boundary
BEGIN
    SELECT CASE
      WHEN NEW.boundary_role = 'current_input' AND NOT EXISTS (
        SELECT 1 FROM relation_current AS c
        WHERE c.relation_version_id = NEW.relation_version_id
      ) THEN RAISE(ABORT, 'relation current boundary requires exact current version')
      WHEN NEW.boundary_role = 'reconsideration_hint' AND EXISTS (
        SELECT 1
        FROM relation_versions AS v
        JOIN relation_current AS c ON c.relation_id = v.relation_id
        WHERE v.relation_version_id = NEW.relation_version_id
      ) THEN RAISE(ABORT, 'relation reconsideration hint cannot already be current')
    END;
END"""),
    'relation_facts_cannot_be_deleted': ('trigger', 'relation_facts', """CREATE TRIGGER relation_facts_cannot_be_deleted
BEFORE DELETE ON relation_facts
BEGIN
    SELECT RAISE(ABORT, 'relation facts are immutable');
END"""),
    'relation_facts_cannot_be_updated': ('trigger', 'relation_facts', """CREATE TRIGGER relation_facts_cannot_be_updated
BEFORE UPDATE ON relation_facts
BEGIN
    SELECT RAISE(ABORT, 'relation facts are immutable');
END"""),
    'relation_identities_cannot_be_deleted': ('trigger', 'relation_identities', """CREATE TRIGGER relation_identities_cannot_be_deleted
BEFORE DELETE ON relation_identities
BEGIN
    SELECT RAISE(ABORT, 'relation identities are immutable');
END"""),
    'relation_identities_cannot_be_updated': ('trigger', 'relation_identities', """CREATE TRIGGER relation_identities_cannot_be_updated
BEFORE UPDATE ON relation_identities
BEGIN
    SELECT RAISE(ABORT, 'relation identities are immutable');
END"""),
    'relation_participant_within_event_boundary': ('trigger', 'relation_version_participants', """CREATE TRIGGER relation_participant_within_event_boundary
BEFORE INSERT ON relation_version_participants
BEGIN
    SELECT CASE
      WHEN NEW.input_kind = 'source_knowledge' AND NOT EXISTS (
        SELECT 1
        FROM relation_versions AS v
        JOIN organization_event_source_boundary AS b
          ON b.event_id = v.produced_event_id
        WHERE v.relation_version_id = NEW.relation_version_id
          AND b.knowledge_result_id = NEW.knowledge_result_id
      ) THEN RAISE(ABORT, 'relation source participant is outside event boundary')
      WHEN NEW.input_kind = 'accepted_insight' AND NOT EXISTS (
        SELECT 1
        FROM relation_versions AS v
        JOIN organization_event_accepted_boundary AS b
          ON b.event_id = v.produced_event_id
        WHERE v.relation_version_id = NEW.relation_version_id
          AND b.insight_version_id = NEW.accepted_insight_version_id
      ) THEN RAISE(ABORT, 'relation accepted participant is outside event boundary')
    END;
END"""),
    'relation_used_relation_within_event_boundary': ('trigger', 'relation_version_used_relations', """CREATE TRIGGER relation_used_relation_within_event_boundary
BEFORE INSERT ON relation_version_used_relations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM relation_versions AS owner
        JOIN organization_event_relation_boundary AS b
          ON b.event_id = owner.produced_event_id
        WHERE owner.relation_version_id = NEW.relation_version_id
          AND b.relation_version_id = NEW.used_relation_version_id
    ) THEN RAISE(ABORT, 'relation used relation is outside event boundary') END;
END"""),
    'relation_version_lineage_must_be_consecutive': ('trigger', 'relation_versions', """CREATE TRIGGER relation_version_lineage_must_be_consecutive
BEFORE INSERT ON relation_versions
BEGIN
    SELECT CASE
      WHEN NEW.version_no = 1 AND NOT EXISTS (
        SELECT 1 FROM relation_identities AS i
        WHERE i.relation_id = NEW.relation_id
          AND i.created_event_id = NEW.produced_event_id
      ) THEN RAISE(ABORT, 'first relation version must match identity event')
      WHEN NEW.version_no > 1 AND NOT EXISTS (
        SELECT 1 FROM relation_versions AS p
        WHERE p.relation_id = NEW.relation_id
          AND p.relation_version_id = NEW.previous_version_id
          AND p.version_no = NEW.version_no - 1
      ) THEN RAISE(ABORT, 'relation predecessor must be same identity and consecutive')
    END;
END"""),
    'relation_version_participants_cannot_be_deleted': ('trigger', 'relation_version_participants', """CREATE TRIGGER relation_version_participants_cannot_be_deleted
BEFORE DELETE ON relation_version_participants
BEGIN
    SELECT RAISE(ABORT, 'relation participants are immutable');
END"""),
    'relation_version_participants_cannot_be_updated': ('trigger', 'relation_version_participants', """CREATE TRIGGER relation_version_participants_cannot_be_updated
BEFORE UPDATE ON relation_version_participants
BEGIN
    SELECT RAISE(ABORT, 'relation participants are immutable');
END"""),
    'relation_version_used_relations_cannot_be_deleted': ('trigger', 'relation_version_used_relations', """CREATE TRIGGER relation_version_used_relations_cannot_be_deleted
BEFORE DELETE ON relation_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'relation used edges are immutable');
END"""),
    'relation_version_used_relations_cannot_be_updated': ('trigger', 'relation_version_used_relations', """CREATE TRIGGER relation_version_used_relations_cannot_be_updated
BEFORE UPDATE ON relation_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'relation used edges are immutable');
END"""),
    'relation_versions_cannot_be_deleted': ('trigger', 'relation_versions', """CREATE TRIGGER relation_versions_cannot_be_deleted
BEFORE DELETE ON relation_versions
BEGIN
    SELECT RAISE(ABORT, 'relation versions are immutable');
END"""),
    'relation_versions_cannot_be_updated': ('trigger', 'relation_versions', """CREATE TRIGGER relation_versions_cannot_be_updated
BEFORE UPDATE ON relation_versions
BEGIN
    SELECT RAISE(ABORT, 'relation versions are immutable');
END"""),
    'source_facts_no_delete': ('trigger', 'source_facts', """CREATE TRIGGER source_facts_no_delete
BEFORE DELETE ON source_facts BEGIN
    SELECT RAISE(ABORT, 'SourceFact is immutable');
END"""),
    'source_facts_no_update': ('trigger', 'source_facts', """CREATE TRIGGER source_facts_no_update
BEFORE UPDATE ON source_facts BEGIN
    SELECT RAISE(ABORT, 'SourceFact is immutable');
END"""),
    'source_media_no_delete': ('trigger', 'source_media', """CREATE TRIGGER source_media_no_delete
                    BEFORE DELETE ON source_media
                    WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = OLD.material_id) BEGIN
                    SELECT RAISE(ABORT, 'SourceFact media is immutable'); END"""),
    'source_media_no_insert': ('trigger', 'source_media', """CREATE TRIGGER source_media_no_insert
                BEFORE INSERT ON source_media
                WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = NEW.material_id) BEGIN
                SELECT RAISE(ABORT, 'SourceFact media is immutable'); END"""),
    'source_media_no_update': ('trigger', 'source_media', """CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media
        WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id=OLD.material_id)
        AND NOT (NEW.material_id=OLD.material_id AND NEW.member_id=OLD.member_id
            AND NEW.position=OLD.position AND NEW.mime_type=OLD.mime_type
            AND NEW.sha256=OLD.sha256 AND length(OLD.content)>0
            AND typeof(NEW.content)='blob' AND length(NEW.content)=0
            AND EXISTS (SELECT 1 FROM materials m WHERE m.material_id=OLD.material_id AND m.source_kind IN ('douyin','youtube','xiaohongshu','x','zhihu','weibo','bilibili','image')
    AND EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=m.material_id)
    AND NOT EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=m.material_id
        AND (i.confirmation_json IS NOT NULL OR i.state='working'
             OR (i.dismissed_at IS NULL AND i.state!='succeeded')))
    AND EXISTS (SELECT 1 FROM source_facts sf WHERE sf.material_id=m.material_id) AND EXISTS (SELECT 1 FROM raw_records r WHERE r.subject_kind='material'
        AND r.subject_id=m.material_id AND r.written_at IS NOT NULL)))
        BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END"""),
    'user_insight_judgments_cannot_be_deleted': ('trigger', 'user_insight_judgments', """CREATE TRIGGER user_insight_judgments_cannot_be_deleted
BEFORE DELETE ON user_insight_judgments
BEGIN
    SELECT RAISE(ABORT, 'insight judgments are immutable');
END"""),
    'user_insight_judgments_cannot_be_updated': ('trigger', 'user_insight_judgments', """CREATE TRIGGER user_insight_judgments_cannot_be_updated
BEFORE UPDATE ON user_insight_judgments
BEGIN
    SELECT RAISE(ABORT, 'insight judgments are immutable');
END"""),
    'distill_items_ingestion_binding_immutable': ('trigger', 'distill_items', """CREATE TRIGGER distill_items_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON distill_items
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END"""),
    'distill_items_ingestion_binding_required': ('trigger', 'distill_items', """CREATE TRIGGER distill_items_ingestion_binding_required
            BEFORE INSERT ON distill_items WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END"""),
    'collection_operations_ingestion_binding_immutable': ('trigger', 'collection_operations', """CREATE TRIGGER collection_operations_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON collection_operations
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END"""),
    'collection_operations_ingestion_binding_required': ('trigger', 'collection_operations', """CREATE TRIGGER collection_operations_ingestion_binding_required
            BEFORE INSERT ON collection_operations WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END"""),
    'distill_items_ingestion_owner_immutable': ('trigger', 'distill_items', """CREATE TRIGGER distill_items_ingestion_owner_immutable
        BEFORE UPDATE OF material_id ON distill_items
        WHEN OLD.ingestion_contract!='legacy' AND OLD.material_id IS NOT NULL
          AND NEW.material_id IS NOT OLD.material_id
        BEGIN SELECT RAISE(ABORT,'ingestion owner is immutable'); END"""),
    'distill_items_ingestion_no_delete': ('trigger', 'distill_items', """CREATE TRIGGER distill_items_ingestion_no_delete
        BEFORE DELETE ON distill_items WHEN OLD.ingestion_contract!='legacy'
        BEGIN SELECT RAISE(ABORT,'ingestion owner is durable'); END"""),
    'collection_members_ingestion_contract_match': ('trigger', 'collection_members', """CREATE TRIGGER collection_members_ingestion_contract_match
        BEFORE INSERT ON collection_members
        WHEN (SELECT ingestion_contract FROM collection_operations WHERE operation_id=NEW.operation_id)
          IS NOT (SELECT ingestion_contract FROM distill_items WHERE item_id=NEW.item_id)
        BEGIN SELECT RAISE(ABORT,'collection ingestion contract mismatch'); END"""),
    'ingestion_events': ('table', 'ingestion_events', """CREATE TABLE ingestion_events (
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
    'ingestion_events_subject': ('index', 'ingestion_events', """CREATE INDEX ingestion_events_subject ON ingestion_events(subject_kind,subject_id,event_id)"""),
    'ingestion_events_observation_typed': ('trigger', 'ingestion_events', """CREATE TRIGGER ingestion_events_observation_typed
        BEFORE INSERT ON ingestion_events WHEN NEW.kind IN ('source_ready','raw_pending')
          AND COALESCE(NOT ((
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
          ) OR (NEW.kind='raw_pending' AND NEW.subject_kind='item'
            AND NEW.subject_id=NEW.item_id AND json_extract(NEW.detail_json,'$.code')='intake_frozen'
            AND (SELECT count(*) FROM json_each(NEW.detail_json))=4
            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json))=4
            AND NOT EXISTS(SELECT 1 FROM json_each(NEW.detail_json)
                WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256'))
            AND json_type(NEW.detail_json,'$.manifest')='object'
            AND (SELECT count(*) FROM json_each(NEW.detail_json,'$.manifest'))=1
            AND json_type(NEW.detail_json,'$.manifest.intake_envelope_json')='text'
            AND local_intake_event(NEW.event_key,NEW.contract,NEW.subject_kind,NEW.subject_id,
                NEW.item_id,NEW.binding_sha256,NEW.detail_json)=1)),1)
        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END"""),
    'ingestion_events_no_update': ('trigger', 'ingestion_events', """CREATE TRIGGER ingestion_events_no_update
            BEFORE UPDATE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END"""),
    'ingestion_events_no_delete': ('trigger', 'ingestion_events', """CREATE TRIGGER ingestion_events_no_delete
            BEFORE DELETE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END"""),
    'ingestion_events_proof_unavailable': ('trigger', 'ingestion_events', """CREATE TRIGGER ingestion_events_proof_unavailable
        BEFORE INSERT ON ingestion_events
        WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')
          AND (ingestion_proof(NEW.kind,NEW.binding_sha256,NEW.detail_json)!=1
               OR NEW.kind IS NOT json_extract(NEW.detail_json,'$.code')
               OR NEW.subject_kind IS NOT json_extract(NEW.detail_json,'$.manifest.subject_kind')
               OR NEW.subject_id IS NOT json_extract(NEW.detail_json,'$.manifest.subject_id')
               OR NEW.item_id IS NOT json_extract(NEW.detail_json,'$.manifest.owner_item_id')
               OR NEW.binding_sha256 IS NOT json_extract(NEW.detail_json,'$.final_binding_sha256'))
        BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END"""),
    'source_media_ingestion_no_update': ('trigger', 'source_media', """CREATE TRIGGER source_media_ingestion_no_update
            BEFORE UPDATE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
              AND ingestion_release(OLD.material_id,OLD.member_id,OLD.sha256,NEW.content)!=1
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END"""),
    'source_media_ingestion_no_delete': ('trigger', 'source_media', "CREATE TRIGGER source_media_ingestion_no_delete\n            BEFORE DELETE ON source_media\n            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id\n                         AND i.ingestion_contract!='legacy')\n              \n            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END"),
    'submitted_sources_ingestion_no_release': ('trigger', 'submitted_sources', """CREATE TRIGGER submitted_sources_ingestion_no_release
        BEFORE UPDATE OF content,input_metadata ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
          AND (NEW.content IS NOT OLD.content OR NEW.input_metadata IS NOT OLD.input_metadata)
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END"""),
    'submitted_sources_ingestion_no_delete': ('trigger', 'submitted_sources', """CREATE TRIGGER submitted_sources_ingestion_no_delete
        BEFORE DELETE ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END"""),
    'submitted_sources_ingestion_owner_immutable': ('trigger', 'submitted_sources', """CREATE TRIGGER submitted_sources_ingestion_owner_immutable
        BEFORE UPDATE OF item_id ON submitted_sources
        WHEN NEW.item_id IS NOT OLD.item_id AND EXISTS (
            SELECT 1 FROM distill_items i WHERE i.item_id IN (OLD.item_id,NEW.item_id)
            AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input owner is immutable'); END"""),
    'source_media_ingestion_capture_binding': ('trigger', 'capture_state', """CREATE TRIGGER source_media_ingestion_capture_binding
        BEFORE UPDATE OF item_id,audio_path ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND (NEW.item_id IS NOT OLD.item_id
            OR (OLD.audio_path IS NOT NULL AND NEW.audio_path IS NOT OLD.audio_path))
        BEGIN SELECT RAISE(ABORT,'ingestion capture binding is immutable'); END"""),
    'source_media_ingestion_capture_release': ('trigger', 'capture_state', """CREATE TRIGGER source_media_ingestion_capture_release
        BEFORE UPDATE OF audio_released_at ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND NEW.audio_released_at IS NOT OLD.audio_released_at
          AND ingestion_release('capture',OLD.capture_id,OLD.audio_path,NEW.audio_released_at)!=1
        BEGIN SELECT RAISE(ABORT,'ingestion audio is retained'); END"""),
    'source_media_ingestion_capture_no_delete': ('trigger', 'capture_state', """CREATE TRIGGER source_media_ingestion_capture_no_delete
        BEFORE DELETE ON capture_state WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1'))
        BEGIN SELECT RAISE(ABORT,'ingestion capture owner is durable'); END"""),
    'distill_items_raw_terminal_no_insert': ('trigger', 'distill_items', """CREATE TRIGGER distill_items_raw_terminal_no_insert
        BEFORE INSERT ON distill_items WHEN NEW.state='raw_saved'
        BEGIN SELECT RAISE(ABORT,'raw terminal writer required'); END"""),
    'distill_items_raw_terminal_proof': ('trigger', 'distill_items', """CREATE TRIGGER distill_items_raw_terminal_proof
        BEFORE UPDATE ON distill_items WHEN NEW.state='raw_saved' AND OLD.state!='raw_saved'
        AND (OLD.state!='working' OR OLD.phase NOT IN ('collecting','reviewing')
            OR NEW.phase!='done' OR NEW.ingestion_contract!='raw-verified-v1'
            OR NEW.confirmation_json IS NOT NULL OR NEW.dismissed_at IS NOT NULL
            OR NEW.material_id IS NULL OR NEW.source_binding_sha256 IS NULL
            OR NEW.relation_binding_sha256 IS NULL
            OR ingestion_raw_terminal(OLD.item_id,OLD.review_revision,NEW.state,NEW.phase)!=1)
        BEGIN SELECT RAISE(ABORT,'raw terminal proof unavailable'); END"""),
    'distill_items_raw_terminal_no_reopen': ('trigger', 'distill_items', """CREATE TRIGGER distill_items_raw_terminal_no_reopen
        BEFORE UPDATE OF state,phase ON distill_items WHEN OLD.state='raw_saved'
        AND (NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase)
        BEGIN SELECT RAISE(ABORT,'raw terminal is durable'); END"""),
    'submitted_sources_local_insert': ('trigger', 'submitted_sources', """CREATE TRIGGER submitted_sources_local_insert BEFORE INSERT ON submitted_sources
        WHEN NEW.binding_scope!='legacy' AND (
          typeof(NEW.binding_scope)!='text' OR
          local_intake_insert(NEW.item_id,NEW.binding_scope,NEW.input_kind,NEW.input_key,
                             NEW.input_label,NEW.input_metadata,NEW.content)!=1)
        BEGIN SELECT RAISE(ABORT,'local intake input unverified'); END"""),
    'submitted_sources_local_tuple': ('trigger', 'submitted_sources', """CREATE TRIGGER submitted_sources_local_tuple BEFORE UPDATE ON submitted_sources
        WHEN (OLD.binding_scope!='legacy' OR NEW.binding_scope!='legacy') AND (
          NEW.item_id IS NOT OLD.item_id OR NEW.binding_scope IS NOT OLD.binding_scope
          OR NEW.input_kind IS NOT OLD.input_kind OR NEW.input_key IS NOT OLD.input_key
          OR NEW.input_label IS NOT OLD.input_label OR NEW.input_metadata IS NOT OLD.input_metadata
          OR NEW.content IS NOT OLD.content OR NEW.retain_until IS NOT OLD.retain_until
          OR NEW.retryable IS NOT OLD.retryable)
        BEGIN SELECT RAISE(ABORT,'local intake input is immutable'); END"""),
    'submitted_sources_local_no_delete': ('trigger', 'submitted_sources', """CREATE TRIGGER submitted_sources_local_no_delete BEFORE DELETE ON submitted_sources
        WHEN OLD.binding_scope!='legacy'
        BEGIN SELECT RAISE(ABORT,'local intake input is retained'); END"""),
    'ingestion_events_local_owner': ('index', 'ingestion_events', """CREATE UNIQUE INDEX ingestion_events_local_owner ON ingestion_events(item_id)
    WHERE kind='raw_pending' AND json_extract(detail_json,'$.code')='intake_frozen'"""),
    'sqlite_sequence': ('table', 'sqlite_sequence', """CREATE TABLE sqlite_sequence(name,seq)"""),
}

_SCHEMA27_IDENTITY_DDL = {
    'source_identity_events': ('table', 'source_identity_events', """CREATE TABLE source_identity_events (
  event_id INTEGER PRIMARY KEY CHECK(typeof(event_id)='integer' AND event_id>0),
  event_key TEXT NOT NULL UNIQUE CHECK(
    typeof(event_key)='text' AND length(event_key)=64
    AND event_key NOT GLOB '*[^0-9a-f]*'),
  item_id INTEGER NOT NULL REFERENCES distill_items(item_id)
    CHECK(typeof(item_id)='integer' AND item_id>0),
  expected_prior_event_id INTEGER REFERENCES source_identity_events(event_id)
    CHECK(expected_prior_event_id IS NULL OR
      (typeof(expected_prior_event_id)='integer' AND expected_prior_event_id>0)),
  kind TEXT NOT NULL CHECK(kind IN ('model_proposal','human_accepted')),
  contract TEXT NOT NULL CHECK(contract='local-source-identity-v1'),
  source_binding_sha256 TEXT NOT NULL CHECK(
    typeof(source_binding_sha256)='text' AND length(source_binding_sha256)=64
    AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
  relation_binding_sha256 TEXT NOT NULL CHECK(
    typeof(relation_binding_sha256)='text' AND length(relation_binding_sha256)=64
    AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
  review_version TEXT NOT NULL CHECK(
    typeof(review_version)='text' AND length(review_version)=64
    AND review_version NOT GLOB '*[^0-9a-f]*'),
  detail_json TEXT NOT NULL CHECK(
    typeof(detail_json)='text' AND json_valid(detail_json)
    AND json_type(detail_json)='object'),
  created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND trim(created_at)!='')
)"""),
    'source_identity_one_root': ('index', 'source_identity_events', """CREATE UNIQUE INDEX source_identity_one_root
ON source_identity_events(item_id) WHERE expected_prior_event_id IS NULL"""),
    'source_identity_one_successor': ('index', 'source_identity_events', """CREATE UNIQUE INDEX source_identity_one_successor
ON source_identity_events(expected_prior_event_id) WHERE expected_prior_event_id IS NOT NULL"""),
    'source_identity_item_order': ('index', 'source_identity_events', """CREATE INDEX source_identity_item_order ON source_identity_events(item_id,event_id)"""),
    'source_identity_events_no_update': ('trigger', 'source_identity_events', """CREATE TRIGGER source_identity_events_no_update
BEFORE UPDATE ON source_identity_events
BEGIN SELECT RAISE(ABORT,'source identity event is immutable'); END"""),
    'source_identity_events_no_delete': ('trigger', 'source_identity_events', """CREATE TRIGGER source_identity_events_no_delete
BEFORE DELETE ON source_identity_events
BEGIN SELECT RAISE(ABORT,'source identity event is immutable'); END"""),
    'source_identity_events_append_verified': ('trigger', 'source_identity_events', """CREATE TRIGGER source_identity_events_append_verified
BEFORE INSERT ON source_identity_events
BEGIN
  SELECT CASE WHEN COALESCE(source_identity_append(
    NEW.item_id,
    json_array(NEW.event_key,NEW.item_id,NEW.expected_prior_event_id,
      NEW.kind,NEW.contract,NEW.source_binding_sha256,
      NEW.relation_binding_sha256,NEW.review_version,NEW.detail_json,NEW.created_at)
  ),0)!=1 THEN RAISE(ABORT,'source identity append proof unavailable') END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM distill_items i
    JOIN submitted_sources s ON s.item_id=i.item_id
    JOIN ingestion_events e ON e.item_id=i.item_id
    JOIN source_facts sf ON sf.material_id=i.material_id
    JOIN materials m ON m.material_id=sf.material_id
    WHERE i.item_id=NEW.item_id
      AND i.ingestion_contract='raw-verified-v1'
      AND i.source_binding_sha256=NEW.source_binding_sha256
      AND i.relation_binding_sha256=NEW.relation_binding_sha256
      AND i.submitted_url=s.input_label
      AND s.input_kind IN ('markdown','pdf','epub','direct_text')
      AND typeof(s.content)='blob'
      AND typeof(s.binding_scope)='text'
      AND substr(s.binding_scope,1,length('intake-binding-proposal-v1:'))=
          'intake-binding-proposal-v1:'
      AND length(s.binding_scope)=length('intake-binding-proposal-v1:')+64
      AND substr(s.binding_scope,length('intake-binding-proposal-v1:')+1)
          NOT GLOB '*[^0-9a-f]*'
      AND (SELECT COUNT(*) FROM submitted_sources sx WHERE sx.item_id=i.item_id)=1
      AND e.contract='raw-verified-v1'
      AND e.subject_kind='item' AND e.subject_id=i.item_id
      AND e.kind='raw_pending'
      AND json_extract(e.detail_json,'$.code')='intake_frozen'
      AND (SELECT COUNT(*) FROM ingestion_events ex WHERE ex.item_id=i.item_id
           AND ex.kind='raw_pending'
           AND json_extract(ex.detail_json,'$.code')='intake_frozen')=1
      AND (SELECT COUNT(*) FROM json_each(e.detail_json))=4
      AND (SELECT COUNT(DISTINCT key) FROM json_each(e.detail_json))=4
      AND NOT EXISTS (
        SELECT 1 FROM json_each(e.detail_json)
        WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256')
      )
      AND json_type(e.detail_json,'$.manifest')='object'
      AND (SELECT COUNT(*) FROM json_each(e.detail_json,'$.manifest'))=1
      AND json_type(e.detail_json,'$.manifest.intake_envelope_json')='text'
      AND json_extract(e.detail_json,'$.source_binding_sha256')=NEW.source_binding_sha256
      AND json_extract(e.detail_json,'$.relation_binding_sha256')=NEW.relation_binding_sha256
      AND json_type(NEW.detail_json,'$.source.item_id')='integer'
      AND json_extract(NEW.detail_json,'$.source.item_id')=i.item_id
      AND json_type(NEW.detail_json,'$.source.intake_event_id')='integer'
      AND json_extract(NEW.detail_json,'$.source.intake_event_id')=e.event_id
      AND json_type(NEW.detail_json,'$.source.intake_envelope_json')='text'
      AND json_extract(NEW.detail_json,'$.source.intake_envelope_json')=
          json_extract(e.detail_json,'$.manifest.intake_envelope_json')
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.contract')='intake-binding-proposal-v1'
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source.contract')='intake-source-v1'
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.relation.contract')='intake-relation-v1'
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source_binding_sha256')=NEW.source_binding_sha256
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.relation_binding_sha256')=NEW.relation_binding_sha256
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source.input.type')='submitted_bytes'
      AND json_type(NEW.detail_json,'$.source.input_kind')='text'
      AND json_extract(NEW.detail_json,'$.source.input_kind')=s.input_kind
      AND json_type(NEW.detail_json,'$.source.input_key')='text'
      AND json_extract(NEW.detail_json,'$.source.input_key')=s.input_key
      AND json_type(NEW.detail_json,'$.source.input_label')='text'
      AND json_extract(NEW.detail_json,'$.source.input_label')=s.input_label
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source.input.input_kind')=s.input_kind
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source.input.input_key')=s.input_key
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source.input.input_label')=s.input_label
      AND json_type(NEW.detail_json,'$.source.input_sha256')='text'
      AND json_extract(NEW.detail_json,'$.source.input_sha256')=
          json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source.input.content_sha256')
      AND json_type(NEW.detail_json,'$.source.input_byte_count')='integer'
      AND json_extract(NEW.detail_json,'$.source.input_byte_count')=length(s.content)
      AND json_extract(json_extract(e.detail_json,'$.manifest.intake_envelope_json'),
                       '$.source.input.content_byte_count')=length(s.content)
      AND json_type(NEW.detail_json,'$.review.material_id')='integer'
      AND json_extract(NEW.detail_json,'$.review.material_id')=i.material_id
      AND json_type(NEW.detail_json,'$.review.source_fact_id')='integer'
      AND json_extract(NEW.detail_json,'$.review.source_fact_id')=sf.source_fact_id
      AND json_type(NEW.detail_json,'$.review.owner_revision')='integer'
      AND json_extract(NEW.detail_json,'$.review.owner_revision')=i.review_revision
      AND (SELECT COUNT(*) FROM source_facts fx WHERE fx.material_id=i.material_id)=1
      AND (SELECT COUNT(*) FROM distill_items ix WHERE ix.material_id=i.material_id)=1
      AND NOT EXISTS (SELECT 1 FROM capture_state c WHERE c.item_id=i.item_id)
      AND NOT EXISTS (SELECT 1 FROM collection_members c
                      WHERE c.item_id=i.item_id OR c.source_fact_id=sf.source_fact_id)
      AND NOT EXISTS (SELECT 1 FROM feishu_parts f WHERE f.item_id=i.item_id)
  ) THEN RAISE(ABORT,'source identity local binding invalid') END;

  SELECT CASE WHEN NOT (
    (NEW.expected_prior_event_id IS NULL AND NOT EXISTS (
      SELECT 1 FROM source_identity_events p WHERE p.item_id=NEW.item_id
    ))
    OR (NEW.expected_prior_event_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM source_identity_events p
      WHERE p.event_id=NEW.expected_prior_event_id AND p.item_id=NEW.item_id
        AND NOT EXISTS (
          SELECT 1 FROM source_identity_events n
          WHERE n.expected_prior_event_id=p.event_id
        )
    ))
  ) THEN RAISE(ABORT,'source identity head conflict') END;

  SELECT CASE WHEN COALESCE((
    (NEW.kind='model_proposal'
      AND json_type(NEW.detail_json,'$.model')='object'
      AND json_extract(NEW.detail_json,'$.evidence.basis')='typed_model'
      AND json_type(NEW.detail_json,'$.decision.proposal_event_id')='null')
    OR (NEW.kind='human_accepted'
      AND json_type(NEW.detail_json,'$.model')='null'
      AND json_extract(NEW.detail_json,'$.evidence.basis')='explicit_user'
      AND (
        json_type(NEW.detail_json,'$.decision.proposal_event_id')='null'
        OR (json_type(NEW.detail_json,'$.decision.proposal_event_id')='integer'
          AND EXISTS (
            SELECT 1 FROM source_identity_events p
            WHERE p.event_id=json_extract(NEW.detail_json,'$.decision.proposal_event_id')
              AND p.item_id=NEW.item_id AND p.kind='model_proposal'
              AND p.contract=NEW.contract
          ))
      ))
  ),0)!=1 THEN RAISE(ABORT,'source identity proposal binding invalid') END;
END"""),
}

_SCHEMA26_TABLE_ABI = {'accepted_insight_versions': (((0, 'insight_version_id', 'INTEGER', 1, None, 1, 0),
                                (1, 'insight_id', 'INTEGER', 1, None, 0, 0),
                                (2, 'judgment_id', 'INTEGER', 1, None, 0, 0),
                                (3, 'judgment_decision', 'TEXT', 1, None, 0, 0),
                                (4, 'reconsideration_id', 'INTEGER', 0, None, 0, 0),
                                (5, 'initial_role', 'TEXT', 1, None, 0, 0),
                                (6, 'current_role', 'TEXT', 1, None, 0, 0),
                                (7, 'accepted_at', 'TEXT', 1, None, 0, 0),
                                (8, 'historical_at', 'TEXT', 0, None, 0, 0),
                                (9, 'historical_reason', 'TEXT', 0, None, 0, 0),
                                (10, 'caused_by_event_id', 'INTEGER', 0, None, 0, 0),
                                (11, 'caused_by_judgment_id', 'INTEGER', 0, None, 0, 0),
                                (12, 'replacement_insight_id', 'INTEGER', 0, None, 0, 0),
                                (13, 'disqualification_reason', 'TEXT', 0, None, 0, 0)),
                               ((0,
                                 0,
                                 'insight_identity_replacements',
                                 'insight_id',
                                 'replaced_insight_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (0,
                                 1,
                                 'insight_identity_replacements',
                                 'replacement_insight_id',
                                 'replacement_insight_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (0,
                                 2,
                                 'insight_identity_replacements',
                                 'caused_by_event_id',
                                 'event_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (1,
                                 0,
                                 'insight_version_disqualifications',
                                 'insight_version_id',
                                 'insight_version_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (1,
                                 1,
                                 'insight_version_disqualifications',
                                 'caused_by_event_id',
                                 'event_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (1,
                                 2,
                                 'insight_version_disqualifications',
                                 'disqualification_reason',
                                 'fact_kind',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (2,
                                 0,
                                 'user_insight_judgments',
                                 'insight_id',
                                 'insight_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (2,
                                 1,
                                 'user_insight_judgments',
                                 'caused_by_judgment_id',
                                 'judgment_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (3,
                                 0,
                                 'user_insight_judgments',
                                 'insight_version_id',
                                 'insight_version_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (3,
                                 1,
                                 'user_insight_judgments',
                                 'judgment_id',
                                 'judgment_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (3,
                                 2,
                                 'user_insight_judgments',
                                 'judgment_decision',
                                 'decision',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (4,
                                 0,
                                 'insight_versions',
                                 'insight_id',
                                 'insight_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (4,
                                 1,
                                 'insight_versions',
                                 'insight_version_id',
                                 'insight_version_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (5,
                                 0,
                                 'insight_reconsiderations',
                                 'insight_version_id',
                                 'insight_version_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (5,
                                 1,
                                 'insight_reconsiderations',
                                 'judgment_id',
                                 'judgment_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'),
                                (5,
                                 2,
                                 'insight_reconsiderations',
                                 'reconsideration_id',
                                 'reconsideration_id',
                                 'NO ACTION',
                                 'NO ACTION',
                                 'NONE'))),
 'capture_identity_events': (((0, 'event_id', 'INTEGER', 0, None, 1, 0),
                              (1, 'capture_id', 'INTEGER', 1, None, 0, 0),
                              (2, 'result', 'TEXT', 1, None, 0, 0),
                              (3, 'basis', 'TEXT', 1, None, 0, 0),
                              (4, 'confidence', 'REAL', 0, None, 0, 0),
                              (5, 'target_message_id', 'TEXT', 0, None, 0, 0),
                              (6, 'created_at', 'TEXT', 1, None, 0, 0)),
                             ((0,
                               0,
                               'captures',
                               'capture_id',
                               'capture_id',
                               'NO ACTION',
                               'NO ACTION',
                               'NONE'),)),
 'capture_state': (((0, 'capture_id', 'INTEGER', 0, None, 1, 0),
                    (1, 'item_id', 'INTEGER', 0, None, 0, 0),
                    (2, 'audio_path', 'TEXT', 0, None, 0, 0),
                    (3, 'audio_released_at', 'TEXT', 0, None, 0, 0)),
                   ((0, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'),
                    (1, 0, 'captures', 'capture_id', 'capture_id', 'NO ACTION', 'NO ACTION', 'NONE'))),
 'capture_transcripts': (((0, 'capture_id', 'INTEGER', 0, None, 1, 0),
                          (1, 'text', 'TEXT', 1, None, 0, 0),
                          (2, 'engine', 'TEXT', 1, None, 0, 0),
                          (3, 'model', 'TEXT', 1, None, 0, 0),
                          (4, 'version', 'TEXT', 1, None, 0, 0),
                          (5, 'chunks_json', 'TEXT', 1, None, 0, 0),
                          (6, 'created_at', 'TEXT', 1, None, 0, 0)),
                         ((0, 0, 'captures', 'capture_id', 'capture_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'captures': (((0, 'capture_id', 'INTEGER', 0, None, 1, 0),
               (1, 'app_id', 'TEXT', 1, None, 0, 0),
               (2, 'message_id', 'TEXT', 1, None, 0, 0),
               (3, 'message_type', 'TEXT', 1, None, 0, 0),
               (4, 'created_ms', 'INTEGER', 1, None, 0, 0),
               (5, 'received_ms', 'INTEGER', 1, None, 0, 0),
               (6, 'text', 'TEXT', 0, None, 0, 0),
               (7, 'file_key', 'TEXT', 0, None, 0, 0),
               (8, 'duration_ms', 'INTEGER', 0, None, 0, 0),
               (9, 'raw_id', 'TEXT', 1, None, 0, 0)),
              ()),
 'collection_confirmations': (((0, 'token', 'TEXT', 1, None, 1, 0),
                               (1, 'ordinal', 'INTEGER', 1, None, 2, 0),
                               (2, 'expected_count', 'INTEGER', 1, None, 0, 0),
                               (3, 'operation_id', 'INTEGER', 1, None, 0, 0)),
                              ((0,
                                0,
                                'collection_operations',
                                'operation_id',
                                'operation_id',
                                'NO ACTION',
                                'NO ACTION',
                                'NONE'),)),
 'collection_events': (((0, 'event_id', 'INTEGER', 0, None, 1, 0),
                        (1, 'operation_id', 'INTEGER', 1, None, 0, 0),
                        (2, 'kind', 'TEXT', 1, None, 0, 0),
                        (3, 'detail_json', 'TEXT', 1, None, 0, 0),
                        (4, 'created_at', 'TEXT', 1, None, 0, 0)),
                       ((0,
                         0,
                         'collection_operations',
                         'operation_id',
                         'operation_id',
                         'NO ACTION',
                         'NO ACTION',
                         'NONE'),)),
 'collection_members': (((0, 'operation_id', 'INTEGER', 1, None, 1, 0),
                         (1, 'ordinal', 'INTEGER', 1, None, 2, 0),
                         (2, 'native_id', 'TEXT', 1, None, 0, 0),
                         (3, 'native_version', 'TEXT', 1, None, 0, 0),
                         (4, 'item_id', 'INTEGER', 1, None, 0, 0),
                         (5, 'known_unsupported', 'INTEGER', 1, None, 0, 0),
                         (6, 'source_fact_id', 'INTEGER', 0, None, 0, 0),
                         (7, 'knowledge_result_id', 'INTEGER', 0, None, 0, 0)),
                        ((0,
                          0,
                          'knowledge_results',
                          'knowledge_result_id',
                          'knowledge_result_id',
                          'NO ACTION',
                          'NO ACTION',
                          'NONE'),
                         (1,
                          0,
                          'source_facts',
                          'source_fact_id',
                          'source_fact_id',
                          'NO ACTION',
                          'NO ACTION',
                          'NONE'),
                         (2, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'),
                         (3,
                          0,
                          'collection_operations',
                          'operation_id',
                          'operation_id',
                          'NO ACTION',
                          'NO ACTION',
                          'NONE'))),
 'collection_operations': (((0, 'operation_id', 'INTEGER', 0, None, 1, 0),
                            (1, 'kind', 'TEXT', 1, None, 0, 0),
                            (2, 'source_key', 'TEXT', 1, None, 0, 0),
                            (3, 'title', 'TEXT', 1, None, 0, 0),
                            (4, 'manifest_json', 'TEXT', 1, None, 0, 0),
                            (5, 'signature', 'TEXT', 1, None, 0, 0),
                            (6, 'content_signature', 'TEXT', 1, None, 0, 0),
                            (7, 'authority_json', 'TEXT', 1, None, 0, 0),
                            (8, 'confirmation_token', 'TEXT', 1, None, 0, 0),
                            (9, 'state', 'TEXT', 1, None, 0, 0),
                            (10, 'consequence', 'TEXT', 0, None, 0, 0),
                            (11, 'error_code', 'TEXT', 0, None, 0, 0),
                            (12, 'cancel_requested', 'INTEGER', 1, '0', 0, 0),
                            (13, 'revision', 'INTEGER', 1, '1', 0, 0),
                            (14, 'queued_at', 'TEXT', 1, None, 0, 0),
                            (15, 'created_at', 'TEXT', 1, None, 0, 0),
                            (16, 'updated_at', 'TEXT', 1, None, 0, 0),
                            (17, 'ingestion_contract', 'TEXT', 1, "'legacy'", 0, 0),
                            (18, 'source_binding_sha256', 'TEXT', 0, None, 0, 0),
                            (19, 'relation_binding_sha256', 'TEXT', 0, None, 0, 0)),
                           ()),
 'collection_previews': (((0, 'token', 'TEXT', 0, None, 1, 0),
                          (1, 'preview_json', 'TEXT', 1, None, 0, 0),
                          (2, 'expires_at', 'REAL', 0, None, 0, 0)),
                         ()),
 'collection_results': (((0, 'result_id', 'INTEGER', 0, None, 1, 0),
                         (1, 'operation_id', 'INTEGER', 1, None, 0, 0),
                         (2, 'payload_json', 'TEXT', 1, None, 0, 0),
                         (3, 'lineage_json', 'TEXT', 1, None, 0, 0),
                         (4, 'created_at', 'TEXT', 1, None, 0, 0)),
                        ((0,
                          0,
                          'collection_operations',
                          'operation_id',
                          'operation_id',
                          'NO ACTION',
                          'NO ACTION',
                          'NONE'),)),
 'confirmation_decisions': (((0, 'item_id', 'INTEGER', 1, None, 1, 0),
                             (1, 'revision', 'TEXT', 1, None, 2, 0),
                             (2, 'action', 'TEXT', 1, None, 0, 0),
                             (3, 'value', 'TEXT', 1, None, 0, 0),
                             (4, 'state', 'TEXT', 1, None, 0, 0)),
                            ((0,
                              0,
                              'distill_items',
                              'item_id',
                              'item_id',
                              'NO ACTION',
                              'NO ACTION',
                              'NONE'),)),
 'delivery_adjacency': (((0, 'app_id', 'TEXT', 1, None, 1, 0),
                         (1, 'message_id', 'TEXT', 1, None, 2, 0),
                         (2, 'earlier_message_id', 'TEXT', 1, None, 3, 0),
                         (3, 'gap_seconds', 'INTEGER', 1, None, 0, 0)),
                        ()),
 'distill_items': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                    (1, 'submitted_url', 'TEXT', 1, None, 0, 0),
                    (2, 'state', 'TEXT', 1, None, 0, 0),
                    (3, 'phase', 'TEXT', 1, None, 0, 0),
                    (4, 'material_id', 'INTEGER', 0, None, 0, 0),
                    (5, 'error_code', 'TEXT', 0, None, 0, 0),
                    (6, 'rejection_reason', 'TEXT', 0, None, 0, 0),
                    (7, 'dismissed_at', 'TEXT', 0, None, 0, 0),
                    (8, 'confirmation_json', 'TEXT', 0, None, 0, 0),
                    (9, 'queued_at', 'TEXT', 1, None, 0, 0),
                    (10, 'created_at', 'TEXT', 1, None, 0, 0),
                    (11, 'updated_at', 'TEXT', 1, None, 0, 0),
                    (12, 'platform_authority_json', 'TEXT', 1, "'{}'", 0, 0),
                    (13, 'submitted_title', 'TEXT', 1, "''", 0, 0),
                    (14, 'review_revision', 'INTEGER', 1, '0', 0, 0),
                    (15, 'ingestion_contract', 'TEXT', 1, "'legacy'", 0, 0),
                    (16, 'source_binding_sha256', 'TEXT', 0, None, 0, 0),
                    (17, 'relation_binding_sha256', 'TEXT', 0, None, 0, 0)),
                   ((0, 0, 'materials', 'material_id', 'material_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'feishu_action_queue': (((0, 'id', 'INTEGER', 0, None, 1, 0),
                          (1, 'action_key', 'TEXT', 1, None, 0, 0),
                          (2, 'app_id', 'TEXT', 1, None, 0, 0),
                          (3, 'message_id', 'TEXT', 1, None, 0, 0),
                          (4, 'payload', 'TEXT', 1, None, 0, 0),
                          (5, 'result', 'TEXT', 0, None, 0, 0)),
                         ()),
 'feishu_binding': (((0, 'app_id', 'TEXT', 0, None, 1, 0),
                     (1, 'bot_open_id', 'TEXT', 1, None, 0, 0),
                     (2, 'user_open_id', 'TEXT', 1, None, 0, 0),
                     (3, 'chat_id', 'TEXT', 1, None, 0, 0),
                     (4, 'start_ms', 'INTEGER', 1, None, 0, 0),
                     (5, 'history_until_ms', 'INTEGER', 1, None, 0, 0)),
                    ()),
 'feishu_parts': (((0, 'app_id', 'TEXT', 1, None, 1, 0),
                   (1, 'message_id', 'TEXT', 1, None, 2, 0),
                   (2, 'position', 'INTEGER', 1, None, 3, 0),
                   (3, 'item_id', 'INTEGER', 0, None, 0, 0),
                   (4, 'error', 'TEXT', 0, None, 0, 0),
                   (5, 'preview_json', 'TEXT', 0, None, 0, 0)),
                  ((0, 0, 'feishu_receipts', 'app_id', 'app_id', 'NO ACTION', 'NO ACTION', 'NONE'),
                   (0, 1, 'feishu_receipts', 'message_id', 'message_id', 'NO ACTION', 'NO ACTION', 'NONE'),
                   (1, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'))),
 'feishu_receipts': (((0, 'app_id', 'TEXT', 1, None, 1, 0),
                      (1, 'message_id', 'TEXT', 1, None, 2, 0),
                      (2, 'created_ms', 'INTEGER', 1, None, 0, 0),
                      (3, 'raw_json', 'TEXT', 1, None, 0, 0),
                      (4, 'text', 'TEXT', 1, None, 0, 0),
                      (5, 'same_topic', 'INTEGER', 1, None, 0, 0),
                      (6, 'content_kind', 'TEXT', 0, None, 0, 0),
                      (7, 'state', 'TEXT', 1, None, 0, 0),
                      (8, 'error', 'TEXT', 0, None, 0, 0),
                      (9, 'card_id', 'TEXT', 0, None, 0, 0),
                      (10, 'card_signature', 'TEXT', 0, None, 0, 0),
                      (11, 'card_attempted_ms', 'INTEGER', 0, None, 0, 0)),
                     ((0, 0, 'feishu_binding', 'app_id', 'app_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'group_decisions': (((0, 'item_id', 'INTEGER', 1, None, 1, 0),
                      (1, 'request_id', 'TEXT', 1, None, 2, 0),
                      (2, 'group_id', 'TEXT', 1, None, 0, 0),
                      (3, 'submitted_revision', 'TEXT', 1, None, 0, 0),
                      (4, 'selection_digest', 'TEXT', 1, None, 0, 0),
                      (5, 'payload_digest', 'TEXT', 1, None, 0, 0),
                      (6, 'result_json', 'TEXT', 1, None, 0, 0),
                      (7, 'audit_json', 'TEXT', 1, None, 0, 0),
                      (8, 'committed_at', 'TEXT', 1, None, 0, 0)),
                     ((0, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'insight_identities': (((0, 'insight_id', 'INTEGER', 1, None, 1, 0),
                         (1, 'created_event_id', 'INTEGER', 1, None, 0, 0),
                         (2, 'created_at', 'TEXT', 1, None, 0, 0)),
                        ((0,
                          0,
                          'organization_events',
                          'created_event_id',
                          'event_id',
                          'NO ACTION',
                          'NO ACTION',
                          'NONE'),)),
 'insight_identity_replacements': (((0, 'replaced_insight_id', 'INTEGER', 1, None, 1, 0),
                                    (1, 'replacement_insight_id', 'INTEGER', 1, None, 0, 0),
                                    (2, 'event_id', 'INTEGER', 1, None, 0, 0),
                                    (3, 'reason_text', 'TEXT', 1, None, 0, 0),
                                    (4, 'created_at', 'TEXT', 1, None, 0, 0)),
                                   ((0,
                                     0,
                                     'organization_events',
                                     'event_id',
                                     'event_id',
                                     'NO ACTION',
                                     'NO ACTION',
                                     'NONE'),
                                    (1,
                                     0,
                                     'insight_identities',
                                     'replacement_insight_id',
                                     'insight_id',
                                     'NO ACTION',
                                     'NO ACTION',
                                     'NONE'),
                                    (2,
                                     0,
                                     'insight_identities',
                                     'replaced_insight_id',
                                     'insight_id',
                                     'NO ACTION',
                                     'NO ACTION',
                                     'NONE'))),
 'insight_reconsiderations': (((0, 'reconsideration_id', 'INTEGER', 0, None, 1, 0),
                               (1, 'insight_version_id', 'INTEGER', 1, None, 0, 0),
                               (2, 'judgment_id', 'INTEGER', 1, None, 0, 0),
                               (3, 'operation_id', 'TEXT', 1, None, 0, 0),
                               (4, 'decision', 'TEXT', 1, None, 0, 0),
                               (5, 'reconsidered_at', 'TEXT', 1, None, 0, 0)),
                              ((0,
                                0,
                                'user_insight_judgments',
                                'judgment_id',
                                'judgment_id',
                                'NO ACTION',
                                'NO ACTION',
                                'NONE'),
                               (1,
                                0,
                                'insight_versions',
                                'insight_version_id',
                                'insight_version_id',
                                'NO ACTION',
                                'NO ACTION',
                                'NONE'))),
 'insight_version_disqualifications': (((0, 'disqualification_id', 'INTEGER', 1, None, 1, 0),
                                        (1, 'insight_version_id', 'INTEGER', 1, None, 0, 0),
                                        (2, 'fact_kind', 'TEXT', 1, None, 0, 0),
                                        (3, 'event_id', 'INTEGER', 1, None, 0, 0),
                                        (4, 'reason_text', 'TEXT', 1, None, 0, 0),
                                        (5, 'created_at', 'TEXT', 1, None, 0, 0)),
                                       ((0,
                                         0,
                                         'organization_events',
                                         'event_id',
                                         'event_id',
                                         'NO ACTION',
                                         'NO ACTION',
                                         'NONE'),
                                        (1,
                                         0,
                                         'insight_versions',
                                         'insight_version_id',
                                         'insight_version_id',
                                         'NO ACTION',
                                         'NO ACTION',
                                         'NONE'))),
 'insight_version_participants': (((0, 'insight_version_id', 'INTEGER', 1, None, 1, 0),
                                   (1, 'participant_key', 'TEXT', 1, None, 2, 0),
                                   (2, 'input_kind', 'TEXT', 1, None, 0, 0),
                                   (3, 'knowledge_result_id', 'INTEGER', 0, None, 0, 0),
                                   (4, 'point_id', 'TEXT', 0, None, 0, 0),
                                   (5, 'accepted_insight_version_id', 'INTEGER', 0, None, 0, 0),
                                   (6, 'position', 'INTEGER', 1, None, 0, 0),
                                   (7, 'contribution_text', 'TEXT', 1, None, 0, 0)),
                                  ((0,
                                    0,
                                    'accepted_insight_versions',
                                    'accepted_insight_version_id',
                                    'insight_version_id',
                                    'NO ACTION',
                                    'NO ACTION',
                                    'NONE'),
                                   (1,
                                    0,
                                    'knowledge_results',
                                    'knowledge_result_id',
                                    'knowledge_result_id',
                                    'NO ACTION',
                                    'NO ACTION',
                                    'NONE'),
                                   (2,
                                    0,
                                    'insight_versions',
                                    'insight_version_id',
                                    'insight_version_id',
                                    'NO ACTION',
                                    'NO ACTION',
                                    'NONE'))),
 'insight_version_used_relations': (((0, 'insight_version_id', 'INTEGER', 1, None, 1, 0),
                                     (1, 'relation_version_id', 'INTEGER', 1, None, 2, 0),
                                     (2, 'position', 'INTEGER', 1, None, 0, 0),
                                     (3, 'role_text', 'TEXT', 1, None, 0, 0)),
                                    ((0,
                                      0,
                                      'relation_versions',
                                      'relation_version_id',
                                      'relation_version_id',
                                      'NO ACTION',
                                      'NO ACTION',
                                      'NONE'),
                                     (1,
                                      0,
                                      'insight_versions',
                                      'insight_version_id',
                                      'insight_version_id',
                                      'NO ACTION',
                                      'NO ACTION',
                                      'NONE'))),
 'insight_versions': (((0, 'insight_version_id', 'INTEGER', 1, None, 1, 0),
                       (1, 'insight_id', 'INTEGER', 1, None, 0, 0),
                       (2, 'version_no', 'INTEGER', 1, None, 0, 0),
                       (3, 'previous_version_id', 'INTEGER', 0, None, 0, 0),
                       (4, 'produced_event_id', 'INTEGER', 1, None, 0, 0),
                       (5, 'payload_json', 'TEXT', 1, None, 0, 0),
                       (6, 'semantic_signature', 'TEXT', 1, None, 0, 0),
                       (7, 'dependency_signature', 'TEXT', 1, None, 0, 0),
                       (8, 'created_at', 'TEXT', 1, None, 0, 0)),
                      ((0,
                        0,
                        'organization_events',
                        'produced_event_id',
                        'event_id',
                        'NO ACTION',
                        'NO ACTION',
                        'NONE'),
                       (1,
                        0,
                        'insight_versions',
                        'insight_id',
                        'insight_id',
                        'NO ACTION',
                        'NO ACTION',
                        'NONE'),
                       (1,
                        1,
                        'insight_versions',
                        'previous_version_id',
                        'insight_version_id',
                        'NO ACTION',
                        'NO ACTION',
                        'NONE'),
                       (2,
                        0,
                        'insight_identities',
                        'insight_id',
                        'insight_id',
                        'NO ACTION',
                        'NO ACTION',
                        'NONE'))),
 'knowledge_results': (((0, 'knowledge_result_id', 'INTEGER', 0, None, 1, 0),
                        (1, 'source_fact_id', 'INTEGER', 1, None, 0, 0),
                        (2, 'payload_json', 'TEXT', 1, None, 0, 0),
                        (3, 'published_path', 'TEXT', 0, None, 0, 0),
                        (4, 'published_vault', 'TEXT', 0, None, 0, 0),
                        (5, 'published_at', 'TEXT', 0, None, 0, 0),
                        (6, 'created_at', 'TEXT', 1, None, 0, 0)),
                       ((0,
                         0,
                         'source_facts',
                         'source_fact_id',
                         'source_fact_id',
                         'NO ACTION',
                         'NO ACTION',
                         'NONE'),)),
 'manual_cards': (((0, 'enqueue_seq', 'INTEGER', 0, None, 1, 0),
                   (1, 'scope_kind', 'TEXT', 1, None, 0, 0),
                   (2, 'scope_id', 'TEXT', 1, None, 0, 0),
                   (3, 'item_id', 'INTEGER', 1, None, 0, 0),
                   (4, 'review_round_id', 'TEXT', 1, None, 0, 0),
                   (5, 'group_id', 'TEXT', 1, None, 0, 0),
                   (6, 'lifecycle', 'TEXT', 1, None, 0, 0),
                   (7, 'ordering_basis', 'TEXT', 1, None, 0, 0),
                   (8, 'ordering_reason', 'TEXT', 1, None, 0, 0),
                   (9, 'entered_at', 'TEXT', 1, None, 0, 0),
                   (10, 'mapping_json', 'TEXT', 1, None, 0, 0)),
                  ((0, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'materials': (((0, 'material_id', 'INTEGER', 0, None, 1, 0),
                (1, 'source_kind', 'TEXT', 1, None, 0, 0),
                (2, 'source_key', 'TEXT', 1, None, 0, 0),
                (3, 'submitted_url', 'TEXT', 1, None, 0, 0),
                (4, 'canonical_url', 'TEXT', 1, None, 0, 0),
                (5, 'metadata_json', 'TEXT', 1, None, 0, 0),
                (6, 'created_at', 'TEXT', 1, None, 0, 0),
                (7, 'snapshot_key', 'TEXT', 1, "'legacy'", 0, 0)),
               ()),
 'media_lifecycle': (((0, 'singleton', 'INTEGER', 0, None, 1, 0),
                      (1, 'legacy_material_id', 'INTEGER', 1, None, 0, 0),
                      (2, 'released_bytes', 'INTEGER', 1, '0', 0, 0),
                      (3, 'compacted_bytes', 'INTEGER', 1, '0', 0, 0)),
                     ()),
 'organization_event_accepted_boundary': (((0, 'event_id', 'INTEGER', 1, None, 1, 0),
                                           (1, 'insight_version_id', 'INTEGER', 1, None, 2, 0),
                                           (2, 'position', 'INTEGER', 1, None, 0, 0),
                                           (3, 'qualification_signature', 'TEXT', 1, None, 0, 0)),
                                          ((0,
                                            0,
                                            'accepted_insight_versions',
                                            'insight_version_id',
                                            'insight_version_id',
                                            'NO ACTION',
                                            'NO ACTION',
                                            'NONE'),
                                           (1,
                                            0,
                                            'organization_events',
                                            'event_id',
                                            'event_id',
                                            'NO ACTION',
                                            'NO ACTION',
                                            'NONE'))),
 'organization_event_coverages': (((0, 'event_id', 'INTEGER', 1, None, 1, 0),
                                   (1, 'knowledge_result_id', 'INTEGER', 1, None, 2, 0),
                                   (2, 'source_fact_id', 'INTEGER', 1, None, 0, 0),
                                   (3, 'covered_at', 'TEXT', 1, None, 0, 0)),
                                  ((0,
                                    0,
                                    'knowledge_results',
                                    'knowledge_result_id',
                                    'knowledge_result_id',
                                    'NO ACTION',
                                    'NO ACTION',
                                    'NONE'),
                                   (0,
                                    1,
                                    'knowledge_results',
                                    'source_fact_id',
                                    'source_fact_id',
                                    'NO ACTION',
                                    'NO ACTION',
                                    'NONE'),
                                   (1,
                                    0,
                                    'organization_events',
                                    'event_id',
                                    'event_id',
                                    'NO ACTION',
                                    'NO ACTION',
                                    'NONE'))),
 'organization_event_relation_boundary': (((0, 'event_id', 'INTEGER', 1, None, 1, 0),
                                           (1, 'relation_version_id', 'INTEGER', 1, None, 2, 0),
                                           (2, 'boundary_role', 'TEXT', 1, None, 0, 0),
                                           (3, 'position', 'INTEGER', 1, None, 0, 0),
                                           (4, 'qualification_signature', 'TEXT', 1, None, 0, 0)),
                                          ((0,
                                            0,
                                            'relation_versions',
                                            'relation_version_id',
                                            'relation_version_id',
                                            'NO ACTION',
                                            'NO ACTION',
                                            'NONE'),
                                           (1,
                                            0,
                                            'organization_events',
                                            'event_id',
                                            'event_id',
                                            'NO ACTION',
                                            'NO ACTION',
                                            'NONE'))),
 'organization_event_source_boundary': (((0, 'event_id', 'INTEGER', 1, None, 1, 0),
                                         (1, 'knowledge_result_id', 'INTEGER', 1, None, 2, 0),
                                         (2, 'source_fact_id', 'INTEGER', 1, None, 0, 0),
                                         (3, 'boundary_role', 'TEXT', 1, None, 0, 0),
                                         (4, 'position', 'INTEGER', 1, None, 0, 0),
                                         (5, 'qualification_signature', 'TEXT', 1, None, 0, 0)),
                                        ((0,
                                          0,
                                          'knowledge_results',
                                          'knowledge_result_id',
                                          'knowledge_result_id',
                                          'NO ACTION',
                                          'NO ACTION',
                                          'NONE'),
                                         (0,
                                          1,
                                          'knowledge_results',
                                          'source_fact_id',
                                          'source_fact_id',
                                          'NO ACTION',
                                          'NO ACTION',
                                          'NONE'),
                                         (1,
                                          0,
                                          'organization_events',
                                          'event_id',
                                          'event_id',
                                          'NO ACTION',
                                          'NO ACTION',
                                          'NONE'))),
 'organization_events': (((0, 'event_id', 'INTEGER', 1, None, 1, 0),
                          (1, 'memory_scope', 'INTEGER', 1, '1', 0, 0),
                          (2, 'status', 'TEXT', 1, None, 0, 0),
                          (3, 'started_at', 'TEXT', 1, None, 0, 0),
                          (4, 'completed_at', 'TEXT', 0, None, 0, 0),
                          (5, 'failure_code', 'TEXT', 0, None, 0, 0),
                          (6, 'topic_before_json', 'TEXT', 1, None, 0, 0),
                          (7, 'topic_before_signature', 'TEXT', 1, None, 0, 0),
                          (8, 'topic_guard_signature', 'TEXT', 1, None, 0, 0),
                          (9, 'boundary_signature', 'TEXT', 1, None, 0, 0),
                          (10, 'success_payload_json', 'TEXT', 0, None, 0, 0)),
                         ()),
 'personal_cognition_entries': (((0, 'entry_id', 'INTEGER', 0, None, 1, 0),
                                 (1, 'insight_version_id', 'INTEGER', 1, None, 0, 0),
                                 (2, 'judgment_id', 'INTEGER', 1, None, 0, 0),
                                 (3, 'reconsideration_id', 'INTEGER', 0, None, 0, 0),
                                 (4, 'entry_kind', 'TEXT', 1, None, 0, 0),
                                 (5, 'text', 'TEXT', 1, None, 0, 0),
                                 (6, 'operation_id', 'TEXT', 1, None, 0, 0),
                                 (7, 'created_at', 'TEXT', 1, None, 0, 0)),
                                ((0,
                                  0,
                                  'insight_reconsiderations',
                                  'reconsideration_id',
                                  'reconsideration_id',
                                  'NO ACTION',
                                  'NO ACTION',
                                  'NONE'),
                                 (1,
                                  0,
                                  'user_insight_judgments',
                                  'judgment_id',
                                  'judgment_id',
                                  'NO ACTION',
                                  'NO ACTION',
                                  'NONE'),
                                 (2,
                                  0,
                                  'insight_versions',
                                  'insight_version_id',
                                  'insight_version_id',
                                  'NO ACTION',
                                  'NO ACTION',
                                  'NONE'))),
 'raw_counters': (((0, 'day', 'TEXT', 0, None, 1, 0), (1, 'last', 'INTEGER', 1, None, 0, 0)), ()),
 'raw_records': (((0, 'raw_id', 'TEXT', 0, None, 1, 0),
                  (1, 'subject_kind', 'TEXT', 1, None, 0, 0),
                  (2, 'subject_id', 'INTEGER', 1, None, 0, 0),
                  (3, 'identity', 'TEXT', 1, None, 0, 0),
                  (4, 'relative_path', 'TEXT', 1, None, 0, 0),
                  (5, 'content', 'TEXT', 1, None, 0, 0),
                  (6, 'content_sha256', 'TEXT', 1, None, 0, 0),
                  (7, 'attachments_json', 'TEXT', 1, "'[]'", 0, 0),
                  (8, 'supersedes', 'TEXT', 0, None, 0, 0),
                  (9, 'origin', 'TEXT', 1, None, 0, 0),
                  (10, 'created_at', 'TEXT', 1, None, 0, 0),
                  (11, 'written_at', 'TEXT', 0, None, 0, 0),
                  (12, 'written_vault', 'TEXT', 0, None, 0, 0),
                  (13, 'attempts', 'INTEGER', 1, '0', 0, 0),
                  (14, 'last_error', 'TEXT', 0, None, 0, 0)),
                 ()),
 'relation_current': (((0, 'relation_id', 'INTEGER', 1, None, 1, 0),
                       (1, 'relation_version_id', 'INTEGER', 1, None, 0, 0),
                       (2, 'activated_at', 'TEXT', 1, None, 0, 0)),
                      ((0,
                        0,
                        'relation_versions',
                        'relation_id',
                        'relation_id',
                        'NO ACTION',
                        'NO ACTION',
                        'NONE'),
                       (0,
                        1,
                        'relation_versions',
                        'relation_version_id',
                        'relation_version_id',
                        'NO ACTION',
                        'NO ACTION',
                        'NONE'))),
 'relation_facts': (((0, 'relation_fact_id', 'INTEGER', 1, None, 1, 0),
                     (1, 'event_id', 'INTEGER', 1, None, 0, 0),
                     (2, 'relation_id', 'INTEGER', 1, None, 0, 0),
                     (3, 'relation_version_id', 'INTEGER', 1, None, 0, 0),
                     (4, 'fact_kind', 'TEXT', 1, None, 0, 0),
                     (5, 'successor_relation_version_id', 'INTEGER', 0, None, 0, 0),
                     (6, 'replacement_relation_id', 'INTEGER', 0, None, 0, 0),
                     (7, 'reason_text', 'TEXT', 1, None, 0, 0),
                     (8, 'created_at', 'TEXT', 1, None, 0, 0)),
                    ((0,
                      0,
                      'relation_identities',
                      'replacement_relation_id',
                      'relation_id',
                      'NO ACTION',
                      'NO ACTION',
                      'NONE'),
                     (1,
                      0,
                      'relation_versions',
                      'relation_id',
                      'relation_id',
                      'NO ACTION',
                      'NO ACTION',
                      'NONE'),
                     (1,
                      1,
                      'relation_versions',
                      'successor_relation_version_id',
                      'relation_version_id',
                      'NO ACTION',
                      'NO ACTION',
                      'NONE'),
                     (2,
                      0,
                      'relation_versions',
                      'relation_id',
                      'relation_id',
                      'NO ACTION',
                      'NO ACTION',
                      'NONE'),
                     (2,
                      1,
                      'relation_versions',
                      'relation_version_id',
                      'relation_version_id',
                      'NO ACTION',
                      'NO ACTION',
                      'NONE'),
                     (3,
                      0,
                      'organization_events',
                      'event_id',
                      'event_id',
                      'NO ACTION',
                      'NO ACTION',
                      'NONE'))),
 'relation_identities': (((0, 'relation_id', 'INTEGER', 1, None, 1, 0),
                          (1, 'created_event_id', 'INTEGER', 1, None, 0, 0),
                          (2, 'created_at', 'TEXT', 1, None, 0, 0)),
                         ((0,
                           0,
                           'organization_events',
                           'created_event_id',
                           'event_id',
                           'NO ACTION',
                           'NO ACTION',
                           'NONE'),)),
 'relation_version_participants': (((0, 'relation_version_id', 'INTEGER', 1, None, 1, 0),
                                    (1, 'participant_key', 'TEXT', 1, None, 2, 0),
                                    (2, 'input_kind', 'TEXT', 1, None, 0, 0),
                                    (3, 'knowledge_result_id', 'INTEGER', 0, None, 0, 0),
                                    (4, 'point_id', 'TEXT', 0, None, 0, 0),
                                    (5, 'accepted_insight_version_id', 'INTEGER', 0, None, 0, 0),
                                    (6, 'position', 'INTEGER', 1, None, 0, 0),
                                    (7, 'contribution_text', 'TEXT', 1, None, 0, 0)),
                                   ((0,
                                     0,
                                     'accepted_insight_versions',
                                     'accepted_insight_version_id',
                                     'insight_version_id',
                                     'NO ACTION',
                                     'NO ACTION',
                                     'NONE'),
                                    (1,
                                     0,
                                     'knowledge_results',
                                     'knowledge_result_id',
                                     'knowledge_result_id',
                                     'NO ACTION',
                                     'NO ACTION',
                                     'NONE'),
                                    (2,
                                     0,
                                     'relation_versions',
                                     'relation_version_id',
                                     'relation_version_id',
                                     'NO ACTION',
                                     'NO ACTION',
                                     'NONE'))),
 'relation_version_used_relations': (((0, 'relation_version_id', 'INTEGER', 1, None, 1, 0),
                                      (1, 'used_relation_version_id', 'INTEGER', 1, None, 2, 0),
                                      (2, 'position', 'INTEGER', 1, None, 0, 0),
                                      (3, 'role_text', 'TEXT', 1, None, 0, 0)),
                                     ((0,
                                       0,
                                       'relation_versions',
                                       'used_relation_version_id',
                                       'relation_version_id',
                                       'NO ACTION',
                                       'NO ACTION',
                                       'NONE'),
                                      (1,
                                       0,
                                       'relation_versions',
                                       'relation_version_id',
                                       'relation_version_id',
                                       'NO ACTION',
                                       'NO ACTION',
                                       'NONE'))),
 'relation_versions': (((0, 'relation_version_id', 'INTEGER', 1, None, 1, 0),
                        (1, 'relation_id', 'INTEGER', 1, None, 0, 0),
                        (2, 'version_no', 'INTEGER', 1, None, 0, 0),
                        (3, 'previous_version_id', 'INTEGER', 0, None, 0, 0),
                        (4, 'produced_event_id', 'INTEGER', 1, None, 0, 0),
                        (5, 'payload_json', 'TEXT', 1, None, 0, 0),
                        (6, 'semantic_signature', 'TEXT', 1, None, 0, 0),
                        (7, 'dependency_signature', 'TEXT', 1, None, 0, 0),
                        (8, 'created_at', 'TEXT', 1, None, 0, 0)),
                       ((0,
                         0,
                         'organization_events',
                         'produced_event_id',
                         'event_id',
                         'NO ACTION',
                         'NO ACTION',
                         'NONE'),
                        (1,
                         0,
                         'relation_versions',
                         'relation_id',
                         'relation_id',
                         'NO ACTION',
                         'NO ACTION',
                         'NONE'),
                        (1,
                         1,
                         'relation_versions',
                         'previous_version_id',
                         'relation_version_id',
                         'NO ACTION',
                         'NO ACTION',
                         'NONE'),
                        (2,
                         0,
                         'relation_identities',
                         'relation_id',
                         'relation_id',
                         'NO ACTION',
                         'NO ACTION',
                         'NONE'))),
 'settings': (((0, 'key', 'TEXT', 0, None, 1, 0), (1, 'value', 'TEXT', 1, None, 0, 0)), ()),
 'source_connections': (((0, 'platform', 'TEXT', 0, None, 1, 0),
                         (1, 'state', 'TEXT', 1, None, 0, 0),
                         (2, 'generation', 'INTEGER', 1, None, 0, 0),
                         (3, 'account_label', 'TEXT', 0, None, 0, 0),
                         (4, 'connected_at', 'TEXT', 1, None, 0, 0),
                         (5, 'browser_context', 'TEXT', 0, None, 0, 0)),
                        ()),
 'source_facts': (((0, 'source_fact_id', 'INTEGER', 0, None, 1, 0),
                   (1, 'material_id', 'INTEGER', 1, None, 0, 0),
                   (2, 'snapshot', 'TEXT', 1, None, 0, 0),
                   (3, 'uncertainties_json', 'TEXT', 1, None, 0, 0),
                   (4, 'lineage_json', 'TEXT', 1, "'{}'", 0, 0),
                   (5, 'created_at', 'TEXT', 1, None, 0, 0)),
                  ((0, 0, 'materials', 'material_id', 'material_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'source_media': (((0, 'material_id', 'INTEGER', 1, None, 1, 0),
                   (1, 'member_id', 'TEXT', 1, None, 2, 0),
                   (2, 'position', 'INTEGER', 1, None, 0, 0),
                   (3, 'mime_type', 'TEXT', 1, None, 0, 0),
                   (4, 'sha256', 'TEXT', 1, None, 0, 0),
                   (5, 'content', 'BLOB', 1, None, 0, 0)),
                  ((0, 0, 'materials', 'material_id', 'material_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'source_review_results': (((0, 'item_id', 'INTEGER', 1, None, 1, 0),
                            (1, 'revision', 'INTEGER', 1, None, 2, 0),
                            (2, 'identity', 'TEXT', 1, None, 0, 0),
                            (3, 'status', 'TEXT', 1, None, 0, 0),
                            (4, 'result_json', 'TEXT', 1, None, 0, 0),
                            (5, 'created_at', 'TEXT', 1, None, 0, 0)),
                           ((0,
                             0,
                             'distill_items',
                             'item_id',
                             'item_id',
                             'NO ACTION',
                             'NO ACTION',
                             'NONE'),)),
 'submitted_sources': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                        (1, 'input_kind', 'TEXT', 1, None, 0, 0),
                        (2, 'input_key', 'TEXT', 1, None, 0, 0),
                        (3, 'input_label', 'TEXT', 1, None, 0, 0),
                        (4, 'input_metadata', 'TEXT', 1, None, 0, 0),
                        (5, 'content', 'BLOB', 0, None, 0, 0),
                        (6, 'retain_until', 'TEXT', 0, None, 0, 0),
                        (7, 'retryable', 'INTEGER', 1, '1', 0, 0),
                        (8, 'binding_scope', 'TEXT', 1, "'legacy'", 0, 0)),
                       ((0, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'topic_entries': (((0, 'topic_id', 'INTEGER', 0, None, 1, 0),
                    (1, 'name', 'TEXT', 1, None, 0, 0),
                    (2, 'scope', 'TEXT', 1, None, 0, 0),
                    (3, 'updated_at', 'TEXT', 1, None, 0, 0)),
                   ()),
 'topic_members': (((0, 'topic_id', 'INTEGER', 1, None, 1, 0),
                    (1, 'position', 'INTEGER', 1, None, 2, 0),
                    (2, 'knowledge_result_id', 'INTEGER', 1, None, 0, 0),
                    (3, 'point_id', 'TEXT', 1, None, 0, 0)),
                   ((0,
                     0,
                     'knowledge_results',
                     'knowledge_result_id',
                     'knowledge_result_id',
                     'NO ACTION',
                     'NO ACTION',
                     'NONE'),
                    (1, 0, 'topic_entries', 'topic_id', 'topic_id', 'NO ACTION', 'CASCADE', 'NONE'))),
 'topic_snapshot': (((0, 'singleton', 'INTEGER', 0, None, 1, 0),
                     (1, 'knowledge_count', 'INTEGER', 1, None, 0, 0),
                     (2, 'input_signature', 'TEXT', 1, None, 0, 0)),
                    ()),
 'user_insight_judgments': (((0, 'judgment_id', 'INTEGER', 1, None, 1, 0),
                             (1, 'insight_id', 'INTEGER', 1, None, 0, 0),
                             (2, 'insight_version_id', 'INTEGER', 1, None, 0, 0),
                             (3, 'decision', 'TEXT', 1, None, 0, 0),
                             (4, 'annotation_text', 'TEXT', 0, None, 0, 0),
                             (5, 'decided_at', 'TEXT', 1, None, 0, 0)),
                            ((0,
                              0,
                              'insight_versions',
                              'insight_id',
                              'insight_id',
                              'NO ACTION',
                              'NO ACTION',
                              'NONE'),
                             (0,
                              1,
                              'insight_versions',
                              'insight_version_id',
                              'insight_version_id',
                              'NO ACTION',
                              'NO ACTION',
                              'NONE'))),
 'wiki_tasks': (((0, 'task_id', 'TEXT', 0, None, 1, 0),
                 (1, 'vault_path', 'TEXT', 1, None, 0, 0),
                 (2, 'vault_key', 'TEXT', 1, None, 0, 0),
                 (3, 'request_kind', 'TEXT', 1, None, 0, 0),
                 (4, 'trigger_source', 'TEXT', 1, None, 0, 0),
                 (5, 'backend', 'TEXT', 1, None, 0, 0),
                 (6, 'model', 'TEXT', 1, None, 0, 0),
                 (7, 'effort', 'TEXT', 1, None, 0, 0),
                 (8, 'kit_version', 'TEXT', 1, None, 0, 0),
                 (9, 'kit_manifest_sha256', 'TEXT', 1, None, 0, 0),
                 (10, 'boundary_sha256', 'TEXT', 1, None, 0, 0),
                 (11, 'state', 'TEXT', 1, None, 0, 0),
                 (12, 'raw_count', 'INTEGER', 1, None, 0, 0),
                 (13, 'batch_count', 'INTEGER', 1, None, 0, 0),
                 (14, 'completed_batch_count', 'INTEGER', 1, '0', 0, 0),
                 (15, 'error_code', 'TEXT', 0, None, 0, 0),
                 (16, 'recovery_state', 'TEXT', 1, "'not_needed'", 0, 0),
                 (17, 'recovery_phase', 'TEXT', 1, "'none'", 0, 0),
                 (18, 'created_at', 'TEXT', 1, None, 0, 0),
                 (19, 'updated_at', 'TEXT', 1, None, 0, 0),
                 (20, 'outcome_contract', 'TEXT', 1, "'legacy'", 0, 0),
                 (21, 'plan_json', 'TEXT', 1, "'{}'", 0, 0),
                 (22, 'plan_sha256', 'TEXT', 0, None, 0, 0)),
                ()),
 'wiki_task_batches': (((0, 'task_id', 'TEXT', 1, None, 1, 0),
                        (1, 'batch_no', 'INTEGER', 1, None, 2, 0),
                        (2, 'state', 'TEXT', 1, None, 0, 0),
                        (3, 'item_count', 'INTEGER', 1, None, 0, 0),
                        (4, 'error_code', 'TEXT', 0, None, 0, 0)),
                       ((0, 0, 'wiki_tasks', 'task_id', 'task_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'wiki_task_raw': (((0, 'task_id', 'TEXT', 1, None, 1, 0),
                    (1, 'ordinal', 'INTEGER', 1, None, 2, 0),
                    (2, 'batch_no', 'INTEGER', 1, None, 0, 0),
                    (3, 'raw_id', 'TEXT', 1, None, 0, 0),
                    (4, 'identity', 'TEXT', 1, None, 0, 0),
                    (5, 'relative_path', 'TEXT', 1, None, 0, 0),
                    (6, 'byte_count', 'INTEGER', 1, None, 0, 0),
                    (7, 'content_sha256', 'TEXT', 1, None, 0, 0)),
                   ((0, 0, 'wiki_task_batches', 'task_id', 'task_id', 'NO ACTION', 'NO ACTION', 'NONE'),
                    (0, 1, 'wiki_task_batches', 'batch_no', 'batch_no', 'NO ACTION', 'NO ACTION', 'NONE'),
                    (1, 0, 'wiki_tasks', 'task_id', 'task_id', 'NO ACTION', 'NO ACTION', 'NONE'))),
 'wiki_observations': (((0, 'vault_key', 'TEXT', 0, None, 1, 0),
                        (1, 'vault_path', 'TEXT', 1, None, 0, 0),
                        (2, 'task_id', 'TEXT', 0, None, 0, 0),
                        (3, 'pending_count', 'INTEGER', 0, None, 0, 0),
                        (4, 'candidate_count', 'INTEGER', 0, None, 0, 0),
                        (5, 'observed_at', 'TEXT', 1, None, 0, 0),
                        (6, 'error_code', 'TEXT', 0, None, 0, 0)),
                       ((0, 0, 'wiki_tasks', 'task_id', 'task_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'ingestion_events': (((0, 'event_id', 'INTEGER', 0, None, 1, 0),
                       (1, 'event_key', 'TEXT', 1, None, 0, 0),
                       (2, 'contract', 'TEXT', 1, None, 0, 0),
                       (3, 'subject_kind', 'TEXT', 1, None, 0, 0),
                       (4, 'subject_id', 'INTEGER', 1, None, 0, 0),
                       (5, 'item_id', 'INTEGER', 0, None, 0, 0),
                       (6, 'kind', 'TEXT', 1, None, 0, 0),
                       (7, 'binding_sha256', 'TEXT', 1, None, 0, 0),
                       (8, 'detail_json', 'TEXT', 1, None, 0, 0),
                       (9, 'created_at', 'TEXT', 1, None, 0, 0)),
                      ((0, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
 'wiki_outcome_receipts': (((0, 'receipt_id', 'TEXT', 1, None, 1, 0),
                            (1, 'task_id', 'TEXT', 1, None, 0, 0),
                            (2, 'batch_no', 'INTEGER', 1, None, 0, 0),
                            (3, 'phase', 'TEXT', 1, None, 2, 0),
                            (4, 'contract', 'TEXT', 1, None, 0, 0),
                            (5, 'boundary_sha256', 'TEXT', 1, None, 0, 0),
                            (6, 'plan_sha256', 'TEXT', 1, None, 0, 0),
                            (7, 'payload_json', 'TEXT', 1, None, 0, 0),
                            (8, 'created_at', 'TEXT', 1, None, 0, 0)),
                           ((0,
                             0,
                             'wiki_task_batches',
                             'task_id',
                             'task_id',
                             'NO ACTION',
                             'NO ACTION',
                             'NONE'),
                            (0,
                             1,
                             'wiki_task_batches',
                             'batch_no',
                             'batch_no',
                             'NO ACTION',
                             'NO ACTION',
                             'NONE'))),
 'sqlite_sequence': (((0, 'name', '', 0, None, 0, 0), (1, 'seq', '', 0, None, 0, 0)), ())}

_SCHEMA26_INDEX_ABI = {'sqlite_autoindex_accepted_insight_versions_1': ('accepted_insight_versions',
                                                  1,
                                                  'u',
                                                  0,
                                                  ((0, 2, 'judgment_id', 0, 'BINARY', 1),
                                                   (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_accepted_insight_versions_2': ('accepted_insight_versions',
                                                  1,
                                                  'u',
                                                  0,
                                                  ((0, 0, 'insight_version_id', 0, 'BINARY', 1),
                                                   (1, 2, 'judgment_id', 0, 'BINARY', 1),
                                                   (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_captures_1': ('captures',
                                 1,
                                 'u',
                                 0,
                                 ((0, 9, 'raw_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_captures_2': ('captures',
                                 1,
                                 'u',
                                 0,
                                 ((0, 1, 'app_id', 0, 'BINARY', 1),
                                  (1, 2, 'message_id', 0, 'BINARY', 1),
                                  (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_confirmations_1': ('collection_confirmations',
                                                 1,
                                                 'pk',
                                                 0,
                                                 ((0, 0, 'token', 0, 'BINARY', 1),
                                                  (1, 1, 'ordinal', 0, 'BINARY', 1),
                                                  (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_members_1': ('collection_members',
                                           1,
                                           'u',
                                           0,
                                           ((0, 4, 'item_id', 0, 'BINARY', 1),
                                            (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_members_2': ('collection_members',
                                           1,
                                           'pk',
                                           0,
                                           ((0, 0, 'operation_id', 0, 'BINARY', 1),
                                            (1, 1, 'ordinal', 0, 'BINARY', 1),
                                            (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_members_3': ('collection_members',
                                           1,
                                           'u',
                                           0,
                                           ((0, 0, 'operation_id', 0, 'BINARY', 1),
                                            (1, 2, 'native_id', 0, 'BINARY', 1),
                                            (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_operations_1': ('collection_operations',
                                              1,
                                              'u',
                                              0,
                                              ((0, 8, 'confirmation_token', 0, 'BINARY', 1),
                                               (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_operations_2': ('collection_operations',
                                              1,
                                              'u',
                                              0,
                                              ((0, 1, 'kind', 0, 'BINARY', 1),
                                               (1, 2, 'source_key', 0, 'BINARY', 1),
                                               (2, 5, 'signature', 0, 'BINARY', 1),
                                               (3, 6, 'content_signature', 0, 'BINARY', 1),
                                               (4, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_previews_1': ('collection_previews',
                                            1,
                                            'pk',
                                            0,
                                            ((0, 0, 'token', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_collection_results_1': ('collection_results',
                                           1,
                                           'u',
                                           0,
                                           ((0, 1, 'operation_id', 0, 'BINARY', 1),
                                            (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_confirmation_decisions_1': ('confirmation_decisions',
                                               1,
                                               'pk',
                                               0,
                                               ((0, 0, 'item_id', 0, 'BINARY', 1),
                                                (1, 1, 'revision', 0, 'BINARY', 1),
                                                (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_delivery_adjacency_1': ('delivery_adjacency',
                                           1,
                                           'pk',
                                           0,
                                           ((0, 0, 'app_id', 0, 'BINARY', 1),
                                            (1, 1, 'message_id', 0, 'BINARY', 1),
                                            (2, 2, 'earlier_message_id', 0, 'BINARY', 1),
                                            (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_feishu_action_queue_1': ('feishu_action_queue',
                                            1,
                                            'u',
                                            0,
                                            ((0, 1, 'action_key', 0, 'BINARY', 1),
                                             (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_feishu_binding_1': ('feishu_binding',
                                       1,
                                       'pk',
                                       0,
                                       ((0, 0, 'app_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_feishu_parts_1': ('feishu_parts',
                                     1,
                                     'pk',
                                     0,
                                     ((0, 0, 'app_id', 0, 'BINARY', 1),
                                      (1, 1, 'message_id', 0, 'BINARY', 1),
                                      (2, 2, 'position', 0, 'BINARY', 1),
                                      (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_feishu_receipts_1': ('feishu_receipts',
                                        1,
                                        'pk',
                                        0,
                                        ((0, 0, 'app_id', 0, 'BINARY', 1),
                                         (1, 1, 'message_id', 0, 'BINARY', 1),
                                         (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_group_decisions_1': ('group_decisions',
                                        1,
                                        'pk',
                                        0,
                                        ((0, 0, 'item_id', 0, 'BINARY', 1),
                                         (1, 1, 'request_id', 0, 'BINARY', 1),
                                         (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_group_decisions_2': ('group_decisions',
                                        1,
                                        'u',
                                        0,
                                        ((0, 0, 'item_id', 0, 'BINARY', 1),
                                         (1, 3, 'submitted_revision', 0, 'BINARY', 1),
                                         (2, 4, 'selection_digest', 0, 'BINARY', 1),
                                         (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_identity_replacements_1': ('insight_identity_replacements',
                                                      1,
                                                      'u',
                                                      0,
                                                      ((0, 0, 'replaced_insight_id', 0, 'BINARY', 1),
                                                       (1, 1, 'replacement_insight_id', 0, 'BINARY', 1),
                                                       (2, 2, 'event_id', 0, 'BINARY', 1),
                                                       (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_reconsiderations_1': ('insight_reconsiderations',
                                                 1,
                                                 'u',
                                                 0,
                                                 ((0, 1, 'insight_version_id', 0, 'BINARY', 1),
                                                  (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_reconsiderations_2': ('insight_reconsiderations',
                                                 1,
                                                 'u',
                                                 0,
                                                 ((0, 2, 'judgment_id', 0, 'BINARY', 1),
                                                  (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_reconsiderations_3': ('insight_reconsiderations',
                                                 1,
                                                 'u',
                                                 0,
                                                 ((0, 3, 'operation_id', 0, 'BINARY', 1),
                                                  (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_reconsiderations_4': ('insight_reconsiderations',
                                                 1,
                                                 'u',
                                                 0,
                                                 ((0, 1, 'insight_version_id', 0, 'BINARY', 1),
                                                  (1, 2, 'judgment_id', 0, 'BINARY', 1),
                                                  (2, 0, 'reconsideration_id', 0, 'BINARY', 1),
                                                  (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_version_disqualifications_1': ('insight_version_disqualifications',
                                                          1,
                                                          'u',
                                                          0,
                                                          ((0, 1, 'insight_version_id', 0, 'BINARY', 1),
                                                           (1, 2, 'fact_kind', 0, 'BINARY', 1),
                                                           (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_version_disqualifications_2': ('insight_version_disqualifications',
                                                          1,
                                                          'u',
                                                          0,
                                                          ((0, 1, 'insight_version_id', 0, 'BINARY', 1),
                                                           (1, 3, 'event_id', 0, 'BINARY', 1),
                                                           (2, 2, 'fact_kind', 0, 'BINARY', 1),
                                                           (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_version_participants_1': ('insight_version_participants',
                                                     1,
                                                     'pk',
                                                     0,
                                                     ((0, 0, 'insight_version_id', 0, 'BINARY', 1),
                                                      (1, 1, 'participant_key', 0, 'BINARY', 1),
                                                      (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_version_participants_2': ('insight_version_participants',
                                                     1,
                                                     'u',
                                                     0,
                                                     ((0, 0, 'insight_version_id', 0, 'BINARY', 1),
                                                      (1, 6, 'position', 0, 'BINARY', 1),
                                                      (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_version_used_relations_1': ('insight_version_used_relations',
                                                       1,
                                                       'pk',
                                                       0,
                                                       ((0, 0, 'insight_version_id', 0, 'BINARY', 1),
                                                        (1, 1, 'relation_version_id', 0, 'BINARY', 1),
                                                        (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_version_used_relations_2': ('insight_version_used_relations',
                                                       1,
                                                       'u',
                                                       0,
                                                       ((0, 0, 'insight_version_id', 0, 'BINARY', 1),
                                                        (1, 2, 'position', 0, 'BINARY', 1),
                                                        (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_versions_1': ('insight_versions',
                                         1,
                                         'u',
                                         0,
                                         ((0, 1, 'insight_id', 0, 'BINARY', 1),
                                          (1, 2, 'version_no', 0, 'BINARY', 1),
                                          (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_versions_2': ('insight_versions',
                                         1,
                                         'u',
                                         0,
                                         ((0, 1, 'insight_id', 0, 'BINARY', 1),
                                          (1, 0, 'insight_version_id', 0, 'BINARY', 1),
                                          (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_versions_3': ('insight_versions',
                                         1,
                                         'u',
                                         0,
                                         ((0, 3, 'previous_version_id', 0, 'BINARY', 1),
                                          (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_insight_versions_4': ('insight_versions',
                                         1,
                                         'u',
                                         0,
                                         ((0, 4, 'produced_event_id', 0, 'BINARY', 1),
                                          (1, 1, 'insight_id', 0, 'BINARY', 1),
                                          (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_knowledge_results_1': ('knowledge_results',
                                          1,
                                          'u',
                                          0,
                                          ((0, 1, 'source_fact_id', 0, 'BINARY', 1),
                                           (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_manual_cards_1': ('manual_cards',
                                     1,
                                     'u',
                                     0,
                                     ((0, 3, 'item_id', 0, 'BINARY', 1),
                                      (1, 4, 'review_round_id', 0, 'BINARY', 1),
                                      (2, 5, 'group_id', 0, 'BINARY', 1),
                                      (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_materials_1': ('materials',
                                  1,
                                  'u',
                                  0,
                                  ((0, 1, 'source_kind', 0, 'BINARY', 1),
                                   (1, 2, 'source_key', 0, 'BINARY', 1),
                                   (2, 7, 'snapshot_key', 0, 'BINARY', 1),
                                   (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_accepted_boundary_1': ('organization_event_accepted_boundary',
                                                             1,
                                                             'pk',
                                                             0,
                                                             ((0, 0, 'event_id', 0, 'BINARY', 1),
                                                              (1, 1, 'insight_version_id', 0, 'BINARY', 1),
                                                              (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_accepted_boundary_2': ('organization_event_accepted_boundary',
                                                             1,
                                                             'u',
                                                             0,
                                                             ((0, 0, 'event_id', 0, 'BINARY', 1),
                                                              (1, 2, 'position', 0, 'BINARY', 1),
                                                              (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_coverages_1': ('organization_event_coverages',
                                                     1,
                                                     'pk',
                                                     0,
                                                     ((0, 0, 'event_id', 0, 'BINARY', 1),
                                                      (1, 1, 'knowledge_result_id', 0, 'BINARY', 1),
                                                      (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_coverages_2': ('organization_event_coverages',
                                                     1,
                                                     'u',
                                                     0,
                                                     ((0, 1, 'knowledge_result_id', 0, 'BINARY', 1),
                                                      (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_relation_boundary_1': ('organization_event_relation_boundary',
                                                             1,
                                                             'pk',
                                                             0,
                                                             ((0, 0, 'event_id', 0, 'BINARY', 1),
                                                              (1, 1, 'relation_version_id', 0, 'BINARY', 1),
                                                              (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_relation_boundary_2': ('organization_event_relation_boundary',
                                                             1,
                                                             'u',
                                                             0,
                                                             ((0, 0, 'event_id', 0, 'BINARY', 1),
                                                              (1, 3, 'position', 0, 'BINARY', 1),
                                                              (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_source_boundary_1': ('organization_event_source_boundary',
                                                           1,
                                                           'pk',
                                                           0,
                                                           ((0, 0, 'event_id', 0, 'BINARY', 1),
                                                            (1, 1, 'knowledge_result_id', 0, 'BINARY', 1),
                                                            (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_organization_event_source_boundary_2': ('organization_event_source_boundary',
                                                           1,
                                                           'u',
                                                           0,
                                                           ((0, 0, 'event_id', 0, 'BINARY', 1),
                                                            (1, 3, 'boundary_role', 0, 'BINARY', 1),
                                                            (2, 4, 'position', 0, 'BINARY', 1),
                                                            (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_personal_cognition_entries_1': ('personal_cognition_entries',
                                                   1,
                                                   'u',
                                                   0,
                                                   ((0, 6, 'operation_id', 0, 'BINARY', 1),
                                                    (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_raw_counters_1': ('raw_counters',
                                     1,
                                     'pk',
                                     0,
                                     ((0, 0, 'day', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_raw_records_1': ('raw_records',
                                    1,
                                    'pk',
                                    0,
                                    ((0, 0, 'raw_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_raw_records_2': ('raw_records',
                                    1,
                                    'u',
                                    0,
                                    ((0, 4, 'relative_path', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_raw_records_3': ('raw_records',
                                    1,
                                    'u',
                                    0,
                                    ((0, 8, 'supersedes', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_current_1': ('relation_current',
                                         1,
                                         'u',
                                         0,
                                         ((0, 1, 'relation_version_id', 0, 'BINARY', 1),
                                          (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_version_participants_1': ('relation_version_participants',
                                                      1,
                                                      'pk',
                                                      0,
                                                      ((0, 0, 'relation_version_id', 0, 'BINARY', 1),
                                                       (1, 1, 'participant_key', 0, 'BINARY', 1),
                                                       (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_version_participants_2': ('relation_version_participants',
                                                      1,
                                                      'u',
                                                      0,
                                                      ((0, 0, 'relation_version_id', 0, 'BINARY', 1),
                                                       (1, 6, 'position', 0, 'BINARY', 1),
                                                       (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_version_used_relations_1': ('relation_version_used_relations',
                                                        1,
                                                        'pk',
                                                        0,
                                                        ((0, 0, 'relation_version_id', 0, 'BINARY', 1),
                                                         (1, 1, 'used_relation_version_id', 0, 'BINARY', 1),
                                                         (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_version_used_relations_2': ('relation_version_used_relations',
                                                        1,
                                                        'u',
                                                        0,
                                                        ((0, 0, 'relation_version_id', 0, 'BINARY', 1),
                                                         (1, 2, 'position', 0, 'BINARY', 1),
                                                         (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_versions_1': ('relation_versions',
                                          1,
                                          'u',
                                          0,
                                          ((0, 1, 'relation_id', 0, 'BINARY', 1),
                                           (1, 2, 'version_no', 0, 'BINARY', 1),
                                           (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_versions_2': ('relation_versions',
                                          1,
                                          'u',
                                          0,
                                          ((0, 1, 'relation_id', 0, 'BINARY', 1),
                                           (1, 0, 'relation_version_id', 0, 'BINARY', 1),
                                           (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_versions_3': ('relation_versions',
                                          1,
                                          'u',
                                          0,
                                          ((0, 3, 'previous_version_id', 0, 'BINARY', 1),
                                           (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_relation_versions_4': ('relation_versions',
                                          1,
                                          'u',
                                          0,
                                          ((0, 4, 'produced_event_id', 0, 'BINARY', 1),
                                           (1, 1, 'relation_id', 0, 'BINARY', 1),
                                           (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_settings_1': ('settings',
                                 1,
                                 'pk',
                                 0,
                                 ((0, 0, 'key', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_source_connections_1': ('source_connections',
                                           1,
                                           'pk',
                                           0,
                                           ((0, 0, 'platform', 0, 'BINARY', 1),
                                            (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_source_facts_1': ('source_facts',
                                     1,
                                     'u',
                                     0,
                                     ((0, 1, 'material_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_source_media_1': ('source_media',
                                     1,
                                     'pk',
                                     0,
                                     ((0, 0, 'material_id', 0, 'BINARY', 1),
                                      (1, 1, 'member_id', 0, 'BINARY', 1),
                                      (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_source_media_2': ('source_media',
                                     1,
                                     'u',
                                     0,
                                     ((0, 0, 'material_id', 0, 'BINARY', 1),
                                      (1, 2, 'position', 0, 'BINARY', 1),
                                      (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_source_review_results_1': ('source_review_results',
                                              1,
                                              'pk',
                                              0,
                                              ((0, 0, 'item_id', 0, 'BINARY', 1),
                                               (1, 1, 'revision', 0, 'BINARY', 1),
                                               (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_submitted_sources_1': ('submitted_sources',
                                          1,
                                          'u',
                                          0,
                                          ((0, 1, 'input_kind', 0, 'BINARY', 1),
                                           (1, 2, 'input_key', 0, 'BINARY', 1),
                                           (2, 8, 'binding_scope', 0, 'BINARY', 1),
                                           (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_topic_members_1': ('topic_members',
                                      1,
                                      'pk',
                                      0,
                                      ((0, 0, 'topic_id', 0, 'BINARY', 1),
                                       (1, 1, 'position', 0, 'BINARY', 1),
                                       (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_topic_members_2': ('topic_members',
                                      1,
                                      'u',
                                      0,
                                      ((0, 0, 'topic_id', 0, 'BINARY', 1),
                                       (1, 2, 'knowledge_result_id', 0, 'BINARY', 1),
                                       (2, 3, 'point_id', 0, 'BINARY', 1),
                                       (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_user_insight_judgments_1': ('user_insight_judgments',
                                               1,
                                               'u',
                                               0,
                                               ((0, 2, 'insight_version_id', 0, 'BINARY', 1),
                                                (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_user_insight_judgments_2': ('user_insight_judgments',
                                               1,
                                               'u',
                                               0,
                                               ((0, 1, 'insight_id', 0, 'BINARY', 1),
                                                (1, 0, 'judgment_id', 0, 'BINARY', 1),
                                                (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_user_insight_judgments_3': ('user_insight_judgments',
                                               1,
                                               'u',
                                               0,
                                               ((0, 2, 'insight_version_id', 0, 'BINARY', 1),
                                                (1, 0, 'judgment_id', 0, 'BINARY', 1),
                                                (2, 3, 'decision', 0, 'BINARY', 1),
                                                (3, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_wiki_tasks_1': ('wiki_tasks',
                                   1,
                                   'pk',
                                   0,
                                   ((0, 0, 'task_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_wiki_tasks_2': ('wiki_tasks',
                                   1,
                                   'u',
                                   0,
                                   ((0, 2, 'vault_key', 0, 'BINARY', 1),
                                    (1, 10, 'boundary_sha256', 0, 'BINARY', 1),
                                    (2, 9, 'kit_manifest_sha256', 0, 'BINARY', 1),
                                    (3, 5, 'backend', 0, 'BINARY', 1),
                                    (4, 6, 'model', 0, 'BINARY', 1),
                                    (5, 7, 'effort', 0, 'BINARY', 1),
                                    (6, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_wiki_task_batches_1': ('wiki_task_batches',
                                          1,
                                          'pk',
                                          0,
                                          ((0, 0, 'task_id', 0, 'BINARY', 1),
                                           (1, 1, 'batch_no', 0, 'BINARY', 1),
                                           (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_wiki_task_raw_1': ('wiki_task_raw',
                                      1,
                                      'pk',
                                      0,
                                      ((0, 0, 'task_id', 0, 'BINARY', 1),
                                       (1, 1, 'ordinal', 0, 'BINARY', 1),
                                       (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_wiki_task_raw_2': ('wiki_task_raw',
                                      1,
                                      'u',
                                      0,
                                      ((0, 0, 'task_id', 0, 'BINARY', 1),
                                       (1, 5, 'relative_path', 0, 'BINARY', 1),
                                       (2, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_wiki_observations_1': ('wiki_observations',
                                          1,
                                          'pk',
                                          0,
                                          ((0, 0, 'vault_key', 0, 'BINARY', 1),
                                           (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_ingestion_events_1': ('ingestion_events',
                                         1,
                                         'u',
                                         0,
                                         ((0, 1, 'event_key', 0, 'BINARY', 1),
                                          (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_wiki_outcome_receipts_1': ('wiki_outcome_receipts',
                                              1,
                                              'pk',
                                              0,
                                              ((0, 0, 'receipt_id', 0, 'BINARY', 1),
                                               (1, 3, 'phase', 0, 'BINARY', 1),
                                               (2, -1, None, 0, 'BINARY', 0))),
 'accepted_insight_versions_role_order': ('accepted_insight_versions',
                                          0,
                                          'c',
                                          0,
                                          ((0, 6, 'current_role', 0, 'BINARY', 1),
                                           (1, 7, 'accepted_at', 1, 'BINARY', 1),
                                           (2, 0, 'insight_version_id', 1, 'BINARY', 1),
                                           (3, -1, None, 0, 'BINARY', 0))),
 'insight_version_disqualifications_kind': ('insight_version_disqualifications',
                                            0,
                                            'c',
                                            0,
                                            ((0, 1, 'insight_version_id', 0, 'BINARY', 1),
                                             (1, 2, 'fact_kind', 0, 'BINARY', 1),
                                             (2, -1, None, 0, 'BINARY', 0))),
 'insight_version_participants_accepted': ('insight_version_participants',
                                           0,
                                           'c',
                                           0,
                                           ((0, 5, 'accepted_insight_version_id', 0, 'BINARY', 1),
                                            (1, -1, None, 0, 'BINARY', 0))),
 'insight_version_participants_source': ('insight_version_participants',
                                         0,
                                         'c',
                                         0,
                                         ((0, 3, 'knowledge_result_id', 0, 'BINARY', 1),
                                          (1, 4, 'point_id', 0, 'BINARY', 1),
                                          (2, -1, None, 0, 'BINARY', 0))),
 'insight_versions_semantic_signature': ('insight_versions',
                                         0,
                                         'c',
                                         0,
                                         ((0, 6, 'semantic_signature', 0, 'BINARY', 1),
                                          (1, -1, None, 0, 'BINARY', 0))),
 'knowledge_result_source_fact_identity': ('knowledge_results',
                                           1,
                                           'c',
                                           0,
                                           ((0, 0, 'knowledge_result_id', 0, 'BINARY', 1),
                                            (1, 1, 'source_fact_id', 0, 'BINARY', 1),
                                            (2, -1, None, 0, 'BINARY', 0))),
 'one_basis_invalid_per_version': ('relation_facts',
                                   1,
                                   'c',
                                   1,
                                   ((0, 3, 'relation_version_id', 0, 'BINARY', 1),
                                    (1, -1, None, 0, 'BINARY', 0))),
 'one_current_accepted_version_per_identity': ('accepted_insight_versions',
                                               1,
                                               'c',
                                               1,
                                               ((0, 1, 'insight_id', 0, 'BINARY', 1),
                                                (1, -1, None, 0, 'BINARY', 0))),
 'one_evolution_successor_per_version': ('relation_facts',
                                         1,
                                         'c',
                                         1,
                                         ((0, 3, 'relation_version_id', 0, 'BINARY', 1),
                                          (1, -1, None, 0, 'BINARY', 0))),
 'one_replacement_per_relation': ('relation_facts',
                                  1,
                                  'c',
                                  1,
                                  ((0, 2, 'relation_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'one_running_organization_event': ('organization_events',
                                    1,
                                    'c',
                                    1,
                                    ((0, 1, 'memory_scope', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'one_wrong_fact_per_relation': ('relation_facts',
                                 1,
                                 'c',
                                 1,
                                 ((0, 2, 'relation_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'organization_event_source_boundary_role_knowledge': ('organization_event_source_boundary',
                                                       0,
                                                       'c',
                                                       0,
                                                       ((0, 3, 'boundary_role', 0, 'BINARY', 1),
                                                        (1, 1, 'knowledge_result_id', 0, 'BINARY', 1),
                                                        (2, -1, None, 0, 'BINARY', 0))),
 'organization_events_status_order': ('organization_events',
                                      0,
                                      'c',
                                      0,
                                      ((0, 2, 'status', 0, 'BINARY', 1),
                                       (1, 0, 'event_id', 1, 'BINARY', 1),
                                       (2, -1, None, 0, 'BINARY', 0))),
 'raw_records_subject': ('raw_records',
                         0,
                         'c',
                         0,
                         ((0, 1, 'subject_kind', 0, 'BINARY', 1),
                          (1, 2, 'subject_id', 0, 'BINARY', 1),
                          (2, -1, None, 0, 'BINARY', 0))),
 'relation_facts_event_kind': ('relation_facts',
                               0,
                               'c',
                               0,
                               ((0, 1, 'event_id', 0, 'BINARY', 1),
                                (1, 4, 'fact_kind', 0, 'BINARY', 1),
                                (2, -1, None, 0, 'BINARY', 0))),
 'relation_version_participants_accepted': ('relation_version_participants',
                                            0,
                                            'c',
                                            0,
                                            ((0, 5, 'accepted_insight_version_id', 0, 'BINARY', 1),
                                             (1, -1, None, 0, 'BINARY', 0))),
 'relation_version_participants_source': ('relation_version_participants',
                                          0,
                                          'c',
                                          0,
                                          ((0, 3, 'knowledge_result_id', 0, 'BINARY', 1),
                                           (1, 4, 'point_id', 0, 'BINARY', 1),
                                           (2, -1, None, 0, 'BINARY', 0))),
 'relation_versions_semantic_signature': ('relation_versions',
                                          0,
                                          'c',
                                          0,
                                          ((0, 6, 'semantic_signature', 0, 'BINARY', 1),
                                           (1, -1, None, 0, 'BINARY', 0))),
 'user_insight_judgments_decision_order': ('user_insight_judgments',
                                           0,
                                           'c',
                                           0,
                                           ((0, 3, 'decision', 0, 'BINARY', 1),
                                            (1, 5, 'decided_at', 0, 'BINARY', 1),
                                            (2, -1, None, 0, 'BINARY', 0))),
 'wiki_one_unresolved_task_per_vault': ('wiki_tasks',
                                        1,
                                        'c',
                                        1,
                                        ((0, 2, 'vault_key', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'ingestion_events_subject': ('ingestion_events',
                              0,
                              'c',
                              0,
                              ((0, 3, 'subject_kind', 0, 'BINARY', 1),
                               (1, 4, 'subject_id', 0, 'BINARY', 1),
                               (2, 0, 'event_id', 0, 'BINARY', 1),
                               (3, -1, None, 0, 'BINARY', 0))),
 'wiki_outcome_one_accepted_batch': ('wiki_outcome_receipts',
                                     1,
                                     'c',
                                     1,
                                     ((0, 1, 'task_id', 0, 'BINARY', 1),
                                      (1, 2, 'batch_no', 0, 'BINARY', 1),
                                      (2, -1, None, 0, 'BINARY', 0))),
 'ingestion_events_local_owner': ('ingestion_events',
                                  1,
                                  'c',
                                  1,
                                  ((0, 5, 'item_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0)))}

_SCHEMA27_TABLE_DELTA = {'source_identity_events': (((0, 'event_id', 'INTEGER', 0, None, 1, 0),
                             (1, 'event_key', 'TEXT', 1, None, 0, 0),
                             (2, 'item_id', 'INTEGER', 1, None, 0, 0),
                             (3, 'expected_prior_event_id', 'INTEGER', 0, None, 0, 0),
                             (4, 'kind', 'TEXT', 1, None, 0, 0),
                             (5, 'contract', 'TEXT', 1, None, 0, 0),
                             (6, 'source_binding_sha256', 'TEXT', 1, None, 0, 0),
                             (7, 'relation_binding_sha256', 'TEXT', 1, None, 0, 0),
                             (8, 'review_version', 'TEXT', 1, None, 0, 0),
                             (9, 'detail_json', 'TEXT', 1, None, 0, 0),
                             (10, 'created_at', 'TEXT', 1, None, 0, 0)),
                            ((0,
                              0,
                              'source_identity_events',
                              'expected_prior_event_id',
                              'event_id',
                              'NO ACTION',
                              'NO ACTION',
                              'NONE'),
                             (1,
                              0,
                              'distill_items',
                              'item_id',
                              'item_id',
                              'NO ACTION',
                              'NO ACTION',
                              'NONE')))}

_SCHEMA27_INDEX_DELTA = {'source_identity_item_order': ('source_identity_events',
                                0,
                                'c',
                                0,
                                ((0, 2, 'item_id', 0, 'BINARY', 1),
                                 (1, 0, 'event_id', 0, 'BINARY', 1),
                                 (2, -1, None, 0, 'BINARY', 0))),
 'source_identity_one_root': ('source_identity_events',
                              1,
                              'c',
                              1,
                              ((0, 2, 'item_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))),
 'source_identity_one_successor': ('source_identity_events',
                                   1,
                                   'c',
                                   1,
                                   ((0, 3, 'expected_prior_event_id', 0, 'BINARY', 1),
                                    (1, -1, None, 0, 'BINARY', 0))),
 'sqlite_autoindex_source_identity_events_1': ('source_identity_events',
                                               1,
                                               'u',
                                               0,
                                               ((0, 1, 'event_key', 0, 'BINARY', 1),
                                                (1, -1, None, 0, 'BINARY', 0))),
 'wiki_task_contract_plan_unique': ('wiki_tasks',
                                    1,
                                    'c',
                                    0,
                                    ((0, 2, 'vault_key', 0, 'BINARY', 1),
                                     (1, 10, 'boundary_sha256', 0, 'BINARY', 1),
                                     (2, 9, 'kit_manifest_sha256', 0, 'BINARY', 1),
                                     (3, 5, 'backend', 0, 'BINARY', 1),
                                     (4, 6, 'model', 0, 'BINARY', 1),
                                     (5, 7, 'effort', 0, 'BINARY', 1),
                                     (6, 20, 'outcome_contract', 0, 'BINARY', 1),
                                     (7, -2, None, 0, 'BINARY', 1),
                                     (8, -1, None, 0, 'BINARY', 0))),
 'wiki_task_legacy_boundary_unique': ('wiki_tasks',
                                      1,
                                      'c',
                                      1,
                                      ((0, 2, 'vault_key', 0, 'BINARY', 1),
                                       (1, 10, 'boundary_sha256', 0, 'BINARY', 1),
                                       (2, 9, 'kit_manifest_sha256', 0, 'BINARY', 1),
                                       (3, 5, 'backend', 0, 'BINARY', 1),
                                       (4, 6, 'model', 0, 'BINARY', 1),
                                       (5, 7, 'effort', 0, 'BINARY', 1),
                                       (6, -1, None, 0, 'BINARY', 0)))}

_SCHEMA27_INDEX_REMOVE = ('sqlite_autoindex_wiki_tasks_2',)

_SCHEMA27_LEGACY_DELTAS = {21: {'remove': ('collection_members_ingestion_contract_match',
                 'collection_operations_ingestion_binding_immutable',
                 'collection_operations_ingestion_binding_required',
                 'distill_items_ingestion_binding_immutable',
                 'distill_items_ingestion_binding_required',
                 'distill_items_ingestion_no_delete',
                 'distill_items_ingestion_owner_immutable',
                 'distill_items_raw_terminal_no_insert',
                 'distill_items_raw_terminal_no_reopen',
                 'distill_items_raw_terminal_proof',
                 'ingestion_events',
                 'ingestion_events_local_owner',
                 'ingestion_events_no_delete',
                 'ingestion_events_no_update',
                 'ingestion_events_observation_typed',
                 'ingestion_events_proof_unavailable',
                 'ingestion_events_subject',
                 'source_media_ingestion_capture_binding',
                 'source_media_ingestion_capture_no_delete',
                 'source_media_ingestion_capture_release',
                 'source_media_ingestion_no_delete',
                 'source_media_ingestion_no_update',
                 'submitted_sources_ingestion_no_delete',
                 'submitted_sources_ingestion_no_release',
                 'submitted_sources_ingestion_owner_immutable',
                 'submitted_sources_local_insert',
                 'submitted_sources_local_no_delete',
                 'submitted_sources_local_tuple',
                 'wiki_batch_identity_immutable',
                 'wiki_batch_no_delete',
                 'wiki_batch_state_transition',
                 'wiki_observations',
                 'wiki_one_unresolved_task_per_vault',
                 'wiki_outcome_one_accepted_batch',
                 'wiki_outcome_receipts',
                 'wiki_outcome_receipts_no_delete',
                 'wiki_outcome_receipts_no_update',
                 'wiki_outcome_receipts_publish_unavailable',
                 'wiki_task_batches',
                 'wiki_task_boundary_immutable',
                 'wiki_task_no_delete',
                 'wiki_task_outcome_binding_immutable',
                 'wiki_task_outcome_binding_required',
                 'wiki_task_raw',
                 'wiki_task_raw_immutable',
                 'wiki_task_raw_no_delete',
                 'wiki_task_recovery_transition',
                 'wiki_task_state_transition',
                 'wiki_tasks'),
      'ddl': {'collection_operations': ('table',
                                        'collection_operations',
                                        'CREATE TABLE collection_operations (\n'
                                        '        operation_id INTEGER PRIMARY KEY,\n'
                                        '        kind TEXT NOT NULL, source_key TEXT NOT NULL, title TEXT '
                                        'NOT NULL,\n'
                                        '        manifest_json TEXT NOT NULL, signature TEXT NOT NULL, '
                                        'content_signature TEXT NOT NULL,\n'
                                        '        authority_json TEXT NOT NULL, confirmation_token TEXT NOT '
                                        'NULL UNIQUE,\n'
                                        '        state TEXT NOT NULL CHECK(state IN '
                                        "('queued','working','waiting_user','partial','failed','succeeded','cancelled')),\n"
                                        '        consequence TEXT CHECK(consequence IN '
                                        "('complete','partial','failed')),\n"
                                        '        error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT '
                                        '0 CHECK(cancel_requested IN (0,1)),\n'
                                        '        revision INTEGER NOT NULL DEFAULT 1,\n'
                                        '        queued_at TEXT NOT NULL, created_at TEXT NOT NULL, '
                                        'updated_at TEXT NOT NULL,\n'
                                        '        UNIQUE(kind,source_key,signature,content_signature)\n'
                                        '    )'),
              'distill_items': ('table',
                                'distill_items',
                                'CREATE TABLE distill_items (\n'
                                '    item_id INTEGER PRIMARY KEY,\n'
                                '    submitted_url TEXT NOT NULL,\n'
                                '    state TEXT NOT NULL CHECK (\n'
                                "        state IN ('queued', 'working', 'waiting_user', 'succeeded', "
                                "'failed')\n"
                                '    ),\n'
                                '    phase TEXT NOT NULL CHECK (\n'
                                "        phase IN ('collecting', 'reviewing', 'distilling', 'publishing', "
                                "'done')\n"
                                '    ),\n'
                                '    material_id INTEGER REFERENCES materials(material_id),\n'
                                '    error_code TEXT,\n'
                                '    rejection_reason TEXT,\n'
                                '    dismissed_at TEXT,\n'
                                '    confirmation_json TEXT,\n'
                                '    queued_at TEXT NOT NULL,\n'
                                '    created_at TEXT NOT NULL,\n'
                                '    updated_at TEXT NOT NULL\n'
                                ", platform_authority_json TEXT NOT NULL DEFAULT '{}', submitted_title TEXT "
                                "NOT NULL DEFAULT '', review_revision INTEGER NOT NULL DEFAULT 0)"),
              'submitted_sources': ('table',
                                    'submitted_sources',
                                    'CREATE TABLE submitted_sources (\n'
                                    '    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),\n'
                                    "    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', "
                                    "'markdown', 'pdf', 'epub', 'image')),\n"
                                    '    input_key TEXT NOT NULL,\n'
                                    '    input_label TEXT NOT NULL,\n'
                                    '    input_metadata TEXT NOT NULL,\n'
                                    '    content BLOB,\n'
                                    '    retain_until TEXT,\n'
                                    '    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),\n'
                                    '    UNIQUE(input_kind, input_key)\n'
                                    ')')},
      'abi_remove': ('ingestion_events',
                     'wiki_observations',
                     'wiki_outcome_receipts',
                     'wiki_task_batches',
                     'wiki_task_raw',
                     'wiki_tasks'),
      'abi': {'collection_operations': (((0, 'operation_id', 'INTEGER', 0, None, 1, 0),
                                         (1, 'kind', 'TEXT', 1, None, 0, 0),
                                         (2, 'source_key', 'TEXT', 1, None, 0, 0),
                                         (3, 'title', 'TEXT', 1, None, 0, 0),
                                         (4, 'manifest_json', 'TEXT', 1, None, 0, 0),
                                         (5, 'signature', 'TEXT', 1, None, 0, 0),
                                         (6, 'content_signature', 'TEXT', 1, None, 0, 0),
                                         (7, 'authority_json', 'TEXT', 1, None, 0, 0),
                                         (8, 'confirmation_token', 'TEXT', 1, None, 0, 0),
                                         (9, 'state', 'TEXT', 1, None, 0, 0),
                                         (10, 'consequence', 'TEXT', 0, None, 0, 0),
                                         (11, 'error_code', 'TEXT', 0, None, 0, 0),
                                         (12, 'cancel_requested', 'INTEGER', 1, '0', 0, 0),
                                         (13, 'revision', 'INTEGER', 1, '1', 0, 0),
                                         (14, 'queued_at', 'TEXT', 1, None, 0, 0),
                                         (15, 'created_at', 'TEXT', 1, None, 0, 0),
                                         (16, 'updated_at', 'TEXT', 1, None, 0, 0)),
                                        ()),
              'distill_items': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                                 (1, 'submitted_url', 'TEXT', 1, None, 0, 0),
                                 (2, 'state', 'TEXT', 1, None, 0, 0),
                                 (3, 'phase', 'TEXT', 1, None, 0, 0),
                                 (4, 'material_id', 'INTEGER', 0, None, 0, 0),
                                 (5, 'error_code', 'TEXT', 0, None, 0, 0),
                                 (6, 'rejection_reason', 'TEXT', 0, None, 0, 0),
                                 (7, 'dismissed_at', 'TEXT', 0, None, 0, 0),
                                 (8, 'confirmation_json', 'TEXT', 0, None, 0, 0),
                                 (9, 'queued_at', 'TEXT', 1, None, 0, 0),
                                 (10, 'created_at', 'TEXT', 1, None, 0, 0),
                                 (11, 'updated_at', 'TEXT', 1, None, 0, 0),
                                 (12, 'platform_authority_json', 'TEXT', 1, "'{}'", 0, 0),
                                 (13, 'submitted_title', 'TEXT', 1, "''", 0, 0),
                                 (14, 'review_revision', 'INTEGER', 1, '0', 0, 0)),
                                ((0,
                                  0,
                                  'materials',
                                  'material_id',
                                  'material_id',
                                  'NO ACTION',
                                  'NO ACTION',
                                  'NONE'),)),
              'submitted_sources': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                                     (1, 'input_kind', 'TEXT', 1, None, 0, 0),
                                     (2, 'input_key', 'TEXT', 1, None, 0, 0),
                                     (3, 'input_label', 'TEXT', 1, None, 0, 0),
                                     (4, 'input_metadata', 'TEXT', 1, None, 0, 0),
                                     (5, 'content', 'BLOB', 0, None, 0, 0),
                                     (6, 'retain_until', 'TEXT', 0, None, 0, 0),
                                     (7, 'retryable', 'INTEGER', 1, '1', 0, 0)),
                                    ((0,
                                      0,
                                      'distill_items',
                                      'item_id',
                                      'item_id',
                                      'NO ACTION',
                                      'NO ACTION',
                                      'NONE'),))},
      'index_remove': ('ingestion_events_local_owner',
                       'ingestion_events_subject',
                       'sqlite_autoindex_ingestion_events_1',
                       'sqlite_autoindex_wiki_observations_1',
                       'sqlite_autoindex_wiki_outcome_receipts_1',
                       'sqlite_autoindex_wiki_task_batches_1',
                       'sqlite_autoindex_wiki_task_raw_1',
                       'sqlite_autoindex_wiki_task_raw_2',
                       'sqlite_autoindex_wiki_tasks_1',
                       'sqlite_autoindex_wiki_tasks_2',
                       'wiki_one_unresolved_task_per_vault',
                       'wiki_outcome_one_accepted_batch'),
      'index': {'sqlite_autoindex_submitted_sources_1': ('submitted_sources',
                                                         1,
                                                         'u',
                                                         0,
                                                         ((0, 1, 'input_kind', 0, 'BINARY', 1),
                                                          (1, 2, 'input_key', 0, 'BINARY', 1),
                                                          (2, -1, None, 0, 'BINARY', 0)))}},
 22: {'remove': ('collection_members_ingestion_contract_match',
                 'collection_operations_ingestion_binding_immutable',
                 'collection_operations_ingestion_binding_required',
                 'distill_items_ingestion_binding_immutable',
                 'distill_items_ingestion_binding_required',
                 'distill_items_ingestion_no_delete',
                 'distill_items_ingestion_owner_immutable',
                 'distill_items_raw_terminal_no_insert',
                 'distill_items_raw_terminal_no_reopen',
                 'distill_items_raw_terminal_proof',
                 'ingestion_events',
                 'ingestion_events_local_owner',
                 'ingestion_events_no_delete',
                 'ingestion_events_no_update',
                 'ingestion_events_observation_typed',
                 'ingestion_events_proof_unavailable',
                 'ingestion_events_subject',
                 'source_media_ingestion_capture_binding',
                 'source_media_ingestion_capture_no_delete',
                 'source_media_ingestion_capture_release',
                 'source_media_ingestion_no_delete',
                 'source_media_ingestion_no_update',
                 'submitted_sources_ingestion_no_delete',
                 'submitted_sources_ingestion_no_release',
                 'submitted_sources_ingestion_owner_immutable',
                 'submitted_sources_local_insert',
                 'submitted_sources_local_no_delete',
                 'submitted_sources_local_tuple',
                 'wiki_observations',
                 'wiki_outcome_one_accepted_batch',
                 'wiki_outcome_receipts',
                 'wiki_outcome_receipts_no_delete',
                 'wiki_outcome_receipts_no_update',
                 'wiki_outcome_receipts_publish_unavailable',
                 'wiki_task_outcome_binding_immutable',
                 'wiki_task_outcome_binding_required'),
      'ddl': {'collection_operations': ('table',
                                        'collection_operations',
                                        'CREATE TABLE collection_operations (\n'
                                        '        operation_id INTEGER PRIMARY KEY,\n'
                                        '        kind TEXT NOT NULL, source_key TEXT NOT NULL, title TEXT '
                                        'NOT NULL,\n'
                                        '        manifest_json TEXT NOT NULL, signature TEXT NOT NULL, '
                                        'content_signature TEXT NOT NULL,\n'
                                        '        authority_json TEXT NOT NULL, confirmation_token TEXT NOT '
                                        'NULL UNIQUE,\n'
                                        '        state TEXT NOT NULL CHECK(state IN '
                                        "('queued','working','waiting_user','partial','failed','succeeded','cancelled')),\n"
                                        '        consequence TEXT CHECK(consequence IN '
                                        "('complete','partial','failed')),\n"
                                        '        error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT '
                                        '0 CHECK(cancel_requested IN (0,1)),\n'
                                        '        revision INTEGER NOT NULL DEFAULT 1,\n'
                                        '        queued_at TEXT NOT NULL, created_at TEXT NOT NULL, '
                                        'updated_at TEXT NOT NULL,\n'
                                        '        UNIQUE(kind,source_key,signature,content_signature)\n'
                                        '    )'),
              'distill_items': ('table',
                                'distill_items',
                                'CREATE TABLE distill_items (\n'
                                '    item_id INTEGER PRIMARY KEY,\n'
                                '    submitted_url TEXT NOT NULL,\n'
                                '    state TEXT NOT NULL CHECK (\n'
                                "        state IN ('queued', 'working', 'waiting_user', 'succeeded', "
                                "'failed')\n"
                                '    ),\n'
                                '    phase TEXT NOT NULL CHECK (\n'
                                "        phase IN ('collecting', 'reviewing', 'distilling', 'publishing', "
                                "'done')\n"
                                '    ),\n'
                                '    material_id INTEGER REFERENCES materials(material_id),\n'
                                '    error_code TEXT,\n'
                                '    rejection_reason TEXT,\n'
                                '    dismissed_at TEXT,\n'
                                '    confirmation_json TEXT,\n'
                                '    queued_at TEXT NOT NULL,\n'
                                '    created_at TEXT NOT NULL,\n'
                                '    updated_at TEXT NOT NULL\n'
                                ", platform_authority_json TEXT NOT NULL DEFAULT '{}', submitted_title TEXT "
                                "NOT NULL DEFAULT '', review_revision INTEGER NOT NULL DEFAULT 0)"),
              'submitted_sources': ('table',
                                    'submitted_sources',
                                    'CREATE TABLE submitted_sources (\n'
                                    '    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),\n'
                                    "    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', "
                                    "'markdown', 'pdf', 'epub', 'image')),\n"
                                    '    input_key TEXT NOT NULL,\n'
                                    '    input_label TEXT NOT NULL,\n'
                                    '    input_metadata TEXT NOT NULL,\n'
                                    '    content BLOB,\n'
                                    '    retain_until TEXT,\n'
                                    '    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),\n'
                                    '    UNIQUE(input_kind, input_key)\n'
                                    ')'),
              'wiki_tasks': ('table',
                             'wiki_tasks',
                             'CREATE TABLE wiki_tasks (\n'
                             '        task_id TEXT PRIMARY KEY CHECK (\n'
                             '            length(task_id) = 32\n'
                             "            AND task_id NOT GLOB '*[^0-9a-f]*'\n"
                             '        ),\n'
                             "        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),\n"
                             '        vault_key TEXT NOT NULL CHECK (length(vault_key) = 64),\n'
                             "        request_kind TEXT NOT NULL CHECK (request_kind IN ('one_batch', "
                             "'all')),\n"
                             '        trigger_source TEXT NOT NULL CHECK (\n'
                             "            trigger_source IN ('local_web', 'claudian', 'cli')\n"
                             '        ),\n'
                             "        backend TEXT NOT NULL CHECK (backend IN ('codex_cli')),\n"
                             '        model TEXT NOT NULL CHECK (\n'
                             '            length(model) BETWEEN 1 AND 80\n'
                             "            AND model NOT GLOB '*[^A-Za-z0-9._-]*'\n"
                             '        ),\n'
                             '        effort TEXT NOT NULL CHECK (\n'
                             "            effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', "
                             "'max', 'ultra')\n"
                             '        ),\n'
                             '        kit_version TEXT NOT NULL CHECK (\n'
                             '            length(kit_version) BETWEEN 1 AND 40\n'
                             "            AND kit_version NOT GLOB '*[^A-Za-z0-9._-]*'\n"
                             '        ),\n'
                             '        kit_manifest_sha256 TEXT NOT NULL CHECK (length(kit_manifest_sha256) = '
                             '64),\n'
                             '        boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),\n'
                             '        state TEXT NOT NULL CHECK (\n'
                             '            state IN (\n'
                             "                'queued', 'preparing', 'running', 'validating', 'publishing',\n"
                             "                'succeeded', 'failed'\n"
                             '            )\n'
                             '        ),\n'
                             '        raw_count INTEGER NOT NULL CHECK (raw_count >= 0),\n'
                             '        batch_count INTEGER NOT NULL CHECK (batch_count >= 0),\n'
                             '        completed_batch_count INTEGER NOT NULL DEFAULT 0 CHECK (\n'
                             '            completed_batch_count >= 0 AND completed_batch_count <= '
                             'batch_count\n'
                             '        ),\n'
                             '        error_code TEXT CHECK (error_code IS NULL OR error_code IN '
                             "('vault_busy', 'config_required', 'kit_missing', 'kit_drift', "
                             "'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', "
                             "'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', "
                             "'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', "
                             "'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', "
                             "'internal_error')),\n"
                             "        recovery_state TEXT NOT NULL DEFAULT 'not_needed' CHECK (\n"
                             "            recovery_state IN ('not_needed', 'required', 'running', "
                             "'succeeded', 'failed')\n"
                             '        ),\n'
                             "        recovery_phase TEXT NOT NULL DEFAULT 'none' CHECK (\n"
                             "            recovery_phase IN ('none', 'staging', 'publishing', 'readback')\n"
                             '        ),\n'
                             "        created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),\n"
                             "        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''),\n"
                             "        CHECK ((state = 'failed') = (error_code IS NOT NULL)),\n"
                             "        CHECK ((recovery_state = 'not_needed') = (recovery_phase = 'none')),\n"
                             '        UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256)\n'
                             '    )'),
              'wiki_one_unresolved_task_per_vault': ('index',
                                                     'wiki_tasks',
                                                     'CREATE UNIQUE INDEX '
                                                     'wiki_one_unresolved_task_per_vault\n'
                                                     '        ON wiki_tasks(vault_key)\n'
                                                     "        WHERE state IN ('queued', 'preparing', "
                                                     "'running', 'validating', 'publishing', 'failed')")},
      'abi_remove': ('ingestion_events', 'wiki_observations', 'wiki_outcome_receipts'),
      'abi': {'collection_operations': (((0, 'operation_id', 'INTEGER', 0, None, 1, 0),
                                         (1, 'kind', 'TEXT', 1, None, 0, 0),
                                         (2, 'source_key', 'TEXT', 1, None, 0, 0),
                                         (3, 'title', 'TEXT', 1, None, 0, 0),
                                         (4, 'manifest_json', 'TEXT', 1, None, 0, 0),
                                         (5, 'signature', 'TEXT', 1, None, 0, 0),
                                         (6, 'content_signature', 'TEXT', 1, None, 0, 0),
                                         (7, 'authority_json', 'TEXT', 1, None, 0, 0),
                                         (8, 'confirmation_token', 'TEXT', 1, None, 0, 0),
                                         (9, 'state', 'TEXT', 1, None, 0, 0),
                                         (10, 'consequence', 'TEXT', 0, None, 0, 0),
                                         (11, 'error_code', 'TEXT', 0, None, 0, 0),
                                         (12, 'cancel_requested', 'INTEGER', 1, '0', 0, 0),
                                         (13, 'revision', 'INTEGER', 1, '1', 0, 0),
                                         (14, 'queued_at', 'TEXT', 1, None, 0, 0),
                                         (15, 'created_at', 'TEXT', 1, None, 0, 0),
                                         (16, 'updated_at', 'TEXT', 1, None, 0, 0)),
                                        ()),
              'distill_items': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                                 (1, 'submitted_url', 'TEXT', 1, None, 0, 0),
                                 (2, 'state', 'TEXT', 1, None, 0, 0),
                                 (3, 'phase', 'TEXT', 1, None, 0, 0),
                                 (4, 'material_id', 'INTEGER', 0, None, 0, 0),
                                 (5, 'error_code', 'TEXT', 0, None, 0, 0),
                                 (6, 'rejection_reason', 'TEXT', 0, None, 0, 0),
                                 (7, 'dismissed_at', 'TEXT', 0, None, 0, 0),
                                 (8, 'confirmation_json', 'TEXT', 0, None, 0, 0),
                                 (9, 'queued_at', 'TEXT', 1, None, 0, 0),
                                 (10, 'created_at', 'TEXT', 1, None, 0, 0),
                                 (11, 'updated_at', 'TEXT', 1, None, 0, 0),
                                 (12, 'platform_authority_json', 'TEXT', 1, "'{}'", 0, 0),
                                 (13, 'submitted_title', 'TEXT', 1, "''", 0, 0),
                                 (14, 'review_revision', 'INTEGER', 1, '0', 0, 0)),
                                ((0,
                                  0,
                                  'materials',
                                  'material_id',
                                  'material_id',
                                  'NO ACTION',
                                  'NO ACTION',
                                  'NONE'),)),
              'submitted_sources': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                                     (1, 'input_kind', 'TEXT', 1, None, 0, 0),
                                     (2, 'input_key', 'TEXT', 1, None, 0, 0),
                                     (3, 'input_label', 'TEXT', 1, None, 0, 0),
                                     (4, 'input_metadata', 'TEXT', 1, None, 0, 0),
                                     (5, 'content', 'BLOB', 0, None, 0, 0),
                                     (6, 'retain_until', 'TEXT', 0, None, 0, 0),
                                     (7, 'retryable', 'INTEGER', 1, '1', 0, 0)),
                                    ((0,
                                      0,
                                      'distill_items',
                                      'item_id',
                                      'item_id',
                                      'NO ACTION',
                                      'NO ACTION',
                                      'NONE'),)),
              'wiki_tasks': (((0, 'task_id', 'TEXT', 0, None, 1, 0),
                              (1, 'vault_path', 'TEXT', 1, None, 0, 0),
                              (2, 'vault_key', 'TEXT', 1, None, 0, 0),
                              (3, 'request_kind', 'TEXT', 1, None, 0, 0),
                              (4, 'trigger_source', 'TEXT', 1, None, 0, 0),
                              (5, 'backend', 'TEXT', 1, None, 0, 0),
                              (6, 'model', 'TEXT', 1, None, 0, 0),
                              (7, 'effort', 'TEXT', 1, None, 0, 0),
                              (8, 'kit_version', 'TEXT', 1, None, 0, 0),
                              (9, 'kit_manifest_sha256', 'TEXT', 1, None, 0, 0),
                              (10, 'boundary_sha256', 'TEXT', 1, None, 0, 0),
                              (11, 'state', 'TEXT', 1, None, 0, 0),
                              (12, 'raw_count', 'INTEGER', 1, None, 0, 0),
                              (13, 'batch_count', 'INTEGER', 1, None, 0, 0),
                              (14, 'completed_batch_count', 'INTEGER', 1, '0', 0, 0),
                              (15, 'error_code', 'TEXT', 0, None, 0, 0),
                              (16, 'recovery_state', 'TEXT', 1, "'not_needed'", 0, 0),
                              (17, 'recovery_phase', 'TEXT', 1, "'none'", 0, 0),
                              (18, 'created_at', 'TEXT', 1, None, 0, 0),
                              (19, 'updated_at', 'TEXT', 1, None, 0, 0)),
                             ())},
      'index_remove': ('ingestion_events_local_owner',
                       'ingestion_events_subject',
                       'sqlite_autoindex_ingestion_events_1',
                       'sqlite_autoindex_wiki_observations_1',
                       'sqlite_autoindex_wiki_outcome_receipts_1',
                       'wiki_outcome_one_accepted_batch'),
      'index': {'sqlite_autoindex_submitted_sources_1': ('submitted_sources',
                                                         1,
                                                         'u',
                                                         0,
                                                         ((0, 1, 'input_kind', 0, 'BINARY', 1),
                                                          (1, 2, 'input_key', 0, 'BINARY', 1),
                                                          (2, -1, None, 0, 'BINARY', 0))),
                'sqlite_autoindex_wiki_tasks_2': ('wiki_tasks',
                                                  1,
                                                  'u',
                                                  0,
                                                  ((0, 2, 'vault_key', 0, 'BINARY', 1),
                                                   (1, 10, 'boundary_sha256', 0, 'BINARY', 1),
                                                   (2, 9, 'kit_manifest_sha256', 0, 'BINARY', 1),
                                                   (3, -1, None, 0, 'BINARY', 0)))}},
 23: {'remove': ('collection_members_ingestion_contract_match',
                 'collection_operations_ingestion_binding_immutable',
                 'collection_operations_ingestion_binding_required',
                 'distill_items_ingestion_binding_immutable',
                 'distill_items_ingestion_binding_required',
                 'distill_items_ingestion_no_delete',
                 'distill_items_ingestion_owner_immutable',
                 'distill_items_raw_terminal_no_insert',
                 'distill_items_raw_terminal_no_reopen',
                 'distill_items_raw_terminal_proof',
                 'ingestion_events',
                 'ingestion_events_local_owner',
                 'ingestion_events_no_delete',
                 'ingestion_events_no_update',
                 'ingestion_events_observation_typed',
                 'ingestion_events_proof_unavailable',
                 'ingestion_events_subject',
                 'source_media_ingestion_capture_binding',
                 'source_media_ingestion_capture_no_delete',
                 'source_media_ingestion_capture_release',
                 'source_media_ingestion_no_delete',
                 'source_media_ingestion_no_update',
                 'submitted_sources_ingestion_no_delete',
                 'submitted_sources_ingestion_no_release',
                 'submitted_sources_ingestion_owner_immutable',
                 'submitted_sources_local_insert',
                 'submitted_sources_local_no_delete',
                 'submitted_sources_local_tuple',
                 'wiki_outcome_one_accepted_batch',
                 'wiki_outcome_receipts',
                 'wiki_outcome_receipts_no_delete',
                 'wiki_outcome_receipts_no_update',
                 'wiki_outcome_receipts_publish_unavailable',
                 'wiki_task_outcome_binding_immutable',
                 'wiki_task_outcome_binding_required'),
      'ddl': {'collection_operations': ('table',
                                        'collection_operations',
                                        'CREATE TABLE collection_operations (\n'
                                        '        operation_id INTEGER PRIMARY KEY,\n'
                                        '        kind TEXT NOT NULL, source_key TEXT NOT NULL, title TEXT '
                                        'NOT NULL,\n'
                                        '        manifest_json TEXT NOT NULL, signature TEXT NOT NULL, '
                                        'content_signature TEXT NOT NULL,\n'
                                        '        authority_json TEXT NOT NULL, confirmation_token TEXT NOT '
                                        'NULL UNIQUE,\n'
                                        '        state TEXT NOT NULL CHECK(state IN '
                                        "('queued','working','waiting_user','partial','failed','succeeded','cancelled')),\n"
                                        '        consequence TEXT CHECK(consequence IN '
                                        "('complete','partial','failed')),\n"
                                        '        error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT '
                                        '0 CHECK(cancel_requested IN (0,1)),\n'
                                        '        revision INTEGER NOT NULL DEFAULT 1,\n'
                                        '        queued_at TEXT NOT NULL, created_at TEXT NOT NULL, '
                                        'updated_at TEXT NOT NULL,\n'
                                        '        UNIQUE(kind,source_key,signature,content_signature)\n'
                                        '    )'),
              'distill_items': ('table',
                                'distill_items',
                                'CREATE TABLE distill_items (\n'
                                '    item_id INTEGER PRIMARY KEY,\n'
                                '    submitted_url TEXT NOT NULL,\n'
                                '    state TEXT NOT NULL CHECK (\n'
                                "        state IN ('queued', 'working', 'waiting_user', 'succeeded', "
                                "'failed')\n"
                                '    ),\n'
                                '    phase TEXT NOT NULL CHECK (\n'
                                "        phase IN ('collecting', 'reviewing', 'distilling', 'publishing', "
                                "'done')\n"
                                '    ),\n'
                                '    material_id INTEGER REFERENCES materials(material_id),\n'
                                '    error_code TEXT,\n'
                                '    rejection_reason TEXT,\n'
                                '    dismissed_at TEXT,\n'
                                '    confirmation_json TEXT,\n'
                                '    queued_at TEXT NOT NULL,\n'
                                '    created_at TEXT NOT NULL,\n'
                                '    updated_at TEXT NOT NULL\n'
                                ", platform_authority_json TEXT NOT NULL DEFAULT '{}', submitted_title TEXT "
                                "NOT NULL DEFAULT '', review_revision INTEGER NOT NULL DEFAULT 0)"),
              'submitted_sources': ('table',
                                    'submitted_sources',
                                    'CREATE TABLE submitted_sources (\n'
                                    '    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),\n'
                                    "    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', "
                                    "'markdown', 'pdf', 'epub', 'image')),\n"
                                    '    input_key TEXT NOT NULL,\n'
                                    '    input_label TEXT NOT NULL,\n'
                                    '    input_metadata TEXT NOT NULL,\n'
                                    '    content BLOB,\n'
                                    '    retain_until TEXT,\n'
                                    '    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),\n'
                                    '    UNIQUE(input_kind, input_key)\n'
                                    ')'),
              'wiki_tasks': ('table',
                             'wiki_tasks',
                             'CREATE TABLE wiki_tasks (\n'
                             '        task_id TEXT PRIMARY KEY CHECK (\n'
                             '            length(task_id) = 32\n'
                             "            AND task_id NOT GLOB '*[^0-9a-f]*'\n"
                             '        ),\n'
                             "        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),\n"
                             '        vault_key TEXT NOT NULL CHECK (length(vault_key) = 64),\n'
                             "        request_kind TEXT NOT NULL CHECK (request_kind IN ('one_batch', "
                             "'all')),\n"
                             '        trigger_source TEXT NOT NULL CHECK (\n'
                             "            trigger_source IN ('local_web', 'claudian', 'cli')\n"
                             '        ),\n'
                             "        backend TEXT NOT NULL CHECK (backend IN ('codex_cli')),\n"
                             '        model TEXT NOT NULL CHECK (\n'
                             '            length(model) BETWEEN 1 AND 80\n'
                             "            AND model NOT GLOB '*[^A-Za-z0-9._-]*'\n"
                             '        ),\n'
                             '        effort TEXT NOT NULL CHECK (\n'
                             "            effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', "
                             "'max', 'ultra')\n"
                             '        ),\n'
                             '        kit_version TEXT NOT NULL CHECK (\n'
                             '            length(kit_version) BETWEEN 1 AND 40\n'
                             "            AND kit_version NOT GLOB '*[^A-Za-z0-9._-]*'\n"
                             '        ),\n'
                             '        kit_manifest_sha256 TEXT NOT NULL CHECK (length(kit_manifest_sha256) = '
                             '64),\n'
                             '        boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),\n'
                             '        state TEXT NOT NULL CHECK (\n'
                             '            state IN (\n'
                             "                'queued', 'preparing', 'running', 'validating', 'publishing',\n"
                             "                'succeeded', 'failed'\n"
                             '            )\n'
                             '        ),\n'
                             '        raw_count INTEGER NOT NULL CHECK (raw_count >= 0),\n'
                             '        batch_count INTEGER NOT NULL CHECK (batch_count >= 0),\n'
                             '        completed_batch_count INTEGER NOT NULL DEFAULT 0 CHECK (\n'
                             '            completed_batch_count >= 0 AND completed_batch_count <= '
                             'batch_count\n'
                             '        ),\n'
                             '        error_code TEXT CHECK (error_code IS NULL OR error_code IN '
                             "('vault_busy', 'config_required', 'kit_missing', 'kit_drift', "
                             "'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', "
                             "'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', "
                             "'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', "
                             "'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', "
                             "'internal_error')),\n"
                             "        recovery_state TEXT NOT NULL DEFAULT 'not_needed' CHECK (\n"
                             "            recovery_state IN ('not_needed', 'required', 'running', "
                             "'succeeded', 'failed')\n"
                             '        ),\n'
                             "        recovery_phase TEXT NOT NULL DEFAULT 'none' CHECK (\n"
                             "            recovery_phase IN ('none', 'staging', 'publishing', 'readback')\n"
                             '        ),\n'
                             "        created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),\n"
                             "        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''),\n"
                             "        CHECK ((state = 'failed') = (error_code IS NOT NULL)),\n"
                             "        CHECK ((recovery_state = 'not_needed') = (recovery_phase = 'none')),\n"
                             '        UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256, backend, '
                             'model, effort)\n'
                             '    )')},
      'abi_remove': ('ingestion_events', 'wiki_outcome_receipts'),
      'abi': {'collection_operations': (((0, 'operation_id', 'INTEGER', 0, None, 1, 0),
                                         (1, 'kind', 'TEXT', 1, None, 0, 0),
                                         (2, 'source_key', 'TEXT', 1, None, 0, 0),
                                         (3, 'title', 'TEXT', 1, None, 0, 0),
                                         (4, 'manifest_json', 'TEXT', 1, None, 0, 0),
                                         (5, 'signature', 'TEXT', 1, None, 0, 0),
                                         (6, 'content_signature', 'TEXT', 1, None, 0, 0),
                                         (7, 'authority_json', 'TEXT', 1, None, 0, 0),
                                         (8, 'confirmation_token', 'TEXT', 1, None, 0, 0),
                                         (9, 'state', 'TEXT', 1, None, 0, 0),
                                         (10, 'consequence', 'TEXT', 0, None, 0, 0),
                                         (11, 'error_code', 'TEXT', 0, None, 0, 0),
                                         (12, 'cancel_requested', 'INTEGER', 1, '0', 0, 0),
                                         (13, 'revision', 'INTEGER', 1, '1', 0, 0),
                                         (14, 'queued_at', 'TEXT', 1, None, 0, 0),
                                         (15, 'created_at', 'TEXT', 1, None, 0, 0),
                                         (16, 'updated_at', 'TEXT', 1, None, 0, 0)),
                                        ()),
              'distill_items': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                                 (1, 'submitted_url', 'TEXT', 1, None, 0, 0),
                                 (2, 'state', 'TEXT', 1, None, 0, 0),
                                 (3, 'phase', 'TEXT', 1, None, 0, 0),
                                 (4, 'material_id', 'INTEGER', 0, None, 0, 0),
                                 (5, 'error_code', 'TEXT', 0, None, 0, 0),
                                 (6, 'rejection_reason', 'TEXT', 0, None, 0, 0),
                                 (7, 'dismissed_at', 'TEXT', 0, None, 0, 0),
                                 (8, 'confirmation_json', 'TEXT', 0, None, 0, 0),
                                 (9, 'queued_at', 'TEXT', 1, None, 0, 0),
                                 (10, 'created_at', 'TEXT', 1, None, 0, 0),
                                 (11, 'updated_at', 'TEXT', 1, None, 0, 0),
                                 (12, 'platform_authority_json', 'TEXT', 1, "'{}'", 0, 0),
                                 (13, 'submitted_title', 'TEXT', 1, "''", 0, 0),
                                 (14, 'review_revision', 'INTEGER', 1, '0', 0, 0)),
                                ((0,
                                  0,
                                  'materials',
                                  'material_id',
                                  'material_id',
                                  'NO ACTION',
                                  'NO ACTION',
                                  'NONE'),)),
              'submitted_sources': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                                     (1, 'input_kind', 'TEXT', 1, None, 0, 0),
                                     (2, 'input_key', 'TEXT', 1, None, 0, 0),
                                     (3, 'input_label', 'TEXT', 1, None, 0, 0),
                                     (4, 'input_metadata', 'TEXT', 1, None, 0, 0),
                                     (5, 'content', 'BLOB', 0, None, 0, 0),
                                     (6, 'retain_until', 'TEXT', 0, None, 0, 0),
                                     (7, 'retryable', 'INTEGER', 1, '1', 0, 0)),
                                    ((0,
                                      0,
                                      'distill_items',
                                      'item_id',
                                      'item_id',
                                      'NO ACTION',
                                      'NO ACTION',
                                      'NONE'),)),
              'wiki_tasks': (((0, 'task_id', 'TEXT', 0, None, 1, 0),
                              (1, 'vault_path', 'TEXT', 1, None, 0, 0),
                              (2, 'vault_key', 'TEXT', 1, None, 0, 0),
                              (3, 'request_kind', 'TEXT', 1, None, 0, 0),
                              (4, 'trigger_source', 'TEXT', 1, None, 0, 0),
                              (5, 'backend', 'TEXT', 1, None, 0, 0),
                              (6, 'model', 'TEXT', 1, None, 0, 0),
                              (7, 'effort', 'TEXT', 1, None, 0, 0),
                              (8, 'kit_version', 'TEXT', 1, None, 0, 0),
                              (9, 'kit_manifest_sha256', 'TEXT', 1, None, 0, 0),
                              (10, 'boundary_sha256', 'TEXT', 1, None, 0, 0),
                              (11, 'state', 'TEXT', 1, None, 0, 0),
                              (12, 'raw_count', 'INTEGER', 1, None, 0, 0),
                              (13, 'batch_count', 'INTEGER', 1, None, 0, 0),
                              (14, 'completed_batch_count', 'INTEGER', 1, '0', 0, 0),
                              (15, 'error_code', 'TEXT', 0, None, 0, 0),
                              (16, 'recovery_state', 'TEXT', 1, "'not_needed'", 0, 0),
                              (17, 'recovery_phase', 'TEXT', 1, "'none'", 0, 0),
                              (18, 'created_at', 'TEXT', 1, None, 0, 0),
                              (19, 'updated_at', 'TEXT', 1, None, 0, 0)),
                             ())},
      'index_remove': ('ingestion_events_local_owner',
                       'ingestion_events_subject',
                       'sqlite_autoindex_ingestion_events_1',
                       'sqlite_autoindex_wiki_outcome_receipts_1',
                       'wiki_outcome_one_accepted_batch'),
      'index': {'sqlite_autoindex_submitted_sources_1': ('submitted_sources',
                                                         1,
                                                         'u',
                                                         0,
                                                         ((0, 1, 'input_kind', 0, 'BINARY', 1),
                                                          (1, 2, 'input_key', 0, 'BINARY', 1),
                                                          (2, -1, None, 0, 'BINARY', 0)))}},
 25: {'remove': ('ingestion_events_local_owner',
                 'submitted_sources_local_insert',
                 'submitted_sources_local_no_delete',
                 'submitted_sources_local_tuple'),
      'ddl': {'submitted_sources': ('table',
                                    'submitted_sources',
                                    'CREATE TABLE submitted_sources (\n'
                                    '    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),\n'
                                    "    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', "
                                    "'markdown', 'pdf', 'epub', 'image')),\n"
                                    '    input_key TEXT NOT NULL,\n'
                                    '    input_label TEXT NOT NULL,\n'
                                    '    input_metadata TEXT NOT NULL,\n'
                                    '    content BLOB,\n'
                                    '    retain_until TEXT,\n'
                                    '    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),\n'
                                    '    UNIQUE(input_kind, input_key)\n'
                                    ')'),
              'ingestion_events_observation_typed': ('trigger',
                                                     'ingestion_events',
                                                     'CREATE TRIGGER ingestion_events_observation_typed\n'
                                                     '        BEFORE INSERT ON ingestion_events WHEN '
                                                     "NEW.kind IN ('source_ready','raw_pending')\n"
                                                     '          AND COALESCE(NOT (\n'
                                                     "            NEW.subject_kind='item' AND "
                                                     'NEW.subject_id=NEW.item_id\n'
                                                     '            AND EXISTS (SELECT 1 FROM distill_items i '
                                                     'WHERE i.item_id=NEW.item_id\n'
                                                     '                        AND '
                                                     'i.ingestion_contract=NEW.contract\n'
                                                     '                        AND '
                                                     "i.source_binding_sha256=json_extract(NEW.detail_json,'$.source_binding_sha256')\n"
                                                     '                        AND '
                                                     "i.relation_binding_sha256=json_extract(NEW.detail_json,'$.relation_binding_sha256'))\n"
                                                     '            AND (SELECT count(*) FROM '
                                                     'json_each(NEW.detail_json))=4\n'
                                                     '            AND (SELECT count(DISTINCT key) FROM '
                                                     'json_each(NEW.detail_json))=4\n'
                                                     '            AND NOT EXISTS (SELECT 1 FROM '
                                                     'json_each(NEW.detail_json)\n'
                                                     '                            WHERE key NOT IN '
                                                     "('code','manifest','source_binding_sha256','relation_binding_sha256'))\n"
                                                     '            AND '
                                                     "json_type(NEW.detail_json,'$.manifest')='object'\n"
                                                     '            AND (SELECT count(*) FROM '
                                                     "json_each(NEW.detail_json,'$.manifest'))=2\n"
                                                     '            AND (SELECT count(DISTINCT key) FROM '
                                                     "json_each(NEW.detail_json,'$.manifest'))=2\n"
                                                     '            AND NOT EXISTS (SELECT 1 FROM '
                                                     "json_each(NEW.detail_json,'$.manifest')\n"
                                                     '                            WHERE key NOT IN '
                                                     "('source_fact_id','snapshot_sha256'))\n"
                                                     "            AND ((NEW.kind='source_ready' AND "
                                                     "json_extract(NEW.detail_json,'$.code')='source_fact_ready')\n"
                                                     "                 OR (NEW.kind='raw_pending' AND "
                                                     "json_extract(NEW.detail_json,'$.code')\n"
                                                     '                     IN '
                                                     "('context_pending','readback_pending','writer_pending')))\n"
                                                     '            AND '
                                                     "((json_type(NEW.detail_json,'$.manifest.source_fact_id')='null'\n"
                                                     '                  AND '
                                                     "json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='null'\n"
                                                     "                  AND NEW.kind='raw_pending')\n"
                                                     '                 OR '
                                                     "(json_type(NEW.detail_json,'$.manifest.source_fact_id')='integer'\n"
                                                     '                     AND '
                                                     "json_extract(NEW.detail_json,'$.manifest.source_fact_id')>0\n"
                                                     '                     AND '
                                                     "json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='text'\n"
                                                     '                     AND '
                                                     "length(json_extract(NEW.detail_json,'$.manifest.snapshot_sha256'))=64\n"
                                                     '                     AND '
                                                     "json_extract(NEW.detail_json,'$.manifest.snapshot_sha256') "
                                                     "NOT GLOB '*[^0-9a-f]*'))\n"
                                                     '          ),1)\n'
                                                     "        BEGIN SELECT RAISE(ABORT,'ingestion "
                                                     "observation invalid'); END")},
      'abi_remove': (),
      'abi': {'submitted_sources': (((0, 'item_id', 'INTEGER', 0, None, 1, 0),
                                     (1, 'input_kind', 'TEXT', 1, None, 0, 0),
                                     (2, 'input_key', 'TEXT', 1, None, 0, 0),
                                     (3, 'input_label', 'TEXT', 1, None, 0, 0),
                                     (4, 'input_metadata', 'TEXT', 1, None, 0, 0),
                                     (5, 'content', 'BLOB', 0, None, 0, 0),
                                     (6, 'retain_until', 'TEXT', 0, None, 0, 0),
                                     (7, 'retryable', 'INTEGER', 1, '1', 0, 0)),
                                    ((0,
                                      0,
                                      'distill_items',
                                      'item_id',
                                      'item_id',
                                      'NO ACTION',
                                      'NO ACTION',
                                      'NONE'),))},
      'index_remove': ('ingestion_events_local_owner',),
      'index': {'sqlite_autoindex_submitted_sources_1': ('submitted_sources',
                                                         1,
                                                         'u',
                                                         0,
                                                         ((0, 1, 'input_kind', 0, 'BINARY', 1),
                                                          (1, 2, 'input_key', 0, 'BINARY', 1),
                                                          (2, -1, None, 0, 'BINARY', 0)))}}}
