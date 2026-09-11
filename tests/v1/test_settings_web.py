from pathlib import Path

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app


class Distiller:
    def run(self, item_id: int):
        raise AssertionError("not used")

    def resolve(self, item_id: int, action: str, value: str):
        raise AssertionError("not used")


class Settings:
    def __init__(self):
        self.actions = []

    def view(self):
        return {
            "platforms": [
                {
                    "key": "douyin",
                    "label": "抖音",
                    "state": "connected",
                    "account_label": "@测试账号",
                    "available": True,
                },
                *[
                    {
                        "key": key,
                        "label": label,
                        "state": "unconfigured",
                        "account_label": None,
                        "available": False,
                    }
                    for key, label in (
                        ("xiaohongshu", "小红书"),
                        ("weibo", "微博"),
                        ("zhihu", "知乎"),
                        ("youtube", "YouTube"),
                        ("x", "X"),
                    )
                ],
            ],
            "llm": {
                "state": "configured",
                "provider": "OpenAI API",
                "provider_id": "openai",
                "codex_models": [],
                "effort": "",
                "model": "model-1",
                "base_url": "https://models.example.com",
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
                "state": "configured",
                "path": "/Users/test/My Vault",
                "action": "更换位置",
            },
        }

    def connect_douyin(self):
        self.actions.append(("connect",))

    def clear_douyin(self):
        self.actions.append(("clear",))

    def activate_llm(self, base_url: str, model: str, secret: str):
        self.actions.append(("llm", base_url, model, secret))

    def activate_asr(self):
        self.actions.append(("asr",))

    def choose_vault(self):
        self.actions.append(("vault",))
        return True


def application(tmp_path: Path):
    store = Store(tmp_path / "knowledge.sqlite3")
    settings = Settings()
    from knowledge_distiller.v1.collections import Collections
    app = create_app(store, Distiller(), settings, collection_service=Collections(store))
    app.config.update(TESTING=True)
    return app.test_client(), settings


def test_settings_projects_three_groups_and_current_values(tmp_path: Path) -> None:
    client, _ = application(tmp_path)

    response = client.get("/settings?open=social")

    assert response.status_code == 200
    assert "社媒连接" in response.text
    assert "模型配置" in response.text
    assert "路径管理" in response.text
    assert "@测试账号" in response.text
    assert "model-1" in response.text
    assert "/Users/test/My Vault" in response.text
    assert "API Key" in response.text
    assert "private" not in response.text
    assert response.text.count(">配置登录</button>") == 5


def test_llm_secret_exists_only_in_post_body_and_is_not_echoed(
    tmp_path: Path,
) -> None:
    client, settings = application(tmp_path)

    response = client.post(
        "/settings/llm",
        data={
            "base_url": "https://models.example.com",
            "model": "model-2",
            "api_key": "very-private",
        },
    )

    assert response.status_code == 302
    assert "very-private" not in response.headers["Location"]
    assert settings.actions == [
        ("llm", "https://models.example.com", "model-2", "very-private")
    ]
    page = client.get(response.headers["Location"])
    assert "very-private" not in page.text
    assert "LLM 已保存并启用" in page.text


def test_explicit_settings_actions_return_to_their_group(tmp_path: Path) -> None:
    client, settings = application(tmp_path)

    responses = [
        client.post("/settings/douyin/connect"),
        client.post("/settings/douyin/clear"),
        client.post("/settings/asr"),
        client.post("/settings/vault"),
    ]

    assert settings.actions == [
        ("connect",),
        ("clear",),
        ("asr",),
        ("vault",),
    ]
    assert all(response.status_code == 302 for response in responses)
    assert "open=social" in responses[0].headers["Location"]
    assert "open=models" in responses[2].headers["Location"]
    assert "open=paths" in responses[3].headers["Location"]


def test_unknown_message_is_never_reflected(tmp_path: Path) -> None:
    client, _ = application(tmp_path)

    response = client.get("/settings?message=%3Cscript%3Ebad%3C/script%3E")

    assert "<script>bad</script>" not in response.text
    assert "连接来源、模型与知识保存位置" in response.text


def test_browser_provider_switch_recomputes_effort_and_activation(tmp_path):
    import threading
    import pytest
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    client, settings = application(tmp_path)
    view = settings.view()
    view['llm'].update(provider='Codex',provider_id='codex',effort='high',api_key_saved=False,
        codex_models=[{'model':'model-1','label':'Model One','efforts':['low','high'],'fast_supported':True},
                      {'model':'model-2','label':'Model Two','efforts':['low']}])
    settings.view = lambda: view
    server = make_server('127.0.0.1',0,client.application)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser=playwright.chromium.launch()
            try:
                page=browser.new_page()
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/settings?open=models')
                page.locator('details.model-setting summary').click()
                expect(page.locator('#llm-model-picker option')).to_have_count(3)
                page.locator('#llm-model-picker').select_option('model-1')
                page.locator('#llm-speed').select_option('fast')
                assert page.locator('#llm-provider').bounding_box()['y'] == page.locator('#llm-model-picker').bounding_box()['y']
                assert page.locator('#llm-effort').bounding_box()['y'] == page.locator('#llm-speed').bounding_box()['y']
                expect(page.locator('#llm-effort')).to_have_value('high')
                page.locator('#llm-provider').select_option('openai')
                expect(page.locator('#llm-speed-row')).to_be_hidden()
                expect(page.locator('#llm-effort-row')).to_be_hidden()
                expect(page.locator('#llm-save')).to_be_disabled()
                expect(page.locator('#llm-key-form')).to_be_visible()
                assert page.locator('#llm-key-form').evaluate("el => el.closest('.setting-group').querySelector('.group-heading').textContent.includes('模型配置')")
                page.locator('#llm-provider').select_option('codex')
                page.locator('#llm-model-picker').select_option('model-2')
                expect(page.locator('#llm-model')).to_have_value('model-2')
                expect(page.locator('#llm-speed')).to_have_value('')
                assert page.locator('#llm-speed option[value="fast"]').evaluate('(option) => option.disabled')
                expect(page.locator('#llm-effort option')).to_have_count(2)
                page.locator('#llm-model-picker').select_option('__custom__')
                expect(page.locator('#llm-custom-row')).to_be_visible()
                expect(page.locator('#llm-save')).to_be_disabled()
                page.locator('#llm-model').fill('custom-id')
                expect(page.locator('#llm-effort-row')).to_be_hidden()
                expect(page.locator('#llm-save')).to_be_enabled()
                assert errors==[]
            finally:browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()


def test_asr_unavailable_shows_cloud_failure_without_requiring_local_model(tmp_path):
    client, settings = application(tmp_path)
    from knowledge_distiller.v1.settings import SettingsService
    store = Store(tmp_path / 'diagnostic.sqlite3')
    store.initialize()
    actual = SettingsService(store)
    settings.view = actual.view
    store.set_settings({'asr_model':'volc.seedasr.auc'})
    actual.mark_asr_unavailable('doubao_tos_access_denied')
    page = client.get('/settings?open=models&asr=doubao').get_data(as_text=True)
    assert 'TOS 拒绝访问' in page
    assert '请先在设置中启用本地语音识别' not in page
    actual.mark_asr_unavailable('SECRET must not display')
    assert 'SECRET' not in client.get('/settings').get_data(as_text=True)


def test_feishu_configuration_in_paths_and_secret_never_echoed(tmp_path):
    from unittest.mock import Mock
    client, settings = application(tmp_path)
    connection=Mock()
    connection.status.return_value={'state':'connected','binding':{'app_id':'cli_test'},'error':None,'delivery_errors':{}}
    client.application.extensions['feishu']=connection
    page=client.get('/settings?open=paths')
    assert page.text.index('路径管理') < page.text.index('飞书机器人')
    response=client.post('/settings/feishu',data={'app_id':'cli_test','app_secret':'private-feishu-secret'})
    connection.configure.assert_called_once_with('cli_test','private-feishu-secret')
    assert 'open=paths' in response.location
    assert 'private-feishu-secret' not in response.location
    assert 'private-feishu-secret' not in client.get(response.location).text
    connection.configure.side_effect=RuntimeError('private-feishu-secret')
    response=client.post('/settings/feishu',data={'app_id':'cli_test','app_secret':'private-feishu-secret'})
    assert 'feishu_configuration_failed' in response.location
    assert 'private-feishu-secret' not in client.get(response.location).text


def test_new_user_guide_and_retry_preserve_id_not_secret(tmp_path):
    from unittest.mock import Mock
    client,_=application(tmp_path)
    connection=Mock()
    connection.status.return_value={'state':'unbound','binding':{},'pairing':{},'error':None,'delivery_errors':{}}
    connection.configure.side_effect=RuntimeError('private-secret')
    client.application.extensions['feishu']=connection
    page=client.get('/settings?open=paths&setup=feishu').text
    for text in ('创建应用，启用机器人','填入应用凭据','配置权限、事件并发布','绑定自己的私聊','im.message.receive_v1','card.action.trigger'):
        assert text in page
    response=client.post('/settings/feishu',data={'app_id':'cli_new','app_secret':'private-secret'})
    assert 'feishu_app_id=cli_new' in response.location
    page=client.get(response.location).text
    assert 'value="cli_new"' in page
    assert 'private-secret' not in page+response.location


def test_doubao_onboarding_saves_in_stages_without_exposing_secrets_or_switching_early(tmp_path):
    from .test_settings import Secrets
    from knowledge_distiller.v1.settings import SettingsService
    store=Store(tmp_path/'onboarding.sqlite3');store.initialize()
    secrets=Secrets()
    settings=SettingsService(store,keychain_factory=secrets.factory,qwen_probe=lambda:True)
    app=create_app(store,Distiller(),settings)
    app.config.update(TESTING=True)
    client=app.test_client()
    settings.activate_asr()
    original=store.setting('asr_model')
    page=client.get('/settings?open=models&asr=doubao').text
    assert 'data-storage-key="doubao"' in page and 'data-max-step="2"' in page
    assert '启用只检查本机配置' in page
    response=client.post('/settings/asr/key',data={'api_key':'test-private-api'})
    assert 'asr_step=3' in response.location
    assert store.setting('asr_model')==original
    assert 'data-max-step="3"' in client.get(response.location).text
    response=client.post('/settings/asr/tos',data={'region':'cn-beijing','bucket':'test-bucket','access_key':'test-private-ak','secret_key':''})
    assert 'asr_step=3' in response.location
    assert 'asr_bucket=test-bucket' in response.location
    assert store.setting('asr_model')==original
    response=client.post('/settings/asr/tos',data={'region':'cn-beijing','bucket':'test-bucket','access_key':'test-private-ak','secret_key':'test-private-sk'})
    assert 'asr_step=4' in response.location
    page=client.get(response.location).text
    assert 'data-max-step="4"' in page
    assert store.setting('asr_model')==original
    for secret in ('test-private-api','test-private-ak','test-private-sk'):
        assert secret not in page+response.location+repr(store.settings())
    response=client.post('/settings/asr',data={'provider':'doubao'})
    assert 'asr=doubao' in response.location and 'asr_step=4' in response.location
    assert store.setting('asr_model')=='volc.seedasr.auc'
    assert '豆包语音已启用' in client.get(response.location).text


def test_reading_style_install_feedback_and_external_disable(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import reading_style
    import json
    client, settings = application(tmp_path)
    vault = tmp_path / 'vault'; vault.mkdir()
    settings.store = Store(tmp_path / 'knowledge.sqlite3')
    settings.store.set_settings({'vault_path': str(vault)})
    original_view = settings.view
    settings.view = lambda: {**original_view(), 'vault': {'path': str(vault), 'state': 'configured'}}
    monkeypatch.setattr(reading_style, 'content', lambda: b'.kd-reading{}')
    response = client.post('/settings/reading-style', follow_redirects=True)
    panel = response.text.split('<div class="reading-style-configuration"', 1)[1].split('</div>', 1)[0]
    assert 'role="status"' in panel and '>已启用</span>' in panel
    assert '<p ' not in panel
    assert '>已启用</span>' in panel
    assert '>关闭此排版</button>' in panel
    appearance = vault / '.obsidian/appearance.json'
    appearance.write_text(json.dumps({'enabledCssSnippets': []}))
    response = client.get('/settings?open=paths')
    assert '已关闭' in response.text
    assert '>启用此排版</button>' in response.text
    (vault / '.obsidian/snippets/kd-reading.css').write_text('user edited')
    response = client.post('/settings/reading-style', follow_redirects=True)
    panel = response.text.split('<div class="reading-style-configuration"', 1)[1].split('</div>', 1)[0]
    assert 'role="status"' in panel and '现有文件或设置已保留' in panel
    assert (vault / '.obsidian/snippets/kd-reading.css').read_text() == 'user edited'


def test_key_folder_is_app_owned_and_vault_details_start_closed(tmp_path,monkeypatch):
    from knowledge_distiller.v1.settings import SettingsService
    store=Store(tmp_path/'keys.sqlite3');store.initialize()
    service=SettingsService(store,qwen_probe=lambda:True)
    client=create_app(store,Distiller(),service).test_client()
    opened=[]
    monkeypatch.setattr('knowledge_distiller.v1.vault_access.open_saved_location',lambda path:opened.append(path))
    response=client.post('/settings/credentials/open',data={'path':'/untrusted'})
    assert response.status_code==302
    assert opened==[str(tmp_path/'credentials')]
    assert (tmp_path/'credentials').is_dir()
    page=client.get('/settings?open=paths').text
    assert '<details class="vault-configuration"' not in page
    assert '重新查状态' not in page
    assert '<details class="reading-style-configuration"' not in page
    assert page.index('飞书机器人') < page.index('密钥管理')


def test_reading_style_disable_preserves_theme_other_snippets_and_css(tmp_path,monkeypatch):
    import json
    from knowledge_distiller.v1 import reading_style
    vault=tmp_path/'vault';vault.mkdir()
    monkeypatch.setattr(reading_style,'content',lambda:b'.kd-reading{}')
    snippet=reading_style.install(vault)
    appearance=vault/'.obsidian/appearance.json'
    appearance.write_text(json.dumps({'theme':'moonstone','enabledCssSnippets':['other','kd-reading']}))
    before=snippet.read_bytes()
    reading_style.disable(vault)
    assert json.loads(appearance.read_text())=={'theme':'moonstone','enabledCssSnippets':['other']}
    assert snippet.read_bytes()==before
    reading_style.disable(vault)
    reading_style.install(vault)
    assert reading_style.state(vault)=='enabled'


def test_key_folder_indicator_reflects_directory_availability(tmp_path):
    from knowledge_distiller.v1.settings import SettingsService
    store=Store(tmp_path/'keys.sqlite3');store.initialize()
    service=SettingsService(store,qwen_probe=lambda:True)
    client=create_app(store,Distiller(),service).test_client()
    page=client.get('/settings?open=paths').text.split('id="credentials"',1)[1]
    assert 'state-dot configured' in page and '本地目录可用' in page
    (tmp_path/'credentials').chmod(0o755)
    page=client.get('/settings?open=paths').text.split('id="credentials"',1)[1]
    assert 'state-dot problem' in page and '本地目录不可用' in page
