import json
from pathlib import Path

import pytest

from knowledge_distiller.v1.chrome import ChromePage, ChromeSessionError, DouyinChromeSession


def active_port(tmp_path: Path) -> Path:
    path = tmp_path / 'DevToolsActivePort'
    path.write_text('43123\n/devtools/browser/session-1\n', encoding='ascii')
    return path


class Socket:
    def __init__(self, *, fail_runtime=False):
        self.sent = []
        self.responses = []
        self.closed = False
        self.fail_runtime = fail_runtime

    def send(self, value):
        message = json.loads(value)
        self.sent.append(message)
        method = message['method']
        # A user-owned frozen tab exists. The client must never attach to it.
        assert method not in {'Target.setAutoAttach', 'Target.getTargets', 'Browser.setDownloadBehavior'}
        if method == 'Target.attachToTarget':
            assert message['params']['targetId'] == 'owned-page'
        result = {'Target.createTarget': {'targetId': 'owned-page'},
                  'Target.attachToTarget': {'sessionId': 'owned-session'}}.get(method, {})
        response = {'id': message['id'], 'result': result}
        if self.fail_runtime and method == 'Runtime.enable':
            response = {'id': message['id'], 'error': {'message': 'page unavailable'}}
        self.responses.extend([{'method': 'Target.targetInfoChanged'}, response])

    def recv(self):
        return json.dumps(self.responses.pop(0))

    def settimeout(self, timeout):
        assert 0 < timeout <= 20

    def close(self):
        self.closed = True


def test_connects_only_owned_page_despite_frozen_existing_target(tmp_path):
    socket = Socket()
    calls = []
    def connector(endpoint, **options):
        calls.append((endpoint, options))
        return socket
    with ChromePage('https://www.douyin.com/user/self', active_port(tmp_path), connector=connector):
        pass
    assert calls[0][0] == 'ws://127.0.0.1:43123/devtools/browser/session-1'
    assert calls[0][1]['suppress_origin'] is True
    assert socket.sent[-1]['method'] == 'Target.closeTarget'
    assert socket.sent[-1]['params'] == {'targetId': 'owned-page'}
    assert socket.closed


def test_initialization_failure_closes_owned_page_and_connection(tmp_path):
    socket = Socket(fail_runtime=True)
    with pytest.raises(ChromeSessionError, match='chrome_connection_failed'):
        ChromePage('about:blank', active_port(tmp_path), connector=lambda *a, **k: socket)
    assert socket.sent[-1]['method'] == 'Target.closeTarget'
    assert socket.closed


def test_missing_debug_endpoint_does_not_start_connection(tmp_path):
    with pytest.raises(ChromeSessionError, match='chrome_remote_debugging_disabled'):
        ChromePage('about:blank', tmp_path/'missing', connector=lambda *a, **k: pytest.fail('must not connect'))


class Page:
    def __init__(self, signed_in=True, label='  @测试账号  ', cookies=None):
        self.signed_in = signed_in
        self.label = label
        self.rows = cookies if cookies is not None else [{'name': 'sessionid', 'value': 'private'}]
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *_): self.closed = True
    def wait_for(self, expression): return self.signed_in
    def evaluate(self, expression): return self.label
    def cookies(self, urls):
        assert urls == ['https://www.douyin.com']
        return self.rows


def test_verified_connection_returns_display_label_and_closes_page():
    page = Page()
    assert DouyinChromeSession(lambda *args: page).verify().account_label == '@测试账号'
    assert page.closed


def test_cookie_transport_is_in_memory_and_drops_invalid_rows():
    page = Page(cookies=[{'name':'sessionid','value':'private'}, {'name':'empty','value':''}, {'name':3,'value':'invalid'}])
    assert DouyinChromeSession(lambda *args: page).cookies() == {'sessionid':'private'}
    assert page.closed


@pytest.mark.parametrize('operation', ['verify', 'cookies'])
def test_missing_douyin_login_closes_page(operation):
    page = Page(signed_in=False, cookies=[])
    with pytest.raises(ChromeSessionError, match='douyin_login_required'):
        getattr(DouyinChromeSession(lambda *args: page), operation)()
    assert page.closed


def test_default_pages_reuse_transport_but_close_each_owned_tab(tmp_path, monkeypatch):
    from knowledge_distiller.v1.chrome import close_browser_connections
    import websocket
    close_browser_connections()
    socket = Socket()
    connections = []
    monkeypatch.setattr(websocket, 'create_connection', lambda *a, **k: connections.append(a[0]) or socket)
    try:
        for _ in range(3):
            with ChromePage('about:blank', active_port(tmp_path)):
                pass
        assert len(connections) == 1
        assert not socket.closed
        assert sum(m['method'] == 'Target.closeTarget' for m in socket.sent) == 3
        assert len({m['id'] for m in socket.sent}) == len(socket.sent)
    finally:
        close_browser_connections()
    assert socket.closed


def test_browser_restart_replaces_old_transport(tmp_path, monkeypatch):
    from knowledge_distiller.v1.chrome import close_browser_connections
    import websocket
    close_browser_connections()
    sockets = []
    def connect(*a, **k):
        socket = Socket(); sockets.append(socket); return socket
    monkeypatch.setattr(websocket, 'create_connection', connect)
    path = active_port(tmp_path)
    try:
        with ChromePage('about:blank', path):pass
        path.write_text('43124\n/devtools/browser/restarted\n')
        with ChromePage('about:blank', path):pass
        assert len(sockets) == 2 and sockets[0].closed
        assert not sockets[1].closed
    finally:close_browser_connections()


def test_broken_transport_fails_current_operation_and_reconnects_next(tmp_path, monkeypatch):
    from knowledge_distiller.v1.chrome import close_browser_connections
    import websocket
    close_browser_connections()
    first, second = Socket(), Socket()
    sockets = iter((first,second))
    monkeypatch.setattr(websocket, 'create_connection', lambda *a, **k: next(sockets))
    path = active_port(tmp_path)
    try:
        with ChromePage('about:blank', path):pass
        first.recv = lambda: (_ for _ in ()).throw(ConnectionError())
        with pytest.raises(ChromeSessionError):ChromePage('about:blank', path)
        assert first.closed
        with ChromePage('about:blank', path):pass
        assert not second.closed
    finally:close_browser_connections()
