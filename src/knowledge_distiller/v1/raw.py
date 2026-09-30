"""Write finished materials into the vault's raw/ (docs/engineering/raw-interface.md).

raw/ is the knowledge base's canonical store and is only ever added to: nothing
here modifies, moves or deletes a file there. The database keeps an index of
what the app assigned (id, path, the exact bytes), so a vault that was
unavailable is filled in later with the same bytes, and the index itself can be
rebuilt from the files' envelopes (``sync_from_vault``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .database import connect


logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
ID_RE = re.compile(r'R-(\d{8})-(\d{4})')
FILE_RE = re.compile(r'R-\d{8}-\d{4}\.md')
DIRECTORIES = {'第三方': '外部', '本人': '自述', '本人附言': '自述'}
CHANNELS = {
    'douyin': '抖音', 'xiaohongshu': '小红书', 'zhihu': '知乎', 'weibo': '微博', 'x': 'X',
    'youtube': 'YouTube', 'bilibili': 'B站', 'direct_text': '直接文本', 'markdown': 'Markdown',
    'pdf': 'PDF', 'epub': 'EPUB', 'image': '图片',
}
EXTENSIONS = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif',
              'image/bmp': '.bmp', 'image/tiff': '.tiff', 'image/heic': '.heic'}
MIGRATION_ACQUISITION = 'V1 数据库存量导出'


class RawError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attachment:
    member_id: str
    filename: str
    sha256: str
    mime_type: str


@dataclass(frozen=True)
class RawDocument:
    relative_path: str
    content: str
    attachments: tuple[Attachment, ...] = ()


# ───────────────────────── Envelope (YAML frontmatter) ─────────────────────────

_RESERVED = {'true', 'false', 'yes', 'no', 'on', 'off', 'null', '~', 'y', 'n'}
_BARE = re.compile(r'[0-9A-Za-z㐀-鿿（）·][0-9A-Za-z㐀-鿿（）·._+\-/]*')
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}')


def _scalar(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (_BARE.fullmatch(text) and text.lower() not in _RESERVED) or _TIMESTAMP.fullmatch(text):
        return text
    # A JSON string is a valid YAML double-quoted scalar.
    return json.dumps(text, ensure_ascii=False)


def _flow(mapping) -> str:
    return '{' + ', '.join(f'{key}: {_scalar(value)}' for key, value in mapping.items()) + '}'


def envelope(fields) -> list[str]:
    """Ordered (key, value) pairs; None values are omitted (optional fields)."""
    lines = ['---']
    for key, value in fields:
        if value is None or value == [] or value == {}:
            continue
        if isinstance(value, dict):
            lines.append(f'{key}: {_flow(value)}')
        elif isinstance(value, list):
            lines.append(f'{key}:')
            for item in value:
                lines.append('  - ' + (_flow(item) if isinstance(item, dict) else _scalar(item)))
        else:
            lines.append(f'{key}: {_scalar(value)}')
    lines.append('---')
    return lines


def parse_envelope(text: str) -> dict:
    """The few single-line fields the app needs back from a raw file."""
    if not text.startswith('---\n'):
        return {}
    end = text.find('\n---\n', 3)
    if end < 0:
        return {}
    fields = {}
    for line in text[4:end].split('\n'):
        match = re.match(r'^([^\s:#-][^:]*):\s*(.*)$', line)
        if match:
            value = match.group(2).strip()
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            fields[match.group(1).strip()] = value
    return fields


# ───────────────────────── Body ─────────────────────────

def _blocks(snapshot: str, lineage) -> list[tuple[int, int, str, str]]:
    """The exact paragraphs V1 notes anchor as ^source-N (markdown.render_markdown)."""
    if lineage.get('image_ocr'):
        from .reading import build_reading
        return [(block.start, block.end, block.text, f'source-{index}')
                for index, block in enumerate(build_reading(snapshot, lineage), 1)]
    from .markdown import _source_blocks
    return [(block.start, block.end, block.text, block.anchor) for block in _source_blocks(snapshot)]


def _positioned_images(block, lineage):
    start, end = block[0], block[1]
    ids = [span['member_id'] for span in lineage.get('spans', [])
           if span.get('member_id') and span['start'] < end and span['end'] > start]
    ids += [image['member_id'] for image in lineage.get('image_ocr', [])
            if (image.get('source_start', end) < end and image.get('source_end', start) > start)
            or any(line['start'] < end and line['end'] > start for line in image.get('lines', []))]
    return [member for member in dict.fromkeys(ids) if isinstance(member, str) and member.startswith('image-')]


def _normalized(text: str) -> str:
    return text.replace('\r\n', '\n').replace('\r', '\n').strip('\n')


def body(snapshot: str, lineage, raw_id: str, members) -> tuple[list[str], tuple[Attachment, ...], list[str]]:
    """members: every recorded media identity, available bytes or not."""
    lines, attachments, missing, placed = [], [], [], set()

    def image(member):
        placed.add(member)
        media = members.get(member)
        if media is None or not media['content_available'] or media['mime_type'] not in EXTENSIONS:
            missing.append(member)
            return
        name = member + EXTENSIONS[media['mime_type']]
        attachments.append(Attachment(member, name, media['sha256'], media['mime_type']))
        lines.extend([f'![[附件/raw/{raw_id}/{name}]]', '', f'^{member}', ''])

    for block in _blocks(snapshot, lineage):
        for member in _positioned_images(block, lineage):
            if member not in placed:
                image(member)
        text = _normalized(block[2])
        if text.strip():
            lines.extend([text, '', f'^{block[3]}', ''])
    # An image with no located text (e.g. a textless picture) still belongs to
    # the material: keep it after the text rather than lose it.
    for member in members:
        if member.startswith('image-') and member not in placed:
            image(member)
    return lines, tuple(attachments), missing


def block_of(snapshot, lineage, start=None, text=None) -> str:
    """The ^source-N holding an offset, or the only block holding a text."""
    blocks = _blocks(snapshot, lineage)
    if isinstance(start, int):
        for block in blocks:
            if block[0] <= start < block[1]:
                return block[3]
    if isinstance(text, str) and text:
        anchors = {block[3] for block in blocks if text in block[2]}
        if len(anchors) == 1:
            return anchors.pop()
    return '未定位'


# ───────────────────────── External materials ─────────────────────────

def _local(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec='seconds')


def _published(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return _iso(parsed) if parsed.tzinfo is not None else None


def _title(metadata, lineage, snapshot):
    for value in (metadata.get('source_title'), lineage.get('native_title')):
        if isinstance(value, str) and value.strip():
            return ' '.join(value.split())
    description = metadata.get('original_description')
    if isinstance(description, str) and description.strip():
        return ' '.join(description.strip().splitlines()[0].split())[:60]
    if isinstance(metadata.get('submitted_name'), str) and metadata['submitted_name'].strip():
        return metadata['submitted_name'].strip()
    return ' '.join(snapshot.split())[:20] or '（无标题）'


def _author(metadata):
    authors = metadata.get('authors') or [metadata.get('author')]
    if not isinstance(authors, list):
        authors = [authors]
    names = []
    for author in authors:
        name = (author.get('display_name') or author.get('name')) if isinstance(author, dict) else author
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return '、'.join(names) or None


def _recognition(lineage, metadata, current_asr):
    if 'primary_subtitle' in lineage or lineage.get('caption_selection'):
        return '平台字幕'
    if 'primary_asr' in lineage:
        return current_asr or '语音识别（未记录模型）'
    images = lineage.get('image_ocr') or []
    if images:
        first = images[0]
        return ' '.join(str(part) for part in (first.get('engine'), first.get('recognition_model'),
                                             first.get('runtime_version')) if part)
    if metadata.get('parser') == 'docling' or lineage.get('parser') == 'docling':
        return 'Docling ' + str(metadata.get('parser_version') or lineage.get('parser_version') or '')
    return None


def corrections(snapshot, lineage, uncertainties):
    """Recorded corrections, each keeping the original recognition (irreversible)."""
    fixed, doubtful = [], []
    for entry in uncertainties:
        if not isinstance(entry, dict):
            continue
        by, replacement = entry.get('by'), entry.get('replacement')
        if by == 'human' and isinstance(replacement, str) and isinstance(entry.get('text'), str):
            where = block_of(snapshot, lineage, text=replacement)
            if where == '未定位' and isinstance(entry.get('member_id'), str):
                where = entry['member_id']
            fixed.append({'片段': where, '原识别': entry['text'], '订正为': replacement,
                          '方式': '用户订正' if entry.get('action') == 'local_transcription' else '用户核对'})
        elif by == 'ai' and entry.get('status') == 'repaired' and isinstance(replacement, str):
            original = entry.get('original_text', entry.get('text'))
            if isinstance(original, str):
                fixed.append({'片段': block_of(snapshot, lineage, start=entry.get('start'), text=replacement),
                              '原识别': original, '订正为': replacement, '方式': '模型修复'})
        elif by == 'human' and entry.get('status') == 'unresolved' and isinstance(entry.get('text'), str):
            doubtful.append({'片段': block_of(snapshot, lineage, start=entry.get('start'), text=entry['text']),
                             '文字': entry['text']})
    return fixed, doubtful


def render_material(row, available, raw_id: str, *, app_version: str, migrated: bool,
                    current_asr: str | None = None, adjacency=None, supersedes=None) -> RawDocument:
    """row: materials + source_facts (+ knowledge_results) columns for one material."""
    metadata = json.loads(row['metadata_json'] or '{}')
    lineage = json.loads(row['lineage_json'] or '{}')
    uncertainties = json.loads(row['uncertainties_json'] or '[]')
    snapshot = row['snapshot']
    collected = _local(row['created_at'])
    kind = row['source_kind']
    lines, attachments, missing = body(snapshot, lineage, raw_id, available)
    fixed, doubtful = corrections(snapshot, lineage, uncertainties)
    url = row['canonical_url'] or row['submitted_url']
    acquisition = {'采集': kind, '识别': _recognition(lineage, metadata, current_asr), '应用版本': app_version}
    if migrated:
        acquisition['导出'] = MIGRATION_ACQUISITION
    record = {'material_id': row['material_id'], 'source_fact_id': row['source_fact_id']}
    if row.get('knowledge_result_id') is not None:
        record['knowledge_result_id'] = row['knowledge_result_id']
    if row.get('published_path'):
        record['来源笔记'] = row['published_path']
    fields = [
        ('编号', raw_id), ('格式版本', FORMAT_VERSION), ('身份', '第三方'),
        ('标题', _title(metadata, lineage, snapshot)), ('作者', _author(metadata)),
        ('渠道', CHANNELS.get(kind, kind)),
        ('原链接', url if urlsplit(url or '').scheme in {'http', 'https'} else None),
        ('产生于', _published(metadata.get('published_at'))), ('收录于', _iso(collected)),
        ('取得方式', {key: value for key, value in acquisition.items() if value}),
        ('订正', fixed), ('存疑', doubtful), ('未保留附件', missing), ('邻接', adjacency or []),
        ('取代', supersedes), ('应用记录', record),
    ]
    text = '\n'.join(envelope(fields) + [''] + lines).rstrip('\n') + '\n'
    return RawDocument(relative_path(raw_id, '第三方', collected), text, attachments)


def relative_path(raw_id: str, identity: str, collected: datetime) -> str:
    return f'raw/{DIRECTORIES[identity]}/{collected:%Y}/{collected:%m}/{raw_id}.md'


# ───────────────────────── Ids ─────────────────────────

def vault_ids(vault: Path | None, day: str) -> int:
    """Highest NNNN already used for this day by any file under raw/ (app or Agent)."""
    if vault is None:
        return 0
    root = vault / 'raw'
    highest = 0
    if root.is_dir() and not root.is_symlink():
        for path in root.rglob(f'R-{day}-*.md'):
            match = ID_RE.fullmatch(path.stem)
            if match:
                highest = max(highest, int(match.group(2)))
    return highest


def allocate(connection, day: str, vault: Path | None) -> str:
    row = connection.execute('SELECT last FROM raw_counters WHERE day=?', (day,)).fetchone()
    number = max(row['last'] if row else 0, vault_ids(vault, day)) + 1
    if number > 9999:
        raise RawError('raw_id_exhausted')
    connection.execute('INSERT INTO raw_counters(day,last) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET last=excluded.last',
                       (day, number))
    return f'R-{day}-{number:04d}'


# ───────────────────────── Placement (no-clobber) ─────────────────────────

def _unsafe(path: Path, vault: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == vault or current.parent == current:
            return False
        current = current.parent


def place(vault: Path, relative: str, content: bytes) -> str:
    """Create or confirm one file; never overwrite. Returns placed/already/conflict."""
    target = vault / relative
    if _unsafe(target, vault):
        raise RawError('raw_path_unsafe')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_file() and not target.is_symlink() and target.read_bytes() == content:
            return 'already'
        return 'conflict'
    descriptor, name = tempfile.mkstemp(prefix='.kd-raw-', suffix='.tmp', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)  # Atomic and never replaces an existing file.
        except FileExistsError:
            return 'already' if target.read_bytes() == content else 'conflict'
        return 'placed'
    finally:
        temporary.unlink(missing_ok=True)


# ───────────────────────── Ledger ─────────────────────────

def app_version() -> str:
    try:
        from .updates import bundle_info
        info = bundle_info()
        return f"{info['display_version']} ({info['version']})" if info['version'] != '0' else '开发版本'
    except Exception:
        return '未记录'


class RawLedger:
    def __init__(self, store, *, version: str | None = None):
        self.store = store
        self.version = version or app_version()

    def vault(self) -> Path | None:
        value = self.store.setting('vault_path')
        return Path(value) if value else None

    def record(self, raw_id):
        with connect(self.store.path) as db:
            return db.execute('SELECT * FROM raw_records WHERE raw_id=?', (raw_id,)).fetchone()

    def current(self, subject_kind, subject_id):
        """The subject's newest record (the one no other record supersedes)."""
        with connect(self.store.path) as db:
            return db.execute('''SELECT * FROM raw_records r WHERE subject_kind=? AND subject_id=?
                AND NOT EXISTS (SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id)
                ORDER BY raw_id DESC LIMIT 1''', (subject_kind, subject_id)).fetchone()

    def ensure_material(self, material_id: int, *, adjacency=None, reserved=None, collected_ms=None,
                        supersedes=None):
        """Assign the material's raw once; a repeated delivery reuses it.

        A third-party quick note keeps the id reserved when it was captured and
        its capture time (captures.Captures.material_hints)."""
        vault = self.vault()
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT raw_id FROM raw_records WHERE subject_kind='material' AND subject_id=? LIMIT 1",
                                  (material_id,)).fetchone()
            if existing is None:
                row = material_row(db, material_id)
                if row is None:
                    raise RawError('raw_material_has_no_source_fact')
                if collected_ms is not None:
                    row['created_at'] = datetime.fromtimestamp(collected_ms / 1000, UTC).isoformat()
                if reserved and not db.execute('SELECT 1 FROM raw_records WHERE raw_id=?', (reserved,)).fetchone():
                    raw_id = reserved
                else:
                    raw_id = allocate(db, _local(row['created_at']).strftime('%Y%m%d'),
                                      vault if vault and vault.is_dir() else None)
                document = render_material(row, media(db, material_id), raw_id, app_version=self.version,
                                           migrated=False, current_asr=_asr_label(db), adjacency=adjacency,
                                           supersedes=supersedes)
                insert(db, raw_id, 'material', material_id, '第三方', document, origin='app', supersedes=supersedes)
        return self.current('material', material_id)

    def supersede(self, old_raw_id: str, document_for, *, identity: str):
        """Write a correction as a new file; the old file stays as it is."""
        vault = self.vault()
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM raw_records WHERE raw_id=?', (old_raw_id,)).fetchone()
            if old is None:
                raise RawError('raw_record_missing')
            if db.execute('SELECT 1 FROM raw_records WHERE supersedes=?', (old_raw_id,)).fetchone():
                raise RawError('raw_already_superseded')
            now = datetime.now().astimezone()
            raw_id = allocate(db, now.strftime('%Y%m%d'), vault if vault and vault.is_dir() else None)
            document = document_for(raw_id, now)
            insert(db, raw_id, old['subject_kind'], old['subject_id'], identity, document,
                   origin='app', supersedes=old_raw_id)
        return self.record(raw_id)

    def write(self, record, vault: Path | None = None) -> str:
        """Place one record's attachments, then its file. Failures stay retryable."""
        vault = vault or self.vault()
        state = 'unavailable'
        try:
            if vault is None or not vault.is_dir():
                raise RawError('vault_unavailable')
            vault = vault.resolve(strict=True)
            if _duplicate_id(vault, record['raw_id'], record['relative_path']):
                raise RawError('raw_id_collision')
            for attachment in json.loads(record['attachments_json']):
                content = _attachment_bytes(self.store, record, attachment)
                if place(vault, f"附件/raw/{record['raw_id']}/{attachment['filename']}", content) == 'conflict':
                    raise RawError('raw_attachment_conflict')
            state = place(vault, record['relative_path'], record['content'].encode('utf-8'))
            if state == 'conflict':
                raise RawError('raw_target_conflict')
        except (RawError, OSError) as error:
            code = str(error) if isinstance(error, RawError) else 'raw_write_failed'
            with connect(self.store.path) as db:
                db.execute('UPDATE raw_records SET attempts=attempts+1, last_error=? WHERE raw_id=?',
                           (code, record['raw_id']))
            logger.warning('raw %s not written (%s)', record['raw_id'], code)
            return code
        with connect(self.store.path) as db:
            db.execute('''UPDATE raw_records SET written_at=?, written_vault=?, attempts=attempts+1, last_error=NULL
                          WHERE raw_id=? AND written_at IS NULL''',
                       (datetime.now(UTC).isoformat(), str(vault), record['raw_id']))
        return state

    def write_pending(self) -> dict:
        """Backfill after the vault comes back; each record keeps its exact bytes."""
        vault = self.vault()
        results = {}
        if vault is None or not vault.is_dir():
            return results
        self.sync(vault)
        with connect(self.store.path) as db:
            pending = db.execute('SELECT * FROM raw_records WHERE written_at IS NULL ORDER BY raw_id').fetchall()
        for record in pending:
            results[record['raw_id']] = self.write(record, vault)
        return results

    def sync(self, vault: Path):
        """Index app-owned raw files already in the vault (e.g. a migration run on V1.3)."""
        key = 'raw_synced_vault'
        resolved = str(vault.resolve())
        if self.store.setting(key) == resolved:
            return 0
        imported = sync_from_vault(self.store.path, vault)
        self.store.set_settings({key: resolved})
        return imported


def _duplicate_id(vault: Path, raw_id: str, relative: str) -> bool:
    root = vault / 'raw'
    if not root.is_dir():
        return False
    return any(path.relative_to(vault).as_posix() != relative for path in root.rglob(raw_id + '.md'))


def _attachment_bytes(store, record, attachment) -> bytes:
    if record['subject_kind'] != 'material':
        raise RawError('raw_attachment_unavailable')
    with connect(store.path) as db:
        row = db.execute('SELECT content, sha256 FROM source_media WHERE material_id=? AND member_id=?',
                         (record['subject_id'], attachment['member_id'])).fetchone()
    if row is None or not row['content'] or hashlib.sha256(row['content']).hexdigest() != attachment['sha256']:
        raise RawError('raw_attachment_unavailable')
    return bytes(row['content'])


def material_row(db, material_id):
    row = db.execute('''SELECT m.material_id, m.source_kind, m.source_key, m.submitted_url, m.canonical_url,
            m.metadata_json, m.created_at, sf.source_fact_id, sf.snapshot, sf.lineage_json, sf.uncertainties_json,
            kr.knowledge_result_id, kr.published_path
        FROM materials m JOIN source_facts sf USING(material_id)
        LEFT JOIN knowledge_results kr USING(source_fact_id) WHERE m.material_id=?''', (material_id,)).fetchone()
    return dict(row) if row is not None else None


def media(db, material_id):
    return {row['member_id']: dict(row) for row in db.execute(
        'SELECT member_id, mime_type, sha256, length(content)>0 AS content_available '
        'FROM source_media WHERE material_id=? ORDER BY position', (material_id,))}


def _asr_label(db):
    row = db.execute("SELECT value FROM settings WHERE key='asr_model'").fetchone()
    value = row['value'] if row else ''
    return {'Qwen/Qwen3-ASR-1.7B': 'Qwen3-ASR 1.7B（本机）', 'volc.seedasr.auc': '豆包录音文件识别 2.0'}.get(value)


def insert(db, raw_id, subject_kind, subject_id, identity, document: RawDocument, *, origin,
           supersedes=None, written_at=None, written_vault=None):
    db.execute('''INSERT INTO raw_records (raw_id, subject_kind, subject_id, identity, relative_path, content,
            content_sha256, attachments_json, supersedes, origin, created_at, written_at, written_vault)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (raw_id, subject_kind, subject_id, identity, document.relative_path, document.content,
         hashlib.sha256(document.content.encode('utf-8')).hexdigest(),
         json.dumps([a.__dict__ for a in document.attachments], ensure_ascii=False),
         supersedes, origin, datetime.now(UTC).isoformat(), written_at, written_vault))
    match = ID_RE.fullmatch(raw_id)
    db.execute('''INSERT INTO raw_counters(day,last) VALUES (?,?)
                  ON CONFLICT(day) DO UPDATE SET last=MAX(last, excluded.last)''',
               (match.group(1), int(match.group(2))))


def _subject(fields):
    record = fields.get('应用记录', '')
    for kind, key in (('material', 'material_id'), ('capture', 'capture_id')):
        match = re.search(key + r':\s*(\d+)', record)
        if match:
            return kind, int(match.group(1))
    return None


def sync_from_vault(database: Path, vault: Path) -> int:
    """Rebuild index rows for app-owned raw files (those with 应用记录)."""
    root = vault / 'raw'
    if not root.is_dir() or root.is_symlink():
        return 0
    imported = 0
    with connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        known = {row['raw_id'] for row in db.execute('SELECT raw_id FROM raw_records')}
        for path in sorted(root.rglob('R-*.md')):
            if not FILE_RE.fullmatch(path.name) or path.stem in known or _unsafe(path, vault):
                continue
            text = path.read_text(encoding='utf-8')
            fields = parse_envelope(text)
            subject = _subject(fields)
            if fields.get('编号') != path.stem or subject is None or fields.get('身份') not in DIRECTORIES:
                continue  # Agent-written or foreign files stay outside the app's index.
            insert(db, path.stem, subject[0], subject[1], fields['身份'],
                   RawDocument(path.relative_to(vault).as_posix(), text), origin='vault',
                   supersedes=fields.get('取代') or None,
                   written_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                   written_vault=str(vault.resolve()))
            known.add(path.stem)
            imported += 1
    return imported
