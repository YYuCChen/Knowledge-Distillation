import pytest

import knowledge_distiller.v1.keychain as keychain_module
from knowledge_distiller.v1.keychain import (
    ERR_SEC_ITEM_NOT_FOUND,
    KeychainError,
    KeychainSecret,
)


class Backend:
    def __init__(
        self,
        *,
        save_status: int = 0,
        load_status: int = 0,
        value: str | None = "private-value",
        clear_status: int = 0,
    ) -> None:
        self.save_status = save_status
        self.load_status = load_status
        self.value = value
        self.clear_status = clear_status
        self.saved: tuple[str, str, str] | None = None

    def save(self, service: str, account: str, value: str) -> int:
        self.saved = (service, account, value)
        return self.save_status

    def load(self, service: str, account: str) -> tuple[int, str | None]:
        return self.load_status, self.value

    def clear(self, service: str, account: str) -> int:
        return self.clear_status


def install_backend(monkeypatch: pytest.MonkeyPatch, backend: Backend) -> None:
    monkeypatch.setattr(keychain_module, "system_keychain", lambda: backend)


def test_secret_save_sends_value_to_keychain_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Backend()
    install_backend(monkeypatch, backend)
    secret = KeychainSecret("llm-api-key-1")

    secret.save("very-private")

    assert backend.saved == (
        "com.knowledge-distiller.credentials",
        "llm-api-key-1",
        "very-private",
    )


@pytest.mark.parametrize("value", ["", "one\ntwo", "one\rtwo"])
def test_secret_rejects_empty_or_multiline_values(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_backend(monkeypatch, Backend())
    with pytest.raises(ValueError):
        KeychainSecret("slot").save(value)


def test_secret_load_returns_backend_value(monkeypatch: pytest.MonkeyPatch) -> None:
    install_backend(monkeypatch, Backend())
    assert KeychainSecret("slot").load() == "private-value"


def test_keychain_failures_expose_only_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_backend(monkeypatch, Backend(save_status=-1))
    with pytest.raises(KeychainError) as save_error:
        KeychainSecret("slot").save("value")

    install_backend(monkeypatch, Backend(load_status=-1, value=None))
    with pytest.raises(KeychainError) as load_error:
        KeychainSecret("slot").load()

    install_backend(monkeypatch, Backend(clear_status=-1))
    with pytest.raises(KeychainError) as clear_error:
        KeychainSecret("slot").clear()

    assert save_error.value.args == ("keychain_save_failed",)
    assert load_error.value.args == ("keychain_secret_unavailable",)
    assert clear_error.value.args == ("keychain_clear_failed",)


def test_absent_keychain_item_is_already_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_backend(monkeypatch, Backend(clear_status=ERR_SEC_ITEM_NOT_FOUND))
    KeychainSecret("slot").clear()
