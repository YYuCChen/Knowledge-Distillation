"""Rollback checkpoints use isolated app trees and SQLite, never user state."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from knowledge_distiller.v1 import windows_update_installer as installer,windows_job


def setup(tmp_path,monkeypatch):
    root=tmp_path/'data';(root/'updates').mkdir(parents=True)
    target=tmp_path/'app';target.mkdir();(target/'version').write_text('old')
    (root/'.desktop-instance.json').write_text(json.dumps({'pid':42}))
    (root/'updates/appcast.xml').write_text('signed feed fixture')
    with sqlite3.connect(root/'knowledge.sqlite3') as db:
        db.execute('create table marker(value text)');db.execute("insert into marker values('preserved')")
    (root/'credentials').write_bytes(b'synthetic-secret')
    asset={'name':'delta.zip'}
    release={'version':'2','full':{'name':'full.zip'},'selected':asset}
    monkeypatch.setattr(installer,'Updates',lambda root,info:SimpleNamespace(root=root/'updates',info=info,download=lambda asset:tmp_path/'delta.zip'))
    monkeypatch.setattr(installer,'parse_feed',lambda *a:release)
    monkeypatch.setattr(windows_job,'wait_for_exit',lambda *a:None)
    def stage(*args,**kwargs):
        output=Path(args[2]);output.mkdir();(output/'version').write_text('new')
    monkeypatch.setattr(installer,'stage_payload',stage)
    plan=root/'updates/install-plan.json'
    plan.write_text(json.dumps({'data_root':str(root),'info':{'bundle':str(target),'version':'1','public_key':'key'},'parent_pid':42,'version':'2','asset_name':'delta.zip','no_open':True}))
    return root,target,plan


def test_candidate_failure_restores_app_database_and_relaunches(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch); launches=[]
    def launch(app,data,no_open,handshake=None):
        launches.append(handshake)
        if handshake:
            with sqlite3.connect(root/'knowledge.sqlite3') as db:db.execute("update marker set value='candidate migration'")
        return SimpleNamespace(poll=lambda:1)
    monkeypatch.setattr(installer,'launch',launch)
    assert installer.run(plan)==1
    assert (target/'version').read_text()=='old'
    with sqlite3.connect(root/'knowledge.sqlite3') as db:assert db.execute('select value from marker').fetchone()[0]=='preserved'
    assert len(launches)==2 and launches[-1] is None
    assert (root/'credentials').read_bytes()==b'synthetic-secret'
    assert not target.with_name('app.update-backup').exists()
    assert not (root/'updates/install-journal.json').exists()


def test_staging_failure_never_stops_or_replaces_app(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    def fail(*args,**kwargs):raise ValueError('baseline mismatch')
    monkeypatch.setattr(installer,'stage_payload',fail)
    monkeypatch.setattr(installer,'launch',lambda *a:pytest.fail('should not relaunch live app'))
    assert installer.run(plan)==1
    assert (target/'version').read_text()=='old'
    assert not (root/'updates/shutdown-request').exists()
    assert json.loads((root/'updates/full-update-required.json').read_text())['version']=='2'


def test_interrupted_swap_recovers_before_workers(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    backup=target.with_name('app.update-backup');target.rename(backup);target.mkdir();(target/'version').write_text('new')
    import shutil
    shutil.copy2(root/'knowledge.sqlite3',root/'updates/before-install.sqlite3')
    with sqlite3.connect(root/'knowledge.sqlite3') as db:db.execute("update marker set value='unaccepted'")
    installer.journal_write(root/'updates/install-journal.json',phase='replacing',snapshot=True)
    launched=[];monkeypatch.setattr(installer,'launch',lambda *a:launched.append(a))
    assert installer.recover(plan,43)==0
    assert (target/'version').read_text()=='old'
    with sqlite3.connect(root/'knowledge.sqlite3') as db:assert db.execute('select value from marker').fetchone()[0]=='preserved'
    assert launched and not backup.exists()


def test_accepted_recovery_never_rolls_back_live_data(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    backup=target.with_name('app.update-backup');target.rename(backup);target.mkdir();(target/'version').write_text('new')
    with sqlite3.connect(root/'knowledge.sqlite3') as db:db.execute("update marker set value='live user work'")
    installer.journal_write(root/'updates/install-journal.json',phase='accepted',snapshot=True)
    monkeypatch.setattr(installer,'launch',lambda *a:None)
    assert installer.recover(plan,43)==0
    assert (target/'version').read_text()=='new'
    with sqlite3.connect(root/'knowledge.sqlite3') as db:assert db.execute('select value from marker').fetchone()[0]=='live user work'


def test_accepted_recovery_unpauses_live_candidate_without_relaunch(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    backup=target.with_name('app.update-backup');target.rename(backup);target.mkdir();(target/'version').write_text('new')
    installer.journal_write(root/'updates/install-journal.json',phase='accepted',snapshot=True)
    real_acquire=installer.acquire
    def acquire(path):
        if Path(path).name=='.instance.lock':raise BlockingIOError('candidate is alive')
        return real_acquire(path)
    monkeypatch.setattr(installer,'acquire',acquire)
    monkeypatch.setattr(installer,'launch',lambda *a:pytest.fail('already running candidate'))
    assert installer.recover(plan,43)==0
    assert (root/'updates/startup-handshake').read_text()=='accepted'
    assert not backup.exists() and not (root/'updates/install-journal.json').exists()


def test_interruption_between_renames_recovers_missing_target(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    backup=target.with_name('app.update-backup');target.rename(backup)
    stage=target.with_name('app.update-stage');stage.mkdir();(stage/'version').write_text('new')
    installer.journal_write(root/'updates/install-journal.json',phase='replacing',snapshot=False)
    monkeypatch.setattr(installer,'launch',lambda *a:None)
    assert installer.recover(plan,0)==0
    assert (target/'version').read_text()=='old' and not stage.exists()


def test_staging_recovery_keeps_live_old_app_and_data(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    stage=target.with_name('app.update-stage');stage.mkdir();(stage/'partial').write_text('partial model')
    installer.journal_write(root/'updates/install-journal.json',phase='staging',snapshot=False,target=str(target))
    real_acquire=installer.acquire
    def acquire(path):
        if Path(path).name=='.instance.lock':raise BlockingIOError('old app remains alive')
        return real_acquire(path)
    monkeypatch.setattr(installer,'acquire',acquire)
    monkeypatch.setattr(installer,'launch',lambda *a:pytest.fail('do not restart live old app'))
    assert installer.recover(plan,0)==0
    assert (target/'version').read_text()=='old' and not stage.exists()
    with sqlite3.connect(root/'knowledge.sqlite3') as db:assert db.execute('select value from marker').fetchone()[0]=='preserved'


def test_retry_cleans_only_recorded_staging_before_rebuild(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    stage=target.with_name('app.update-stage');stage.mkdir();(stage/'partial').write_text('partial')
    installer.journal_write(root/'updates/install-journal.json',phase='staging',snapshot=False,target=str(target))
    rebuilt=[]
    def rebuild(*args,**kwargs):
        assert not stage.exists()
        assert json.loads((root/'updates/install-journal.json').read_text())['phase']=='staging'
        rebuilt.append(True)
        raise ValueError('deliberate stop after safe retry')
    monkeypatch.setattr(installer,'stage_payload',rebuild)
    assert installer.run(plan)==1 and rebuilt==[True]
    assert (target/'version').read_text()=='old' and not stage.exists()


def test_unowned_stage_is_not_removed_on_failed_preflight(tmp_path,monkeypatch):
    root,target,plan=setup(tmp_path,monkeypatch)
    stage=target.with_name('app.update-stage');stage.mkdir();(stage/'foreign').write_text('preserve')
    assert installer.run(plan)==1
    assert (stage/'foreign').read_text()=='preserve'


@pytest.mark.parametrize('backup_fails',[False,True])
def test_snapshot_handles_close_before_swap_and_after_backup_failure(tmp_path,monkeypatch,backup_fails):
    root,target,plan=setup(tmp_path,monkeypatch)
    original=sqlite3.connect;connections=[]
    class Tracked(sqlite3.Connection):
        closed=False
        def close(self):
            self.closed=True
            return super().close()
        def backup(self,destination,*args,**kwargs):
            if backup_fails:raise sqlite3.OperationalError('snapshot failed')
            return super().backup(destination,*args,**kwargs)
    def connect(*args,**kwargs):
        connection=original(*args,**kwargs,factory=Tracked);connections.append(connection);return connection
    monkeypatch.setattr(installer.sqlite3,'connect',connect)
    launches=[]
    def launch(app,data,no_open,handshake=None):
        assert connections and all(connection.closed for connection in connections)
        launches.append(handshake)
        if handshake:
            (root/'.desktop-instance.json').write_text(json.dumps({'pid':99,'port':12345}))
        return SimpleNamespace(pid=99,poll=lambda:None)
    monkeypatch.setattr(installer,'launch',launch)
    monkeypatch.setattr(installer.httpx,'get',lambda *a,**k:SimpleNamespace(status_code=200,json=lambda:{'version':'2','phase':'installing'}))
    assert installer.run(plan)==(1 if backup_fails else 0)
    assert len(connections)>=2 and all(connection.closed for connection in connections)
    assert launches
    assert (target/'version').read_text()==('old' if backup_fails else 'new')
    if not backup_fails:
        assert not (root/'updates/before-install.sqlite3').exists()
        assert not (root/'updates/cleanup-error.txt').exists()
        assert not (root/'updates/install-journal.json').exists()
