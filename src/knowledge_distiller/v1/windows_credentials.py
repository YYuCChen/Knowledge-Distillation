"""Current-user Windows DPAPI storage, including large browser cookie secrets.

Only ciphertext is persisted; Windows owns the protection key. Files cannot be
decrypted by a different Windows user or copied to another machine as credentials.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, *, decrypt: bool = False) -> bytes:
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(value)
    source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately not LOCAL_MACHINE.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError(ctypes.get_last_error(), 'windows_secret_protection_failed')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class WindowsKeychain:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.environ['LOCALAPPDATA']) / 'Knowledge Distiller' / 'credentials'

    def _path(self, service, account):
        key = hashlib.sha256(json.dumps([service, account], ensure_ascii=False).encode()).hexdigest()
        return self.root / (key + '.dpapi')

    def _write(self, path, value):
        encrypted = _crypt(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.secret-', dir=self.root)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def save(self, service, account, value):
        try:
            self._write(self._path(service, account), {'value': value})
            return 0
        except OSError:
            return -1

    def load(self, service, account):
        try:
            data = json.loads(_crypt(self._path(service, account).read_bytes(), decrypt=True))
            return 0, data['value']
        except FileNotFoundError:
            return -25300, None
        except (OSError, ValueError, KeyError, TypeError):
            return -1, None

    def set_label(self, service, account, label):
        status, value = self.load(service, account)
        if status:
            return status
        try:
            self._write(self._path(service, account), {'value': value, 'label': label})
            return 0
        except OSError:
            return -1

    def clear(self, service, account):
        try:
            self._path(service, account).unlink()
            return 0
        except FileNotFoundError:
            return -25300
        except OSError:
            return -1


class WindowsLocalSecret:
    """Retain existing per-user DPAPI files while exposing current credential state."""
    def __init__(self, store, account):
        self.store, self.account = store, account
        self.path = WindowsKeychain(store.root)._path('com.knowledge-distiller.credentials', account)

    def _read(self):
        from .local_secrets import SecretError
        self.store._directory()
        if self.path.is_symlink() or getattr(self.path, 'is_junction', lambda: False)():
            raise SecretError('credential_path_unsafe')
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise SecretError('credential_file_unsafe')
            value = json.loads(_crypt(self.path.read_bytes(), decrypt=True))
            if not isinstance(value, dict) or not isinstance(value.get('value'), str) or not value['value']:
                raise ValueError()
            state = value.get('state', 'validated')  # Existing Windows credentials stay usable.
            if state not in {'pending_validation', 'validated'}:
                raise ValueError()
            return {'version': 1, 'secret': value['value'], 'state': state,
                    'validated_at': value.get('validated_at')}
        except FileNotFoundError:
            raise SecretError('credential_missing') from None
        except (OSError, ValueError, TypeError, UnicodeError):
            raise SecretError('credential_unavailable') from None

    def load(self):
        return self._read()['secret']

    def _write(self, value, state, *, overwrite=True):
        import time
        from .local_secrets import SecretError
        if not isinstance(value, str) or not value or '\n' in value or '\r' in value or len(value.encode()) > 900000:
            raise SecretError('credential_value_invalid')
        self.store._directory()
        if self.path.exists() or self.path.is_symlink():
            self._read()
        temporary = None
        try:
            payload = {'value': value, 'state': state,
                       'validated_at': int(time.time()) if state == 'validated' else None}
            encrypted = _crypt(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            fd, temporary = tempfile.mkstemp(prefix='.credential-', dir=self.store.root)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            if overwrite:
                os.replace(temporary, self.path)
            else:
                try:
                    os.link(temporary, self.path)
                except FileExistsError:
                    raise SecretError('credential_already_saved') from None
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
        pass  # Current labels are derived from application bindings.

    def clear(self):
        from .local_secrets import SecretError
        self.store._directory()
        if self.path.is_symlink() or getattr(self.path, 'is_junction', lambda: False)():
            raise SecretError('credential_path_unsafe')
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise SecretError('credential_clear_failed') from None
