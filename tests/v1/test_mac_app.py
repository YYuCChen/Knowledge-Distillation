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
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path),raising=False)
    monkeypatch.setattr(module.shutil,'which',lambda name:'/bundled/node' if name=='node' else None)
    calls=[]
    def run(args,**kwargs):
        calls.append(args);return subprocess.CompletedProcess(args,0,'{"ok":true}','')
    monkeypatch.setattr(module.subprocess,'run',run)
    assert read_opencli('xpost','x','https://x.com/a/status/1','test')=={'ok':True}
    assert calls[0][0]=='/bundled/node' and calls[0][2]==str(tmp_path/'opencli')


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
    DesktopPages().reopen(lambda:workspace.openURL_('http://127.0.0.1:1234/'),
        lambda:reveal_browser('http://127.0.0.1:1234/', workspace, str, lambda:[], 3, 'browser.saved'))
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
