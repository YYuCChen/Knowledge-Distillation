import json
from pathlib import Path
import sqlite3
import pytest

from knowledge_distiller.v1.component_install import install, recover, require_recovered
from knowledge_distiller.v1.program_tree import identity
from knowledge_distiller.v1.windows_platform import filesystem_path
from knowledge_distiller.v1.file_lock import acquire
from knowledge_distiller.v1.updates import UpdateError


def program(path, version):
    (path / '_internal').mkdir(parents=True)
    (path / 'KnowledgeDistiller.exe').write_bytes(('business-' + version).encode())
    (path / '_internal/windows-version.json').write_text(json.dumps({'version': version}))
    return path


class Process:
    def poll(self): return 0


@pytest.mark.parametrize('failure', [False, True])
def test_install_acceptance_or_rollback_preserves_real_sqlite(tmp_path, failure):
    target = program(tmp_path / 'installed', '1')
    candidate = program(tmp_path / 'candidate', '2')
    root = tmp_path / 'data'
    root.mkdir()
    database = root / 'knowledge.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE facts(text TEXT)')
        connection.execute("INSERT INTO facts VALUES ('original')")
    def launcher(target, root, platform, handshake):
        # Simulate a paused candidate schema migration, never user work.
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE facts SET text='startup migration'")
        return Process()
    def acceptance(*args):
        if failure: raise UpdateError('injected startup failure')
    kwargs = dict(platform='windows-x86_64', version='2',
        target_identity=identity(candidate, 'windows-x86_64'), launcher=launcher, acceptance=acceptance)
    if failure:
        with pytest.raises(UpdateError, match='startup failure'):
            install(candidate, target, root, **kwargs)
    else:
        assert install(candidate, target, root, **kwargs)['accepted']
    assert (target / 'KnowledgeDistiller.exe').read_bytes() == (b'business-1' if failure else b'business-2')
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT text FROM facts').fetchone()[0] == ('original' if failure else 'startup migration')
    assert not (root / 'updates/component-install-journal.json').exists()


def test_running_application_is_never_stopped_by_installer(tmp_path):
    target = program(tmp_path / 'installed', '1')
    candidate = program(tmp_path / 'candidate', '2')
    root = tmp_path / 'data'
    root.mkdir()
    with acquire(root / '.instance.lock'):
        with pytest.raises(UpdateError, match='正常退出'):
            install(candidate, target, root, platform='windows-x86_64', version='2',
                    target_identity=identity(candidate, 'windows-x86_64'))
    assert (target / 'KnowledgeDistiller.exe').read_bytes() == b'business-1'


def test_crash_recovery_restores_database_before_old_launch_path(tmp_path, monkeypatch):
    target = program(tmp_path / 'installed', '2')
    previous = program(tmp_path / 'installed.component-previous', '1')
    root = tmp_path / 'data'
    updates = root / 'updates'
    updates.mkdir(parents=True)
    database = root / 'knowledge.sqlite3'
    backup = updates / 'component-before-install.sqlite3'
    for path, text in ((database, 'new schema'), (backup, 'original')):
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE facts(text TEXT)')
            connection.execute('INSERT INTO facts VALUES (?)', (text,))
        connection.close()
    (updates / 'component-install-journal.json').write_text(json.dumps({
        'target': str(target.resolve()), 'platform': 'windows-x86_64', 'phase': 'startup',
        'target_identity': identity(target, 'windows-x86_64'), 'had_target': True, 'had_database': True}))
    with pytest.raises(UpdateError, match='中断'):
        require_recovered(root)
    rename = Path.rename
    def checked_rename(path, destination):
        if path == filesystem_path(previous):
            with sqlite3.connect(database) as connection:
                assert connection.execute('SELECT text FROM facts').fetchone()[0] == 'original'
        return rename(path, destination)
    monkeypatch.setattr(Path, 'rename', checked_rename)
    assert recover(target, root)['recovered']
    assert (target / 'KnowledgeDistiller.exe').read_bytes() == b'business-1'


def test_startup_rejects_unavailable_document_component(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from knowledge_distiller.v1 import component_install as module
    (tmp_path / '.desktop-instance.json').write_text(json.dumps({'pid': 321, 'port': 45678}))
    process = SimpleNamespace(pid=321, poll=lambda: None)
    monkeypatch.setattr(module.httpx, 'get', lambda *a, **k: SimpleNamespace(json=lambda: {
        'version': '2', 'phase': 'installing', 'document_component': {'state': 'unavailable'}}))
    with pytest.raises(module.UpdateError, match='文档组件启动检查'):
        module.accept_startup(process, tmp_path, '2')


def test_windows_transient_handles_are_waited_through_every_program_rename(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import windows_update_installer as legacy
    target = program(tmp_path / 'installed', '1')
    candidate = program(tmp_path / 'candidate', '2')
    original_identity = identity(target, 'windows-x86_64')
    rename = Path.rename
    attempts, pauses = {}, []
    def delayed(source, destination):
        key = (source.name, destination.name)
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] == 1:
            raise PermissionError('fixture delayed Windows handle release')
        return rename(source, destination)
    monkeypatch.setattr(Path, 'rename', delayed)
    monkeypatch.setattr(legacy.time, 'sleep', pauses.append)
    def reject(*args):raise UpdateError('fixture startup refusal')
    with pytest.raises(UpdateError, match='fixture startup refusal'):
        install(candidate, target, tmp_path / 'data', platform='windows-x86_64',
                version='2', target_identity=identity(candidate, 'windows-x86_64'),
                launcher=lambda *args:Process(), acceptance=reject)
    assert len(attempts) == 3 and all(count == 2 for count in attempts.values())
    assert pauses == [.2, .2, .2]
    assert identity(target, 'windows-x86_64') == original_identity
    assert not (tmp_path / 'data/updates/component-install-journal.json').exists()


def test_windows_persistent_rename_failure_keeps_recoverable_journal(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import windows_update_installer as legacy
    target = program(tmp_path / 'installed', '1')
    candidate = program(tmp_path / 'candidate', '2')
    root = tmp_path / 'data'
    original_identity = identity(target, 'windows-x86_64')
    rename = Path.rename
    clock = [0.0]
    def unavailable(source, destination):
        if source.name.endswith('.component-previous'):
            raise PermissionError('fixture persistent handle')
        return rename(source, destination)
    monkeypatch.setattr(Path, 'rename', unavailable)
    monkeypatch.setattr(legacy.time, 'monotonic', lambda:clock[0])
    monkeypatch.setattr(legacy.time, 'sleep', lambda seconds:clock.__setitem__(0, clock[0] + seconds))
    def reject(*args):raise UpdateError('fixture startup refusal')
    with pytest.raises(PermissionError, match='fixture persistent handle'):
        install(candidate, target, root, platform='windows-x86_64', version='2',
                target_identity=identity(candidate, 'windows-x86_64'),
                launcher=lambda *args:Process(), acceptance=reject)
    assert 15 <= clock[0] < 16
    assert not target.exists()
    journal = root / 'updates/component-install-journal.json'
    assert json.loads(journal.read_text())['phase'] == 'startup'
    assert identity(target.with_name('installed.component-previous'), 'windows-x86_64') == original_identity
    monkeypatch.setattr(Path, 'rename', rename)
    assert recover(target, root)['recovered']
    assert identity(target, 'windows-x86_64') == original_identity
    assert not journal.exists()
