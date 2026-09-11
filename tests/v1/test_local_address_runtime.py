import socket

from knowledge_distiller.v1.app import AppPaths
from knowledge_distiller.v1.local_address import LocalAddress, load, save
from knowledge_distiller.v1.mac_app import serve


def test_runtime_host_origin_boundary_and_conflict_preserve_configuration(tmp_path):
    save(tmp_path, LocalAddress('trusted-name', 57740))
    app, server, thread = serve(AppPaths(tmp_path), 0, start_workers=False)
    app.extensions['updates'].phase = 'idle'
    client = app.test_client()
    port = server.server_port
    base = f'http://trusted-name.localhost:{port}'
    try:
        assert client.get('/settings', base_url=base).status_code == 200
        assert client.get('/settings', base_url=f'http://evil.localhost:{port}').status_code == 403
        assert client.get('/settings', base_url=f'http://trusted-name.localhost:{port+1}').status_code == 403
        assert client.post('/settings/local-address', base_url=base,
            headers={'Origin':'https://evil.test'}, data={'name':'attacker','port':'57842'}).status_code == 403
        assert client.post('/settings/local-address', base_url=base,
            headers={'Origin':base.replace('http:', 'https:')}, data={'name':'attacker','port':'57842'}).status_code == 403
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            response = client.post('/settings/local-address', base_url=base,
                data={'name':'valid-name', 'port':str(occupied.getsockname()[1])})
            assert 'local_address_port_busy' in response.location
            assert load(tmp_path) == LocalAddress('trusted-name', 57740)
        assert client.get('/settings/updates/status', base_url=base).status_code == 200
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
        app.config['KNOWLEDGE_DISTILLER_WORKER'].stop()
        app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()
