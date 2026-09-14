import threading
import time
from flask import Flask
import pytest

from knowledge_distiller.v1.desktop_pages import DesktopPages, install


def registry():
    return DesktopPages(probe_timeout=.03, navigation_grace=.02, recovery_timeout=.06, opening_timeout=.03)


def connect(pages, name='page', **kwargs):
    return pages.connect(name, kwargs.pop('document', name + '-document'), **kwargs)


def respond(pages, page, *, visible=True, focused=True):
    def run():
        with pages.condition:
            assert pages.condition.wait_for(lambda: pages.generation > 0, timeout=1)
            pages.acknowledge(page.page_id, page.connection_epoch, pages.generation,
                              request_id=pages.request_id, visible=visible, focused=focused)
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def opener(pages, calls):
    def open_page(nonce):
        calls.append(nonce)
        connect(pages, 'opened', launch=nonce)
    return open_page


def test_zero_page_opens_once_and_requires_matching_handshake():
    pages = registry(); opened = []
    result = pages.reopen(opener(pages, opened))
    assert result.status == 'online' and result.reason == 'opened_handshake'
    assert len(opened) == 1 and not result.foreground_verified


def test_live_visible_ack_reports_visibility_not_foreground():
    pages = registry(); page = connect(pages)
    responder = respond(pages, page)
    result = pages.reopen(lambda _: pytest.fail('duplicate'))
    responder.join(1)
    assert result.status == 'visible_reported'
    assert result.received and result.visible and result.focused and not result.foreground_verified


def test_hidden_ack_is_online_not_display_success():
    pages = registry(); page = connect(pages)
    responder = respond(pages, page, visible=False, focused=False)
    result = pages.reopen(lambda _: pytest.fail('duplicate'))
    responder.join(1)
    assert result.status == 'online' and result.received and not result.visible


def test_visible_without_focus_is_not_display_success():
    pages = registry(); page = connect(pages)
    responder = respond(pages, page, focused=False)
    result = pages.reopen(lambda _: pytest.fail('duplicate'))
    responder.join(1)
    assert result.status == 'online' and result.visible and not result.focused


def test_close_needs_pagehide_and_stream_end_then_zero_page_opens():
    pages = registry(); page = connect(pages)
    pages.leave(page.page_id, page.connection_epoch)
    assert pages.reopen(lambda _: pytest.fail('live stream')).status == 'unknown'
    pages.disconnect(page.page_id, page.connection_epoch)
    assert pages.reopen(opener(pages, [])).reason == 'opened_handshake'


def test_transport_loss_and_suspension_are_unknown_not_zero():
    for disconnected in [False, True]:
        pages = registry(); page = connect(pages)
        if disconnected:
            pages.disconnect(page.page_id, page.connection_epoch)
        for _ in range(3):
            assert pages.reopen(lambda _: pytest.fail('duplicate')).status == 'unknown'


def test_old_epoch_disconnect_close_and_ack_cannot_change_new_document():
    pages = registry(); old = connect(pages); new = connect(pages)
    assert old.connection_epoch != new.connection_epoch
    pages.disconnect(old.page_id, old.connection_epoch)
    pages.leave(old.page_id, old.connection_epoch)
    assert not pages.acknowledge(old.page_id, old.connection_epoch, visible=True)
    assert pages.connected == {'page'}
    assert pages.pages['page'].leaving_at is None


def test_duplicate_session_id_gets_distinct_page_without_losing_original():
    pages = registry(); first = connect(pages)
    second = connect(pages, document='duplicate-document')
    assert first.page_id != second.page_id
    assert len(pages.pages) == 2
    assert pages.pages[first.page_id] is first


def test_navigation_claim_preserves_id_and_selection_even_slow():
    pages = registry(); before = connect(pages, route='/settings')
    pages.acknowledge(before.page_id, before.connection_epoch, visible=True, focused=True, interaction=True)
    pages.leave(before.page_id, before.connection_epoch, navigating=True)
    pages.disconnect(before.page_id, before.connection_epoch)
    time.sleep(.04)  # beyond the navigation grace, still not a closed tab
    assert pages.reopen(lambda _: pytest.fail('slow navigation duplicate')).status == 'unknown'
    after = connect(pages, document='new-document', route='/topics')
    assert before.page_id == after.page_id
    assert before.registration_seq == after.registration_seq
    assert pages.selected == after.page_id and after.route == '/topics'


def test_navigation_gap_waits_for_new_connection():
    pages = registry(); before = connect(pages)
    pages.leave(before.page_id, before.connection_epoch)
    pages.disconnect(before.page_id, before.connection_epoch)
    def navigate():
        time.sleep(.005)
        after = connect(pages, document='new-document')
        thread = respond(pages, after); thread.join(1)
    thread = threading.Thread(target=navigate); thread.start()
    assert pages.reopen(lambda _: pytest.fail('duplicate')).status == 'visible_reported'
    thread.join(1)


def test_background_connect_never_steals_recent_interaction_and_fallback_is_stable():
    pages = registry(); first = connect(pages, 'z'); second = connect(pages, 'a')
    assert pages.selected == 'z'  # registration order, not random UUID order
    pages.acknowledge('a', second.connection_epoch, visible=True, focused=True, interaction=True)
    connect(pages, 'background')
    assert pages.selected == 'a'
    pages.leave('a', second.connection_epoch); pages.disconnect('a', second.connection_epoch)
    time.sleep(.03)
    with pages.condition: pages._retire_departed()
    assert pages.selected == 'z'


def test_other_page_wrong_request_and_late_generation_do_not_ack():
    pages = registry(); first = connect(pages, 'one'); second = connect(pages, 'two')
    pages.target = 'one'; pages.generation = 2; pages.request_id = 'current'
    for page, generation, request_id in [(second, 2, 'current'), (first, 1, 'current'), (first, 2, 'old')]:
        pages.acknowledge(page.page_id, page.connection_epoch, generation,
                          request_id=request_id, visible=True, focused=True)
        assert pages.ack is None


def test_hidden_ack_then_visible_ack_same_generation_can_complete():
    pages = registry(); page = connect(pages)
    def run():
        with pages.condition:
            pages.condition.wait_for(lambda: pages.generation, timeout=1)
            pages.acknowledge(page.page_id, page.connection_epoch, pages.generation,
                              request_id=pages.request_id)
        time.sleep(.005)
        pages.acknowledge(page.page_id, page.connection_epoch, pages.generation,
                          request_id=pages.request_id, visible=True, focused=True)
    thread = threading.Thread(target=run); thread.start()
    assert pages.reopen(lambda _: pytest.fail('duplicate')).status == 'visible_reported'
    thread.join(1)


def test_open_timeout_does_not_loop_new_tabs_and_explicit_recovery_is_deduplicated():
    pages = registry(); calls = []
    result = pages.reopen(calls.append)
    assert result.status == 'unknown' and result.reason == 'open_handshake_timeout'
    for _ in range(5):
        assert pages.reopen(calls.append).status == 'unknown'
    assert len(calls) == 1
    assert pages.reopen(calls.append, explicit_request=result.request_id).status == 'unknown'
    assert pages.reopen(calls.append, explicit_request=result.request_id).reason == 'duplicate_action'
    assert len(calls) == 2


def test_failed_open_can_retry_without_false_success():
    pages = registry()
    assert pages.reopen(lambda _: (_ for _ in ()).throw(OSError())).status == 'failed'
    assert pages.reopen(opener(pages, [])).status == 'online'


def test_wrong_launch_nonce_does_not_complete_opening():
    pages = registry()
    assert pages.reopen(lambda _: connect(pages, launch='wrong')).status == 'unknown'


def test_routes_require_process_token_epoch_and_valid_payload():
    app = Flask(__name__); pages = install(app); client = app.test_client()
    assert client.post('/desktop/ack', json={'page': 'p'}).status_code == 403
    assert client.post('/desktop/ack', json={'token': pages.token, 'page': [], 'epoch': 'e'}).status_code == 400
    assert client.post('/desktop/close', json={'token': pages.token, 'page': 'p'}).status_code == 400
    page = connect(pages, 'p')
    data = {'token': pages.token, 'page': 'p', 'epoch': page.connection_epoch}
    assert client.post('/desktop/ack', json=data).status_code == 204
    assert client.post('/desktop/close', json=data).status_code == 204
    assert pages.pages['p'].leaving_at is not None
    assert client.get('/desktop/events?page=p&document=d', headers={'X-Desktop-Token': 'old-process'}).status_code == 403


def test_stream_handshake_and_stale_finally_are_epoch_safe():
    app = Flask(__name__); pages = install(app); client = app.test_client()
    headers = {'X-Desktop-Token': pages.token}
    first = client.get('/desktop/events?page=p&document=d&route=https://foreign.test/&browser=untrusted', headers=headers, buffered=False)
    assert b'hello' in next(iter(first.response))
    old = pages.pages['p']
    second = client.get('/desktop/events?page=p&document=d', headers=headers, buffered=False)
    assert b'hello' in next(iter(second.response))
    first.close()
    assert pages.connected == {'p'}
    assert old.route == '/' and old.browser_hint == 'unknown'
    second.close()
    assert pages.pages['p'].transport_state == 'disconnected'  # unknown until pagehide


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


def test_native_confirmed_browser_exit_retires_only_nonce_associated_pages():
    pages = registry()
    owned = connect(pages, 'owned', launch='native-launch', browser_hint='chrome')
    manual = connect(pages, 'manual', browser_hint='chrome')
    pages.associate_launch('native-launch', 'chrome:123:instance-a')
    pages.disconnect(owned.page_id, owned.connection_epoch)
    # Same family is not proof that a manually opened document belonged to PID 123.
    pages.browser_exited('chrome:123:instance-a')
    assert set(pages.pages) == {'manual'}
    assert pages.pages['manual'] is manual


def test_browser_identity_survives_navigation_and_copied_tab():
    pages = registry()
    first = connect(pages, launch='launch')
    pages.associate_launch('launch', 'browser:one')
    duplicate = connect(pages, document='duplicate')
    assert duplicate.browser_instance == 'browser:one'
    pages.leave(first.page_id, first.connection_epoch, navigating=True)
    after = connect(pages, document='navigated')
    assert after.browser_instance == 'browser:one'
    pages.browser_exited('browser:unrelated')
    assert len(pages.pages) == 2
    pages.browser_exited('browser:one')
    assert not pages.pages


def test_favicon_uses_brand_content_hash_and_all_browser_sizes():
    import hashlib
    import re
    import struct
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'src/knowledge_distiller/v1'
    template = (root/'templates/base.html').read_text()
    names = re.findall(r'icons/(favicon-(\d+)-([a-f0-9]+)\.png)', template)
    assert {int(size) for _, size, _ in names} == {16, 32, 48, 64, 96}
    for name, size, digest in names:
        data = (root/'static/icons'/name).read_bytes()
        assert data.startswith(b'\x89PNG\r\n\x1a\n')
        assert struct.unpack('>II', data[16:24]) == (int(size), int(size))
        assert hashlib.sha256(data).hexdigest().startswith(digest)
    assert '?protocol=2' in template and 'href="data:,"' not in template


def test_new_background_window_visible_but_unfocused_does_not_steal_selection():
    pages = registry(); first = connect(pages, 'first')
    pages.acknowledge('first', first.connection_epoch, visible=True, focused=True, interaction=True)
    background = connect(pages, 'new-window')
    pages.acknowledge('new-window', background.connection_epoch, visible=True, focused=False)
    assert pages.selected == 'first'


def test_navigation_new_handshake_can_overtake_old_pagehide_beacon():
    pages = registry(); before = connect(pages)
    def delayed_beacon():
        time.sleep(.005)
        pages.leave(before.page_id, before.connection_epoch)
        pages.disconnect(before.page_id, before.connection_epoch)
    thread = threading.Thread(target=delayed_beacon); thread.start()
    after = connect(pages, document='next-document')
    thread.join(1)
    assert after.page_id == before.page_id
    assert after.registration_seq == before.registration_seq
    assert pages.connected == {after.page_id}


def test_reconnect_after_finished_probe_does_not_replay_old_show_request():
    app = Flask(__name__); pages = install(app); pages.probe_timeout = .005
    client = app.test_client(); headers = {'X-Desktop-Token': pages.token}
    first = client.get('/desktop/events?page=p&document=d', headers=headers, buffered=False)
    next(iter(first.response))
    assert pages.reopen(lambda _: pytest.fail('duplicate')).status == 'unknown'
    assert pages.probe_until == 0
    second = client.get('/desktop/events?page=p&document=d', headers=headers, buffered=False)
    iterator = iter(second.response)
    assert b'hello' in next(iterator)
    assert next(iterator).startswith(b': alive')
    first.close(); second.close()
