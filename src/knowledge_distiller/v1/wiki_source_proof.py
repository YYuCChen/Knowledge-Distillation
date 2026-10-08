"""Read-only source qualification, not publication or universal completeness.

The callback is an application capability constructed with Store and a held
Vault lock. Its digest is useful only alongside the manifest's capabilities.
Cooperating writers are locked; before/after observations are not an atomic
database/filesystem snapshot or protection against malicious same-UID writes.
"""
from dataclasses import dataclass
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat

from .ingestion import Ingestion, IngestionError, envelope_fields, read_regular
from .raw import FORMAT_VERSION, RawError, _duplicate_id
from .wiki_lock import VaultWriteLock, WikiLockError, canonical_vault, session_fd_holds_lock
from .wiki_tasks import FrozenRaw, _boundary

CONTRACT = 'wiki-source-proof-v1'
INPUT_LIMIT = 32 * 1024 * 1024  # application bytes; never model tokens
# Exact schema25 DDL from database.py at 58bc8ee. SQLite stores this CREATE
# statement with the initial IF NOT EXISTS removed; no other normalization.
_PROOF_TRIGGER_SQL = """CREATE TRIGGER ingestion_events_proof_unavailable
        BEFORE INSERT ON ingestion_events
        WHEN NEW.kind IN ('raw_verified','release_authorized','media_released')
          AND (ingestion_proof(NEW.kind,NEW.binding_sha256,NEW.detail_json)!=1
               OR NEW.kind IS NOT json_extract(NEW.detail_json,'$.code')
               OR NEW.subject_kind IS NOT json_extract(NEW.detail_json,'$.manifest.subject_kind')
               OR NEW.subject_id IS NOT json_extract(NEW.detail_json,'$.manifest.subject_id')
               OR NEW.item_id IS NOT json_extract(NEW.detail_json,'$.manifest.owner_item_id')
               OR NEW.binding_sha256 IS NOT json_extract(NEW.detail_json,'$.final_binding_sha256'))
        BEGIN SELECT RAISE(ABORT,'filesystem proof unavailable'); END"""


class SourceProofError(ValueError):
    """Fixed codes only, no source text or exception contents."""


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _proof_guard_available(db):
    row = db.execute('SELECT type,tbl_name,sql FROM sqlite_schema WHERE name=?',
                     ('ingestion_events_proof_unavailable',)).fetchone()
    if row is None or row['type'] != 'trigger' or row['tbl_name'] != 'ingestion_events':
        return False
    sql = row['sql']
    prefix = 'CREATE TRIGGER IF NOT EXISTS '
    if isinstance(sql, str) and sql.startswith(prefix):
        sql = 'CREATE TRIGGER ' + sql[len(prefix):]
    return sql == _PROOF_TRIGGER_SQL


@dataclass(frozen=True)
class SourceProof:
    manifest_bytes: bytes

    @property
    def digest(self):
        return _sha(self.manifest_bytes)

    @property
    def manifest(self):
        return json.loads(self.manifest_bytes)  # fresh view, no mutable green flag


def _key(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SourceProofError('source_path_invalid')
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _files(root, relative):
    directory = root / relative
    if not directory.exists() and not directory.is_symlink():
        return ()
    canonical_vault(directory)
    return tuple(sorted(p.name for p in directory.iterdir()))


def _scope(fields, gaps):
    # A declaration identifies a finite capture scope, not platform coverage.
    declaration = fields.get('覆盖范围')
    kind = 'retained_literal'
    if (type(declaration) is dict and set(declaration) == {'status', 'scope'}
            and declaration['status'] in {'partial', 'full'}
            and type(declaration['scope']) is str and declaration['scope'].strip()):
        kind = 'declared_' + declaration['status']
    if fields.get('未保留附件'):
        gaps.add('known_missing_attachment')
    if fields.get('截断') or (type(declaration) is dict and declaration.get('status') == 'truncated'):
        gaps.add('capture_truncated')
    if fields.get('存疑'):
        gaps.add('source_uncertainty')
    if fields.get('邻接未定'):
        gaps.add('relation_unresolved')
    decision = fields.get('身份判定', {})
    if not isinstance(decision, dict) or decision.get('结果') in {'unknown', '未知', '待定', 'pending'}:
        gaps.add('identity_unresolved')
    if fields.get('渠道') in {'PDF', 'EPUB'}:
        gaps.add('original_file_unproven')
    if kind == 'retained_literal' and fields.get('渠道') not in {'直接文本', '飞书文字'}:
        gaps.add('capture_scope_unknown')
    return {'kind': kind, 'declaration_sha256': _sha(_encoded(declaration)),
            'platform_total_verified': False}


def _record_observation(ingestion, db, record, max_bytes):
    events = [dict(r) for r in db.execute(
        'SELECT event_key,contract,item_id,kind,binding_sha256,detail_json '
        'FROM ingestion_events WHERE subject_kind=? AND subject_id=? ORDER BY event_id',
        (record['subject_kind'], record['subject_id']))]
    try:
        state = ingestion._source_state(db, record['subject_kind'], record['subject_id'])
    except IngestionError:
        # Still bind actual source/head/decision drift when current qualification
        # fails; never reuse an old successful descriptor on a failed readback.
        if record['subject_kind'] == 'material':
            state = {'source': [dict(r) for r in db.execute(
                'SELECT m.*,sf.* FROM materials m LEFT JOIN source_facts sf USING(material_id) '
                'WHERE material_id=?', (record['subject_id'],))]}
            state['owners'] = [ingestion._item_state(db, r[0]) for r in db.execute(
                'SELECT item_id FROM distill_items WHERE material_id=? ORDER BY item_id',
                (record['subject_id'],))]
        else:
            state = {'capture': ingestion.captures.get(record['subject_id']),
                     'decision': ingestion.captures.identity(record['subject_id']),
                     'transcript': ingestion.captures.transcript(record['subject_id'])}
        state['heads'] = [dict(r) for r in db.execute(
            'SELECT raw_id,identity,content_sha256,relative_path,supersedes FROM raw_records '
            'WHERE subject_kind=? AND subject_id=? ORDER BY raw_id',
            (record['subject_kind'], record['subject_id']))]
    # Include actual retained bytes, not just their declared digests. _rows
    # hashes BLOB values; neither bytes nor metadata escape this observation.
    media_subject = record['subject_id'] if record['subject_kind'] == 'material' else -1
    if record['subject_kind'] == 'capture' and (state.get('capture') or {}).get('item_id'):
        owner = db.execute('SELECT material_id FROM distill_items WHERE item_id=?',
                           (state['capture']['item_id'],)).fetchone()
        media_subject = owner[0] if owner else -1
    retained = db.execute('SELECT coalesce(sum(length(content)),0) FROM source_media '
                          'WHERE material_id=?', (media_subject,)).fetchone()[0]
    if retained > max_bytes:
        raise SourceProofError('source_input_limit')
    state['retained_media'] = ingestion._rows(db,
        'SELECT * FROM source_media WHERE material_id=? ORDER BY position,member_id',
        (media_subject,))
    fields = envelope_fields(record['content'].encode())
    dependencies = [link['编号'] for link in fields.get('邻接', [])]
    if fields.get('附言对象'):
        dependencies.append(fields['附言对象'])
    state['referenced_sources'] = []
    for raw_id in sorted(set(dependencies)):
        row = db.execute('SELECT * FROM raw_records WHERE raw_id=?', (raw_id,)).fetchone()
        if row is not None:
            try:
                dependent = ingestion._source_state(db, row['subject_kind'], row['subject_id'])
            except IngestionError:
                dependent = {'unqualified': True}
            state['referenced_sources'].append({'raw': dict(row), 'state': dependent})
        else:
            state['referenced_sources'].append({'raw_id': raw_id, 'missing': True})
    observation = _encoded({'record': {k: record[k] for k in (
        'raw_id', 'subject_kind', 'subject_id', 'identity', 'relative_path',
        'content_sha256', 'attachments_json', 'supersedes')}, 'state': state, 'events': events})
    if len(observation) > max_bytes:
        raise SourceProofError('source_input_limit')
    return events, _sha(observation)


def verify_wiki_sources(store, task, snapshot, context, lock, *, max_bytes=INPUT_LIMIT):
    """Observe exact frozen raws twice; return finite source capabilities.

    Raw/path/hash or owned attachment inconsistency rejects the whole call.
    Missing provenance/events/source facts yields a literal-only descriptor.
    No initialize, allocation, placement, migration, event writes or release.
    """
    try:
        return _verify(store, task, snapshot, tuple(context), lock, max_bytes)
    except SourceProofError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError,
            sqlite3.Error, RawError, WikiLockError):
        raise SourceProofError('source_proof_invalid') from None


def _verify(store, task, snapshot, context, lock, max_bytes):
    if (type(max_bytes) is not int or not 0 < max_bytes <= INPUT_LIMIT
            or not isinstance(lock, VaultWriteLock)
            or canonical_vault(task.vault_path) != lock.vault
            or task.vault_key != lock.key
            or not session_fd_holds_lock(lock.vault, lock.descriptor)
            or snapshot.task_id != task.task_id or not context):
        raise SourceProofError('source_boundary_invalid')
    vault, workspace = lock.vault, canonical_vault(snapshot.workspace)
    if workspace == vault or workspace.is_relative_to(vault) or vault.is_relative_to(workspace):
        raise SourceProofError('source_boundary_invalid')
    raws = {r.raw_id: r for r in task.raw}
    baseline = {f.relative_path: f for f in snapshot.files}
    if (len(raws) != len(task.raw) or len(baseline) != len(snapshot.files)
            or tuple(r for r, _ in context) != tuple(task.raw)
            or tuple(r.ordinal for r in task.raw) != tuple(range(1, len(task.raw) + 1))
            or task.raw_count != len(task.raw) or task.batch_count != len(task.batches)
            or tuple(b.batch_no for b in task.batches) != tuple(range(1, len(task.batches) + 1))
            or any(b.item_count != sum(r.batch_no == b.batch_no for r in task.raw) for b in task.batches)
            or any(r.batch_no not in {b.batch_no for b in task.batches} for r in task.raw)
            or any(r.relative_path not in baseline or baseline[r.relative_path].role != 'raw'
                   or baseline[r.relative_path].sha256 != r.content_sha256
                   or baseline[r.relative_path].byte_count != r.byte_count for r in task.raw)
            or len({r.raw_id for r, _ in context}) != len(context)
            or _boundary(tuple(tuple(r for r in task.raw if r.batch_no == b.batch_no)
                               for b in task.batches)) != task.boundary_sha256):
        raise SourceProofError('source_boundary_invalid')
    budget, identities = {}, {}
    def observe(root, relative):
        path = PurePosixPath(relative)
        if (path.is_absolute() or any(p in {'', '.', '..'} for p in relative.split('/'))
                or any(c in relative for c in '\\:\x00')):
            raise SourceProofError('source_path_invalid')
        target = root / relative
        before = _key(target)
        budget[(str(root), relative)] = before[3]
        if sum(budget.values()) > max_bytes:
            raise SourceProofError('source_input_limit')
        content = read_regular(root, relative)
        if before != _key(target):
            raise SourceProofError('source_changed')
        identities[(root, relative)] = before
        frozen = baseline.get(relative)
        if frozen is None or frozen.sha256 != _sha(content) or frozen.byte_count != len(content):
            raise SourceProofError('source_snapshot_mismatch')
        return content

    database = Path(store.path)
    canonical_vault(database.absolute().parent)
    if not database.is_file() or database.is_symlink():
        raise SourceProofError('source_database_unavailable')
    database_identity = _key(database)
    ingestion = Ingestion(store)  # constructor only; no initialize
    def pass_once():
        result = []
        with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA user_version').fetchone()[0] != 25:
                raise SourceProofError('source_schema_unsupported')
            for frozen, supplied in context:
                if (not isinstance(frozen, FrozenRaw) or raws.get(frozen.raw_id) != frozen
                        or type(supplied) is not bytes or len(supplied) != frozen.byte_count
                        or _sha(supplied) != frozen.content_sha256):
                    raise SourceProofError('source_boundary_invalid')
                parts = PurePosixPath(frozen.relative_path).parts
                if (len(parts) != 5 or parts[:2] != ('raw', '外部' if frozen.identity == '第三方' else '自述')
                        or frozen.identity not in {'第三方', '本人', '本人附言'}
                        or re.fullmatch(r'R-\d{8}-\d{4}', frozen.raw_id) is None
                        or re.fullmatch(r'\d{4}', parts[2]) is None
                        or parts[3] not in {f'{m:02}' for m in range(1, 13)}
                        or parts[-1] != frozen.raw_id + '.md'):
                    raise SourceProofError('source_boundary_invalid')
                formal = observe(vault, frozen.relative_path)
                if formal != supplied or observe(workspace, frozen.relative_path) != formal:
                    raise SourceProofError('source_bytes_mismatch')
                fields = envelope_fields(formal)
                if (fields.get('编号') != frozen.raw_id or fields.get('身份') != frozen.identity
                        or type(fields.get('格式版本')) is not int or fields['格式版本'] != FORMAT_VERSION):
                    raise SourceProofError('source_identity_invalid')
                if (_duplicate_id(vault, frozen.raw_id, frozen.relative_path)
                        or _duplicate_id(workspace, frozen.raw_id, frozen.relative_path)):
                    raise SourceProofError('source_identity_invalid')
                record = db.execute('SELECT * FROM raw_records WHERE raw_id=?', (frozen.raw_id,)).fetchone()
                declared = re.findall(r'!\[\[(附件/raw/[^\]\n]+)\]\]', formal.decode())
                if len(set(declared)) != len(declared):
                    raise SourceProofError('source_attachment_set_invalid')
                directory = '附件/raw/' + frozen.raw_id
                if any(PurePosixPath(p).parent.as_posix() != directory for p in declared):
                    raise SourceProofError('source_attachment_set_invalid')
                attachments = []
                for relative in sorted(declared):
                    data = observe(vault, relative)
                    if observe(workspace, relative) != data:
                        raise SourceProofError('source_attachment_changed')
                    attachments.append({'path': relative, 'sha256': _sha(data), 'byte_count': len(data)})
                names = tuple(sorted(PurePosixPath(p).name for p in declared))
                if _files(vault, directory) != names or _files(workspace, directory) != names:
                    raise SourceProofError('source_attachment_set_invalid')
                gaps, capabilities, eventkeys = set(), ['literal_text', 'declared_attachments_readback'], []
                binding, observation, subject = None, None, None
                versions = {'source_fact_id': None, 'capture_decision_event_id': None,
                            'current_head_ids': []}
                if record is None:
                    gaps.add('ledger_record_missing')
                else:
                    if (record['content'].encode() != formal or record['content_sha256'] != frozen.content_sha256
                            or record['relative_path'] != frozen.relative_path or record['identity'] != frozen.identity):
                        raise SourceProofError('source_ledger_mismatch')
                    manifest = json.loads(record['attachments_json'])
                    if (len({a['member_id'] for a in manifest}) != len(manifest)
                            or len({a['filename'] for a in manifest}) != len(manifest)
                            or {(directory + '/' + a['filename'], a['sha256']) for a in manifest}
                            != {(a['path'], a['sha256']) for a in attachments}):
                        raise SourceProofError('source_attachment_set_invalid')
                    events, observation = _record_observation(ingestion, db, record, max_bytes)
                    subject = {'kind': record['subject_kind'], 'id': record['subject_id']}
                    versions['current_head_ids'] = [r[0] for r in db.execute(
                        'SELECT r.raw_id FROM raw_records r WHERE subject_kind=? AND subject_id=? '
                        'AND NOT EXISTS(SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id) ORDER BY r.raw_id',
                        (record['subject_kind'], record['subject_id']))]
                    material_id = record['subject_id'] if record['subject_kind'] == 'material' else None
                    if record['subject_kind'] == 'capture':
                        capture = ingestion.captures.get(record['subject_id'])
                        decision = ingestion.captures.identity(record['subject_id'])
                        versions['capture_decision_event_id'] = decision['event_id'] if decision else None
                        if capture and capture['item_id']:
                            owner = db.execute('SELECT material_id FROM distill_items WHERE item_id=?',
                                               (capture['item_id'],)).fetchone()
                            material_id = owner[0] if owner else None
                        if capture and capture['message_type'] == 'audio':
                            # This closure freezes raw/attachments, not owned
                            # recordings. A historical audio_manifest digest
                            # does not prove current audio bytes or coverage.
                            gaps.add('original_audio_unproven')
                    fact = db.execute('SELECT source_fact_id FROM source_facts WHERE material_id=?',
                                      (material_id,)).fetchone()
                    versions['source_fact_id'] = fact[0] if fact else None
                    if not _proof_guard_available(db):
                        gaps.add('internal_event_guard_unavailable')
                    try:
                        # Every callee here is read-only in fixed schema25.
                        ingestion._validate_context(record, vault, None)
                        ingestion._message_readback(db, record, vault)
                        matching = []
                        for event in events:
                            if (gaps.intersection({'internal_event_guard_unavailable'})
                                    or event['kind'] != 'raw_verified' or event['contract'] != 'raw-verified-v1'):
                                continue
                            current = ingestion._binding(db, record, event['item_id'])
                            detail = json.loads(event['detail_json'])
                            m = detail['manifest']
                            key = _sha(_encoded(['raw-verified-v1', record['subject_kind'],
                                record['subject_id'], event['item_id'], 'raw_verified',
                                current, event['detail_json']]))
                            if (key == event['event_key']
                                    and detail['code'] == 'raw_verified'
                                    and m['subject_kind'] == record['subject_kind']
                                    and m['subject_id'] == record['subject_id']
                                    and m['owner_item_id'] == event['item_id']
                                    and current == event['binding_sha256'] == detail['final_binding_sha256']
                                    and detail['vault_sha256'] == _sha(os.fsencode(vault))
                                    and m['raw_id'] == frozen.raw_id and m['content_sha256'] == frozen.content_sha256
                                    and m['relative_path'] == frozen.relative_path and m['byte_count'] == len(formal)
                                    and m['identity'] == frozen.identity and m['format_version'] == FORMAT_VERSION
                                    and {(directory + '/' + a['filename'], a['sha256'], a['byte_count'])
                                         for a in m['attachment_manifest']}
                                    == {(a['path'], a['sha256'], a['byte_count']) for a in attachments}):
                                matching.append((event['event_key'], current))
                        if matching:
                            eventkeys = sorted(k for k, _ in matching)
                            binding = _sha(_encoded(sorted(matching)))
                            capabilities += ['canonical_source_binding', 'canonical_ingestion_event']
                        else:
                            gaps.add('canonical_event_unavailable')
                    except (IngestionError, RawError, KeyError, TypeError, AttributeError, IndexError):
                        gaps.add('current_source_unqualified')
                scope = _scope(fields, gaps)
                if not gaps and 'canonical_ingestion_event' in capabilities:
                    capabilities.append('declared_capture_verified')
                result.append({'raw_id': frozen.raw_id, 'path': frozen.relative_path,
                    'identity': frozen.identity, 'sha256': frozen.content_sha256, 'byte_count': frozen.byte_count,
                    'format_version': FORMAT_VERSION, 'versions': versions,
                    'subject': subject, 'attachments': attachments,
                    'scope': scope, 'capabilities': sorted(capabilities), 'gaps': sorted(gaps),
                    'source_binding_sha256': binding, 'observation_sha256': observation,
                    'event_keys': eventkeys, 'contracts': [CONTRACT, 'raw-verified-v1'] if eventkeys else [CONTRACT]})
        return result
    before = pass_once()
    held = dict(identities)
    after = pass_once()
    if (before != after or held != identities
            or _key(database) != database_identity
            or any(_key(root / p) != key for (root, p), key in held.items())
            or not session_fd_holds_lock(vault, lock.descriptor)):
        raise SourceProofError('source_changed')
    return SourceProof(_encoded({'contract': CONTRACT, 'task_id': task.task_id,
        'boundary_sha256': task.boundary_sha256, 'sources': before}))


def trusted_source_callback(store, lock, *, max_bytes=INPUT_LIMIT):
    """Only application assembly may supply Store/lock, never a request body.

    The caller must use callback.verify(...).manifest to admit the capabilities
    required by its operation; a well-formed returned digest is not admission.
    """
    def verify(*, task, snapshot, context):
        return verify_wiki_sources(store, task, snapshot, context, lock, max_bytes=max_bytes)
    def callback(**inputs):
        return verify(**inputs).digest
    callback.verify = verify
    return callback
