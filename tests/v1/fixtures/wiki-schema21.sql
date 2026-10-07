-- Synthetic empty schema 21 fixture generated from public commit
-- 85b710cc758fead41846a69f417a764343135009 via v1.database.initialize.
-- Contains schema only; no user content or private paths.
BEGIN IMMEDIATE;
CREATE TABLE accepted_insight_versions (
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
);
CREATE TABLE capture_identity_events (
        event_id INTEGER PRIMARY KEY,
        capture_id INTEGER NOT NULL REFERENCES captures(capture_id),
        result TEXT NOT NULL CHECK (result IN ('my_thought', 'third_party', 'annotation', 'pending')),
        basis TEXT NOT NULL, confidence REAL, target_message_id TEXT,
        created_at TEXT NOT NULL
    );
CREATE TABLE capture_state (
        capture_id INTEGER PRIMARY KEY REFERENCES captures(capture_id),
        item_id INTEGER REFERENCES distill_items(item_id),
        audio_path TEXT, audio_released_at TEXT
    );
CREATE TABLE capture_transcripts (
        capture_id INTEGER PRIMARY KEY REFERENCES captures(capture_id),
        text TEXT NOT NULL, engine TEXT NOT NULL, model TEXT NOT NULL, version TEXT NOT NULL,
        chunks_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
CREATE TABLE captures (
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
    );
CREATE TABLE collection_confirmations (
        token TEXT NOT NULL, ordinal INTEGER NOT NULL, expected_count INTEGER NOT NULL,
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        PRIMARY KEY(token,ordinal)
    );
CREATE TABLE collection_events (
        event_id INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        kind TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
CREATE TABLE collection_members (
        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),
        ordinal INTEGER NOT NULL,
        native_id TEXT NOT NULL, native_version TEXT NOT NULL,
        item_id INTEGER NOT NULL UNIQUE REFERENCES distill_items(item_id),
        known_unsupported INTEGER NOT NULL CHECK(known_unsupported IN (0,1)),
        source_fact_id INTEGER REFERENCES source_facts(source_fact_id),
        knowledge_result_id INTEGER REFERENCES knowledge_results(knowledge_result_id),
        PRIMARY KEY(operation_id,ordinal), UNIQUE(operation_id,native_id)
    );
CREATE TABLE collection_operations (
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
    );
CREATE TABLE collection_previews (
        token TEXT PRIMARY KEY, preview_json TEXT NOT NULL,
        expires_at REAL
    );
CREATE TABLE collection_results (
        result_id INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL UNIQUE REFERENCES collection_operations(operation_id),
        payload_json TEXT NOT NULL, lineage_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
CREATE TABLE confirmation_decisions (
                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
                revision TEXT NOT NULL, action TEXT NOT NULL, value TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(item_id, revision));
CREATE TABLE delivery_adjacency (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, earlier_message_id TEXT NOT NULL,
        gap_seconds INTEGER NOT NULL CHECK (gap_seconds >= 0),
        PRIMARY KEY (app_id, message_id, earlier_message_id)
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
, platform_authority_json TEXT NOT NULL DEFAULT '{}', submitted_title TEXT NOT NULL DEFAULT '', review_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE feishu_action_queue (
                id INTEGER PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
                app_id TEXT NOT NULL, message_id TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT);
CREATE TABLE feishu_binding (
        app_id TEXT PRIMARY KEY, bot_open_id TEXT NOT NULL,
        user_open_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
        history_until_ms INTEGER NOT NULL CHECK(history_until_ms >= start_ms)
    );
CREATE TABLE feishu_parts (
        app_id TEXT NOT NULL, message_id TEXT NOT NULL, position INTEGER NOT NULL,
        item_id INTEGER REFERENCES distill_items(item_id),
        error TEXT, preview_json TEXT,
        PRIMARY KEY(app_id,message_id,position),
        FOREIGN KEY(app_id,message_id) REFERENCES feishu_receipts(app_id,message_id)
    );
CREATE TABLE feishu_receipts (
        app_id TEXT NOT NULL REFERENCES feishu_binding(app_id),
        message_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
        raw_json TEXT NOT NULL, text TEXT NOT NULL,
        same_topic INTEGER NOT NULL CHECK(same_topic IN (0,1)),
        content_kind TEXT CHECK(content_kind IN ('text','links')),
        state TEXT NOT NULL CHECK(state IN ('received','waiting_input','accepted','rejected','needs_desktop')),
        error TEXT, card_id TEXT, card_signature TEXT, card_attempted_ms INTEGER,
        PRIMARY KEY(app_id,message_id)
    );
CREATE TABLE group_decisions (
        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
        request_id TEXT NOT NULL, group_id TEXT NOT NULL,
        submitted_revision TEXT NOT NULL, selection_digest TEXT NOT NULL,
        payload_digest TEXT NOT NULL, result_json TEXT NOT NULL,
        audit_json TEXT NOT NULL, committed_at TEXT NOT NULL,
        PRIMARY KEY(item_id,request_id),
        UNIQUE(item_id,submitted_revision,selection_digest));
CREATE TABLE insight_identities (
    insight_id INTEGER NOT NULL PRIMARY KEY,
    created_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    FOREIGN KEY (created_event_id) REFERENCES organization_events(event_id)
);
CREATE TABLE insight_identity_replacements (
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
);
CREATE TABLE insight_reconsiderations (
    reconsideration_id INTEGER PRIMARY KEY,
    insight_version_id INTEGER NOT NULL UNIQUE REFERENCES insight_versions(insight_version_id),
    judgment_id INTEGER NOT NULL UNIQUE REFERENCES user_insight_judgments(judgment_id),
    operation_id TEXT NOT NULL UNIQUE CHECK(TRIM(operation_id) != ''),
    decision TEXT NOT NULL CHECK(decision='interesting_after_rethink'),
    reconsidered_at TEXT NOT NULL,
    UNIQUE(insight_version_id,judgment_id,reconsideration_id)
);
CREATE TABLE insight_version_disqualifications (
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
);
CREATE TABLE insight_version_participants (
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
);
CREATE TABLE insight_version_used_relations (
    insight_version_id INTEGER NOT NULL,
    relation_version_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    role_text TEXT NOT NULL CHECK (TRIM(role_text) != ''),
    PRIMARY KEY (insight_version_id, relation_version_id),
    UNIQUE (insight_version_id, position),
    FOREIGN KEY (insight_version_id) REFERENCES insight_versions(insight_version_id),
    FOREIGN KEY (relation_version_id) REFERENCES relation_versions(relation_version_id)
);
CREATE TABLE insight_versions (
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
);
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
CREATE TABLE manual_cards (
        enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
        review_round_id TEXT NOT NULL, group_id TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','suspended','resolved','superseded')),
        ordering_basis TEXT NOT NULL CHECK(ordering_basis IN ('observed','migration_inferred')),
        ordering_reason TEXT NOT NULL, entered_at TEXT NOT NULL,
        mapping_json TEXT NOT NULL,
        UNIQUE(item_id,review_round_id,group_id));
CREATE TABLE "materials" (
        material_id INTEGER PRIMARY KEY,
        source_kind TEXT NOT NULL, source_key TEXT NOT NULL,
        submitted_url TEXT NOT NULL, canonical_url TEXT NOT NULL,
        metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
        snapshot_key TEXT NOT NULL DEFAULT 'legacy',
        UNIQUE (source_kind, source_key, snapshot_key)
    );
CREATE TABLE media_lifecycle (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        legacy_material_id INTEGER NOT NULL,
        released_bytes INTEGER NOT NULL DEFAULT 0,
        compacted_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE organization_event_accepted_boundary (
    event_id INTEGER NOT NULL,
    insight_version_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    qualification_signature TEXT NOT NULL CHECK (LENGTH(qualification_signature) = 64),
    PRIMARY KEY (event_id, insight_version_id),
    UNIQUE (event_id, position),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id),
    FOREIGN KEY (insight_version_id)
      REFERENCES accepted_insight_versions(insight_version_id)
);
CREATE TABLE organization_event_coverages (
    event_id INTEGER NOT NULL,
    knowledge_result_id INTEGER NOT NULL,
    source_fact_id INTEGER NOT NULL,
    covered_at TEXT NOT NULL CHECK (TRIM(covered_at) != ''),
    PRIMARY KEY (event_id, knowledge_result_id),
    UNIQUE (knowledge_result_id),
    FOREIGN KEY (event_id) REFERENCES organization_events(event_id),
    FOREIGN KEY (knowledge_result_id, source_fact_id)
      REFERENCES knowledge_results(knowledge_result_id, source_fact_id)
);
CREATE TABLE organization_event_relation_boundary (
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
);
CREATE TABLE organization_event_source_boundary (
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
);
CREATE TABLE organization_events (
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
);
CREATE TABLE personal_cognition_entries (
    entry_id INTEGER PRIMARY KEY,
    insight_version_id INTEGER NOT NULL REFERENCES insight_versions(insight_version_id),
    judgment_id INTEGER NOT NULL REFERENCES user_insight_judgments(judgment_id),
    reconsideration_id INTEGER REFERENCES insight_reconsiderations(reconsideration_id),
    entry_kind TEXT NOT NULL CHECK(entry_kind IN ('new_idea','new_view')),
    text TEXT NOT NULL CHECK(TRIM(text) != ''),
    operation_id TEXT NOT NULL UNIQUE CHECK(TRIM(operation_id) != ''),
    created_at TEXT NOT NULL,
    CHECK(entry_kind != 'new_view' OR reconsideration_id IS NOT NULL)
);
CREATE TABLE raw_counters (
        day TEXT PRIMARY KEY CHECK (length(day) = 8),
        last INTEGER NOT NULL CHECK (last BETWEEN 0 AND 9999)
    );
CREATE TABLE raw_records (
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
    );
CREATE TABLE relation_current (
    relation_id INTEGER NOT NULL PRIMARY KEY,
    relation_version_id INTEGER NOT NULL UNIQUE,
    activated_at TEXT NOT NULL CHECK (TRIM(activated_at) != ''),
    FOREIGN KEY (relation_id, relation_version_id)
      REFERENCES relation_versions(relation_id, relation_version_id)
);
CREATE TABLE relation_facts (
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
);
CREATE TABLE relation_identities (
    relation_id INTEGER NOT NULL PRIMARY KEY,
    created_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    FOREIGN KEY (created_event_id) REFERENCES organization_events(event_id)
);
CREATE TABLE relation_version_participants (
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
);
CREATE TABLE relation_version_used_relations (
    relation_version_id INTEGER NOT NULL,
    used_relation_version_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    role_text TEXT NOT NULL CHECK (TRIM(role_text) != ''),
    PRIMARY KEY (relation_version_id, used_relation_version_id),
    UNIQUE (relation_version_id, position),
    CHECK (relation_version_id != used_relation_version_id),
    FOREIGN KEY (relation_version_id) REFERENCES relation_versions(relation_version_id),
    FOREIGN KEY (used_relation_version_id) REFERENCES relation_versions(relation_version_id)
);
CREATE TABLE relation_versions (
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
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source_connections (
    platform TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('connected', 'relogin_required', 'unconfigured')
    ),
    generation INTEGER NOT NULL CHECK (generation > 0),
    account_label TEXT,
    connected_at TEXT NOT NULL
, browser_context TEXT);
CREATE TABLE source_facts (
    source_fact_id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL UNIQUE REFERENCES materials(material_id),
    snapshot TEXT NOT NULL CHECK (TRIM(snapshot) != ''),
    uncertainties_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE source_media (
                material_id INTEGER NOT NULL REFERENCES materials(material_id),
                member_id TEXT NOT NULL, position INTEGER NOT NULL,
                mime_type TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY(material_id, member_id), UNIQUE(material_id, position)
            );
CREATE TABLE source_review_results (
                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
                revision INTEGER NOT NULL, identity TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('complete','failed')),
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(item_id, revision));
CREATE TABLE submitted_sources (
    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),
    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', 'markdown', 'pdf', 'epub', 'image')),
    input_key TEXT NOT NULL,
    input_label TEXT NOT NULL,
    input_metadata TEXT NOT NULL,
    content BLOB,
    retain_until TEXT,
    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),
    UNIQUE(input_kind, input_key)
);
CREATE TABLE topic_entries (
        topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, scope TEXT NOT NULL, updated_at TEXT NOT NULL
    );
CREATE TABLE topic_members (
        topic_id INTEGER NOT NULL REFERENCES topic_entries(topic_id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        knowledge_result_id INTEGER NOT NULL REFERENCES knowledge_results(knowledge_result_id),
        point_id TEXT NOT NULL,
        PRIMARY KEY(topic_id, position), UNIQUE(topic_id, knowledge_result_id, point_id)
    );
CREATE TABLE topic_snapshot (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        knowledge_count INTEGER NOT NULL CHECK(knowledge_count >= 0),
        input_signature TEXT NOT NULL
    );
CREATE TABLE user_insight_judgments (
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
);
CREATE INDEX accepted_insight_versions_role_order
ON accepted_insight_versions(current_role, accepted_at DESC, insight_version_id DESC)
;
CREATE INDEX insight_version_disqualifications_kind
ON insight_version_disqualifications(insight_version_id, fact_kind)
;
CREATE INDEX insight_version_participants_accepted
ON insight_version_participants(accepted_insight_version_id)
;
CREATE INDEX insight_version_participants_source
ON insight_version_participants(knowledge_result_id, point_id)
;
CREATE INDEX insight_versions_semantic_signature
ON insight_versions(semantic_signature)
;
CREATE UNIQUE INDEX knowledge_result_source_fact_identity
ON knowledge_results(knowledge_result_id, source_fact_id)
;
CREATE UNIQUE INDEX one_basis_invalid_per_version
ON relation_facts(relation_version_id) WHERE fact_kind = 'basis_invalid'
;
CREATE UNIQUE INDEX one_current_accepted_version_per_identity
ON accepted_insight_versions(insight_id) WHERE current_role = 'current'
;
CREATE UNIQUE INDEX one_evolution_successor_per_version
ON relation_facts(relation_version_id) WHERE fact_kind = 'evolved'
;
CREATE UNIQUE INDEX one_replacement_per_relation
ON relation_facts(relation_id) WHERE fact_kind = 'replaced'
;
CREATE UNIQUE INDEX one_running_organization_event
ON organization_events(memory_scope) WHERE status = 'running'
;
CREATE UNIQUE INDEX one_wrong_fact_per_relation
ON relation_facts(relation_id) WHERE fact_kind = 'wrong'
;
CREATE INDEX organization_event_source_boundary_role_knowledge
ON organization_event_source_boundary(boundary_role, knowledge_result_id)
;
CREATE INDEX organization_events_status_order
ON organization_events(status, event_id DESC)
;
CREATE INDEX raw_records_subject ON raw_records(subject_kind, subject_id);
CREATE INDEX relation_facts_event_kind
ON relation_facts(event_id, fact_kind)
;
CREATE INDEX relation_version_participants_accepted
ON relation_version_participants(accepted_insight_version_id)
;
CREATE INDEX relation_version_participants_source
ON relation_version_participants(knowledge_result_id, point_id)
;
CREATE INDEX relation_versions_semantic_signature
ON relation_versions(semantic_signature)
;
CREATE INDEX user_insight_judgments_decision_order
ON user_insight_judgments(decision, decided_at)
;
CREATE TRIGGER accepted_born_historical_cause_on_insert
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
END;
CREATE TRIGGER accepted_boundary_requires_current
BEFORE INSERT ON organization_event_accepted_boundary
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM accepted_insight_versions AS a
        WHERE a.insight_version_id = NEW.insight_version_id
          AND a.current_role = 'current'
    ) THEN RAISE(ABORT, 'accepted boundary requires current exact version') END;
END;
CREATE TRIGGER accepted_grant_no_change BEFORE UPDATE OF reconsideration_id ON accepted_insight_versions
        BEGIN SELECT RAISE(ABORT,'Accepted grant is immutable'); END;
CREATE TRIGGER accepted_initial_current_must_be_inserted_current
BEFORE INSERT ON accepted_insight_versions
WHEN NEW.initial_role = 'current' AND NEW.current_role != 'current'
BEGIN
    SELECT RAISE(ABORT, 'initial current accepted row must be inserted current');
END;
CREATE TRIGGER accepted_insight_versions_cannot_be_deleted
BEFORE DELETE ON accepted_insight_versions
BEGIN
    SELECT RAISE(ABORT, 'accepted insight history is immutable');
END;
CREATE TRIGGER accepted_insight_versions_current_to_historical_only
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
END;
CREATE TRIGGER accepted_newer_current_cause_on_update
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
END;
CREATE TRIGGER capture_identity_events_no_delete BEFORE DELETE ON capture_identity_events BEGIN
                SELECT RAISE(ABORT, 'capture_identity_events is immutable'); END;
CREATE TRIGGER capture_identity_events_no_update BEFORE UPDATE ON capture_identity_events BEGIN
                SELECT RAISE(ABORT, 'capture_identity_events is immutable'); END;
CREATE TRIGGER capture_transcripts_no_delete BEFORE DELETE ON capture_transcripts BEGIN
                SELECT RAISE(ABORT, 'capture_transcripts is immutable'); END;
CREATE TRIGGER capture_transcripts_no_update BEFORE UPDATE ON capture_transcripts BEGIN
                SELECT RAISE(ABORT, 'capture_transcripts is immutable'); END;
CREATE TRIGGER captures_no_delete BEFORE DELETE ON captures BEGIN
                SELECT RAISE(ABORT, 'captures is immutable'); END;
CREATE TRIGGER captures_no_update BEFORE UPDATE ON captures BEGIN
                SELECT RAISE(ABORT, 'captures is immutable'); END;
CREATE TRIGGER collection_confirmations_no_delete BEFORE DELETE ON collection_confirmations
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER collection_confirmations_no_update BEFORE UPDATE ON collection_confirmations
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER collection_events_no_delete BEFORE DELETE ON collection_events
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER collection_events_no_update BEFORE UPDATE ON collection_events
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER collection_manifest_no_update
        BEFORE UPDATE OF kind,source_key,manifest_json,signature,content_signature,authority_json,confirmation_token ON collection_operations
        BEGIN SELECT RAISE(ABORT,'Collection manifest is immutable'); END;
CREATE TRIGGER collection_member_binding_no_replace
        BEFORE UPDATE OF source_fact_id,knowledge_result_id ON collection_members
        WHEN (OLD.source_fact_id IS NOT NULL AND NEW.source_fact_id IS NOT OLD.source_fact_id)
          OR (OLD.knowledge_result_id IS NOT NULL AND NEW.knowledge_result_id IS NOT OLD.knowledge_result_id)
        BEGIN SELECT RAISE(ABORT,'Collection result binding is immutable'); END;
CREATE TRIGGER collection_members_no_delete BEFORE DELETE ON collection_members
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER collection_membership_no_update
        BEFORE UPDATE OF operation_id,ordinal,native_id,native_version,item_id,known_unsupported ON collection_members
        BEGIN SELECT RAISE(ABORT,'Collection membership is immutable'); END;
CREATE TRIGGER collection_operations_no_delete BEFORE DELETE ON collection_operations
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER collection_results_no_delete BEFORE DELETE ON collection_results
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER collection_results_no_update BEFORE UPDATE ON collection_results
        BEGIN SELECT RAISE(ABORT,'Collection evidence is immutable'); END;
CREATE TRIGGER delivery_adjacency_no_delete BEFORE DELETE ON delivery_adjacency BEGIN
                SELECT RAISE(ABORT, 'delivery_adjacency is immutable'); END;
CREATE TRIGGER delivery_adjacency_no_update BEFORE UPDATE ON delivery_adjacency BEGIN
                SELECT RAISE(ABORT, 'delivery_adjacency is immutable'); END;
CREATE TRIGGER distill_review_revision AFTER UPDATE ON distill_items
                WHEN NEW.review_revision = OLD.review_revision AND (
                    NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase
                    OR NEW.material_id IS NOT OLD.material_id
                    OR NEW.submitted_url IS NOT OLD.submitted_url
                    OR NEW.confirmation_json IS NOT OLD.confirmation_json
                    OR NEW.platform_authority_json IS NOT OLD.platform_authority_json
                ) BEGIN
                UPDATE distill_items SET review_revision = OLD.review_revision + 1
                WHERE item_id = NEW.item_id;
            END;
CREATE TRIGGER insight_identities_cannot_be_deleted
BEFORE DELETE ON insight_identities
BEGIN
    SELECT RAISE(ABORT, 'insight identities are immutable');
END;
CREATE TRIGGER insight_identities_cannot_be_updated
BEFORE UPDATE ON insight_identities
BEGIN
    SELECT RAISE(ABORT, 'insight identities are immutable');
END;
CREATE TRIGGER insight_identity_replacements_cannot_be_deleted
BEFORE DELETE ON insight_identity_replacements
BEGIN
    SELECT RAISE(ABORT, 'insight replacements are immutable');
END;
CREATE TRIGGER insight_identity_replacements_cannot_be_updated
BEFORE UPDATE ON insight_identity_replacements
BEGIN
    SELECT RAISE(ABORT, 'insight replacements are immutable');
END;
CREATE TRIGGER insight_participant_within_event_boundary
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
END;
CREATE TRIGGER insight_reconsiderations_no_delete BEFORE DELETE ON insight_reconsiderations BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END;
CREATE TRIGGER insight_reconsiderations_no_update BEFORE UPDATE ON insight_reconsiderations BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END;
CREATE TRIGGER insight_used_relation_is_boundary_or_same_event
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
END;
CREATE TRIGGER insight_version_disqualifications_cannot_be_deleted
BEFORE DELETE ON insight_version_disqualifications
BEGIN
    SELECT RAISE(ABORT, 'insight disqualifications are immutable');
END;
CREATE TRIGGER insight_version_disqualifications_cannot_be_updated
BEFORE UPDATE ON insight_version_disqualifications
BEGIN
    SELECT RAISE(ABORT, 'insight disqualifications are immutable');
END;
CREATE TRIGGER insight_version_lineage_must_be_consecutive
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
END;
CREATE TRIGGER insight_version_participants_cannot_be_deleted
BEFORE DELETE ON insight_version_participants
BEGIN
    SELECT RAISE(ABORT, 'insight participants are immutable');
END;
CREATE TRIGGER insight_version_participants_cannot_be_updated
BEFORE UPDATE ON insight_version_participants
BEGIN
    SELECT RAISE(ABORT, 'insight participants are immutable');
END;
CREATE TRIGGER insight_version_used_relations_cannot_be_deleted
BEFORE DELETE ON insight_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'insight used edges are immutable');
END;
CREATE TRIGGER insight_version_used_relations_cannot_be_updated
BEFORE UPDATE ON insight_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'insight used edges are immutable');
END;
CREATE TRIGGER insight_versions_cannot_be_deleted
BEFORE DELETE ON insight_versions
BEGIN
    SELECT RAISE(ABORT, 'insight versions are immutable');
END;
CREATE TRIGGER insight_versions_cannot_be_updated
BEFORE UPDATE ON insight_versions
BEGIN
    SELECT RAISE(ABORT, 'insight versions are immutable');
END;
CREATE TRIGGER knowledge_results_content_no_update
BEFORE UPDATE OF source_fact_id, payload_json, created_at ON knowledge_results BEGIN
    SELECT RAISE(ABORT, 'KnowledgeResult content is immutable');
END;
CREATE TRIGGER knowledge_results_no_delete
BEFORE DELETE ON knowledge_results BEGIN
    SELECT RAISE(ABORT, 'KnowledgeResult is immutable');
END;
CREATE TRIGGER material_snapshot_identity_no_update
        BEFORE UPDATE OF source_kind, source_key, snapshot_key ON materials BEGIN
        SELECT RAISE(ABORT, 'Material snapshot identity is immutable'); END;
CREATE TRIGGER material_snapshot_metadata_no_update
        BEFORE UPDATE OF metadata_json, submitted_url, canonical_url ON materials
        WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = OLD.material_id) BEGIN
        SELECT RAISE(ABORT, 'SourceFact metadata is immutable'); END;
CREATE TRIGGER organization_coverage_matches_frozen_source
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
END;
CREATE TRIGGER organization_event_accepted_boundary_cannot_be_deleted
BEFORE DELETE ON organization_event_accepted_boundary
BEGIN
    SELECT RAISE(ABORT, 'accepted event boundary is immutable');
END;
CREATE TRIGGER organization_event_accepted_boundary_cannot_be_updated
BEFORE UPDATE ON organization_event_accepted_boundary
BEGIN
    SELECT RAISE(ABORT, 'accepted event boundary is immutable');
END;
CREATE TRIGGER organization_event_coverages_cannot_be_deleted
BEFORE DELETE ON organization_event_coverages
BEGIN
    SELECT RAISE(ABORT, 'organization event coverage is immutable');
END;
CREATE TRIGGER organization_event_coverages_cannot_be_updated
BEFORE UPDATE ON organization_event_coverages
BEGIN
    SELECT RAISE(ABORT, 'organization event coverage is immutable');
END;
CREATE TRIGGER organization_event_relation_boundary_cannot_be_deleted
BEFORE DELETE ON organization_event_relation_boundary
BEGIN
    SELECT RAISE(ABORT, 'relation event boundary is immutable');
END;
CREATE TRIGGER organization_event_relation_boundary_cannot_be_updated
BEFORE UPDATE ON organization_event_relation_boundary
BEGIN
    SELECT RAISE(ABORT, 'relation event boundary is immutable');
END;
CREATE TRIGGER organization_event_source_boundary_cannot_be_deleted
BEFORE DELETE ON organization_event_source_boundary
BEGIN
    SELECT RAISE(ABORT, 'organization event source boundary is immutable');
END;
CREATE TRIGGER organization_event_source_boundary_cannot_be_updated
BEFORE UPDATE ON organization_event_source_boundary
BEGIN
    SELECT RAISE(ABORT, 'organization event source boundary is immutable');
END;
CREATE TRIGGER organization_events_cannot_be_deleted
BEFORE DELETE ON organization_events
BEGIN
    SELECT RAISE(ABORT, 'organization events are immutable history');
END;
CREATE TRIGGER organization_events_terminal_transition_only
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
END;
CREATE TRIGGER personal_cognition_entries_no_delete BEFORE DELETE ON personal_cognition_entries BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END;
CREATE TRIGGER personal_cognition_entries_no_update BEFORE UPDATE ON personal_cognition_entries BEGIN SELECT RAISE(ABORT,'personal cognition is append-only'); END;
CREATE TRIGGER raw_records_content_no_update
    BEFORE UPDATE OF raw_id, subject_kind, subject_id, identity, relative_path, content, content_sha256,
        attachments_json, supersedes, origin, created_at ON raw_records BEGIN
        SELECT RAISE(ABORT, 'raw record is immutable');
    END;
CREATE TRIGGER raw_records_no_delete
    BEFORE DELETE ON raw_records BEGIN
        SELECT RAISE(ABORT, 'raw record is immutable');
    END;
CREATE TRIGGER raw_records_written_once
    BEFORE UPDATE OF written_at, written_vault ON raw_records WHEN OLD.written_at IS NOT NULL BEGIN
        SELECT RAISE(ABORT, 'raw record was already written');
    END;
CREATE TRIGGER relation_boundary_matches_role
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
END;
CREATE TRIGGER relation_facts_cannot_be_deleted
BEFORE DELETE ON relation_facts
BEGIN
    SELECT RAISE(ABORT, 'relation facts are immutable');
END;
CREATE TRIGGER relation_facts_cannot_be_updated
BEFORE UPDATE ON relation_facts
BEGIN
    SELECT RAISE(ABORT, 'relation facts are immutable');
END;
CREATE TRIGGER relation_identities_cannot_be_deleted
BEFORE DELETE ON relation_identities
BEGIN
    SELECT RAISE(ABORT, 'relation identities are immutable');
END;
CREATE TRIGGER relation_identities_cannot_be_updated
BEFORE UPDATE ON relation_identities
BEGIN
    SELECT RAISE(ABORT, 'relation identities are immutable');
END;
CREATE TRIGGER relation_participant_within_event_boundary
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
END;
CREATE TRIGGER relation_used_relation_within_event_boundary
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
END;
CREATE TRIGGER relation_version_lineage_must_be_consecutive
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
END;
CREATE TRIGGER relation_version_participants_cannot_be_deleted
BEFORE DELETE ON relation_version_participants
BEGIN
    SELECT RAISE(ABORT, 'relation participants are immutable');
END;
CREATE TRIGGER relation_version_participants_cannot_be_updated
BEFORE UPDATE ON relation_version_participants
BEGIN
    SELECT RAISE(ABORT, 'relation participants are immutable');
END;
CREATE TRIGGER relation_version_used_relations_cannot_be_deleted
BEFORE DELETE ON relation_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'relation used edges are immutable');
END;
CREATE TRIGGER relation_version_used_relations_cannot_be_updated
BEFORE UPDATE ON relation_version_used_relations
BEGIN
    SELECT RAISE(ABORT, 'relation used edges are immutable');
END;
CREATE TRIGGER relation_versions_cannot_be_deleted
BEFORE DELETE ON relation_versions
BEGIN
    SELECT RAISE(ABORT, 'relation versions are immutable');
END;
CREATE TRIGGER relation_versions_cannot_be_updated
BEFORE UPDATE ON relation_versions
BEGIN
    SELECT RAISE(ABORT, 'relation versions are immutable');
END;
CREATE TRIGGER source_facts_no_delete
BEFORE DELETE ON source_facts BEGIN
    SELECT RAISE(ABORT, 'SourceFact is immutable');
END;
CREATE TRIGGER source_facts_no_update
BEFORE UPDATE ON source_facts BEGIN
    SELECT RAISE(ABORT, 'SourceFact is immutable');
END;
CREATE TRIGGER source_media_no_delete
                    BEFORE DELETE ON source_media
                    WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = OLD.material_id) BEGIN
                    SELECT RAISE(ABORT, 'SourceFact media is immutable'); END;
CREATE TRIGGER source_media_no_insert
                BEFORE INSERT ON source_media
                WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = NEW.material_id) BEGIN
                SELECT RAISE(ABORT, 'SourceFact media is immutable'); END;
CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media
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
        BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END;
CREATE TRIGGER user_insight_judgments_cannot_be_deleted
BEFORE DELETE ON user_insight_judgments
BEGIN
    SELECT RAISE(ABORT, 'insight judgments are immutable');
END;
CREATE TRIGGER user_insight_judgments_cannot_be_updated
BEFORE UPDATE ON user_insight_judgments
BEGIN
    SELECT RAISE(ABORT, 'insight judgments are immutable');
END;
PRAGMA user_version=21;
COMMIT;
