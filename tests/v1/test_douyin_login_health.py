"""BUG-20260922-02: Douyin live login state, error mapping and download path."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import knowledge_distiller.v1.douyin as douyin
from knowledge_distiller.v1.chrome import ChromeSessionError, DouyinConnection
from knowledge_distiller.v1.douyin_collection_browser import BrowserCollectionClient
from knowledge_distiller.v1.douyin_session import DouyinOwnedSession
from knowledge_distiller.v1.settings import AuthorizedDouyinSession, SettingsService
from knowledge_distiller.v1.store import Store

ITEM = '7000000000000000001'
LOGGED_OUT = json.dumps({'status_code': 8, 'status_msg': '用户未登录'})


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'isolated.sqlite3')
    result.initialize()
    return result


@pytest.mark.parametrize('path', ['/aweme/v1/web/user/profile/self/', '/aweme/v1/web/aweme/detail/'])
def test_logged_out_profile_is_login_required_not_upstream_failure(path):
    # Real Douyin answer for the dedicated profile without a session (2026-09-29).
    class Page:
        def call(self, *args, **kwargs):
            return {'result': {'value': {'status': 200, 'body': LOGGED_OUT}}}
    client = BrowserCollectionClient(object())
    client.page = Page()
    with pytest.raises(ChromeSessionError, match='douyin_login_required'):
        asyncio.run(client._fetch(path, {}))


def test_native_detail_read_logout_is_login_required():
    class Page:
        def call(self, *args, **kwargs):
            return {'result': {'value': {'status': 403, 'body': ''}}}
        def evaluate(self, expression): return None
        def wait_for(self, expression, **kwargs):
            return {'status': 200, 'data': {'status_code': 8}, 'key': ITEM}
    client = BrowserCollectionClient(object())
    client.page = Page()
    client._native_start = lambda url: None
    with pytest.raises(ChromeSessionError, match='douyin_login_required'):
        asyncio.run(client.get_video_detail(ITEM))


def video_detail(item=ITEM):
    return {'aweme_id': item, 'aweme_type': 0, 'desc': '原始描述', 'duration': 12000,
            'create_time': 1767225600, 'author': {'nickname': '测试作者', 'sec_uid': 'author-1'},
            'video': {'play_addr': {'uri': 'v0200'}}}


@pytest.fixture
def installed(monkeypatch):
    """Fakes for the installed downloader and the authorized browser reader."""
    state = SimpleNamespace(detail=video_detail(), detail_after=None, aenter_error=None,
                            result=(1, 1, 0, 0), media_names=[ITEM + '.mp4'],
                            api_detail_calls=0, downloaded_detail=None, download_error=None)

    class ApiClient:
        BASE_URL = 'https://www.douyin.com'
        def __init__(self, cookies): self.cookies = cookies
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get_video_detail(self, aweme_id):
            state.api_detail_calls += 1  # The unsigned request Douyin now refuses (HTTP 403).
            return None

    class Browser:
        def __init__(self, session): pass
        async def __aenter__(self):
            if state.aenter_error: raise state.aenter_error
            return self
        async def __aexit__(self, *args): pass
        async def resolve_short_url(self, url): return 'https://www.douyin.com/video/' + ITEM
        async def get_video_detail(self, key):
            if state.detail_after is not None and state.downloaded_detail is not None:
                return state.detail_after
            return state.detail

    class Downloader:
        def __init__(self, config, api_client, file_manager, cookie_manager, database=None):
            self.api_client, self.path = api_client, Path(file_manager.path)
        async def download(self, parsed):
            if state.download_error: raise state.download_error
            state.downloaded_detail = await self.api_client.get_video_detail(parsed['aweme_id'])
            for index, name in enumerate(state.media_names):
                target = self.path / f'dir-{index}' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'complete media')
            total, success, failed, skipped = state.result
            return SimpleNamespace(total=total, success=success, failed=failed, skipped=skipped)

    import core.api_client, core.video_downloader, storage, knowledge_distiller.v1.douyin_collection_browser as browser_module
    monkeypatch.setattr(core.api_client, 'DouyinAPIClient', ApiClient)
    monkeypatch.setattr(core.video_downloader, 'VideoDownloader', Downloader)
    monkeypatch.setattr(storage, 'FileManager', lambda path: SimpleNamespace(path=path))
    monkeypatch.setattr(browser_module, 'BrowserCollectionClient', Browser)
    monkeypatch.setattr(douyin, '_download_config', lambda work_dir: object())
    return state


class Verifier:
    def verify(self, path, **kwargs): return 12.0


def connected_source(store, tmp_path):
    identifier = 'a' * 32
    store.save_connection('douyin', '@测试', browser_context='owned:' + identifier)
    chrome = SimpleNamespace(cookies=lambda: {'sessionid': 'fixture'}, store=store)
    return douyin.DouyinSource(AuthorizedDouyinSession(store, chrome), verifier=Verifier())


def test_downloader_receives_the_browser_verified_detail(installed, store, tmp_path):
    captured = connected_source(store, tmp_path).capture('https://v.douyin.com/example/', tmp_path / 'work')
    assert captured.source_key == ITEM and captured.duration_seconds == 12.0
    assert installed.api_detail_calls == 0
    assert installed.downloaded_detail == video_detail()
    assert installed.downloaded_detail is not installed.detail  # A copy; the verified detail is untouched.


def test_logged_out_dedicated_browser_requires_relogin(installed, store, tmp_path):
    installed.aenter_error = ChromeSessionError('douyin_login_required')
    with pytest.raises(douyin.DouyinSourceError) as failure:
        connected_source(store, tmp_path).capture('https://v.douyin.com/example/', tmp_path / 'work')
    assert failure.value.args == ('douyin_login_required',)
    assert failure.value.diagnostic() == {'subcode': 'session_logged_out'}
    assert store.connection('douyin')['state'] == 'relogin_required'


@pytest.mark.parametrize('change,code,subcode', [
    (lambda s: setattr(s, 'detail', None), 'douyin_source_unavailable', 'detail_missing'),
    (lambda s: setattr(s, 'detail', video_detail('1')), 'douyin_source_unavailable', 'detail_identity_mismatch'),
    (lambda s: setattr(s, 'result', (1, 0, 1, 0)), 'douyin_source_unavailable', 'download_failed'),
    (lambda s: setattr(s, 'media_names', []), 'douyin_source_unavailable', 'media_missing'),
    (lambda s: setattr(s, 'media_names', [ITEM + '.mp4', ITEM + '.mp4']), 'douyin_source_unavailable', 'media_ambiguous'),
    (lambda s: setattr(s, 'download_error', TimeoutError()), 'douyin_upstream_failed', 'upstream_exception'),
])
def test_source_failures_keep_distinct_redacted_subcodes(installed, store, tmp_path, change, code, subcode):
    change(installed)
    with pytest.raises(douyin.DouyinSourceError) as failure:
        connected_source(store, tmp_path).capture('https://v.douyin.com/example/', tmp_path / 'work')
    assert failure.value.args == (code,)
    assert failure.value.diagnostic()['subcode'] == subcode
    assert 'fixture' not in json.dumps(failure.value.diagnostic())
    assert store.connection('douyin')['state'] == 'connected'  # Not a login problem.


def test_pipeline_records_redacted_source_diagnostic(installed, store, tmp_path):
    from knowledge_distiller.v1.pipeline import Distiller
    installed.result = (1, 0, 1, 0)
    service = Distiller(store=store, source=connected_source(store, tmp_path), normalizer=None,
                        recognizer=None, reviewer=None, confirmation_clipper=None, knowledge_model=None,
                        runtime_root=tmp_path / 'runtime', vault=tmp_path)
    item = store.create_item('https://v.douyin.com/example/')
    assert service.run(item).state == 'failed'
    record = json.loads((tmp_path / 'runtime/items' / str(item) / 'source-diagnostic.json').read_text())
    assert record == {'code': 'douyin_source_unavailable', 'subcode': 'download_failed'}


class Secrets:
    def __init__(self): self.values, self.writes = {}, []
    def __call__(self, account):
        owner = self
        class Secret:
            def save(self, value): owner.writes.append(('save', account)); owner.values[account] = value
            def load(self): return owner.values[account]
            def clear(self): owner.writes.append(('clear', account)); owner.values.pop(account, None)
            def set_label(self, label): pass
        return Secret()


@pytest.mark.parametrize('answer,expected', [('connected', 'connected'), ('logged_out', 'logged_out'),
                                             (None, 'unknown'), ('anything', 'unknown')])
def test_live_status_reads_without_touching_credentials(store, tmp_path, monkeypatch, answer, expected):
    identifier = 'b' * 32
    store.save_connection('douyin', '@测试', browser_context='owned:' + identifier)
    secrets = Secrets()
    secrets.values['douyin-session-' + identifier] = '{"sessionid":"fixture"}'
    session = DouyinOwnedSession(store, tmp_path / 'profiles', secret_factory=secrets)
    class Page:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait_for(self, expression, **kwargs): return True
        def call(self, method, params=None, **kwargs):
            assert 'profile/self' in params['expression'] and params['awaitPromise']
            return {'result': {'value': answer}}
    monkeypatch.setattr(session, '_launch', lambda identity, **kwargs: tmp_path / 'port')
    monkeypatch.setattr(session, '_page', lambda url, port: Page())
    assert session.live_status() == expected
    assert secrets.writes == [] and store.connection('douyin')['state'] == 'connected'


def test_graceful_quit_asks_chrome_to_close_before_any_signal(store, tmp_path, monkeypatch):
    # Signalling Chrome right after login lost the new auth cookies from the
    # profile; Browser.close lets Chrome flush its cookie store first.
    session = DouyinOwnedSession(store, tmp_path / 'profiles', secret_factory=Secrets())
    profile = tmp_path / 'profiles' / ('c' * 32)
    profile.mkdir(parents=True)
    (profile / 'DevToolsActivePort').write_text('9222\n/devtools/browser/test\n')
    sent = []
    class Socket:
        def send(self, message): sent.append(json.loads(message))
        def close(self): pass
    import websocket
    monkeypatch.setattr(websocket, 'create_connection', lambda endpoint, **kwargs: sent.append(endpoint) or Socket())
    class Process:
        exited = False
        def poll(self): return 0 if self.exited else None
        def wait(self, timeout=None): self.exited = True; return 0
        def terminate(self): pytest.fail('graceful exit must not be replaced by a signal')
    session._process, session._profile = Process(), profile
    session.close()
    assert sent == ['ws://127.0.0.1:9222/devtools/browser/test', {'id': 1, 'method': 'Browser.close'}]


def test_graceful_quit_failure_falls_back_to_terminate(store, tmp_path):
    session = DouyinOwnedSession(store, tmp_path / 'profiles', secret_factory=Secrets())
    calls = []
    class Process:
        def poll(self): return None
        def wait(self, timeout=None): calls.append('wait'); return 0
        def terminate(self): calls.append('terminate')
    session._process, session._profile = Process(), tmp_path / 'missing-profile'
    session.close()
    assert calls == ['terminate', 'wait']


class LiveChrome:
    def __init__(self, answer): self.answer, self.calls = answer, 0
    def live_status(self):
        self.calls += 1
        if isinstance(self.answer, Exception): raise self.answer
        return self.answer
    def browser_page(self, url): pass
    def verify(self): return DouyinConnection('@测试', 'owned:' + 'd' * 32)


@pytest.mark.parametrize('answer,state,stored', [
    ('connected', 'connected', 'connected'),
    ('logged_out', 'relogin_required', 'relogin_required'),
    ('unknown', 'configured', 'connected'),
    (ChromeSessionError('douyin_browser_unavailable'), 'configured', 'connected'),
    (ChromeSessionError('douyin_login_required'), 'relogin_required', 'relogin_required'),
])
def test_settings_health_reports_only_what_the_live_check_concluded(store, answer, state, stored):
    chrome = LiveChrome(answer)
    settings = SettingsService(store, chrome=chrome, qwen_probe=lambda: True)
    store.save_connection('douyin', '@测试', browser_context='owned:' + 'd' * 32)
    douyin_row = next(p for p in settings.view()['platforms'] if p['key'] == 'douyin')
    assert douyin_row['state'] == 'checking'  # Stored material alone is not "已登录".
    assert settings.platform_health('douyin') == {'state': state}
    assert store.connection('douyin')['state'] == stored
    assert settings.platform_health('douyin') == {'state': state} and chrome.calls == 1  # Cached briefly.


def test_reconnect_during_check_is_not_marked_expired(store):
    settings = SettingsService(store, qwen_probe=lambda: True, chrome=None)
    store.save_connection('douyin', '@测试', browser_context='owned:' + 'e' * 32)
    class Racing(LiveChrome):
        def live_status(self):
            store.save_connection('douyin', '@测试', browser_context='owned:' + 'f' * 32)
            return 'logged_out'
    settings.chrome = Racing('logged_out')
    assert settings.platform_health('douyin') == {'state': 'relogin_required'}
    assert store.connection('douyin')['state'] == 'connected'  # The new login is untouched.


def test_settings_page_checks_douyin_live_and_login_failure_points_to_settings(store, tmp_path):
    from knowledge_distiller.v1.web import create_app
    chrome = LiveChrome('logged_out')
    settings = SettingsService(store, chrome=chrome, qwen_probe=lambda: True)
    store.save_connection('douyin', '@测试', browser_context='owned:' + 'd' * 32)
    client = create_app(store, None, settings).test_client()
    page = client.get('/settings').get_data(as_text=True)
    assert 'data-platform-health="/settings/platforms/douyin/health"' in page and '正在校验' in page
    assert client.get('/settings/platforms/douyin/health').get_json() == {'state': 'relogin_required'}
    assert client.get('/settings/platforms/x/health').status_code == 404
    assert '需重新登录' in client.get('/settings').get_data(as_text=True)
    item = store.create_item('https://v.douyin.com/example/')
    store.mark_failed(item, 'collecting', 'douyin_login_required')
    home = client.get('/').get_data(as_text=True)
    assert '需要重新登录抖音后再试。' in home and '前往设置' in home


@pytest.mark.parametrize('answer,text,dot,relogin_visible', [
    ('logged_out', '需重新登录', 'relogin_required', True),
    ('connected', '已登录', 'connected', False),
    ('unknown', '已配置', 'configured', False),
])
def test_browser_settings_row_switches_to_the_live_answer(store, answer, text, dot, relogin_visible):
    import threading
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    from knowledge_distiller.v1.web import create_app
    settings = SettingsService(store, chrome=LiveChrome(answer), qwen_probe=lambda: True)
    store.save_connection('douyin', '@测试', browser_context='owned:' + 'd' * 32)
    server = make_server('127.0.0.1', 0, create_app(store, None, settings), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}/settings?open=social')
            row = page.locator('.media-row').filter(has_text='抖音')
            expect(row.locator('.status-text')).to_have_text(text)
            assert row.locator('.state-dot').get_attribute('class') == 'state-dot ' + dot
            expect(row.get_by_role('button', name='重新登录')).to_be_visible(visible=relogin_visible)
            expect(row.get_by_role('button', name='替换配置')).to_be_visible(visible=not relogin_visible)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
