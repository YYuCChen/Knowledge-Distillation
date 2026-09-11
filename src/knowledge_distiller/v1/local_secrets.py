"""Application-owned credentials. Legacy Keychain is read only on explicit import."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from .keychain import KeychainError


class SecretError(KeychainError):
    pass


class LocalSecrets:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def __call__(self, account):
        if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}', account):
            raise SecretError('credential_account_invalid')
        return LocalSecret(self, account)

    def _directory(self):
        try:
            # Never follow a symlink in the application credential path.
            for parent in (*reversed(self.root.parents), self.root):
                if parent.is_symlink(): raise SecretError('credential_path_unsafe')
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise SecretError('credential_permissions_unsafe')
        except OSError:
            raise SecretError('credential_directory_unavailable') from None

    def status(self, account):
        try:
            return self(account)._read()['state']
        except SecretError as error:
            return 'missing' if str(error) == 'credential_missing' else 'unavailable'

    def checked_at(self, account):
        try: return self(account)._read().get('validated_at')
        except SecretError: return None

    def import_legacy(self, account, legacy_factory=None):
        target = self(account)
        if self.status(account) != 'missing': raise SecretError('credential_already_saved')
        if legacy_factory is None:
            from .keychain import KeychainSecret
            legacy_factory = KeychainSecret
        value = legacy_factory(account).load()
        target._write(value, "pending_validation", overwrite=False)
        # No legacy delete, even after successful validation.

    def mark_validated(self, account):
        item = self(account)
        item._write(item.load(), 'validated')


class LocalSecret:
    def __init__(self, store, account):
        self.store, self.account = store, account
        self.path = store.root / (account + '.json')

    def _read(self):
        self.store._directory()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
                    raise SecretError('credential_file_unsafe')
                data = json.load(stream)
            if (not isinstance(data, dict) or data.get('version') != 1
                or not isinstance(data.get('secret'), str) or not data['secret']
                or data.get('state') not in {'pending_validation', 'validated'}):
                raise SecretError('credential_content_invalid')
            return data
        except FileNotFoundError:
            raise SecretError('credential_missing') from None
        except (OSError, ValueError, UnicodeError):
            raise SecretError('credential_unavailable') from None

    def load(self):
        return self._read()['secret']

    def _write(self, value, state, *, overwrite=True):
        if not isinstance(value, str) or not value or '\n' in value or '\r' in value or len(value.encode()) > 900000:
            raise SecretError('credential_value_invalid')
        self.store._directory()
        if self.path.exists() or self.path.is_symlink(): self._read()
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.credential-', dir=self.store.root)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({'version': 1, 'secret': value, 'state': state, 'validated_at': int(time.time()) if state == 'validated' else None}, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            if overwrite:
                os.replace(temporary, self.path)
            else:
                try: os.link(temporary, self.path)
                except FileExistsError: raise SecretError("credential_already_saved") from None
            directory = os.open(self.store.root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        except OSError:
            raise SecretError('credential_save_failed') from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def save(self, value):
        self._write(value, 'pending_validation')

    def save_validated(self, value):
        self._write(value, 'validated')

    def set_label(self, label):
        """Legacy caller compatibility; local labels come from bindings."""

    def clear(self):
        self.store._directory()
        if self.path.is_symlink(): raise SecretError('credential_path_unsafe')
        try: self.path.unlink(missing_ok=True)
        except OSError: raise SecretError('credential_clear_failed') from None
