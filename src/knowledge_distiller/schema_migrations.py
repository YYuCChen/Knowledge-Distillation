from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


CREATE_MATERIALS = """
CREATE TABLE materials (
    material_id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    original_url TEXT NOT NULL,
    canonical_url TEXT,
    current_source_fact_id INTEGER,
    current_knowledge_result_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (platform, platform_item_id),
    FOREIGN KEY (current_source_fact_id) REFERENCES source_facts(source_fact_id),
    FOREIGN KEY (current_knowledge_result_id) REFERENCES knowledge_results(knowledge_result_id)
)
"""

CREATE_SOURCE_FACTS = """
CREATE TABLE source_facts (
    source_fact_id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    content_snapshot TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL,
    replaces_source_fact_id INTEGER,
    change_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (material_id) REFERENCES materials(material_id),
    FOREIGN KEY (replaces_source_fact_id) REFERENCES source_facts(source_fact_id)
)
"""

CREATE_KNOWLEDGE_RESULTS = """
CREATE TABLE knowledge_results (
    knowledge_result_id INTEGER PRIMARY KEY,
    source_fact_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    published_at TEXT,
    published_path TEXT,
    CHECK (
        (published_at IS NULL AND published_path IS NULL)
        OR (published_at IS NOT NULL AND published_path IS NOT NULL)
    ),
    FOREIGN KEY (source_fact_id) REFERENCES source_facts(source_fact_id)
)
"""

CREATE_TASKS = """
CREATE TABLE tasks (
    task_id INTEGER PRIMARY KEY,
    material_id INTEGER,
    submitted_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_failure_boundary TEXT,
    last_failure_reason TEXT,
    waiting_boundary TEXT,
    waiting_reason TEXT,
    CHECK (
        (last_failure_boundary IS NULL AND last_failure_reason IS NULL)
        OR (last_failure_boundary IS NOT NULL AND last_failure_reason IS NOT NULL)
    ),
    CHECK (
        (waiting_boundary IS NULL AND waiting_reason IS NULL)
        OR (waiting_boundary IS NOT NULL AND waiting_reason IS NOT NULL)
    ),
    FOREIGN KEY (material_id) REFERENCES materials(material_id)
)
"""

CREATE_TOPICS = """
CREATE TABLE topics (
    topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name TEXT NOT NULL UNIQUE CHECK (TRIM(normalized_name) != ''),
    name TEXT NOT NULL CHECK (TRIM(name) != ''),
    scope TEXT NOT NULL CHECK (TRIM(scope) != '')
)
"""

CREATE_TOPIC_MEMBERSHIPS = """
CREATE TABLE topic_memberships (
    topic_id INTEGER NOT NULL,
    knowledge_result_id INTEGER NOT NULL,
    point_id TEXT NOT NULL CHECK (TRIM(point_id) != ''),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (topic_id, knowledge_result_id, point_id),
    UNIQUE (topic_id, position),
    FOREIGN KEY (topic_id) REFERENCES topics(topic_id) ON DELETE CASCADE,
    FOREIGN KEY (knowledge_result_id) REFERENCES knowledge_results(knowledge_result_id)
)
"""

CREATE_TOPIC_INDEX_STATE = """
CREATE TABLE topic_index_state (
    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
    source_signature TEXT NOT NULL,
    indexed_at TEXT NOT NULL
)
"""

CREATE_ONE_TASK_PER_IDENTIFIED_MATERIAL = """
CREATE UNIQUE INDEX one_task_per_identified_material
ON tasks(material_id)
WHERE material_id IS NOT NULL
"""

CREATE_CURRENT_SOURCE_FACT_TRIGGER = """
CREATE TRIGGER current_source_fact_must_belong_to_material
BEFORE UPDATE OF current_source_fact_id ON materials
WHEN NEW.current_source_fact_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM source_facts
        WHERE source_fact_id = NEW.current_source_fact_id
          AND material_id = NEW.material_id
    ) THEN RAISE(ABORT, 'current source fact does not belong to material') END;
END
"""

CREATE_CURRENT_KNOWLEDGE_RESULT_TRIGGER = """
CREATE TRIGGER current_knowledge_result_must_match_source_fact
BEFORE UPDATE OF current_knowledge_result_id ON materials
WHEN NEW.current_knowledge_result_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM knowledge_results AS kr
        JOIN source_facts AS sf ON sf.source_fact_id = kr.source_fact_id
        WHERE kr.knowledge_result_id = NEW.current_knowledge_result_id
          AND sf.material_id = NEW.material_id
          AND kr.source_fact_id = NEW.current_source_fact_id
    ) THEN RAISE(ABORT, 'current knowledge result does not match material source fact') END;
END
"""

CREATE_SOURCE_FACT_UPDATE_TRIGGER = """
CREATE TRIGGER source_facts_cannot_be_updated
BEFORE UPDATE ON source_facts
BEGIN
    SELECT RAISE(ABORT, 'source facts are immutable');
END
"""

CREATE_SOURCE_FACT_DELETE_TRIGGER = """
CREATE TRIGGER source_facts_cannot_be_deleted
BEFORE DELETE ON source_facts
BEGIN
    SELECT RAISE(ABORT, 'source facts are immutable');
END
"""

CREATE_KNOWLEDGE_RESULT_UPDATE_TRIGGER = """
CREATE TRIGGER knowledge_result_content_cannot_be_updated
BEFORE UPDATE OF source_fact_id, payload_json, created_at ON knowledge_results
BEGIN
    SELECT RAISE(ABORT, 'knowledge result content is immutable');
END
"""

CREATE_KNOWLEDGE_RESULT_DELETE_TRIGGER = """
CREATE TRIGGER knowledge_results_cannot_be_deleted
BEFORE DELETE ON knowledge_results
BEGIN
    SELECT RAISE(ABORT, 'knowledge results are immutable');
END
"""

CURRENT_BASELINE_CORE_STATEMENTS = (
    CREATE_MATERIALS,
    CREATE_SOURCE_FACTS,
    CREATE_KNOWLEDGE_RESULTS,
    CREATE_TASKS,
    CREATE_ONE_TASK_PER_IDENTIFIED_MATERIAL,
    CREATE_CURRENT_SOURCE_FACT_TRIGGER,
    CREATE_CURRENT_KNOWLEDGE_RESULT_TRIGGER,
    CREATE_SOURCE_FACT_UPDATE_TRIGGER,
    CREATE_SOURCE_FACT_DELETE_TRIGGER,
    CREATE_KNOWLEDGE_RESULT_UPDATE_TRIGGER,
    CREATE_KNOWLEDGE_RESULT_DELETE_TRIGGER,
)

CURRENT_BASELINE_TOPIC_STATEMENTS = (
    CREATE_TOPICS,
    CREATE_TOPIC_MEMBERSHIPS,
    CREATE_TOPIC_INDEX_STATE,
)

CURRENT_BASELINE_STATEMENTS = (
    CREATE_MATERIALS,
    CREATE_SOURCE_FACTS,
    CREATE_KNOWLEDGE_RESULTS,
    CREATE_TASKS,
    CREATE_TOPICS,
    CREATE_TOPIC_MEMBERSHIPS,
    CREATE_TOPIC_INDEX_STATE,
    CREATE_ONE_TASK_PER_IDENTIFIED_MATERIAL,
    CREATE_CURRENT_SOURCE_FACT_TRIGGER,
    CREATE_CURRENT_KNOWLEDGE_RESULT_TRIGGER,
    CREATE_SOURCE_FACT_UPDATE_TRIGGER,
    CREATE_SOURCE_FACT_DELETE_TRIGGER,
    CREATE_KNOWLEDGE_RESULT_UPDATE_TRIGGER,
    CREATE_KNOWLEDGE_RESULT_DELETE_TRIGGER,
)

CREATE_KNOWLEDGE_RESULT_OWNERSHIP_INDEX = """
CREATE UNIQUE INDEX knowledge_result_source_fact_identity
ON knowledge_results(knowledge_result_id, source_fact_id)
"""

CREATE_ORGANIZATION_EVENTS = """
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
)
"""

CREATE_ONE_RUNNING_ORGANIZATION_EVENT = """
CREATE UNIQUE INDEX one_running_organization_event
ON organization_events(memory_scope) WHERE status = 'running'
"""

CREATE_ORGANIZATION_EVENTS_STATUS_ORDER = """
CREATE INDEX organization_events_status_order
ON organization_events(status, event_id DESC)
"""

CREATE_ORGANIZATION_EVENT_SOURCE_BOUNDARY = """
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
)
"""

CREATE_ORGANIZATION_SOURCE_BOUNDARY_ROLE_INDEX = """
CREATE INDEX organization_event_source_boundary_role_knowledge
ON organization_event_source_boundary(boundary_role, knowledge_result_id)
"""

CREATE_ORGANIZATION_EVENT_COVERAGES = """
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
)
"""

CREATE_COVERAGE_MATCH_TRIGGER = """
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
END
"""

CREATE_ORGANIZATION_EVENT_UPDATE_TRIGGER = """
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
END
"""

CREATE_ORGANIZATION_EVENT_DELETE_TRIGGER = """
CREATE TRIGGER organization_events_cannot_be_deleted
BEFORE DELETE ON organization_events
BEGIN
    SELECT RAISE(ABORT, 'organization events are immutable history');
END
"""

CREATE_SOURCE_BOUNDARY_UPDATE_TRIGGER = """
CREATE TRIGGER organization_event_source_boundary_cannot_be_updated
BEFORE UPDATE ON organization_event_source_boundary
BEGIN
    SELECT RAISE(ABORT, 'organization event source boundary is immutable');
END
"""

CREATE_SOURCE_BOUNDARY_DELETE_TRIGGER = """
CREATE TRIGGER organization_event_source_boundary_cannot_be_deleted
BEFORE DELETE ON organization_event_source_boundary
BEGIN
    SELECT RAISE(ABORT, 'organization event source boundary is immutable');
END
"""

CREATE_COVERAGE_UPDATE_TRIGGER = """
CREATE TRIGGER organization_event_coverages_cannot_be_updated
BEFORE UPDATE ON organization_event_coverages
BEGIN
    SELECT RAISE(ABORT, 'organization event coverage is immutable');
END
"""

CREATE_COVERAGE_DELETE_TRIGGER = """
CREATE TRIGGER organization_event_coverages_cannot_be_deleted
BEFORE DELETE ON organization_event_coverages
BEGIN
    SELECT RAISE(ABORT, 'organization event coverage is immutable');
END
"""

ORGANIZATION_EVENT_AND_COVERAGE_STATEMENTS = (
    CREATE_KNOWLEDGE_RESULT_OWNERSHIP_INDEX,
    CREATE_ORGANIZATION_EVENTS,
    CREATE_ONE_RUNNING_ORGANIZATION_EVENT,
    CREATE_ORGANIZATION_EVENTS_STATUS_ORDER,
    CREATE_ORGANIZATION_EVENT_SOURCE_BOUNDARY,
    CREATE_ORGANIZATION_SOURCE_BOUNDARY_ROLE_INDEX,
    CREATE_ORGANIZATION_EVENT_COVERAGES,
    CREATE_COVERAGE_MATCH_TRIGGER,
    CREATE_ORGANIZATION_EVENT_UPDATE_TRIGGER,
    CREATE_ORGANIZATION_EVENT_DELETE_TRIGGER,
    CREATE_SOURCE_BOUNDARY_UPDATE_TRIGGER,
    CREATE_SOURCE_BOUNDARY_DELETE_TRIGGER,
    CREATE_COVERAGE_UPDATE_TRIGGER,
    CREATE_COVERAGE_DELETE_TRIGGER,
)

CREATE_RELATION_IDENTITIES = """
CREATE TABLE relation_identities (
    relation_id INTEGER NOT NULL PRIMARY KEY,
    created_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    FOREIGN KEY (created_event_id) REFERENCES organization_events(event_id)
)
"""

CREATE_RELATION_VERSIONS = """
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
)
"""

CREATE_RELATION_CURRENT = """
CREATE TABLE relation_current (
    relation_id INTEGER NOT NULL PRIMARY KEY,
    relation_version_id INTEGER NOT NULL UNIQUE,
    activated_at TEXT NOT NULL CHECK (TRIM(activated_at) != ''),
    FOREIGN KEY (relation_id, relation_version_id)
      REFERENCES relation_versions(relation_id, relation_version_id)
)
"""

CREATE_INSIGHT_IDENTITIES = """
CREATE TABLE insight_identities (
    insight_id INTEGER NOT NULL PRIMARY KEY,
    created_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),
    FOREIGN KEY (created_event_id) REFERENCES organization_events(event_id)
)
"""

CREATE_INSIGHT_VERSIONS = """
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
)
"""

CREATE_INSIGHT_IDENTITY_REPLACEMENTS = """
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
)
"""

CREATE_INSIGHT_VERSION_DISQUALIFICATIONS = """
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
)
"""

CREATE_USER_INSIGHT_JUDGMENTS = """
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
)
"""

CREATE_ACCEPTED_INSIGHT_VERSIONS = """
CREATE TABLE accepted_insight_versions (
    insight_version_id INTEGER NOT NULL PRIMARY KEY,
    insight_id INTEGER NOT NULL,
    judgment_id INTEGER NOT NULL UNIQUE,
    judgment_decision TEXT NOT NULL CHECK (judgment_decision = 'interesting'),
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
)
"""

CREATE_ORGANIZATION_EVENT_ACCEPTED_BOUNDARY = """
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
)
"""

CREATE_ORGANIZATION_EVENT_RELATION_BOUNDARY = """
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
)
"""

CREATE_RELATION_VERSION_PARTICIPANTS = """
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
)
"""

CREATE_RELATION_VERSION_USED_RELATIONS = """
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
)
"""

CREATE_INSIGHT_VERSION_PARTICIPANTS = """
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
)
"""

CREATE_INSIGHT_VERSION_USED_RELATIONS = """
CREATE TABLE insight_version_used_relations (
    insight_version_id INTEGER NOT NULL,
    relation_version_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    role_text TEXT NOT NULL CHECK (TRIM(role_text) != ''),
    PRIMARY KEY (insight_version_id, relation_version_id),
    UNIQUE (insight_version_id, position),
    FOREIGN KEY (insight_version_id) REFERENCES insight_versions(insight_version_id),
    FOREIGN KEY (relation_version_id) REFERENCES relation_versions(relation_version_id)
)
"""

CREATE_RELATION_FACTS = """
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
)
"""

CREATE_RELATION_PARTICIPANT_SOURCE_INDEX = """
CREATE INDEX relation_version_participants_source
ON relation_version_participants(knowledge_result_id, point_id)
"""

CREATE_RELATION_PARTICIPANT_ACCEPTED_INDEX = """
CREATE INDEX relation_version_participants_accepted
ON relation_version_participants(accepted_insight_version_id)
"""

CREATE_INSIGHT_PARTICIPANT_SOURCE_INDEX = """
CREATE INDEX insight_version_participants_source
ON insight_version_participants(knowledge_result_id, point_id)
"""

CREATE_INSIGHT_PARTICIPANT_ACCEPTED_INDEX = """
CREATE INDEX insight_version_participants_accepted
ON insight_version_participants(accepted_insight_version_id)
"""

CREATE_RELATION_VERSION_SEMANTIC_INDEX = """
CREATE INDEX relation_versions_semantic_signature
ON relation_versions(semantic_signature)
"""

CREATE_INSIGHT_VERSION_SEMANTIC_INDEX = """
CREATE INDEX insight_versions_semantic_signature
ON insight_versions(semantic_signature)
"""

CREATE_JUDGMENT_DECISION_ORDER_INDEX = """
CREATE INDEX user_insight_judgments_decision_order
ON user_insight_judgments(decision, decided_at)
"""

CREATE_DISQUALIFICATION_KIND_INDEX = """
CREATE INDEX insight_version_disqualifications_kind
ON insight_version_disqualifications(insight_version_id, fact_kind)
"""

CREATE_ONE_CURRENT_ACCEPTED_VERSION = """
CREATE UNIQUE INDEX one_current_accepted_version_per_identity
ON accepted_insight_versions(insight_id) WHERE current_role = 'current'
"""

CREATE_ACCEPTED_ROLE_ORDER_INDEX = """
CREATE INDEX accepted_insight_versions_role_order
ON accepted_insight_versions(current_role, accepted_at DESC, insight_version_id DESC)
"""

CREATE_ONE_WRONG_RELATION_FACT = """
CREATE UNIQUE INDEX one_wrong_fact_per_relation
ON relation_facts(relation_id) WHERE fact_kind = 'wrong'
"""

CREATE_ONE_REPLACEMENT_RELATION_FACT = """
CREATE UNIQUE INDEX one_replacement_per_relation
ON relation_facts(relation_id) WHERE fact_kind = 'replaced'
"""

CREATE_ONE_EVOLUTION_SUCCESSOR = """
CREATE UNIQUE INDEX one_evolution_successor_per_version
ON relation_facts(relation_version_id) WHERE fact_kind = 'evolved'
"""

CREATE_ONE_BASIS_INVALID_RELATION_FACT = """
CREATE UNIQUE INDEX one_basis_invalid_per_version
ON relation_facts(relation_version_id) WHERE fact_kind = 'basis_invalid'
"""

CREATE_RELATION_FACT_EVENT_KIND_INDEX = """
CREATE INDEX relation_facts_event_kind
ON relation_facts(event_id, fact_kind)
"""

CREATE_RELATION_LINEAGE_TRIGGER = """
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
END
"""

CREATE_INSIGHT_LINEAGE_TRIGGER = """
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
END
"""

CREATE_ACCEPTED_BOUNDARY_TRIGGER = """
CREATE TRIGGER accepted_boundary_requires_current
BEFORE INSERT ON organization_event_accepted_boundary
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM accepted_insight_versions AS a
        WHERE a.insight_version_id = NEW.insight_version_id
          AND a.current_role = 'current'
    ) THEN RAISE(ABORT, 'accepted boundary requires current exact version') END;
END
"""

CREATE_RELATION_BOUNDARY_TRIGGER = """
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
END
"""

CREATE_RELATION_PARTICIPANT_BOUNDARY_TRIGGER = """
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
END
"""

CREATE_INSIGHT_PARTICIPANT_BOUNDARY_TRIGGER = """
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
END
"""

CREATE_RELATION_USED_BOUNDARY_TRIGGER = """
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
END
"""

CREATE_INSIGHT_USED_RELATION_TRIGGER = """
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
END
"""

CREATE_ACCEPTED_INITIAL_CURRENT_TRIGGER = """
CREATE TRIGGER accepted_initial_current_must_be_inserted_current
BEFORE INSERT ON accepted_insight_versions
WHEN NEW.initial_role = 'current' AND NEW.current_role != 'current'
BEGIN
    SELECT RAISE(ABORT, 'initial current accepted row must be inserted current');
END
"""

CREATE_ACCEPTED_BORN_HISTORICAL_TRIGGER = """
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
          AND j.decision = 'interesting'
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
END
"""

CREATE_ACCEPTED_NEWER_CURRENT_TRIGGER = """
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
          AND j.decision = 'interesting'
          AND cause_v.version_no > target_v.version_no
    ) THEN RAISE(ABORT, 'accepted historical judgment cause must be newer interesting') END;
END
"""

CREATE_ACCEPTED_UPDATE_TRIGGER = """
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
END
"""

CREATE_ACCEPTED_DELETE_TRIGGER = """
CREATE TRIGGER accepted_insight_versions_cannot_be_deleted
BEFORE DELETE ON accepted_insight_versions
BEGIN
    SELECT RAISE(ABORT, 'accepted insight history is immutable');
END
"""


def _immutable_triggers(table: str, message: str) -> tuple[str, str]:
    return (
        f"""
CREATE TRIGGER {table}_cannot_be_updated
BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}');
END
""",
        f"""
CREATE TRIGGER {table}_cannot_be_deleted
BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}');
END
""",
    )


RELATION_INSIGHT_IMMUTABILITY_TRIGGERS = tuple(
    statement
    for table, message in (
        ("relation_identities", "relation identities are immutable"),
        ("relation_versions", "relation versions are immutable"),
        ("insight_identities", "insight identities are immutable"),
        ("insight_versions", "insight versions are immutable"),
        ("insight_identity_replacements", "insight replacements are immutable"),
        ("insight_version_disqualifications", "insight disqualifications are immutable"),
        ("user_insight_judgments", "insight judgments are immutable"),
        ("organization_event_accepted_boundary", "accepted event boundary is immutable"),
        ("organization_event_relation_boundary", "relation event boundary is immutable"),
        ("relation_version_participants", "relation participants are immutable"),
        ("relation_version_used_relations", "relation used edges are immutable"),
        ("insight_version_participants", "insight participants are immutable"),
        ("insight_version_used_relations", "insight used edges are immutable"),
        ("relation_facts", "relation facts are immutable"),
    )
    for statement in _immutable_triggers(table, message)
)

RELATION_INSIGHT_AND_ACCEPTANCE_CORE_STATEMENTS = (
    CREATE_RELATION_IDENTITIES,
    CREATE_RELATION_VERSIONS,
    CREATE_RELATION_CURRENT,
    CREATE_INSIGHT_IDENTITIES,
    CREATE_INSIGHT_VERSIONS,
    CREATE_INSIGHT_IDENTITY_REPLACEMENTS,
    CREATE_INSIGHT_VERSION_DISQUALIFICATIONS,
    CREATE_USER_INSIGHT_JUDGMENTS,
    CREATE_ACCEPTED_INSIGHT_VERSIONS,
    CREATE_ORGANIZATION_EVENT_ACCEPTED_BOUNDARY,
    CREATE_ORGANIZATION_EVENT_RELATION_BOUNDARY,
    CREATE_RELATION_VERSION_PARTICIPANTS,
    CREATE_RELATION_VERSION_USED_RELATIONS,
    CREATE_INSIGHT_VERSION_PARTICIPANTS,
    CREATE_INSIGHT_VERSION_USED_RELATIONS,
    CREATE_RELATION_FACTS,
    CREATE_RELATION_PARTICIPANT_SOURCE_INDEX,
    CREATE_RELATION_PARTICIPANT_ACCEPTED_INDEX,
    CREATE_INSIGHT_PARTICIPANT_SOURCE_INDEX,
    CREATE_INSIGHT_PARTICIPANT_ACCEPTED_INDEX,
    CREATE_RELATION_VERSION_SEMANTIC_INDEX,
    CREATE_INSIGHT_VERSION_SEMANTIC_INDEX,
    CREATE_JUDGMENT_DECISION_ORDER_INDEX,
    CREATE_DISQUALIFICATION_KIND_INDEX,
    CREATE_ONE_CURRENT_ACCEPTED_VERSION,
    CREATE_ACCEPTED_ROLE_ORDER_INDEX,
    CREATE_ONE_WRONG_RELATION_FACT,
    CREATE_ONE_REPLACEMENT_RELATION_FACT,
    CREATE_ONE_EVOLUTION_SUCCESSOR,
    CREATE_ONE_BASIS_INVALID_RELATION_FACT,
    CREATE_RELATION_FACT_EVENT_KIND_INDEX,
    CREATE_RELATION_LINEAGE_TRIGGER,
    CREATE_INSIGHT_LINEAGE_TRIGGER,
    CREATE_ACCEPTED_BOUNDARY_TRIGGER,
    CREATE_RELATION_BOUNDARY_TRIGGER,
    CREATE_RELATION_PARTICIPANT_BOUNDARY_TRIGGER,
    CREATE_INSIGHT_PARTICIPANT_BOUNDARY_TRIGGER,
    CREATE_RELATION_USED_BOUNDARY_TRIGGER,
    CREATE_INSIGHT_USED_RELATION_TRIGGER,
    CREATE_ACCEPTED_INITIAL_CURRENT_TRIGGER,
    CREATE_ACCEPTED_BORN_HISTORICAL_TRIGGER,
    CREATE_ACCEPTED_NEWER_CURRENT_TRIGGER,
    CREATE_ACCEPTED_UPDATE_TRIGGER,
    CREATE_ACCEPTED_DELETE_TRIGGER,
    *RELATION_INSIGHT_IMMUTABILITY_TRIGGERS,
)

CREATE_ACCEPTED_INSIGHT_PUBLICATIONS = """
CREATE TABLE accepted_insight_publications (
    publication_id INTEGER NOT NULL PRIMARY KEY,
    insight_version_id INTEGER NOT NULL UNIQUE,
    judgment_id INTEGER NOT NULL UNIQUE,
    relative_path TEXT NOT NULL UNIQUE CHECK (TRIM(relative_path) != ''),
    machine_identity TEXT NOT NULL UNIQUE CHECK (TRIM(machine_identity) != ''),
    content_sha256 TEXT NOT NULL CHECK (LENGTH(content_sha256) = 64),
    render_context_signature TEXT NOT NULL CHECK (LENGTH(render_context_signature) = 64),
    placement_receipt_json TEXT NOT NULL CHECK (TRIM(placement_receipt_json) != ''),
    placed_at TEXT NOT NULL CHECK (TRIM(placed_at) != ''),
    recorded_at TEXT NOT NULL CHECK (TRIM(recorded_at) != ''),
    FOREIGN KEY (insight_version_id, judgment_id)
      REFERENCES accepted_insight_versions(insight_version_id, judgment_id)
)
"""

ACCEPTED_INSIGHT_PUBLICATION_IMMUTABILITY_TRIGGERS = _immutable_triggers(
    "accepted_insight_publications",
    "accepted insight publications are immutable",
)

ACCEPTED_INSIGHT_PUBLICATION_STATEMENTS = (
    CREATE_ACCEPTED_INSIGHT_PUBLICATIONS,
    *ACCEPTED_INSIGHT_PUBLICATION_IMMUTABILITY_TRIGGERS,
)

CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE schema_migrations (
    migration_id TEXT NOT NULL PRIMARY KEY CHECK (TRIM(migration_id) != ''),
    position INTEGER NOT NULL UNIQUE CHECK (position > 0),
    checksum TEXT NOT NULL CHECK (LENGTH(checksum) = 64),
    applied_at TEXT NOT NULL CHECK (TRIM(applied_at) != '')
)
"""


class SchemaMigrationError(RuntimeError):
    pass


class MigrationManifestError(SchemaMigrationError):
    pass


class UnsupportedLegacySchema(SchemaMigrationError):
    pass


def _checksum(
    migration_id: str,
    position: int,
    statements: tuple[str, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(migration_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(position).encode("ascii"))
    for statement in statements:
        digest.update(b"\0")
        digest.update(statement.strip().encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class Migration:
    migration_id: str
    position: int
    statements: tuple[str, ...]
    checksum: str

    @classmethod
    def create(
        cls,
        migration_id: str,
        position: int,
        statements: tuple[str, ...],
    ) -> Migration:
        return cls(
            migration_id=migration_id,
            position=position,
            statements=statements,
            checksum=_checksum(migration_id, position, statements),
        )


MIGRATIONS = (
    Migration.create(
        "0001_current_baseline",
        1,
        CURRENT_BASELINE_STATEMENTS,
    ),
    Migration.create(
        "0002_organization_event_and_coverage",
        2,
        ORGANIZATION_EVENT_AND_COVERAGE_STATEMENTS,
    ),
    Migration.create(
        "0003_relation_insight_and_acceptance_core",
        3,
        RELATION_INSIGHT_AND_ACCEPTANCE_CORE_STATEMENTS,
    ),
    Migration.create(
        "0004_accepted_insight_publication",
        4,
        ACCEPTED_INSIGHT_PUBLICATION_STATEMENTS,
    ),
)


_CORE_TABLES = ("materials", "source_facts", "knowledge_results", "tasks")
_TOPIC_TABLES = ("topics", "topic_memberships", "topic_index_state")

_BASELINE_TABLE_SQL = {
    "materials": CREATE_MATERIALS,
    "source_facts": CREATE_SOURCE_FACTS,
    "knowledge_results": CREATE_KNOWLEDGE_RESULTS,
    "tasks": CREATE_TASKS,
    "topics": CREATE_TOPICS,
    "topic_memberships": CREATE_TOPIC_MEMBERSHIPS,
    "topic_index_state": CREATE_TOPIC_INDEX_STATE,
}

_BASELINE_COLUMNS = {
    "materials": (
        ("material_id", "INTEGER", 0, None, 1),
        ("platform", "TEXT", 1, None, 0),
        ("platform_item_id", "TEXT", 1, None, 0),
        ("original_url", "TEXT", 1, None, 0),
        ("canonical_url", "TEXT", 0, None, 0),
        ("current_source_fact_id", "INTEGER", 0, None, 0),
        ("current_knowledge_result_id", "INTEGER", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ),
    "source_facts": (
        ("source_fact_id", "INTEGER", 0, None, 1),
        ("material_id", "INTEGER", 1, None, 0),
        ("metadata_json", "TEXT", 1, None, 0),
        ("content_snapshot", "TEXT", 1, None, 0),
        ("uncertainty_json", "TEXT", 1, None, 0),
        ("replaces_source_fact_id", "INTEGER", 0, None, 0),
        ("change_reason", "TEXT", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ),
    "knowledge_results": (
        ("knowledge_result_id", "INTEGER", 0, None, 1),
        ("source_fact_id", "INTEGER", 1, None, 0),
        ("payload_json", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("invalidated_at", "TEXT", 0, None, 0),
        ("invalidation_reason", "TEXT", 0, None, 0),
        ("published_at", "TEXT", 0, None, 0),
        ("published_path", "TEXT", 0, None, 0),
    ),
    "tasks": (
        ("task_id", "INTEGER", 0, None, 1),
        ("material_id", "INTEGER", 0, None, 0),
        ("submitted_url", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("last_failure_boundary", "TEXT", 0, None, 0),
        ("last_failure_reason", "TEXT", 0, None, 0),
        ("waiting_boundary", "TEXT", 0, None, 0),
        ("waiting_reason", "TEXT", 0, None, 0),
    ),
    "topics": (
        ("topic_id", "INTEGER", 0, None, 1),
        ("normalized_name", "TEXT", 1, None, 0),
        ("name", "TEXT", 1, None, 0),
        ("scope", "TEXT", 1, None, 0),
    ),
    "topic_memberships": (
        ("topic_id", "INTEGER", 1, None, 1),
        ("knowledge_result_id", "INTEGER", 1, None, 2),
        ("point_id", "TEXT", 1, None, 3),
        ("position", "INTEGER", 1, None, 0),
    ),
    "topic_index_state": (
        ("state_id", "INTEGER", 0, None, 1),
        ("source_signature", "TEXT", 1, None, 0),
        ("indexed_at", "TEXT", 1, None, 0),
    ),
}

_BASELINE_FOREIGN_KEYS = {
    "materials": {
        (
            "knowledge_results",
            "current_knowledge_result_id",
            "knowledge_result_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
        (
            "source_facts",
            "current_source_fact_id",
            "source_fact_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    },
    "source_facts": {
        (
            "materials",
            "material_id",
            "material_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
        (
            "source_facts",
            "replaces_source_fact_id",
            "source_fact_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    },
    "knowledge_results": {
        (
            "source_facts",
            "source_fact_id",
            "source_fact_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        )
    },
    "tasks": {
        (
            "materials",
            "material_id",
            "material_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        )
    },
    "topics": set(),
    "topic_memberships": {
        (
            "topics",
            "topic_id",
            "topic_id",
            "NO ACTION",
            "CASCADE",
            "NONE",
        ),
        (
            "knowledge_results",
            "knowledge_result_id",
            "knowledge_result_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    },
    "topic_index_state": set(),
}

_BASELINE_INDEXES = {
    "materials": {(True, "u", False, ("platform", "platform_item_id"))},
    "source_facts": set(),
    "knowledge_results": set(),
    "tasks": {(True, "c", True, ("material_id",))},
    "topics": {(True, "u", False, ("normalized_name",))},
    "topic_memberships": {
        (True, "pk", False, ("topic_id", "knowledge_result_id", "point_id")),
        (True, "u", False, ("topic_id", "position")),
    },
    "topic_index_state": set(),
}

_BASELINE_NAMED_OBJECT_SQL = {
    "one_task_per_identified_material": CREATE_ONE_TASK_PER_IDENTIFIED_MATERIAL,
    "current_source_fact_must_belong_to_material": CREATE_CURRENT_SOURCE_FACT_TRIGGER,
    "current_knowledge_result_must_match_source_fact": CREATE_CURRENT_KNOWLEDGE_RESULT_TRIGGER,
    "source_facts_cannot_be_updated": CREATE_SOURCE_FACT_UPDATE_TRIGGER,
    "source_facts_cannot_be_deleted": CREATE_SOURCE_FACT_DELETE_TRIGGER,
    "knowledge_result_content_cannot_be_updated": CREATE_KNOWLEDGE_RESULT_UPDATE_TRIGGER,
    "knowledge_results_cannot_be_deleted": CREATE_KNOWLEDGE_RESULT_DELETE_TRIGGER,
}

_MANIFEST_COLUMNS = (
    ("migration_id", "TEXT", 1, None, 1),
    ("position", "INTEGER", 1, None, 0),
    ("checksum", "TEXT", 1, None, 0),
    ("applied_at", "TEXT", 1, None, 0),
)

_MANIFEST_INDEXES = {
    (True, "pk", False, ("migration_id",)),
    (True, "u", False, ("position",)),
}


def migrate_database(database_path: Path) -> None:
    _validate_binary_migrations()
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _begin_immediate(connection)
        manifest = _read_and_validate_manifest(connection)
        if manifest is None:
            manifest = _adopt_or_create_baseline(connection)

        for migration in MIGRATIONS[len(manifest) :]:
            _validate_manifest_rows(manifest)
            _execute_migration_statements(connection, migration)
            _require_foreign_keys_valid(connection)
            _record_migration(connection, migration)
            manifest = _read_and_validate_manifest(connection)
            if manifest is None:
                raise MigrationManifestError("migration manifest disappeared")

        if len(manifest) != len(MIGRATIONS):
            raise MigrationManifestError(
                "migration manifest is not the complete binary prefix"
            )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _begin_immediate(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _adopt_or_create_baseline(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    legacy_shape = _detect_legacy_shape(connection)
    if legacy_shape == "unsupported":
        raise UnsupportedLegacySchema("unsupported legacy schema")

    connection.execute(CREATE_SCHEMA_MIGRATIONS)
    manifest = _read_and_validate_manifest(connection)
    if manifest != ():
        raise MigrationManifestError("new migration manifest must be empty")

    baseline = MIGRATIONS[0]
    if legacy_shape == "empty":
        _execute_migration_statements(connection, baseline)
    elif legacy_shape == "four_table":
        _execute_statements(connection, CURRENT_BASELINE_TOPIC_STATEMENTS)
    elif legacy_shape != "seven_table":
        raise UnsupportedLegacySchema("unsupported legacy schema")

    if not _matches_baseline(connection, include_topics=True):
        raise UnsupportedLegacySchema("baseline did not reach the exact current shape")
    _require_foreign_keys_valid(connection)
    _record_migration(connection, baseline)
    manifest = _read_and_validate_manifest(connection)
    if manifest is None:
        raise MigrationManifestError("migration manifest disappeared")
    return manifest


def _detect_legacy_shape(connection: sqlite3.Connection) -> str:
    object_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            """
        ).fetchone()[0]
    )
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if object_count == 0 and user_version == 0:
        return "empty"
    if user_version != 0:
        return "unsupported"
    if _matches_baseline(connection, include_topics=False):
        return "four_table"
    if _matches_baseline(connection, include_topics=True):
        return "seven_table"
    return "unsupported"


def _matches_baseline(
    connection: sqlite3.Connection,
    *,
    include_topics: bool,
) -> bool:
    tables = _CORE_TABLES + (_TOPIC_TABLES if include_topics else ())
    actual_tables = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name != 'schema_migrations'
            """
        )
    }
    if actual_tables != set(tables):
        return False

    for table in tables:
        if _column_signature(connection, table) != _BASELINE_COLUMNS[table]:
            return False
        if _foreign_key_signature(connection, table) != _BASELINE_FOREIGN_KEYS[table]:
            return False
        if _index_signature(connection, table) != _BASELINE_INDEXES[table]:
            return False
        actual_sql = _schema_object_sql(connection, "table", table)
        if _normalize_sql(actual_sql) != _normalize_sql(_BASELINE_TABLE_SQL[table]):
            return False

    expected_named_sql = _BASELINE_NAMED_OBJECT_SQL
    actual_named = {
        str(row[1]): (str(row[0]), str(row[2]), str(row[3]))
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND name != 'schema_migrations'
              AND type IN ('index', 'trigger', 'view')
              AND sql IS NOT NULL
            """
        )
    }
    if set(actual_named) != set(expected_named_sql):
        return False
    for name, expected_sql in expected_named_sql.items():
        if _normalize_sql(actual_named[name][2]) != _normalize_sql(expected_sql):
            return False
    return True


def _read_and_validate_manifest(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...] | None:
    if not _table_exists(connection, "schema_migrations"):
        return None
    try:
        rows = tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT migration_id, position, checksum, applied_at
                FROM schema_migrations
                ORDER BY position, rowid
                """
            )
        )
    except sqlite3.DatabaseError as error:
        raise MigrationManifestError("invalid migration manifest schema") from error
    _validate_manifest_rows(rows)
    if not _matches_manifest_table(connection):
        raise MigrationManifestError("invalid migration manifest schema")
    return rows


def _validate_manifest_rows(rows: tuple[tuple[object, ...], ...]) -> None:
    positions = [row[1] for row in rows]
    if len(positions) != len(set(positions)):
        raise MigrationManifestError("duplicate migration position")
    migration_ids = [row[0] for row in rows]
    if len(migration_ids) != len(set(migration_ids)):
        raise MigrationManifestError("duplicate migration id")
    if len(rows) > len(MIGRATIONS):
        raise MigrationManifestError("manifest contains an unknown migration")
    for index, row in enumerate(rows):
        expected = MIGRATIONS[index]
        migration_id, position, checksum, applied_at = row
        if position != expected.position:
            raise MigrationManifestError(
                "migration manifest has a gap or position mismatch"
            )
        if migration_id != expected.migration_id:
            raise MigrationManifestError(
                "manifest contains an unknown or out-of-order migration"
            )
        if checksum != expected.checksum:
            raise MigrationManifestError("migration checksum mismatch")
        if not isinstance(applied_at, str) or not applied_at.strip():
            raise MigrationManifestError("migration applied_at is invalid")


def _validate_binary_migrations() -> None:
    if not MIGRATIONS:
        raise MigrationManifestError("binary migration list cannot be empty")
    ids: set[str] = set()
    positions: set[int] = set()
    for expected_position, migration in enumerate(MIGRATIONS, start=1):
        if migration.position in positions:
            raise MigrationManifestError("binary has a duplicate migration position")
        if migration.migration_id in ids:
            raise MigrationManifestError("binary has a duplicate migration id")
        if migration.position != expected_position:
            raise MigrationManifestError("binary migration positions must be contiguous")
        if migration.checksum != _checksum(
            migration.migration_id,
            migration.position,
            migration.statements,
        ):
            raise MigrationManifestError("binary migration checksum is invalid")
        positions.add(migration.position)
        ids.add(migration.migration_id)


def _matches_manifest_table(connection: sqlite3.Connection) -> bool:
    if _column_signature(connection, "schema_migrations") != _MANIFEST_COLUMNS:
        return False
    if _foreign_key_signature(connection, "schema_migrations") != set():
        return False
    if _index_signature(connection, "schema_migrations") != _MANIFEST_INDEXES:
        return False
    actual_sql = _schema_object_sql(connection, "table", "schema_migrations")
    return _normalize_sql(actual_sql) == _normalize_sql(CREATE_SCHEMA_MIGRATIONS)


def _execute_migration_statements(
    connection: sqlite3.Connection,
    migration: Migration,
) -> None:
    _execute_statements(connection, migration.statements)


def _execute_statements(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _record_migration(
    connection: sqlite3.Connection,
    migration: Migration,
) -> None:
    connection.execute(
        """
        INSERT INTO schema_migrations (
            migration_id, position, checksum, applied_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            migration.migration_id,
            migration.position,
            migration.checksum,
            datetime.now(UTC).isoformat(),
        ),
    )


def _require_foreign_keys_valid(connection: sqlite3.Connection) -> None:
    violations = tuple(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise SchemaMigrationError(f"foreign_key_check failed: {violations!r}")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table,),
        ).fetchone()
        is not None
    )


def _column_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _foreign_key_signature(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[object, ...]]:
    return {
        (
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
        )
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
    }


def _index_signature(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[object, ...]]:
    indexes: set[tuple[object, ...]] = set()
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        name = str(row[1])
        columns = tuple(
            str(index_row[2])
            for index_row in connection.execute(f'PRAGMA index_xinfo("{name}")')
            if int(index_row[5]) == 1 and int(index_row[1]) >= 0
        )
        indexes.add((bool(row[2]), str(row[3]), bool(row[4]), columns))
    return indexes


def _schema_object_sql(
    connection: sqlite3.Connection,
    object_type: str,
    name: str,
) -> str:
    row = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = ? AND name = ?
        """,
        (object_type, name),
    ).fetchone()
    return "" if row is None or row[0] is None else str(row[0])


def _normalize_sql(sql: str) -> str:
    without_if_not_exists = re.sub(
        r"\bif\s+not\s+exists\b",
        "",
        sql.casefold(),
    )
    return "".join(without_if_not_exists.split()).rstrip(";")
