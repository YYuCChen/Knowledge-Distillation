"""BUG-20260915-02: the document-model notice follows the polled readiness."""
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app

CHECKING = {'state': 'checking', 'message': '正在检查本地文档模型，完成前文档任务会等待。'}
UNAVAILABLE = {'state': 'unavailable', 'message':
               '本地文档模型缺失或校验失败。请使用知识蒸馏器安装器，选择当前程序和数据目录重新检查并修复，然后重试文档任务。'}
READY = {'state': 'ready', 'message': ''}
SHARED_PAGES = ('/', '/settings', '/topics', '/insights')


class Documents:
    """The same readiness surface DoclingSourceConverter exposes to the app."""
    def __init__(self, readiness):
        self.value = dict(readiness)

    @property
    def readiness(self):
        return dict(self.value)


def application(tmp_path, readiness, *, manual=False):
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    documents = Documents(readiness)
    app = create_app(store, object())
    # Exercise the actual manual_download_url branch without a release server,
    # model service, desktop lifecycle, credentials or an existing data root.
    updates = app.extensions['updates']
    updates.info['download_url'] = 'https://example.invalid/manual' if manual else ''
    updates.info['feed_url'] = ''
    updates.info['public_key'] = ''
    app.context_processor(lambda: {'document_component': documents.readiness})
    app.extensions['document_component'] = documents
    return app, documents


def notice(html):
    return html.split('data-document-component=', 1)[1].split('</p>', 1)[0]


@pytest.mark.parametrize('manual', [False, True], ids=['ordinary', 'manual'])
@pytest.mark.parametrize('readiness,shown', [(CHECKING, False), (UNAVAILABLE, True),
                                           (READY, False)])
def test_shared_ssr_notice_shows_only_unavailable(tmp_path, readiness, shown, manual):
    app, _ = application(tmp_path, readiness, manual=manual)
    for path in SHARED_PAGES:
        response = app.test_client().get(path)
        assert response.status_code == 200, path
        html = response.get_data(as_text=True)
        assert html.count('data-document-component=') == 1, path
        assert 'id="update-state"' in html and 'updates.js' in html, path
        assert notice(html).startswith(f'"{readiness["state"]}"'), path
        assert ('hidden' in notice(html).split('>', 1)[0]) is not shown, path
        if shown:
            assert readiness['message'] in notice(html)


@pytest.mark.parametrize('manual', [False, True], ids=['ordinary', 'manual'])
def test_status_poll_reports_current_readiness(tmp_path, manual):
    app, documents = application(tmp_path, CHECKING, manual=manual)
    client = app.test_client()
    for readiness in (CHECKING, UNAVAILABLE, CHECKING, READY):
        documents.value = dict(readiness)
        response = client.get('/settings/updates/status')
        assert response.status_code == 200
        status = response.get_json()
        assert status['document_component'] == readiness
        assert bool(status['manual_download_url']) is manual


@pytest.mark.parametrize('manual', [False, True], ids=['ordinary', 'manual'])
@pytest.mark.parametrize('path', SHARED_PAGES)
def test_real_poll_hides_checking_ready_and_preserves_failure(tmp_path, path, manual):
    from playwright.sync_api import expect, sync_playwright
    from werkzeug.serving import make_server
    app, documents = application(tmp_path, CHECKING, manual=manual)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.route('**/*', lambda route: route.continue_()
                       if urlparse(route.request.url).hostname == '127.0.0.1'
                       else route.abort())
            page.goto(f'http://127.0.0.1:{server.server_port}{path}')
            node = page.locator('[data-document-component]')
            expect(node).to_be_hidden()
            draft = page.locator('textarea[name="content"]')
            if path == '/':
                draft.fill('尚未提交的草稿')

            def observe_poll(readiness, shown):
                # Wait for a real Flask status response and the real updates.js
                # DOM update; never invoke syncDocumentComponent from the test.
                response = page.wait_for_event('response', predicate=lambda response:
                    response.url.endswith('/settings/updates/status')
                    and response.status == 200
                    and response.json()['document_component'] == readiness,
                    timeout=8000)
                assert bool(response.json()['manual_download_url']) is manual
                expect(node).to_have_attribute('data-document-component', readiness['state'])
                if shown:
                    expect(node).to_be_visible()
                    expect(node).to_have_text(UNAVAILABLE['message'])
                else:
                    expect(node).to_be_hidden()
                assert page.url.endswith(path)
                if path == '/':
                    assert draft.input_value() == '尚未提交的草稿'

            observe_poll(CHECKING, False)  # Slow checking remains hidden.
            documents.value = dict(UNAVAILABLE)
            observe_poll(UNAVAILABLE, True)  # Neither mode swallows failure.
            documents.value = dict(CHECKING)
            if manual:
                # The existing manual contract stops at the first terminal
                # readiness. Reopening starts a new check; do not change it.
                page.reload()
                expect(node).to_be_hidden()
                if path == '/':
                    draft.fill('尚未提交的草稿')
            observe_poll(CHECKING, False)
            documents.value = dict(READY)
            observe_poll(READY, False)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
