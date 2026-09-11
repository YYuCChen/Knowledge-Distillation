import json
from pathlib import Path
import subprocess

import pytest

from knowledge_distiller.v1.vault_access import publication_status, vault_status
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.store import Store
from .test_web import _complete_item
from .test_settings import service


def registry(home, entries):
    path=home/'Library/Application Support/obsidian/obsidian.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'vaults':entries}))
    return path


def test_registration_refresh_and_same_named_vault(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    first=tmp_path/'first/Vault';first.mkdir(parents=True)
    second=tmp_path/'second/Vault';second.mkdir(parents=True)
    note=first/'原文.md';note.write_text('不可改写')
    assert vault_status(None)['state']=='unconfigured'
    assert vault_status(str(first))['state']=='configured'
    assert publication_status(str(first),note.name)['state']=='unregistered'
    registry(tmp_path,{'second':{'path':str(second)}})
    assert publication_status(str(first),note.name)['url'] is None
    registry(tmp_path,{'first-id':{'path':str(first)}})
    assert publication_status(str(first),note.name)['url'].startswith('obsidian://open?vault=first-id&')
    assert note.read_text()=='不可改写'
    assert vault_status(str(first))['registration']=='registered'


def test_file_missing_invalid_path_and_unreadable_registry(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    vault=tmp_path/'Vault';vault.mkdir()
    assert publication_status(str(vault),'missing.md')['state']=='file_missing'
    assert publication_status(str(vault),'../outside.md')['state']=='invalid_path'
    assert publication_status(str(vault),str(tmp_path/'absolute.md'))['state']=='invalid_path'
    outside=tmp_path/'outside.md';outside.write_text('private')
    (vault/'linked.md').symlink_to(outside)
    assert publication_status(str(vault),'linked.md')['state']=='invalid_path'
    (vault/'note.md').write_text('body')
    registry(tmp_path,{}).write_text('{broken')
    assert publication_status(str(vault),'note.md')['state']=='registry_unavailable'
    assert publication_status(str(tmp_path/'gone'),'note.md')['state']=='vault_missing'


def test_home_rechecks_and_fallback_uses_original_publication(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    store=Store(tmp_path/'app.sqlite3');app=create_app(store,object());client=app.test_client()
    item=_complete_item(store,tmp_path)
    row=store.item_bundle(item);original=row['published_vault'];relative=row['published_path']
    other=tmp_path/'new-Vault';other.mkdir();store.set_setting('vault_path',str(other))
    before=Path(original,relative).read_bytes()
    page=client.get('/').text
    assert '连接仓库' not in page and '在 Finder 中显示笔记' not in page
    assert 'publication-message' not in page and 'is-disabled' not in page
    assert 'obsidian://open' not in page
    calls=[]
    monkeypatch.setattr('knowledge_distiller.v1.vault_access.subprocess.run',lambda args,**kwargs:calls.append(args))
    assert client.post(f'/items/{item}/open-publication/file').status_code==302
    assert calls[-1]==['/usr/bin/open','-R',str(Path(original,relative).resolve())]
    registry(tmp_path,{'old':{'path':original}})
    assert 'obsidian://open?vault=old' in client.get('/').text
    assert Path(original,relative).read_bytes()==before
    Path(original,relative).unlink()
    page=client.get('/').text
    assert '归档文件未找到' not in page and '打开原保存文件夹' not in page
    assert 'obsidian://open' not in page
    assert '注意力' in page
    assert client.post(f'/items/{item}/open-publication/file').status_code==400
    assert client.post(f'/items/{item}/open-publication/folder').status_code==302
    assert calls[-1]==['/usr/bin/open',str(Path(original).resolve())]
    assert store.item_bundle(item)['published_vault']==original


def test_choose_vault_rejects_unwritable_and_preserves_existing(tmp_path,monkeypatch):
    store=Store(tmp_path/'store.sqlite3');store.initialize()
    old=tmp_path/'old';old.mkdir();new=tmp_path/'new';new.mkdir()
    store.set_setting('vault_path',str(old));settings=service(store,vault_picker=lambda:new)
    def denied(**kwargs):raise PermissionError('fixture')
    monkeypatch.setattr('tempfile.TemporaryFile',denied)
    from knowledge_distiller.v1.settings import SettingsError
    with pytest.raises(SettingsError,match='vault_not_writable'):settings.choose_vault()
    assert store.setting('vault_path')==str(old)
