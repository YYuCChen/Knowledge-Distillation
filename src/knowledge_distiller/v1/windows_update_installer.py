"""Detached Windows updater, staged replacement and paused-startup acceptance."""
from __future__ import annotations
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import httpx
from .file_lock import acquire
from .updates import Updates, UpdateError, parse_feed, validate_install_paths
from .windows_delta import stage_payload


def launch(target, root, no_open, handshake=None):
    arguments=[str(target/'KnowledgeDistiller.exe'), '--data-dir', str(root)]
    if no_open: arguments.append('--no-open')
    if handshake: arguments += ['--update-handshake',str(handshake)]
    return subprocess.Popen(arguments, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


def rename_ready(source,target):
    # The Job closes at desktop exit, but Windows may release DLL handles later.
    deadline=time.monotonic()+15
    while True:
        try:
            source.rename(target)
            return
        except PermissionError:
            if time.monotonic()>=deadline: raise
            time.sleep(.2)


def journal_write(path,**state):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state),encoding='utf-8')
    temporary.replace(path)


def recover(plan_path,parent_pid):
    """Resume an interrupted transaction; accepted user work is never rolled back."""
    from .windows_job import wait_for_exit
    plan=json.loads(Path(plan_path).read_text(encoding='utf-8'))
    root=Path(plan['data_root']); target=Path(plan['info']['bundle'])
    validate_install_paths(root,target)
    if parent_pid: wait_for_exit(parent_pid)
    running=False
    with acquire(root/'.update.lock'):
        journal=root/'updates/install-journal.json'
        state=json.loads(journal.read_text(encoding='utf-8'))
        previous=target.with_name(target.name+'.update-backup')
        stage=target.with_name(target.name+'.update-stage')
        if state.get('target',str(target))!=str(target):
            raise UpdateError('更新恢复目录与原始请求不一致。')
        if state['phase']=='staging':
            if previous.exists(): raise UpdateError('更新阶段与回退目录不一致，已保留。')
            if stage.exists(): shutil.rmtree(stage)
            try:
                instance_lock=acquire(root/'.instance.lock');instance_lock.close()
            except BlockingIOError:
                running=True
            (root/'updates/install-error.txt').write_text('上次更新重建被中断，当前应用未改变，可重新尝试安装。',encoding='utf-8')
        elif state['phase']=='accepted':
            # The verified candidate may still be waiting for its handshake.
            decision=root/'updates/startup-accept.tmp'
            decision.write_text('accepted')
            decision.replace(root/'updates/startup-handshake')
            try:
                instance_lock=acquire(root/'.instance.lock')
                instance_lock.close()
            except BlockingIOError:
                running=True
            if previous.exists(): shutil.rmtree(previous)
            (root/'updates/before-install.sqlite3').unlink(missing_ok=True)
        else:
            # Refuse manual recovery while any app using these data is live.
            with acquire(root/'.instance.lock'):
                if previous.exists():
                    if target.exists(): shutil.rmtree(target)
                    rename_ready(previous,target)
                    if state.get('snapshot'):
                        database=root/'knowledge.sqlite3'
                        for suffix in ('-wal','-shm'): Path(str(database)+suffix).unlink(missing_ok=True)
                        shutil.copy2(root/'updates/before-install.sqlite3',database)
                if stage.exists(): shutil.rmtree(stage)
                (root/'updates/install-error.txt').write_text('上次更新被中断，已恢复旧版本，可重新检查并重试。',encoding='utf-8')
        journal.unlink(missing_ok=True)
        (root/'updates/shutdown-request').unlink(missing_ok=True)
    target.with_name(target.name+'.恢复更新.cmd').unlink(missing_ok=True)
    if not running: launch(target,root,plan.get('no_open'))
    return 0


def run(plan_path):
    from .windows_job import wait_for_exit
    plan=json.loads(Path(plan_path).read_text(encoding='utf-8'))
    root=Path(plan['data_root'])
    updates=Updates(root,info=plan['info'])
    target=Path(updates.info['bundle'])
    stage=target.with_name(target.name+'.update-stage')
    previous=target.with_name(target.name+'.update-backup')
    handshake=updates.root/'startup-handshake'
    shutdown=updates.root/'shutdown-request'
    backup=updates.root/'before-install.sqlite3'
    journal=updates.root/'install-journal.json'
    rescue=target.with_name(target.name+'.恢复更新.cmd')
    result=1
    stopped=swapped=snapshot=accepted=False
    candidate=None
    lock=None
    owns_transaction=False
    try:
        validate_install_paths(root,target)
        lock=acquire(root/'.update.lock')
        state=json.loads((root/'.desktop-instance.json').read_text(encoding='utf-8'))
        if state['pid']!=plan['parent_pid']:
            raise UpdateError('安装请求的应用进程已变化。')
        if journal.exists():
            pending=json.loads(journal.read_text(encoding='utf-8'))
            if pending.get('phase')=='staging' and pending.get('target')==str(target) and not previous.exists():
                # Retry after a killed helper while this same old desktop is still live.
                if stage.exists(): shutil.rmtree(stage)
                journal.unlink();rescue.unlink(missing_ok=True)
            else:
                raise UpdateError('上次更新尚待恢复，请重新启动应用。')
        if previous.exists() or stage.exists():
            raise UpdateError('发现尚未处理的更新目录，已保留当前应用。')
        release=parse_feed((updates.root/'appcast.xml').read_bytes(),updates.info['public_key'],updates.info['version'])
        if not release or release['version']!=plan['version']:
            raise UpdateError('安装目标已变化，请重新检查更新。')
        asset=release['full'] if plan['asset_name']==release['full']['name'] else release['selected']
        if asset['name']!=plan['asset_name']:
            raise UpdateError('安装文件已变化。')
        path=updates.download(asset)
        owns_transaction=True
        # Record ownership before the potentially long full-directory reconstruction.
        journal_write(journal,phase='staging',snapshot=False,target=str(target),version=plan['version'])
        helper=updates.root/'update-helper.exe'
        quote=lambda path: str(path).replace('%','%%')
        rescue.write_text('@echo off\nchcp 65001 >nul\nstart "" "'+quote(helper)+'" --recover "'+quote(Path(plan_path).resolve())+'" 0\n',encoding='utf-8')
        try:
            stage_payload(path,target,stage,version=plan['version'],current=updates.info['version'])
        except Exception:
            if asset!=release['full']:
                (updates.root/'full-update-required.json').write_text(json.dumps({'version':plan['version'],'reason':'delta_not_applicable'}))
            raise
        journal_write(journal,phase='staged',snapshot=False,target=str(target),version=plan['version'])
        shutdown.write_text(str(plan['parent_pid']))
        wait_for_exit(plan['parent_pid'],120000)
        stopped=True
        # The desktop owns all workers in its Windows Job; exit closes that job.
        database=root/'knowledge.sqlite3'
        if database.exists():
            with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
            snapshot=True
        journal_write(journal,phase='replacing',snapshot=snapshot,target=str(target),version=plan['version'])
        rename_ready(target,previous)
        swapped=True
        rename_ready(stage,target)
        handshake.unlink(missing_ok=True)
        candidate=launch(target,root,plan.get('no_open'),handshake)
        deadline=time.monotonic()+120
        ready=False
        while time.monotonic()<deadline and candidate.poll() is None:
            try:
                state=json.loads((root/'.desktop-instance.json').read_text(encoding='utf-8'))
                if state['pid']==candidate.pid:
                    url='http://127.0.0.1:'+str(state['port'])
                    status=httpx.get(url+'/settings/updates/status',timeout=2).json()
                    if status['version']==plan['version'] and status['phase']=='installing' and httpx.get(url+'/',timeout=2).status_code==200:
                        ready=True
                        break
            except (OSError,ValueError,httpx.HTTPError):
                pass
            time.sleep(.25)
        if not ready:
            raise UpdateError('新版本启动检查未通过，恢复旧版本。')
        decision=updates.root/'startup-accept.tmp'
        decision.write_text('accepted')
        # Once acceptance can be observed no rollback may discard live user work.
        journal_write(journal,phase='accepted',snapshot=snapshot,target=str(target),version=plan['version'])
        accepted=True
        decision.replace(handshake)
        result=0
        try:
            shutil.rmtree(previous)
            backup.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            rescue.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        except OSError as error:
            (updates.root/'cleanup-error.txt').write_text(str(error))
    except Exception as error:
        (updates.root/'install-error.txt').write_text(str(error),encoding='utf-8')
        if not accepted:
            if candidate is not None and candidate.poll() is None:
                candidate.terminate(); candidate.wait(timeout=60)
            if swapped:
                if target.exists(): shutil.rmtree(target)
                rename_ready(previous,target)
                if snapshot:
                    database=root/'knowledge.sqlite3'
                    for suffix in ('-wal','-shm'): Path(str(database)+suffix).unlink(missing_ok=True)
                    shutil.copy2(backup,database)
            if owns_transaction:
                if stage.exists(): shutil.rmtree(stage)
                journal.unlink(missing_ok=True)
                rescue.unlink(missing_ok=True)
            if stopped:
                if lock: lock.close(); lock=None
                launch(target,root,plan.get('no_open'))
    finally:
        shutdown.unlink(missing_ok=True)
        if lock: lock.close()
        (updates.root/'install-result.json').write_text(json.dumps({'version':plan['version'],'exit_code':result}),encoding='utf-8')
    return result
