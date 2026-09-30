"""BUG-20260916-01 (整理按钮) and BUG-20260916-02 (失败提示生命周期), accepted separately."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from knowledge_distiller.organization_service import OrganizationStartKind
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.web import create_app
from .test_organization import organization
from .test_topics import library


def failing(point, connection):
    if point == 'after_topic_replace':
        raise sqlite3.IntegrityError('isolated organization failure')


def events(store):
    with connect(store.path) as db:
        return [tuple(row) for row in db.execute('SELECT event_id,status FROM organization_events ORDER BY event_id')]


def test_parallel_starts_create_or_reuse_exactly_one_event(tmp_path):
    _, store, _ = library(tmp_path)
    service, _ = organization(store)
    barrier = threading.Barrier(4)
    def start():
        barrier.wait()
        return service.start_or_reuse()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: start(), range(4)))
    kinds = sorted(result.kind.name for result in results)
    assert kinds.count(OrganizationStartKind.STARTED.name) == 1
    assert {result.event_id for result in results} == {events(store)[0][0]}
    assert events(store) == [(1, 'running')]


def test_repeated_posts_from_two_tabs_reuse_the_running_event(tmp_path):
    _, store, _ = library(tmp_path)
    service, _ = organization(store)
    app = create_app(store, object(), organization=service)
    first, second = app.test_client(), app.test_client()
    assert first.post('/organization').status_code == 302
    assert second.post('/organization').status_code == 302
    assert first.post('/organization').status_code == 302
    assert events(store) == [(1, 'running')]
    page = first.get('/').text
    assert '正在整理' in page and 'disabled>正在整理</button>' in page


def test_failure_notice_is_rendered_hidden_but_history_is_kept(tmp_path):
    _, store, _ = library(tmp_path)
    service, _ = organization(store, fail=failing)
    service.drive(service.start_or_reuse().event_id)
    page = create_app(store, object()).test_client().get('/').text
    notice = page.split('<p class="organization-feedback"', 1)[1].split('</p>', 1)[0]
    assert 'data-organization-event="1"' in notice and 'data-organization-failed="true"' in notice
    assert 'hidden' in notice.split('>', 1)[0]  # A refresh or a new visit starts a new baseline.
    with connect(store.path) as db:
        row = db.execute('SELECT status,failure_code FROM organization_events').fetchone()
    assert row['status'] == 'failed' and row['failure_code']  # Event and code remain traceable.


def test_read_failure_stays_visible_on_every_render(tmp_path, monkeypatch):
    _, store, _ = library(tmp_path)
    import knowledge_distiller.v1.organization as module
    monkeypatch.setattr(module, 'organization_status', lambda store: (_ for _ in ()).throw(sqlite3.OperationalError()))
    page = create_app(store, object()).test_client().get('/').text
    notice = page.split('<p class="organization-feedback"', 1)[1].split('</p>', 1)[0]
    assert '暂时无法读取待整理知识。' in notice and 'hidden' not in notice.split('>', 1)[0]


@pytest.fixture
def served(tmp_path):
    from werkzeug.serving import make_server
    _, store, _ = library(tmp_path)
    state = {'engine': None}
    app = create_app(store, object(), organization=lambda: state['engine'])
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', store, state
    server.shutdown()
    thread.join()


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip('Browser regression requires playwright install chromium')
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.mark.parametrize('gesture', ['click', 'dblclick', 'Enter', 'Space'])
def test_accepted_organize_request_really_disables_the_button(served, browser, gesture):
    url, store, state = served
    state['engine'], _ = organization(store)
    page = browser.new_page()
    posts = []
    page.on('request', lambda request: request.method == 'POST' and posts.append(request.url))
    page.goto(url + '/')
    button = page.locator('.organization-section button')
    assert button.inner_text() == '开始整理' and button.is_enabled()
    if gesture in {'Enter', 'Space'}:
        button.focus()
        page.keyboard.press(gesture)
    else:
        getattr(button, gesture)()
    page.wait_for_function("document.querySelector('.organization-section button')?.textContent === '正在整理'")
    page.wait_for_timeout(2600)  # Past the request's finally and one status poll.
    assert button.is_disabled() and button.inner_text() == '正在整理'
    button.hover(force=True)
    assert button.evaluate("e => getComputedStyle(e).boxShadow") == 'none'  # No enabled hover.
    assert len(posts) == 1 and events(store) == [(1, 'running')]
    page.reload()
    assert button.is_disabled() and button.inner_text() == '正在整理'  # Same after refresh.


def test_in_flight_disable_is_immediate_and_start_failure_restores(served, browser):
    url, store, state = served
    class Unreadable:
        def start_or_reuse(self):
            from knowledge_distiller.organization_service import OrganizationStartResult
            return OrganizationStartResult(OrganizationStartKind.READ_FAILED)
    state['engine'] = Unreadable()
    page = browser.new_page()
    page.goto(url + '/')
    disabled_in_same_task = page.evaluate("""() => {
        const button = document.querySelector('.organization-section button');
        button.click();
        return button.disabled;
    }""")
    assert disabled_in_same_task is True
    dialog = page.locator('.app-dialog[open]')
    dialog.wait_for()
    assert '暂时无法读取待整理知识' in dialog.inner_text()
    page.get_by_role('button', name='知道了').click()
    button = page.locator('.organization-section button')
    assert button.is_enabled() and button.inner_text() == '开始整理'
    assert events(store) == []


def fail_once_more(store):
    service, _ = organization(store, fail=failing)
    service.drive(service.start_or_reuse().event_id)


def test_failure_notice_shows_only_for_failures_this_tab_watched(served, browser):
    url, store, state = served
    fail_once_more(store)  # History before any page is open.
    watcher = browser.new_page()
    watcher.goto(url + '/')
    notice = watcher.locator('.organization-feedback')
    assert notice.is_hidden()
    watcher.wait_for_timeout(2600)
    assert notice.is_hidden()  # Polling the same historical failure keeps it hidden.
    fail_once_more(store)  # A new failure while the tab is open.
    watcher.wait_for_function("!document.querySelector('.organization-feedback').hidden", timeout=8000)
    assert notice.inner_text() == '本次知识整理未完成，已有主题和新知保留，可以重试。'
    later = browser.new_page()
    later.goto(url + '/')
    later.wait_for_timeout(2600)
    assert later.locator('.organization-feedback').is_hidden()  # Its own baseline.
    assert notice.is_visible()  # The watching tab is not silenced by another tab.
    watcher.reload()
    assert watcher.locator('.organization-feedback').is_hidden()  # Refresh ends the notice.
    assert [status for _, status in events(store)] == ['failed', 'failed']


def test_running_event_that_fails_after_load_is_shown(served, browser):
    url, store, state = served
    service, _ = organization(store, fail=failing)
    event = service.start_or_reuse()
    page = browser.new_page()
    page.goto(url + '/')
    assert page.locator('.organization-feedback').is_hidden()
    service.drive(event.event_id)
    page.wait_for_function("!document.querySelector('.organization-feedback').hidden", timeout=8000)
    button = page.locator('.organization-section button')
    assert button.is_enabled() and button.inner_text() == '开始整理'  # Retry is available.
    page.wait_for_timeout(300)  # Past the 100 ms background transition.
    assert button.evaluate('e => getComputedStyle(e).backgroundColor') == 'rgb(20, 20, 19)'
