BEGIN IMMEDIATE;
CREATE TABLE wiki_tasks (
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
        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        CHECK ((recovery_state = 'not_needed') = (recovery_phase = 'none')),
        UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256)
    );
CREATE UNIQUE INDEX wiki_one_unresolved_task_per_vault
        ON wiki_tasks(vault_key)
        WHERE state IN ('queued', 'preparing', 'running', 'validating', 'publishing', 'failed');
CREATE TABLE wiki_task_batches (
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
    );
CREATE TABLE wiki_task_raw (
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
    );
CREATE TRIGGER wiki_task_state_transition
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
        BEGIN SELECT RAISE(ABORT, 'invalid wiki task transition'); END;
CREATE TRIGGER wiki_batch_state_transition
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
        BEGIN SELECT RAISE(ABORT, 'invalid wiki batch transition'); END;
CREATE TRIGGER wiki_task_recovery_transition
        BEFORE UPDATE OF recovery_state ON wiki_tasks
        WHEN NOT (
            OLD.recovery_state = NEW.recovery_state
            OR (OLD.recovery_state = 'not_needed' AND NEW.recovery_state = 'required')
            OR (OLD.recovery_state = 'required' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'running' AND NEW.recovery_state IN ('succeeded', 'failed'))
            OR (OLD.recovery_state = 'failed' AND NEW.recovery_state = 'running')
            OR (OLD.recovery_state = 'succeeded' AND NEW.recovery_state = 'not_needed')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid wiki recovery transition'); END;
CREATE TRIGGER wiki_task_boundary_immutable
        BEFORE UPDATE OF task_id, vault_path, vault_key, request_kind, trigger_source,
            backend, model, effort, kit_version, kit_manifest_sha256,
            boundary_sha256, raw_count, batch_count ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task boundary is immutable'); END;
CREATE TRIGGER wiki_task_no_delete
        BEFORE DELETE ON wiki_tasks
        BEGIN SELECT RAISE(ABORT, 'wiki task is durable'); END;
CREATE TRIGGER wiki_task_raw_immutable
        BEFORE UPDATE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END;
CREATE TRIGGER wiki_task_raw_no_delete
        BEFORE DELETE ON wiki_task_raw
        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END;
CREATE TRIGGER wiki_batch_identity_immutable
        BEFORE UPDATE OF task_id, batch_no, item_count ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch identity is immutable'); END;
CREATE TRIGGER wiki_batch_no_delete
        BEFORE DELETE ON wiki_task_batches
        BEGIN SELECT RAISE(ABORT, 'wiki task batch is durable'); END;
PRAGMA user_version=22;
COMMIT;
