"""Durable V3 wiki workflow tasks, observations and frozen raw boundaries."""
from __future__ import annotations

import sqlite3


ERROR_CODES = (
    "vault_busy",
    "config_required",
    "kit_missing",
    "kit_drift",
    "kit_incompatible",
    "protocol_error",
    "raw_path_invalid",
    "raw_symlink",
    "raw_changed",
    "runner_unavailable",
    "runner_timeout",
    "model_unavailable",
    "network_error",
    "agent_failed",
    "validation_failed",
    "publish_conflict",
    "publish_interrupted",
    "readback_failed",
    "interrupted",
    "recovery_failed",
    "internal_error",
)

_ERROR_CHECK = ", ".join(repr(value) for value in ERROR_CODES)

STATEMENTS = (
    f"""CREATE TABLE wiki_tasks (
        task_id TEXT PRIMARY KEY CHECK (
            length(task_id) = 32
            AND task_id NOT GLOB '*[^0-9a-f]*'
        ),
        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),
        vault_key TEXT NOT NULL CHECK (length(vault_key) = 64),
        request_kind TEXT NOT NULL CHECK (request_kind IN ('one_batch', 'all')),
        trigger_source TEXT NOT NULL CHECK (
            trigger_source IN ('local_web', 'claudian', 'cli')
        ),
        backend TEXT NOT NULL CHECK (backend IN ('codex_cli')),
        model TEXT NOT NULL CHECK (
            length(model) BETWEEN 1 AND 80
            AND model NOT GLOB '*[^A-Za-z0-9._-]*'
        ),
        effort TEXT NOT NULL CHECK (
            effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
        ),
        kit_version TEXT NOT NULL CHECK (
            length(kit_version) BETWEEN 1 AND 40
            AND kit_version NOT GLOB '*[^A-Za-z0-9._-]*'
        ),
        kit_manifest_sha256 TEXT NOT NULL CHECK (length(kit_manifest_sha256) = 64),
        boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),
        state TEXT NOT NULL CHECK (
            state IN (
                'queued', 'preparing', 'running', 'validating', 'publishing',
                'succeeded', 'failed'
            )
        ),
        raw_count INTEGER NOT NULL CHECK (raw_count >= 0),
        batch_count INTEGER NOT NULL CHECK (batch_count >= 0),
        completed_batch_count INTEGER NOT NULL DEFAULT 0 CHECK (
            completed_batch_count >= 0 AND completed_batch_count <= batch_count
        ),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ({_ERROR_CHECK})),
        recovery_state TEXT NOT NULL DEFAULT 'not_needed' CHECK (
            recovery_state IN ('not_needed', 'required', 'running', 'succeeded', 'failed')
        ),
        recovery_phase TEXT NOT NULL DEFAULT 'none' CHECK (
            recovery_phase IN ('none', 'staging', 'publishing', 'readback')
        ),
        created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        CHECK ((recovery_state = 'not_needed') = (recovery_phase = 'none')),
        UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256, backend, model, effort)
    )""",
    """CREATE UNIQUE INDEX wiki_one_unresolved_task_per_vault
        ON wiki_tasks(vault_key)
        WHERE state IN ('queued', 'preparing', 'running', 'validating', 'publishing')""",
    f"""CREATE TABLE wiki_task_batches (
        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),
        batch_no INTEGER NOT NULL CHECK (batch_no > 0),
        state TEXT NOT NULL CHECK (
            state IN (
                'queued', 'preparing', 'running', 'validating', 'publishing',
                'succeeded', 'failed'
            )
        ),
        item_count INTEGER NOT NULL CHECK (item_count > 0),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ({_ERROR_CHECK})),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        PRIMARY KEY(task_id, batch_no)
    )""",
    """CREATE TABLE wiki_task_raw (
        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        batch_no INTEGER NOT NULL CHECK (batch_no > 0),
        raw_id TEXT NOT NULL CHECK (
            raw_id GLOB 'R-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'
        ),
        identity TEXT NOT NULL CHECK (identity IN ('第三方', '本人', '本人附言')),
        relative_path TEXT NOT NULL CHECK (
            TRIM(relative_path) != '' AND relative_path LIKE 'raw/%'
        ),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
        PRIMARY KEY(task_id, ordinal),
        UNIQUE(task_id, relative_path),
        FOREIGN KEY(task_id, batch_no)
            REFERENCES wiki_task_batches(task_id, batch_no)
    )""",
    """CREATE TRIGGER wiki_task_state_transition
        BEFORE UPDATE OF state ON wiki_tasks
        WHEN NOT (
            OLD.state = NEW.state
            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))
            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))
            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))
            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))
            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))
            OR (OLD.state = 'failed' AND NEW.state = 'queued'
                AND NEW.recovery_state IN ('not_needed', 'succeeded'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki task transition'); END""",
    """CREATE TRIGGER wiki_batch_state_transition
        BEFORE UPDATE OF state ON wiki_task_batches
        WHEN NOT (
            OLD.state = NEW.state
            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))
            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))
            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))
            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))
            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))
            OR (OLD.state = 'failed' AND NEW.state = 'queued')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki batch transition'); END""",
    """CREATE TRIGGER wiki_task_recovery_transition
        BEFORE UPDATE OF recovery_state ON wiki_tasks
        WHEN NOT (
            OLD.recovery_state = NEW.recovery_state
            OR (OLD.recovery_state = 'not_needed' AND NEW.recovery_state = 'required')
            OR (OLD.recovery_state = 'required' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'running' AND NEW.recovery_state IN ('succeeded', 'failed'))
            OR (OLD.recovery_state = 'failed' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'succeeded' AND NEW.recovery_state = 'not_needed')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki recovery transition'); END""",
    """CREATE TRIGGER wiki_task_boundary_immutable
        BEFORE UPDATE OF task_id, vault_path, vault_key, request_kind, trigger_source,
            backend, model, effort, kit_version, kit_manifest_sha256,
            boundary_sha256, raw_count, batch_count ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task boundary is immutable'); END""",
    """CREATE TRIGGER wiki_task_no_delete
        BEFORE DELETE ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task is durable'); END""",
    """CREATE TRIGGER wiki_task_raw_immutable
        BEFORE UPDATE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END""",
    """CREATE TRIGGER wiki_task_raw_no_delete
        BEFORE DELETE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END""",
    """CREATE TRIGGER wiki_batch_identity_immutable
        BEFORE UPDATE OF task_id, batch_no, item_count ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch identity is immutable'); END""",
    """CREATE TRIGGER wiki_batch_no_delete
        BEFORE DELETE ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch is durable'); END""",
)

OBSERVATION_STATEMENTS = (
    f"""CREATE TABLE wiki_observations (
        vault_key TEXT PRIMARY KEY CHECK (length(vault_key) = 64),
        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),
        task_id TEXT REFERENCES wiki_tasks(task_id),
        pending_count INTEGER CHECK (pending_count IS NULL OR pending_count >= 0),
        candidate_count INTEGER CHECK (candidate_count IS NULL OR candidate_count >= 0),
        observed_at TEXT NOT NULL CHECK (TRIM(observed_at) != ''),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ({_ERROR_CHECK})),
        CHECK (
            (error_code IS NULL AND pending_count IS NOT NULL AND candidate_count IS NOT NULL)
            OR (error_code IS NOT NULL AND pending_count IS NULL AND candidate_count IS NULL)
        )
    )""",
)


def migrate(connection: sqlite3.Connection) -> None:
    for statement in STATEMENTS:
        connection.execute(statement)


def migrate_observations(connection: sqlite3.Connection) -> None:
    for statement in OBSERVATION_STATEMENTS:
        connection.execute(statement)


def migrate_v23(connection: sqlite3.Connection) -> None:
    """Rebuild only the V3 domain to permit an explicit new configured attempt."""
    connection.execute("DROP INDEX wiki_one_unresolved_task_per_vault")
    for name in (
        "wiki_task_state_transition", "wiki_batch_state_transition",
        "wiki_task_recovery_transition", "wiki_task_boundary_immutable",
        "wiki_task_no_delete", "wiki_task_raw_immutable",
        "wiki_task_raw_no_delete", "wiki_batch_identity_immutable",
        "wiki_batch_no_delete",
    ):
        connection.execute(f"DROP TRIGGER {name}")
    for name in ("wiki_task_raw", "wiki_task_batches", "wiki_tasks"):
        connection.execute(f"ALTER TABLE {name} RENAME TO {name}_v22")
    for statement in STATEMENTS:
        connection.execute(statement)
    task_columns = (
        "task_id,vault_path,vault_key,request_kind,trigger_source,backend,model,effort,"
        "kit_version,kit_manifest_sha256,boundary_sha256,state,raw_count,batch_count,"
        "completed_batch_count,error_code,recovery_state,recovery_phase,created_at,updated_at"
    )
    connection.execute(
        f"INSERT INTO wiki_tasks({task_columns}) SELECT {task_columns} FROM wiki_tasks_v22")
    connection.execute("INSERT INTO wiki_task_batches SELECT * FROM wiki_task_batches_v22")
    connection.execute("INSERT INTO wiki_task_raw SELECT * FROM wiki_task_raw_v22")
    for name in ("wiki_task_raw_v22", "wiki_task_batches_v22", "wiki_tasks_v22"):
        connection.execute(f"DROP TABLE {name}")
    migrate_observations(connection)


OUTCOME_COLUMNS = (
    "outcome_contract TEXT NOT NULL DEFAULT 'legacy' CHECK(outcome_contract IN ('legacy','r08-wiki-outcomes-v1'))",
    "plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(plan_json) AND json_type(plan_json)='object')",
    "plan_sha256 TEXT CHECK(plan_sha256 IS NULL OR (length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'))",
)


def migrate_outcome_storage(connection: sqlite3.Connection) -> None:
    """Reserve outcome storage without changing claims, scheduling or dedup."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info(wiki_tasks)')}
    for definition in OUTCOME_COLUMNS:
        if definition.split()[0] not in columns:
            connection.execute(f'ALTER TABLE wiki_tasks ADD COLUMN {definition}')
    connection.execute("""CREATE TRIGGER IF NOT EXISTS wiki_task_outcome_binding_immutable
        BEFORE UPDATE OF outcome_contract,plan_json,plan_sha256 ON wiki_tasks
        WHEN NEW.outcome_contract IS NOT OLD.outcome_contract OR NEW.plan_json IS NOT OLD.plan_json
          OR NEW.plan_sha256 IS NOT OLD.plan_sha256
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding is immutable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS wiki_task_outcome_binding_required
        BEFORE INSERT ON wiki_tasks WHEN NEW.outcome_contract!='legacy' AND NEW.plan_sha256 IS NULL
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding required'); END""")
    connection.execute("""CREATE TABLE IF NOT EXISTS wiki_outcome_receipts (
        receipt_id TEXT NOT NULL CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
        task_id TEXT NOT NULL,
        batch_no INTEGER NOT NULL CHECK(batch_no>0),
        phase TEXT NOT NULL CHECK(phase IN ('validated','accepted')),
        contract TEXT NOT NULL CHECK(contract='r08-wiki-outcomes-v1'),
        boundary_sha256 TEXT NOT NULL CHECK(length(boundary_sha256)=64 AND boundary_sha256 NOT GLOB '*[^0-9a-f]*'),
        plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
        created_at TEXT NOT NULL CHECK(trim(created_at)!=''),
        PRIMARY KEY(receipt_id,phase),
        FOREIGN KEY(task_id,batch_no) REFERENCES wiki_task_batches(task_id,batch_no)
    )""")
    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS wiki_outcome_one_accepted_batch
        ON wiki_outcome_receipts(task_id,batch_no) WHERE phase='accepted'""")
    for action in ('UPDATE', 'DELETE'):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS wiki_outcome_receipts_no_{action.lower()}
            BEFORE {action} ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS wiki_outcome_receipts_publish_unavailable
        BEFORE INSERT ON wiki_outcome_receipts WHEN NEW.phase='accepted'
        BEGIN SELECT RAISE(ABORT,'publish proof unavailable'); END""")


def migrate_v27(connection: sqlite3.Connection) -> None:
    """Caller owns the single transaction, preflight and full preservation audit.

    Install new INSERT policy only after copying grandfathered successful rows.
    Children retain their table, rowid, FK and complete index ABI.
    """
    if not connection.in_transaction or connection.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise RuntimeError('schema27 migration connection required')
    columns = (
        'task_id', 'vault_path', 'vault_key', 'request_kind', 'trigger_source',
        'backend', 'model', 'effort', 'kit_version', 'kit_manifest_sha256',
        'boundary_sha256', 'state', 'raw_count', 'batch_count',
        'completed_batch_count', 'error_code', 'recovery_state', 'recovery_phase',
        'created_at', 'updated_at', 'outcome_contract', 'plan_json', 'plan_sha256',
    )
    parent = SCHEMA27_WIKI_DDL['wiki_tasks'][2]
    connection.execute(parent.replace('CREATE TABLE wiki_tasks (',
                                      'CREATE TABLE wiki_tasks_v27 (', 1))
    projection = ','.join('"' + name + '"' for name in columns)
    connection.execute(f'INSERT INTO wiki_tasks_v27(rowid,{projection}) '
                       f'SELECT rowid,{projection} FROM wiki_tasks ORDER BY rowid')
    owned = tuple(name for name, (kind, table, _) in SCHEMA26_WIKI_DDL.items()
                  if table == 'wiki_tasks' and kind != 'table')
    for name in owned:
        kind = SCHEMA26_WIKI_DDL[name][0]
        connection.execute(f'DROP {kind.upper()} "{name}"')
    connection.execute('DROP TABLE wiki_tasks')
    connection.execute('ALTER TABLE wiki_tasks_v27 RENAME TO wiki_tasks')
    for name, (kind, table, sql) in SCHEMA27_WIKI_DDL.items():
        if table == 'wiki_tasks' and kind != 'table':
            connection.execute(sql)
    connection.execute('DROP TRIGGER wiki_outcome_receipts_publish_unavailable')
    for name in ('wiki_outcome_receipts_publish_unavailable',
                 'wiki_batch_typed_success_requires_accepted',
                 'wiki_batch_typed_insert_requires_queued'):
        connection.execute(SCHEMA27_WIKI_DDL[name][2])


# Independent historical storage spelling, derived from primary AST;
# not from a live database or a schema27 downgrade. Old migrations stay intact.
SCHEMA26_WIKI_DDL = {
    'wiki_tasks': ('table', 'wiki_tasks', """CREATE TABLE wiki_tasks (
        task_id TEXT PRIMARY KEY CHECK (
            length(task_id) = 32
            AND task_id NOT GLOB '*[^0-9a-f]*'
        ),
        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),
        vault_key TEXT NOT NULL CHECK (length(vault_key) = 64),
        request_kind TEXT NOT NULL CHECK (request_kind IN ('one_batch', 'all')),
        trigger_source TEXT NOT NULL CHECK (
            trigger_source IN ('local_web', 'claudian', 'cli')
        ),
        backend TEXT NOT NULL CHECK (backend IN ('codex_cli')),
        model TEXT NOT NULL CHECK (
            length(model) BETWEEN 1 AND 80
            AND model NOT GLOB '*[^A-Za-z0-9._-]*'
        ),
        effort TEXT NOT NULL CHECK (
            effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
        ),
        kit_version TEXT NOT NULL CHECK (
            length(kit_version) BETWEEN 1 AND 40
            AND kit_version NOT GLOB '*[^A-Za-z0-9._-]*'
        ),
        kit_manifest_sha256 TEXT NOT NULL CHECK (length(kit_manifest_sha256) = 64),
        boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),
        state TEXT NOT NULL CHECK (
            state IN (
                'queued', 'preparing', 'running', 'validating', 'publishing',
                'succeeded', 'failed'
            )
        ),
        raw_count INTEGER NOT NULL CHECK (raw_count >= 0),
        batch_count INTEGER NOT NULL CHECK (batch_count >= 0),
        completed_batch_count INTEGER NOT NULL DEFAULT 0 CHECK (
            completed_batch_count >= 0 AND completed_batch_count <= batch_count
        ),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),
        recovery_state TEXT NOT NULL DEFAULT 'not_needed' CHECK (
            recovery_state IN ('not_needed', 'required', 'running', 'succeeded', 'failed')
        ),
        recovery_phase TEXT NOT NULL DEFAULT 'none' CHECK (
            recovery_phase IN ('none', 'staging', 'publishing', 'readback')
        ),
        created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''), outcome_contract TEXT NOT NULL DEFAULT 'legacy' CHECK(outcome_contract IN ('legacy','r08-wiki-outcomes-v1')), plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(plan_json) AND json_type(plan_json)='object'), plan_sha256 TEXT CHECK(plan_sha256 IS NULL OR (length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*')),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        CHECK ((recovery_state = 'not_needed') = (recovery_phase = 'none')),
        UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256, backend, model, effort)
    )"""),
    'wiki_one_unresolved_task_per_vault': ('index', 'wiki_tasks', """CREATE UNIQUE INDEX wiki_one_unresolved_task_per_vault
        ON wiki_tasks(vault_key)
        WHERE state IN ('queued', 'preparing', 'running', 'validating', 'publishing')"""),
    'wiki_task_batches': ('table', 'wiki_task_batches', """CREATE TABLE wiki_task_batches (
        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),
        batch_no INTEGER NOT NULL CHECK (batch_no > 0),
        state TEXT NOT NULL CHECK (
            state IN (
                'queued', 'preparing', 'running', 'validating', 'publishing',
                'succeeded', 'failed'
            )
        ),
        item_count INTEGER NOT NULL CHECK (item_count > 0),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        PRIMARY KEY(task_id, batch_no)
    )"""),
    'wiki_task_raw': ('table', 'wiki_task_raw', """CREATE TABLE wiki_task_raw (
        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        batch_no INTEGER NOT NULL CHECK (batch_no > 0),
        raw_id TEXT NOT NULL CHECK (
            raw_id GLOB 'R-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'
        ),
        identity TEXT NOT NULL CHECK (identity IN ('第三方', '本人', '本人附言')),
        relative_path TEXT NOT NULL CHECK (
            TRIM(relative_path) != '' AND relative_path LIKE 'raw/%'
        ),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
        PRIMARY KEY(task_id, ordinal),
        UNIQUE(task_id, relative_path),
        FOREIGN KEY(task_id, batch_no)
            REFERENCES wiki_task_batches(task_id, batch_no)
    )"""),
    'wiki_task_state_transition': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_state_transition
        BEFORE UPDATE OF state ON wiki_tasks
        WHEN NOT (
            OLD.state = NEW.state
            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))
            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))
            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))
            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))
            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))
            OR (OLD.state = 'failed' AND NEW.state = 'queued'
                AND NEW.recovery_state IN ('not_needed', 'succeeded'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki task transition'); END"""),
    'wiki_batch_state_transition': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_state_transition
        BEFORE UPDATE OF state ON wiki_task_batches
        WHEN NOT (
            OLD.state = NEW.state
            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))
            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))
            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))
            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))
            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))
            OR (OLD.state = 'failed' AND NEW.state = 'queued')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki batch transition'); END"""),
    'wiki_task_recovery_transition': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_recovery_transition
        BEFORE UPDATE OF recovery_state ON wiki_tasks
        WHEN NOT (
            OLD.recovery_state = NEW.recovery_state
            OR (OLD.recovery_state = 'not_needed' AND NEW.recovery_state = 'required')
            OR (OLD.recovery_state = 'required' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'running' AND NEW.recovery_state IN ('succeeded', 'failed'))
            OR (OLD.recovery_state = 'failed' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'succeeded' AND NEW.recovery_state = 'not_needed')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki recovery transition'); END"""),
    'wiki_task_boundary_immutable': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_boundary_immutable
        BEFORE UPDATE OF task_id, vault_path, vault_key, request_kind, trigger_source,
            backend, model, effort, kit_version, kit_manifest_sha256,
            boundary_sha256, raw_count, batch_count ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task boundary is immutable'); END"""),
    'wiki_task_no_delete': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_no_delete
        BEFORE DELETE ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task is durable'); END"""),
    'wiki_task_raw_immutable': ('trigger', 'wiki_task_raw', """CREATE TRIGGER wiki_task_raw_immutable
        BEFORE UPDATE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END"""),
    'wiki_task_raw_no_delete': ('trigger', 'wiki_task_raw', """CREATE TRIGGER wiki_task_raw_no_delete
        BEFORE DELETE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END"""),
    'wiki_batch_identity_immutable': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_identity_immutable
        BEFORE UPDATE OF task_id, batch_no, item_count ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch identity is immutable'); END"""),
    'wiki_batch_no_delete': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_no_delete
        BEFORE DELETE ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch is durable'); END"""),
    'wiki_observations': ('table', 'wiki_observations', """CREATE TABLE wiki_observations (
        vault_key TEXT PRIMARY KEY CHECK (length(vault_key) = 64),
        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),
        task_id TEXT REFERENCES wiki_tasks(task_id),
        pending_count INTEGER CHECK (pending_count IS NULL OR pending_count >= 0),
        candidate_count INTEGER CHECK (candidate_count IS NULL OR candidate_count >= 0),
        observed_at TEXT NOT NULL CHECK (TRIM(observed_at) != ''),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),
        CHECK (
            (error_code IS NULL AND pending_count IS NOT NULL AND candidate_count IS NOT NULL)
            OR (error_code IS NOT NULL AND pending_count IS NULL AND candidate_count IS NULL)
        )
    )"""),
    'wiki_task_outcome_binding_immutable': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_outcome_binding_immutable
        BEFORE UPDATE OF outcome_contract,plan_json,plan_sha256 ON wiki_tasks
        WHEN NEW.outcome_contract IS NOT OLD.outcome_contract OR NEW.plan_json IS NOT OLD.plan_json
          OR NEW.plan_sha256 IS NOT OLD.plan_sha256
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding is immutable'); END"""),
    'wiki_task_outcome_binding_required': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_outcome_binding_required
        BEFORE INSERT ON wiki_tasks WHEN NEW.outcome_contract!='legacy' AND NEW.plan_sha256 IS NULL
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding required'); END"""),
    'wiki_outcome_receipts': ('table', 'wiki_outcome_receipts', """CREATE TABLE wiki_outcome_receipts (
        receipt_id TEXT NOT NULL CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
        task_id TEXT NOT NULL,
        batch_no INTEGER NOT NULL CHECK(batch_no>0),
        phase TEXT NOT NULL CHECK(phase IN ('validated','accepted')),
        contract TEXT NOT NULL CHECK(contract='r08-wiki-outcomes-v1'),
        boundary_sha256 TEXT NOT NULL CHECK(length(boundary_sha256)=64 AND boundary_sha256 NOT GLOB '*[^0-9a-f]*'),
        plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
        created_at TEXT NOT NULL CHECK(trim(created_at)!=''),
        PRIMARY KEY(receipt_id,phase),
        FOREIGN KEY(task_id,batch_no) REFERENCES wiki_task_batches(task_id,batch_no)
    )"""),
    'wiki_outcome_one_accepted_batch': ('index', 'wiki_outcome_receipts', """CREATE UNIQUE INDEX wiki_outcome_one_accepted_batch
        ON wiki_outcome_receipts(task_id,batch_no) WHERE phase='accepted'"""),
    'wiki_outcome_receipts_no_update': ('trigger', 'wiki_outcome_receipts', """CREATE TRIGGER wiki_outcome_receipts_no_update
            BEFORE UPDATE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END"""),
    'wiki_outcome_receipts_no_delete': ('trigger', 'wiki_outcome_receipts', """CREATE TRIGGER wiki_outcome_receipts_no_delete
            BEFORE DELETE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END"""),
    'wiki_outcome_receipts_publish_unavailable': ('trigger', 'wiki_outcome_receipts', """CREATE TRIGGER wiki_outcome_receipts_publish_unavailable
        BEFORE INSERT ON wiki_outcome_receipts WHEN NEW.phase='accepted'
        BEGIN SELECT RAISE(ABORT,'publish proof unavailable'); END"""),
}

# Frozen proposal 96cf7a17...: six restored objects plus eight changes.
SCHEMA27_WIKI_DDL = {
    'wiki_tasks': ('table', 'wiki_tasks', """CREATE TABLE wiki_tasks (
        task_id TEXT PRIMARY KEY CHECK (
            length(task_id) = 32
            AND task_id NOT GLOB '*[^0-9a-f]*'
        ),
        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),
        vault_key TEXT NOT NULL CHECK (length(vault_key) = 64),
        request_kind TEXT NOT NULL CHECK (request_kind IN ('one_batch', 'all')),
        trigger_source TEXT NOT NULL CHECK (
            trigger_source IN ('local_web', 'claudian', 'cli')
        ),
        backend TEXT NOT NULL CHECK (backend IN ('codex_cli')),
        model TEXT NOT NULL CHECK (
            length(model) BETWEEN 1 AND 80
            AND model NOT GLOB '*[^A-Za-z0-9._-]*'
        ),
        effort TEXT NOT NULL CHECK (
            effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
        ),
        kit_version TEXT NOT NULL CHECK (
            length(kit_version) BETWEEN 1 AND 40
            AND kit_version NOT GLOB '*[^A-Za-z0-9._-]*'
        ),
        kit_manifest_sha256 TEXT NOT NULL CHECK (length(kit_manifest_sha256) = 64),
        boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),
        state TEXT NOT NULL CHECK (
            state IN (
                'queued', 'preparing', 'running', 'validating', 'publishing',
                'succeeded', 'failed'
            )
        ),
        raw_count INTEGER NOT NULL CHECK (raw_count >= 0),
        batch_count INTEGER NOT NULL CHECK (batch_count >= 0),
        completed_batch_count INTEGER NOT NULL DEFAULT 0 CHECK (
            completed_batch_count >= 0 AND completed_batch_count <= batch_count
        ),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),
        recovery_state TEXT NOT NULL DEFAULT 'not_needed' CHECK (
            recovery_state IN ('not_needed', 'required', 'running', 'succeeded', 'failed')
        ),
        recovery_phase TEXT NOT NULL DEFAULT 'none' CHECK (
            recovery_phase IN ('none', 'staging', 'publishing', 'readback')
        ),
        created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''), outcome_contract TEXT NOT NULL DEFAULT 'legacy' CHECK(outcome_contract IN ('legacy','r08-wiki-outcomes-v1')), plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(plan_json) AND json_type(plan_json)='object'), plan_sha256 TEXT CHECK(plan_sha256 IS NULL OR (length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*')),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        CHECK ((recovery_state = 'not_needed') = (recovery_phase = 'none'))
    )"""),
    'wiki_one_unresolved_task_per_vault': ('index', 'wiki_tasks', """CREATE UNIQUE INDEX wiki_one_unresolved_task_per_vault
        ON wiki_tasks(vault_key)
        WHERE state IN ('queued', 'preparing', 'running', 'validating', 'publishing')"""),
    'wiki_task_batches': ('table', 'wiki_task_batches', """CREATE TABLE wiki_task_batches (
        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),
        batch_no INTEGER NOT NULL CHECK (batch_no > 0),
        state TEXT NOT NULL CHECK (
            state IN (
                'queued', 'preparing', 'running', 'validating', 'publishing',
                'succeeded', 'failed'
            )
        ),
        item_count INTEGER NOT NULL CHECK (item_count > 0),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        PRIMARY KEY(task_id, batch_no)
    )"""),
    'wiki_task_raw': ('table', 'wiki_task_raw', """CREATE TABLE wiki_task_raw (
        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        batch_no INTEGER NOT NULL CHECK (batch_no > 0),
        raw_id TEXT NOT NULL CHECK (
            raw_id GLOB 'R-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'
        ),
        identity TEXT NOT NULL CHECK (identity IN ('第三方', '本人', '本人附言')),
        relative_path TEXT NOT NULL CHECK (
            TRIM(relative_path) != '' AND relative_path LIKE 'raw/%'
        ),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
        PRIMARY KEY(task_id, ordinal),
        UNIQUE(task_id, relative_path),
        FOREIGN KEY(task_id, batch_no)
            REFERENCES wiki_task_batches(task_id, batch_no)
    )"""),
    'wiki_task_state_transition': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_state_transition
        BEFORE UPDATE OF state ON wiki_tasks
        WHEN NOT (
            OLD.state = NEW.state
            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))
            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))
            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))
            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))
            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))
            OR (OLD.state = 'failed' AND NEW.state = 'queued'
                AND NEW.recovery_state IN ('not_needed', 'succeeded'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki task transition'); END"""),
    'wiki_batch_state_transition': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_state_transition
        BEFORE UPDATE OF state ON wiki_task_batches
        WHEN NOT (
            OLD.state = NEW.state
            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))
            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))
            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))
            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))
            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))
            OR (OLD.state = 'failed' AND NEW.state = 'queued')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki batch transition'); END"""),
    'wiki_task_recovery_transition': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_recovery_transition
        BEFORE UPDATE OF recovery_state ON wiki_tasks
        WHEN NOT (
            OLD.recovery_state = NEW.recovery_state
            OR (OLD.recovery_state = 'not_needed' AND NEW.recovery_state = 'required')
            OR (OLD.recovery_state = 'required' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'running' AND NEW.recovery_state IN ('succeeded', 'failed'))
            OR (OLD.recovery_state = 'failed' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'succeeded' AND NEW.recovery_state = 'not_needed')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki recovery transition'); END"""),
    'wiki_task_boundary_immutable': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_boundary_immutable
        BEFORE UPDATE OF task_id, vault_path, vault_key, request_kind, trigger_source,
            backend, model, effort, kit_version, kit_manifest_sha256,
            boundary_sha256, raw_count, batch_count ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task boundary is immutable'); END"""),
    'wiki_task_no_delete': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_no_delete
        BEFORE DELETE ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task is durable'); END"""),
    'wiki_task_raw_immutable': ('trigger', 'wiki_task_raw', """CREATE TRIGGER wiki_task_raw_immutable
        BEFORE UPDATE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END"""),
    'wiki_task_raw_no_delete': ('trigger', 'wiki_task_raw', """CREATE TRIGGER wiki_task_raw_no_delete
        BEFORE DELETE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END"""),
    'wiki_batch_identity_immutable': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_identity_immutable
        BEFORE UPDATE OF task_id, batch_no, item_count ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch identity is immutable'); END"""),
    'wiki_batch_no_delete': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_no_delete
        BEFORE DELETE ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch is durable'); END"""),
    'wiki_observations': ('table', 'wiki_observations', """CREATE TABLE wiki_observations (
        vault_key TEXT PRIMARY KEY CHECK (length(vault_key) = 64),
        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),
        task_id TEXT REFERENCES wiki_tasks(task_id),
        pending_count INTEGER CHECK (pending_count IS NULL OR pending_count >= 0),
        candidate_count INTEGER CHECK (candidate_count IS NULL OR candidate_count >= 0),
        observed_at TEXT NOT NULL CHECK (TRIM(observed_at) != ''),
        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),
        CHECK (
            (error_code IS NULL AND pending_count IS NOT NULL AND candidate_count IS NOT NULL)
            OR (error_code IS NOT NULL AND pending_count IS NULL AND candidate_count IS NULL)
        )
    )"""),
    'wiki_task_outcome_binding_immutable': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_outcome_binding_immutable
        BEFORE UPDATE OF outcome_contract,plan_json,plan_sha256 ON wiki_tasks
        WHEN NEW.outcome_contract IS NOT OLD.outcome_contract OR NEW.plan_json IS NOT OLD.plan_json
          OR NEW.plan_sha256 IS NOT OLD.plan_sha256
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding is immutable'); END"""),
    'wiki_task_outcome_binding_required': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_outcome_binding_required
BEFORE INSERT ON wiki_tasks
WHEN NEW.outcome_contract IS NOT 'legacy'
BEGIN
  SELECT CASE WHEN NOT (
    NEW.plan_sha256 IS NOT NULL
    AND NEW.state IS 'queued'
    AND NEW.error_code IS NULL
    AND NEW.completed_batch_count IS 0
    AND NEW.recovery_state IS 'not_needed'
    AND NEW.recovery_phase IS 'none'
  ) THEN RAISE(ABORT,'wiki outcome binding required') END;
END"""),
    'wiki_outcome_receipts': ('table', 'wiki_outcome_receipts', """CREATE TABLE wiki_outcome_receipts (
        receipt_id TEXT NOT NULL CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
        task_id TEXT NOT NULL,
        batch_no INTEGER NOT NULL CHECK(batch_no>0),
        phase TEXT NOT NULL CHECK(phase IN ('validated','accepted')),
        contract TEXT NOT NULL CHECK(contract='r08-wiki-outcomes-v1'),
        boundary_sha256 TEXT NOT NULL CHECK(length(boundary_sha256)=64 AND boundary_sha256 NOT GLOB '*[^0-9a-f]*'),
        plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
        created_at TEXT NOT NULL CHECK(trim(created_at)!=''),
        PRIMARY KEY(receipt_id,phase),
        FOREIGN KEY(task_id,batch_no) REFERENCES wiki_task_batches(task_id,batch_no)
    )"""),
    'wiki_outcome_one_accepted_batch': ('index', 'wiki_outcome_receipts', """CREATE UNIQUE INDEX wiki_outcome_one_accepted_batch
        ON wiki_outcome_receipts(task_id,batch_no) WHERE phase='accepted'"""),
    'wiki_outcome_receipts_no_update': ('trigger', 'wiki_outcome_receipts', """CREATE TRIGGER wiki_outcome_receipts_no_update
            BEFORE UPDATE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END"""),
    'wiki_outcome_receipts_no_delete': ('trigger', 'wiki_outcome_receipts', """CREATE TRIGGER wiki_outcome_receipts_no_delete
            BEFORE DELETE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END"""),
    'wiki_outcome_receipts_publish_unavailable': ('trigger', 'wiki_outcome_receipts', """CREATE TRIGGER wiki_outcome_receipts_publish_unavailable
BEFORE INSERT ON wiki_outcome_receipts
WHEN NEW.phase='accepted'
BEGIN
  SELECT CASE WHEN COALESCE(wiki_outcome_accept(
    NEW.receipt_id, NEW.task_id, NEW.batch_no, NEW.phase, NEW.contract,
    NEW.boundary_sha256, NEW.plan_sha256, NEW.payload_json, NEW.created_at
  ),0) != 1 THEN RAISE(ABORT,'publish proof unavailable') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM wiki_tasks t JOIN wiki_task_batches b ON b.task_id=t.task_id
    WHERE t.task_id=NEW.task_id AND b.batch_no=NEW.batch_no
      AND t.outcome_contract=NEW.contract
      AND t.outcome_contract='r08-wiki-outcomes-v1'
      AND t.boundary_sha256=NEW.boundary_sha256
      AND t.plan_sha256=NEW.plan_sha256
      AND b.state='publishing'
  ) THEN RAISE(ABORT,'wiki outcome binding invalid') END;
END"""),
    'wiki_task_contract_plan_unique': ('index', 'wiki_tasks', """CREATE UNIQUE INDEX wiki_task_contract_plan_unique
ON wiki_tasks(vault_key,boundary_sha256,kit_manifest_sha256,backend,model,effort,
              outcome_contract,COALESCE(plan_sha256,''))"""),
    'wiki_task_legacy_boundary_unique': ('index', 'wiki_tasks', """CREATE UNIQUE INDEX wiki_task_legacy_boundary_unique
ON wiki_tasks(vault_key,boundary_sha256,kit_manifest_sha256,backend,model,effort)
WHERE outcome_contract='legacy'"""),
    'wiki_batch_typed_success_requires_accepted': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_typed_success_requires_accepted
BEFORE UPDATE OF state ON wiki_task_batches
WHEN NEW.state IS 'succeeded' AND OLD.state IS NOT 'succeeded'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM wiki_tasks t
    WHERE t.task_id=NEW.task_id AND (
      t.outcome_contract IS 'legacy'
      OR (
        t.outcome_contract IS 'r08-wiki-outcomes-v1'
        AND typeof(t.plan_sha256)='text'
        AND EXISTS (
          SELECT 1 FROM wiki_outcome_receipts r
          WHERE r.task_id=NEW.task_id AND r.batch_no=NEW.batch_no
            AND r.phase IS 'accepted'
            AND r.contract=t.outcome_contract
            AND r.boundary_sha256=t.boundary_sha256
            AND r.plan_sha256=t.plan_sha256
        )
      )
    )
  ) THEN RAISE(ABORT,'wiki accepted receipt required') END;
END"""),
    'wiki_task_typed_success_requires_all_accepted': ('trigger', 'wiki_tasks', """CREATE TRIGGER wiki_task_typed_success_requires_all_accepted
BEFORE UPDATE OF state ON wiki_tasks
WHEN NEW.state IS 'succeeded' AND OLD.state IS NOT 'succeeded'
  AND NEW.outcome_contract IS NOT 'legacy'
BEGIN
  SELECT CASE WHEN NOT (
    NEW.outcome_contract IS 'r08-wiki-outcomes-v1'
    AND typeof(NEW.plan_sha256)='text'
    AND typeof(NEW.batch_count)='integer'
    AND typeof(NEW.completed_batch_count)='integer'
    AND NEW.batch_count=(
      SELECT COUNT(*) FROM wiki_task_batches b WHERE b.task_id=NEW.task_id
    )
    AND NEW.completed_batch_count=NEW.batch_count
    AND NOT EXISTS (
      SELECT 1 FROM wiki_task_batches b
      WHERE b.task_id=NEW.task_id AND (
        b.state IS NOT 'succeeded'
        OR NOT EXISTS (
          SELECT 1 FROM wiki_outcome_receipts r
          WHERE r.task_id=NEW.task_id AND r.batch_no=b.batch_no
            AND r.phase IS 'accepted'
            AND r.contract=NEW.outcome_contract
            AND r.boundary_sha256=NEW.boundary_sha256
            AND r.plan_sha256=NEW.plan_sha256
        )
      )
    )
    AND NEW.batch_count=(
      SELECT COUNT(*) FROM wiki_outcome_receipts r
      WHERE r.task_id=NEW.task_id AND r.phase IS 'accepted'
    )
  ) THEN RAISE(ABORT,'wiki accepted batches required') END;
END"""),
    'wiki_batch_typed_insert_requires_queued': ('trigger', 'wiki_task_batches', """CREATE TRIGGER wiki_batch_typed_insert_requires_queued
BEFORE INSERT ON wiki_task_batches
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM wiki_tasks t
    WHERE t.task_id=NEW.task_id AND (
      t.outcome_contract IS 'legacy'
      OR (
        t.outcome_contract IS NOT 'legacy'
        AND NEW.state IS 'queued'
        AND NEW.error_code IS NULL
      )
    )
  ) THEN RAISE(ABORT,'wiki typed batch must start queued') END;
END"""),
}
