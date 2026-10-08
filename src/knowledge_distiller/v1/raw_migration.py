"""One-time export of existing V1 source facts into raw/外部/ (raw-interface §7).

Planning reads a supported legacy database without changing it. Replay holds
an SQLite writer transaction over source checks and no-clobber file placement,
then records the result in the raw ledger; it never initializes or migrates a
schema. Already-placed files survive a later database rollback. Existing files
are only confirmed by exact readback, never replaced. IDs follow
``materials.created_at``; an envelope alone cannot qualify a source.

    python -m knowledge_distiller.v1.raw_migration --data-dir DIR --vault VAULT [--dry-run]
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from . import raw
from .database import connect
from .ingestion import read_regular


MIGRATED_VERSION = '未记录（V1 数据库）'


def existing_subjects(vault: Path) -> dict[int, str]:
    """material_id → raw id for app-owned raw files already in this vault."""
    found = {}
    root = vault / 'raw'
    if not root.is_dir():
        return found
    for path in sorted(root.rglob('R-*.md')):
        if not raw.FILE_RE.fullmatch(path.name) or path.is_symlink():
            continue
        fields = raw.parse_envelope(path.read_text(encoding='utf-8'))
        subject = raw._subject(fields)
        if subject and subject[0] == 'material' and fields.get('编号') == path.stem:
            found.setdefault(subject[1], path.stem)
    return found


@contextmanager
def _read_only(database: Path):
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


def _head(db, material_id):
    heads = db.execute("""SELECT * FROM raw_records r WHERE subject_kind='material' AND subject_id=?
        AND NOT EXISTS(SELECT 1 FROM raw_records n WHERE n.supersedes=r.raw_id)""", (material_id,)).fetchall()
    if len(heads) > 1:
        raise raw.RawError('raw_migration_heads_ambiguous')
    return heads[0] if heads else None


def _render(db, material_id, raw_id):
    record = raw.material_row(db, material_id)
    if record is None:
        raise raw.RawError('raw_material_has_no_source_fact')
    return raw.render_material(record, raw.media(db, material_id), raw_id,
                               app_version=MIGRATED_VERSION, migrated=True)


def _media_bytes(db, material_id, document):
    return {(material_id, a.member_id): raw._attachment_bytes(None,
        {'subject_kind': 'material', 'subject_id': material_id}, a.__dict__, db=db)
        for a in document.attachments}


def _readback(vault, raw_id, document, media_bytes, material_id):
    if read_regular(vault, document.relative_path) != document.content.encode('utf-8'):
        raise raw.RawError('raw_migration_readback_mismatch')
    for a in document.attachments:
        if read_regular(vault, f'附件/raw/{raw_id}/{a.filename}') != media_bytes[(material_id, a.member_id)]:
            raise raw.RawError('raw_migration_readback_mismatch')


def _existing(db, vault, material_id, raw_id, document, row):
    if (row['raw_id'] != raw_id or row['subject_kind'] != 'material' or row['subject_id'] != material_id
            or row['identity'] != '第三方' or row['origin'] != 'migration' or row['supersedes'] is not None
            or row['relative_path'] != document.relative_path or row['content'] != document.content
            or row['content_sha256'] != hashlib.sha256(document.content.encode('utf-8')).hexdigest()
            or json.loads(row['attachments_json']) != [a.__dict__ for a in document.attachments]
            or not row['written_at'] or row['written_vault'] != str(vault.resolve())):
        raise raw.RawError('raw_migration_record_conflict')
    if raw._duplicate_id(vault, raw_id, document.relative_path):
        raise raw.RawError('raw_id_collision')
    _readback(vault, raw_id, document, _media_bytes(db, material_id, document), material_id)


def _replay_one(database, vault, material_id, raw_id, document):
    """Fresh qualification and full-render CAS, not a frozen whole-graph proof.

    Returning exits the connection context first, so commit failure cannot be
    reported as success. Files already placed are not database rollback data.
    """
    with connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        raw._source_inventory(db)
        if type(raw_id) is not str or raw.ID_RE.fullmatch(raw_id) is None or type(document) is not raw.RawDocument:
            raise raw.RawError('raw_migration_plan_invalid')
        raw._legacy_source_gate(db, 'material', material_id,
            referenced_raw_ids=raw._document_reference_ids(document.content, None))
        head = _head(db, material_id)
        row = db.execute('SELECT * FROM raw_records WHERE raw_id=?', (raw_id,)).fetchone()
        if (head is not None and head['raw_id'] != raw_id) or (row is not None and
                (row['subject_kind'], row['subject_id']) != ('material', material_id)):
            raise raw.RawError('raw_id_collision')
        if raw._duplicate_id(vault, raw_id, document.relative_path):
            raise raw.RawError('raw_id_collision')
        if _render(db, material_id, raw_id) != document:
            raise raw.RawError('raw_migration_source_changed')
        if row is not None:
            if head is None:
                raise raw.RawError('raw_migration_record_conflict')
            _existing(db, vault, material_id, raw_id, document, row)
            return 'already'
        media_bytes = _media_bytes(db, material_id, document)
        outcome = _write(vault, raw_id, document, media_bytes, material_id)
        if outcome in {'placed', 'already'}:
            _readback(vault, raw_id, document, media_bytes, material_id)
            raw.insert(db, raw_id, 'material', material_id, '第三方', document, origin='migration',
                       written_at=datetime.now(UTC).isoformat(), written_vault=str(vault.resolve()))
        return outcome


def plan(database: Path, vault: Path):
    """Deterministic list of (material_id, raw_id, document) still to create."""
    planned, counters = [], {}
    with _read_only(database) as db:
        raw._source_inventory(db)
        version = int(db.execute('PRAGMA user_version').fetchone()[0])
        counters = {row['day']: row['last'] for row in db.execute('SELECT day, last FROM raw_counters')}
        rows = db.execute('''SELECT m.material_id FROM materials m JOIN source_facts sf USING(material_id)
                             ORDER BY m.created_at, m.material_id''').fetchall()
        failures = []
        for row in rows:
            material_id = row['material_id']
            try:
                raw._legacy_source_gate(db, 'material', material_id)
                head = _head(db, material_id)
                if head is not None:
                    document = _render(db, material_id, head['raw_id'])
                    _existing(db, vault, material_id, head['raw_id'], document, head)
                    continue
                record = raw.material_row(db, material_id)
                day = raw._local(record['created_at']).strftime('%Y%m%d')
                number = max(counters.get(day, 0), raw.vault_ids(vault, day)) + 1
                if number > 9999:
                    raise raw.RawError('raw_id_exhausted')
                raw_id = f'R-{day}-{number:04d}'
                document = _render(db, material_id, raw_id)
            except raw.LegacySourceVeto as error:
                if error.args != ('local_source_qualification_pending',):
                    raise
                failures.append({'material_id': material_id, 'error': 'local_source_qualification_pending'})
                continue
            counters[day] = number
            planned.append((material_id, raw_id, document))
    return version, planned, failures


def run(data_dir: Path, vault: Path, *, dry_run: bool = False) -> dict:
    data_dir, vault = Path(data_dir).expanduser().resolve(), Path(vault).expanduser()
    database = data_dir / 'knowledge.sqlite3'
    if not database.is_file():
        raise SystemExit('找不到应用数据库：' + str(database))
    if not vault.is_dir():
        raise SystemExit('找不到 Vault 目录：' + str(vault))
    vault = vault.resolve()
    lock = (data_dir / '.instance.lock').open('a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('知识蒸馏器正在使用这个数据目录，请先退出应用再迁移。') from None
        version, planned, failures = plan(database, vault)
        report = {'database_schema': version, 'vault': str(vault), 'dry_run': dry_run,
                  'planned': len(planned), 'failures': failures, 'results': []}
        if dry_run:
            report['results'] = [{'material_id': m, 'raw_id': r, 'path': d.relative_path,
                                  'attachments': len(d.attachments)} for m, r, d in planned]
            return report
        for material_id, raw_id, document in planned:
            try:
                outcome = _replay_one(database, vault, material_id, raw_id, document)
            except raw.LegacySourceVeto as error:
                if error.args != ('local_source_qualification_pending',):
                    raise
                outcome = 'local_source_qualification_pending'
            report['results'].append({'material_id': material_id, 'raw_id': raw_id,
                                      'path': document.relative_path, 'outcome': outcome})
        return report
    finally:
        lock.close()


def _write(vault, raw_id, document, media_bytes, material_id):
    import hashlib
    for attachment in document.attachments:
        content = media_bytes.get((material_id, attachment.member_id), b'')
        if not content or hashlib.sha256(content).hexdigest() != attachment.sha256:
            return 'attachment_unavailable'
        if raw.place(vault, f'附件/raw/{raw_id}/{attachment.filename}', content) == 'conflict':
            return 'attachment_conflict'
    return raw.place(vault, document.relative_path, document.content.encode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='把 V1 数据库中已有的来源原文导出到 Vault 的 raw/外部/（只新建文件）')
    parser.add_argument('--data-dir', type=Path, required=True, help='应用数据目录（含 knowledge.sqlite3）')
    parser.add_argument('--vault', type=Path, required=True, help='Obsidian Vault 根目录')
    parser.add_argument('--dry-run', action='store_true', help='只列出将要创建的文件，不写入')
    parser.add_argument('--report', type=Path, help='把结果（不含正文）写成 JSON')
    args = parser.parse_args(argv)
    report = run(args.data_dir, args.vault, dry_run=args.dry_run)
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    outcomes = {}
    for result in report['results']:
        outcomes[result.get('outcome', 'planned')] = outcomes.get(result.get('outcome', 'planned'), 0) + 1
    print(json.dumps({'planned': report['planned'], 'outcomes': outcomes, 'failures': len(report['failures']),
                      'database_schema': report['database_schema'], 'dry_run': report['dry_run']},
                     ensure_ascii=False))
    return 1 if report['failures'] or any(o not in {'placed', 'already', 'planned'} for o in outcomes) else 0


if __name__ == '__main__':
    sys.exit(main())
