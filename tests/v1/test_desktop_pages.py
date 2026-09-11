import threading
import time
from flask import Flask

from knowledge_distiller.v1.desktop_pages import DesktopPages, install


def test_closed_page_reopens_even_though_browser_is_running():
    pages = DesktopPages(probe_timeout=0.02)
    opened = []
    assert pages.reopen(lambda:opened.append('open')) == 'opened'
    assert opened == ['open']


def test_live_page_reused_and_fast_clicks_never_open_duplicate():
    pages = DesktopPages(probe_timeout=0.05)
    pages.connect('page')
    opened = []
    def respond():
        deadline = time.monotonic()+1
        while not pages.generation and time.monotonic()<deadline:
            time.sleep(0.001)
        pages.acknowledge('page', pages.generation)
    responder = threading.Thread(target=respond); responder.start()
    assert pages.reopen(lambda:opened.append('open')) == 'reused'
    responder.join()
    pages.disconnect('page')
    assert pages.reopen(lambda:opened.append('open')) == 'opened'
    for _ in range(4):
        assert pages.reopen(lambda:opened.append('duplicate')) == 'opening'
    assert opened == ['open']


def test_lost_browser_connection_opens_current_default():
    pages = DesktopPages(probe_timeout=0.01)
    pages.connect('old'); pages.disconnect('old')
    opened = []
    assert pages.reopen(lambda:opened.append('default')) == 'opened'
    assert opened == ['default']


def test_failed_open_can_retry_immediately():
    pages = DesktopPages(probe_timeout=0.01)
    import pytest
    with pytest.raises(RuntimeError):
        pages.reopen(lambda: (_ for _ in ()).throw(RuntimeError('open failed')))
    calls = []
    assert pages.reopen(lambda:calls.append(1)) == 'opened'
    assert calls == [1]


def test_routes_require_process_token_and_validate_payload():
    app = Flask(__name__); pages = install(app)
    client = app.test_client()
    assert client.post('/desktop/ack', json={'page':'p', 'generation':1}).status_code == 403
    assert client.post('/desktop/ack', json={'token':pages.token, 'page':[], 'generation':1}).status_code == 400
    pages.connect('p')
    assert client.post('/desktop/ack', json={'token':pages.token, 'page':'p', 'generation':0}).status_code == 204
    assert client.post('/desktop/close', json={'token':pages.token, 'page':'p'}).status_code == 204
    assert not pages.connected


def test_second_launcher_queues_on_owning_server(tmp_path):
    from werkzeug.serving import make_server
    from knowledge_distiller.v1.mac_app import request_reopen
    app = Flask(__name__); pages = install(app)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        request_reopen(server.server_port, pages.token)
        request_reopen(server.server_port, pages.token)
        assert pages.take_request() is True
        assert pages.take_request() is False
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_navigation_gap_waits_for_existing_page_instead_of_opening_duplicate():
    pages = DesktopPages(probe_timeout=0.1)
    pages.connect('before'); pages.disconnect('before')
    def navigate():
        time.sleep(0.01)
        pages.connect('after')
        deadline = time.monotonic()+1
        while not pages.generation and time.monotonic()<deadline:
            time.sleep(0.001)
        pages.acknowledge('after', pages.generation)
    thread = threading.Thread(target=navigate); thread.start()
    assert pages.reopen(lambda: (_ for _ in ()).throw(AssertionError('duplicate'))) == 'reused'
    thread.join(2)


def test_stream_close_removes_presence_and_does_not_leave_lease():
    app = Flask(__name__); pages = install(app)
    response = app.test_client().get('/desktop/events?page=tab', headers={'X-Desktop-Token':pages.token}, buffered=False)
    assert next(iter(response.response)).startswith(b': alive')
    assert pages.connected == {'tab'}
    response.close()
    assert not pages.connected


def test_suspended_page_is_not_mistaken_for_closed_tab():
    pages = DesktopPages(probe_timeout=0.01); pages.connect('suspended')
    activated=[]
    assert pages.reopen(lambda: (_ for _ in ()).throw(AssertionError('duplicate')),
                        lambda:activated.append(True)) == 'unresponsive'
    assert activated == [True]


def test_other_page_ack_does_not_claim_selected_tab_was_reused():
    pages = DesktopPages(probe_timeout=0.01)
    pages.connect('other'); pages.connect('selected')
    pages.target='selected'; pages.generation=1
    pages.acknowledge('other',1)
    assert pages.ack == 0
