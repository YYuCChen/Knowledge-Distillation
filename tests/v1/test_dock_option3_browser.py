"""Dock option ③ (Q1/Q5, 2026-09-29): browser-protocol evidence in an isolated Chromium.

No new permission: the app reopens a page only when none is alive, otherwise it
activates the browser and asks the chosen product page to show itself. A page
the browser keeps in a background tab may stay hidden; that is the documented
boundary, never reported as success. This is Playwright's own Chromium, not the
user's Chrome or Safari, and it cannot certify a real Dock click.
"""
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from flask import Flask, render_template_string
from werkzeug.serving import make_server

from knowledge_distiller.v1.desktop_pages import install

ROOT = Path(__file__).resolve().parents[2] / 'src/knowledge_distiller/v1'
PAGE = '''{% extends "base.html" %}{% block content %}<main><h1>合成页面</h1>
<textarea id="draft" aria-label="合成草稿"></textarea></main>{% endblock %}'''


@pytest.fixture
def app_server():
    app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
    pages = install(app)
    pages.navigation_grace = .5  # Shorter than production (2 s) to keep the run brief.
    for route in ('/', '/settings', '/topics'):
        app.add_url_rule(route, route, lambda: render_template_string(PAGE))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', pages
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture
def chromium():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip('Browser regression requires playwright install chromium')
        yield playwright.chromium


def wait(predicate, timeout=8, describe=lambda: ''):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('condition not reached ' + describe())


def connected(pages):
    with pages.condition:
        return {p.page_id: p for p in pages.pages.values() if p.transport_state == 'connected'}


def alive(pages):
    with pages.condition:
        pages._retire_departed()
        return dict(pages.pages)


def reopen(pages, open_in):
    """Run the app's reopen on a worker; open requests are served on this thread."""
    requests, activated, result = queue.Queue(), [], {}
    worker = threading.Thread(target=lambda: result.update(outcome=pages.reopen(
        requests.put, activated.append)), daemon=True)
    worker.start()
    opened = []
    while worker.is_alive():
        try:
            nonce = requests.get(timeout=.05)
        except queue.Empty:
            continue
        opened.append(nonce)
        open_in(nonce)
    worker.join()
    return result['outcome'], activated, opened


def product_page(context, url, draft):
    page = context.new_page()
    page.goto(url)
    page.locator('#draft').fill(draft)
    page.evaluate('window.__sameDocument = true')
    return page


def test_multi_tab_target_is_the_last_used_page_and_nothing_reloads(app_server, chromium):
    url, pages = app_server
    browser = chromium.launch()
    context = browser.new_context()
    tabs = {route: product_page(context, url + route, '草稿' + route) for route in ('/', '/settings', '/topics')}
    wait(lambda: len(connected(pages)) == 3)
    tabs['/settings'].locator('#draft').click()  # A trusted interaction marks the page used last.
    wait(lambda: pages.selected in connected(pages) and connected(pages)[pages.selected].route == '/settings')
    outcome, activated, opened = reopen(pages, lambda nonce: pytest.fail('no page may be opened'))
    assert [page.route for page in activated] == ['/settings'] and opened == []
    assert outcome.status in {'visible_reported', 'online'} and outcome.target == pages.selected
    for route, page in tabs.items():
        assert page.locator('#draft').input_value() == '草稿' + route
        assert page.evaluate('window.__sameDocument === true')  # Not reloaded or replaced.
    browser.close()


def test_closed_tab_and_closed_window_reopen_exactly_one_page(app_server, chromium):
    url, pages = app_server
    browser = chromium.launch()
    window = browser.new_context()
    first, second = product_page(window, url + '/', 'a'), product_page(window, url + '/topics', 'b')
    wait(lambda: len(connected(pages)) == 2)
    first.close(run_before_unload=True)
    wait(lambda: len(alive(pages)) == 1)  # pagehide beacon + stream end retire only that tab.
    # Closing a window unloads each of its tabs (pagehide), like closing them one by one.
    for page in [second]:  # The window's remaining product tab.
        page.close(run_before_unload=True)
    wait(lambda: not alive(pages))
    reopened = browser.new_context()
    outcome, activated, opened = reopen(pages, lambda nonce: reopened.new_page().goto(
        url + '/?_desktop_launch=' + nonce))
    assert len(opened) == 1 and activated == []
    assert (outcome.status, outcome.reason) == ('online', 'opened_handshake')
    assert len(reopened.pages) == 1
    again, activated, opened = reopen(pages, lambda nonce: pytest.fail('a live page must be reused'))
    assert opened == [] and len(activated) == 1 and len(reopened.pages) == 1
    browser.close()


def test_page_gone_without_pagehide_is_unknown_not_success_or_duplicate(app_server, chromium):
    # Playwright's context teardown runs no unload handlers, like a crash. With
    # no departure signal the page is not proven closed: no duplicate is opened
    # and the outcome is unknown; since 2026-09-30 the native app shows no prompt
    # and opens another page only if a second click is still unanswered.
    url, pages = app_server
    pages.recovery_timeout = pages.probe_timeout = .5
    browser = chromium.launch()
    context = browser.new_context()
    product_page(context, url + '/', 'x')
    wait(lambda: len(connected(pages)) == 1)
    context.close()
    wait(lambda: not connected(pages))
    outcome, activated, opened = reopen(pages, lambda nonce: pytest.fail('no duplicate page'))
    assert opened == [] and (outcome.status, outcome.reason) == ('unknown', 'page_not_responding')
    browser.close()


def test_quit_browser_retires_its_pages_only_on_the_native_exit_signal(app_server, chromium):
    url, pages = app_server
    browser = chromium.launch()
    context = browser.new_context()
    outcome, _, opened = reopen(pages, lambda nonce: context.new_page().goto(url + '/?_desktop_launch=' + nonce))
    assert outcome.reason == 'opened_handshake'
    pages.associate_launch(opened[0], 'isolated-chromium-1')  # What mac_app records for its own openURL.
    browser.close()
    # Quitting may or may not deliver pagehide; only native NSRunningApplication
    # termination (simulated here) is proof the browser instance is gone.
    pages.browser_exited('isolated-chromium-1')
    assert not alive(pages)
    browser = chromium.launch()
    context = browser.new_context()
    outcome, _, opened = reopen(pages, lambda nonce: context.new_page().goto(url + '/?_desktop_launch=' + nonce))
    assert len(opened) == 1 and outcome.reason == 'opened_handshake'
    browser.close()


def test_background_tab_is_never_reported_as_shown_and_keeps_its_draft(app_server, chromium):
    url, pages = app_server
    browser = chromium.launch()
    context = browser.new_context()
    product = product_page(context, url + '/', '后台草稿')
    other = context.new_page()
    other.goto('about:blank')
    other.bring_to_front()
    wait(lambda: len(connected(pages)) == 1)
    hidden = product.evaluate('document.visibilityState') == 'hidden' or not product.evaluate('document.hasFocus()')
    outcome, activated, opened = reopen(pages, lambda nonce: pytest.fail('no duplicate page'))
    assert opened == [] and len(activated) == 1
    # Playwright emulates every page as visible and focused (headless and
    # headed alike), so this isolated run cannot reach a real background tab;
    # the hidden-ACK rule itself is covered in test_desktop_pages.py.
    if hidden:
        # The boundary of option ③: activation plus a show request cannot pick a
        # background tab; the app must not claim it did (no prompt since 2026-09-30).
        assert outcome.status != 'visible_reported'
    assert product.locator('#draft').input_value() == '后台草稿'
    assert product.evaluate('window.__sameDocument === true')
    evidence = {'hidden_in_isolated_chromium': hidden, 'outcome': asdict(outcome)}
    print('dock-option3-background', evidence)
    browser.close()
