import os
import pytest
from knowledge_distiller.v1.local_secrets import LocalSecrets, SecretError


def test_local_roundtrip_and_clear(tmp_path):
    store = LocalSecrets(tmp_path / 'credentials')
    item = store('fake-account')
    item.save('test-only-value')
    assert item.load() == 'test-only-value'
    assert (store.root.stat().st_mode & 0o777) == 0o700
    assert (item.path.stat().st_mode & 0o777) == 0o600
    item.clear(); item.clear()
    with pytest.raises(SecretError): item.load()


def test_rejects_links_permissions_and_corruption(tmp_path):
    store = LocalSecrets(tmp_path / 'credentials')
    item = store('account')
    item.save('fake')
    item.path.chmod(0o644)
    with pytest.raises(SecretError): item.load()
    with pytest.raises(SecretError): item.save('replacement')
    item.path.chmod(0o600)
    item.path.write_text('broken')
    with pytest.raises(SecretError): item.save('replacement')
    item.path.unlink()
    target = tmp_path / 'private'; target.write_text('unchanged')
    item.path.symlink_to(target)
    with pytest.raises(SecretError): item.save('replacement')
    assert target.read_text() == 'unchanged'


def test_import_is_explicit_staged_and_never_deletes_legacy(tmp_path):
    class Legacy:
        def load(self): return 'legacy-test-only'
        def clear(self): pytest.fail('Must never delete legacy')
    store = LocalSecrets(tmp_path / 'credentials')
    store.import_legacy('account', lambda _: Legacy())
    assert store('account').load() == 'legacy-test-only'
    assert store.status('account') == 'pending_validation'
    store.mark_validated('account')
    assert store.status('account') == 'validated'
    with pytest.raises(SecretError): store.import_legacy('account', lambda _: Legacy())


def test_invalid_account_and_root_link(tmp_path):
    store = LocalSecrets(tmp_path / 'credentials')
    with pytest.raises(SecretError): store('../escape')
    other = tmp_path / 'other'; other.mkdir()
    store.root.symlink_to(other)
    with pytest.raises(SecretError): store('account').save('fake')


def test_default_runtime_and_settings_never_read_keychain(tmp_path, monkeypatch):
    from knowledge_distiller.v1.settings import SettingsService
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.douyin_session import DouyinOwnedSession
    from knowledge_distiller.v1.feishu_api import FeishuAPI, FeishuAPIError
    monkeypatch.setattr('knowledge_distiller.v1.keychain.system_keychain', lambda: pytest.fail('Unexpected Keychain'))
    store = Store(tmp_path / 'knowledge.sqlite3'); store.initialize()
    settings = SettingsService(store, qwen_probe=lambda: True)
    settings.activate_llm('https://example.test', 'test-model', 'test-api-secret')
    assert settings.llm_secret() == 'test-api-secret'
    session = DouyinOwnedSession(store, tmp_path / 'browser')
    session._secret('a' * 32).save('{"test":"fake-cookie"}')
    assert session._secret('a' * 32).load() == '{"test":"fake-cookie"}'
    assert 'test-api-secret' not in str(settings.view())
    api = FeishuAPI('cli_test')
    try:
        with pytest.raises(FeishuAPIError): api.secret_loader()
    finally: api.close()


def test_failed_replace_preserves_old_value(tmp_path, monkeypatch):
    item = LocalSecrets(tmp_path / 'credentials')('account')
    item.save('old-fake')
    def fail(*args): raise OSError('private message must not leak')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(SecretError, match='credential_save_failed'): item.save('new-fake')
    assert item.load() == 'old-fake'
    assert len(list(item.store.root.iterdir())) == 1


def test_paths_never_ask_users_for_session_credentials_or_read_keychain(tmp_path, monkeypatch):
    from knowledge_distiller.v1.settings import SettingsService
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.web import create_app
    store=Store(tmp_path/'knowledge.sqlite3');store.initialize()
    settings=SettingsService(store,qwen_probe=lambda:True)
    store.set_settings({'llm_secret_account':'legacy-account','llm_provider':'openai',
                        'llm_base_url':'https://example.test','llm_model':'model'})
    monkeypatch.setattr('knowledge_distiller.v1.keychain.KeychainSecret',lambda _:pytest.fail('runtime read Keychain'))
    settings.local_secrets('legacy-account').save('private-test-value')
    client=create_app(store, object(), settings).test_client()
    page=client.get('/settings?open=paths')
    panel=page.text.split('<strong>路径管理</strong>',1)[1]
    assert 'private-test-value' not in page.text
    assert '密钥管理' in panel
    assert page.text.index('飞书机器人') < page.text.index('密钥管理')
    assert 'name="secret"' not in panel and 'name="account"' not in panel
    assert 'id="llm-key-form"' not in panel and 'id="doubao-credentials"' not in panel
    assert '从钥匙串导入' not in page.text
    assert client.post('/settings/credentials',data={'action':'import'}).status_code==404
    assert page.text.index('id="llm-key-form"') < page.text.index('<strong>路径管理</strong>')
    assert LocalSecrets(tmp_path/'credentials')('legacy-account').load()=='private-test-value'


def test_slow_import_does_not_overwrite_a_concurrent_manual_save(tmp_path):
    store=LocalSecrets(tmp_path/'credentials')
    class Legacy:
        def load(self):
            store('account').save('new-manual-value')
            return 'old-import-value'
    with pytest.raises(SecretError,match='credential_already_saved'):
        store.import_legacy('account',lambda _:Legacy())
    assert store('account').load()=='new-manual-value'
