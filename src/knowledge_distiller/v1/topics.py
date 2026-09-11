"""Frozen topic organization over immutable, published source knowledge."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from threading import Lock

from knowledge_distiller.topic_indexing import (
    ExistingTopicInput, TopicPointInput, TopicPointReference, TopicPlan,
    parse_topic_plan,
)
from .database import connect
from .domain import knowledge_from_dict


_REFRESH_LOCKS = defaultdict(Lock)


class TopicError(ValueError):
    pass


@dataclass(frozen=True)
class TopicGuard:
    inputs: str
    baseline: str


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _signature(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class TopicLibrary:
    def __init__(self, store):
        self.store = store
        self._refresh_lock = _REFRESH_LOCKS[store.path.resolve()]

    def read_points(self, connection=None, knowledge_ids=None):
        if connection is None:
            with connect(self.store.path) as db:
                return self.read_points(db, knowledge_ids)
        return read_formal_points(connection, knowledge_ids)

    def snapshot(self, connection=None):
        if connection is None:
            with connect(self.store.path) as db:
                db.execute('BEGIN')
                return self.snapshot(db)
        state = connection.execute('SELECT * FROM topic_snapshot WHERE singleton=1').fetchone()
        topics = []
        for row in connection.execute('SELECT * FROM topic_entries ORDER BY updated_at DESC, topic_id'):
            members = [dict(member) for member in connection.execute(
                'SELECT knowledge_result_id, point_id FROM topic_members WHERE topic_id=? ORDER BY position',
                (row['topic_id'],))]
            if len(members) < 2:
                raise TopicError('主题快照无法完整读取。')
            topics.append(dict(id=row['topic_id'], name=row['name'], scope=row['scope'],
                               updated_at=row['updated_at'], members=members))
        if topics and state is None:
            raise TopicError('主题快照无法完整读取。')
        return dict(topics=topics, knowledge_count=state['knowledge_count'] if state else 0)

    def prepare(self, connection=None, knowledge_ids=None):
        if connection is None:
            with connect(self.store.path) as db:
                db.execute('BEGIN')
                return self.prepare(db, knowledge_ids)
        records = self.read_points(connection, knowledge_ids)
        fields = TopicPointInput.__dataclass_fields__
        points = tuple(TopicPointInput(**{key: row[key] for key in fields}) for row in records)
        snapshot = self.snapshot(connection)
        existing = tuple(ExistingTopicInput(t['id'], t['name'], t['scope'],
            tuple(TopicPointReference(**m) for m in t['members'])) for t in snapshot['topics'])
        return points, existing, TopicGuard(_signature([asdict(p) for p in points]), _signature(snapshot))

    def commit(self, plan: TopicPlan, guard: TopicGuard, connection=None, knowledge_ids=None):
        if connection is None:
            with connect(self.store.path) as db:
                db.execute('BEGIN IMMEDIATE')
                return self.commit(plan, guard, db, knowledge_ids)
        if not connection.in_transaction:
            raise TopicError('主题提交需要事务。')
        points, existing, current = self.prepare(connection, knowledge_ids)
        if current != guard:
            raise TopicError('整理期间知识或主题已变化，请重新发起整理。')
        raw_topics = []
        for topic in plan.topics:
            value = dict(name=topic.name, scope=topic.scope,
                         members=[asdict(m) for m in topic.members])
            value.update({'topic_id': topic.topic_id} if topic.topic_id is not None else {'new_topic_key': topic.new_topic_key})
            raw_topics.append(value)
        validated = parse_topic_plan(_json(dict(topics=raw_topics,
            unassigned_points=[asdict(p) for p in plan.unassigned_points])), points, existing)
        if validated is None:
            raise TopicError('主题结果不完整或引用无效，原主题保持不变。')
        before = {t['id']: t for t in self.snapshot(connection)['topics']}
        now = datetime.now(UTC).isoformat()
        connection.execute('SAVEPOINT topic_commit')
        try:
            retained = {t.topic_id for t in validated.topics if t.topic_id is not None}
            for topic_id in before.keys() - retained:
                connection.execute('DELETE FROM topic_entries WHERE topic_id=?', (topic_id,))
            for topic in validated.topics:
                members = [asdict(m) for m in topic.members]
                if topic.topic_id is None:
                    cursor = connection.execute('INSERT INTO topic_entries(name,scope,updated_at) VALUES(?,?,?)',
                                                (topic.name, topic.scope, now))
                    topic_id = cursor.lastrowid
                else:
                    topic_id = topic.topic_id
                    old = before[topic_id]
                    changed = (old['name'], old['scope'], old['members']) != (topic.name, topic.scope, members)
                    connection.execute('UPDATE topic_entries SET name=?,scope=?,updated_at=? WHERE topic_id=?',
                        (topic.name, topic.scope, now if changed else old['updated_at'], topic_id))
                    connection.execute('DELETE FROM topic_members WHERE topic_id=?', (topic_id,))
                connection.executemany('INSERT INTO topic_members VALUES(?,?,?,?)',
                    [(topic_id, index, m.knowledge_result_id, m.point_id) for index, m in enumerate(topic.members)])
            count = len({p.knowledge_result_id for p in points})
            connection.execute('INSERT INTO topic_snapshot VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET knowledge_count=excluded.knowledge_count,input_signature=excluded.input_signature', (count, guard.inputs))
            result = self.snapshot(connection)
        except Exception:
            connection.execute('ROLLBACK TO topic_commit')
            connection.execute('RELEASE topic_commit')
            raise
        connection.execute('RELEASE topic_commit')
        return result

    def refresh(self, indexer):
        """Explicit internal operation; never called by a page read or search."""
        waiting_guard = self.prepare()[2]
        with self._refresh_lock:
            points, existing, guard = self.prepare()
            if waiting_guard != guard:
                raise TopicError('等待期间知识或主题已变化，请重新发起整理。')
            if not points:
                plan = TopicPlan((), ())
            else:
                result = indexer.organize(points, existing)
                if result.failure is not None or result.plan is None:
                    raise TopicError('主题整理未完成，原主题保持不变。')
                plan = result.plan
            return self.commit(plan, guard)


def read_formal_points(connection, knowledge_ids=None):
    restriction = ''
    params = ()
    if knowledge_ids is not None:
        params = tuple(knowledge_ids)
        if not params:
            return []
        restriction = ' AND k.knowledge_result_id IN (' + ','.join('?' for _ in params) + ')'
    rows = connection.execute('''SELECT k.*, sf.snapshot, sf.material_id,
        m.source_kind, m.canonical_url, m.submitted_url, m.metadata_json,
        (SELECT MIN(i.item_id) FROM distill_items i WHERE i.material_id=m.material_id) AS item_id
        FROM knowledge_results k JOIN source_facts sf USING(source_fact_id)
        JOIN materials m ON m.material_id=sf.material_id
        WHERE k.published_path IS NOT NULL''' + restriction + ' ORDER BY k.knowledge_result_id', params).fetchall()
    points = []
    for row in rows:
        try:
            knowledge = knowledge_from_dict(row['snapshot'], json.loads(row['payload_json']))
            metadata = json.loads(row['metadata_json'])
            if not isinstance(metadata, dict):
                raise ValueError('invalid source metadata')
        except (ValueError, TypeError, KeyError) as error:
            raise TopicError('正式知识无法完整读取，本次未更新主题。') from error
        for role, members in (('core', knowledge.core_points), ('other', knowledge.other_points)):
            for point in members:
                points.append(dict(knowledge_result_id=row['knowledge_result_id'],
                    source_fact_id=row['source_fact_id'], point_id=point.point_id,
                    role=role, statement=point.statement, argument=point.argument,
                    title=knowledge.title, summary=knowledge.summary,
                    item_id=row['item_id'], source_kind=row['source_kind'],
                    canonical_url=row['canonical_url'], submitted_url=row['submitted_url'], metadata=metadata,
                    published_vault=row['published_vault'], published_path=row['published_path']))
    return points
