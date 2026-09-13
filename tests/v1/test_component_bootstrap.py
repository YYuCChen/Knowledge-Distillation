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
