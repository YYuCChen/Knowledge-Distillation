"""Closed-application installation with a durable journal and paused startup.

Assembly and model activation finish before this transaction. Existing instances
must exit normally; this installer never terminates an active user application.
"""
from contextlib import closing
import json
import os
import plistlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

import httpx

from .file_lock import acquire
from .local_records import write_record
from .program_tree import identity
from .updates import UpdateError, validate_install_paths, version_key


def launch(target, data_root, platform, handshake):
    executable = target / ('Contents/MacOS/KnowledgeDistiller' if platform == 'macos-arm64'
                           else 'KnowledgeDistiller.exe')
    arguments = [str(executable), '--data-dir', str(data_root), '--no-open',
                 '--update-handshake', str(handshake)]
    return subprocess.Popen(arguments, **({'creationflags': subprocess.CREATE_NO_WINDOW}
        if platform == 'windows-x86_64' else {'start_new_session': True}))


def accept_startup(process, root, version):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline and process.poll() is None:
        try:
            state = json.loads((root / '.desktop-instance.json').read_text())
            if state['pid'] == process.pid and type(state['port']) is int:
                url = 'http://127.0.0.1:' + str(state['port'])
                status = httpx.get(url + '/settings/updates/status', timeout=2).json()
                if (status['version'] == version and status['phase'] == 'installing'
                        and httpx.get(url + '/', timeout=2).status_code == 200):
                    return
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            pass
        time.sleep(.25)
    raise UpdateError('新版本启动验收未通过。')


def require_recovered(root):
    journal = Path(root) / 'updates/component-install-journal.json'
    if journal.exists():
        state = json.loads(journal.read_text())
        if state.get('phase') != 'accepted':
            raise UpdateError('组件安装被中断，请重新打开安装器完成恢复。知识数据尚未启动处理。')


def recover(target, data_root):
    target, root = Path(target), Path(data_root)
    validate_install_paths(root, target)
    updates = root / 'updates'
    journal = updates / 'component-install-journal.json'
    with acquire(root / '.update.lock'):
        state = json.loads(journal.read_text())
        if state['target'] != str(target.resolve()):
            raise UpdateError('安装恢复目标与原记录不一致。')
        previous = target.with_name(target.name + '.component-previous')
        stage = target.with_name(target.name + '.component-stage')
        backup = updates / 'component-before-install.sqlite3'
        if state['phase'] == 'accepted':
            temporary = updates / 'component-startup-handshake.tmp'
            temporary.write_text('accepted')
            temporary.replace(updates / 'component-startup-handshake')
            if previous.exists():
                shutil.rmtree(previous)
        else:
            with acquire(root / '.instance.lock'):
                swapped = previous.exists() or (not state['had_target']
                                                and state['phase'] in {'replacing', 'startup'})
                if swapped:
                    if state['had_database']:
                        if not backup.is_file():
                            raise UpdateError('安装前数据库快照不可用，已停止恢复。')
                        with closing(sqlite3.connect(backup)) as connection:
                            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                                raise UpdateError('数据库快照完整性检查失败。')
                    if target.exists():
                        if identity(target, state['platform']) != state['target_identity']:
                            raise UpdateError('安装后的程序已发生变化，已保留恢复副本。')
                        shutil.rmtree(target)
                    database = root / 'knowledge.sqlite3'
                    for suffix in ('-wal', '-shm'):
                        Path(str(database) + suffix).unlink(missing_ok=True)
                    if state['had_database']:
                        shutil.copy2(backup, database)
                    else:
                        database.unlink(missing_ok=True)
                    # Restore the launchable old path last, so a crash cannot
                    # expose it with a half-restored database.
                    if previous.exists():
                        previous.rename(target)
                if stage.exists():
                    shutil.rmtree(stage)
        backup.unlink(missing_ok=True)
        journal.unlink()
    return {'recovered': True, 'accepted': state['phase'] == 'accepted'}


def install(candidate, target, data_root, *, platform, version, target_identity,
            launcher=launch, acceptance=accept_startup):
    candidate, target, root = Path(candidate), Path(target), Path(data_root)
    validate_install_paths(root, target)
    from .windows_platform import is_link_or_reparse
    if is_link_or_reparse(target) or is_link_or_reparse(candidate):
        raise UpdateError('程序根目录不能是链接。')
    if target.exists():
        try:
            if platform == 'macos-arm64':
                metadata = plistlib.loads((target / 'Contents/Info.plist').read_bytes())
                if metadata['CFBundleIdentifier'] != 'local.knowledge-distiller.app':
                    raise ValueError()
            else:
                metadata = json.loads((target / '_internal/windows-version.json').read_text())
                version_key(metadata['version'])
                if not (target / 'KnowledgeDistiller.exe').is_file():
                    raise ValueError()
        except (OSError, ValueError, KeyError) as error:
            raise UpdateError('所选目录不是已安装的知识蒸馏器，请选择新的安装目录。') from error
    if candidate.resolve().is_relative_to(target.resolve()):
        raise UpdateError('安装候选不能位于被替换程序内。')
    if identity(candidate, platform) != target_identity:
        raise UpdateError('安装候选已经变化。')
    root.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    updates = root / 'updates'
    updates.mkdir(exist_ok=True)
    journal = updates / 'component-install-journal.json'
    stage = target.with_name(target.name + '.component-stage')
    previous = target.with_name(target.name + '.component-previous')
    backup = updates / 'component-before-install.sqlite3'
    handshake = updates / 'component-startup-handshake'
    database = root / 'knowledge.sqlite3'
    if any(path.exists() or path.is_symlink() for path in (journal, stage, previous, backup)):
        raise UpdateError('上次组件安装仍待恢复，请先处理恢复记录。')
    process, accepted, swapped = None, False, False
    had_target, had_database = target.exists(), database.exists()
    state = {'target': str(target.resolve()), 'platform': platform, 'version': version,
             'target_identity': target_identity, 'had_target': had_target,
             'had_database': had_database, 'phase': 'preparing'}
    with acquire(root / '.update.lock'):
        # Holding update.lock prevents a regular application launch between
        # releasing instance.lock and starting our explicitly paused candidate.
        try:
            instance = acquire(root / '.instance.lock')
        except BlockingIOError as error:
            raise UpdateError('请先正常退出知识蒸馏器，再继续安装；已下载内容会保留。') from error
        try:
            write_record(journal, state)
            shutil.copytree(candidate, stage, symlinks=True)
            if identity(stage, platform) != target_identity:
                raise UpdateError('同盘暂存程序校验失败。')
            if had_database:
                with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                os.chmod(backup, 0o600)
            state['phase'] = 'replacing'
            write_record(journal, state)
            if had_target:
                target.rename(previous)
            swapped = True
            stage.rename(target)
            state['phase'] = 'startup'
            write_record(journal, state)
            handshake.unlink(missing_ok=True)
            instance.close()
            process = launcher(target, root, platform, handshake)
            acceptance(process, root, version)
            # Journal acceptance precedes the visible handshake: after this
            # point recovery must never restore a database over possible work.
            state['phase'] = 'accepted'
            write_record(journal, state)
            accepted = True
            temporary = handshake.with_suffix('.tmp')
            temporary.write_text('accepted')
            temporary.replace(handshake)
            if previous.exists():
                shutil.rmtree(previous)
            backup.unlink(missing_ok=True)
            journal.unlink()
            return {'version': version, 'target_identity': target_identity, 'accepted': True}
        except BaseException:
            if accepted:
                raise  # Leave journal for cleanup/handshake recovery, never rollback.
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=60)
            if swapped:
                if target.exists():
                    shutil.rmtree(target)
                for suffix in ('-wal', '-shm'):
                    Path(str(database) + suffix).unlink(missing_ok=True)
                if had_database and backup.exists():
                    shutil.copy2(backup, database)
                elif not had_database:
                    database.unlink(missing_ok=True)
                if previous.exists():
                    previous.rename(target)
            if stage.exists():
                shutil.rmtree(stage)
            backup.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            raise
        finally:
            instance.close()
