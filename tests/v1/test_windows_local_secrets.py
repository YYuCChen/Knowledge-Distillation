"""Actual DPAPI integration, restricted to disposable synthetic credentials."""
import sys
import pytest

from knowledge_distiller.v1.local_secrets import LocalSecrets, SecretError
from knowledge_distiller.v1.windows_credentials import WindowsKeychain

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native DPAPI')


def test_existing_windows_secret_and_current_validation_state(tmp_path):
    root = tmp_path / 'credentials'
    old = WindowsKeychain(root)
    assert old.save('com.knowledge-distiller.credentials', 'sample', 'synthetic-old') == 0
    current = LocalSecrets(root)
    assert current('sample').load() == 'synthetic-old'
    assert current.status('sample') == 'validated'
    current('sample').save('synthetic-new')
    assert current.status('sample') == 'pending_validation'
    current.mark_validated('sample')
    assert current.status('sample') == 'validated'
    assert isinstance(current.checked_at('sample'), int)
    assert old.load('com.knowledge-distiller.credentials', 'sample') == (0, 'synthetic-new')
    assert b'synthetic-new' not in current('sample').path.read_bytes()


def test_secret_no_clobber_and_corruption_remain_visible(tmp_path):
    current = LocalSecrets(tmp_path / 'credentials')
    item = current('sample')
    item.save_validated('synthetic-original')
    original = item.path.read_bytes()
    with pytest.raises(SecretError, match='credential_already_saved'):
        item._write('replacement', 'pending_validation', overwrite=False)
    assert item.path.read_bytes() == original
    damaged = bytearray(original)
    damaged[-1] ^= 1
    item.path.write_bytes(damaged)
    assert current.status('sample') == 'unavailable'
    with pytest.raises(SecretError, match='credential_unavailable'):
        item.save('replacement')
    assert item.path.read_bytes() == damaged
    assert not list(item.path.parent.glob('.credential-*'))
