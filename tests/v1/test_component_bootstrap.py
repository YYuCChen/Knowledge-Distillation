from knowledge_distiller.v1.component_bootstrap import create_installer


def test_close_waits_until_response_body_is_finished(tmp_path):
    import re
    import threading
    app = create_installer(target=tmp_path/'program', data_root=tmp_path/'data',
        platform='windows-x86_64', public_key='unused', manifest_url='https://example.com/release.json')
    stopped = threading.Event()
    app.config['SHUTDOWN'] = stopped.set
    client = app.test_client()
    token = re.search(r'name="token" value="([^"]+)"', client.get('/').text).group(1)
    response = client.post('/', data={'token': token, 'action': 'close'}, buffered=False)
    assert not stopped.wait(.1)
    assert response.get_data(as_text=True) == '安装器已关闭，可以关闭此页面。'
    response.close()
    assert stopped.wait(2)


def test_installer_get_is_read_only_and_mutations_require_token(tmp_path):
    root, target = tmp_path / 'data', tmp_path / 'program'
    app = create_installer(target=target, data_root=root, platform='windows-x86_64',
        public_key='unused', manifest_url='https://example.com/release.json')
    client = app.test_client()
    response = client.get('/')
    assert response.status_code == 200
    assert '检查安装内容' in response.get_data(as_text=True)
    assert not root.exists() and not target.exists()
    assert client.post('/', data={'action': 'install'}).status_code == 403
    assert client.get('/', headers={'Host': 'foreign.example'}).status_code == 403
    assert client.post('/', headers={'Origin': 'https://foreign.example'}).status_code == 403


def test_manifest_http_failure_keeps_target_and_has_recovery_message(tmp_path, monkeypatch):
    import httpx
    import re
    import time
    import knowledge_distiller.v1.component_bootstrap as module
    def fail(*args, **kwargs):
        response = httpx.Response(404, request=httpx.Request('GET', 'https://example.com/release.json'))
        response.raise_for_status()
    monkeypatch.setattr(module.httpx, 'stream', fail)
    root, target = tmp_path / 'data', tmp_path / 'program'
    app = create_installer(target=target, data_root=root, platform='windows-x86_64',
        public_key='unused', manifest_url='https://example.com/release.json')
    client = app.test_client()
    token = re.search(r'name="token" value="([^"]+)"', client.get('/').text).group(1)
    assert client.post('/', data={'token': token, 'action': 'prepare',
        'target': str(target), 'data_root': str(root)}).status_code == 302
    deadline = time.monotonic() + 2
    while True:
        body = client.get('/').text
        if '当前发布尚未提供' in body:
            break
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert '程序未被替换' in body and 'HTTPStatusError' not in body
    assert not target.exists() and not root.exists()



import pytest


@pytest.mark.parametrize('role,fault,lead', [
    ('base', 404, '应用基础文件不可取得。'), ('docling', 404, '文档识别组件不可取得。'),
    ('delta', 404, '目标更新文件不可取得。'), ('docling', 'timeout', '文档识别组件需处理。'),
    ('base', 'hash', '应用基础文件需处理。'),
])
def test_component_asset_failure_is_not_reported_as_missing_manifest(tmp_path, monkeypatch, role, fault, lead):
    # BUG-20260914-09 (Q9): the installer page names the failing resource and
    # stage; only a manifest 404 may say the release has no platform manifest.
    import hashlib, re, time
    from types import SimpleNamespace
    import httpx
    import knowledge_distiller.v1.component_bootstrap as module
    from knowledge_distiller.v1.component_assembly import ComponentAssembly
    from knowledge_distiller.v1.component_download import ComponentDownloader
    data = b'expected'
    asset = {'url': 'https://example.com/asset.zip?private=secret', 'sha256': hashlib.sha256(data).hexdigest(),
             'size': len(data), 'unpacked_size': 100}
    other = {**asset, 'sha256': 'f' * 64}
    release = {'version': '2.0', 'target_identity': 'fixture', 'deltas': [],
               'base': asset if role == 'base' else other, 'docling': asset if role == 'docling' else other}
    def respond(request):
        if fault == 'timeout':
            raise httpx.ReadTimeout('timed out', request=request)
        if isinstance(fault, int):
            return httpx.Response(fault)
        return httpx.Response(200, content=b'x' * len(data))
    client = httpx.Client(transport=httpx.MockTransport(respond))
    class Assembly(ComponentAssembly):
        def prepare(self, envelope, **kwargs):  # Signed-manifest parsing is covered elsewhere.
            return release, SimpleNamespace(assets=[asset], download_bytes=len(data), source='base')
    class Stream:
        def __enter__(self): return SimpleNamespace(raise_for_status=lambda: None, iter_bytes=lambda size: [b'{}'])
        def __exit__(self, *args): pass
    monkeypatch.setattr(module, 'ComponentAssembly', Assembly)
    monkeypatch.setattr(module, 'ComponentDownloader', lambda cache, **kwargs: ComponentDownloader(cache, client=client))
    monkeypatch.setattr(module.httpx, 'stream', lambda *args, **kwargs: Stream())
    root, target = tmp_path / 'data', tmp_path / 'program'
    app = create_installer(target=target, data_root=root, platform='macos-arm64',
        public_key='unused', manifest_url='https://example.com/release.json')
    web = app.test_client()
    token = re.search(r'name="token" value="([^"]+)"', web.get('/').text).group(1)
    for action in ('prepare', 'install'):
        web.post('/', data={'token': token, 'action': action, 'target': str(target), 'data_root': str(root)})
        deadline = time.monotonic() + 3
        while web.get('/status').get_json()['busy']:
            assert time.monotonic() < deadline
            time.sleep(.01)
    state = web.get('/status').get_json()
    assert state['error'].startswith(lead) and '当前发布尚未提供' not in state['error']
    assert state['problem']['resource_role'] == role and state['problem']['stage'] == 'prepare'
    assert 'secret' not in web.get('/').text and not target.exists()
    client.close()
