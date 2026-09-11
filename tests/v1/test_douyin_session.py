import json
from pathlib import Path

import pytest

from knowledge_distiller.v1.chrome import ChromeSessionError
from knowledge_distiller.v1.douyin_session import DouyinOwnedSession
from knowledge_distiller.v1.keychain import KeychainError
from knowledge_distiller.v1.store import Store


class Secrets:
    def __init__(self):
        self.values = {}
    def __call__(self, account):
        owner = self
        class Secret:
            def save(self, value): owner.values[account] = value
            def load(self):
                if account not in owner.values: raise KeychainError('missing')
                return owner.values[account]
            def clear(self): owner.values.pop(account, None)
            def set_label(self, label): pass
        return Secret()


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    secrets = Secrets()
    return store, secrets, DouyinOwnedSession(store, tmp_path / 'profiles', secret_factory=secrets)


def test_cached_credentials_survive_new_session_without_browser(setup, monkeypatch):
    store, secrets, session = setup
    identifier = 'a' * 32
    store.save_connection('douyin', 'test', browser_context='owned:' + identifier)
    secrets('douyin-session-' + identifier).save(json.dumps({'sessionid': 'test-only'}))
    fresh = DouyinOwnedSession(store, session.root, secret_factory=secrets)
    monkeypatch.setattr(fresh, '_launch', lambda *a, **k: pytest.fail('cookie read must not launch browser'))
    assert fresh.cookies() == {'sessionid': 'test-only'}


def test_legacy_connection_never_falls_back_to_daily_chrome(setup, monkeypatch):
    store, _, session = setup
    store.save_connection('douyin', 'legacy')
    monkeypatch.setattr(session, '_launch', lambda *a, **k: pytest.fail('no implicit browser launch'))
    with pytest.raises(ChromeSessionError, match='douyin_login_required'):
        session.browser_page('https://www.douyin.com/')


def test_background_page_uses_owned_headless_profile(setup, monkeypatch):
    store, secrets, session = setup
    identifier = 'b' * 32
    store.save_connection('douyin', 'test', browser_context='owned:' + identifier)
    secrets('douyin-session-' + identifier).save('{"sessionid":"test"}')
    calls = []
    monkeypatch.setattr(session, '_launch', lambda identity, **kwargs: calls.append((identity, kwargs)) or Path('/test-owned-port'))
    monkeypatch.setattr(session, '_page', lambda url, port: (url, port))
    assert session.browser_page('https://www.douyin.com/')[1] == Path('/test-owned-port')
    assert calls == [(identifier, {})]


def test_clear_removes_only_owned_profile_and_secret(setup, tmp_path):
    store, secrets, session = setup
    identifier = 'c' * 32
    profile = session.root / identifier
    profile.mkdir(parents=True)
    (profile / 'Cookies').write_text('test')
    other = tmp_path / 'daily-browser'
    other.mkdir()
    secrets('douyin-session-' + identifier).save('{"sessionid":"test"}')
    session.discard('owned:' + identifier)
    assert not profile.exists()
    assert other.exists()
    assert not secrets.values
    session.discard('owned:../../daily-browser')
    assert other.exists()


def test_failed_login_preserves_previous_credentials(setup, monkeypatch):
    store, secrets, session = setup
    identifier = 'd' * 32
    store.save_connection('douyin', 'test', browser_context='owned:' + identifier)
    secrets('douyin-session-' + identifier).save('{"sessionid":"previous"}')
    before = dict(store.connection('douyin'))
    monkeypatch.setattr(session, '_launch', lambda *a, **k: (_ for _ in ()).throw(ChromeSessionError('douyin_login_required')))
    with pytest.raises(ChromeSessionError): session.verify()
    assert dict(store.connection('douyin')) == before
    assert session.cookies() == {'sessionid': 'previous'}


def test_successful_login_stages_credentials_until_explicit_commit(setup, monkeypatch):
    store, secrets, session = setup
    class Page:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait_for(self, expression, **kwargs): return True
        def evaluate(self, expression): return 'test account'
        def cookies(self, urls): return [{'name': 'sessionid', 'value': 'test-only'}]
    launches = []
    monkeypatch.setattr(session, '_launch', lambda identity, **kwargs: launches.append(kwargs) or Path('/owned-port'))
    monkeypatch.setattr(session, '_page', lambda *args: Page())
    result = session.verify()
    assert launches == [{'headed': True}]
    assert store.connection('douyin') is None
    assert result.browser_context.startswith('owned:')
    store.save_connection('douyin', result.account_label, browser_context=result.browser_context)
    assert session.cookies() == {'sessionid': 'test-only'}


def test_cleared_or_expired_connection_cannot_use_saved_cookies(setup):
    store, secrets, session = setup
    identifier = 'e' * 32
    store.save_connection('douyin', 'test', browser_context='owned:' + identifier)
    secrets('douyin-session-' + identifier).save('{"sessionid":"test"}')
    store.require_relogin('douyin')
    with pytest.raises(ChromeSessionError): session.cookies()
    store.clear_connection('douyin')
    with pytest.raises(ChromeSessionError): session.cookies()

@pytest.mark.parametrize('parent,matching', [('1',True),('123',True),('1',False)])
def test_reclaims_only_orphan_with_exact_owned_profile(setup, monkeypatch, parent, matching):
    import os, signal, subprocess
    store, _, session = setup
    profile=session.root/('e'*32);profile.mkdir(parents=True)
    (profile/'SingletonLock').symlink_to('test-host-12345')
    executable=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    actual=profile if matching else profile.parent/'another-profile'
    monkeypatch.setattr(subprocess,'run',lambda *a,**k: subprocess.CompletedProcess(a,0,
        f'{parent} {executable} --user-data-dir={actual} --headless=new about:blank',''))
    calls=[]
    def kill(pid,sig):
        calls.append((pid,sig))
        if sig==0:raise ProcessLookupError
    monkeypatch.setattr(os,'kill',kill)
    session._stop_orphan(profile,executable)
    assert calls==([(12345,signal.SIGTERM),(12345,0)] if parent=='1' and matching else [])
