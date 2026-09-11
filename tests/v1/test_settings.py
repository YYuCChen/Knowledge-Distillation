from pathlib import Path

import pytest

from knowledge_distiller.v1.chrome import ChromeSessionError, DouyinConnection
from knowledge_distiller.v1.keychain import KeychainError
from knowledge_distiller.v1.settings import (
    AuthorizedDouyinSession,
    SettingsError,
    SettingsService,
)
from knowledge_distiller.v1.store import Store


class Secrets:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.fail_save: set[str] = set()
        self.fail_load: set[str] = set()

    def factory(self, account: str):
        owner = self

        class Secret:
            def set_label(self, label: str) -> None:
                owner.labels[account] = label

            def save(self, value: str) -> None:
                if account in owner.fail_save:
                    raise KeychainError("keychain_save_failed")
                owner.values[account] = value

            def load(self) -> str:
                if account in owner.fail_load or account not in owner.values:
                    raise KeychainError("keychain_secret_unavailable")
                return owner.values[account]

            def clear(self) -> None:
                owner.values.pop(account, None)

        return Secret()


class Chrome:
    def __init__(self, *, label: str | None = "@测试账号", error=None):
        self.label = label
        self.error = error
        self.verify_calls = 0
        self.cookie_calls = 0

    def verify(self):
        self.verify_calls += 1
        if self.error:
            raise self.error
        return DouyinConnection(self.label)

    def cookies(self):
        self.cookie_calls += 1
        if self.error:
            raise self.error
        return {"sessionid": "private"}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "knowledge.sqlite3")
    result.initialize()
    return result


def service(store: Store, secrets: Secrets | None = None, **kwargs) -> SettingsService:
    secrets = secrets or Secrets()
    return SettingsService(
        store,
        keychain_factory=secrets.factory,
        qwen_probe=lambda: True,
        slot_id=iter(("one", "two", "three")).__next__,
        **kwargs,
    )


def test_static_view_reads_no_secret_browser_or_provider(store: Store) -> None:
    chrome = Chrome(error=AssertionError("must not connect"))
    secrets = Secrets()
    settings = service(store, secrets, chrome=chrome)

    view = settings.view()

    assert len(view["platforms"]) == 6
    assert view["platforms"][0]["state"] == "unconfigured"
    assert view["llm"]["state"] == "unconfigured"
    assert view["asr"]["state"] == "unconfigured"
    assert view["vault"] == {
        "state": "unconfigured",
        "path": "-",
        "action": "选择位置",
        "registration": "unregistered",
        "openable": False,
        "message": "尚未选择笔记保存位置。",
    }
    assert chrome.verify_calls == 0
    assert secrets.values == {}


def test_douyin_connection_is_explicit_and_local_clear_preserves_browser(
    store: Store,
) -> None:
    chrome = Chrome()
    settings = service(store, chrome=chrome)

    settings.connect_douyin()
    connected = store.connection("douyin")
    settings.clear_douyin()

    assert connected["state"] == "connected"
    assert connected["generation"] == 1
    assert connected["account_label"] == "@测试账号"
    assert store.connection("douyin")["state"] == "unconfigured"
    assert chrome.verify_calls == 1
    assert chrome.cookie_calls == 0


def test_failed_llm_replacement_keeps_old_active_and_never_stores_secret(
    store: Store,
) -> None:
    secrets = Secrets()
    settings = service(store, secrets)
    settings.activate_llm("https://models.example.com", "model-1", "first-secret")
    old_values = store.settings()
    secrets.fail_save.add("llm-api-key-two")

    with pytest.raises(SettingsError) as failure:
        settings.activate_llm("https://new.example.com", "model-2", "second-secret")

    assert failure.value.args == ("llm_save_failed",)
    assert store.settings() == old_values
    assert secrets.values == {"llm-api-key-one": "first-secret"}
    assert "first-secret" not in repr(store.settings())
    assert "second-secret" not in repr(store.settings())


def test_successful_llm_replacement_switches_slot_then_clears_old(store: Store) -> None:
    secrets = Secrets()
    settings = service(store, secrets)
    settings.activate_llm("https://models.example.com", "model-1", "first")

    settings.activate_llm("https://new.example.com", "model-2", "second")

    assert store.settings()["llm_secret_account"] == "llm-api-key-two"
    assert settings.llm_secret() == "second"
    assert secrets.values == {"llm-api-key-two": "second"}


def test_missing_active_secret_marks_model_unavailable(store: Store) -> None:
    secrets = Secrets()
    settings = service(store, secrets)
    settings.activate_llm("https://models.example.com", "model-1", "first")
    secrets.fail_load.add("llm-api-key-one")

    assert settings.llm_secret() == ""
    assert settings.view()["llm"]["state"] == "unavailable"


def test_asr_activates_only_when_runtime_is_present(store: Store) -> None:
    missing = SettingsService(store, qwen_probe=lambda: False)
    with pytest.raises(SettingsError) as failure:
        missing.activate_asr()

    assert failure.value.args == ("asr_runtime_unavailable",)
    assert missing.view()["asr"]["state"] == "unconfigured"

    service(store).activate_asr()
    assert service(store).view()["asr"]["state"] == "configured"


@pytest.mark.parametrize("initialized", [False, True])
def test_vault_selection_accepts_existing_folders(
    store: Store, tmp_path: Path, initialized: bool
) -> None:
    vault = tmp_path / "My Vault"
    vault.mkdir()
    if initialized:
        (vault / ".obsidian").mkdir()
    settings = service(store, vault_picker=lambda: vault)

    assert settings.choose_vault() is True
    assert settings.view()["vault"]["state"] == "configured"
    assert store.setting("vault_path") == str(vault.resolve())

    invalid = tmp_path / "ordinary folder"
    invalid.write_text("not a folder")
    with pytest.raises(SettingsError):
        service(store, vault_picker=lambda: invalid).choose_vault()
    assert store.setting("vault_path") == str(vault.resolve())


def test_authorized_session_gates_use_and_records_expired_login(store: Store) -> None:
    chrome = Chrome()
    session = AuthorizedDouyinSession(store, chrome)
    with pytest.raises(ChromeSessionError) as missing:
        session.cookies()
    assert missing.value.args == ("douyin_not_configured",)
    assert chrome.cookie_calls == 0

    store.save_connection("douyin", "@测试账号")
    chrome.error = ChromeSessionError("douyin_login_required")
    with pytest.raises(ChromeSessionError):
        session.cookies()
    assert store.connection("douyin")["state"] == "relogin_required"


def test_api_draft_save_does_not_replace_binding(store):
    secrets = Secrets()
    settings = SettingsService(store, keychain_factory=secrets.factory)
    settings.activate_llm('https://models.example.com', 'old', 'old-secret')
    settings.save_llm_key('new-secret')
    assert store.setting('llm_model') == 'old'
    assert settings.llm_secret() == 'old-secret'
    settings.activate_saved_api('https://models.example.com', 'new')
    assert store.setting('llm_model') == 'new'
    assert settings.llm_secret() == 'new-secret'
    assert 'new-secret' not in str(store.settings())


def test_doubao_separate_saves_and_active_snapshot(store, monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, 'tos', SimpleNamespace())
    secrets = Secrets()
    settings = SettingsService(store, keychain_factory=secrets.factory, qwen_probe=lambda: True)
    settings.activate_asr()
    settings.save_doubao_key('private-api')
    with pytest.raises(SettingsError, match='model_credentials_invalid'):
        settings.activate_doubao()
    assert settings.view()['asr']['provider'] == 'Qwen'
    settings.save_doubao_tos('cn-beijing', 'test-bucket', 'private-ak', 'private-sk')
    assert settings.view()['asr']['provider'] == 'Qwen'
    settings.activate_doubao()
    assert settings.view()['asr']['provider'] == '豆包'
    assert settings.doubao_secret('api_key') == 'private-api'
    assert settings.doubao_secret('access_key') == 'private-ak'
    old_tos = store.setting('asr_seed_tos_account')
    settings.save_doubao_tos('cn-shanghai', 'other-bucket', 'other-ak', 'other-sk')
    assert store.setting('asr_seed_region') == 'cn-beijing'
    assert settings.doubao_secret('access_key') == 'private-ak'
    assert old_tos in secrets.values
    assert 'private-' not in str(settings.view()) + str(store.settings())
    settings.activate_asr()
    assert settings.view()['asr']['provider'] == 'Qwen'


def test_doubao_keychain_failure_does_not_change_active_or_draft(store):
    secrets = Secrets()
    settings = service(store, secrets)
    settings.save_doubao_key('first')
    old = store.setting('seed_draft_api_account')
    secrets.fail_save.add('seed_draft_api_account-two')
    with pytest.raises(SettingsError, match='model_credentials_save_failed'):
        settings.save_doubao_key('second')
    assert store.setting('seed_draft_api_account') == old
    assert secrets.values[old] == 'first'


def test_api_activation_uses_openai_protocol(store):
    from knowledge_distiller.v1.llm import OpenAIResponsesClient
    settings=service(store)
    settings.activate_llm('https://api.openai.com/v1','chosen-model','test-secret')
    assert store.setting('llm_provider')=='openai'
    assert settings.view()['llm']['provider']=='OpenAI API'
    assert isinstance(settings.llm_client(),OpenAIResponsesClient)


def test_keychain_labels_follow_saved_and_active_doubao_bindings(store):
    secrets = Secrets()
    settings = SettingsService(store, keychain_factory=secrets.factory, qwen_probe=lambda: True)
    settings.save_doubao_key("first-api")
    settings.save_doubao_tos("cn-guangzhou", "audio-bucket", "ak", "sk")
    first = store.setting("seed_draft_api_account")
    assert "豆包语音 API｜已保存未启用" in secrets.labels[first]
    settings.activate_doubao()
    assert "当前启用" in secrets.labels[first]
    tos = store.setting("asr_seed_tos_account")
    assert "TOS 存储凭据｜当前启用｜audio-bucket" in secrets.labels[tos]
    settings.save_doubao_key("second-api")
    second = store.setting("seed_draft_api_account")
    assert "当前启用" in secrets.labels[first]
    assert "已保存未启用" in secrets.labels[second]
    assert settings.doubao_secret("api_key") == "first-api"
    settings.activate_doubao()
    assert "已替换" in secrets.labels[first]
    assert "当前启用" in secrets.labels[second]
    assert secrets.values[first] == "first-api"
    settings.activate_asr()
    assert "已保存未启用" in secrets.labels[second]


def test_label_failure_does_not_break_credential_save(store):
    secrets = Secrets()
    def factory(account):
        secret = secrets.factory(account)
        def fail(label):
            raise KeychainError("keychain_label_failed")
        secret.set_label = fail
        return secret
    settings = SettingsService(store, keychain_factory=factory)
    settings.save_doubao_key("private")
    assert secrets.values[store.setting("seed_draft_api_account")] == "private"
