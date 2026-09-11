"""Real Windows boundary tests; all secrets and files are disposable."""
import hashlib
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows native interfaces')


def test_dpapi_large_secret_persists_without_plaintext_and_tamper_fails(tmp_path):
    from knowledge_distiller.v1.windows_credentials import WindowsKeychain
    value = '合成cookie-secret-' * 2000
    backend = WindowsKeychain(tmp_path / '独立 凭据')
    assert backend.save('test-service', 'test-account', value) == 0
    files = list(backend.root.iterdir())
    assert len(files) == 1
    assert b'cookie-secret' not in files[0].read_bytes()
    fresh = WindowsKeychain(backend.root)
    assert fresh.load('test-service', 'test-account') == (0, value)
    assert fresh.set_label('test-service', 'test-account', '测试标签') == 0
    assert fresh.load('test-service', 'test-account') == (0, value)
    assert fresh.load('different-service', 'test-account') == (-25300, None)
    damaged = bytearray(files[0].read_bytes())
    damaged[-1] ^= 1
    files[0].write_bytes(damaged)
    assert fresh.load('test-service', 'test-account') == (-1, None)
    assert fresh.clear('test-service', 'test-account') == 0
    assert fresh.clear('test-service', 'test-account') == -25300


def test_windows_copy_no_clobber_and_unicode_open(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import source_files as files
    content = '原始文本'.encode()
    key = hashlib.sha256(content).hexdigest()
    path = files.retain_copy(tmp_path, 'markdown', key, '中文 文件.md', content)
    assert files.read_copy(tmp_path, 'markdown', key, path.name) == content
    calls = []
    monkeypatch.setattr(files.os, 'startfile', calls.append)
    files.open_copy(tmp_path, 'markdown', key, path.name)
    assert calls == [str(path)]
    path.write_bytes(b'edited')
    with pytest.raises(files.SourceCopyError, match='已被修改'):
        files.retain_copy(tmp_path, 'markdown', key, path.name, content)
    assert path.read_bytes() == b'edited'


def test_chrome_standard_install_path_and_missing(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import desktop_paths
    for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(desktop_paths.shutil, 'which', lambda name: None)
    assert not desktop_paths.chrome_executable().is_file()
    chrome = tmp_path / 'LOCALAPPDATA/Google/Chrome/Application/chrome.exe'
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    assert desktop_paths.chrome_executable() == chrome


def test_folder_selection_cancel_and_failure(monkeypatch):
    from knowledge_distiller.v1 import desktop_paths, settings
    monkeypatch.setattr(desktop_paths, 'choose_windows_folder', lambda: None)
    assert settings.choose_vault() is None
    def fail():
        raise OSError('native dialog failure')
    monkeypatch.setattr(desktop_paths, 'choose_windows_folder', fail)
    with pytest.raises(settings.SettingsError, match='vault_picker_failed'):
        settings.choose_vault()


def test_frozen_opencli_uses_bundled_node_without_path(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import opencli_session
    from types import SimpleNamespace
    page = tmp_path / 'opencli/dist/src/browser/page.js'
    page.parent.mkdir(parents=True)
    page.touch()
    node = tmp_path / 'bin/node.exe'
    node.parent.mkdir()
    node.touch()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(opencli_session.shutil, 'which', lambda name: None)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout='{"ok":true}', returncode=0)
    monkeypatch.setattr(opencli_session.subprocess, 'run', run)
    assert opencli_session.read_opencli('read', 'x', 'https://x.com/test') == {'ok': True}
    assert calls[0][0][0] == str(node)
    assert calls[0][1]['creationflags'] == opencli_session.subprocess.CREATE_NO_WINDOW


def test_chrome_cleanup_targets_only_owned_live_pid(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import douyin_session
    from types import SimpleNamespace
    session = douyin_session.DouyinOwnedSession(None, tmp_path, secret_factory=lambda account: None)
    calls = []
    state = {'alive': True}
    process = SimpleNamespace(pid=12345, poll=lambda: None if state['alive'] else 0)
    session._process = process
    def run(command, **kwargs):
        calls.append(command)
        state['alive'] = False
    monkeypatch.setattr(douyin_session.subprocess, 'run', run)
    session.close()
    assert calls[0][1:] == ['/PID', '12345', '/T', '/F']
    session.close()
    assert len(calls) == 1
