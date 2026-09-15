"""Durable manual-card identity, separate from media and mutable review counters.

Schema-18 upgrades populate the side table without rewriting old pending JSON.
All subsequent writes use the same mapping; reads never allocate identities.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from uuid import uuid4


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


STATEMENTS = (
    '''CREATE TABLE manual_cards (
        enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
        review_round_id TEXT NOT NULL, group_id TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','suspended','resolved','superseded')),
        ordering_basis TEXT NOT NULL CHECK(ordering_basis IN ('observed','migration_inferred')),
        ordering_reason TEXT NOT NULL, entered_at TEXT NOT NULL,
        mapping_json TEXT NOT NULL,
        UNIQUE(item_id,review_round_id,group_id))''',
    '''CREATE TABLE group_decisions (
        item_id INTEGER NOT NULL REFERENCES distill_items(item_id),
        request_id TEXT NOT NULL, group_id TEXT NOT NULL,
        submitted_revision TEXT NOT NULL, selection_digest TEXT NOT NULL,
        payload_digest TEXT NOT NULL, result_json TEXT NOT NULL,
        audit_json TEXT NOT NULL, committed_at TEXT NOT NULL,
        PRIMARY KEY(item_id,request_id),
        UNIQUE(item_id,submitted_revision,selection_digest))''',
)


def mappings(db, item_id):
    return [json.loads(r['mapping_json']) for r in db.execute(
        'SELECT mapping_json FROM manual_cards WHERE item_id=? ORDER BY enqueue_seq', (item_id,))]


def normalize(pending, item_id, previous=(), *, legacy=False):
    """Return a copy, retaining unknown fields and immutable mapping metadata."""
    result = deepcopy(dict(pending))
    identity = result.get('review_identity') or result.get('token')
    matching = [m for m in previous if m.get('review_identity') == identity] if identity else []
    round_id = result.get('review_round_id') or (matching[0]['review_round_id'] if matching else None)
    round_id = round_id or (digest(['legacy-round', item_id, identity]) if legacy else uuid4().hex)
    matching = [m for m in previous if m['review_round_id'] == round_id]
    original_hash = result.get('original_review_hash') or (matching[0].get('original_review_hash') if matching else None)
    original_hash = original_hash or digest(result.get('snapshot', ''))
    source_version = result.get('source_version_id') or (matching[0]['source_version_id'] if matching else None)
    source_version = source_version or digest(['review-source', item_id, original_hash, round_id])
    result.update(format_version=2, review_round_id=round_id,
                  source_version_id=source_version, original_review_hash=original_hash)
    known = {c['concern_uid']: c for m in matching for c in m.get('members', [])}
    all_members = result.get('concerns', []) + result.get('deferred_concerns', [])
    used = set()
    for ordinal, member in enumerate(all_members):
        uid = member.get('concern_uid')
        old = known.get(uid)
        if not uid:
            choices = [c for c in known.values() if c['concern_uid'] not in used
                       and c.get('audio_name') == member.get('audio_name')]
            if len(choices) != 1:
                choices = [c for c in known.values() if c['concern_uid'] not in used
                           and (c.get('start'), c.get('end'), c.get('text'), c.get('member_id')) ==
                           (member.get('start'), member.get('end'), member.get('text'), member.get('member_id'))]
            old = choices[0] if len(choices) == 1 else None
            uid = old['concern_uid'] if old else digest(['concern', item_id, round_id, ordinal, member.get('audio_name')])
        if uid in used:
            # The deferred representation may also be present during recovery.
            if any(c is not member and c.get('concern_uid') == uid for c in result.get('concerns', [])):
                continue
            raise ValueError('duplicate_concern_uid')
        used.add(uid)
        member['concern_uid'] = uid
        member.setdefault('media_member_id', member.get('member_id'))
        member.setdefault('source_version_id', source_version)
        member.setdefault('original_review_hash', original_hash)
        member.setdefault('original_span', old.get('original_span') if old else [member.get('start'), member.get('end')])
        member['current_span'] = [member.get('start'), member.get('end')]
        member.setdefault('evidence_refs', [])
        member['decision_revision'] = digest([source_version, round_id, uid, member.get('member_id'),
            member.get('text'), member.get('candidates'), member.get('evidence_refs'), member.get('decision_basis')])
        member['audio_revision'] = digest([member.get('audio_file', member.get('audio_name')),
            member.get('audio_recovery_required'), member.get('audio_range')])
    members = {c['concern_uid']: c for c in all_members}
    old_groups = {m['group']['group_id']: m['group'] for m in matching}
    groups = deepcopy(result.get('groups', []))
    if not groups:
        groups = deepcopy(list(old_groups.values()))
    covered, normalized_groups = set(), []
    for group in groups:
        selected = [u for u in group['member_uids'] if u in members]
        if not selected:
            continue
        if group['group_id'] in old_groups and not set(selected) <= set(old_groups[group['group_id']]['member_uids']):
            raise ValueError('published_group_cannot_expand')
        if covered.intersection(selected):
            raise ValueError('concern_in_multiple_groups')
        covered.update(selected)
        group['member_uids'] = selected
        normalized_groups.append(group)
    for uid in members:
        if uid not in covered:
            normalized_groups.append({'group_id': digest(['single', round_id, uid]), 'member_uids': [uid],
                'equivalence_basis': {'kind': 'single_member'}, 'formation_version': 1})
    if result.get('review_required') and not result.get('concerns') and not any(g.get('kind') == 'review' for g in normalized_groups):
        normalized_groups.append({'group_id': digest(['review', round_id]), 'member_uids': [],
            'kind': 'review', 'equivalence_basis': {'kind': 'single_review'}, 'formation_version': 1})
    for group in normalized_groups:
        group.update(review_round_id=round_id, source_version_id=source_version, published=True)
        group['group_revision'] = digest([round_id, source_version, group['group_id'],
            [(u, members[u]['decision_revision']) for u in group['member_uids']],
            group.get('equivalence_basis'), group.get('formation_version')])
    result['groups'] = normalized_groups
    return result


def view(db, row):
    if not row['confirmation_json']:
        return None
    return normalize(json.loads(row['confirmation_json']), row['item_id'], mappings(db, row['item_id']), legacy=True)


def prepare(db, item_id, pending):
    return normalize(pending, item_id, mappings(db, item_id))


def sync(db, item_id, *, migration=False):
    row = db.execute('SELECT * FROM distill_items WHERE item_id=?', (item_id,)).fetchone()
    if row is None:
        return
    if not row['confirmation_json']:
        db.execute("UPDATE manual_cards SET lifecycle='resolved' WHERE item_id=? AND lifecycle IN ('active','suspended')", (item_id,))
        return
    pending = view(db, row)
    membership = db.execute('SELECT operation_id FROM collection_members WHERE item_id=?', (item_id,)).fetchone()
    scope_kind, scope_id = ('collection', str(membership[0])) if membership else ('items', 'independent')
    db.execute("UPDATE manual_cards SET lifecycle='superseded' WHERE item_id=? AND review_round_id!=? AND lifecycle IN ('active','suspended')",
               (item_id, pending['review_round_id']))
    current_ids = {c['concern_uid'] for c in pending.get('concerns', [])}
    all_members = {c['concern_uid']: c for c in pending.get('concerns', []) + pending.get('deferred_concerns', [])}
    live_groups = []
    for group in pending['groups']:
        live_groups.append(group['group_id'])
        active = row['state'] == 'waiting_user' and row['dismissed_at'] is None and (
            bool(current_ids.intersection(group['member_uids'])) or group.get('kind') == 'review')
        mapping = {key: pending.get(key) for key in ('review_identity','review_round_id','source_version_id','original_review_hash')}
        mapping.update(group=group, members=[all_members[u] for u in group['member_uids']])
        reason = '投递时间、任务编号、原疑点顺序推定；非真实人工入队时间' if migration else '首次成为可操作判断项'
        try:
            datetime.fromisoformat(row['created_at'])
        except (ValueError, TypeError):
            if migration:
                reason += '；原投递时间缺失或无效，置于有效时间之后'
        db.execute('''INSERT INTO manual_cards
            (scope_kind,scope_id,item_id,review_round_id,group_id,lifecycle,ordering_basis,ordering_reason,entered_at,mapping_json)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id,review_round_id,group_id) DO UPDATE SET
            lifecycle=excluded.lifecycle,mapping_json=excluded.mapping_json,scope_kind=excluded.scope_kind,scope_id=excluded.scope_id''',
            (scope_kind,scope_id,item_id,pending['review_round_id'],group['group_id'],'active' if active else 'suspended',
             'migration_inferred' if migration else 'observed', reason, row['created_at'] if migration else row['updated_at'], encoded(mapping)))
    for card in db.execute('SELECT group_id FROM manual_cards WHERE item_id=? AND review_round_id=?', (item_id,pending['review_round_id'])).fetchall():
        if card['group_id'] not in live_groups:
            lifecycle = 'superseded' if card['group_id'] in pending.get('superseded_group_ids', []) else 'resolved'
            db.execute("UPDATE manual_cards SET lifecycle=? WHERE item_id=? AND review_round_id=? AND group_id=?", (lifecycle,item_id,pending['review_round_id'],card['group_id']))


def migrate(db):
    for statement in STATEMENTS:
        db.execute(statement)
    rows = db.execute("SELECT * FROM distill_items WHERE confirmation_json IS NOT NULL ORDER BY CASE WHEN julianday(created_at) IS NULL THEN 1 ELSE 0 END,julianday(created_at),item_id").fetchall()
    for row in rows:
        sync(db, row['item_id'], migration=True)
