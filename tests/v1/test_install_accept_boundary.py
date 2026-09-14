"""SC-INSTALL-ACCEPT: durable acceptance owns the rollback boundary (K09)."""
import json
import sqlite3
from pathlib import Path

import pytest

from knowledge_distiller.v1 import component_install as module
from knowledge_distiller.v1.program_tree import identity


def fixture(tmp_path):
    def program(path, version):
        (path / '_internal').mkdir(parents=True)
        (path / 'KnowledgeDistiller.exe').write_bytes(version.encode())
        (path / '_internal/windows-version.json').write_text(json.dumps({'version': version}))
        return path
    candidate, target = program(tmp_path/'candidate', '2'), program(tmp_path/'app', '1')
    root = tmp_path/'data'; root.mkdir()
    with sqlite3.connect(root/'knowledge.sqlite3') as db:
        db.execute('CREATE TABLE facts(value)'); db.execute("INSERT INTO facts VALUES ('old')")
    class Process:
        def poll(self): return 0
    def launch(*args):
        with sqlite3.connect(root/'knowledge.sqlite3') as db:
            db.execute("UPDATE facts SET value='new'")
        return Process()
    kwargs = dict(platform='windows-x86_64', version='2', target_identity=identity(candidate,'windows-x86_64'),
                  launcher=launch, acceptance=lambda *args: None, activation=lambda *a:None)
    return candidate, target, root, kwargs


def assert_new(target, root):
    assert (target/'KnowledgeDistiller.exe').read_bytes() == b'2'
    with sqlite3.connect(root/'knowledge.sqlite3') as db:
        assert db.execute('SELECT value FROM facts').fetchone()[0] == 'new'


def test_accepted_write_raises_after_replace_never_rolls_back(tmp_path, monkeypatch):
    candidate,target,root,kwargs=fixture(tmp_path)
    original=module.write_record
    def uncertain(path, value):
        original(path,value)
        if value.get('phase') == 'accepted': raise OSError('write returned failure after persistence')
    monkeypatch.setattr(module,'write_record',uncertain)
    outcome=module.install(candidate,target,root,**kwargs)
    assert outcome['accepted'] is True
    assert_new(target,root)


def test_handshake_failure_returns_activation_pending_and_recover_preserves_new_data(tmp_path,monkeypatch):
    candidate,target,root,kwargs=fixture(tmp_path)
    original=Path.replace
    def fail(self,target):
        if Path(target).name == 'component-startup-handshake': raise PermissionError('handshake locked')
        return original(self,target)
    monkeypatch.setattr(Path,'replace',fail)
    outcome=module.install(candidate,target,root,**kwargs)
    assert outcome['accepted'] is True
    assert outcome['activation']['status']=='pending'
    assert_new(target,root)
    monkeypatch.setattr(Path,'replace',original)
    result=module.recover(target,root,activation=lambda *a:None)
    assert result['accepted'] is True
    assert (root/'updates/component-startup-handshake').read_text()=='accepted'
    assert_new(target,root)


def test_previous_cleanup_failure_is_finalization_pending_not_install_failure(tmp_path,monkeypatch):
    candidate,target,root,kwargs=fixture(tmp_path)
    original=module.shutil.rmtree
    def fail(path,*a,**kw):
        if Path(path).name.endswith('.component-previous'): raise PermissionError('previous locked')
        return original(path,*a,**kw)
    monkeypatch.setattr(module.shutil,'rmtree',fail)
    outcome=module.install(candidate,target,root,**kwargs)
    assert outcome['accepted'] is True and outcome['activation']['status']=='ready'
    assert outcome['finalization']['status']=='pending'
    assert_new(target,root)
    assert (root/'updates/component-install-journal.json').exists()


def test_interrupt_after_accept_preserves_journal_without_running_cleanup(tmp_path,monkeypatch):
    candidate,target,root,kwargs=fixture(tmp_path)
    original=module.write_record
    def interrupt(path,value):
        original(path,value)
        if value.get('phase')=='accepted': raise KeyboardInterrupt()
    monkeypatch.setattr(module,'write_record',interrupt)
    with pytest.raises(KeyboardInterrupt): module.install(candidate,target,root,**kwargs)
    assert_new(target,root)
    assert (root/'updates/component-install-journal.json').exists()
