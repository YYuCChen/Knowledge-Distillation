"""Content identity for a captured platform snapshot, excluding session metadata."""
import hashlib
import json
from pathlib import Path


class SourceVersionError(ValueError):
    pass


def snapshot_key(material):
    metadata = material.metadata
    media = [(m.kind, m.sha256) for m in getattr(material, 'members', ())]
    path = None if hasattr(material, 'members') else getattr(material, 'media_path', None)
    if path is not None and Path(path).is_file() and not media:
        with Path(path).open('rb') as stream:
            media.append(('audio_or_video', hashlib.file_digest(stream, 'sha256').hexdigest()))
    selected = {
        'title': metadata.get('source_title'),
        'body': metadata.get('original_description'),
        'captions': metadata.get('captions'),
        'media': media,
    }
    return hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def migrate_material_snapshots(connection):
    # Keep every old local capture id and every SourceFact/evidence reference.
    # Stable platform identity remains (source_kind, source_key); local rows
    # distinguish immutable captures of that identity.
    connection.execute('''CREATE TABLE materials_v10 (
        material_id INTEGER PRIMARY KEY,
        source_kind TEXT NOT NULL, source_key TEXT NOT NULL,
        submitted_url TEXT NOT NULL, canonical_url TEXT NOT NULL,
        metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
        snapshot_key TEXT NOT NULL DEFAULT 'legacy',
        UNIQUE (source_kind, source_key, snapshot_key)
    )''')
    connection.execute('''INSERT INTO materials_v10
        (material_id, source_kind, source_key, submitted_url, canonical_url, metadata_json, created_at)
        SELECT material_id, source_kind, source_key, submitted_url, canonical_url, metadata_json, created_at FROM materials''')
    connection.execute('DROP TABLE materials')
    connection.execute('ALTER TABLE materials_v10 RENAME TO materials')
    connection.execute('''CREATE TRIGGER material_snapshot_identity_no_update
        BEFORE UPDATE OF source_kind, source_key, snapshot_key ON materials BEGIN
        SELECT RAISE(ABORT, 'Material snapshot identity is immutable'); END''')
    connection.execute('''CREATE TRIGGER material_snapshot_metadata_no_update
        BEFORE UPDATE OF metadata_json, submitted_url, canonical_url ON materials
        WHEN EXISTS (SELECT 1 FROM source_facts WHERE material_id = OLD.material_id) BEGIN
        SELECT RAISE(ABORT, 'SourceFact metadata is immutable'); END''')
