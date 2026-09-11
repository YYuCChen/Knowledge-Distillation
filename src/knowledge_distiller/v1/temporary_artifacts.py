"""Targeted expiry of this database's item-owned working files."""
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import shutil
import sqlite3

from .database import connect

logger = logging.getLogger(__name__)
MARKER = '.retained-at.json'


class TemporaryArtifacts:
    def __init__(self, store, runtime_root):
        self.store = store
        self.root = Path(runtime_root)

    def _directory(self, item):
        target = self.root / 'items' / str(item)
        # Never follow an item or runtime symlink into user-owned files.
        if any(p.is_symlink() for p in (self.root, self.root / 'items', target)):
            raise OSError('temporary path is a symbolic link')
        return target

    def prepare(self, item):
        target = self._directory(item)
        marker = target / MARKER
        now = datetime.now(UTC)
        retained = now
        if target.exists():
            retained = self._retained_at(item, target)
            if (now - retained >= timedelta(hours=72)
                    and not self._needs_media(self.store.item_bundle(item))
                    and not self._shared_needs_media(item)):
                # Called by the sole worker before acquiring replacement bytes.
                if not self._remote_cleanup_pending(target):
                    shutil.rmtree(target)
                    retained = now
        target.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            with marker.open('x') as output:
                json.dump({'database': str(self.store.path.resolve()), 'item': item,
                           'retained_at': retained.isoformat()}, output)
        elif marker.is_symlink():
            raise OSError('temporary marker is a symbolic link')

    def _retained_at(self, item, target):
        marker = target / MARKER
        if marker.is_symlink():
            raise OSError('temporary marker is a symbolic link')
        if marker.exists():
            value = json.loads(marker.read_text())
            if value['database'] != str(self.store.path.resolve()) or value['item'] != item:
                raise ValueError('temporary owner mismatch')
            stamp = datetime.fromisoformat(value['retained_at'])
        else:
            # Pre-marker working files belong to this known item. Queue creation
            # is a conservative upper bound on their age; never extend retention.
            row = self.store.item_bundle(item)
            metadata = json.loads(row['metadata_json'] or '{}')
            stamp = datetime.fromisoformat(metadata.get('captured_at') or row['created_at'])
        if stamp.tzinfo is None:
            raise ValueError('temporary timestamp has no timezone')
        return stamp

    def clean_item(self, item, *, now=None):
        now = now or datetime.now(UTC)
        diagnostic = f'temporary_cleanup_item_{item}'
        try:
            target = self._directory(item)
            if not target.exists():
                self._clear_diagnostic(diagnostic)
                return
            row = self.store.item_bundle(item)
            if row is None:
                return
            # Pending confirmation owns its audio independently of failed state.
            if row['confirmation_json'] is not None or row['state'] == 'working':
                return
            if self._needs_media(row):
                return
            if self._shared_needs_media(item):
                return
            unsupported = (row['error_code'] or '').endswith('_input_unsupported')
            if row['source_fact_id'] is None and not unsupported and now - self._retained_at(item, target) < timedelta(hours=72):
                return
            if self._remote_cleanup_pending(target):
                raise OSError("remote ASR cleanup pending; retry recognition first")
            shutil.rmtree(target)
            self._clear_diagnostic(diagnostic)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Preserve the business outcome; retry this exact residue next pass.
            self.store.set_setting(diagnostic, type(error).__name__)
            logger.error('Temporary cleanup for item %s failed (%s)',item,type(error).__name__)

    @staticmethod
    def _needs_media(row):
        # A retry before SourceFact still needs capture/ASR/OCR bytes. Elapsed
        # time alone is not abandonment; an explicit dismissal ends ownership.
        return bool(row is not None and row['source_fact_id'] is None
                    and row['dismissed_at'] is None
                    and not (row['error_code'] or '').endswith('_input_unsupported'))

    def _shared_needs_media(self, item):
        # A captured path can belong to a snapshot reused by another queue row.
        # Completion of this row alone does not end that other row's ownership.
        with connect(self.store.path) as db:
            return db.execute('''SELECT 1 FROM distill_items i
                WHERE i.item_id!=? AND i.material_id=(SELECT material_id FROM distill_items WHERE item_id=?)
                AND (i.confirmation_json IS NOT NULL OR i.state='working'
                     OR (i.dismissed_at IS NULL AND i.state!='succeeded')) LIMIT 1''', (item, item)).fetchone() is not None

    @staticmethod
    def _remote_cleanup_pending(target):
        # Preserve the only remote task/object identity until its cleanup succeeds.
        for path in target.rglob('seed-asr-*.json'):
            state = json.loads(path.read_text())
            if not state.get('cleaned'):
                return True
        return False

    def _clear_diagnostic(self, key):
        with connect(self.store.path) as db:
            db.execute('DELETE FROM settings WHERE key=?',(key,))

    def sweep(self):
        self.store.expire_submitted_sources()
        self.store.expire_platform_media()
        from .media_lifecycle import release_completed, compact
        try:
            release_completed(self.store.path)
            compact(self.store.path)
            self._clear_diagnostic('platform_media_cleanup')
        except (OSError, sqlite3.Error) as error:
            self.store.set_setting('platform_media_cleanup', type(error).__name__)
            logger.error('Platform media cleanup failed (%s)', type(error).__name__)
        with connect(self.store.path) as db:
            ids = [row[0] for row in db.execute("SELECT item_id FROM distill_items WHERE state!='working' AND confirmation_json IS NULL")]
        for item in ids:
            self.clean_item(item)
