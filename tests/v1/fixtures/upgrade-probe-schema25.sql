-- CANDIDATE: genuine schema23 -> 24 -> 25 from fixed 58bc8ee primary AST.
-- Apply only after wiki-schema21.sql, wiki-schema22.sql, upgrade-probe-schema23.sql.
-- No target26 downgrade, probe expected or business execution generated this fixture.
-- 58bc8ee:database.py SHA256=ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea
-- 58bc8ee:media_lifecycle.py SHA256=b4de2b4473935728c5e09844f9fe1c7e1f811ff04ba2705a6ea404d7c97f297b
-- 58bc8ee:wiki_schema.py SHA256=1270e9b700079db8894510af3826ceb567b441bf3f3c3c0e322908c7ad7b1163
-- fixture:upgrade-probe-schema23.sql SHA256=8032555ed073a4f1c1e712fdb3be7b5c4d5789f4be1843fe6af8a2736aca4630
-- fixture:wiki-schema21.sql SHA256=f4d536c3d36531d1a039f2e6714a96be83bc43c5f74c2409007b41f68aeaaf8d
-- fixture:wiki-schema22.sql SHA256=8facd87b1e7dd679e2a584875b2c606c09b51505439629fec6dfa5e98fd42090
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
ALTER TABLE distill_items ADD COLUMN ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1'));
ALTER TABLE distill_items ADD COLUMN source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE distill_items ADD COLUMN relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE collection_operations ADD COLUMN ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1'));
ALTER TABLE collection_operations ADD COLUMN source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE collection_operations ADD COLUMN relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE wiki_tasks ADD COLUMN outcome_contract TEXT NOT NULL DEFAULT 'legacy' CHECK(outcome_contract IN ('legacy','r08-wiki-outcomes-v1'));
ALTER TABLE wiki_tasks ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(plan_json) AND json_type(plan_json)='object');
ALTER TABLE wiki_tasks ADD COLUMN plan_sha256 TEXT CHECK(plan_sha256 IS NULL OR (length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'));
CREATE TRIGGER distill_items_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON distill_items
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END;
CREATE TRIGGER distill_items_ingestion_binding_required
            BEFORE INSERT ON distill_items WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END;
CREATE TRIGGER collection_operations_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON collection_operations
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END;
CREATE TRIGGER collection_operations_ingestion_binding_required
            BEFORE INSERT ON collection_operations WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END;
CREATE TRIGGER distill_items_ingestion_owner_immutable
        BEFORE UPDATE OF material_id ON distill_items
        WHEN OLD.ingestion_contract!='legacy' AND OLD.material_id IS NOT NULL
          AND NEW.material_id IS NOT OLD.material_id
        BEGIN SELECT RAISE(ABORT,'ingestion owner is immutable'); END;
CREATE TRIGGER distill_items_ingestion_no_delete
        BEFORE DELETE ON distill_items WHEN OLD.ingestion_contract!='legacy'
        BEGIN SELECT RAISE(ABORT,'ingestion owner is durable'); END;
CREATE TRIGGER collection_members_ingestion_contract_match
        BEFORE INSERT ON collection_members
        WHEN (SELECT ingestion_contract FROM collection_operations WHERE operation_id=NEW.operation_id)
          IS NOT (SELECT ingestion_contract FROM distill_items WHERE item_id=NEW.item_id)
        BEGIN SELECT RAISE(ABORT,'collection ingestion contract mismatch'); END;
CREATE TABLE ingestion_events (
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
    );
CREATE INDEX ingestion_events_subject ON ingestion_events(subject_kind,subject_id,event_id);
CREATE TRIGGER ingestion_events_observation_typed
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
        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END;
CREATE TRIGGER ingestion_events_no_update
            BEFORE UPDATE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END;
CREATE TRIGGER ingestion_events_no_delete
            BEFORE DELETE ON ingestion_events
            BEGIN SELECT RAISE(ABORT,'ingestion event is immutable'); END;
CREATE TRIGGER ingestion_events_proof_unavailable
        BEFORE INSERT ON ingestion_events
        WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')
          AND (ingestion_proof(NEW.kind,NEW.binding_sha256,NEW.detail_json)!=1
               OR NEW.kind IS NOT json_extract(NEW.detail_json,'$.code')
               OR NEW.subject_kind IS NOT json_extract(NEW.detail_json,'$.manifest.subject_kind')
               OR NEW.subject_id IS NOT json_extract(NEW.detail_json,'$.manifest.subject_id')
               OR NEW.item_id IS NOT json_extract(NEW.detail_json,'$.manifest.owner_item_id')
               OR NEW.binding_sha256 IS NOT json_extract(NEW.detail_json,'$.final_binding_sha256'))
        BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END;
CREATE TRIGGER wiki_task_outcome_binding_immutable
        BEFORE UPDATE OF outcome_contract,plan_json,plan_sha256 ON wiki_tasks
        WHEN NEW.outcome_contract IS NOT OLD.outcome_contract OR NEW.plan_json IS NOT OLD.plan_json
          OR NEW.plan_sha256 IS NOT OLD.plan_sha256
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding is immutable'); END;
CREATE TRIGGER wiki_task_outcome_binding_required
        BEFORE INSERT ON wiki_tasks WHEN NEW.outcome_contract!='legacy' AND NEW.plan_sha256 IS NULL
        BEGIN SELECT RAISE(ABORT,'wiki outcome binding required'); END;
CREATE TABLE wiki_outcome_receipts (
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
    );
CREATE UNIQUE INDEX wiki_outcome_one_accepted_batch
        ON wiki_outcome_receipts(task_id,batch_no) WHERE phase='accepted';
CREATE TRIGGER wiki_outcome_receipts_no_update
            BEFORE UPDATE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END;
CREATE TRIGGER wiki_outcome_receipts_no_delete
            BEFORE DELETE ON wiki_outcome_receipts
            BEGIN SELECT RAISE(ABORT,'wiki outcome receipt is immutable'); END;
CREATE TRIGGER wiki_outcome_receipts_publish_unavailable
        BEFORE INSERT ON wiki_outcome_receipts WHEN NEW.phase='accepted'
        BEGIN SELECT RAISE(ABORT,'publish proof unavailable'); END;
CREATE TRIGGER source_media_ingestion_no_update
            BEFORE UPDATE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
              AND ingestion_release(OLD.material_id,OLD.member_id,OLD.sha256,NEW.content)!=1
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END;
CREATE TRIGGER source_media_ingestion_no_delete
            BEFORE DELETE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
              
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_no_release
        BEFORE UPDATE OF content,input_metadata ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
          AND (NEW.content IS NOT OLD.content OR NEW.input_metadata IS NOT OLD.input_metadata)
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_no_delete
        BEFORE DELETE ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_owner_immutable
        BEFORE UPDATE OF item_id ON submitted_sources
        WHEN NEW.item_id IS NOT OLD.item_id AND EXISTS (
            SELECT 1 FROM distill_items i WHERE i.item_id IN (OLD.item_id,NEW.item_id)
            AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input owner is immutable'); END;
CREATE TRIGGER source_media_ingestion_capture_binding
        BEFORE UPDATE OF item_id,audio_path ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND (NEW.item_id IS NOT OLD.item_id
            OR (OLD.audio_path IS NOT NULL AND NEW.audio_path IS NOT OLD.audio_path))
        BEGIN SELECT RAISE(ABORT,'ingestion capture binding is immutable'); END;
CREATE TRIGGER source_media_ingestion_capture_release
        BEFORE UPDATE OF audio_released_at ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND NEW.audio_released_at IS NOT OLD.audio_released_at
          AND ingestion_release('capture',OLD.capture_id,OLD.audio_path,NEW.audio_released_at)!=1
        BEGIN SELECT RAISE(ABORT,'ingestion audio is retained'); END;
CREATE TRIGGER source_media_ingestion_capture_no_delete
        BEFORE DELETE ON capture_state WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1'))
        BEGIN SELECT RAISE(ABORT,'ingestion capture owner is durable'); END;
PRAGMA user_version=24;
CREATE TABLE distill_items_v25 (
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
, platform_authority_json TEXT NOT NULL DEFAULT '{}', submitted_title TEXT NOT NULL DEFAULT '', review_revision INTEGER NOT NULL DEFAULT 0, ingestion_contract TEXT NOT NULL DEFAULT 'legacy' CHECK (ingestion_contract IN ('legacy','raw-verified-v1')), source_binding_sha256 TEXT CHECK (source_binding_sha256 IS NULL OR (length(source_binding_sha256)=64 AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*')), relation_binding_sha256 TEXT CHECK (relation_binding_sha256 IS NULL OR (length(relation_binding_sha256)=64 AND relation_binding_sha256 NOT GLOB '*[^0-9a-f]*')), CHECK(state!='raw_saved' OR (phase='done' AND ingestion_contract='raw-verified-v1')));
INSERT INTO distill_items_v25("item_id","submitted_url","state","phase","material_id","error_code","rejection_reason","dismissed_at","confirmation_json","queued_at","created_at","updated_at","platform_authority_json","submitted_title","review_revision","ingestion_contract","source_binding_sha256","relation_binding_sha256") SELECT "item_id","submitted_url","state","phase","material_id","error_code","rejection_reason","dismissed_at","confirmation_json","queued_at","created_at","updated_at","platform_authority_json","submitted_title","review_revision","ingestion_contract","source_binding_sha256","relation_binding_sha256" FROM distill_items;
DROP TRIGGER "collection_members_ingestion_contract_match";
DROP TRIGGER "distill_items_ingestion_binding_immutable";
DROP TRIGGER "distill_items_ingestion_binding_required";
DROP TRIGGER "distill_items_ingestion_no_delete";
DROP TRIGGER "distill_items_ingestion_owner_immutable";
DROP TRIGGER "distill_review_revision";
DROP TRIGGER "ingestion_events_observation_typed";
DROP TRIGGER "source_media_ingestion_capture_binding";
DROP TRIGGER "source_media_ingestion_capture_no_delete";
DROP TRIGGER "source_media_ingestion_capture_release";
DROP TRIGGER "source_media_ingestion_no_delete";
DROP TRIGGER "source_media_ingestion_no_update";
DROP TRIGGER "source_media_no_update";
DROP TRIGGER "submitted_sources_ingestion_no_delete";
DROP TRIGGER "submitted_sources_ingestion_no_release";
DROP TRIGGER "submitted_sources_ingestion_owner_immutable";
DROP TABLE distill_items;
ALTER TABLE distill_items_v25 RENAME TO distill_items;
CREATE TRIGGER collection_members_ingestion_contract_match
        BEFORE INSERT ON collection_members
        WHEN (SELECT ingestion_contract FROM collection_operations WHERE operation_id=NEW.operation_id)
          IS NOT (SELECT ingestion_contract FROM distill_items WHERE item_id=NEW.item_id)
        BEGIN SELECT RAISE(ABORT,'collection ingestion contract mismatch'); END;
CREATE TRIGGER distill_items_ingestion_binding_immutable
            BEFORE UPDATE OF ingestion_contract,source_binding_sha256,relation_binding_sha256 ON distill_items
            WHEN NEW.ingestion_contract IS NOT OLD.ingestion_contract
              OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
              OR NEW.relation_binding_sha256 IS NOT OLD.relation_binding_sha256
            BEGIN SELECT RAISE(ABORT,'ingestion binding is immutable'); END;
CREATE TRIGGER distill_items_ingestion_binding_required
            BEFORE INSERT ON distill_items WHEN NEW.ingestion_contract!='legacy'
              AND (NEW.source_binding_sha256 IS NULL OR NEW.relation_binding_sha256 IS NULL)
            BEGIN SELECT RAISE(ABORT,'ingestion binding required'); END;
CREATE TRIGGER distill_items_ingestion_no_delete
        BEFORE DELETE ON distill_items WHEN OLD.ingestion_contract!='legacy'
        BEGIN SELECT RAISE(ABORT,'ingestion owner is durable'); END;
CREATE TRIGGER distill_items_ingestion_owner_immutable
        BEFORE UPDATE OF material_id ON distill_items
        WHEN OLD.ingestion_contract!='legacy' AND OLD.material_id IS NOT NULL
          AND NEW.material_id IS NOT OLD.material_id
        BEGIN SELECT RAISE(ABORT,'ingestion owner is immutable'); END;
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
CREATE TRIGGER ingestion_events_observation_typed
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
        BEGIN SELECT RAISE(ABORT,'ingestion observation invalid'); END;
CREATE TRIGGER source_media_ingestion_capture_binding
        BEFORE UPDATE OF item_id,audio_path ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND (NEW.item_id IS NOT OLD.item_id
            OR (OLD.audio_path IS NOT NULL AND NEW.audio_path IS NOT OLD.audio_path))
        BEGIN SELECT RAISE(ABORT,'ingestion capture binding is immutable'); END;
CREATE TRIGGER source_media_ingestion_capture_no_delete
        BEFORE DELETE ON capture_state WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1'))
        BEGIN SELECT RAISE(ABORT,'ingestion capture owner is durable'); END;
CREATE TRIGGER source_media_ingestion_capture_release
        BEFORE UPDATE OF audio_released_at ON capture_state
        WHEN (EXISTS(SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
        AND i.ingestion_contract!='legacy') OR EXISTS(SELECT 1 FROM ingestion_events e
        WHERE e.subject_kind='capture' AND e.subject_id=OLD.capture_id AND e.contract='raw-verified-v1')) AND NEW.audio_released_at IS NOT OLD.audio_released_at
          AND ingestion_release('capture',OLD.capture_id,OLD.audio_path,NEW.audio_released_at)!=1
        BEGIN SELECT RAISE(ABORT,'ingestion audio is retained'); END;
CREATE TRIGGER source_media_ingestion_no_delete
            BEFORE DELETE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
              
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END;
CREATE TRIGGER source_media_ingestion_no_update
            BEFORE UPDATE ON source_media
            WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=OLD.material_id
                         AND i.ingestion_contract!='legacy')
              AND ingestion_release(OLD.material_id,OLD.member_id,OLD.sha256,NEW.content)!=1
            BEGIN SELECT RAISE(ABORT,'ingestion media is retained'); END;
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
CREATE TRIGGER submitted_sources_ingestion_no_delete
        BEFORE DELETE ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_no_release
        BEFORE UPDATE OF content,input_metadata ON submitted_sources
        WHEN EXISTS (SELECT 1 FROM distill_items i WHERE i.item_id=OLD.item_id
                     AND i.ingestion_contract!='legacy')
          AND (NEW.content IS NOT OLD.content OR NEW.input_metadata IS NOT OLD.input_metadata)
        BEGIN SELECT RAISE(ABORT,'ingestion input is retained'); END;
CREATE TRIGGER submitted_sources_ingestion_owner_immutable
        BEFORE UPDATE OF item_id ON submitted_sources
        WHEN NEW.item_id IS NOT OLD.item_id AND EXISTS (
            SELECT 1 FROM distill_items i WHERE i.item_id IN (OLD.item_id,NEW.item_id)
            AND i.ingestion_contract!='legacy')
        BEGIN SELECT RAISE(ABORT,'ingestion input owner is immutable'); END;
CREATE TRIGGER distill_items_raw_terminal_no_insert
        BEFORE INSERT ON distill_items WHEN NEW.state='raw_saved'
        BEGIN SELECT RAISE(ABORT,'raw terminal writer required'); END;
CREATE TRIGGER distill_items_raw_terminal_no_reopen
        BEFORE UPDATE OF state,phase ON distill_items WHEN OLD.state='raw_saved'
        AND (NEW.state IS NOT OLD.state OR NEW.phase IS NOT OLD.phase)
        BEGIN SELECT RAISE(ABORT,'raw terminal is durable'); END;
CREATE TRIGGER distill_items_raw_terminal_proof
        BEFORE UPDATE ON distill_items WHEN NEW.state='raw_saved' AND OLD.state!='raw_saved'
        AND (OLD.state!='working' OR OLD.phase NOT IN ('collecting','reviewing')
            OR NEW.phase!='done' OR NEW.ingestion_contract!='raw-verified-v1'
            OR NEW.confirmation_json IS NOT NULL OR NEW.dismissed_at IS NOT NULL
            OR NEW.material_id IS NULL OR NEW.source_binding_sha256 IS NULL
            OR NEW.relation_binding_sha256 IS NULL
            OR ingestion_raw_terminal(OLD.item_id,OLD.review_revision,NEW.state,NEW.phase)!=1)
        BEGIN SELECT RAISE(ABORT,'raw terminal proof unavailable'); END;
PRAGMA user_version=25;
COMMIT;
PRAGMA foreign_keys=ON;
