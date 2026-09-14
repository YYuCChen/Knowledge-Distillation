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
from .windows_platform import filesystem_path
from .updates import UpdateError, validate_install_paths, version_key


def _rename_program(source, target, platform):
    if platform == 'windows-x86_64':
        # Process exit can precede release of Windows DLL/directory handles.
        # Reuse the bounded wait already required by the legacy updater.
        from .windows_update_installer import rename_ready
        rename_ready(filesystem_path(source), filesystem_path(target))
    else:
        source.rename(target)


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
            state = json.loads((root / '.desktop-instance.json').read_text(encoding='utf-8'))
            if state['pid'] == process.pid and type(state['port']) is int:
                url = 'http://127.0.0.1:' + str(state['port'])
                status = httpx.get(url + '/settings/updates/status', timeout=2).json()
                documents = status.get('document_component', {'state': 'ready'})
                if documents.get('state') == 'unavailable':
                    raise UpdateError('新版本文档组件启动检查未通过。')
                if (status['version'] == version and status['phase'] == 'installing'
                        and documents.get('state') == 'ready'
                        and httpx.get(url + '/', timeout=2).status_code == 200):
                    return
        except UpdateError:
            raise
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            pass
        time.sleep(.25)
    raise UpdateError('新版本启动验收未通过。')


def confirm_activation(root, state):
    """Observe the accepted instance leaving paused startup, not merely HTTP 200."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            instance = json.loads((root / '.desktop-instance.json').read_text(encoding='utf-8'))
            if state.get('pid') is not None and instance['pid'] != state['pid']:
                raise UpdateError('启动检查实例已变化。')
            if type(instance['port']) is not int or not 0 < instance['port'] < 65536:
                raise UpdateError('启动检查端口无效。')
            status = httpx.get('http://127.0.0.1:' + str(instance['port']) + '/settings/updates/status', timeout=2).json()
            if (status.get('version') == state.get('version') and status.get('phase') != 'installing'
                    and status.get('document_component', {'state':'ready'}).get('state') == 'ready'):
                return
        except (OSError, ValueError, KeyError, httpx.HTTPError): pass
        time.sleep(.2)
    raise UpdateError('新程序已接受，尚未确认启动放行；请恢复安装。')


def _outcome(state):
    return {'accepted': True, 'target': state['target'], 'platform': state['platform'],
            'target_identity': state['target_identity'], 'version': state.get('version'),
            'activation': {'status': 'pending'}, 'finalization': {'status': 'pending'},
            'cleanup': {'status': 'pending', 'reason': 'no_process_capability'},
            'shortcut': {'status': 'not_run'}, 'warnings': []}


def _finish_accepted(state, root, activation=confirm_activation):
    """Only called with proven durable accepted state; never restores data."""
    result = _outcome(state)
    target = Path(state['target'])
    updates = root / 'updates'
    try:
        if identity(target, state['platform']) != state['target_identity']:
            raise UpdateError('已接受程序的内容已变化，请通过安装器恢复。')
        handshake = updates / 'component-startup-handshake'
        temporary = handshake.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write('accepted'); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(handshake)
        activation(root, state)
        result['activation'] = {'status': 'ready'}
    except Exception as error:
        result['warnings'].append('安装已接受，启动放行待处理：' + str(error))
        return result
    try:
        previous = target.with_name(target.name + '.component-previous')
        if previous.exists(): shutil.rmtree(filesystem_path(previous))
        (updates / 'component-before-install.sqlite3').unlink(missing_ok=True)
        (updates / 'component-install-journal.json').unlink()
        result['finalization'] = {'status': 'complete'}
    except Exception as error:
        result['warnings'].append('安装已完成，部分事务收尾待处理：' + str(error))
    return result


def finalize_install(outcome, *, capability=None, shortcut=None):
    """Independent ancillary actions; diagnostics never revoke acceptance."""
    from .component_attempt import cleanup_attempt
    if outcome.get('accepted') is not True:
        return outcome
    try:
        if identity(Path(outcome['target']), outcome['platform']) != outcome['target_identity']:
            raise UpdateError('已接受目标身份已变化，保留本次组装副本。')
        outcome['cleanup'] = cleanup_attempt(capability, outcome)
    except Exception as error:
        outcome['cleanup'] = {'status': 'pending', 'reason': str(error)}
    if outcome['cleanup']['status'] == 'pending':
        outcome['warnings'].append('本次组装副本保留：' + outcome['cleanup'].get('reason', '待处理'))
    if shortcut is not None and outcome['activation']['status'] == 'ready':
        try: outcome['shortcut'] = shortcut()
        except Exception as error:
            outcome['shortcut'] = {'status': 'pending', 'reason': str(error)}
        if outcome['shortcut']['status'] == 'pending':
            outcome['warnings'].append('桌面入口待处理：' + outcome['shortcut'].get('reason', ''))
    return outcome


def require_recovered(root):
    journal = Path(root) / 'updates/component-install-journal.json'
    if journal.exists():
        state = json.loads(journal.read_text(encoding='utf-8'))
        if state.get('phase') != 'accepted':
            raise UpdateError('组件安装被中断，请重新打开安装器完成恢复。知识数据尚未启动处理。')


def recover(target, data_root, *, activation=confirm_activation):
    target, root = Path(target), Path(data_root)
    validate_install_paths(root, target)
    updates = root / 'updates'
    journal = updates / 'component-install-journal.json'
    with acquire(root / '.update.lock'):
        state = json.loads(journal.read_text(encoding='utf-8'))
        if state['target'] != str(target.resolve()):
            raise UpdateError('安装恢复目标与原记录不一致。')
        previous = target.with_name(target.name + '.component-previous')
        stage = target.with_name(target.name + '.component-stage')
        backup = updates / 'component-before-install.sqlite3'
        if state['phase'] == 'accepted':
            result = _finish_accepted(state, root, activation)
            result['recovered'] = result['activation']['status'] == 'ready'
            return result
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
                        shutil.rmtree(filesystem_path(target))
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
                        _rename_program(previous, target, state['platform'])
                if stage.exists():
                    shutil.rmtree(filesystem_path(stage))
        backup.unlink(missing_ok=True)
        journal.unlink()
    return {'recovered': True, 'accepted': state['phase'] == 'accepted'}


def install(candidate, target, data_root, *, platform, version, target_identity,
            launcher=launch, acceptance=accept_startup, activation=confirm_activation):
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
                metadata = json.loads((target / '_internal/windows-version.json').read_text(encoding='utf-8'))
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
            shutil.copytree(filesystem_path(candidate), filesystem_path(stage), symlinks=True)
            if identity(stage, platform) != target_identity:
                raise UpdateError('同盘暂存程序校验失败。')
            if had_database:
                with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                os.chmod(backup, 0o600)
            state['phase'] = 'replacing'
            write_record(journal, state)
            if had_target:
                _rename_program(target, previous, platform)
            swapped = True
            _rename_program(stage, target, platform)
            state['phase'] = 'startup'
            write_record(journal, state)
            handshake.unlink(missing_ok=True)
            instance.close()
            process = launcher(target, root, platform, handshake)
            state['pid'] = getattr(process, 'pid', None)
            acceptance(process, root, version)
            # Journal acceptance precedes the visible handshake: after this
            # point recovery must never restore a database over possible work.
            state['phase'] = 'accepted'
            write_record(journal, state)
            accepted = True
            return _finish_accepted(state, root, activation)
        except Exception:
            # write_record may have persisted accepted and then raised (fsync,
            # injected I/O, etc.). Read the journal before choosing rollback.
            if state['phase'] == 'accepted':
                try:
                    persisted = json.loads(journal.read_text(encoding='utf-8'))
                    if (persisted.get('target') != state['target'] or
                            persisted.get('target_identity') != target_identity):
                        raise ValueError('journal identity mismatch')
                except Exception as error:
                    raise UpdateError('安装接受状态无法核实，已保留新程序和恢复材料；请恢复安装。') from error
                if persisted.get('phase') == 'accepted':
                    return _finish_accepted(persisted, root, activation)
            if accepted:
                raise
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=60)
            if swapped:
                if target.exists():
                    shutil.rmtree(filesystem_path(target))
                for suffix in ('-wal', '-shm'):
                    Path(str(database) + suffix).unlink(missing_ok=True)
                if had_database and backup.exists():
                    shutil.copy2(backup, database)
                elif not had_database:
                    database.unlink(missing_ok=True)
                if previous.exists():
                    _rename_program(previous, target, state['platform'])
            if stage.exists():
                shutil.rmtree(filesystem_path(stage))
            backup.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            raise
        finally:
            instance.close()
