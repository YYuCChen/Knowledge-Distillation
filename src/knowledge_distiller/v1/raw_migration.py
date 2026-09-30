"""One-time export of existing V1 source facts into raw/外部/ (raw-interface §7).

It reads the application database read-only and never changes its schema, so
an installed V1.3 keeps opening it. It never touches V1 notes or any existing
vault file and only creates files. Ids follow ``materials.created_at``; running
it again adds nothing, and a copy of the same data yields the same bytes.

    python -m knowledge_distiller.v1.raw_migration --data-dir DIR --vault VAULT [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import fcntl
import json
from pathlib import Path
import sqlite3
import sys

from . import raw


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


def _read_only(database: Path):
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def plan(database: Path, vault: Path):
    """Deterministic list of (material_id, raw_id, document) still to create."""
    present = existing_subjects(vault)
    planned, counters = [], {}
    with _read_only(database) as db:
        version = int(db.execute('PRAGMA user_version').fetchone()[0])
        ledger = {}
        if version >= 20:
            ledger = {row['subject_id']: row['raw_id'] for row in db.execute(
                "SELECT subject_id, raw_id FROM raw_records WHERE subject_kind='material'")}
            counters = {row['day']: row['last'] for row in db.execute('SELECT day, last FROM raw_counters')}
        rows = db.execute('''SELECT m.material_id FROM materials m JOIN source_facts sf USING(material_id)
                             ORDER BY m.created_at, m.material_id''').fetchall()
        failures = []
        for row in rows:
            material_id = row['material_id']
            if material_id in present or material_id in ledger:
                continue
            record = raw.material_row(db, material_id)
            day = raw._local(record['created_at']).strftime('%Y%m%d')
            number = max(counters.get(day, 0), raw.vault_ids(vault, day)) + 1
            raw_id = f'R-{day}-{number:04d}'
            try:
                document = raw.render_material(record, raw.media(db, material_id), raw_id,
                                               app_version=MIGRATED_VERSION, migrated=True)
            except (ValueError, KeyError, TypeError) as error:
                failures.append({'material_id': material_id, 'error': type(error).__name__})
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
        with _read_only(database) as db:
            media_bytes = {}
            for material_id, raw_id, document in planned:
                for attachment in document.attachments:
                    row = db.execute('SELECT content FROM source_media WHERE material_id=? AND member_id=?',
                                     (material_id, attachment.member_id)).fetchone()
                    media_bytes[(material_id, attachment.member_id)] = bytes(row['content']) if row else b''
        writer = sqlite3.connect(database) if version >= 20 else None
        try:
            for material_id, raw_id, document in planned:
                outcome = _write(vault, raw_id, document, media_bytes, material_id)
                report['results'].append({'material_id': material_id, 'raw_id': raw_id,
                                          'path': document.relative_path, 'outcome': outcome})
                if writer is not None and outcome in {'placed', 'already'}:
                    writer.row_factory = sqlite3.Row
                    with writer:
                        raw.insert(writer, raw_id, 'material', material_id, '第三方', document, origin='migration',
                                   written_at=datetime.now(UTC).isoformat(), written_vault=str(vault))
        finally:
            if writer is not None:
                writer.close()
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
