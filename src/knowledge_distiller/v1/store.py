from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .database import connect, initialize
from .confirmation_schema import sync as _sync_manual_cards
from .domain import CapturedMaterial, Knowledge, SourceFact, knowledge_to_dict
from .source_files import FILE_KINDS, copy_path, read_copy, retain_copy, open_copy, SourceCopyError


class SourceReviewConflict(ValueError):
    """A newer source/user state superseded this asynchronous judgment."""


class Store:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        initialize(self.path)
        self._retain_available_files()
        self.expire_submitted_sources()
        self.expire_platform_media()

    def expire_platform_media(self):
        cutoff = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute("""SELECT m.material_id FROM materials m
                WHERE m.source_kind IN ('douyin','youtube','xiaohongshu','x','zhihu','weibo','bilibili')
                AND julianday(COALESCE(json_extract(m.metadata_json,'$.captured_at'),m.created_at)) < julianday(?)
                AND NOT EXISTS (SELECT 1 FROM source_facts sf WHERE sf.material_id=m.material_id)
                AND NOT EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=m.material_id
                                AND (i.confirmation_json IS NOT NULL OR i.state='working'
                                     OR (i.dismissed_at IS NULL AND substr(COALESCE(i.error_code,''),-length('_input_unsupported')) != '_input_unsupported')))""",(cutoff,)).fetchall()
            for row in rows:
                connection.execute('DELETE FROM source_media WHERE material_id=?',(row[0],))
                connection.execute("UPDATE materials SET metadata_json=? WHERE material_id=?",(_json({"temporary_expired": True}),row[0]))

    def _retain_available_files(self) -> None:
        # Upgrade only still-owned bytes, never reconstruct a file from SourceFact.
        with connect(self.path) as connection:
            rows = connection.execute("SELECT * FROM submitted_sources WHERE input_kind IN ('markdown','pdf','epub') AND content IS NOT NULL").fetchall()
        for row in rows:
            if row['retain_until'] is not None and row['retain_until'] <= _now():
                continue
            target = copy_path(self.path.parent, row['input_kind'], row['input_key'], row['input_label'])
            if not target.exists():
                retain_copy(self.path.parent, row['input_kind'], row['input_key'], row['input_label'], row['content'])

    def open_source_file(self, item_id: int) -> None:
        with connect(self.path) as connection:
            row = connection.execute("SELECT * FROM submitted_sources WHERE item_id = ?", (item_id,)).fetchone()
        if row is None or row['input_kind'] not in FILE_KINDS:
            raise SourceCopyError('这条来源没有上传文件副本。')
        open_copy(self.path.parent, row['input_kind'], row['input_key'], row['input_label'])

    def submit_source(self, source, *, receipt_key=None) -> int:
        """Persist exact intake and FIFO item together; replay never refreshes it."""
        now = _now()
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            from .feishu_inbox import prior_item, bind_item
            prior = prior_item(connection, receipt_key)
            if prior is not None:
                return prior
            existing = connection.execute(
                "SELECT item_id, input_label FROM submitted_sources WHERE input_kind = ? AND input_key = ?",
                (source.source_kind, source.source_key),
            ).fetchone()
            if source.source_kind in FILE_KINDS:
                label = existing['input_label'] if existing is not None else source.label
                retain_copy(self.path.parent, source.source_kind, source.source_key, label, source.content)
            if existing is not None:
                bind_item(connection, receipt_key, int(existing['item_id']))
                return int(existing["item_id"])
            cursor = connection.execute(
                """INSERT INTO distill_items
                   (submitted_url, state, phase, queued_at, created_at, updated_at)
                   VALUES (?, 'queued', 'collecting', ?, ?, ?)""",
                (source.label, now, now, now),
            )
            item_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO submitted_sources
                   (item_id, input_kind, input_key, input_label, input_metadata, content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (item_id, source.source_kind, source.source_key, source.label,
                 _json(source.metadata), source.content),
            )
            bind_item(connection, receipt_key, item_id)
            return item_id

    def submitted_source(self, item_id: int):
        from .file_sources import SubmittedSource
        with connect(self.path) as connection:
            row = connection.execute("SELECT * FROM submitted_sources WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("原提交副本不可用，请重新提交相同内容后重试。")
        content = (read_copy(self.path.parent, row['input_kind'], row['input_key'], row['input_label'])
                   if row['input_kind'] in FILE_KINDS else row['content'])
        if content is None:
            raise ValueError("原提交副本不可用，请重新提交相同内容后重试。")
        return SubmittedSource(row["input_kind"], row["input_key"], row["input_label"],
                               content, json.loads(row["input_metadata"]))

    def establish_submitted_fact(self, item_id: int, source, parsed, *, expected_revision=None,
                                 review_result=None) -> int:
        """Commit full fact and locator before relinquishing the temporary input."""
        fact = SourceFact(parsed.snapshot, parsed.uncertainties)
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_revision is not None:
                row = connection.execute('SELECT * FROM distill_items WHERE item_id=?', (item_id,)).fetchone()
                if (row is None or row['review_revision'] != expected_revision
                        or row['state'] != 'working' or row['confirmation_json'] is not None
                        or row['material_id'] is not None):
                    raise SourceReviewConflict('source_review_revision_conflict')
                if not review_result or review_result.get('failure'):
                    raise ValueError('completed_review_required')
                connection.execute('INSERT INTO source_review_results VALUES (?,?,?,?,?,?)',
                    (item_id, expected_revision, source.source_key, 'complete', _json(review_result), _now()))
            cursor = connection.execute(
                """INSERT INTO materials
                   (source_kind, source_key, submitted_url, canonical_url, metadata_json, created_at)
                   VALUES (?, ?, ?, '', ?, ?)""",
                (source.source_kind, source.source_key, source.label, _json(parsed.metadata), _now()),
            )
            material_id = int(cursor.lastrowid)
            import hashlib
            for position, member in enumerate(parsed.media):
                connection.execute('INSERT INTO source_media VALUES (?, ?, ?, ?, ?, ?)',
                    (material_id, member.member_id, position, member.mime_type,
                     hashlib.sha256(member.content).hexdigest(), member.content))
            from .image_confirmation import pending_review
            pending=pending_review(fact,parsed.lineage)
            if pending:
                fact_id=None
                connection.execute("UPDATE distill_items SET state='waiting_user',phase='reviewing',confirmation_json=? WHERE item_id=?",
                                   (_confirmation_json(pending, connection, item_id),item_id))
            else:
                fact_id = _establish_source_fact(connection, material_id, fact, lineage=parsed.lineage)
            connection.execute("UPDATE distill_items SET material_id = ? WHERE item_id = ?", (material_id, item_id))
            connection.execute("UPDATE submitted_sources SET content = NULL, input_metadata = '{}', retain_until = NULL WHERE item_id = ?", (item_id,))
            _sync_manual_cards(connection, item_id)
            return fact_id

    def expire_submitted_sources(self) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """UPDATE submitted_sources SET content = NULL, input_metadata = '{}'
                   WHERE retain_until IS NOT NULL AND retain_until <= ?""", (_now(),)
            )

    def reject_submitted_source(self, item_id: int, code: str) -> None:
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE distill_items SET state = 'failed', error_code = ?, updated_at = ? WHERE item_id = ?", (code, _now(), item_id))
            connection.execute("UPDATE submitted_sources SET retryable = 0, content = NULL, input_metadata = '{}', retain_until = NULL WHERE item_id = ?", (item_id,))

    def create_item(self, submitted_url: str, *, title: str = '', expected_authority=None, receipt_key=None) -> int:
        submitted_url = submitted_url.strip()
        if not submitted_url:
            raise ValueError("submission is empty")
        now = _now()
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            from .feishu_inbox import prior_item, bind_item
            prior = prior_item(connection, receipt_key)
            if prior is not None:
                return prior
            authority = {}
            from .youtube import youtube_identity, connection_authority
            try:
                youtube_identity(submitted_url)
            except ValueError:
                pass
            else:
                authority = connection_authority(connection.execute(
                    "SELECT * FROM source_connections WHERE platform = 'youtube'"
                ).fetchone())
            from .xiaohongshu import xiaohongshu_input, connection_authority as xhs_authority
            try:
                xiaohongshu_input(submitted_url)
            except ValueError:
                pass
            else:
                authority = xhs_authority(connection.execute(
                    "SELECT * FROM source_connections WHERE platform = 'xiaohongshu'"
                ).fetchone())
            from .xpost import xpost_identity, connection_authority as x_authority
            try:
                xpost_identity(submitted_url)
            except ValueError:
                pass
            else:
                authority = x_authority(connection.execute(
                    "SELECT * FROM source_connections WHERE platform = 'x'"
                ).fetchone())
            from .zhihu import zhihu_identity, connection_authority as zhihu_authority
            try:
                zhihu_identity(submitted_url)
            except ValueError:
                pass
            else:
                authority = zhihu_authority(connection.execute(
                    "SELECT * FROM source_connections WHERE platform = 'zhihu'"
                ).fetchone())
            from .weibo import weibo_identity, connection_authority as weibo_authority
            try:
                weibo_identity(submitted_url)
            except ValueError:
                pass
            else:
                authority = weibo_authority(connection.execute(
                    "SELECT * FROM source_connections WHERE platform = 'weibo'"
                ).fetchone())
            if expected_authority is not None and authority != expected_authority:
                raise ValueError('来源连接已变化，请重新读取链接。')
            cursor = connection.execute(
                """
                INSERT INTO distill_items (
                    submitted_url, state, phase, queued_at, created_at, updated_at, platform_authority_json, submitted_title
                ) VALUES (?, 'queued', 'collecting', ?, ?, ?, ?, ?)
                """,
                (submitted_url, now, now, now, _json(authority), title),
            )
            item_id = int(cursor.lastrowid)
            bind_item(connection, receipt_key, item_id)
            return item_id

    def claim_next_work(self):
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM (
                SELECT 'item' AS kind,item_id AS id,queued_at FROM distill_items i WHERE state='queued'
                  AND NOT EXISTS(SELECT 1 FROM collection_members cm WHERE cm.item_id=i.item_id)
                UNION ALL
                SELECT 'collection',operation_id,queued_at FROM collection_operations WHERE state='queued'
                ) ORDER BY queued_at,id,kind LIMIT 1""").fetchone()
            if row is None:
                return None
            table,key = ('distill_items','item_id') if row['kind']=='item' else ('collection_operations','operation_id')
            db.execute(f"UPDATE {table} SET state='working',updated_at=? WHERE {key}=?",(_now(),row['id']))
            return row['kind'],row['id']

    def claim_next_item(self) -> int | None:
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT item_id FROM distill_items
                   WHERE state = 'queued' AND NOT EXISTS (
                       SELECT 1 FROM collection_members cm WHERE cm.item_id=distill_items.item_id)
                   ORDER BY queued_at, item_id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            item_id = int(row["item_id"])
            changed = connection.execute(
                """UPDATE distill_items SET state = 'working', updated_at = ?
                   WHERE item_id = ? AND state = 'queued'""",
                (_now(), item_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("queued item changed during claim")
            return item_id

    def requeue_interrupted(self) -> int:
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""UPDATE collection_operations
                SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'queued' END,updated_at=?
                WHERE state='working'""", (_now(),))
            return connection.execute(
                """UPDATE distill_items SET state = 'queued', updated_at = ?
                   WHERE state = 'working'""",
                (_now(),),
            ).rowcount

    def retry_item(self, item_id: int, replacement=None) -> None:
        self.expire_submitted_sources()
        self._enqueue_existing(item_id, expected_state="failed", replacement=replacement)

    def resume_item(self, item_id: int) -> None:
        self._enqueue_existing(item_id, expected_state="waiting_user")

    def mark_working(self, item_id: int, phase: str) -> None:
        self._set_item(item_id, state="working", phase=phase)

    def mark_waiting(self, item_id: int, confirmation: Mapping[str, object]) -> None:
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            changed = connection.execute("UPDATE distill_items SET state='waiting_user',phase='reviewing',error_code=NULL,confirmation_json=?,updated_at=? WHERE item_id=?",
                (_confirmation_json(confirmation, connection, item_id), _now(), item_id)).rowcount
            if changed != 1:
                raise LookupError(f'distill item {item_id} does not exist')
            _sync_manual_cards(connection, item_id)

    def confirmation_view(self, item_id):
        """Read v2 identities, including persisted schema-18 compatibility maps."""
        from .confirmation_schema import view
        with connect(self.path) as db:
            row = db.execute('SELECT * FROM distill_items WHERE item_id=?', (item_id,)).fetchone()
            return view(db, row) if row is not None else None

    def manual_cards(self, scope_kind='items', scope_id='independent', *, include_inactive=False):
        with connect(self.path) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM manual_cards WHERE scope_kind=? AND scope_id=? " +
                ("" if include_inactive else "AND lifecycle='active' ") + "ORDER BY enqueue_seq",
                (scope_kind, str(scope_id)))]

    def update_confirmation_suggestions(self, item_id: int, expected_json: str, pending: Mapping[str, object]) -> None:
        """Compare the pending revision before saving advisory options only."""
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE distill_items SET confirmation_json = ?, updated_at = ?
                   WHERE item_id = ? AND state = 'waiting_user' AND confirmation_json = ?""",
                (_confirmation_json(pending, connection, item_id), _now(), item_id, expected_json))
            if result.rowcount != 1:
                raise ValueError('来源确认已更新，请刷新后再操作。')
            _sync_manual_cards(connection, item_id)

    def resolve_confirmation(
        self,
        item_id: int,
        expected_json: str,
        *,
        next_confirmation: Mapping[str, object] | None = None,
        fact: SourceFact | None = None,
        lineage=None,
        unable: bool = False,
        decision=None,
        group_decision=None,
    ) -> str:
        """Consume exactly one pending revision with its formal fact and queue state."""
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if group_decision is not None:
                prior = _group_decision(connection, item_id, group_decision)
                if prior is not None:
                    return prior
            row = connection.execute(
                """SELECT i.*, sf.source_fact_id FROM distill_items AS i
                   LEFT JOIN source_facts AS sf ON sf.material_id = i.material_id
                   WHERE i.item_id = ?""",
                (item_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"distill item {item_id} does not exist")
            if row["state"] != "waiting_user" or row["confirmation_json"] != expected_json:
                from .confirmation_revision import ConfirmationConflict
                raise ConfirmationConflict("来源确认已更新，请查看该疑点当前状态。")
            if group_decision is not None:
                from .confirmation_schema import view
                from .confirmation_revision import ConfirmationConflict
                current = view(connection, row)
                group = next((g for g in current['groups'] if g['group_id'] == group_decision['group_id']), None)
                selection = group_decision['selected_member_uids']
                if (group is None or group['group_revision'] != group_decision['group_revision']
                        or not selection or len(set(selection)) != len(selection)
                        or not set(selection) <= set(group['member_uids'])):
                    raise ConfirmationConflict('group_revision_conflict')
                if {a.get('concern_uid') for a in group_decision['audit']} != set(selection):
                    raise ValueError('group_member_audit_required')

            # Another submission may already have established this material's fact.
            # Its old confirmation cannot replace that fact or keep the item blocked.
            if row["source_fact_id"] is not None:
                if next_confirmation is not None or fact is not None:
                    raise ValueError("这份来源已由另一次确认成立；当前纠正尚未保存，不能覆盖原事实。")
                next_confirmation, fact, unable = None, None, False
            if fact is not None:
                _establish_source_fact(connection, int(row["material_id"]), fact, lineage=lineage)
            if next_confirmation is not None:
                state = "waiting_user" if next_confirmation["concerns"] or next_confirmation.get("review_required") else "queued"
            else:
                state = "failed" if unable else "queued"
            pending_json = (
                _confirmation_json(next_confirmation, connection, item_id)
                if next_confirmation is not None else row["confirmation_json"] if unable else None
            )
            now = _now()
            connection.execute(
                """UPDATE distill_items
                   SET state = ?, phase = 'reviewing', error_code = ?,
                       confirmation_json = ?, queued_at = ?, updated_at = ?
                   WHERE item_id = ?""",
                (
                    state,
                    "source_unconfirmed" if unable else None,
                    pending_json,
                    now if state == "queued" else row["queued_at"],
                    now,
                    item_id,
                ),
            )
            _sync_manual_cards(connection, item_id)
            if decision:
                connection.execute('INSERT INTO confirmation_decisions VALUES (?,?,?,?,?)',
                                   (item_id, *decision, state))
            if group_decision is not None:
                from .confirmation_schema import digest
                selection = digest(sorted(group_decision['selected_member_uids']))
                payload = _group_payload(group_decision)
                connection.execute('INSERT INTO group_decisions VALUES (?,?,?,?,?,?,?,?,?)',
                    (item_id, group_decision['request_id'], group_decision['group_id'],
                     group_decision['group_revision'], selection, digest(payload),
                     _json({'state': state}), _json(group_decision['audit']), now))
            if state == "queued":
                _wake_collection(connection, item_id)
            return state

    def group_decision(self, item_id, request):
        with connect(self.path) as db:
            return _group_decision(db, item_id, request)

    def confirmation_decision(self, item_id, revision, action, value):
        with connect(self.path) as db:
            row = db.execute('SELECT * FROM confirmation_decisions WHERE item_id=? AND revision=?',
                             (item_id, revision)).fetchone()
        if row is None:
            return None
        if (row['action'], row['value']) != (action, value):
            raise ValueError('该疑点已保存另一项决定，请查看当前结果。')
        return row['state']

    def mark_failed(self, item_id: int, phase: str, error_code: str, *, rejection_reason: str | None = None) -> None:
        self._set_item(item_id, state="failed", phase=phase, error_code=error_code,
                       rejection_reason=rejection_reason if error_code == 'knowledge_not_qualified' else None)

    def dismiss_item(self, item_id: int) -> None:
        with connect(self.path) as connection:
            changed = connection.execute(
                """UPDATE distill_items SET dismissed_at=COALESCE(dismissed_at, ?)
                   WHERE item_id=? AND state='failed'
                   AND NOT EXISTS(SELECT 1 FROM collection_members WHERE item_id=?)""",
                (_now(), item_id, item_id),
            ).rowcount
            if changed != 1:
                raise ValueError('只能放弃已停止的独立条目；当前内容没有改变。')
            _sync_manual_cards(connection, item_id)

    def return_to_confirmation(self, item_id: int) -> None:
        with connect(self.path) as connection:
            changed = connection.execute(
                """UPDATE distill_items SET state='waiting_user', error_code=NULL,
                   updated_at=? WHERE item_id=? AND state='failed' AND dismissed_at IS NULL
                   AND error_code='source_unconfirmed' AND confirmation_json IS NOT NULL""",
                (_now(), item_id),
            ).rowcount
            if changed != 1:
                raise ValueError("没有可继续的来源确认")
            _sync_manual_cards(connection, item_id)

    def mark_succeeded(self, item_id: int) -> None:
        self._set_item(item_id, state="succeeded", phase="done", confirmation_json=None)

    def attach_material(self, item_id: int, material: CapturedMaterial) -> int:
        from .source_versions import snapshot_key, SourceVersionError
        capture_key = snapshot_key(material)
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if material.source_kind in {"youtube", "xiaohongshu", "x", "zhihu", "weibo"}:
                from .chrome import ChromeSessionError
                from .youtube import connection_authority
                if material.source_kind == 'douyin':
                    from .douyin import DouyinSourceError as YouTubeSourceError
                elif material.source_kind == 'xiaohongshu':
                    from .xiaohongshu import connection_authority
                elif material.source_kind == 'x':
                    from .xpost import connection_authority
                elif material.source_kind == 'zhihu':
                    from .zhihu import connection_authority
                elif material.source_kind == 'weibo':
                    from .weibo import connection_authority
                current = connection.execute("SELECT * FROM source_connections WHERE platform = ?", (material.source_kind,)).fetchone()
                if material.metadata.get('session_authority') != connection_authority(current):
                    raise ChromeSessionError(material.source_kind + '_connection_changed')
                submitted = connection.execute('SELECT platform_authority_json FROM distill_items WHERE item_id = ?', (item_id,)).fetchone()
                if submitted is None or json.loads(submitted['platform_authority_json']) != material.metadata.get('session_authority'):
                    raise ChromeSessionError(material.source_kind + '_connection_changed')
            membership = connection.execute('''SELECT native_id,native_version,authority_json FROM collection_members
                JOIN collection_operations USING(operation_id) WHERE item_id=?''',(item_id,)).fetchone()
            from .same_topic_discovery import CAPTURE_ON_PROCESSING
            capture_on_processing = (membership is not None and membership['native_version'] == CAPTURE_ON_PROCESSING
                                     and material.source_kind in {'youtube','xiaohongshu','x','zhihu','weibo'})
            if membership is not None and (material.source_kind != json.loads(membership['authority_json']).get('platform', 'douyin') or material.source_key != membership['native_id']
                    or (not capture_on_processing and material.metadata.get('native_content_version') != membership['native_version'])):
                raise SourceVersionError('collection_member_changed')
            existing = connection.execute(
                "SELECT * FROM materials WHERE source_kind = ? AND source_key = ? AND snapshot_key = ?",
                (material.source_kind, material.source_key, capture_key),
            ).fetchone()
            bound = connection.execute("""SELECT i.material_id, i.confirmation_json, m.snapshot_key,
                EXISTS(SELECT 1 FROM source_facts sf WHERE sf.material_id = m.material_id) AS established
                FROM distill_items i JOIN materials m USING(material_id) WHERE i.item_id = ?""", (item_id,)).fetchone()
            if bound is not None and bound['snapshot_key'] != capture_key and (bound['confirmation_json'] or bound['established'] or capture_on_processing):
                raise SourceVersionError('source_snapshot_changed')
            if existing is not None and material.source_kind in {'douyin', 'youtube', 'xiaohongshu', 'x', 'zhihu', 'weibo'}:
                from .youtube import YouTubeSourceError
                if material.source_kind == 'douyin':
                    from .douyin import DouyinSourceError as YouTubeSourceError
                elif material.source_kind == 'xiaohongshu':
                    from .xiaohongshu import XiaohongshuSourceError as YouTubeSourceError
                elif material.source_kind == 'x':
                    from .xpost import XPostSourceError as YouTubeSourceError
                elif material.source_kind == 'zhihu':
                    from .zhihu import ZhihuSourceError as YouTubeSourceError
                elif material.source_kind == 'weibo':
                    from .weibo import WeiboSourceError as YouTubeSourceError
                has_fact = connection.execute('SELECT 1 FROM source_facts WHERE material_id = ?', (existing['material_id'],)).fetchone()
                if not has_fact and existing['metadata_json'] != _json(material.metadata):
                    pending = connection.execute('SELECT 1 FROM distill_items WHERE material_id = ? AND confirmation_json IS NOT NULL', (existing['material_id'],)).fetchone()
                    if pending:
                        raise YouTubeSourceError(material.source_kind + '_snapshot_changed')
                    connection.execute('UPDATE materials SET metadata_json = ? WHERE material_id = ?',
                                       (_json(material.metadata), existing['material_id']))
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO materials (
                        source_kind, source_key, submitted_url, canonical_url,
                        metadata_json, created_at, snapshot_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        material.source_kind,
                        material.source_key,
                        material.submitted_url,
                        material.canonical_url,
                        _json(material.metadata),
                        _now(),
                        capture_key,
                    ),
                )
                material_id = int(cursor.lastrowid)
            else:
                material_id = int(existing["material_id"])
            if hasattr(material, 'members') and not connection.execute(
                    'SELECT 1 FROM source_facts WHERE material_id = ?', (material_id,)).fetchone():
                import hashlib
                connection.execute('DELETE FROM source_media WHERE material_id = ?', (material_id,))
                for position, member in enumerate(material.members):
                    content = member.path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != member.sha256:
                        from .xiaohongshu import XiaohongshuSourceError
                        raise XiaohongshuSourceError(material.source_kind + '_media_invalid')
                    connection.execute('INSERT INTO source_media VALUES (?, ?, ?, ?, ?, ?)',
                        (material_id, member.member_id, position, member.mime_type, member.sha256, content))
            changed = connection.execute(
                "UPDATE distill_items SET material_id = ?, updated_at = ? WHERE item_id = ?",
                (material_id, _now(), item_id),
            ).rowcount
            if changed != 1:
                raise LookupError(f"distill item {item_id} does not exist")
            return material_id

    def establish_source_fact(self, material_id: int, fact: SourceFact, *, lineage=None) -> int:
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            return _establish_source_fact(connection, material_id, fact, lineage=lineage)

    def commit_source_review(self, item_id, expected_revision, identity, result, *, fact=None,
                             lineage=None, confirmation=None):
        """Commit the complete judgment and its business state under one CAS.

        A late model response cannot replace a user decision, a requeued source,
        or a source fact established by another item. Raw response files alone
        have no authority to establish a fact.
        """
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM distill_items WHERE item_id=?', (item_id,)).fetchone()
            if (row is None or row['review_revision'] != expected_revision or row['state'] != 'working'
                    or row['confirmation_json'] is not None):
                raise SourceReviewConflict('source_review_revision_conflict')
            if connection.execute('SELECT 1 FROM source_facts WHERE material_id=?', (row['material_id'],)).fetchone():
                raise SourceReviewConflict('source_review_fact_already_established')
            failure = result.get('failure')
            if failure:
                if fact is not None or confirmation is not None:
                    raise ValueError('failed_review_cannot_establish_source')
            elif (fact is None) == (confirmation is None):
                raise ValueError('completed_review_requires_one_outcome')
            connection.execute('INSERT INTO source_review_results VALUES (?,?,?,?,?,?)',
                (item_id, expected_revision, identity, 'failed' if failure else 'complete', _json(result), _now()))
            if fact is not None:
                _establish_source_fact(connection, row['material_id'], fact, lineage=lineage)
            if confirmation is not None:
                connection.execute("UPDATE distill_items SET review_revision=review_revision+1,state='waiting_user',phase='reviewing',confirmation_json=?,updated_at=? WHERE item_id=?",
                    (_confirmation_json(confirmation, connection, item_id), _now(), item_id))
            else:
                connection.execute("UPDATE distill_items SET review_revision=review_revision+1,state=?,error_code=?,updated_at=? WHERE item_id=?",
                    ('failed' if failure else 'working', 'review_' + failure if failure else None, _now(), item_id))

            _sync_manual_cards(connection, item_id)

    def record_video_transcript(self, material_id, chunks):
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM source_facts WHERE material_id = ?', (material_id,)).fetchone():
                raise ValueError('SourceFact is immutable')
            row = connection.execute('SELECT metadata_json FROM materials WHERE material_id = ?', (material_id,)).fetchone()
            metadata = json.loads(row['metadata_json'])
            metadata['transcript_chunks'] = [
                {'text': c.text, 'start_seconds': c.start_seconds, 'end_seconds': c.end_seconds} for c in chunks]
            connection.execute('UPDATE materials SET metadata_json = ? WHERE material_id = ?', (_json(metadata), material_id))

    def media_members(self, material_id):
        """Only currently available bytes; released identities are in media_manifest."""
        with connect(self.path) as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM source_media WHERE material_id = ? AND length(content)>0 ORDER BY position', (material_id,))]

    def media_manifest(self, material_id):
        with connect(self.path) as connection:
            return [dict(row) for row in connection.execute('''SELECT member_id,position,mime_type,sha256,
                length(content)>0 AS content_available FROM source_media WHERE material_id=? ORDER BY position''', (material_id,))]

    def prepare_image_review(self, item_id: int) -> bool:
        """Retain a legacy frozen OCR fact; review a separate local revision."""
        from .image_confirmation import pending_review
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''SELECT m.*, sf.source_fact_id, sf.snapshot,
                sf.uncertainties_json, sf.lineage_json FROM distill_items i
                JOIN materials m ON m.material_id=i.material_id
                JOIN source_facts sf ON sf.material_id=m.material_id
                WHERE i.item_id=? AND i.state IN ('failed','working','queued')
                AND NOT EXISTS (SELECT 1 FROM knowledge_results k WHERE k.source_fact_id=sf.source_fact_id)''', (item_id,)).fetchone()
            if row is None:
                return False
            lineage = json.loads(row['lineage_json'])
            uncertainties = json.loads(row['uncertainties_json'])
            requalified = False
            for uncertainty in uncertainties:
                if uncertainty.get('by') != 'ocr' or uncertainty.get('status') != 'unresolved':
                    continue
                image = next((i for i in lineage.get('image_ocr', [])
                    if i['member_id'] == uncertainty.get('member_id') and i.get('engine') == 'apple_vision'), None)
                line = next((l for l in image['lines'] if l['start'] == uncertainty['start']
                    and l['end'] == uncertainty['end']), None) if image else None
                if line and not line.get('alternatives') and uncertainty.get('reason') == '图片文字识别置信度较低':
                    uncertainty.update(status='advisory', reason='Apple Vision 原始评分，仅作诊断，不代表文字错误')
                    requalified = True
            fact = SourceFact(row['snapshot'], tuple(uncertainties))
            pending = pending_review(fact, lineage)
            if pending is None and not requalified:
                return False
            revision_key = 'ocr-review-v1:' + str(row['source_fact_id'])
            revised = connection.execute('SELECT material_id FROM materials WHERE source_kind=? AND source_key=? AND snapshot_key=?',
                (row['source_kind'], row['source_key'], revision_key)).fetchone()
            if revised is None:
                metadata = json.loads(row['metadata_json'])
                metadata['ocr_review_of_source_fact_id'] = row['source_fact_id']
                revision = connection.execute('''INSERT INTO materials
                    (source_kind,source_key,submitted_url,canonical_url,metadata_json,created_at,snapshot_key)
                    VALUES (?,?,?,?,?,?,?)''', (row['source_kind'], row['source_key'], row['submitted_url'],
                    row['canonical_url'], _json(metadata), _now(), revision_key)).lastrowid
                connection.execute('''INSERT INTO source_media
                    SELECT ?,member_id,position,mime_type,sha256,content FROM source_media WHERE material_id=?''',
                    (revision, row['material_id']))
            else:
                revision = revised['material_id']
            has_fact = connection.execute('SELECT 1 FROM source_facts WHERE material_id=?', (revision,)).fetchone()
            if not has_fact and pending is None:
                _establish_source_fact(connection, revision, fact, lineage=lineage)
                has_fact = True
            connection.execute('''UPDATE distill_items SET material_id=?,state=?,phase='reviewing',
                confirmation_json=?,error_code=NULL,updated_at=? WHERE item_id=?''',
                (revision, 'queued' if has_fact else 'waiting_user', None if has_fact else _confirmation_json(pending, connection, item_id), _now(), item_id))
            _sync_manual_cards(connection, item_id)
            return True

    def establish_knowledge(self, source_fact_id: int, knowledge: Knowledge) -> int:
        from dataclasses import replace
        from .domain import validate_knowledge
        with connect(self.path) as connection:
            source = connection.execute('SELECT * FROM source_facts WHERE source_fact_id = ?', (source_fact_id,)).fetchone()
            lineage = json.loads(source['lineage_json'])
            segments = lineage.get('video_segments', [])
            mapped = []
            for evidence in knowledge.evidence:
                if 'image_ocr' in lineage:
                    from .knowledge_model import KnowledgeModelError
                    if evidence.member_id and evidence.member_id.startswith('image-'):
                        raise KnowledgeModelError('knowledge_evidence_invalid')
                    if any(u.get('by') == 'ocr' and u.get('status') == 'unresolved'
                           and u['start'] < evidence.end and u['end'] > evidence.start
                           for u in json.loads(source['uncertainties_json'])):
                        raise KnowledgeModelError('knowledge_evidence_invalid')
                selected = [s for s in segments if s['start'] < evidence.end and s['end'] > evidence.start]
                if evidence.member_id == 'video-1' and not selected:
                    raise ValueError('video evidence has no transcript locator')
                if selected and evidence.member_id in {None, 'video-1'}:
                    evidence = replace(evidence, member_id='video-1', start_seconds=min(s['start_seconds'] for s in selected),
                                       end_seconds=max(s['end_seconds'] for s in selected))
                mapped.append(evidence)
            knowledge = replace(knowledge, evidence=tuple(mapped))
            validate_knowledge(source['snapshot'], knowledge)
            payload = _json(knowledge_to_dict(knowledge))
            images = {row['member_id'] for row in connection.execute(
                "SELECT sm.member_id FROM source_media sm JOIN source_facts sf ON sf.material_id = sm.material_id WHERE sf.source_fact_id = ?", (source_fact_id,))} if any(e.member_id is not None for e in knowledge.evidence) else set()
            if any(e.member_id is not None and e.member_id not in images for e in knowledge.evidence):
                raise ValueError('knowledge image evidence is absent')
            existing = connection.execute(
                "SELECT * FROM knowledge_results WHERE source_fact_id = ?",
                (source_fact_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload:
                    raise ValueError("existing KnowledgeResult differs")
                return int(existing["knowledge_result_id"])
            cursor = connection.execute(
                """
                INSERT INTO knowledge_results (
                    source_fact_id, payload_json, created_at
                ) VALUES (?, ?, ?)
                """,
                (source_fact_id, payload, _now()),
            )
            return int(cursor.lastrowid)

    def mark_published(
        self, knowledge_result_id: int, relative_path: str, *, vault: Path
    ) -> None:
        destination = str(vault.resolve(strict=True))
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT published_path, published_vault FROM knowledge_results WHERE knowledge_result_id = ?",
                (knowledge_result_id,),
            ).fetchone()
            if row is None:
                raise LookupError("KnowledgeResult does not exist")
            if row["published_path"] is not None:
                if row["published_path"] != relative_path or row["published_vault"] != destination:
                    raise ValueError("KnowledgeResult was published elsewhere")
                return
            connection.execute(
                """
                UPDATE knowledge_results
                SET published_path = ?, published_at = ?, published_vault = ?
                WHERE knowledge_result_id = ? AND published_path IS NULL
                """,
                (relative_path, _now(), destination, knowledge_result_id),
            )

    def item_bundle(self, item_id: int) -> sqlite3.Row | None:
        with connect(self.path) as connection:
            return connection.execute(
                """
                SELECT i.*, m.*, ss.input_kind, ss.input_key, ss.input_label, ss.retryable, ss.content IS NOT NULL AS input_available, ss.retain_until, sf.source_fact_id, sf.snapshot, sf.lineage_json,
                       sf.uncertainties_json, kr.knowledge_result_id,
                       kr.payload_json, kr.published_path, kr.published_at, kr.published_vault
                FROM distill_items AS i
                LEFT JOIN submitted_sources AS ss ON ss.item_id = i.item_id
                LEFT JOIN materials AS m ON m.material_id = i.material_id
                LEFT JOIN source_facts AS sf ON sf.material_id = m.material_id
                LEFT JOIN knowledge_results AS kr
                  ON kr.source_fact_id = sf.source_fact_id
                WHERE i.item_id = ?
                """,
                (item_id,),
            ).fetchone()

    def recent_items(self, limit: int = 30) -> tuple[sqlite3.Row, ...]:
        with connect(self.path) as connection:
            return tuple(
                connection.execute(
                    """
                    WITH completed AS (
                        SELECT item_id, updated_at,
                               ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY updated_at, item_id) AS attempt
                        FROM distill_items WHERE state='succeeded'
                    )
                    SELECT i.*, ss.input_kind, ss.input_key, ss.input_label, ss.retryable, ss.content IS NOT NULL AS input_available, ss.retain_until, m.source_kind, m.canonical_url, m.metadata_json,
                           kr.knowledge_result_id, kr.payload_json,
                           kr.published_path, kr.published_at, kr.published_vault, sf.lineage_json
                    FROM distill_items AS i
                    LEFT JOIN submitted_sources AS ss ON ss.item_id = i.item_id
                    LEFT JOIN materials AS m ON m.material_id = i.material_id
                    LEFT JOIN source_facts AS sf ON sf.material_id = m.material_id
                    LEFT JOIN knowledge_results AS kr
                      ON kr.source_fact_id = sf.source_fact_id
                    WHERE (i.state!='succeeded' AND i.dismissed_at IS NULL AND NOT EXISTS(SELECT 1 FROM collection_members cm WHERE cm.item_id=i.item_id))
                       OR i.item_id IN (SELECT item_id FROM completed WHERE attempt=1 ORDER BY updated_at DESC,item_id DESC LIMIT ?)
                    ORDER BY i.updated_at DESC, i.item_id DESC
                    """,
                    (limit,),
                )
            )

    def set_setting(self, key: str, value: str) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: Mapping[str, str]) -> None:
        if not values or any(not key or not isinstance(value, str) for key, value in values.items()):
            raise ValueError("settings are incomplete")
        with connect(self.path) as connection:
            connection.executemany(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                values.items(),
            )

    def settings(self) -> dict[str, str]:
        with connect(self.path) as connection:
            return {
                str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key, value FROM settings")
            }

    def setting(self, key: str) -> str | None:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else None

    def save_connection(self, platform: str, account_label: str | None, *, browser_context: str | None = None) -> None:
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation FROM source_connections WHERE platform = ?",
                (platform,),
            ).fetchone()
            generation = 1 if row is None else int(row["generation"]) + 1
            connection.execute(
                """
                INSERT INTO source_connections (
                    platform, state, generation, account_label, connected_at, browser_context
                ) VALUES (?, 'connected', ?, ?, ?, ?)
                ON CONFLICT (platform) DO UPDATE SET
                    state = excluded.state,
                    generation = excluded.generation,
                    account_label = excluded.account_label,
                    connected_at = excluded.connected_at,
                    browser_context = excluded.browser_context
                """,
                (platform, generation, account_label, _now(), browser_context),
            )

    def connection(self, platform: str) -> sqlite3.Row | None:
        with connect(self.path) as connection:
            return connection.execute(
                "SELECT * FROM source_connections WHERE platform = ?", (platform,)
            ).fetchone()

    def connections(self) -> dict[str, sqlite3.Row]:
        with connect(self.path) as connection:
            return {
                str(row["platform"]): row
                for row in connection.execute("SELECT * FROM source_connections")
            }

    def clear_connection(self, platform: str) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                UPDATE source_connections
                SET state = 'unconfigured', account_label = NULL
                WHERE platform = ?
                """,
                (platform,),
            )

    def require_relogin(self, platform: str) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                UPDATE source_connections SET state = 'relogin_required'
                WHERE platform = ?
                """,
                (platform,),
            )

    def _set_item(
        self,
        item_id: int,
        *,
        state: str,
        phase: str,
        error_code: str | None = None,
        confirmation_json: str | None = None,
        rejection_reason: str | None = None,
    ) -> None:
        with connect(self.path) as connection:
            changed = connection.execute(
                """
                UPDATE distill_items
                SET state = ?, phase = ?, error_code = ?, rejection_reason = ?,
                    confirmation_json = CASE WHEN ? IN ('working', 'failed')
                                             THEN COALESCE(?, confirmation_json) ELSE ? END,
                    updated_at = ?
                WHERE item_id = ?
                """,
                (state, phase, error_code, rejection_reason, state, confirmation_json, confirmation_json, _now(), item_id),
            ).rowcount
            if state == "failed":
                connection.execute(
                    """UPDATE submitted_sources SET retain_until = ?
                       WHERE item_id = ? AND content IS NOT NULL AND retain_until IS NULL""",
                    ((datetime.now(UTC) + timedelta(hours=72)).isoformat(), item_id),
                )
            if changed != 1:
                raise LookupError(f"distill item {item_id} does not exist")
            _sync_manual_cards(connection, item_id)

    def _enqueue_existing(self, item_id: int, *, expected_state: str, replacement=None) -> None:
        now = _now()
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM distill_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"distill item {item_id} does not exist")
            if row["state"] != expected_state:
                raise ValueError(f"item is not {expected_state}")
            if row['dismissed_at'] is not None:
                raise ValueError('本条已放弃，如需再次处理请重新投递。')
            if expected_state == 'failed' and connection.execute(
                'SELECT 1 FROM collection_members WHERE item_id=?', (item_id,)
            ).fetchone() and row['error_code'] in {'collection_member_changed','douyin_input_unsupported','source_snapshot_changed'}:
                raise ValueError('本条无法在已确认范围内重试，请重新投递并确认新的范围。')
            if (expected_state == 'failed' and row['platform_authority_json'] != '{}'
                    and row['confirmation_json'] is None
                    and not connection.execute('SELECT 1 FROM source_facts WHERE material_id = ?', (row['material_id'],)).fetchone()):
                from .youtube import connection_authority
                from .chrome import ChromeSessionError
                platform = 'youtube'
                if any(host in row['submitted_url'] for host in ('xiaohongshu.com', 'xhslink.com')):
                    from .xiaohongshu import connection_authority
                    platform = 'xiaohongshu'
                elif any(host in row['submitted_url'] for host in ('x.com', 'twitter.com')):
                    from .xpost import connection_authority
                    platform = 'x'
                elif any(host in row['submitted_url'] for host in ('weibo.com', 'weibo.cn')):
                    from .weibo import connection_authority
                    platform = 'weibo'
                elif 'zhihu.com' in row['submitted_url']:
                    from .zhihu import connection_authority
                    platform = 'zhihu'
                try:
                    authority = connection_authority(connection.execute(
                        "SELECT * FROM source_connections WHERE platform = ?", (platform,)
                    ).fetchone())
                except ChromeSessionError as error:
                    raise ValueError('请先在设置中连接对应平台，然后重试。') from error
                connection.execute("UPDATE distill_items SET platform_authority_json = ? WHERE item_id = ?",
                                   (_json(authority), item_id))
            source = connection.execute("SELECT * FROM submitted_sources WHERE item_id = ?", (item_id,)).fetchone()
            if source is not None:
                if not source["retryable"]:
                    raise ValueError("原文件无法完整读取，请修改后重新投递。")
                if replacement is not None and (replacement.source_kind, replacement.source_key) != (source["input_kind"], source["input_key"]):
                    raise ValueError("重试只能使用同一份原内容，修改后的内容请重新投递。")
                has_fact = connection.execute("SELECT 1 FROM source_facts sf JOIN distill_items i ON i.material_id = sf.material_id WHERE i.item_id = ?", (item_id,)).fetchone()
                if not has_fact and (source["content"] is None or source["input_kind"] in FILE_KINDS):
                    if source['input_kind'] in FILE_KINDS:
                        if replacement is not None:
                            retain_copy(self.path.parent, source['input_kind'], source['input_key'], source['input_label'], replacement.content)
                        content = read_copy(self.path.parent, source['input_kind'], source['input_key'], source['input_label'])
                        from .file_sources import SubmittedSource
                        replacement = SubmittedSource(source['input_kind'], source['input_key'], source['input_label'], content, {})
                    if replacement is None or (replacement.source_kind, replacement.source_key) != (source["input_kind"], source["input_key"]):
                        raise ValueError("副本已过期，请重新提供完全相同的原内容及来源声明。")
                    connection.execute("UPDATE submitted_sources SET content = ?, input_metadata = ? WHERE item_id = ?", (replacement.content, _json(replacement.metadata), item_id))
                connection.execute("UPDATE submitted_sources SET retain_until = NULL WHERE item_id = ?", (item_id,))
            connection.execute(
                """UPDATE distill_items
                   SET state = 'queued', error_code = NULL, rejection_reason = NULL,
                       confirmation_json = CASE WHEN json_array_length(confirmation_json, '$.concerns') = 0
                                                THEN confirmation_json ELSE NULL END,
                       queued_at = ?, updated_at = ?
                   WHERE item_id = ?""",
                (now, now, item_id),
            )
            _sync_manual_cards(connection, item_id)
            _wake_collection(connection, item_id)


def _group_payload(request):
    return {key: (sorted(request[key]) if key == 'selected_member_uids' else request[key])
            for key in ('group_id', 'group_revision', 'selected_member_uids', 'action', 'value')}


def _group_decision(db, item_id, request):
    from .confirmation_schema import digest
    row = db.execute('''SELECT * FROM group_decisions WHERE item_id=? AND
        (request_id=? OR (submitted_revision=? AND selection_digest=?))''',
        (item_id, request['request_id'], request['group_revision'],
         digest(sorted(request['selected_member_uids'])))).fetchone()
    if row is None:
        return None
    if row['payload_digest'] != digest(_group_payload(request)):
        from .confirmation_revision import ConfirmationConflict
        raise ConfirmationConflict('group_decision_payload_conflict')
    return json.loads(row['result_json'])['state']


def _establish_source_fact(
    connection: sqlite3.Connection, material_id: int, fact: SourceFact, *, lineage=None
) -> int:
    material = connection.execute('SELECT * FROM materials WHERE material_id = ?', (material_id,)).fetchone()
    if material is not None and json.loads(material['metadata_json']).get('temporary_expired'):
        from .source_versions import SourceVersionError
        raise SourceVersionError('source_capture_expired')
    if material is not None and material['source_kind'] == 'douyin':
        metadata = json.loads(material['metadata_json'])
        if metadata.get('note_kind') == 'normal':
            authority = metadata.get('session_authority')
            if authority is not None:
                current = connection.execute("SELECT * FROM source_connections WHERE platform='douyin'").fetchone()
                if current is None or current['state'] != 'connected' or any(current[key] != authority[key] for key in ('generation','connected_at')):
                    from .douyin import DouyinSourceError
                    raise DouyinSourceError('douyin_connection_changed')
            media = list(connection.execute('SELECT member_id, sha256 FROM source_media WHERE material_id=? ORDER BY position', (material_id,)))
            if [(m['member_id'],m['sha256']) for m in media] != [(m['member_id'],m['sha256']) for m in metadata['media_members']]:
                from .douyin import DouyinSourceError
                raise DouyinSourceError('douyin_media_invalid')
            lineage = {**(lineage or {}), 'kind': 'douyin', 'media_members': metadata['media_members'],
                       'native_title': metadata['source_title'], 'native_description': metadata['original_description'],
                       'original_markdown': metadata.get('original_markdown')}
    if material is not None and material['source_kind'] in {'xiaohongshu', 'x', 'zhihu', 'weibo'}:
        from .xiaohongshu import connection_authority, XiaohongshuSourceError
        from .chrome import ChromeSessionError
        platform = material['source_kind']
        if platform == 'x':
            from .xpost import connection_authority, XPostSourceError as XiaohongshuSourceError
        elif platform == 'zhihu':
            from .zhihu import connection_authority, ZhihuSourceError as XiaohongshuSourceError
        elif platform == 'weibo':
            from .weibo import connection_authority, WeiboSourceError as XiaohongshuSourceError
        metadata = json.loads(material['metadata_json'])
        has_fact = connection.execute('SELECT 1 FROM source_facts WHERE material_id = ?', (material_id,)).fetchone()
        if not has_fact:
            current = connection.execute("SELECT * FROM source_connections WHERE platform = ?", (platform,)).fetchone()
            if metadata['session_authority'] != connection_authority(current):
                raise ChromeSessionError(platform + '_connection_changed')
            media = list(connection.execute('SELECT * FROM source_media WHERE material_id = ? ORDER BY position', (material_id,)))
            if [(m['member_id'], m['sha256']) for m in media] != [(m['member_id'], m['sha256']) for m in metadata['media_members']]:
                raise XiaohongshuSourceError(platform + '_media_incomplete')
        lineage = {**(lineage or {}), 'kind': platform, 'media_members': metadata['media_members'],
                   'native_title': metadata['source_title'], 'native_description': metadata['original_description']}
        if metadata['note_kind'] == 'video':
            from .xiaohongshu import native_video_fact, video_segments
            lineage['video_segments'] = video_segments(metadata, fact.snapshot)
            fact = native_video_fact(metadata, fact)
    uncertainties = _json(list(fact.uncertainties))
    existing = connection.execute(
        "SELECT * FROM source_facts WHERE material_id = ?", (material_id,)
    ).fetchone()
    if existing is not None:
        if existing["snapshot"] != fact.snapshot or existing["uncertainties_json"] != uncertainties:
            raise ValueError("existing SourceFact differs")
        return int(existing["source_fact_id"])
    cursor = connection.execute(
        """INSERT INTO source_facts (
            material_id, snapshot, uncertainties_json, created_at, lineage_json
        ) VALUES (?, ?, ?, ?, ?)""",
        (material_id, fact.snapshot, uncertainties, _now(), _json(lineage or {})),
    )
    return int(cursor.lastrowid)


def _confirmation_json(confirmation: Mapping[str, object], connection=None, item_id=None) -> str:
    if connection is not None:
        from .confirmation_schema import prepare
        confirmation = prepare(connection, item_id, confirmation)
    from .confirmation_display import concern_total
    return _json({**confirmation, "review_identity": confirmation.get("review_identity", confirmation.get("token", uuid4().hex)), "concern_total": concern_total(confirmation), "token": uuid4().hex})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _wake_collection(connection, item_id):
    connection.execute("""UPDATE collection_operations SET state='queued',updated_at=?
        WHERE operation_id IN (SELECT operation_id FROM collection_members WHERE item_id=?)
        AND state IN ('waiting_user','partial','failed') AND cancel_requested=0""", (_now(),item_id))
