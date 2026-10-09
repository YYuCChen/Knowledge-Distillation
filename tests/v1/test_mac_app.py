from pathlib import Path
import urllib.request

from knowledge_distiller.v1.app import AppPaths
from knowledge_distiller.v1.mac_app import serve,configure_bundled_runtime


def test_desktop_server_uses_owned_data_and_assigned_port(tmp_path):
    root=tmp_path/'desktop-data'
    app,server,thread=serve(AppPaths(root),0)
    try:
        assert server.server_port>0
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
            assert response.status==200 and '知识蒸馏器' in response.read().decode()
        assert (root/'knowledge.sqlite3').exists()
        assert app.config['KNOWLEDGE_DISTILLER_STORE'].path.parent==root
    finally:
        server.shutdown();server.server_close()
        app.config['KNOWLEDGE_DISTILLER_WORKER'].stop();thread.join(2)
    assert not thread.is_alive()


def test_frozen_runtime_needs_no_developer_path(tmp_path,monkeypatch):
    import sys,os
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path),raising=False)
    monkeypatch.setenv('PATH','/developer-only/bin')
    configure_bundled_runtime()
    assert os.environ['PATH'].split(':')==[str(tmp_path/'bin'),'/usr/bin','/bin','/usr/sbin','/sbin']


def test_frozen_opencli_uses_bundle_without_global_install(tmp_path,monkeypatch):
    import sys,subprocess
    from knowledge_distiller.v1.opencli_session import read_opencli
    import knowledge_distiller.v1.opencli_session as module
    page=tmp_path/'opencli/dist/src/browser/page.js';page.parent.mkdir(parents=True);page.write_text('')
    node=tmp_path/'bin/node';node.parent.mkdir();node.write_bytes(b'candidate-node')
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path),raising=False)
    monkeypatch.setattr(module.shutil,'which',lambda name:None)
    calls=[]
    def run(args,**kwargs):
        calls.append(args);return subprocess.CompletedProcess(args,0,'{"ok":true}','')
    monkeypatch.setattr(module.subprocess,'run',run)
    assert read_opencli('xpost','x','https://x.com/a/status/1','test')=={'ok':True}
    assert calls[0][0]==str(node) and calls[0][2]==str(tmp_path/'opencli')


def test_application_shutdown_closes_owned_browser_sessions(tmp_path, monkeypatch):
    from knowledge_distiller.v1.app import create_application
    from knowledge_distiller.v1.douyin_session import DouyinOwnedSession
    closed=[]
    monkeypatch.setattr(DouyinOwnedSession,'close',lambda self:closed.append(self.platform))
    app=create_application(AppPaths(tmp_path))
    try:
        app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()
        assert set(closed)=={'douyin','xiaohongshu','youtube','x','zhihu','weibo'}
    finally:
        app.config['KNOWLEDGE_DISTILLER_WORKER'].stop()


def test_dock_reopen_activates_browser_without_opening_another_page():
    from types import SimpleNamespace
    from knowledge_distiller.v1.mac_app import reveal_browser
    opened, raised = [], []
    app = SimpleNamespace(bundleIdentifier=lambda: 'browser.saved', bundleURL=lambda: '/browser',
        isTerminated=lambda: False, activateWithOptions_=lambda flags: raised.append(flags))
    workspace = SimpleNamespace(URLForApplicationToOpenURL_=lambda url: '/browser', openURL_=opened.append)
    for _ in range(3):
        assert reveal_browser('http://127.0.0.1:1234/', workspace, str, lambda:[app], 3, 'browser.saved') == 'browser.saved'
    assert opened == []
    assert raised == [3, 3, 3]


def test_dock_reopen_starts_browser_when_no_app_page_survives():
    from types import SimpleNamespace
    from knowledge_distiller.v1.mac_app import reveal_browser
    opened = []
    workspace = SimpleNamespace(URLForApplicationToOpenURL_=lambda url: '/browser', openURL_=opened.append)
    from knowledge_distiller.v1.desktop_pages import DesktopPages
    DesktopPages(opening_timeout=.001).reopen(lambda nonce:workspace.openURL_('http://127.0.0.1:1234/'),
        lambda page:reveal_browser('http://127.0.0.1:1234/', workspace, str, lambda:[], 3, 'browser.saved'))
    assert opened == ['http://127.0.0.1:1234/']


def test_second_launcher_finds_running_default_browser_without_saved_identity():
    from types import SimpleNamespace
    from knowledge_distiller.v1.mac_app import reveal_browser
    opened, raised = [], []
    app = SimpleNamespace(bundleIdentifier=lambda: 'browser.default', bundleURL=lambda: '/browser',
        isTerminated=lambda:False, activateWithOptions_=lambda flags:raised.append(flags))
    workspace = SimpleNamespace(URLForApplicationToOpenURL_=lambda url:'/browser', openURL_=opened.append)
    assert reveal_browser('http://127.0.0.1:1234/', workspace, str, lambda:[app], 3) == 'browser.default'
    assert not opened and raised == [3]


def test_changed_default_browser_does_not_duplicate_pages_after_previous_browser_exits():
    from types import SimpleNamespace
    from knowledge_distiller.v1.mac_app import reveal_browser
    opened, raised = [], []
    app = SimpleNamespace(bundleIdentifier=lambda: 'browser.new', bundleURL=lambda: '/new',
        isTerminated=lambda:False, activateWithOptions_=lambda flags:raised.append(flags))
    workspace = SimpleNamespace(URLForApplicationToOpenURL_=lambda url:'/new', openURL_=opened.append)
    for _ in range(3):
        reveal_browser('http://127.0.0.1:1234/', workspace, str, lambda:[app], 3, 'browser.old')
    assert not opened and raised == [3, 3, 3]


def test_native_reopen_runs_probes_off_main_loop_and_coalesces_clicks():
    import threading
    import time
    from knowledge_distiller.v1.desktop_pages import DesktopPages
    from knowledge_distiller.v1.mac_app import NativeReopener
    pages = DesktopPages(opening_timeout=.1)
    main = threading.get_ident()
    calls, results = [], []
    def open_page(nonce):
        assert threading.get_ident() == main
        calls.append(nonce)
        pages.connect('page', 'document', launch=nonce)
    native = NativeReopener(pages, open_page, lambda page: None, results.append)
    assert native.request()
    for _ in range(5):
        assert not native.request()
    deadline = time.monotonic() + 1
    while not results and time.monotonic() < deadline:
        native.poll()
        time.sleep(.001)
    assert len(calls) == 1 and results[0].status == 'online'
    assert not native.running


def test_native_recovery_callback_handles_unknown_on_owner_loop():
    import threading
    import time
    from knowledge_distiller.v1.desktop_pages import DesktopPages
    from knowledge_distiller.v1.mac_app import NativeReopener
    pages = DesktopPages(probe_timeout=.05)
    pages.connect('hidden', 'document')
    main = threading.get_ident()
    results = []
    def complete(outcome):
        assert threading.get_ident() == main
        results.append(outcome)
    native = NativeReopener(pages, lambda _: None, lambda _: None, complete)
    start = time.monotonic()
    assert native.request()
    assert time.monotonic() - start < .04
    deadline = time.monotonic() + 1
    ticks = 0
    while not results and time.monotonic() < deadline:
        native.poll(); ticks += 1
        time.sleep(.001)
    assert ticks > 2 and results[0].status == 'unknown'
