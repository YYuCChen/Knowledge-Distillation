import hashlib
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from knowledge_distiller.v1.component_updates import ComponentUpdates
from knowledge_distiller.v1.updates import UpdateError


def make(tmp_path):
    return ComponentUpdates(tmp_path, info={'version': '1', 'display_version': '1.2',
        'bundle': str(tmp_path / 'program'), 'feed_url': 'https://example.com/appcast-windows.xml',
        'public_key': 'unused', 'windows_update': True, 'component_updates': True})


def test_component_ui_uses_shared_plan_and_verifies_cache_before_install(tmp_path, monkeypatch):
    updates = make(tmp_path)
    data = b'component bytes'
    asset = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    plan = SimpleNamespace(source='base', assets=(asset,), download_bytes=len(data))
    calls = []
    def prepare(envelope, **kwargs):
        calls.append((envelope, kwargs))
        return {'version': '2', 'product_version': '1.2'}, plan
    monkeypatch.setattr(updates.assembler, 'prepare', prepare)
    monkeypatch.setattr('knowledge_distiller.v1.component_updates.httpx.stream',
        lambda *a, **k: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'signed envelope'))).stream('GET', 'https://example.com'))
    updates._run('check')
    assert updates.phase == 'available' and len(calls) == 1
    assert updates.release['selected']['size'] == len(data)
    assert (updates.root / 'component-release.json').read_bytes() == b'signed envelope'
    cache = updates.assembler.downloader.root
    cache.mkdir()
    (cache / asset['sha256']).write_bytes(data)
    updates._run('download')
    assert updates.phase == 'downloaded'
    accepted = []
    updates.install = lambda: accepted.append(True)
    (cache / asset['sha256']).write_bytes(b'bad')
    with pytest.raises(UpdateError, match='缓存已变化'):
        updates.request_install()
    assert not accepted
    (cache / asset['sha256']).write_bytes(data)
    updates.request_install()
    assert updates.phase == 'installing' and accepted == [True]


def test_automatic_component_check_can_create_its_own_state(tmp_path, monkeypatch):
    updates = make(tmp_path)
    monkeypatch.setattr(updates, '_run', lambda action: None)
    assert updates.start('check', automatic=True)
    updates.thread.join()
    assert (updates.root / 'component-state.json').exists()


def test_helper_refuses_changed_request_before_asking_process_to_exit(tmp_path, monkeypatch):
    import json
    from knowledge_distiller.v1 import component_update_helper as helper
    (tmp_path / '.desktop-instance.json').write_text(json.dumps({'pid': 123, 'port': 45678}))
    monkeypatch.setattr(helper.os, 'kill', lambda *args: pytest.fail('must not stop another instance'))
    with pytest.raises(UpdateError, match='进程已变化'):
        helper.request_exit(tmp_path, {'parent_pid': 124}, 'macos-arm64')
    monkeypatch.setattr(helper.httpx, 'get', lambda *a, **k: SimpleNamespace(json=lambda: {'token': 'other', 'phase': 'installing'}))
    with pytest.raises(UpdateError, match='身份已变化'):
        helper.request_exit(tmp_path, {'parent_pid': 123, 'request_token': 'expected'}, 'macos-arm64')
