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
import json
import logging
from pathlib import Path
import re
import time

from . import raw
from .database import connect


logger = logging.getLogger(__name__)

IDENTITIES = {'my_thought': '本人', 'annotation': '本人附言', 'third_party': '第三方'}
SETTLE_HOURS = 24
DEFAULT_WINDOW_MINUTES = 30
CLOUD_SETTING = 'capture_cloud_judgment'
WINDOW_SETTING = 'capture_adjacency_minutes'

_ANNOTATION = re.compile(r'(这篇|这个视频|这条|这期|这段|这本|这集|这个链接|上面|刚才那|刚发的|重点看|前半|后半|'
                         r'第.{1,3}(段|分钟|部分|章)|注意看|值得看|可以看看|先存着|回头看)')
_REPOST = re.compile(r'(转自|转载|来源[:：]|原文[:：]|作者[:：]|出处[:：]|原标题|via\s*@|#\S+#)')
_FORMAT = re.compile(r'^\s*(#{1,6}\s|>\s|[-*•]\s|\d+[.、)]\s)', re.M)


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
    text = capture['text'].strip()
    lines = text.count('\n')
    if (len(text) >= 300 or (lines >= 5 and len(text) >= 120) or _REPOST.search(text)
            or (len(_FORMAT.findall(text)) >= 2 and len(text) >= 80)):
        return 'third_party', '规则·大段、带格式或标明转载的文本', 0.9, None
    if recent_delivery is not None and len(text) <= 80 and _ANNOTATION.search(text):
        return 'annotation', '规则·紧随投递的附言', 0.85, recent_delivery
    return None


class CloudIdentityJudge:
    """Replaceable judge on the configured model; used only when the user enabled it."""

    PROMPT = ('判断一条用户发给自己私聊机器人的消息属于哪一类：my_thought（用户自己的想法、感受、计划）、'
              'third_party（转发或粘贴的他人内容）、annotation（对刚投递的材料的附言，例如"这篇重点看后半段"）、'
              'unknown（无法判断）。只返回 JSON：{"identity": 类别, "confidence": 0 到 1 的数}。'
              '不要改写或评价消息，拿不准就返回 unknown。')

    def __init__(self, client):
        self.client = client

    def judge(self, text, *, recent_delivery):
        from .model_json import parse_model_json
        answer = self.client.complete(system=self.PROMPT, user=json.dumps(
            {'message': text, 'just_after_a_delivery': recent_delivery is not None}, ensure_ascii=False), max_tokens=200)
        value = parse_model_json(answer).value
        identity, confidence = value.get('identity'), value.get('confidence')
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return None
        confidence = float(confidence)
        basis = '模型·' + (getattr(self.client, 'model', '') or '未记录')
        # Undecided never becomes "mine"; the bar for my_thought is the highest.
        if identity == 'my_thought' and confidence >= 0.9:
            return 'my_thought', basis, confidence, None
        if identity == 'third_party' and confidence >= 0.8:
            return 'third_party', basis, confidence, None
        if identity == 'annotation' and confidence >= 0.8 and recent_delivery is not None:
            return 'annotation', basis, confidence, recent_delivery
        return None


class Captures:
    def __init__(self, store, *, client_factory=None, api=None, audio_root: Path | None = None):
        self.store = store
        self.client_factory = client_factory
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
        """The latest earlier non-capture delivery in the window (a possible annotation target)."""
        with connect(self.store.path) as db:
            row = db.execute('''SELECT a.earlier_message_id FROM delivery_adjacency a
                JOIN feishu_receipts r ON r.app_id=a.app_id AND r.message_id=a.earlier_message_id
                WHERE a.app_id=? AND a.message_id=? AND r.state IN ('received','waiting_input','accepted','needs_desktop')
                AND NOT EXISTS (SELECT 1 FROM captures c WHERE c.app_id=a.app_id AND c.message_id=a.earlier_message_id)
                ORDER BY a.gap_seconds LIMIT 1''', (capture['app_id'], capture['message_id'])).fetchone()
        return row['earlier_message_id'] if row else None

    def cloud_enabled(self):
        return self.store.setting(CLOUD_SETTING) == 'on' and self.client_factory is not None

    # ── decisions ──
    def _event(self, capture_id, result, basis, confidence=None, target=None):
        with connect(self.store.path) as db:
            db.execute('''INSERT INTO capture_identity_events(capture_id, result, basis, confidence, target_message_id,
                          created_at) VALUES (?,?,?,?,?,?)''',
                       (capture_id, result, basis, confidence, target, datetime.now(UTC).isoformat()))

    def judge(self, capture):
        """Rules, then the cloud judge if enabled; otherwise the desk decides."""
        if self.identity(capture['capture_id']) is not None:
            return self.identity(capture['capture_id'])
        target = self.recent_delivery(capture)
        decided = rule_judgment(capture, recent_delivery=target)
        if decided is None and self.cloud_enabled():
            try:
                decided = CloudIdentityJudge(self.client_factory()).judge(capture['text'], recent_delivery=target)
            except Exception as error:
                logger.warning('capture %s cloud judgment unavailable (%s)', capture['capture_id'], type(error).__name__)
        if decided is None:
            self._event(capture['capture_id'], 'pending', '规则未决' + ('' if self.cloud_enabled() else '·云端判断关闭'))
        else:
            self._event(capture['capture_id'], decided[0], decided[1], decided[2], decided[3])
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
        written = self._written(capture_id)
        self._event(capture_id, result, '用户', 1.0, target)
        if written is not None and IDENTITIES[result] != written['identity']:
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

    def write_ready(self, ledger=None, *, now=None) -> dict:
        ledger = ledger or raw.RawLedger(self.store)
        now = now or datetime.now(UTC)
        results = {}
        for capture, decided in self.ready():
            adjacency, unsettled = self.adjacency(capture['app_id'], capture['message_id'])
            target_id, target_settled = None, True
            if decided['result'] == 'annotation':
                with connect(self.store.path) as db:
                    target_id, target_settled = self.raw_id_of_message(db, capture['app_id'], decided['target_message_id'])
            young = now - _local(capture['received_ms']) < timedelta(hours=SETTLE_HOURS)
            if young and (unsettled or not target_settled or (decided['result'] == 'annotation' and target_id is None)):
                continue  # Earlier deliveries are still being processed; cite them once they have raw ids.
            document = self.render(capture, decided, adjacency, unsettled if not young else 0, target_id)
            with connect(self.store.path) as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM raw_records WHERE subject_kind='capture' AND subject_id=?",
                              (capture['capture_id'],)).fetchone():
                    continue
                raw.insert(db, capture['raw_id'], 'capture', capture['capture_id'], IDENTITIES[decided['result']],
                           document, origin='app')
            record = ledger.record(capture['raw_id'])
            results[capture['raw_id']] = ledger.write(record)
            if results[capture['raw_id']] in {'placed', 'already'}:
                self.release_audio(capture)
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
            identity=IDENTITIES[result])
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
