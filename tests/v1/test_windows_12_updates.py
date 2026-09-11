import sys
import json
import pytest
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows runtime')

from knowledge_distiller.v1 import updates
from knowledge_distiller.v1.app import AppPaths, create_application

def test_windows_frozen_version_never_reads_mac_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path/'KnowledgeDistiller.exe'))
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    (tmp_path/'windows-version.json').write_text(json.dumps({'version': '2026.09.11.8', 'product_version': '1.11'}))
    info=updates.bundle_info()
    assert info['version']=='2026.09.11.8'
    service=updates.Updates(tmp_path)
    state=service.snapshot()
    assert state['system'].startswith('Windows ')
    assert state['manual_download_url']=='https://github.com/YYuCChen/Knowledge-Distillation/releases/latest'
    assert not state['configured'] and not state['can_install']
    with pytest.raises(updates.UpdateError): service.start('download')
    with pytest.raises(updates.UpdateError): service.request_install()

def test_windows_settings_and_resources(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    monkeypatch.setenv('APPDATA',str(tmp_path/'roaming'))
    app=create_application(AppPaths(tmp_path/'data'),start_workers=False)
    try:
        client=app.test_client()
        response=client.get('/settings?open=updates')
        assert response.status_code==200
        text=response.get_data(as_text=True)
        assert 'Windows 官方下载' in text
        assert 'Windows 应用内安装尚未支持' in text
        assert 'data-update-primary' not in text
        for resource in ('updates.js','settings.css','guides/doubao-asr.html','guides/guide.css','guides/assets/doubao-asr/create-bucket.png'):
            assert client.get('/static/'+resource).status_code==200
    finally:
        app.config['KNOWLEDGE_DISTILLER_CLOSE_FEISHU']()
        app.config['KNOWLEDGE_DISTILLER_WORKER'].stop()
        app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()


def test_offline_probe_allows_event_loop_ipc_but_blocks_external_network(tmp_path, monkeypatch):
    import socket
    from knowledge_distiller.v1 import windows_app, runtime_probe
    monkeypatch.setattr(socket.socket, 'connect', socket.socket.connect)
    monkeypatch.setattr(socket, 'create_connection', socket.create_connection)

    def check(*args, **kwargs):
        left, right = socket.socketpair()
        try:
            left.sendall(b'event-loop')
            assert right.recv(10) == b'event-loop'
        finally:
            left.close()
            right.close()
        with socket.socket() as external:
            with pytest.raises(RuntimeError, match='network_disabled'):
                external.connect(('1.1.1.1', 443))
        with pytest.raises(RuntimeError, match='network_disabled'):
            socket.create_connection(('example.invalid', 443))
        return 0

    monkeypatch.setattr(runtime_probe, 'check', check)
    assert windows_app.main(['--data-dir', str(tmp_path), '--no-open',
                             '--check-runtime', str(tmp_path/'result.json'), '--check-offline']) == 0
