"""BUG-20260915-02: the document-model notice follows the polled readiness."""
import threading
from pathlib import Path

import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app

CHECKING = {'state': 'checking', 'message': '正在检查本地文档模型，完成前文档任务会等待。'}
UNAVAILABLE = {'state': 'unavailable', 'message':
               '本地文档模型缺失或校验失败。请使用知识蒸馏器安装器，选择当前程序和数据目录重新检查并修复，然后重试文档任务。'}


class Documents:
    """The same readiness surface DoclingSourceConverter exposes to the app."""
    def __init__(self, readiness):
        self.value = dict(readiness)

    @property
    def readiness(self):
        return dict(self.value)


def application(tmp_path, readiness):
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    documents = Documents(readiness)
    app = create_app(store, object())
    app.context_processor(lambda: {'document_component': documents.readiness})
    app.extensions['document_component'] = documents
    return app, documents


def notice(html):
    return html.split('data-document-component=', 1)[1].split('</p>', 1)[0]


@pytest.mark.parametrize('readiness,shown', [(CHECKING, True), (UNAVAILABLE, True),
                                             ({'state': 'ready', 'message': ''}, False)])
def test_notice_has_stable_state_and_is_hidden_when_ready(tmp_path, readiness, shown):
    app, _ = application(tmp_path, readiness)
    for path in ('/', '/settings', '/topics', '/insights'):
        html = app.test_client().get(path).get_data(as_text=True)
        assert notice(html).startswith(f'"{readiness["state"]}"'), path
        assert ('hidden' in notice(html).split('>', 1)[0]) is not shown, path
        if shown:
            assert readiness['message'] in notice(html)


def test_status_poll_reports_current_readiness(tmp_path):
    app, documents = application(tmp_path, CHECKING)
    client = app.test_client()
    assert client.get('/settings/updates/status').get_json()['document_component']['state'] == 'checking'
    documents.value = {'state': 'ready', 'message': ''}
    assert client.get('/settings/updates/status').get_json()['document_component']['state'] == 'ready'


@pytest.mark.parametrize('path', ['/', '/settings'])
def test_open_page_drops_the_notice_on_ready_and_keeps_repair_guidance(tmp_path, path):
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    app, documents = application(tmp_path, CHECKING)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}{path}')
            node = page.locator('[data-document-component]')
            assert node.is_visible() and node.inner_text() == CHECKING['message']
            draft = page.locator('textarea[name="content"]')
            if path == '/':
                draft.fill('尚未提交的草稿')
            documents.value = {'state': 'ready', 'message': ''}
            page.wait_for_function("document.querySelector('[data-document-component]').hidden", timeout=8000)
            assert page.url.endswith(path)  # No refresh or navigation was needed.
            if path == '/':
                assert draft.input_value() == '尚未提交的草稿'
            documents.value = dict(UNAVAILABLE)
            page.wait_for_function("!document.querySelector('[data-document-component]').hidden", timeout=8000)
            assert node.inner_text() == UNAVAILABLE['message']
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
