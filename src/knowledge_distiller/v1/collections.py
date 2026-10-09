"""Confirmed collection work on the existing item pipeline and single worker."""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from .database import connect
from .douyin_collections import CollectionError, connection_authority


logger = logging.getLogger(__name__)


class CollectionDiscovery:
    """Route only the two implemented native-range readers."""
    def __init__(self, douyin):
        self.douyin = douyin

    def discover(self, urls, selected=None):
        from .intake import platform_for_url
        if urls and all(platform_for_url(url) == 'bilibili' for url in urls):
            from .bilibili import BilibiliDiscovery
            return BilibiliDiscovery().discover(urls, selected=selected)
        return self.douyin.discover(urls, selected=selected)

def _now():
    return datetime.now(UTC).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _event(db, operation, kind, detail):
    db.execute('INSERT INTO collection_events(operation_id,kind,detail_json,created_at) VALUES (?,?,?,?)',
               (operation, kind, _json(detail), _now()))


class PreviewChanged(CollectionError):
    def __init__(self, preview):
        super().__init__('collection_scope_changed')
        self.preview = preview


class PartialConfirmation(CollectionError):
    def __init__(self, operations, expected):
        super().__init__("collection_confirmation_partial")
        self.operations = operations
        self.expected = expected


class Collections:
    def __init__(self, store, discovery=None):
        self.store = store
        self.discovery = discovery

    def preview(self, urls, selected=None, *, submitted_text=None, durable=False):
        if self.discovery is None:
            raise CollectionError('collection_runtime_unavailable')
        result = self.discovery.discover(urls, selected=selected)
        return self._save_preview(result, urls, selected, submitted_text=submitted_text, durable=durable)

    def preview_same_topic(self, urls, *, submitted_text=None, durable=False):
        from .same_topic_discovery import discover
        result = discover(self.store, self.discovery, urls)
        return self._save_preview(result, urls, None, submitted_text=submitted_text, durable=durable, explicit_same_topic=True)

    def _save_preview(self, result, urls, selected, *, submitted_text=None, durable=False, explicit_same_topic=False):
        token = uuid4().hex
        from dataclasses import replace
        from .same_topic_discovery import CAPTURE_ON_PROCESSING
        result = {**result, 'scopes': [replace(s, capture_nonce=token)
                  if any(m.version == CAPTURE_ON_PROCESSING for m in s.members) else s for s in result['scopes']]}
        preview = {**result, 'token': token, 'urls': list(urls), 'selected': selected,
                   'explicit_same_topic': explicit_same_topic,
                   'submitted_text': submitted_text if submitted_text is not None else '\n'.join(urls)}
        saved = {**preview, 'scopes': [s.to_dict() for s in preview['scopes']], 'durable': durable}
        with connect(self.store.path) as db:
            db.execute('DELETE FROM collection_previews WHERE expires_at<=?', (time.time(),))
            db.execute('INSERT INTO collection_previews VALUES (?,?,?)',
                       (token, _json(saved), None if durable else time.time() + 1800))
        return preview

    def _draft(self, token):
        with connect(self.store.path) as db:
            draft = db.execute('SELECT * FROM collection_previews WHERE token=?', (token,)).fetchone()
        if draft is None or (draft['expires_at'] is not None and draft['expires_at'] <= time.time()):
            self.dismiss(token)
            raise CollectionError('collection_preview_expired')
        from .douyin_collections import Scope, Member
        from .bilibili import BilibiliMember
        preview = json.loads(draft['preview_json'])
        scopes = []
        for value in preview['scopes']:
            member_class = BilibiliMember if value['members'] and 'url' in value['members'][0] else Member
            scopes.append(Scope(**{key: value[key] for key in ('kind','key','title','creator_id','captured_at','authority')},
                                members=tuple(member_class(**m) for m in value['members']),
                                capture_nonce=value.get('capture_nonce', '')))
        preview['scopes'] = scopes
        return preview

    def select(self, token, selected):
        draft = self._draft(token)
        result = self.preview(draft['urls'], selected, submitted_text=draft['submitted_text'], durable=draft.get('durable', False))
        self._replace_preview(token,result['token'])
        return result

    def _replace_preview(self, old, new):
        # Keep the desktop and bound-message pointers on the same durable draft.
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE feishu_parts SET preview_json=? WHERE json_extract(preview_json,'$.token')=?",
                       (_json({'token':new}),old))
            db.execute('DELETE FROM collection_previews WHERE token=?',(old,))

    def dismiss(self, token):
        with connect(self.store.path) as db:
            db.execute('DELETE FROM collection_previews WHERE token=?', (token,))

    def confirm(self, token, signatures, *, same_topic=False):
        with connect(self.store.path) as db:
            accepted = db.execute('SELECT operation_id,expected_count FROM collection_confirmations WHERE token=? ORDER BY ordinal',(token,)).fetchall()
        if accepted:
            operations = [row[0] for row in accepted]
            if len(accepted) != accepted[0]['expected_count']:
                raise PartialConfirmation(operations, accepted[0]['expected_count'])
            return operations
        draft = self._draft(token)
        scopes = draft['scopes']
        if any(s.kind=='same_topic' for s in scopes) and not same_topic:
            raise CollectionError('collection_topic_confirmation_required')
        expected = [s.signature for s in scopes]
        if not scopes or signatures != expected:
            raise CollectionError('collection_confirmation_mismatch')
        if draft.get('explicit_same_topic'):
            from .same_topic_discovery import discover
            fresh = discover(self.store, self.discovery, draft['urls'])
            from dataclasses import replace
            fresh['scopes'] = [replace(s, capture_nonce=scopes[0].capture_nonce) for s in fresh['scopes']]
        else:
            fresh = self.discovery.discover(draft['urls'], selected=draft['selected'])
        identity = lambda rows: [(s.kind, s.key, s.signature, s.content_signature, s.authority) for s in rows]
        if identity(fresh['scopes']) != identity(scopes):
            replacement = (self.preview_same_topic(draft['urls'], submitted_text=draft['submitted_text'], durable=draft.get('durable', False))
                           if draft.get('explicit_same_topic') else
                           self.preview(draft['urls'], draft['selected'], submitted_text=draft['submitted_text'], durable=draft.get('durable', False)))
            self._replace_preview(token,replacement['token'])
            raise PreviewChanged(replacement)
        operations = []
        try:
            for n, scope in enumerate(scopes):
                operations.append(self._accept(scope, token + ':' + str(n), len(scopes)))
        except Exception:
            if operations:
                raise PartialConfirmation(operations, len(scopes)) from None
            raise
        self.dismiss(token)
        # The durable confirmation bindings handle duplicate POSTs after deletion.
        return operations

    def _accept(self, scope, token, expected_count=1):
        if scope.kind=='same_topic' and len(scope.members)<2:
            raise CollectionError('collection_input_unsupported')
        if not scope.members or not any(m.supported for m in scope.members):
            raise CollectionError('collection_no_supported_members')
        now = _now()
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            platform = scope.authority.get('platform', 'douyin')
            from .same_topic_discovery import authority_for, CAPTURE_ON_PROCESSING
            current_authority = authority_for(self.store, platform)
            if current_authority != scope.authority:
                raise CollectionError('collection_connection_changed')
            old = db.execute('''SELECT operation_id FROM collection_operations
                WHERE kind=? AND source_key=? AND signature=? AND content_signature=?''',
                (scope.kind, scope.key, scope.signature, scope.content_signature)).fetchone()
            base_token, _, confirmation_ordinal = token.rpartition(':')
            if old is not None and not any(m.version == CAPTURE_ON_PROCESSING for m in scope.members):
                db.execute('INSERT OR IGNORE INTO collection_confirmations VALUES (?,?,?,?)',(base_token,int(confirmation_ordinal or 0),expected_count,old['operation_id']))
                return old['operation_id']
            operation = db.execute('''INSERT INTO collection_operations
                (kind,source_key,title,manifest_json,signature,content_signature,authority_json,confirmation_token,
                 state,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?)''',
                (scope.kind, scope.key, scope.title, _json(scope.to_dict()), scope.signature, scope.content_signature,
                 _json(scope.authority), token, now, now, now)).lastrowid
            for ordinal, member in enumerate(scope.members):
                item = db.execute('''INSERT INTO distill_items
                    (submitted_url,state,phase,error_code,queued_at,created_at,updated_at,platform_authority_json,submitted_title)
                    VALUES (?,?,'collecting',?,?,?,?,?,?)''',
                    (getattr(member, 'url', '') or 'https://www.douyin.com/video/' + member.item_id,
                     'queued' if member.supported else 'failed', None if member.supported else platform + '_input_unsupported',
                     now, now, now, _json({k:v for k,v in scope.authority.items() if k != 'platform'}), member.title)).lastrowid
                if member.supported:
                    stable = db.execute("""SELECT m.material_id FROM materials m
                        JOIN source_facts sf USING(material_id) JOIN knowledge_results kr USING(source_fact_id)
                        WHERE m.source_kind=? AND m.source_key=? AND m.snapshot_key!='legacy'
                        AND json_extract(m.metadata_json,'$.native_content_version')=?
                        ORDER BY m.material_id DESC LIMIT 1""", (platform,member.item_id,member.version)).fetchone()
                    if stable is not None:
                        db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?',(stable['material_id'],item))
                db.execute('''INSERT INTO collection_members
                    (operation_id,ordinal,native_id,native_version,item_id,known_unsupported) VALUES (?,?,?,?,?,?)''',
                    (operation, ordinal, member.item_id, member.version, item, int(not member.supported)))
            db.execute('INSERT INTO collection_confirmations VALUES (?,?,?,?)',(base_token,int(confirmation_ordinal or 0),expected_count,operation))
            _event(db, operation, 'confirmed', {'signature': scope.signature, 'count': len(scope.members)})
            return operation

    def detail(self, operation):
        with connect(self.store.path) as db:
            row = db.execute('SELECT * FROM collection_operations WHERE operation_id=?', (operation,)).fetchone()
            if row is None:
                raise LookupError('collection not found')
            members = db.execute('''SELECT cm.*,i.state,i.phase,i.error_code,i.confirmation_json
                FROM collection_members cm JOIN distill_items i USING(item_id)
                WHERE cm.operation_id=? ORDER BY ordinal''', (operation,)).fetchall()
            result = db.execute('SELECT * FROM collection_results WHERE operation_id=?', (operation,)).fetchone()
        return {**dict(row), 'manifest': json.loads(row['manifest_json']),
                'members': [dict(m) for m in members],
                'result': json.loads(result['payload_json']) if result else None}

    def list(self):
        with connect(self.store.path) as db:
            ids = [r[0] for r in db.execute('SELECT operation_id FROM collection_operations ORDER BY created_at DESC,operation_id DESC')]
        return [self.detail(i) for i in ids]

    def cancel(self, operation, revision):
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM collection_operations WHERE operation_id=?', (operation,)).fetchone()
            if row is None or row['revision'] != revision or row['state'] == 'succeeded':
                raise CollectionError('collection_command_stale')
            if row['state'] == 'cancelled':
                return
            state = 'working' if row['state'] == 'working' else 'cancelled'
            db.execute('''UPDATE collection_operations SET cancel_requested=1,state=?,revision=revision+1,updated_at=?
                WHERE operation_id=?''', (state, _now(), operation))
            _event(db, operation, 'cancel_requested', {'revision': revision})

    def resume(self, operation, revision):
        # Only a user command queues retries; ordinary duplicate submissions do not.
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM collection_operations WHERE operation_id=?', (operation,)).fetchone()
            if row is None or row['revision'] != revision or row['state'] not in {'cancelled','partial','failed'}:
                raise CollectionError('collection_command_stale')
            failures = [dict(r) for r in db.execute('''SELECT i.item_id,i.error_code FROM distill_items i
                JOIN collection_members cm USING(item_id) WHERE cm.operation_id=? AND i.state='failed'
                AND cm.known_unsupported=0 AND i.confirmation_json IS NULL
                AND i.error_code NOT IN ('collection_member_changed','douyin_input_unsupported','source_snapshot_changed')''', (operation,))]
            pending = db.execute("""SELECT 1 FROM collection_members cm JOIN distill_items i USING(item_id)
                WHERE cm.operation_id=? AND i.state='queued' LIMIT 1""",(operation,)).fetchone()
            if not failures and not pending and row['consequence'] != 'complete':
                raise CollectionError('collection_nothing_to_retry')
            now = _now()
            for failure in failures:
                db.execute("UPDATE distill_items SET state='queued',error_code=NULL,queued_at=?,updated_at=? WHERE item_id=?",
                           (now, now, failure['item_id']))
            db.execute('''UPDATE collection_operations SET state='queued',cancel_requested=0,error_code=NULL,
                revision=revision+1,queued_at=?,updated_at=? WHERE operation_id=?''', (now, now, operation))
            _event(db, operation, 'resume', {'failures': failures, 'revision': revision})

    def run(self, operation, distiller):
        info = self.detail(operation)
        if info['cancel_requested']:
            self._reconcile(operation)
            return
        member = next((m for m in info['members'] if m['state'] == 'queued'), None)
        if member is not None:
            item = member['item_id']
            self.store.mark_working(item, 'collecting')
            try:
                distiller.run(item)
            except Exception as error:
                logger.error("Collection item %s failed (%s)",item,type(error).__name__)
                row = self.store.item_bundle(item)
                if row['state'] == 'working':
                    self.store.mark_failed(item, row['phase'], 'processing_unexpected_failure')
            self._reconcile(operation, item)
            return
        state = self._reconcile(operation, combining=True)
        if state == 'ready_combined':
            try:
                from .collection_model import derive_combined, validate_combined
                basis = self._basis(operation)
                payload = derive_combined(distiller.knowledge_model, basis)
                payload = validate_combined(payload, basis)
                with connect(self.store.path) as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT * FROM collection_operations WHERE operation_id=?', (operation,)).fetchone()
                    if row['cancel_requested']:
                        db.execute("UPDATE collection_operations SET state='cancelled',updated_at=? WHERE operation_id=?", (_now(),operation))
                        return
                    db.execute('INSERT INTO collection_results(operation_id,payload_json,lineage_json,created_at) VALUES (?,?,?,?)',
                               (operation, _json(payload), _json(basis), _now()))
                    db.execute("UPDATE collection_operations SET state='succeeded',error_code=NULL,updated_at=? WHERE operation_id=?", (_now(),operation))
                    _event(db, operation, 'combined_created', {})
            except Exception as error:
                logger.error("Collection combined %s failed (%s)",operation,type(error).__name__)
                code = str(error) if isinstance(error, CollectionError) else 'collection_combined_failed'
                with connect(self.store.path) as db:
                    db.execute("UPDATE collection_operations SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END,error_code=?,updated_at=? WHERE operation_id=?", (code,_now(),operation))
                    _event(db, operation, 'combined_failed', {'code': code})

    def _reconcile(self, operation, completed_item=None, *, combining=False):
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT cm.*,i.state,i.error_code,i.confirmation_json,sf.source_fact_id AS sf,kr.knowledge_result_id AS kr
                FROM collection_members cm JOIN distill_items i USING(item_id)
                LEFT JOIN source_facts sf ON sf.material_id=i.material_id
                LEFT JOIN knowledge_results kr ON kr.source_fact_id=sf.source_fact_id
                WHERE cm.operation_id=? ORDER BY ordinal''', (operation,)).fetchall()
            for row in rows:
                db.execute('UPDATE collection_members SET source_fact_id=?,knowledge_result_id=? WHERE item_id=?',
                           (row['sf'],row['kr'],row['item_id']))
            op = db.execute('SELECT * FROM collection_operations WHERE operation_id=?', (operation,)).fetchone()
            complete = all(r['state'] == 'succeeded' and r['kr'] is not None for r in rows)
            consequence = 'complete' if complete else None
            if op['cancel_requested']:
                state = 'cancelled'
            elif any(r['state'] in {'queued','working'} for r in rows):
                state = 'queued'
            elif any(r['state'] == 'waiting_user' or r['confirmation_json'] for r in rows):
                state = 'waiting_user'
            elif all(r['state'] == 'succeeded' and r['kr'] is not None for r in rows):
                state = 'succeeded' if op['kind'] in {'bilibili_range','same_topic'} else 'ready_combined'
                consequence = 'complete'
            elif any(r['state'] == 'succeeded' for r in rows):
                state = 'partial';consequence = 'partial'
            else:
                state = 'failed';consequence = 'failed'
            db.execute('UPDATE collection_operations SET state=?,consequence=?,updated_at=? WHERE operation_id=?',
                       (('working' if combining else 'queued') if state=='ready_combined' else state,consequence,_now(),operation))
            _event(db, operation, 'member_boundary', {'state':state,'members':[{'item':r['item_id'],'state':r['state'],'error':r['error_code']} for r in rows if r['item_id']==completed_item]})
            return state

    def _basis(self, operation):
        info = self.detail(operation)
        if info['consequence'] != 'complete':
            raise CollectionError('collection_not_complete')
        basis = []
        for member in info['members']:
            row = self.store.item_bundle(member['item_id'])
            if row['state'] != 'succeeded' or row['knowledge_result_id'] != member['knowledge_result_id']:
                raise CollectionError('collection_binding_invalid')
            basis.append({'native_id': member['native_id'], 'source_fact_id': row['source_fact_id'],
                          'knowledge_result_id': row['knowledge_result_id'], 'knowledge': json.loads(row['payload_json']),
                          'source_url': row['canonical_url'], 'uncertainties': json.loads(row['uncertainties_json'])})
        return basis
