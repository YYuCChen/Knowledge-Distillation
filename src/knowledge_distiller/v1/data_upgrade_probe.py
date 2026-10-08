"""Offline schema-upgrade proof for an explicitly marked disposable data copy."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import uuid
from contextlib import closing
from pathlib import Path

from .database import SCHEMA_VERSION, initialize


MARKER_NAME = ".kd-offline-upgrade.json"
MARKER_PURPOSE = "knowledge-distiller-v3-offline-upgrade"
DATABASE_NAME = "knowledge.sqlite3"
VAULT_RELPATH = "synthetic-vault"
_SHA256_LENGTH = 64

# Frozen historical storage plus the finite approved26 transform; no target DB oracle.
IDENTITY_CONTRACT = 'all-prior-schema-and-columns-v2'
_PRIOR_SCHEMAS = (21, 22, 23, 25)
_APPROVED_TARGET_SCHEMA = 26
_PARENT_STATE_PATTERN = r"state IN \(\s*'queued',\s*'working',\s*'waiting_user',\s*'succeeded',\s*'failed'\s*\)"
_PARENT_STATE_V25 = "state IN ('queued','working','waiting_user','succeeded','failed','raw_saved')"
_PARENT_CHECK_V25 = ", CHECK(state!='raw_saved' OR (phase='done' AND ingestion_contract='raw-verified-v1')))"
_PARENT_FINAL_NAME = 'CREATE TABLE "distill_items"'
_APPROVED_DDL = {
    "wiki_tasks": ("table", "wiki_tasks", "CREATE TABLE wiki_tasks (\n        task_id TEXT PRIMARY KEY CHECK (\n            length(task_id) = 32\n            AND task_id NOT GLOB '*[^0-9a-f]*'\n        ),\n        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),\n        vault_key TEXT NOT NULL CHECK (length(vault_key) = 64),\n        request_kind TEXT NOT NULL CHECK (request_kind IN ('one_batch', 'all')),\n        trigger_source TEXT NOT NULL CHECK (\n            trigger_source IN ('local_web', 'claudian', 'cli')\n        ),\n        backend TEXT NOT NULL CHECK (backend IN ('codex_cli')),\n        model TEXT NOT NULL CHECK (\n            length(model) BETWEEN 1 AND 80\n            AND model NOT GLOB '*[^A-Za-z0-9._-]*'\n        ),\n        effort TEXT NOT NULL CHECK (\n            effort IN ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')\n        ),\n        kit_version TEXT NOT NULL CHECK (\n            length(kit_version) BETWEEN 1 AND 40\n            AND kit_version NOT GLOB '*[^A-Za-z0-9._-]*'\n        ),\n        kit_manifest_sha256 TEXT NOT NULL CHECK (length(kit_manifest_sha256) = 64),\n        boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),\n        state TEXT NOT NULL CHECK (\n            state IN (\n                'queued', 'preparing', 'running', 'validating', 'publishing',\n                'succeeded', 'failed'\n            )\n        ),\n        raw_count INTEGER NOT NULL CHECK (raw_count >= 0),\n        batch_count INTEGER NOT NULL CHECK (batch_count >= 0),\n        completed_batch_count INTEGER NOT NULL DEFAULT 0 CHECK (\n            completed_batch_count >= 0 AND completed_batch_count <= batch_count\n        ),\n        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),\n        recovery_state TEXT NOT NULL DEFAULT 'not_needed' CHECK (\n            recovery_state IN ('not_needed', 'required', 'running', 'succeeded', 'failed')\n        ),\n        recovery_phase TEXT NOT NULL DEFAULT 'none' CHECK (\n            recovery_phase IN ('none', 'staging', 'publishing', 'readback')\n        ),\n        created_at TEXT NOT NULL CHECK (TRIM(created_at) != ''),\n        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''),\n        CHECK ((state = 'failed') = (error_code IS NOT NULL)),\n        CHECK ((recovery_state = 'not_needed') = (recovery_phase = 'none')),\n        UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256, backend, model, effort)\n    )"),
    "wiki_one_unresolved_task_per_vault": ("index", "wiki_tasks", "CREATE UNIQUE INDEX wiki_one_unresolved_task_per_vault\n        ON wiki_tasks(vault_key)\n        WHERE state IN ('queued', 'preparing', 'running', 'validating', 'publishing')"),
    "wiki_task_batches": ("table", "wiki_task_batches", "CREATE TABLE wiki_task_batches (\n        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),\n        batch_no INTEGER NOT NULL CHECK (batch_no > 0),\n        state TEXT NOT NULL CHECK (\n            state IN (\n                'queued', 'preparing', 'running', 'validating', 'publishing',\n                'succeeded', 'failed'\n            )\n        ),\n        item_count INTEGER NOT NULL CHECK (item_count > 0),\n        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),\n        CHECK ((state = 'failed') = (error_code IS NOT NULL)),\n        PRIMARY KEY(task_id, batch_no)\n    )"),
    "wiki_task_raw": ("table", "wiki_task_raw", "CREATE TABLE wiki_task_raw (\n        task_id TEXT NOT NULL REFERENCES wiki_tasks(task_id),\n        ordinal INTEGER NOT NULL CHECK (ordinal > 0),\n        batch_no INTEGER NOT NULL CHECK (batch_no > 0),\n        raw_id TEXT NOT NULL CHECK (\n            raw_id GLOB 'R-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]'\n        ),\n        identity TEXT NOT NULL CHECK (identity IN ('第三方', '本人', '本人附言')),\n        relative_path TEXT NOT NULL CHECK (\n            TRIM(relative_path) != '' AND relative_path LIKE 'raw/%'\n        ),\n        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),\n        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),\n        PRIMARY KEY(task_id, ordinal),\n        UNIQUE(task_id, relative_path),\n        FOREIGN KEY(task_id, batch_no)\n            REFERENCES wiki_task_batches(task_id, batch_no)\n    )"),
    "wiki_task_state_transition": ("trigger", "wiki_tasks", "CREATE TRIGGER wiki_task_state_transition\n        BEFORE UPDATE OF state ON wiki_tasks\n        WHEN NOT (\n            OLD.state = NEW.state\n            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))\n            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))\n            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))\n            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))\n            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))\n            OR (OLD.state = 'failed' AND NEW.state = 'queued'\n                AND NEW.recovery_state IN ('not_needed', 'succeeded'))\n        )\n        BEGIN SELECT RAISE(ABORT, 'invalid wiki task transition'); END"),
    "wiki_batch_state_transition": ("trigger", "wiki_task_batches", "CREATE TRIGGER wiki_batch_state_transition\n        BEFORE UPDATE OF state ON wiki_task_batches\n        WHEN NOT (\n            OLD.state = NEW.state\n            OR (OLD.state = 'queued' AND NEW.state IN ('preparing', 'failed'))\n            OR (OLD.state = 'preparing' AND NEW.state IN ('running', 'failed'))\n            OR (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))\n            OR (OLD.state = 'validating' AND NEW.state IN ('publishing', 'failed'))\n            OR (OLD.state = 'publishing' AND NEW.state IN ('succeeded', 'failed'))\n            OR (OLD.state = 'failed' AND NEW.state = 'queued')\n        )\n        BEGIN SELECT RAISE(ABORT, 'invalid wiki batch transition'); END"),
    "wiki_task_recovery_transition": ("trigger", "wiki_tasks", "CREATE TRIGGER wiki_task_recovery_transition\n        BEFORE UPDATE OF recovery_state ON wiki_tasks\n        WHEN NOT (\n            OLD.recovery_state = NEW.recovery_state\n            OR (OLD.recovery_state = 'not_needed' AND NEW.recovery_state = 'required')\n            OR (OLD.recovery_state = 'required' AND NEW.recovery_state = 'running')\n            OR (OLD.recovery_state = 'running' AND NEW.recovery_state IN ('succeeded', 'failed'))\n            OR (OLD.recovery_state = 'failed' AND NEW.recovery_state = 'running')\n            OR (OLD.recovery_state = 'succeeded' AND NEW.recovery_state = 'not_needed')\n        )\n        BEGIN SELECT RAISE(ABORT, 'invalid wiki recovery transition'); END"),
    "wiki_task_boundary_immutable": ("trigger", "wiki_tasks", "CREATE TRIGGER wiki_task_boundary_immutable\n        BEFORE UPDATE OF task_id, vault_path, vault_key, request_kind, trigger_source,\n            backend, model, effort, kit_version, kit_manifest_sha256,\n            boundary_sha256, raw_count, batch_count ON wiki_tasks\n        BEGIN SELECT RAISE(ABORT, 'wiki task boundary is immutable'); END"),
    "wiki_task_no_delete": ("trigger", "wiki_tasks", "CREATE TRIGGER wiki_task_no_delete\n        BEFORE DELETE ON wiki_tasks\n        BEGIN SELECT RAISE(ABORT, 'wiki task is durable'); END"),
    "wiki_task_raw_immutable": ("trigger", "wiki_task_raw", "CREATE TRIGGER wiki_task_raw_immutable\n        BEFORE UPDATE ON wiki_task_raw\n        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END"),
    "wiki_task_raw_no_delete": ("trigger", "wiki_task_raw", "CREATE TRIGGER wiki_task_raw_no_delete\n        BEFORE DELETE ON wiki_task_raw\n        BEGIN SELECT RAISE(ABORT, 'wiki task raw boundary is immutable'); END"),
    "wiki_batch_identity_immutable": ("trigger", "wiki_task_batches", "CREATE TRIGGER wiki_batch_identity_immutable\n        BEFORE UPDATE OF task_id, batch_no, item_count ON wiki_task_batches\n        BEGIN SELECT RAISE(ABORT, 'wiki task batch identity is immutable'); END"),
    "wiki_batch_no_delete": ("trigger", "wiki_task_batches", "CREATE TRIGGER wiki_batch_no_delete\n        BEFORE DELETE ON wiki_task_batches\n        BEGIN SELECT RAISE(ABORT, 'wiki task batch is durable'); END"),
    "wiki_observations": ("table", "wiki_observations", "CREATE TABLE wiki_observations (\n        vault_key TEXT PRIMARY KEY CHECK (length(vault_key) = 64),\n        vault_path TEXT NOT NULL CHECK (TRIM(vault_path) != ''),\n        task_id TEXT REFERENCES wiki_tasks(task_id),\n        pending_count INTEGER CHECK (pending_count IS NULL OR pending_count >= 0),\n        candidate_count INTEGER CHECK (candidate_count IS NULL OR candidate_count >= 0),\n        observed_at TEXT NOT NULL CHECK (TRIM(observed_at) != ''),\n        error_code TEXT CHECK (error_code IS NULL OR error_code IN ('vault_busy', 'config_required', 'kit_missing', 'kit_drift', 'kit_incompatible', 'protocol_error', 'raw_path_invalid', 'raw_symlink', 'raw_changed', 'runner_unavailable', 'runner_timeout', 'model_unavailable', 'network_error', 'agent_failed', 'validation_failed', 'publish_conflict', 'publish_interrupted', 'readback_failed', 'interrupted', 'recovery_failed', 'internal_error')),\n        CHECK (\n            (error_code IS NULL AND pending_count IS NOT NULL AND candidate_count IS NOT NULL)\n            OR (error_code IS NOT NULL AND pending_count IS NULL AND candidate_count IS NULL)\n        )\n    )"),
    "distill_items_ingestion_binding_immutable": ("trigger", "distill_items", "CREATE TRIGGER distill_items_ingestion_binding_immutable\n            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON distill_items\n            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract\n              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256\n              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256\n            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END"),
    "distill_items_ingestion_binding_required": ("trigger", "distill_items", "CREATE TRIGGER distill_items_ingestion_binding_required\n            BEFORE INSERT ON distill_items WHEN NEW.ingestion_contract!='legacy'\n              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)\n            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END"),
    "collection_operations_ingestion_binding_immutable": ("trigger", "collection_operations", "CREATE TRIGGER collection_operations_ingestion_binding_immutable\n            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON collection_operations\n            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract\n              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256\n              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256\n            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END"),
    "collection_operations_ingestion_binding_required": ("trigger", "collection_operations", "CREATE TRIGGER collection_operations_ingestion_binding_required\n            BEFORE INSERT ON collection_operations WHEN NEW.ingestion_contract!='legacy'\n              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)\n            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END"),
    "distill_items_ingestion_owner_immutable": ("trigger", "distill_items", "CREATE TRIGGER distill_items_ingestion_owner_immutable\n        BEFORE UPDATE OF material_id ON distill_items\n        WHEN OLD.ingestion_contract!='legacy' AND OLD.material_id IS NOT NULL\n          AND NEW.material_id IS NOT OLD.material_id\n        BEGIN SELECT RAISE(ABORT,'ingestion owner is immutable'); END"),
    "distill_items_ingestion_no_delete": ("trigger", "distill_items", "CREATE TRIGGER distill_items_ingestion_no_delete\n        BEFORE DELETE ON distill_items WHEN OLD.ingestion_contract!='legacy'\n        BEGIN SELECT RAISE(ABORT,'ingestion owner is durable'); END"),
    "collection_members_ingestion_contract_match": ("trigger", "collection_members", "CREATE TRIGGER collection_members_ingestion_contract_match\n        BEFORE INSERT ON collection_members\n        WHEN (SELECT ingestion_contract FROM collection_operations WHERE operation_id=NEW.operation_id)\n          IS NOT (SELECT ingestion_contract FROM distill_items WHERE item_id=NEW.item_id)\n        BEGIN SELECT RAISE(ABORT,'collection ingestion contract mismatch'); END"),
    "ingestion_events": ("table", "ingestion_events", "CREATE TABLE ingestion_events (\n        event_id INTEGER PRIMARY KEY,\n        event_key TEXT NOT NULL UNIQUE CHECK(length(event_key)=64 AND event_key NOT GLOB '*[^0-9a-f]*'),\n        contract TEXT NOT NULL CHECK(contract='raw-verified-v1'),\n        subject_kind TEXT NOT NULL CHECK(subject_kind IN ('item','material','capture')),\n        subject_id INTEGER NOT NULL CHECK(subject_id>0),\n        item_id INTEGER REFERENCES distill_items(item_id),\n        kind TEXT NOT NULL CHECK(kind IN ('source_ready','raw_pending','raw_verified','release_authorized','media_released')),\n        binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),\n        detail_json TEXT NOT NULL CHECK(json_valid(detail_json) AND json_type(detail_json)='object'),\n        created_at TEXT NOT NULL CHECK(trim(created_at)!='')\n    )"),
    "ingestion_events_subject": ("index", "ingestion_events", "CREATE INDEX ingestion_events_subject ON ingestion_events(subject_kind,subject_id,event_id)"),
    "ingestion_events_observation_typed": ("trigger", "ingestion_events", "CREATE TRIGGER ingestion_events_observation_typed\n        BEFORE INSERT ON ingestion_events WHEN NEW.kind IN ('source_ready','raw_pending')\n          AND COALESCE(NOT (\n            NEW.subject_kind='item' AND NEW.subject_id=NEW.item_id\n            AND EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=NEW.item_id\n                        AND i.ingestion_contract=NEW.contract\n                        AND i.source_binding_sha256=json_extract(NEW.detail_json,'$.source_binding_sha256')\n                        AND i.relation_binding_sha256=json_extract(NEW.detail_json,'$.relation_binding_sha256'))\n            AND (SELECT count(*) FROM json_each(NEW.detail_json))=4\n            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json))=4\n            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json)\n                            WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256'))\n            AND json_type(NEW.detail_json,'$.manifest')='object'\n            AND (SELECT count(*) FROM json_each(NEW.detail_json,'$.manifest'))=2\n            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json,'$.manifest'))=2\n            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json,'$.manifest')\n                            WHERE key NOT IN ('source_fact_id','snapshot_sha256'))\n            AND ((NEW.kind='source_ready' AND json_extract(NEW.detail_json,'$.code')='source_fact_ready')\n                 OR (NEW.kind='raw_pending' AND json_extract(NEW.detail_json,'$.code')\n                     IN ('context_pending','readback_pending','writer_pending')))\n            AND ((json_type(NEW.detail_json,'$.manifest.source_fact_id')='null'\n                  AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='null'\n                  AND NEW.kind='raw_pending')\n                 OR (json_type(NEW.detail_json,'$.manifest.source_fact_id')='integer'\n                     AND json_extract(NEW.detail_json,'$.manifest.source_fact_id')>0\n                     AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='text'\n                     AND length(json_extract(NEW.detail_json,'$.manifest.snapshot_sha256'))=64\n                     AND json_extract(NEW.detail_json,'$.manifest.snapshot_sha256') NOT GLOB '*[^0-9a-f]*'))\n          ),1)\n        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END"),
    "ingestion_events_no_update": ("trigger", "ingestion_events", "CREATE TRIGGER ingestion_events_no_update\n            BEFORE UPDATE ON ingestion_events\n            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END"),
    "ingestion_events_no_delete": ("trigger", "ingestion_events", "CREATE TRIGGER ingestion_events_no_delete\n            BEFORE DELETE ON ingestion_events\n            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END"),
    "ingestion_events_proof_unavailable": ("trigger", "ingestion_events", "CREATE TRIGGER ingestion_events_proof_unavailable\n        BEFORE INSERT ON ingestion_events\n        WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')\n          AND (ingestion_proof(NEW.kind,NEW.binding_sha256,NEW.detail_json)!=1\n               OR NEW.kind IS NOT json_extract(NEW.detail_json,'$.code')\n               OR NEW.subject_kind IS NOT json_extract(NEW.detail_json,'$.manifest.subject_kind')\n               OR NEW.subject_id IS NOT json_extract(NEW.detail_json,'$.manifest.subject_id')\n               OR NEW.item_id IS NOT json_extract(NEW.detail_json,'$.manifest.owner_item_id')\n               OR NEW.binding_sha256 IS NOT json_extract(NEW.detail_json,'$.final_binding_sha256'))\n        BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END"),
    "wiki_task_outcome_binding_immutable": ("trigger", "wiki_tasks", "CREATE TRIGGER wiki_task_outcome_binding_immutable\n        BEFORE UPDATE OF outcome_contract,plan_json,plan_sha256 ON wiki_tasks\n        WHEN NEW.outcome_contract IS NOT OLD.outcome_contract OR NEW.plan_json IS NOT OLD.plan_json\n          OR NEW.plan_sha256 IS NOT OLD.plan_sha256\n        BEGIN SELECT RAISE(ABORT,'wiki outcome binding is immutable'); END"),
    "wiki_task_outcome_binding_required": ("trigger", "wiki_tasks", "CREATE TRIGGER wiki_task_outcome_binding_required\n        BEFORE INSERT ON wiki_tasks WHEN NEW.outcome_contract!='legacy' AND NEW.plan_sha256 IS NULL\n        BEGIN SELECT RAISE(ABORT,'wiki outcome binding required'); END"),
    "wiki_outcome_receipts": ("table", "wiki_outcome_receipts", "CREATE TABLE wiki_outcome_receipts (\n        receipt_id TEXT NOT NULL CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),\n        task_id TEXT NOT NULL,\n        batch_no INTEGER NOT NULL CHECK(batch_no>0),\n        phase TEXT NOT NULL CHECK(phase IN ('validated','accepted')),\n        contract TEXT NOT NULL CHECK(contract='r08-wiki-outcomes-v1'),\n        boundary_sha256 TEXT NOT NULL CHECK(length(boundary_sha256)=64 AND boundary_sha256 NOT GLOB '*[^0-9a-f]*'),\n        plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),\n        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),\n        created_at TEXT NOT NULL CHECK(trim(created_at)!=''),\n        PRIMARY KEY(receipt_id,phase),\n        FOREIGN KEY(task_id,batch_no) REFERENCES wiki_task_batches(task_id,batch_no)\n    )"),
    "wiki_outcome_one_accepted_batch": ("index", "wiki_outcome_receipts", "CREATE UNIQUE INDEX wiki_outcome_one_accepted_batch\n        ON wiki_outcome_receipts(task_id,batch_no) WHERE phase='accepted'"),
    "wiki_outcome_receipts_no_update": ("trigger", "wiki_outcome_receipts", "CREATE TRIGGER wiki_outcome_receipts_no_update\n            BEFORE UPDATE ON wiki_outcome_receipts\n            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END"),
    "wiki_outcome_receipts_no_delete": ("trigger", "wiki_outcome_receipts", "CREATE TRIGGER wiki_outcome_receipts_no_delete\n            BEFORE DELETE ON wiki_outcome_receipts\n            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END"),
    "wiki_outcome_receipts_publish_unavailable": ("trigger", "wiki_outcome_receipts", "CREATE TRIGGER wiki_outcome_receipts_publish_unavailable\n        BEFORE INSERT ON wiki_outcome_receipts WHEN NEW.phase='accepted'\n        BEGIN SELECT RAISE(ABORT,'publish proof unavailable'); END"),
    "source_media_ingestion_no_update": ("trigger", "source_media", "CREATE TRIGGER source_media_ingestion_no_update\n            BEFORE UPDATE ON source_media\n            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id\n                         AND i.ingestion_contract!='legacy')\n              AND ingestion_release(OLD.material_id,OLD.member_id,OLD.sha256,NEW.content)!=1\n            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END"),
    "source_media_ingestion_no_delete": ("trigger", "source_media", "CREATE TRIGGER source_media_ingestion_no_delete\n            BEFORE DELETE ON source_media\n            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id\n                         AND i.ingestion_contract!='legacy')\n              \n            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END"),
    "submitted_sources_ingestion_no_release": ("trigger", "submitted_sources", "CREATE TRIGGER submitted_sources_ingestion_no_release\n        BEFORE UPDATE OF content,input_metadata ON submitted_sources\n        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id\n                     AND i.ingestion_contract!='legacy')\n          AND (NEW.content IS NOT OLD.content OR NEW.input_metadata IS NOT OLD.input_metadata)\n        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END"),
    "submitted_sources_ingestion_no_delete": ("trigger", "submitted_sources", "CREATE TRIGGER submitted_sources_ingestion_no_delete\n        BEFORE DELETE ON submitted_sources\n        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id\n                     AND i.ingestion_contract!='legacy')\n        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END"),
    "submitted_sources_ingestion_owner_immutable": ("trigger", "submitted_sources", "CREATE TRIGGER submitted_sources_ingestion_owner_immutable\n        BEFORE UPDATE OF item_id ON submitted_sources\n        WHEN NEW.item_id IS NOT OLD.item_id AND EXISTS (\n            SELECT 1 FROM distill_items i WHERE i.item_id IN (OLD.item_id,NEW.item_id)\n            AND i.ingestion_contract!='legacy')\n        BEGIN SELECT RAISE(ABORT,'ingestion input owner is immutable'); END"),
    "source_media_ingestion_capture_binding": ("trigger", "capture_state", "CREATE TRIGGER source_media_ingestion_capture_binding\n        BEFORE UPDATE OF item_id,audio_path ON capture_state\n        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id\n        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e\n        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND (NEW.item_id IS NOT OLD.item_id\n            OR (OLD.audio_path IS NOT NULL AND NEW.audio_path IS NOT OLD.audio_path))\n        BEGIN SELECT RAISE(ABORT,'ingestion capture binding is immutable'); END"),
    "source_media_ingestion_capture_release": ("trigger", "capture_state", "CREATE TRIGGER source_media_ingestion_capture_release\n        BEFORE UPDATE OF audio_released_at ON capture_state\n        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id\n        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e\n        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND NEW.audio_released_at IS NOT OLD.audio_released_at\n          AND ingestion_release('capture',OLD.capture_id,OLD.audio_path,NEW.audio_released_at)!=1\n        BEGIN SELECT RAISE(ABORT,'ingestion audio is retained'); END"),
    "source_media_ingestion_capture_no_delete": ("trigger", "capture_state", "CREATE TRIGGER source_media_ingestion_capture_no_delete\n        BEFORE DELETE ON capture_state WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id\n        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e\n        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1'))\n        BEGIN SELECT RAISE(ABORT,'ingestion capture owner is durable'); END"),
    "distill_items_raw_terminal_no_insert": ("trigger", "distill_items", "CREATE TRIGGER distill_items_raw_terminal_no_insert\n        BEFORE INSERT ON distill_items WHEN NEW.state='raw_saved'\n        BEGIN SELECT RAISE(ABORT,'raw terminal writer required'); END"),
    "distill_items_raw_terminal_proof": ("trigger", "distill_items", "CREATE TRIGGER distill_items_raw_terminal_proof\n        BEFORE UPDATE ON distill_items WHEN NEW.state='raw_saved' AND OLD.state!='raw_saved'\n        AND (OLD.state!='working' OR OLD.phase NOT IN ('collecting','reviewing')\n            OR NEW.phase!='done' OR NEW.ingestion_contract!='raw-verified-v1'\n            OR NEW.confirmation_json IS NOT NULL OR NEW.dismissed_at IS NOT NULL\n            OR NEW.material_id IS NULL OR NEW.source_binding_sha256 IS NULL\n            OR NEW.relation_binding_sha256 IS NULL\n            OR ingestion_raw_terminal(OLD.item_id,OLD.review_revision,NEW.state,NEW.phase)!=1)\n        BEGIN SELECT RAISE(ABORT,'raw terminal proof unavailable'); END"),
    "distill_items_raw_terminal_no_reopen": ("trigger", "distill_items", "CREATE TRIGGER distill_items_raw_terminal_no_reopen\n        BEFORE UPDATE OF state,phase ON distill_items WHEN OLD.state='raw_saved'\n        AND (NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase)\n        BEGIN SELECT RAISE(ABORT,'raw terminal is durable'); END"),
}

_INGESTION_COLUMNS = (
    "ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1'))",
    "source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'))",
    "relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*'))",
)
_OUTCOME_COLUMNS = (
    "outcome_contract TEXT NOT NULL DEFAULT 'legacy' CHECK(outcome_contract IN ('legacy','r08-wiki-outcomes-v1'))",
    "plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(plan_json) AND json_type(plan_json)='object')",
    "plan_sha256 TEXT CHECK(plan_sha256 IS NULL OR (length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'))",
)
_ADDITIONS = {'distill_items': _INGESTION_COLUMNS,
              'collection_operations': _INGESTION_COLUMNS,
              'wiki_tasks': _OUTCOME_COLUMNS}
_AUTO_INDEX_COLUMNS = {
    'sqlite_autoindex_wiki_tasks_1': ('wiki_tasks', ('task_id',)),
    'sqlite_autoindex_wiki_tasks_2': ('wiki_tasks', ('vault_key', 'boundary_sha256', 'kit_manifest_sha256', 'backend', 'model', 'effort')),
    'sqlite_autoindex_wiki_task_batches_1': ('wiki_task_batches', ('task_id', 'batch_no')),
    'sqlite_autoindex_wiki_task_raw_1': ('wiki_task_raw', ('task_id', 'ordinal')),
    'sqlite_autoindex_wiki_task_raw_2': ('wiki_task_raw', ('task_id', 'relative_path')),
    'sqlite_autoindex_wiki_observations_1': ('wiki_observations', ('vault_key',)),
    'sqlite_autoindex_ingestion_events_1': ('ingestion_events', ('event_key',)),
    'sqlite_autoindex_wiki_outcome_receipts_1': ('wiki_outcome_receipts', ('receipt_id', 'phase')),
}
_WIKI_V23 = {name: value for name, value in _APPROVED_DDL.items()
             if name.startswith('wiki_') and not name.startswith('wiki_outcome_')
             and name not in ('wiki_task_outcome_binding_immutable', 'wiki_task_outcome_binding_required')}




# Independent historical25 and current26 primary expressions, frozen as literals.
_PRIOR25_BASE_DDL = {'distill_items': ('table', 'distill_items', 'CREATE TABLE "distill_items" (\n    item_id INTEGER PRIMARY KEY,\n    submitted_url TEXT NOT NULL,\n    state TEXT NOT NULL CHECK (\n        state IN (\'queued\',\'working\',\'waiting_user\',\'succeeded\',\'failed\',\'raw_saved\')\n    ),\n    phase TEXT NOT NULL CHECK (\n        phase IN (\'collecting\', \'reviewing\', \'distilling\', \'publishing\', \'done\')\n    ),\n    material_id INTEGER REFERENCES materials(material_id),\n    error_code TEXT,\n    rejection_reason TEXT,\n    dismissed_at TEXT,\n    confirmation_json TEXT,\n    queued_at TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL\n, platform_authority_json TEXT NOT NULL DEFAULT \'{}\', submitted_title TEXT NOT NULL DEFAULT \'\', review_revision INTEGER NOT NULL DEFAULT 0, ingestion_contract TEXT NOT NULL DEFAULT \'legacy\' CHECK (ingestion_contract IN (\'legacy\',\'raw-verified-v1\')), source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB \'*[^0-9a-f]*\')), relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB \'*[^0-9a-f]*\')), CHECK(state!=\'raw_saved\' OR (phase=\'done\' AND ingestion_contract=\'raw-verified-v1\')))'), 'submitted_sources': ('table', 'submitted_sources', "CREATE TABLE submitted_sources (\n    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),\n    input_kind TEXT NOT NULL CHECK (input_kind IN ('direct_text', 'markdown', 'pdf', 'epub', 'image')),\n    input_key TEXT NOT NULL,\n    input_label TEXT NOT NULL,\n    input_metadata TEXT NOT NULL,\n    content BLOB,\n    retain_until TEXT,\n    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),\n    UNIQUE(input_kind, input_key)\n)"), 'distill_review_revision': ('trigger', 'distill_items', 'CREATE TRIGGER distill_review_revision AFTER UPDATE ON distill_items\n                WHEN NEW.review_revision = OLD.review_revision AND (\n                    NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase\n                    OR NEW.material_id IS NOT OLD.material_id\n                    OR NEW.submitted_url IS NOT OLD.submitted_url\n                    OR NEW.confirmation_json IS NOT OLD.confirmation_json\n                    OR NEW.platform_authority_json IS NOT OLD.platform_authority_json\n                ) BEGIN\n                UPDATE distill_items SET review_revision = OLD.review_revision + 1\n                WHERE item_id = NEW.item_id;\n            END'), 'source_media_no_update': ('trigger', 'source_media', "CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media\n        WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id=OLD.material_id)\n        AND NOT (NEW.material_id=OLD.material_id AND NEW.member_id=OLD.member_id\n            AND NEW.position=OLD.position AND NEW.mime_type=OLD.mime_type\n            AND NEW.sha256=OLD.sha256 AND length(OLD.content)>0\n            AND typeof(NEW.content)='blob' AND length(NEW.content)=0\n            AND EXISTS (SELECT 1 FROM materials m WHERE m.material_id=OLD.material_id AND m.source_kind IN ('douyin','youtube','xiaohongshu','x','zhihu','weibo','bilibili','image')\n    AND EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=m.material_id)\n    AND NOT EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=m.material_id\n        AND (i.confirmation_json IS NOT NULL OR i.state='working'\n             OR (i.dismissed_at IS NULL AND i.state!='succeeded')))\n    AND EXISTS (SELECT 1 FROM source_facts sf WHERE sf.material_id=m.material_id) AND EXISTS (SELECT 1 FROM raw_records r WHERE r.subject_kind='material'\n        AND r.subject_id=m.material_id AND r.written_at IS NOT NULL)))\n        BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END"), 'confirmation_decisions': ('table', 'confirmation_decisions', 'CREATE TABLE confirmation_decisions (\n                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),\n                revision TEXT NOT NULL, action TEXT NOT NULL, value TEXT NOT NULL,\n                state TEXT NOT NULL, PRIMARY KEY(item_id, revision))'), 'source_review_results': ('table', 'source_review_results', "CREATE TABLE source_review_results (\n                item_id INTEGER NOT NULL REFERENCES distill_items(item_id),\n                revision INTEGER NOT NULL, identity TEXT NOT NULL,\n                status TEXT NOT NULL CHECK(status IN ('complete','failed')),\n                result_json TEXT NOT NULL, created_at TEXT NOT NULL,\n                PRIMARY KEY(item_id, revision))"), 'collection_members': ('table', 'collection_members', 'CREATE TABLE collection_members (\n        operation_id INTEGER NOT NULL REFERENCES collection_operations(operation_id),\n        ordinal INTEGER NOT NULL,\n        native_id TEXT NOT NULL, native_version TEXT NOT NULL,\n        item_id INTEGER NOT NULL UNIQUE REFERENCES distill_items(item_id),\n        known_unsupported INTEGER NOT NULL CHECK(known_unsupported IN (0,1)),\n        source_fact_id INTEGER REFERENCES source_facts(source_fact_id),\n        knowledge_result_id INTEGER REFERENCES knowledge_results(knowledge_result_id),\n        PRIMARY KEY(operation_id,ordinal), UNIQUE(operation_id,native_id)\n    )'), 'manual_cards': ('table', 'manual_cards', "CREATE TABLE manual_cards (\n        enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,\n        scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,\n        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),\n        review_round_id TEXT NOT NULL, group_id TEXT NOT NULL,\n        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','suspended','resolved','superseded')),\n        ordering_basis TEXT NOT NULL CHECK(ordering_basis IN ('observed','migration_inferred')),\n        ordering_reason TEXT NOT NULL, entered_at TEXT NOT NULL,\n        mapping_json TEXT NOT NULL,\n        UNIQUE(item_id,review_round_id,group_id))"), 'group_decisions': ('table', 'group_decisions', 'CREATE TABLE group_decisions (\n        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),\n        request_id TEXT NOT NULL, group_id TEXT NOT NULL,\n        submitted_revision TEXT NOT NULL, selection_digest TEXT NOT NULL,\n        payload_digest TEXT NOT NULL, result_json TEXT NOT NULL,\n        audit_json TEXT NOT NULL, committed_at TEXT NOT NULL,\n        PRIMARY KEY(item_id,request_id),\n        UNIQUE(item_id,submitted_revision,selection_digest))'), 'capture_state': ('table', 'capture_state', 'CREATE TABLE capture_state (\n        capture_id INTEGER PRIMARY KEY REFERENCES captures(capture_id),\n        item_id INTEGER REFERENCES distill_items(item_id),\n        audio_path TEXT, audio_released_at TEXT\n    )'), 'feishu_parts': ('table', 'feishu_parts', 'CREATE TABLE feishu_parts (\n        app_id TEXT NOT NULL, message_id TEXT NOT NULL, position INTEGER NOT NULL,\n        item_id INTEGER REFERENCES distill_items(item_id),\n        error TEXT, preview_json TEXT,\n        PRIMARY KEY(app_id,message_id,position),\n        FOREIGN KEY(app_id,message_id) REFERENCES feishu_receipts(app_id,message_id)\n    )')}
_PRIOR25_PARENT_CHILDREN = ('submitted_sources', 'confirmation_decisions', 'source_review_results', 'collection_members', 'manual_cards', 'group_decisions', 'capture_state', 'feishu_parts', 'ingestion_events')
_PRIOR25_PARENT_DEPENDENTS = ('distill_review_revision', 'distill_items_ingestion_binding_immutable', 'distill_items_ingestion_binding_required', 'distill_items_ingestion_owner_immutable', 'distill_items_ingestion_no_delete', 'collection_members_ingestion_contract_match', 'ingestion_events_observation_typed', 'source_media_no_update', 'source_media_ingestion_no_update', 'source_media_ingestion_no_delete', 'submitted_sources_ingestion_no_release', 'submitted_sources_ingestion_no_delete', 'submitted_sources_ingestion_owner_immutable', 'source_media_ingestion_capture_binding', 'source_media_ingestion_capture_release', 'source_media_ingestion_capture_no_delete')
_V26_DDL = {'submitted_sources_local_insert': ('trigger', 'submitted_sources', "CREATE TRIGGER submitted_sources_local_insert BEFORE INSERT ON submitted_sources\n        WHEN NEW.binding_scope!='legacy' AND (\n          typeof(NEW.binding_scope)!='text' OR\n          local_intake_insert(NEW.item_id,NEW.binding_scope,NEW.input_kind,NEW.input_key,\n                             NEW.input_label,NEW.input_metadata,NEW.content)!=1)\n        BEGIN SELECT RAISE(ABORT,'local intake input unverified'); END"), 'submitted_sources_local_tuple': ('trigger', 'submitted_sources', "CREATE TRIGGER submitted_sources_local_tuple BEFORE UPDATE ON submitted_sources\n        WHEN (OLD.binding_scope!='legacy' OR NEW.binding_scope!='legacy') AND (\n          NEW.item_id IS NOT OLD.item_id OR NEW.binding_scope IS NOT OLD.binding_scope\n          OR NEW.input_kind IS NOT OLD.input_kind OR NEW.input_key IS NOT OLD.input_key\n          OR NEW.input_label IS NOT OLD.input_label OR NEW.input_metadata IS NOT OLD.input_metadata\n          OR NEW.content IS NOT OLD.content OR NEW.retain_until IS NOT OLD.retain_until\n          OR NEW.retryable IS NOT OLD.retryable)\n        BEGIN SELECT RAISE(ABORT,'local intake input is immutable'); END"), 'submitted_sources_local_no_delete': ('trigger', 'submitted_sources', "CREATE TRIGGER submitted_sources_local_no_delete BEFORE DELETE ON submitted_sources\n        WHEN OLD.binding_scope!='legacy'\n        BEGIN SELECT RAISE(ABORT,'local intake input is retained'); END"), 'ingestion_events_observation_typed': ('trigger', 'ingestion_events', "CREATE TRIGGER ingestion_events_observation_typed\n        BEFORE INSERT ON ingestion_events WHEN NEW.kind IN ('source_ready','raw_pending')\n          AND COALESCE(NOT ((\n            NEW.subject_kind='item' AND NEW.subject_id=NEW.item_id\n            AND EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=NEW.item_id\n                        AND i.ingestion_contract=NEW.contract\n                        AND i.source_binding_sha256=json_extract(NEW.detail_json,'$.source_binding_sha256')\n                        AND i.relation_binding_sha256=json_extract(NEW.detail_json,'$.relation_binding_sha256'))\n            AND (SELECT count(*) FROM json_each(NEW.detail_json))=4\n            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json))=4\n            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json)\n                            WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256'))\n            AND json_type(NEW.detail_json,'$.manifest')='object'\n            AND (SELECT count(*) FROM json_each(NEW.detail_json,'$.manifest'))=2\n            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json,'$.manifest'))=2\n            AND NOT EXISTS (SELECT 1 FROM json_each(NEW.detail_json,'$.manifest')\n                            WHERE key NOT IN ('source_fact_id','snapshot_sha256'))\n            AND ((NEW.kind='source_ready' AND json_extract(NEW.detail_json,'$.code')='source_fact_ready')\n                 OR (NEW.kind='raw_pending' AND json_extract(NEW.detail_json,'$.code')\n                     IN ('context_pending','readback_pending','writer_pending')))\n            AND ((json_type(NEW.detail_json,'$.manifest.source_fact_id')='null'\n                  AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='null'\n                  AND NEW.kind='raw_pending')\n                 OR (json_type(NEW.detail_json,'$.manifest.source_fact_id')='integer'\n                     AND json_extract(NEW.detail_json,'$.manifest.source_fact_id')>0\n                     AND json_type(NEW.detail_json,'$.manifest.snapshot_sha256')='text'\n                     AND length(json_extract(NEW.detail_json,'$.manifest.snapshot_sha256'))=64\n                     AND json_extract(NEW.detail_json,'$.manifest.snapshot_sha256') NOT GLOB '*[^0-9a-f]*'))\n          ) OR (NEW.kind='raw_pending' AND NEW.subject_kind='item'\n            AND NEW.subject_id=NEW.item_id AND json_extract(NEW.detail_json,'$.code')='intake_frozen'\n            AND (SELECT count(*) FROM json_each(NEW.detail_json))=4\n            AND (SELECT count(DISTINCT key) FROM json_each(NEW.detail_json))=4\n            AND NOT EXISTS(SELECT 1 FROM json_each(NEW.detail_json)\n                WHERE key NOT IN ('code','manifest','source_binding_sha256','relation_binding_sha256'))\n            AND json_type(NEW.detail_json,'$.manifest')='object'\n            AND (SELECT count(*) FROM json_each(NEW.detail_json,'$.manifest'))=1\n            AND json_type(NEW.detail_json,'$.manifest.intake_envelope_json')='text'\n            AND local_intake_event(NEW.event_key,NEW.contract,NEW.subject_kind,NEW.subject_id,\n                NEW.item_id,NEW.binding_sha256,NEW.detail_json)=1)),1)\n        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END"), 'ingestion_events_local_owner': ('index', 'ingestion_events', "CREATE UNIQUE INDEX ingestion_events_local_owner ON ingestion_events(item_id)\n    WHERE kind='raw_pending' AND json_extract(detail_json,'$.code')='intake_frozen'")}
_SUBMITTED_V26_SQL = 'CREATE TABLE "submitted_sources" (\n    item_id INTEGER PRIMARY KEY REFERENCES distill_items(item_id),\n    input_kind TEXT NOT NULL CHECK (input_kind IN (\'direct_text\', \'markdown\', \'pdf\', \'epub\', \'image\')),\n    input_key TEXT NOT NULL,\n    input_label TEXT NOT NULL,\n    input_metadata TEXT NOT NULL,\n    content BLOB,\n    retain_until TEXT,\n    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),\n    binding_scope TEXT NOT NULL DEFAULT \'legacy\' CHECK(typeof(binding_scope)=\'text\'),\n    UNIQUE(input_kind, input_key, binding_scope)\n)'

class UpgradeProbeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _absolute(path: Path | str) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise UpgradeProbeError("fixture_invalid")
    return Path(os.path.normpath(str(raw)))


def _reject_symlinks(path: Path, *, missing_leaf: bool = False) -> Path:
    path = _absolute(path)
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], start=1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing_leaf and index == len(path.parts) - 1:
                return path
            raise UpgradeProbeError("fixture_invalid") from None
        if stat.S_ISLNK(info.st_mode):
            raise UpgradeProbeError("fixture_invalid")
    return path


def _mode(path: Path, expected: int, *, directory: bool) -> os.stat_result:
    path = _reject_symlinks(path)
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or stat.S_IMODE(info.st_mode) != expected:
        raise UpgradeProbeError("fixture_invalid")
    return info


def _related(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"kd-synthetic-vault-v1\0")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise UpgradeProbeError("fixture_invalid")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if stat.S_ISDIR(info.st_mode):
            kind = b"D"
        elif stat.S_ISREG(info.st_mode):
            kind = b"F"
        else:
            raise UpgradeProbeError("fixture_invalid")
        digest.update(kind + len(relative).to_bytes(8, "big") + relative)
        digest.update(stat.S_IMODE(info.st_mode).to_bytes(4, "big"))
        if kind == b"F":
            digest.update(info.st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _read_marker(path: Path) -> dict:
    _mode(path, 0o600, directory=False)
    if path.stat().st_size > 16 * 1024:
        raise UpgradeProbeError("fixture_invalid")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise UpgradeProbeError("fixture_invalid") from None
    expected = {
        "schema_version", "purpose", "database_name", "database_before_sha256",
        "expected_schema", "vault_relpath", "vault_tree_sha256",
    }
    if (not isinstance(marker, dict) or set(marker) != expected
            or marker.get("schema_version") != 1
            or marker.get("purpose") != MARKER_PURPOSE
            or marker.get("database_name") != DATABASE_NAME
            or type(marker.get("expected_schema")) is not int
            or marker["expected_schema"] not in _PRIOR_SCHEMAS
            or marker.get("vault_relpath") != VAULT_RELPATH):
        raise UpgradeProbeError("fixture_invalid")
    for key in ("database_before_sha256", "vault_tree_sha256"):
        value = marker.get(key)
        if (not isinstance(value, str) or len(value) != _SHA256_LENGTH
                or any(character not in "0123456789abcdef" for character in value)):
            raise UpgradeProbeError("fixture_invalid")
    return marker


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except Exception:
        connection.close()
        raise
    return connection


def _hash_value(digest: "hashlib._Hash", value) -> None:
    if value is None:
        tag, payload = b"N", b""
    elif type(value) is int:
        tag, payload = b"I", str(value).encode("ascii")
    elif type(value) is float:
        tag, payload = b"F", struct.pack(">d", value)
    elif type(value) is str:
        tag, payload = b"T", value.encode("utf-8")
    elif isinstance(value, bytes):
        tag, payload = b"B", value
    else:
        raise UpgradeProbeError("precheck_failed")
    digest.update(tag + len(payload).to_bytes(8, "big") + payload)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _objects(connection):
    return {row[1]: (row[0], row[2], row[3]) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")}


def _columns(connection, name):
    return tuple(tuple(row) for row in connection.execute(
        f"PRAGMA table_xinfo({_quote_identifier(name)})"))


def _indices(connection, tables):
    result = {}
    for table in tables:
        for row in connection.execute(f"PRAGMA index_list({_quote_identifier(table)})"):
            result[row[1]] = (row[2], row[3], row[4], tuple(tuple(value) for value in
                connection.execute(f"PRAGMA index_xinfo({_quote_identifier(row[1])})")))
    return result


def _added_columns(connection, table, definitions, *, code, check_defaults):
    current = _columns(connection, table)
    names = tuple(definition.split()[0] for definition in definitions)
    defaults = ("'legacy'", "'{}'", None) if table == 'wiki_tasks' else ("'legacy'", None, None)
    suffix = current[-len(definitions):]
    if len(suffix) != len(definitions):
        raise UpgradeProbeError(code)
    for row, name, default in zip(suffix, names, defaults):
        if tuple(row[1:]) != (name, 'TEXT', int(default is not None), default, 0, 0):
            raise UpgradeProbeError(code)
    if check_defaults:
        selected = ','.join(_quote_identifier(name) for name in names)
        wanted = ('legacy', '{}', None) if table == 'wiki_tasks' else ('legacy', None, None)
        if any(tuple(row) != wanted for row in connection.execute(
                f'SELECT {selected} FROM {_quote_identifier(table)}')):
            raise UpgradeProbeError(code)


def _full_autoindices(connection, indices, *, code):
    for name, (table, names) in _AUTO_INDEX_COLUMNS.items():
        columns = {row[1]: row[0] for row in _columns(connection, table)}
        origin = 'u' if table == 'ingestion_events' or name.endswith('_2') else 'pk'
        expected = tuple((position, columns[column], column, 0, 'BINARY', 1)
                         for position, column in enumerate(names))
        expected += ((len(names), -1, None, 0, 'BINARY', 0),)
        if indices.get(name) != (1, origin, 0, expected):
            raise UpgradeProbeError(code)


def _input_index(*, target):
    columns = ((1, 'input_kind'), (2, 'input_key'))
    if target:
        columns += ((8, 'binding_scope'),)
    rows = tuple((position, cid, name, 0, 'BINARY', 1)
                 for position, (cid, name) in enumerate(columns))
    return (1, 'u', 0, rows + ((len(columns), -1, None, 0, 'BINARY', 0),))


def _freeze25(connection, objects, columns):
    code = 'precheck_failed'
    expected = {**_APPROVED_DDL, **_PRIOR25_BASE_DDL}
    kind, parent, sql = expected['wiki_tasks']
    anchor = "        updated_at TEXT NOT NULL CHECK (TRIM(updated_at) != ''),"
    if sql.count(anchor) != 1:
        raise UpgradeProbeError(code)
    expected['wiki_tasks'] = (kind, parent, sql.replace(
        anchor, anchor[:-1] + ', ' + ', '.join(_OUTCOME_COLUMNS) + ',', 1))
    for name, value in expected.items():
        actual = objects.get(name)
        # Historical initialization can keep this one unquoted input table header.
        if name == 'submitted_sources' and actual is not None:
            actual = (actual[0], actual[1], actual[2].replace(
                'CREATE TABLE "submitted_sources"', 'CREATE TABLE submitted_sources', 1))
        if actual != value:
            raise UpgradeProbeError(code)
    reserved = (set(_V26_DDL) - {'ingestion_events_observation_typed'}) | {
        'submitted_sources_v26', 'distill_items_v25'}
    if reserved & objects.keys():
        raise UpgradeProbeError(code)
    if connection.execute("SELECT 1 FROM ingestion_events WHERE "
            "json_extract(detail_json,'$.code')='intake_frozen' LIMIT 1").fetchone():
        raise UpgradeProbeError(code)
    for table, definitions in _ADDITIONS.items():
        _added_columns(connection, table, definitions, code=code, check_defaults=False)
    input_columns = (
        (0, 'item_id', 'INTEGER', 0, None, 1, 0),
        (1, 'input_kind', 'TEXT', 1, None, 0, 0),
        (2, 'input_key', 'TEXT', 1, None, 0, 0),
        (3, 'input_label', 'TEXT', 1, None, 0, 0),
        (4, 'input_metadata', 'TEXT', 1, None, 0, 0),
        (5, 'content', 'BLOB', 0, None, 0, 0),
        (6, 'retain_until', 'TEXT', 0, None, 0, 0),
        (7, 'retryable', 'INTEGER', 1, '1', 0, 0),
    )
    if columns['submitted_sources'] != input_columns:
        raise UpgradeProbeError(code)
    if connection.execute("""SELECT 1 FROM submitted_sources WHERE typeof(item_id)!='integer'
        OR typeof(input_kind)!='text' OR typeof(input_key)!='text' OR typeof(input_label)!='text'
        OR typeof(input_metadata)!='text' OR typeof(content) NOT IN ('blob','null')
        OR typeof(retain_until) NOT IN ('text','null') OR typeof(retryable)!='integer'
        OR retryable NOT IN (0,1) LIMIT 1""").fetchone():
        raise UpgradeProbeError(code)
    incoming = set()
    for table in columns:
        fks = tuple(tuple(row) for row in connection.execute(
            f'PRAGMA foreign_key_list({_quote_identifier(table)})'))
        for fk in fks:
            if fk[2] == 'submitted_sources':
                raise UpgradeProbeError(code)
            if fk[2] == 'distill_items':
                if fk[3:] != ('item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'):
                    raise UpgradeProbeError(code)
                incoming.add(table)
    if incoming != set(_PRIOR25_PARENT_CHILDREN):
        raise UpgradeProbeError(code)
    for table, wanted in (
            ('submitted_sources', ((0, 0, 'distill_items', 'item_id', 'item_id', 'NO ACTION', 'NO ACTION', 'NONE'),)),
            ('distill_items', ((0, 0, 'materials', 'material_id', 'material_id', 'NO ACTION', 'NO ACTION', 'NONE'),))):
        if tuple(tuple(row) for row in connection.execute(
                f'PRAGMA foreign_key_list({_quote_identifier(table)})')) != wanted:
            raise UpgradeProbeError(code)
    parent_guards = set(_PRIOR25_PARENT_DEPENDENTS) | {
        'distill_items_raw_terminal_no_insert', 'distill_items_raw_terminal_proof',
        'distill_items_raw_terminal_no_reopen'}
    input_guards = {name for name in _APPROVED_DDL if name.startswith('submitted_sources_')}
    for table, allowed in (('submitted_sources', input_guards), ('distill_items', parent_guards)):
        found = set()
        for name, (kind, owner, sql) in objects.items():
            if kind not in ('trigger', 'view', 'index'):
                continue
            if owner == table or (sql and re.search(r'\b' + table + r'\b', sql, re.IGNORECASE)):
                if table == 'submitted_sources' and name == 'sqlite_autoindex_submitted_sources_1' and kind == 'index' and sql is None:
                    continue
                if kind != 'trigger' or name not in allowed:
                    raise UpgradeProbeError(code)
                found.add(name)
        if found != allowed:
            raise UpgradeProbeError(code)
    indices = _indices(connection, columns)
    if indices.get('sqlite_autoindex_submitted_sources_1') != _input_index(target=False):
        raise UpgradeProbeError(code)
    _full_autoindices(connection, indices, code=code)


def _freeze(connection, version):
    objects = _objects(connection)
    columns = {name: _columns(connection, name) for name, value in objects.items()
               if value[0] == 'table'}
    if version not in _PRIOR_SCHEMAS:
        raise UpgradeProbeError('unsupported_schema')
    if not {'settings', 'raw_records', 'captures', 'capture_state', 'source_media',
            'source_facts', 'distill_items', 'collection_operations'} <= columns.keys():
        raise UpgradeProbeError('precheck_failed')
    required = {
        'settings': {'key', 'value'},
        'raw_records': {'raw_id', 'subject_kind', 'subject_id', 'identity', 'relative_path',
                        'content', 'content_sha256', 'attachments_json', 'supersedes'},
        'captures': {'capture_id', 'app_id', 'message_id', 'raw_id', 'text'},
        'capture_state': {'capture_id', 'item_id', 'audio_path', 'audio_released_at'},
        'source_media': {'material_id', 'member_id', 'position', 'sha256', 'content'},
        'source_facts': {'source_fact_id', 'material_id', 'snapshot', 'lineage_json'},
        'distill_items': {'item_id', 'material_id', 'state', 'phase'},
        'collection_operations': {'operation_id', 'manifest_json', 'signature', 'content_signature'},
    }
    if any(not names <= {row[1] for row in columns[table]} for table, names in required.items()):
        raise UpgradeProbeError('precheck_failed')
    if version == 25:
        _freeze25(connection, objects, columns)
        return {'version': version, 'objects': objects, 'columns': columns,
                'indices': _indices(connection, columns),
                'foreign_keys': {name: tuple(tuple(row) for row in connection.execute(
                    f'PRAGMA foreign_key_list({_quote_identifier(name)})')) for name in columns}}
    wiki = dict(_WIKI_V23)
    if version == 21:
        wiki = {}
    elif version == 22:
        wiki.pop('wiki_observations')
        kind, parent, sql = wiki['wiki_tasks']
        wiki['wiki_tasks'] = (kind, parent, sql.replace(
            'UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256, backend, model, effort)',
            'UNIQUE(vault_key, boundary_sha256, kit_manifest_sha256)'))
        kind, parent, sql = wiki['wiki_one_unresolved_task_per_vault']
        wiki['wiki_one_unresolved_task_per_vault'] = (kind, parent, sql.replace(
            "('queued', 'preparing', 'running', 'validating', 'publishing')",
            "('queued', 'preparing', 'running', 'validating', 'publishing', 'failed')"))
    for name, expected in wiki.items():
        if objects.get(name) != expected:
            raise UpgradeProbeError('precheck_failed')
    for name in _APPROVED_DDL:
        if name not in wiki and name in objects:
            raise UpgradeProbeError('precheck_failed')
    for table, definitions in _ADDITIONS.items():
        if table in columns and any(row[1] == definition.split()[0]
                for row in columns[table] for definition in definitions):
            raise UpgradeProbeError('precheck_failed')
    return {'version': version, 'objects': objects, 'columns': columns,
            'indices': _indices(connection, columns),
            'foreign_keys': {name: tuple(tuple(row) for row in connection.execute(
                f"PRAGMA foreign_key_list({_quote_identifier(name)})")) for name in columns}}


def _parent_v25_sql(sql):
    if not sql.startswith('CREATE TABLE distill_items (') or not sql.rstrip().endswith(')'):
        raise UpgradeProbeError('legacy_changed')
    sql, count = re.subn(_PARENT_STATE_PATTERN, _PARENT_STATE_V25, sql)
    if count != 1:
        raise UpgradeProbeError('legacy_changed')
    sql = sql.rstrip()[:-1] + _PARENT_CHECK_V25
    return sql.replace('CREATE TABLE distill_items', _PARENT_FINAL_NAME, 1)


def _without_additions(sql, table):
    for definition in _ADDITIONS.get(table, ()):
        fragment = ', ' + definition
        if sql.count(fragment) != 1:
            raise UpgradeProbeError('legacy_changed')
        sql = sql.replace(fragment, '', 1)
    return sql


def _check_after(connection, prior):
    if connection.execute('PRAGMA user_version').fetchone()[0] != _APPROVED_TARGET_SCHEMA:
        raise UpgradeProbeError('legacy_changed')
    actual = _objects(connection)
    expected = {**prior['objects'], **_APPROVED_DDL, **_V26_DDL}
    if prior['version'] == 25:
        expected = {**prior['objects'], **_V26_DDL}
    expected['submitted_sources'] = ('table', 'submitted_sources', _SUBMITTED_V26_SQL)
    for name, (table, _) in _AUTO_INDEX_COLUMNS.items():
        expected[name] = ('index', table, None)
    if actual.keys() != expected.keys():
        raise UpgradeProbeError('legacy_changed')
    for name, wanted in expected.items():
        kind, parent, sql = actual[name]
        if kind == 'table' and name in _ADDITIONS and prior['version'] != 25:
            sql = _without_additions(sql, name)
        if name == 'distill_items' and prior['version'] != 25:
            wanted = (wanted[0], wanted[1], _parent_v25_sql(wanted[2]))
        if (kind, parent, sql) != wanted:
            raise UpgradeProbeError('legacy_changed')
    for table, old in prior['columns'].items():
        current = _columns(connection, table)
        additions = _ADDITIONS.get(table, ()) if prior['version'] != 25 else ()
        if table == 'submitted_sources':
            additions = ("binding_scope TEXT NOT NULL DEFAULT 'legacy' CHECK(typeof(binding_scope)='text')",)
        if current[:len(old)] != old or len(current) != len(old) + len(additions):
            raise UpgradeProbeError('legacy_changed')
        fks = tuple(tuple(row) for row in connection.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"))
        if fks != prior['foreign_keys'][table]:
            raise UpgradeProbeError('legacy_changed')
    for table, definitions in _ADDITIONS.items():
        _added_columns(connection, table, definitions, code='legacy_changed',
                       check_defaults=prior['version'] != 25)
    if _columns(connection, 'submitted_sources')[-1] != (8, 'binding_scope', 'TEXT', 1, "'legacy'", 0, 0):
        raise UpgradeProbeError('legacy_changed')
    if connection.execute("SELECT 1 FROM submitted_sources WHERE binding_scope IS NOT 'legacy' LIMIT 1").fetchone():
        raise UpgradeProbeError('legacy_changed')
    indices = _indices(connection, [name for name, value in actual.items() if value[0] == 'table'])
    for name, old in prior['indices'].items():
        if name == 'sqlite_autoindex_submitted_sources_1':
            continue  # Only this exact full three-key index transform is allowed.
        if name == 'sqlite_autoindex_wiki_tasks_2' and prior['version'] == 22:
            continue  # Exact approved six-column uniqueness is checked below.
        if indices.get(name) != old:
            raise UpgradeProbeError('legacy_changed')
    _full_autoindices(connection, indices, code='legacy_changed')
    if indices.get('sqlite_autoindex_submitted_sources_1') != _input_index(target=True):
        raise UpgradeProbeError('legacy_changed')
    if indices.get('ingestion_events_local_owner') != (1, 'c', 1, (
            (0, 5, 'item_id', 0, 'BINARY', 1), (1, -1, None, 0, 'BINARY', 0))):
        raise UpgradeProbeError('legacy_changed')
    for table in ('ingestion_events', 'wiki_outcome_receipts'):
        if table not in prior['columns'] and connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
            raise UpgradeProbeError('legacy_changed')


def _legacy_identity(connection, prior):
    digest = hashlib.sha256(b'kd-all-prior-schema-and-columns-v2\0')
    _hash_value(digest, prior['version'])
    # The frozen source descriptor is hashed; actual post-DDL is separately
    # compared against its precise approved transform before hashing rows.
    for name, value in sorted(prior['objects'].items()):
        _hash_value(digest, name)
        for part in value:
            _hash_value(digest, part)
    counts = {}
    for name, columns in sorted(prior['columns'].items()):
        _hash_value(digest, name)
        for column in columns:
            for value in column:
                _hash_value(digest, value)
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        order = ','.join(_quote_identifier(column) for column in primary) if primary else 'rowid'
        selected = ','.join(_quote_identifier(row[1]) for row in columns)
        # Preserve physical row identities even on tables with a text/composite PK.
        # WITHOUT ROWID tables have no physical rowid to preserve.
        if not re.search(r'\bWITHOUT\s+ROWID\b', prior['objects'][name][2] or '', re.IGNORECASE):
            selected = 'rowid,' + selected
        query = f"SELECT {selected} FROM {_quote_identifier(name)} ORDER BY {order}"
        count = 0
        for row in connection.execute(query):
            count += 1
            digest.update(len(row).to_bytes(4, 'big'))
            for value in row:
                _hash_value(digest, value)
        counts[name] = count
    return counts, digest.hexdigest()


def _snapshot(database: Path, expected_vault: Path, *, prior=None) -> dict:
    with closing(_sqlite_readonly(database)) as connection:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_rows = [tuple(row) for row in connection.execute("PRAGMA quick_check")]
        foreign_key_violations = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        vault_rows = list(connection.execute("SELECT value FROM settings WHERE key = 'vault_path'"))
        if len(vault_rows) != 1 or vault_rows[0][0] != str(expected_vault):
            raise UpgradeProbeError("fixture_invalid")
        if prior is None:
            prior = _freeze(connection, schema)
        else:
            _check_after(connection, prior)
        counts, legacy_digest = _legacy_identity(connection, prior)
    return {"schema": schema, "quick_check": quick_rows == [("ok",)],
            "foreign_key_violations": foreign_key_violations,
            "legacy_table_counts": counts, "legacy_digest": legacy_digest, "_prior": prior}


def _report_target(path: Path, formal_root: Path) -> Path:
    path = _absolute(path)
    if path.exists() or path.is_symlink():
        raise UpgradeProbeError("report_exists")
    parent = _reject_symlinks(path.parent)
    _mode(parent, 0o700, directory=True)
    if _related(parent, formal_root):
        raise UpgradeProbeError("fixture_invalid")
    return path


def _write_report(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _failure(code: str) -> dict:
    return {"schema_version": 1, "ok": False, "error_code": code}


def _no_sidecars(database):
    for suffix in ('-wal', '-shm'):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise UpgradeProbeError('fixture_invalid')


def _try_write_report(path: Path, value: dict) -> bool:
    try:
        _write_report(path, value)
    except Exception:
        return False
    return True


def run(data_root: Path | str, report: Path | str, *, formal_root: Path | str) -> int:
    try:
        formal = _absolute(formal_root)
        report_path = _report_target(Path(report), formal)
    except Exception:
        return 1
    try:
        if SCHEMA_VERSION != _APPROVED_TARGET_SCHEMA:
            raise UpgradeProbeError('unsupported_schema')
        root = _reject_symlinks(Path(data_root))
        _mode(root, 0o700, directory=True)
        if _related(root, formal):
            raise UpgradeProbeError("fixture_invalid")
        if root == report_path.parent or root in report_path.parents:
            return 1
        marker = _read_marker(root / MARKER_NAME)
        database = root / DATABASE_NAME
        database_stat = _mode(database, 0o600, directory=False)
        if database_stat.st_nlink != 1:
            raise UpgradeProbeError("fixture_invalid")
        _no_sidecars(database)
        vault = root / VAULT_RELPATH
        _mode(vault, 0o700, directory=True)
        vault_before = _digest_tree(vault)
        if vault_before != marker["vault_tree_sha256"]:
            raise UpgradeProbeError("fixture_invalid")
        database_before = _digest_file(database)
        if database_before != marker["database_before_sha256"]:
            raise UpgradeProbeError("database_changed")
        lock_path = root / ".instance.lock"
        try:
            lock_descriptor = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise UpgradeProbeError("fixture_invalid") from None
        try:
            if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
                raise UpgradeProbeError("fixture_invalid")
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpgradeProbeError("fixture_busy") from None
            current_stat = database.lstat()
            if ((current_stat.st_dev, current_stat.st_ino)
                    != (database_stat.st_dev, database_stat.st_ino)
                    or _digest_file(database) != database_before):
                raise UpgradeProbeError("database_changed")
            _no_sidecars(database)
            try:
                before = _snapshot(database, vault)
            except UpgradeProbeError:
                raise
            except (OSError, sqlite3.Error):
                raise UpgradeProbeError("precheck_failed") from None
            if (before["schema"] != marker["expected_schema"]
                    or not before["quick_check"]
                    or before["foreign_key_violations"]):
                raise UpgradeProbeError(
                    "unsupported_schema" if before["schema"] != marker["expected_schema"]
                    else "precheck_failed")
            try:
                initialize(database)
            except Exception:
                raise UpgradeProbeError("migration_failed") from None
            current_stat = database.lstat()
            if (current_stat.st_dev, current_stat.st_ino) != (database_stat.st_dev, database_stat.st_ino):
                raise UpgradeProbeError("postcheck_failed")
            try:
                _no_sidecars(database)
                _mode(database, 0o600, directory=False)
                if database.stat().st_nlink != 1:
                    raise UpgradeProbeError('postcheck_failed')
                after = _snapshot(database, vault, prior=before['_prior'])
            except UpgradeProbeError as error:
                if error.code == 'legacy_changed':
                    raise
                raise UpgradeProbeError('postcheck_failed') from None
            except (OSError, sqlite3.Error):
                raise UpgradeProbeError("postcheck_failed") from None
            try:
                vault_after = _digest_tree(vault)
            except (OSError, UpgradeProbeError):
                raise UpgradeProbeError('postcheck_failed') from None
            if (after["schema"] != SCHEMA_VERSION or not after["quick_check"]
                    or after["foreign_key_violations"]):
                raise UpgradeProbeError("postcheck_failed")
            unchanged = (before["legacy_table_counts"] == after["legacy_table_counts"]
                         and before["legacy_digest"] == after["legacy_digest"])
            if not unchanged or vault_after != vault_before:
                raise UpgradeProbeError("legacy_changed")
            result = {
                "schema_version": 1,
                "ok": True,
                "error_code": None,
                "database": {
                    "schema_before": before["schema"],
                    "schema_after": after["schema"],
                    "quick_check_before": before["quick_check"],
                    "quick_check_after": after["quick_check"],
                    "foreign_key_violations_before": before["foreign_key_violations"],
                    "foreign_key_violations_after": after["foreign_key_violations"],
                    "sha256_before": database_before,
                    "sha256_after": _digest_file(database),
                },
                "legacy": {
                    "identity_contract": IDENTITY_CONTRACT,
                    "table_counts_before": before["legacy_table_counts"],
                    "table_counts_after": after["legacy_table_counts"],
                    "digest_before": before["legacy_digest"],
                    "digest_after": after["legacy_digest"],
                    "unchanged": True,
                },
                "vault": {
                    "sha256_before": vault_before,
                    "sha256_after": vault_after,
                    "unchanged": True,
                },
            }
        finally:
            os.close(lock_descriptor)
    except UpgradeProbeError as error:
        _try_write_report(report_path, _failure(error.code))
        return 1
    except Exception:
        _try_write_report(report_path, _failure("internal_error"))
        return 1
    return 0 if _try_write_report(report_path, result) else 1
