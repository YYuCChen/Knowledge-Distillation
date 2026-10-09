"""Explicit collection display choices, separate from execution (R09)."""
from .database import connect


class CollectionVisibilityError(ValueError):
    pass


class CollectionVisibility:
    """Persist one visible/hidden setting per existing stable operation ID.

    Missing settings mean visible. Hiding never cancels or removes work;
    resuming elsewhere does not erase an existing explicit display choice.
    """

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _key(operation_id):
        if type(operation_id) is not int or not 0 < operation_id <= 2**63 - 1:
            raise CollectionVisibilityError('collection_visibility_invalid_id')
        return f'collection_visibility:{operation_id}'

    @staticmethod
    def _operation(db, operation_id):
        row = db.execute('SELECT state FROM collection_operations WHERE operation_id=?',
                         (operation_id,)).fetchone()
        if row is None:
            raise CollectionVisibilityError('collection_visibility_not_found')
        return row

    @staticmethod
    def _read(db, key):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        value = row['value'] if row is not None else 'visible'
        if value not in {'visible', 'hidden'}:
            raise CollectionVisibilityError('collection_visibility_invalid_setting')
        return value

    def visibility(self, operation_id):
        key = self._key(operation_id)
        with connect(self.store.path) as db:
            db.execute('BEGIN')
            self._operation(db, operation_id)
            return self._read(db, key)

    def hide(self, operation_id):
        return self._set(operation_id, 'hidden')

    def restore(self, operation_id):
        return self._set(operation_id, 'visible')

    def _set(self, operation_id, value):
        key = self._key(operation_id)
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            operation = self._operation(db, operation_id)
            if value == 'hidden':
                working_member = db.execute('''SELECT 1 FROM collection_members cm
                    JOIN distill_items i USING(item_id)
                    WHERE cm.operation_id=? AND i.state='working' LIMIT 1''',
                    (operation_id,)).fetchone()
                if operation['state'] not in {'cancelled', 'partial', 'failed', 'succeeded'} or working_member:
                    raise CollectionVisibilityError('collection_visibility_active')
            db.execute('''INSERT INTO settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE settings.value != excluded.value''', (key, value))
        return value
