from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_distiller.v1.collections import Collections
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate


class Distiller:
    def run(self, _item_id: int):
        raise AssertionError("not used")

    def resolve(self, _item_id: int, _action: str, _value: str):
        raise AssertionError("not used")


class Settings:
    def __init__(self, vault: str | None):
        self.vault = vault

    def view(self):
        return {
            "platforms": [],
            "llm": {
                "state": "unconfigured",
                "provider": "-",
                "provider_id": "openai",
                "codex_models": [],
                "effort": "",
                "model": "-",
                "base_url": "",
            },
            "asr": {
                "state": "unconfigured",
                "provider": "-",
                "provider_id": "qwen",
                "api_key_saved": False,
                "tos_saved": False,
                "region": "",
                "bucket": "",
                "model": "",
            },
            "vault": {
                "state": "configured" if self.vault else "unconfigured",
                "path": self.vault or "-",
                "action": "更换位置" if self.vault else "选择位置",
            },
        }


@dataclass(frozen=True)
class Status:
    state: str
    action: str | None = None
    error_code: str | None = None
    kit_version: str | None = None


class CodeError(RuntimeError):
    def __init__(self, code: str, private_detail: str):
        self.code = code
        super().__init__(private_detail)


class FakeKit:
    def __init__(self, style, status: Status | Exception | None = None):
        self.style = style
        self.current = status or Status("missing", "install", kit_version="3.0")
        self.calls = []

    def status(self, vault):
        self.calls.append(("status", vault))
        if isinstance(self.current, Exception):
            raise self.current
        return self.current

    def install(self, vault):
        self.calls.append(("install", vault))
        if isinstance(self.current, Exception):
            raise self.current
        self.current = Status("ready", kit_version="3.0")
        if self.style.current.state == "asset_unavailable":
            self.style.current = Status("missing", "install")

    def recover(self, vault):
        self.calls.append(("recover", vault))
        if isinstance(self.current, Exception):
            raise self.current
        self.current = Status("missing", "install", kit_version="3.0")


class FakeStyle:
    def __init__(self, status: Status | Exception | None = None):
        self.current = status or Status("asset_unavailable")
        self.calls = []

    def status(self, vault):
        self.calls.append(("status", vault))
        if isinstance(self.current, Exception):
            raise self.current
        return self.current

    def install(self, vault):
        self.calls.append(("install", vault))
        if isinstance(self.current, Exception):
            raise self.current
        self.current = Status("installed", "enable")

    def enable(self, vault):
        self.calls.append(("enable", vault))
        if isinstance(self.current, Exception):
            raise self.current
        self.current = Status("enabled", "disable")

    def disable(self, vault):
        self.calls.append(("disable", vault))
        if isinstance(self.current, Exception):
            raise self.current
        self.current = Status("installed", "enable")

    def recover(self, vault):
        self.calls.append(("recover", vault))
        if isinstance(self.current, Exception):
            raise self.current
        self.current = Status("missing", "install")


def application(tmp_path: Path, *, vault=True, kit_status=None, style_status=None,
                gate=None):
    store = Store(tmp_path / "data" / "knowledge.sqlite3")
    store.initialize()
    selected = tmp_path / "Synthetic Vault" if vault else None
    if selected is not None:
        selected.mkdir()
        (selected / ".obsidian").mkdir()
        store.set_settings({"vault_path": str(selected)})
    style = FakeStyle(style_status)
    kit = FakeKit(style, kit_status)
    app = create_app(
        store,
        Distiller(),
        Settings(str(selected) if selected else None),
        collection_service=Collections(store),
        admission_gate=gate,
        wiki_kit_installer=kit,
        wiki_style_service=style,
    )
    app.config.update(TESTING=True)
    return app.test_client(), store, selected, kit, style


def section(html: str, label_id: str) -> str:
    return html.split(f'id="{label_id}"', 1)[1].split("</section>", 1)[0]


def test_settings_runs_separate_install_enable_disable_journey(tmp_path):
    client, _store, vault, kit, style = application(tmp_path)

    first = client.get("/settings?open=paths").text
    assert "尚未安装" in section(first, "kit-label")
    assert "安装工具" in section(first, "kit-label")
    assert "请先安装知识库工具" in section(first, "wiki-style-label")

    response = client.post("/settings/wiki-kit", data={"action": "install"})
    assert "wiki_kit_installed" in response.location
    installed_kit = client.get(response.location).text
    assert 'settings-message is-error' not in installed_kit
    assert "已安装 · 版本 3.0" in section(installed_kit, "kit-label")
    assert "安装样式" in section(installed_kit, "wiki-style-label")

    response = client.post("/settings/wiki-style", data={"action": "install"})
    installed_style = client.get(response.location).text
    assert "已安装，未启用" in section(installed_style, "wiki-style-label")
    assert "启用样式" in section(installed_style, "wiki-style-label")
    assert [call[0] for call in style.calls if call[0] != "status"] == ["install"]

    response = client.post("/settings/wiki-style", data={"action": "enable"})
    enabled = client.get(response.location).text
    assert "已启用" in section(enabled, "wiki-style-label")
    assert "关闭此样式" in section(enabled, "wiki-style-label")
    assert "知识库页面样式设置已保存。重新打开这个 Obsidian 库后生效。" in enabled

    response = client.post("/settings/wiki-style", data={"action": "disable"})
    disabled = client.get(response.location).text
    assert "已安装，未启用" in section(disabled, "wiki-style-label")
    assert "知识库页面样式的关闭设置已保存。重新打开这个 Obsidian 库后生效" in disabled
    assert "正文和链接保持不变" in disabled
    assert [call[0] for call in style.calls if call[0] != "status"] == [
        "install", "enable", "disable"
    ]
    assert all(call[1] == str(vault) for call in kit.calls + style.calls)


@pytest.mark.parametrize(
    ("kind", "action", "expected_call", "message_code"),
    [
        ("wiki-kit", "repair", "install", "wiki_kit_repaired"),
        ("wiki-kit", "recover", "recover", "wiki_kit_recovered"),
        ("wiki-style", "update", "install", "wiki_style_updated"),
        ("wiki-style", "recover", "recover", "wiki_style_recovered"),
    ],
)
def test_all_phase4_post_actions_dispatch_to_the_frozen_service_api(
    tmp_path, kind, action, expected_call, message_code
):
    kit_status = Status("recovery_required", "recover") if action == "recover" else None
    style_status = Status("recovery_required", "recover") if action == "recover" else Status(
        "update_available", "update"
    )
    client, _store, vault, kit, style = application(
        tmp_path,
        kit_status=kit_status,
        style_status=style_status,
    )

    response = client.post(f"/settings/{kind}", data={"action": action})

    assert response.status_code == 302
    assert message_code in response.location
    service = kit if kind == "wiki-kit" else style
    assert (expected_call, str(vault)) in service.calls


@pytest.mark.parametrize(
    ("kit_status", "style_status", "kit_text", "style_text", "action_text"),
    [
        (Status("update_available", "repair", kit_version="3.0"),
         Status("update_available", "update"), "需要修复", "有新版样式", "更新样式"),
        (Status("conflict", "repair", error_code="kit_drift"),
         Status("conflict", "install", error_code="style_drift"),
         "检测到修改，未覆盖", "检测到现有样式，未覆盖", "已停止"),
        (Status("recovery_required", "recover"),
         Status("recovery_required", "recover"),
         "安装未完成，需要恢复", "安装未完成，需要恢复", "恢复样式"),
    ],
)
def test_settings_maps_update_conflict_and_recovery_without_trusting_action(
    tmp_path, kit_status, style_status, kit_text, style_text, action_text
):
    client, *_ = application(
        tmp_path,
        kit_status=kit_status,
        style_status=style_status,
    )

    page = client.get("/settings?open=paths").text
    kit_html = section(page, "kit-label")
    style_html = section(page, "wiki-style-label")

    assert kit_text in kit_html
    assert style_text in style_html
    assert action_text in page
    if kit_status.state == "conflict":
        assert "修复工具" not in kit_html
        assert "安装样式" not in style_html


def test_get_with_unconfigured_or_missing_vault_never_calls_status_or_creates_path(tmp_path):
    client, _store, _vault, kit, style = application(tmp_path, vault=False)

    page = client.get("/settings?open=paths")

    assert page.status_code == 200
    assert page.text.count("请先选择 Obsidian 库") == 2
    assert kit.calls == []
    assert style.calls == []

    missing = tmp_path / "removed-vault"
    client.application.config["KNOWLEDGE_DISTILLER_STORE"] = _store
    _store.set_settings({"vault_path": str(missing)})
    kit.current = FileNotFoundError("/private/formal/wiki must stay private")
    style.current = FileNotFoundError("/private/formal/raw must stay private")
    page = client.get("/settings?open=paths")
    assert page.status_code == 200
    assert page.text.count("暂时无法读取状态") == 2
    assert "/private/formal" not in page.text
    assert not missing.exists()


@pytest.mark.parametrize(
    ("kind", "code", "message_code", "fixed_text"),
    [
        ("wiki-kit", "operation_busy", "wiki_settings_operation_busy", "任务或组件操作正在进行"),
        ("wiki-kit", "update_reserved", "wiki_settings_update_reserved", "应用正在准备更新"),
        ("wiki-kit", "install_conflict", "wiki_kit_conflict", "检测到工具文件修改"),
        ("wiki-style", "vault_busy", "wiki_settings_vault_busy", "知识库正在被其他整理会话使用"),
        ("wiki-style", "style_drift", "wiki_style_conflict", "检测到现有样式或设置修改"),
    ],
)
def test_action_errors_use_fixed_codes_and_never_echo_exception_or_path(
    tmp_path, kind, code, message_code, fixed_text
):
    private = "/private/formal/wiki/SECRET"
    failed = CodeError(code, private)
    kwargs = {"kit_status": failed} if kind == "wiki-kit" else {"style_status": failed}
    client, *_ = application(tmp_path, **kwargs)

    response = client.post(
        f"/settings/{kind}",
        data={"action": "install"},
    )

    assert response.status_code == 302
    assert message_code in response.location
    assert private not in response.location
    page = client.get(response.location).text
    assert fixed_text in page
    assert private not in page


def test_existing_post_admission_gate_rejects_phase4_actions_during_update(tmp_path):
    gate = WorkAdmissionGate()
    client, *_ = application(tmp_path, gate=gate)
    assert gate.reserve()
    try:
        response = client.post("/settings/wiki-kit", data={"action": "install"})
    finally:
        gate.release_reservation()

    assert response.status_code == 503
    assert "应用正在准备更新" in response.text


def test_invalid_action_and_missing_vault_are_fixed_safe_redirects(tmp_path):
    client, *_rest, kit, style = application(tmp_path, vault=False)

    invalid = client.post("/settings/wiki-style", data={"action": "force"})
    missing = client.post("/settings/wiki-kit", data={"action": "install"})

    assert "wiki_settings_action_invalid" in invalid.location
    assert "wiki_settings_vault_required" in missing.location
    assert not [call for call in kit.calls + style.calls if call[0] != "status"]


def test_source_assembly_uses_wiki_runtime_kit_root_for_phase4_services(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import wiki_kit_runtime
    from knowledge_distiller.v1.app import AppPaths, create_application

    bundled = tmp_path / "bundled-kit"
    bundled.mkdir()
    monkeypatch.setattr(wiki_kit_runtime, "bundled_kit_root", lambda: bundled)

    class Chrome:
        def close(self):
            pass

    paths = AppPaths(tmp_path / "application")
    app = create_application(paths, chrome=Chrome(), start_workers=False)
    try:
        assert app.extensions["wiki_kit_installer"].kit_root == bundled
        assert app.extensions["wiki_kit_installer"].runtime_root.parent == paths.runtime
        assert app.extensions["wiki_style_service"].runtime_root.parent == paths.runtime
    finally:
        assert app.config["KNOWLEDGE_DISTILLER_WORKERS"].stop()
        app.config["KNOWLEDGE_DISTILLER_CLOSE_BROWSERS"]()
