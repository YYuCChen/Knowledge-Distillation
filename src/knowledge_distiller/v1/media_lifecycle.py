"""Release owned platform bytes, preserving immutable media identity and text.

Empty content is an explicit released representation, never a replacement image.
The migration watermark excludes every pre-upgrade material from automatic release.
"""
from pathlib import Path
import hashlib
import re
import sqlite3

from .database import connect

PLATFORMS = "('douyin','youtube','xiaohongshu','x','zhihu','weibo','bilibili','image')"
# All references must have ended. Failure and pending review protect even a
# frozen legacy fact, which may still need OCR review on retry.
ELIGIBLE = f"""m.source_kind IN {PLATFORMS}
    AND EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=m.material_id)
    AND NOT EXISTS (SELECT 1 FROM distill_items i WHERE i.material_id=m.material_id
        AND (i.confirmation_json IS NOT NULL OR i.state='working'
             OR (i.dismissed_at IS NULL AND i.state!='succeeded')))
    AND EXISTS (SELECT 1 FROM source_facts sf WHERE sf.material_id=m.material_id)"""


def migrate(db):
    db.execute("""CREATE TABLE media_lifecycle (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        legacy_material_id INTEGER NOT NULL,
        released_bytes INTEGER NOT NULL DEFAULT 0,
        compacted_bytes INTEGER NOT NULL DEFAULT 0)""")
    db.execute('INSERT INTO media_lifecycle(singleton,legacy_material_id) SELECT 1,COALESCE(MAX(material_id),0) FROM materials')
    db.execute('DROP TRIGGER source_media_no_update')
    # Keep the old delete/insert guards. A frozen identity can only lose content,
    # and only once all owners have finished or explicitly dismissed their task.
    db.execute(f"""CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media
        WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id=OLD.material_id)
        AND NOT (NEW.material_id=OLD.material_id AND NEW.member_id=OLD.member_id
            AND NEW.position=OLD.position AND NEW.mime_type=OLD.mime_type
            AND NEW.sha256=OLD.sha256 AND length(OLD.content)>0
            AND typeof(NEW.content)='blob' AND length(NEW.content)=0
            AND EXISTS (SELECT 1 FROM materials m WHERE m.material_id=OLD.material_id AND {ELIGIBLE}))
        BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END""")


def preview(path):
    """Read-only inventory works on pre-migration databases too; never initializes."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(f"""SELECT m.material_id,m.source_kind,m.source_key,
            sm.member_id,sm.sha256,length(sm.content) AS bytes,
            CASE WHEN {ELIGIBLE} THEN 'eligible' ELSE 'protected' END AS disposition,
            (SELECT group_concat(item_id) FROM distill_items i WHERE i.material_id=m.material_id) AS item_ids
            FROM source_media sm JOIN materials m USING(material_id)
            WHERE length(sm.content)>0 ORDER BY m.material_id,sm.position""").fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def release_completed(path):
    """Automatic policy for new captures only; no historical authorization implied."""
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute(f"""SELECT m.material_id,SUM(length(sm.content)) AS bytes
            FROM materials m JOIN source_media sm USING(material_id)
            WHERE m.material_id>(SELECT legacy_material_id FROM media_lifecycle WHERE singleton=1)
            AND {ELIGIBLE} AND length(sm.content)>0 GROUP BY m.material_id""").fetchall()
        released = sum(row['bytes'] for row in rows)
        for row in rows:
            db.execute("UPDATE source_media SET content=X'' WHERE material_id=? AND length(content)>0", (row['material_id'],))
        db.execute('UPDATE media_lifecycle SET released_bytes=released_bytes+? WHERE singleton=1', (released,))
        return released


def preview_vault(path, vault):
    """Inventory only paths derivable from recorded publications and media hashes.

    Matching bytes prove the known copy, not permission to remove references.
    All Markdown references are listed; notes are never rewritten by this API.
    """
    vault = Path(vault).absolute()
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(f"""SELECT sm.*,sf.source_fact_id,k.published_path,k.published_vault
            FROM source_media sm JOIN materials m USING(material_id)
            JOIN source_facts sf USING(material_id) JOIN knowledge_results k USING(source_fact_id)
            WHERE m.source_kind IN {PLATFORMS} AND k.published_path IS NOT NULL""").fetchall()
    finally:
        db.close()
    notes = []
    if not vault.is_symlink():
        for note in vault.rglob('*.md'):
            if not any(p.is_symlink() for p in (note, *note.parents)):
                notes.append((note.relative_to(vault).as_posix(), note.read_text(errors='replace')))
    results = []
    extensions = {'image/png':'png','image/jpeg':'jpg','image/webp':'webp','video/mp4':'mp4'}
    for row in rows:
        if row['published_vault'] != str(vault) or row['mime_type'] not in extensions:
            continue
        note = Path(row['published_path'])
        name = f"sf-{row['source_fact_id']}-{row['member_id']}-{row['sha256']}.{extensions[row['mime_type']]}"
        relative = note.parent / '附件' / re.sub(r'[\[\]#^]', '-', note.stem) / name
        target = vault / relative
        # Malformed identities or publication paths do not grant filesystem scope.
        if relative.is_absolute() or '..' in relative.parts or '/' in row['member_id']:
            continue
        state, size = 'missing', 0
        if any(p.is_symlink() for p in (target, *target.parents)):
            state = 'protected_symlink'
        elif target.is_file():
            size = target.stat().st_size
            with target.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            state = 'known_copy' if actual == row['sha256'] else 'protected_modified'
        results.append({'path': str(target), 'bytes': size, 'sha256': row['sha256'],
                        'disposition': state, 'references': [p for p, text in notes if name in text],
                        'action': 'preview_only_requires_approved_backup_and_note_review'})
    return results


def compact(path):
    """Reclaim SQLite free pages after releases, outside any business transaction.

    SQLite serializes VACUUM with other writers. Busy/disk errors remain visible
    to the owning cleanup boundary and can be retried after restart.
    """
    with connect(path) as db:
        row = db.execute('SELECT released_bytes,compacted_bytes FROM media_lifecycle WHERE singleton=1').fetchone()
        if row['released_bytes'] == row['compacted_bytes']:
            return 0
        before = Path(path).stat().st_size
        db.execute('VACUUM')
        db.execute('UPDATE media_lifecycle SET compacted_bytes=? WHERE singleton=1', (row['released_bytes'],))
        return max(0, before - Path(path).stat().st_size)
