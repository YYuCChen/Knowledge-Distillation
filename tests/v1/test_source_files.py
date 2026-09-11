import subprocess
import sys

import pytest

from knowledge_distiller.v1.file_sources import prepare_file
from knowledge_distiller.v1.source_files import copy_path, SourceCopyError
from knowledge_distiller.v1.settings import SettingsService
from knowledge_distiller.v1.web import create_app
from .test_submitted_sources import setup


def test_copy_survives_success_and_reupload_restores_same_item(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    source = prepare_file('原文.md', '# 标题\n\n正文'.encode())
    item = store.submit_source(source)
    path = copy_path(tmp_path, source.source_kind, source.source_key, source.label)
    assert path.read_bytes() == source.content
    worker.run_one()
    assert store.item_bundle(item)['state'] == 'succeeded'
    assert path.read_bytes() == source.content
    assert not store.item_bundle(item)['input_available']
    path.unlink()
    assert store.submit_source(prepare_file('重命名.md', source.content)) == item
    assert path.read_bytes() == source.content
    assert model.calls == 1
    store.initialize()
    assert path.exists()


def test_copy_no_clobber_and_failed_intake_has_no_item(tmp_path):
    store, *_ = setup(tmp_path)
    source = prepare_file('原文.md', b'body')
    path = copy_path(tmp_path, source.source_kind, source.source_key, source.label)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'edited')
    with pytest.raises(SourceCopyError, match='已被修改'):
        store.submit_source(source)
    assert path.read_bytes() == b'edited'
    assert not store.recent_items()


def test_open_actions_use_owned_paths_and_report_failures(tmp_path, monkeypatch):
    store, service, *_ = setup(tmp_path)
    settings = SettingsService(store)
    client = create_app(store, service, settings).test_client()
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw: calls.append(command))
    if sys.platform == 'win32':
        import os
        monkeypatch.setattr(os, 'startfile', lambda path: calls.append(['startfile', path]))
    opener = 'startfile' if sys.platform == 'win32' else '/usr/bin/open'
    page = client.get('/settings?open=paths').text
    assert '原文件副本' in page and '打开文件夹' in page
    assert str(tmp_path / 'source-files') in page
    assert not (tmp_path / 'source-files').exists()
    assert client.post('/settings/source-files/open', data={'path': '/arbitrary'}).status_code == 302
    assert calls == [[opener, str(tmp_path / 'source-files')]]
    source = prepare_file('原文.md', b'body')
    item = store.submit_source(source)
    path = copy_path(tmp_path, source.source_kind, source.source_key, source.label)
    assert client.get(f'/items/{item}/open-source-file').status_code == 405
    assert client.post(f'/items/{item}/open-source-file').status_code == 302
    assert calls[-1] == [opener, str(path)]
    path.unlink()
    assert client.post(f'/items/{item}/open-source-file').status_code == 400
    def fail(*args, **kwargs):
        raise OSError('test unavailable')
    monkeypatch.setattr(subprocess, 'run', fail)
    if sys.platform == 'win32':
        monkeypatch.setattr(os, 'startfile', fail)
    assert '未能打开' in client.post('/settings/source-files/open', follow_redirects=True).text


def test_missing_copy_fails_truthfully_and_reupload_recovers(tmp_path):
    store, service, model, worker, vault = setup(tmp_path)
    source = prepare_file('原文.md', '正文'.encode())
    item = store.submit_source(source)
    path = copy_path(tmp_path, source.source_kind, source.source_key, source.label)
    path.unlink()
    worker.run_one()
    assert store.item_bundle(item)['error_code'] == 'source_copy_unavailable'
    assert model.calls == 0
    assert store.submit_source(source) == item
    store.retry_item(item)
    worker.run_one()
    assert store.item_bundle(item)['state'] == 'succeeded'


def test_expired_working_bytes_do_not_expire_file_copy(tmp_path):
    from knowledge_distiller.v1.database import connect
    store, service, model, worker, vault = setup(tmp_path)
    source = prepare_file('原文.md', '正文'.encode())
    item = store.submit_source(source)
    store.mark_failed(item, 'collecting', 'test_failure')
    with connect(store.path) as connection:
        connection.execute("UPDATE submitted_sources SET content=NULL, retain_until='2000-01-01' WHERE item_id=?", (item,))
    store.initialize()
    store.retry_item(item)
    worker.run_one()
    assert store.item_bundle(item)['state'] == 'succeeded'


def test_upgrade_backfills_only_available_bytes(tmp_path):
    from knowledge_distiller.v1.database import connect
    store, *_ = setup(tmp_path)
    source = prepare_file('原文.md', b'body')
    item = store.submit_source(source)
    path = copy_path(tmp_path, source.source_kind, source.source_key, source.label)
    path.unlink()
    store.initialize()
    assert path.read_bytes() == source.content
    path.unlink()
    with connect(store.path) as connection:
        connection.execute('UPDATE submitted_sources SET content=NULL WHERE item_id=?', (item,))
    store.initialize()
    assert not path.exists()


def test_symlink_cannot_redirect_upload(tmp_path):
    store, *_ = setup(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'source-files').symlink_to(outside)
    with pytest.raises(SourceCopyError):
        store.submit_source(prepare_file('原文.md', b'body'))
    assert not list(outside.iterdir())
    assert not store.recent_items()
