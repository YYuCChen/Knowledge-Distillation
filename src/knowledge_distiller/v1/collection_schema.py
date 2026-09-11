"""Only the durable state required for one confirmed collection operation."""
STATEMENTS = (
    '''CREATE TABLE collection_operations (
        operation_id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL, source_key TEXT NOT NULL, title TEXT NOT NULL,
        manifest_json TEXT NOT NULL, signature TEXT NOT NULL, content_signature TEXT NOT NULL,
        authority_json TEXT NOT NULL, confirmation_token TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('queued','working','waiting_user','partial','failed','succeeded','cancelled')),
        consequence TEXT CHECK(consequence IN ('complete','partial','failed')),
        error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1,
        queued_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(kind,source_key,signature,content_signature)
    )''',
    '''CREATE TABLE collection_confirmations (
        token TEXT NOT NULL, ordinal INTEGER NOT NULL, expected_count INTEGER NOT NULL,
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        PRIMARY KEY(token,ordinal)
    )''',
    '''CREATE TABLE collection_members (
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        ordinal INTEGER NOT NULL,
        native_id TEXT NOT NULL, native_version TEXT NOT NULL,
        item_id INTEGER NOT NULL UNIQUE REFERENCES distill_items(item_id),
        known_unsupported INTEGER NOT NULL CHECK(known_unsupported IN (0,1)),
        source_fact_id INTEGER REFERENCES source_facts(source_fact_id),
        knowledge_result_id INTEGER REFERENCES knowledge_results(knowledge_result_id),
        PRIMARY KEY(operation_id,ordinal), UNIQUE(operation_id,native_id)
    )''',
    '''CREATE TABLE collection_results (
        result_id INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL UNIQUE REFERENCES collection_operations(operation_id),
        payload_json TEXT NOT NULL, lineage_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE collection_events (
        event_id INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        kind TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
    )''',
    '''CREATE TRIGGER collection_manifest_no_update
        BEFORE UPDATE OF kind,source_key,manifest_json,signature,content_signature,authority_json,confirmation_token ON collection_operations
        BEGIN SELECT RAISE(ABORT,'Collection manifest is immutable'); END''',
    '''CREATE TRIGGER collection_membership_no_update
        BEFORE UPDATE OF operation_id,ordinal,native_id,native_version,item_id,known_unsupported ON collection_members
        BEGIN SELECT RAISE(ABORT,'Collection membership is immutable'); END''',
    '''CREATE TRIGGER collection_member_binding_no_replace
        BEFORE UPDATE OF source_fact_id,knowledge_result_id ON collection_members
        WHEN (OLD.source_fact_id IS NOT NULL AND NEW.source_fact_id IS NOT OLD.source_fact_id)
          OR (OLD.knowledge_result_id IS NOT NULL AND NEW.knowledge_result_id IS NOT OLD.knowledge_result_id)
        BEGIN SELECT RAISE(ABORT,'Collection result binding is immutable'); END''',
    *tuple(f'''CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END'''
        for table in ('collection_members','collection_operations','collection_results','collection_events','collection_confirmations')
        for action in (('DELETE',) if table in ('collection_members','collection_operations') else ('UPDATE','DELETE'))),
)
