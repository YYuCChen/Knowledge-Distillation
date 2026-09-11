from __future__ import annotations

from datetime import UTC, datetime

from knowledge_distiller.accepted_insight_library import load_insight_lineage_nodes, read_accepted_row
from knowledge_distiller.insight_judgment_service import (
    InsightJudgmentService, JudgmentResultKind, accepted_role_for_version,
)
from knowledge_distiller.organization_models import decode_insight_payload
from .database import connect
from .organization import load_sources
from .topics import read_formal_points


class InsightError(ValueError):
    pass


def read_lineage(connection, version_id):
    points = {p.identity for source in load_sources(connection) for p in source.points}
    return load_insight_lineage_nodes(connection, version_id, points.__contains__, root_requires_accepted=False)


class InsightLibrary:
    def __init__(self, store):
        self.store = store
        self.judgments = InsightJudgmentService(store.path, lineage_reader=read_lineage)

    def read(self, version_id, connection=None):
        if connection is None:
            with connect(self.store.path) as db:
                db.execute('BEGIN')
                return self.read(version_id, db)
        row = connection.execute('''SELECT v.*, j.judgment_id, j.decision, j.annotation_text, j.decided_at,
            r.reconsideration_id, r.reconsidered_at
            FROM insight_versions v JOIN organization_events e ON e.event_id=v.produced_event_id
            LEFT JOIN user_insight_judgments j ON j.insight_version_id=v.insight_version_id
            LEFT JOIN insight_reconsiderations r ON r.insight_version_id=v.insight_version_id
            WHERE e.status='succeeded' AND v.insight_version_id=?''',(version_id,)).fetchone()
        if row is None:
            raise InsightError('这条新知不存在或尚未正式成立。')
        payload = decode_insight_payload(row['payload_json'])
        if len(payload.scan_tags) != 3:
            raise InsightError('这条新知的正式标签无法完整读取。')
        lineage = read_lineage(connection, version_id)
        accepted = read_accepted_row(connection, version_id)
        state = ('interesting' if accepted.effective_role.value == 'current' else 'historical') if accepted else ('rethink' if row['decision'] == 'rethink' else 'pending')
        all_points = {(p['knowledge_result_id'],p['point_id']):p for p in read_formal_points(connection)}
        nodes = {node.insight_version_id: node for node in lineage}
        ordered = {}
        def collect(node):
            for participant in node.participants:
                if participant.knowledge_result_id is not None:
                    key = (participant.knowledge_result_id, participant.point_id)
                    ordered.setdefault(participant.knowledge_result_id, all_points[key])
                else:
                    collect(nodes[participant.accepted_insight_version_id])
        collect(lineage[0])
        sources = list(ordered.values())
        notes = [dict(n) for n in connection.execute('SELECT * FROM personal_cognition_entries WHERE insight_version_id=? ORDER BY entry_id',(version_id,))]
        return dict(id=version_id,insight_id=row['insight_id'],payload=payload,state=state,
            annotation=row['annotation_text'],annotation_time=row['decided_at'],judgment_id=row['judgment_id'],reconsideration_id=row['reconsideration_id'],
            time=(row['reconsidered_at'] or row['decided_at'] or row['created_at']),notes=notes,sources=sources)

    def list(self, state):
        if state not in ('pending','interesting','rethink'):
            raise InsightError('无效的新知筛选。')
        with connect(self.store.path) as db:
            db.execute('BEGIN')
            ids = [r[0] for r in db.execute("SELECT insight_version_id FROM insight_versions v JOIN organization_events e ON e.event_id=v.produced_event_id WHERE e.status='succeeded'")]
            results = [self.read(i, db) for i in ids]
        return sorted((r for r in results if r['state']==state),key=lambda r:(r['time'],r['id']),reverse=True)

    def judge(self, version_id, decision, annotation=None):
        result = self.judgments.record_judgment(version_id, decision, annotation)
        if result.kind not in (JudgmentResultKind.RECORDED,JudgmentResultKind.ALREADY_RECORDED):
            raise InsightError('这条新知的状态已变化或无法完整读取，判断未保存。')
        return result

    def reconsider(self, version_id, operation_id, new_view=''):
        if not operation_id or not isinstance(new_view,str):
            raise InsightError('改观请求不完整。')
        note = new_view if new_view.strip() else None
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM personal_cognition_entries WHERE operation_id=? AND entry_kind='new_idea'", (operation_id,)).fetchone():
                raise InsightError('这个请求已经用于记录想法。')
            existing = db.execute('SELECT * FROM insight_reconsiderations WHERE operation_id=?',(operation_id,)).fetchone()
            if existing is None:
                existing = db.execute('SELECT * FROM insight_reconsiderations WHERE insight_version_id=?',(version_id,)).fetchone()
            if existing is not None:
                saved_note = db.execute("SELECT text FROM personal_cognition_entries WHERE reconsideration_id=? AND entry_kind='new_view'",(existing['reconsideration_id'],)).fetchone()
                if existing['insight_version_id'] != version_id or (saved_note['text'] if saved_note else None) != note:
                    raise InsightError('这个改观请求已经用于不同内容。')
                return existing['reconsideration_id']
            if db.execute('SELECT 1 FROM personal_cognition_entries WHERE operation_id=?',(operation_id,)).fetchone():
                raise InsightError('这个请求已经用于记录想法。')
            target = self.read(version_id,db)
            if target['state'] != 'rethink':
                raise InsightError('只有“再想想”中的新知可以改观。')
            raw = db.execute('SELECT * FROM insight_versions WHERE insight_version_id=?',(version_id,)).fetchone()
            role = accepted_role_for_version(db, raw)
            now = datetime.now(UTC).isoformat()
            cursor = db.execute("INSERT INTO insight_reconsiderations(insight_version_id,judgment_id,operation_id,decision,reconsidered_at) VALUES(?,?,?,'interesting_after_rethink',?)",(version_id,target['judgment_id'],operation_id,now))
            reconsideration_id = cursor.lastrowid
            if note is not None:
                db.execute("INSERT INTO personal_cognition_entries(insight_version_id,judgment_id,reconsideration_id,entry_kind,text,operation_id,created_at) VALUES(?,?,?,'new_view',?,?,?)",(version_id,target['judgment_id'],reconsideration_id,note,operation_id,now))
            if role.retire_current_judgment_id is not None:
                changed = db.execute("UPDATE accepted_insight_versions SET current_role='historical',historical_at=?,historical_reason='newer_accepted_current',caused_by_judgment_id=? WHERE judgment_id=? AND current_role='current'",(now,target['judgment_id'],role.retire_current_judgment_id)).rowcount
                if changed != 1:
                    raise InsightError('已认可版本发生变化，改观未保存。')
            db.execute('''INSERT INTO accepted_insight_versions(insight_version_id,insight_id,judgment_id,judgment_decision,
                reconsideration_id,initial_role,current_role,accepted_at,historical_at,historical_reason,
                caused_by_event_id,caused_by_judgment_id,replacement_insight_id,disqualification_reason)
                VALUES(?,?,?,'rethink',?,?,?,?,?,?,?,?,?,?)''',
                (version_id,raw['insight_id'],target['judgment_id'],reconsideration_id,role.initial_role,role.current_role,now,
                 now if role.current_role=='historical' else None,role.historical_reason,role.caused_by_event_id,
                 role.caused_by_judgment_id,role.replacement_insight_id,role.disqualification_reason))
            return reconsideration_id

    def add_idea(self, version_id, operation_id, text):
        if not operation_id or not isinstance(text,str) or not text.strip():
            raise InsightError('请先写下你的新想法。')
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM personal_cognition_entries WHERE operation_id=?',(operation_id,)).fetchone()
            if existing is not None:
                if (existing['insight_version_id'],existing['entry_kind'],existing['text']) != (version_id,'new_idea',text):
                    raise InsightError('这个请求已经用于不同内容。')
                return existing['entry_id']
            if db.execute('SELECT 1 FROM insight_reconsiderations WHERE operation_id=?',(operation_id,)).fetchone():
                raise InsightError('这个请求已经用于改观。')
            target = self.read(version_id,db)
            if target['state'] != 'interesting':
                raise InsightError('只有当前“有意思”的新知可以追加想法。')
            return db.execute("INSERT INTO personal_cognition_entries(insight_version_id,judgment_id,reconsideration_id,entry_kind,text,operation_id,created_at) VALUES(?,?,?,'new_idea',?,?,?)", (version_id,target['judgment_id'],target['reconsideration_id'],text,operation_id,datetime.now(UTC).isoformat())).lastrowid
