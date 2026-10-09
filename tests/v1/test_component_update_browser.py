"""Real update UI over a component release and disposable local data."""
import threading
from types import SimpleNamespace

import httpx
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from tests.v1.test_component_updates import make


def test_component_release_download_action_polling_and_empty_response(tmp_path, monkeypatch):
    updates = make(tmp_path)
    updates.info['windows_update'] = False
    updates.platform = 'macos-arm64'
    plan = SimpleNamespace(source='base', assets=(), download_bytes=7969178)
    monkeypatch.setattr(updates.assembler, 'prepare', lambda *args, **kwargs: (
        {'version': '2026.09.30.4', 'product_version': '2026.09.30.4',
         'notes': '合成组件发行说明'}, plan))
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'synthetic manifest'))) as transport:
        monkeypatch.setattr('knowledge_distiller.v1.component_updates.httpx.stream',
                            lambda *args, **kwargs: transport.stream('GET', 'https://example.test'))
        updates._run('check')
    assert set(updates.release) == {'version', 'display_version', 'notes', 'selected', 'full_reason'}
    assert updates.phase == 'available' and not updates.full_update_required()
    monkeypatch.setattr('knowledge_distiller.v1.update_web.bundle_info', lambda: updates.info)
    monkeypatch.setattr('knowledge_distiller.v1.component_updates.ComponentUpdates',
                        lambda *args, **kwargs: updates)
    app = create_app(Store(tmp_path / 'synthetic.sqlite3'), lambda: None)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    app.config['LOCAL_ADDRESS_PORT'] = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 1280, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.continue_()
                       if route.request.url.startswith(base + '/') else route.abort())
            # Load the actual source template and static updates.js, without
            # replacing JavaScript or triggering an updater download/install.
            page.goto(base + '/settings?open=updates')
            primary = page.locator('[data-update-primary]')
            expect(primary).to_have_text('下载更新')
            expect(primary).to_have_attribute('data-action', 'download')
            expect(page.locator('[data-update-package-size]')).to_have_text('更新包大小 7.6MB')
            expect(page.locator('[data-update-full-fallback]')).to_be_hidden()
            with page.expect_response(base + '/settings/updates/status', timeout=6000) as polled:
                pass
            assert polled.value.status == 200
            assert polled.value.json()['release'] == updates.release
            assert not errors

            requests = []
            def empty_response(route):
                requests.append((route.request.method, route.request.url))
                route.fulfill(status=404, body='', content_type='text/plain')
            page.route('**/settings/updates/download', empty_response)
            primary.click()
            expect(page.locator('[data-update-status]')).to_have_text('操作未完成，请重试。')
            assert requests == [('POST', base + '/settings/updates/download')]
            assert not errors
            expect(primary).to_have_text('下载更新')
            expect(primary).to_have_attribute('data-action', 'download')
            assert updates.phase == 'available'
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
