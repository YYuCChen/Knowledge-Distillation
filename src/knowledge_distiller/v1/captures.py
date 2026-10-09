"""Feishu quick notes (docs/roadmap/handoff-feishu-capture.md, raw-interface §5.2).

A capture is a message the user sends to the bound private chat that is not a
material delivery: plain text without links, or a voice message. It is stored
once and never edited, together with the earlier deliveries it followed
(adjacency cannot be reconstructed later). Its identity comes from rules, then
from an optional cloud judge; an undecided message never defaults to the user
and waits for the desk. Only a decided, recognition-checked capture is written
to raw/自述. Third-party text follows the existing direct-text material path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import time

from . import raw
from .database import connect


logger = logging.getLogger(__name__)

IDENTITIES = {'my_thought': '本人', 'annotation': '本人附言', 'third_party': '第三方'}
SETTLE_HOURS = 24
DEFAULT_WINDOW_MINUTES = 30
WINDOW_SETTING = 'capture_adjacency_minutes'
JEV_FAILED = 'Jev 失败·'  # Basis prefix of a pending event after a Jev error.

def now_ms() -> int:
    return int(time.time() * 1000)


def _local(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone()


# ───────────────────────── At receive time ─────────────────────────

def record_adjacency(db, app_id: str, message_id: str, created_ms: int, window_minutes: int) -> None:
    """Earlier deliveries in the same chat within the window; each later message records the earlier."""
    window = window_minutes * 60_000
    for row in db.execute('''SELECT message_id, created_ms FROM feishu_receipts
            WHERE app_id=? AND message_id!=? AND created_ms BETWEEN ? AND ?''',
            (app_id, message_id, created_ms - window, created_ms)):
        if row['created_ms'] < created_ms or (row['created_ms'] == created_ms and row['message_id'] < message_id):
            db.execute('''INSERT OR IGNORE INTO delivery_adjacency(app_id, message_id, earlier_message_id, gap_seconds)
                          VALUES (?,?,?,?)''', (app_id, message_id, row['message_id'], (created_ms - row['created_ms']) // 1000))
    # A delivery that arrives late (history backfill) is still "earlier" for later ones.
    for row in db.execute('''SELECT message_id, created_ms FROM feishu_receipts
            WHERE app_id=? AND message_id!=? AND created_ms BETWEEN ? AND ?''',
            (app_id, message_id, created_ms, created_ms + window)):
        if row['created_ms'] > created_ms or (row['created_ms'] == created_ms and row['message_id'] > message_id):
            db.execute('''INSERT OR IGNORE INTO delivery_adjacency(app_id, message_id, earlier_message_id, gap_seconds)
                          VALUES (?,?,?,?)''', (app_id, row['message_id'], message_id, (row['created_ms'] - created_ms) // 1000))


def record_capture(db, app_id: str, message_id: str, *, message_type: str, created_ms: int, received_ms: int,
                   text: str | None = None, file_key: str | None = None, duration_ms: int | None = None,
                   vault: Path | None = None) -> None:
    """The immutable capture, with a raw id reserved now so later files can refer to it."""
    import sqlite3
    from .ingestion import IngestionError, require_legacy_message
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    raw._source_inventory(db)
    try:
        require_legacy_message(db, app_id, message_id)
    except IngestionError as error:
        code = 'candidate_schema_rebuild_required' if error.args == ('candidate_schema_rebuild_required',) else 'local_source_qualification_pending'
        raise raw.LegacySourceVeto(code) from None
    except sqlite3.DatabaseError:
        raise raw.LegacySourceVeto('candidate_schema_rebuild_required') from None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise raw.LegacySourceVeto('local_source_qualification_pending') from None
    if db.execute('SELECT 1 FROM captures WHERE app_id=? AND message_id=?', (app_id, message_id)).fetchone():
        return
    day = _local(received_ms).strftime('%Y%m%d')
    raw_id = raw.allocate(db, day, vault if vault is not None and vault.is_dir() else None)
    capture_id = db.execute('''INSERT INTO captures(app_id, message_id, message_type, created_ms, received_ms, text,
            file_key, duration_ms, raw_id) VALUES (?,?,?,?,?,?,?,?,?)''',
        (app_id, message_id, message_type, created_ms, received_ms, text, file_key, duration_ms, raw_id)).lastrowid
    db.execute('INSERT INTO capture_state(capture_id) VALUES (?)', (capture_id,))


# ───────────────────────── Identity ─────────────────────────

def rule_judgment(capture, *, recent_delivery):
    """(result, basis, confidence, target) or None when rules cannot decide."""
    if capture['message_type'] == 'audio':
        return 'my_thought', '规则·语音默认本人', 1.0, None
    # Length, formatting, quotations and adjacency do not establish authorship
    # or a unique annotation target. Text goes through the bounded candidate.
    return None


class JevIdentityJudge:
    """Two independent dimensions through the existing active/legacy ask API.

    Jev retains the approved selected-probability thresholds. Clef confidence
    is not a calibrated Jev probability and cannot reuse that automatic policy.
    """

    def __init__(self, client):
        self.client = client
        self.last = None
        self.answers = None
        self.prepared = None

    def judge(self, source, *, targets=(), scope_complete=True):
        from .capture_identity_context import CaptureSource, prepare_identity_context
        from .decision_client import DecisionProfile, JEV_ENDPOINT
        from .jev import JevChoice, JevError
        if not isinstance(source, CaptureSource):
            raise JevError('decision_request_invalid')
        active = getattr(self.client, 'client', None)
        profile = getattr(active, 'profile', None)
        if profile is None:
            profile = DecisionProfile('jev', getattr(self.client, 'endpoint', JEV_ENDPOINT),
                getattr(self.client, 'model', 'jev-latest'), auth_ref='jev-api-key')
        self.prepared = prepare_identity_context(source, targets, profile=profile,
            profile_version=getattr(self.client, 'profile_id', 'legacy-jev'),
            scope_complete=scope_complete, excluded_count=int(not scope_complete))
        request = self.prepared.request()
        if request is None:
            raise JevError('decision_budget_exceeded')
        state, questions = request
        answers, model = self.client.ask(state, {key: q.wire() for key, q in questions.items()})
        try:
            if set(answers) != set(questions) or not isinstance(model, str) or not model:
                raise ValueError
            if profile.provider == 'clef' and model != profile.model:
                raise ValueError
            parsed = {}
            semantics = ('clef-max-probability' if profile.provider == 'clef'
                         else 'jev-normalized-concentration')
            for key, question in questions.items():
                answer = answers[key]
                probabilities = answer['probabilities']
                confidence = answer['confidence']
                if (answer.get('type') != 'choice' or set(probabilities) != set(question.criteria)
                        or answer['choice'] not in probabilities
                        or any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
                               for p in probabilities.values())
                        or abs(sum(probabilities.values()) - 1) > .02
                        or type(confidence) not in (int, float) or not math.isfinite(confidence)
                        or not 0 <= confidence <= 1
                        or answer.get('provider', profile.provider) != profile.provider
                        or answer.get('confidence_semantics', semantics) != semantics):
                    raise ValueError
                parsed[key] = JevChoice(answer['choice'], probabilities[answer['choice']],
                    dict(probabilities), confidence, model, profile.provider, semantics)
            self.answers = parsed
            self.last = parsed['author_identity']
        except (KeyError, TypeError, ValueError, AttributeError):
            raise JevError('decision_response_invalid') from None
        author, relation = parsed['author_identity'], parsed['relation_target']
        if profile.provider != 'jev' or not self.prepared.scope_complete or relation.probability < .8:
            return None
        basis = 'Jev·' + model
        if relation.choice == 'independent':
            if author.choice == 'self' and author.probability >= .9:
                return 'my_thought', basis, author.probability, None
            if author.choice == 'third_party' and author.probability >= .8:
                return 'third_party', basis, author.probability, None
        elif author.choice == 'self' and author.probability >= .8:
            # A selected stable ID is necessary, but recency cannot resolve a
            # multi-target ambiguity. No fallback to recent_delivery here.
            if len(self.prepared.targets) == 1:
                target = self.prepared.targets[0]
                if relation.choice == target.candidate_id and target.item_id is not None:
                    return 'annotation', basis, relation.probability, target.message_id
        return None


class Captures:
    def __init__(self, store, *, jev=None, api=None, audio_root: Path | None = None):
        self.store = store
        self.jev = jev  # () -> JevClient | None (SettingsService.jev_client)
        self.api = api
        self.audio_root = audio_root or store.path.parent / 'runtime' / 'captures'

    # ── lookups ──
    def get(self, capture_id):
        with connect(self.store.path) as db:
            row = db.execute('''SELECT c.*, s.item_id, s.audio_path, s.audio_released_at FROM captures c
                                JOIN capture_state s USING(capture_id) WHERE capture_id=?''', (capture_id,)).fetchone()
            return dict(row) if row else None

    def for_message(self, app_id, message_id):
        with connect(self.store.path) as db:
            row = db.execute('SELECT capture_id FROM captures WHERE app_id=? AND message_id=?', (app_id, message_id)).fetchone()
        return self.get(row['capture_id']) if row else None

    def for_item(self, item_id):
        with connect(self.store.path) as db:
            row = db.execute('SELECT capture_id FROM capture_state WHERE item_id=?', (item_id,)).fetchone()
        return self.get(row['capture_id']) if row else None

    def events(self, capture_id):
        with connect(self.store.path) as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM capture_identity_events WHERE capture_id=? ORDER BY event_id', (capture_id,))]

    def identity(self, capture_id):
        events = self.events(capture_id)
        return events[-1] if events else None

    def pending(self):
        """Captures waiting for the user at the desk, oldest first."""
        with connect(self.store.path) as db:
            rows = db.execute('''SELECT c.capture_id FROM captures c JOIN capture_identity_events e USING(capture_id)
                WHERE e.event_id=(SELECT MAX(event_id) FROM capture_identity_events WHERE capture_id=c.capture_id)
                AND e.result='pending' ORDER BY c.received_ms, c.capture_id''').fetchall()
        return [self.get(row['capture_id']) for row in rows]

    def recent_delivery(self, capture):
        """The latest earlier non-capture delivery (existing UI/default behavior)."""
        with connect(self.store.path) as db:
            row = db.execute('''SELECT a.earlier_message_id FROM delivery_adjacency a
                JOIN feishu_receipts r ON r.app_id=a.app_id AND r.message_id=a.earlier_message_id
                WHERE a.app_id=? AND a.message_id=? AND r.state IN ('received','waiting_input','accepted','needs_desktop')
                AND NOT EXISTS (SELECT 1 FROM captures c WHERE c.app_id=a.app_id AND c.message_id=a.earlier_message_id)
                ORDER BY a.gap_seconds LIMIT 1''',
                (capture['app_id'], capture['message_id'])).fetchone()
        return row['earlier_message_id'] if row else None

    def identity_context(self, capture):
        """At most eight same-app adjacent parts; ninth marks an incomplete scope.

        Titles/summaries are excerpts of existing fields, never generated here.
        Candidate version binds part/item and the supplied context snapshot.
        """
        from .capture_identity_context import CaptureSource, TargetCandidate, ReferenceEvidence
        data = capture['text'].encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        source = CaptureSource(capture['app_id'], capture['message_id'], str(capture['capture_id']),
            digest, data, digest)
        with connect(self.store.path) as db:
            rows = db.execute('''SELECT a.earlier_message_id, p.position, p.item_id,
                    i.submitted_title, i.submitted_url, m.metadata_json,
                    k.knowledge_result_id, k.payload_json
                FROM delivery_adjacency a
                JOIN feishu_receipts r ON r.app_id=a.app_id AND r.message_id=a.earlier_message_id
                LEFT JOIN feishu_parts p ON p.app_id=r.app_id AND p.message_id=r.message_id
                LEFT JOIN distill_items i ON i.item_id=p.item_id
                LEFT JOIN materials m ON m.material_id=i.material_id
                LEFT JOIN source_facts f ON f.material_id=m.material_id
                LEFT JOIN knowledge_results k ON k.source_fact_id=f.source_fact_id
                WHERE a.app_id=? AND a.message_id=?
                AND r.state IN ('received','waiting_input','accepted','needs_desktop')
                AND NOT EXISTS(SELECT 1 FROM captures c WHERE c.app_id=r.app_id AND c.message_id=r.message_id)
                ORDER BY a.gap_seconds, a.earlier_message_id, p.position LIMIT 9''',
                (capture['app_id'], capture['message_id'])).fetchall()
        targets = []
        for row in rows[:8]:
            def fields(value):
                try:
                    value = json.loads(value or '{}')
                    return value if type(value) is dict else {}
                except (ValueError, TypeError):
                    return {}
            payload, metadata = fields(row['payload_json']), fields(row['metadata_json'])
            title = next((v for v in (payload.get('title'), row['submitted_title'], metadata.get('title'))
                          if isinstance(v, str) and v.strip()), None)
            summary = payload.get('summary')
            summary = summary[:1024] if isinstance(summary, str) and summary.strip() else None
            title = title[:256] if title else None
            part = str(row['position']) if row['position'] is not None else 'message'
            version = hashlib.sha256(json.dumps([capture['app_id'], row['earlier_message_id'], part,
                row['item_id'], title, summary, row['knowledge_result_id']],
                ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
            ref = f"feishu:{capture['app_id']}:{row['earlier_message_id']}:{part}"
            url = row['submitted_url']
            literals = (url,) if isinstance(url, str) and re.fullmatch(r'https?://[^\s]+', url) else ()
            target = TargetCandidate(capture['app_id'], row['earlier_message_id'], part, version, ref,
                title=title, summary=summary,
                summary_provenance=f"knowledge-result:{row['knowledge_result_id']}:summary:prefix1024" if summary else None,
                literal_refs=literals, item_id=row['item_id'])
            evidence = [ReferenceEvidence('adjacency', digest, target.candidate_id, version, ref)]
            for literal in literals:
                start = capture['text'].find(literal)
                if start >= 0:
                    evidence.append(ReferenceEvidence('literal', digest, target.candidate_id,
                        version, ref, start, start + len(literal)))
            targets.append(replace(target, evidence=tuple(evidence)))
        return source, tuple(targets), len(rows) <= 8

    # ── decisions ──
    def _event(self, capture_id, result, basis, confidence=None, target=None):
        with connect(self.store.path) as db:
            db.execute('''INSERT INTO capture_identity_events(capture_id, result, basis, confidence, target_message_id,
                          created_at) VALUES (?,?,?,?,?,?)''',
                       (capture_id, result, basis, confidence, target, datetime.now(UTC).isoformat()))

    def _first_judgment(self, capture_id, result, basis, confidence=None, target=None, *, prepared=None):
        """Do not overwrite a user decision or another judgment made in flight."""
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM capture_identity_events WHERE capture_id=?', (capture_id,)).fetchone():
                return
            if prepared is not None:
                # The write lock excludes target changes while the current
                # source/candidate versions are read back before accepting Jev.
                source, targets, complete = self.identity_context(self.get(capture_id))
                if (source != prepared.source or not complete or
                        {t.candidate_id: t.version for t in targets} !=
                        {t.candidate_id: t.version for t in prepared.targets}):
                    result, target, confidence = 'pending', None, None
            db.execute('''INSERT INTO capture_identity_events(capture_id,result,basis,confidence,target_message_id,created_at)
                VALUES(?,?,?,?,?,?)''', (capture_id, result, basis, confidence, target, datetime.now(UTC).isoformat()))

    def judge(self, capture):
        """Only new captures: voice rule or bounded two-dimensional judgment.

        Existing events, including old pending, are never automatically retried.
        Jev may decide at approved thresholds; ambiguity/Clef remain pending.
        """
        if self.identity(capture['capture_id']) is not None:
            return self.identity(capture['capture_id'])
        decided = rule_judgment(capture, recent_delivery=None)
        prepared = None
        client = self.jev() if decided is None and self.jev is not None else None
        if decided is None and client is not None:
            from .jev import JevError
            judge = JevIdentityJudge(client)
            try:
                source, targets, complete = self.identity_context(capture)
                decided = judge.judge(source, targets=targets, scope_complete=complete)
                prepared = judge.prepared
            except JevError as error:
                logger.error('capture %s Jev judgment failed (%s)', capture['capture_id'], error)
                self._first_judgment(capture['capture_id'], 'pending', JEV_FAILED + str(error))
                return self.identity(capture['capture_id'])
            if decided is None:
                provider = 'Jev' if judge.last.provider == 'jev' else 'Clef'
                self._first_judgment(capture['capture_id'], 'pending', f'{provider}·{judge.last.model}',
                            judge.last.probability)
                return self.identity(capture['capture_id'])
        if decided is None:
            self._first_judgment(capture['capture_id'], 'pending', '规则未决·未配置 Jev')
        else:
            self._first_judgment(capture['capture_id'], decided[0], decided[1], decided[2], decided[3],
                                prepared=prepared)
        return self.identity(capture['capture_id'])

    def decide(self, capture_id, result, *, target=None):
        """The user's decision is a new event; earlier judgments stay as they were."""
        if result not in IDENTITIES:
            raise ValueError('请选择这条随手记的身份。')
        capture = self.get(capture_id)
        if capture is None:
            raise LookupError(capture_id)
        if capture['message_type'] == 'audio' and result == 'third_party':
            raise ValueError('语音随手记按本人的话保存，不能改为第三方内容。')
        if result == 'annotation':
            target = target or self.recent_delivery(capture)
            if target is None:
                raise ValueError('这条随手记前面没有可附言的投递。')
            with connect(self.store.path) as db:
                if not db.execute('SELECT 1 FROM feishu_receipts WHERE app_id=? AND message_id=?',
                                  (capture['app_id'], target)).fetchone():
                    raise ValueError('这条随手记前面没有可附言的投递。')
        else:
            target = None
        written = self._written(capture_id)
        previous = self.identity(capture_id)
        self._event(capture_id, result, '用户', 1.0, target)
        if written is not None and (IDENTITIES[result] != written['identity'] or
                (result == 'annotation' and previous and previous['target_message_id'] != target)):
            self._supersede(capture, written, result)
        self.advance(capture)

    def advance(self, capture):
        """Start whatever the decided identity needs; safe to repeat."""
        decided = self.identity(capture['capture_id'])
        if decided is None or decided['result'] == 'pending':
            return
        if decided['result'] == 'third_party' and capture['message_type'] == 'text' and capture['item_id'] is None:
            from .file_sources import prepare_direct_text
            item = self.store.submit_source(prepare_direct_text(capture['text']))
            self._link(capture['capture_id'], item)
        elif decided['result'] == 'my_thought' and capture['message_type'] == 'audio' and capture['item_id'] is None:
            self._start_voice(capture)

    def _link(self, capture_id, item_id):
        with connect(self.store.path) as db:
            db.execute('UPDATE capture_state SET item_id=? WHERE capture_id=? AND item_id IS NULL', (item_id, capture_id))

    # ── voice ──
    def _start_voice(self, capture):
        path = Path(capture['audio_path']) if capture['audio_path'] else None
        if path is None or not path.is_file():
            if self.api is None:
                raise ValueError('capture_audio_unavailable')
            content = self.api.download_message_file(capture['message_id'], capture['file_key'])
            if not content:
                raise ValueError('capture_audio_unavailable')
            self.audio_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.audio_root / f"{capture['capture_id']}.opus"
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            temporary.replace(path)
            with connect(self.store.path) as db:
                db.execute('UPDATE capture_state SET audio_path=? WHERE capture_id=?', (str(path), capture['capture_id']))
        item = self.store.create_item(voice_url(capture), title='飞书语音')
        self._link(capture['capture_id'], item)

    def record_transcript(self, capture_id, recovery, *, engine, model, version):
        from dataclasses import asdict
        with connect(self.store.path) as db:
            db.execute('''INSERT OR IGNORE INTO capture_transcripts(capture_id, text, engine, model, version, chunks_json,
                          created_at) VALUES (?,?,?,?,?,?,?)''',
                       (capture_id, recovery.text, engine, model, version,
                        json.dumps([asdict(chunk) for chunk in recovery.chunks], ensure_ascii=False),
                        datetime.now(UTC).isoformat()))

    def transcript(self, capture_id):
        with connect(self.store.path) as db:
            row = db.execute('SELECT * FROM capture_transcripts WHERE capture_id=?', (capture_id,)).fetchone()
        return dict(row) if row else None

    def release_audio(self, capture):
        """Recognition concerns are handled: drop the recording, keep the fact it existed."""
        if capture['message_type'] != 'audio' or capture['audio_released_at']:
            return
        # Legacy callers do not possess locked readback authority. New-contract
        # recordings remain retained, including cancellation and pending ASR.
        if capture['item_id']:
            item = self.store.item_bundle(capture['item_id'])
            if item is not None and item['ingestion_contract'] != 'legacy':
                return
        with connect(self.store.path) as db:
            if db.execute("SELECT 1 FROM ingestion_events WHERE subject_kind='capture' AND subject_id=? LIMIT 1",
                          (capture['capture_id'],)).fetchone():
                return
        if capture['audio_path']:
            Path(capture['audio_path']).unlink(missing_ok=True)
        with connect(self.store.path) as db:
            db.execute('UPDATE capture_state SET audio_released_at=? WHERE capture_id=? AND audio_released_at IS NULL',
                       (datetime.now(UTC).isoformat(), capture['capture_id']))

    # ── raw ──
    def _written(self, capture_id):
        with connect(self.store.path) as db:
            row = db.execute('''SELECT * FROM raw_records r WHERE subject_kind='capture' AND subject_id=?
                AND NOT EXISTS (SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id)
                ORDER BY raw_id DESC LIMIT 1''', (capture_id,)).fetchone()
        return dict(row) if row else None

    def raw_id_of_message(self, db, app_id, message_id):
        """(raw id, settled) for an earlier delivery: the id to cite, or None when there is none."""
        capture = db.execute('SELECT capture_id, raw_id FROM captures WHERE app_id=? AND message_id=?',
                             (app_id, message_id)).fetchone()
        if capture is not None:
            item = db.execute('SELECT item_id FROM capture_state WHERE capture_id=?', (capture['capture_id'],)).fetchone()
            material_raw = self._material_raw(db, item['item_id']) if item and item['item_id'] else None
            written = db.execute('''SELECT raw_id FROM raw_records WHERE subject_kind='capture' AND subject_id=?
                                    ORDER BY raw_id DESC LIMIT 1''', (capture['capture_id'],)).fetchone()
            # A capture's reserved id is always cited; it becomes its file once decided.
            return (written['raw_id'] if written else material_raw or capture['raw_id']), True
        ids, settled = [], True
        for part in db.execute('SELECT item_id FROM feishu_parts WHERE app_id=? AND message_id=? AND item_id IS NOT NULL',
                               (app_id, message_id)):
            found = self._material_raw(db, part['item_id'])
            if found:
                ids.append(found)
                continue
            item = db.execute('SELECT state, dismissed_at FROM distill_items WHERE item_id=?', (part['item_id'],)).fetchone()
            if item is not None and item['state'] != 'failed' and item['dismissed_at'] is None:
                settled = False
        receipt = db.execute('SELECT state FROM feishu_receipts WHERE app_id=? AND message_id=?', (app_id, message_id)).fetchone()
        if receipt is not None and receipt['state'] in {'received', 'waiting_input'}:
            settled = False
        return (ids[0] if ids else None), settled

    def message_raws(self, db, app_id, message_id):
        """Complete ordered objects for explicit ingestion, no allocation/write.

        Reserved IDs, errors, dismissal and missing SourceFacts are pending.
        Distinct parts may have distinct raw; multiple heads of one object are
        ambiguous. The old scalar projection remains unchanged for legacy UI.
        """
        receipt = db.execute('SELECT state FROM feishu_receipts WHERE app_id=? AND message_id=?',
                             (app_id, message_id)).fetchone()
        if receipt is not None and receipt['state'] != 'accepted':
            return (), 'message_raw_pending'
        capture = db.execute('''SELECT c.*,s.item_id FROM captures c JOIN capture_state s USING(capture_id)
            WHERE c.app_id=? AND c.message_id=?''', (app_id, message_id)).fetchone()
        parts = db.execute('SELECT position,item_id,error FROM feishu_parts WHERE app_id=? AND message_id=? ORDER BY position',
                           (app_id, message_id)).fetchall()
        if any(p['item_id'] is None or p['error'] for p in parts):
            return (), 'message_raw_pending'
        objects, own = [], None
        def heads(kind, subject):
            return tuple(dict(r) for r in db.execute('''SELECT r.* FROM raw_records r WHERE subject_kind=? AND subject_id=?
                AND NOT EXISTS(SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id) ORDER BY raw_id''', (kind, subject)))
        if capture is not None:
            decision = db.execute('SELECT * FROM capture_identity_events WHERE capture_id=? ORDER BY event_id DESC LIMIT 1',
                                  (capture['capture_id'],)).fetchone()
            current = heads('capture', capture['capture_id'])
            if decision is None or decision['result'] not in {'my_thought', 'annotation', 'third_party'}:
                return (), 'message_raw_pending'
            if decision['result'] == 'third_party':
                if current or capture['item_id'] is None:
                    return (), 'message_raw_pending'
            else:
                if len(current) != 1:
                    return (), 'message_raw_ambiguous' if len(current) > 1 else 'message_raw_pending'
                if current[0]['identity'] != IDENTITIES[decision['result']]:
                    return (), 'message_raw_pending'
                own = current[0]
                objects.append({'ordinal': -1, 'item_id': capture['item_id'], 'record': own})
        inputs = [(p['position'], p['item_id']) for p in parts]
        if capture is not None and capture['item_id'] is not None and capture['item_id'] not in {i for _, i in inputs}:
            inputs.append((-1, capture['item_id']))
        seen = {own['raw_id']} if own else set()
        for ordinal, item_id in inputs:
            item = db.execute('''SELECT i.*,m.source_kind,sf.source_fact_id FROM distill_items i
                LEFT JOIN materials m USING(material_id) LEFT JOIN source_facts sf USING(material_id) WHERE item_id=?''',
                              (item_id,)).fetchone()
            if (item is None or item['source_fact_id'] is None or item['confirmation_json'] is not None
                    or item['state'] == 'failed' or item['dismissed_at'] is not None):
                return (), 'message_raw_pending'
            if own is not None and item_id == capture['item_id']:
                if heads('material', item['material_id']):
                    return (), 'message_raw_ambiguous'
                continue  # Latest own identity, no obsolete external head.
            if item['source_kind'] == 'feishu_voice':
                return (), 'message_raw_pending'  # No matching own capture object.
            current = heads('material', item['material_id'])
            if len(current) != 1:
                return (), 'message_raw_ambiguous' if len(current) > 1 else 'message_raw_pending'
            if current[0]['raw_id'] not in seen:
                objects.append({'ordinal': ordinal, 'item_id': item_id, 'record': current[0]})
                seen.add(current[0]['raw_id'])
        return tuple(objects), None if objects else 'message_raw_pending'

    @staticmethod
    def _material_raw(db, item_id):
        row = db.execute('''SELECT r.raw_id FROM distill_items i JOIN raw_records r
            ON r.subject_kind='material' AND r.subject_id=i.material_id WHERE i.item_id=?
            ORDER BY r.raw_id DESC LIMIT 1''', (item_id,)).fetchone()
        return row['raw_id'] if row else None

    def adjacency(self, app_id, message_id):
        """[{编号, 间隔秒}] for earlier deliveries, plus how many are not settled yet."""
        with connect(self.store.path) as db:
            rows = db.execute('''SELECT earlier_message_id, gap_seconds FROM delivery_adjacency
                WHERE app_id=? AND message_id=? ORDER BY gap_seconds DESC, earlier_message_id''', (app_id, message_id)).fetchall()
            listed, unsettled = [], 0
            for row in rows:
                raw_id, settled = self.raw_id_of_message(db, app_id, row['earlier_message_id'])
                if raw_id:
                    listed.append({'编号': raw_id, '间隔秒': row['gap_seconds']})
                elif not settled:
                    unsettled += 1
        return listed, unsettled

    def material_hints(self, item_id):
        """For ensure_material: a capture's reserved id and time, and the delivery's adjacency."""
        capture = self.for_item(item_id)
        with connect(self.store.path) as db:
            part = db.execute('SELECT app_id, message_id FROM feishu_parts WHERE item_id=? LIMIT 1', (item_id,)).fetchone()
        if capture is not None:
            adjacency, _ = self.adjacency(capture['app_id'], capture['message_id'])
            hints = {'collected_ms': capture['received_ms'], 'adjacency': adjacency}
            written = self._written(capture['capture_id'])
            if written is None:
                hints['reserved'] = capture['raw_id']
            else:  # It was filed as the user's own words first; the new file supersedes it.
                hints['supersedes'] = written['raw_id']
            return hints
        if part is not None:
            adjacency, _ = self.adjacency(part['app_id'], part['message_id'])
            return {'adjacency': adjacency}
        return {}

    def ready(self):
        """Decided own-words captures whose content is final and whose raw is not assigned yet."""
        with connect(self.store.path) as db:
            rows = db.execute('''SELECT c.capture_id FROM captures c
                WHERE NOT EXISTS (SELECT 1 FROM raw_records r WHERE r.subject_kind='capture' AND r.subject_id=c.capture_id)
                ORDER BY c.received_ms, c.capture_id''').fetchall()
        result = []
        for row in rows:
            capture = self.get(row['capture_id'])
            decided = self.identity(capture['capture_id'])
            if decided is None or decided['result'] not in {'my_thought', 'annotation'}:
                continue
            if capture['message_type'] == 'audio':
                item = self.store.item_bundle(capture['item_id']) if capture['item_id'] else None
                if item is None or item['state'] != 'succeeded' or item['source_fact_id'] is None:
                    continue  # Recognition or its concerns are not finished.
            result.append((capture, decided))
        return result

    def ensure_raw(self, capture, decided, adjacency, target_id, *, ledger=None, unsettled=0, existing_ok=True,
                   check_source=None):
        """Assign using the existing reserved ID/render/insert; never release.

        The default legacy gate checks actual owners/relations before render.
        The explicit ingestion caller adds source CAS and holds the Vault lock.
        Re-read the decision and capture in the same assignment txn;
        stale existing heads are returned for the caller to reject, not fixed
        by silently making a new version.
        """
        ledger = ledger or raw.RawLedger(self.store)
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM capture_identity_events WHERE capture_id=? ORDER BY event_id DESC LIMIT 1',
                                 (capture['capture_id'],)).fetchone()
            if current is None or dict(current) != dict(decided) or current['result'] not in {'my_thought', 'annotation'}:
                raise raw.RawError('capture_decision_changed')
            row = db.execute('''SELECT c.*, s.item_id, s.audio_path, s.audio_released_at FROM captures c
                                JOIN capture_state s USING(capture_id) WHERE capture_id=?''',
                             (capture['capture_id'],)).fetchone()
            fresh = dict(row) if row else None
            if fresh != dict(capture):
                raise raw.RawError('capture_source_changed')
            raw._legacy_source_gate(db, 'capture', capture['capture_id'],
                                    referenced_raw_ids=raw._reference_ids(adjacency, target_id))
            if check_source is not None:
                check_source(db)  # Read-only ingestion CAS, before freezing bytes.
            heads = db.execute("""SELECT r.raw_id FROM raw_records r WHERE subject_kind='capture' AND subject_id=?
                AND NOT EXISTS(SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id)""",
                               (capture['capture_id'],)).fetchall()
            if len(heads) > 1:
                raise raw.RawError('capture_heads_ambiguous')
            if heads:
                if not existing_ok:
                    return None  # Original write_ready concurrently-assigned skip.
                raw_id = heads[0]['raw_id']
            else:
                if db.execute('SELECT 1 FROM raw_records WHERE raw_id=?', (capture['raw_id'],)).fetchone():
                    raise raw.RawError('capture_id_assigned_elsewhere')
                document = self.render(capture, decided, adjacency, unsettled, target_id)
                raw_id = capture['raw_id']
                raw.insert(db, raw_id, 'capture', capture['capture_id'], IDENTITIES[decided['result']],
                           document, origin='app')
        return ledger.record(raw_id)

    def write_ready(self, ledger=None, *, now=None) -> dict:
        ledger = ledger or raw.RawLedger(self.store)
        now = now or datetime.now(UTC)
        results = {}
        for capture, decided in self.ready():
            try:
                adjacency, unsettled = self.adjacency(capture['app_id'], capture['message_id'])
                target_id, target_settled = None, True
                if decided['result'] == 'annotation':
                    with connect(self.store.path) as db:
                        target_id, target_settled = self.raw_id_of_message(db, capture['app_id'], decided['target_message_id'])
                young = now - _local(capture['received_ms']) < timedelta(hours=SETTLE_HOURS)
                if young and (unsettled or not target_settled or (decided['result'] == 'annotation' and target_id is None)):
                    continue  # Earlier deliveries are still being processed; cite them once they have raw ids.
                record = self.ensure_raw(capture, decided, adjacency, target_id, ledger=ledger,
                                         unsettled=unsettled if not young else 0, existing_ok=False)
                if record is None:
                    continue
                results[capture['raw_id']] = ledger.write(record)
                if results[capture['raw_id']] in {'placed', 'already'}:
                    self.release_audio(capture)
            except raw.LegacySourceVeto as error:
                if error.args != ('local_source_qualification_pending',):
                    raise
                results[capture['raw_id']] = 'local_source_qualification_pending'
        return results

    def render(self, capture, decided, adjacency, unsettled, target_id, *, raw_id=None, supersedes=None, now=None):
        raw_id = raw_id or capture['raw_id']
        identity = IDENTITIES[decided['result']]
        collected = now or _local(capture['received_ms'])
        acquisition = {'采集': 'feishu', '应用版本': raw.app_version()}
        record = {'capture_id': capture['capture_id']}
        fixed, doubtful = [], []
        if capture['message_type'] == 'audio':
            transcript = self.transcript(capture['capture_id']) or {}
            acquisition['识别'] = ' '.join(filter(None, (transcript.get('model'), transcript.get('version')))) or '未记录'
            item = self.store.item_bundle(capture['item_id'])
            text, lineage = item['snapshot'], json.loads(item['lineage_json'] or '{}')
            fixed, doubtful = raw.corrections(text, lineage, json.loads(item['uncertainties_json'] or '[]'))
            record['material_id'] = item['material_id']
        else:
            text, lineage = capture['text'], {}
        lines, _, _ = raw.body(text, lineage, raw_id, {})
        users = [event for event in self.events(capture['capture_id']) if event['basis'] == '用户']
        judged = {'结果': identity, '依据': decided['basis'], '置信度': decided['confidence'] if decided['confidence'] is not None else 1.0,
                  '用户改判': '有' if users else '无'}
        fields = [
            ('编号', raw_id), ('格式版本', raw.FORMAT_VERSION), ('身份', identity),
            ('标题', ' '.join(text.split())[:20] or '（空白）'), ('作者', '本人'),
            ('渠道', '飞书语音' if capture['message_type'] == 'audio' else '飞书文字'),
            ('产生于', raw._iso(_local(capture['created_ms']))), ('收录于', raw._iso(collected)),
            ('取得方式', acquisition), ('订正', fixed), ('存疑', doubtful), ('身份判定', judged),
            ('邻接', adjacency), ('邻接未定', unsettled or None),
            ('附言对象', target_id if identity == '本人附言' else None), ('取代', supersedes), ('应用记录', record),
        ]
        content = '\n'.join(raw.envelope(fields) + [''] + lines).rstrip('\n') + '\n'
        return raw.RawDocument(raw.relative_path(raw_id, identity, collected), content)

    def _supersede(self, capture, written, result):
        """A changed decision after writing: a new file that supersedes the old one."""
        if result == 'third_party':
            # It leaves the personal layer as a material; material_hints makes
            # that material's raw supersede the file written here before.
            return
        decided = self.identity(capture['capture_id'])
        adjacency, _ = self.adjacency(capture['app_id'], capture['message_id'])
        target_id = None
        if result == 'annotation':
            with connect(self.store.path) as db:
                target_id, _ = self.raw_id_of_message(db, capture['app_id'], decided['target_message_id'])
        ledger = raw.RawLedger(self.store)
        record = ledger.supersede(written['raw_id'], lambda raw_id, now: self.render(
            capture, decided, adjacency, 0, target_id, raw_id=raw_id, supersedes=written['raw_id'], now=now),
            identity=IDENTITIES[result],
            referenced_raw_ids=raw._reference_ids(adjacency, target_id))
        ledger.write(record)


def voice_url(capture) -> str:
    return f"feishu-voice://{capture['app_id']}/{capture['message_id']}"


def is_voice_url(value: str) -> bool:
    return isinstance(value, str) and value.startswith('feishu-voice://')


def asr_identity(store):
    """(engine, model, version) of the configured recognizer, as recorded with a transcript."""
    model = store.setting('asr_model') or ''
    if model == 'volc.seedasr.auc':
        return 'doubao', model, '录音文件识别 2.0'
    from knowledge_distiller.primary import QWEN_MODEL_ID, QWEN_MODEL_REVISION, QWEN_RUNTIME_VERSION
    if model == QWEN_MODEL_ID:
        return 'qwen3-asr', 'Qwen3-ASR 1.7B（本机）', f'{QWEN_RUNTIME_VERSION} · {QWEN_MODEL_REVISION[:12]}'
    return 'unknown', model or '未记录', '未记录'


class FeishuVoiceSource:
    """The pipeline's source for a voice note: the audio downloaded at intake."""

    def __init__(self, store, verifier=None):
        from knowledge_distiller.media import FFmpegMediaVerifier
        self.store = store
        self.verifier = verifier or FFmpegMediaVerifier()

    def _capture(self, url):
        app_id, _, message_id = url.removeprefix('feishu-voice://').partition('/')
        capture = Captures(self.store).for_message(app_id, message_id)
        if capture is None:
            from .pipeline import DistillError
            raise DistillError('capture_missing')
        return capture

    def _material(self, capture, url, destination):
        from .domain import CapturedMaterial
        duration = self.verifier.verify(destination, expected_duration_seconds=None, complete_decode=True,
                                        require_video=False)
        return CapturedMaterial('feishu_voice', capture['message_id'], url, url,
                                {'capture_id': capture['capture_id'], 'source_title': '飞书语音',
                                 'published_at': raw._iso(_local(capture['created_ms']))}, destination, duration)

    def capture(self, submitted_url, work_dir, **_):
        import shutil
        from .pipeline import DistillError
        capture = self._capture(submitted_url)
        path = Path(capture['audio_path']) if capture['audio_path'] else None
        if path is None or not path.is_file():
            raise DistillError('capture_audio_unavailable')
        destination = Path(work_dir) / 'media' / 'source.opus'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        try:
            return self._material(capture, submitted_url, destination)
        except Exception as error:
            destination.unlink(missing_ok=True)
            raise DistillError('audio_invalid') from error

    def reuse_retained(self, *, source_key, submitted_url, canonical_url, metadata, work_dir, **_):
        destination = Path(work_dir) / 'media' / 'source.opus'
        if not destination.is_file() or destination.is_symlink():
            return None
        try:
            return self._material(self._capture(submitted_url), submitted_url, destination)
        except Exception:
            return None
