from knowledge_distiller.v1.component_bootstrap import create_installer


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
