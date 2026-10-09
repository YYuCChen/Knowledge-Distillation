"""Real Store/SQLite/Mac rollback on SQL-built disposable 21/22/23/25/26 roots.

No App, native helper process, signing executable, HTTP server or download.
Only external boundaries are fake; the existing installer function performs
its real backup, lock, filesystem, acceptance and restoration operations.
"""
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
import pytest

from knowledge_distiller.v1 import database as database_module
from knowledge_distiller.v1 import data_upgrade_probe as approved_probe
from knowledge_distiller.v1 import update_installer as installer
from knowledge_distiller.v1.store import Store


FIXTURES = Path(__file__).with_name('fixtures')
VERSIONS = (21, 22, 23, 25, 26)
BODY = b'\x00\xff\r\nA'
TEXT = 'synthetic-only 原件\r\ne\u0301，字节不重写'
PARENT_PID = 987654320
CANDIDATE_PID = 987654321
FAKE_PORT = 12345  # Never bound; used only to validate fake HTTP arguments.


def _private(path):
    assert path.resolve().is_relative_to(Path('/tmp').resolve())
    assert path.parent.stat().st_uid == os.getuid()
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    return closing(db)


def _snapshot(path, selected=None):
    """Independent oracle for every prior object/column/value, including wiki."""
    with _readonly(path) as db:
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        objects = dict((row[1], (row[0], row[2], row[3])) for row in db.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
        columns = selected if selected is not None else {
            name: tuple(db.execute(f'PRAGMA table_xinfo({_quote(name)})'))
            for name, value in objects.items() if value[0] == 'table'}
        rows, indices, typed_rows, foreign_keys = {}, {}, {}, {}
        for name, metadata in columns.items():
            primary = [row[1] for row in sorted(metadata, key=lambda row: row[5]) if row[5]]
            order = ','.join(_quote(key) for key in primary) if primary else 'rowid'
            projection = ','.join(_quote(row[1]) for row in metadata)
            if not primary:
                projection = 'rowid,' + projection
            rows[name] = tuple(db.execute(f'SELECT {projection} FROM {_quote(name)} ORDER BY {order}'))
            values = ','.join(_quote(row[1]) for row in metadata)
            types = ','.join(f'typeof({_quote(row[1])})' for row in metadata)
            typed_rows[name] = tuple(db.execute(
                f'SELECT rowid,{values},{types} FROM {_quote(name)} ORDER BY rowid'))
            foreign_keys[name] = tuple(db.execute(f'PRAGMA foreign_key_list({_quote(name)})'))
            for index in db.execute(f'PRAGMA index_list({_quote(name)})'):
                indices[index[1]] = (index[2], index[3], index[4], tuple(
                    db.execute(f'PRAGMA index_xinfo({_quote(index[1])})')))
        return {'version': db.execute('PRAGMA user_version').fetchone()[0],
                'objects': objects, 'columns': columns, 'rows': rows, 'indices': indices,
                'typed_rows': typed_rows, 'foreign_keys': foreign_keys}


def _vault_tree(vault):
    return {path.relative_to(vault).as_posix():
            (path.stat().st_mode & 0o777, None if path.is_dir() else path.read_bytes())
            for path in sorted(vault.rglob('*'))}


def _sql_fixture(root, version):
    data = _private(root / 'data')
    vault = _private(data / 'synthetic-vault')
    raw = _private(vault / 'raw')
    _private(vault / '空目录')
    for name, content in (('原件.md', TEXT.encode()), ('附件.bin', BODY)):
        path = raw / name
        path.write_bytes(content)
        path.chmod(0o600)
    database = data / 'knowledge.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.executescript((FIXTURES / 'wiki-schema21.sql').read_text(encoding='utf-8'))
        if version >= 22:
            db.executescript((FIXTURES / 'wiki-schema22.sql').read_text(encoding='utf-8'))
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript((FIXTURES / 'upgrade-probe-seed.sql').read_text(encoding='utf-8'))
        db.execute("INSERT INTO confirmation_decisions VALUES(51,'old-revision','manual','继续','waiting_user')")
        db.execute('''INSERT INTO group_decisions VALUES
            (51,'old-request','old-group','old-revision','old-selection','old-payload',
             '{ "state" : "waiting_user" }','{ "human" : "继续" }',?)''', (TEXT,))
        db.execute('''INSERT INTO manual_cards(scope_kind,scope_id,item_id,review_round_id,group_id,
            lifecycle,ordering_basis,ordering_reason,entered_at,mapping_json)
            VALUES ('items','independent',51,'old-round','old-group','active','observed',
                    'explicit synthetic human',?,'{ "human" : "继续" }')''', (TEXT,))
        db.execute('INSERT INTO settings VALUES(?,?)', ('vault_path', str(vault)))
        db.execute('INSERT INTO settings VALUES(?,?)', ('synthetic-byte-sentinel', TEXT))
        for ordinal, kind, subject, identity in ((1, 'material', 41, '第三方'),
                                                (2, 'capture', 81, '本人附言')):
            original = raw / f'{ordinal}.md'
            original.write_bytes(TEXT.encode())
            original.chmod(0o600)
            db.execute('''INSERT INTO raw_records(raw_id,subject_kind,subject_id,identity,
                relative_path,content,content_sha256,attachments_json,origin,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (f'R-20261008-{ordinal:04}', kind, subject, identity, f'raw/{ordinal}.md', TEXT,
                 hashlib.sha256(TEXT.encode()).hexdigest(), '[ ]', 'app', '2099-01-01T00:00:00Z'))
        if version >= 22:
            db.execute('''INSERT INTO wiki_tasks(task_id,vault_path,vault_key,request_kind,
                trigger_source,backend,model,effort,kit_version,kit_manifest_sha256,
                boundary_sha256,state,raw_count,batch_count,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('1' * 32, str(vault), 'a' * 64, 'all', 'cli', 'codex_cli', 'synthetic-model',
                 'medium', '3.0.0', 'c' * 64, 'b' * 64, 'queued', 1, 1,
                 '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z'))
            db.execute('INSERT INTO wiki_task_batches VALUES(?,?,?,?,NULL)', ('1' * 32, 1, 'queued', 1))
            db.execute('INSERT INTO wiki_task_raw VALUES(?,?,?,?,?,?,?,?)',
                       ('1' * 32, 1, 1, 'R-20261008-0001', '第三方', 'raw/1.md',
                        len(TEXT.encode()), hashlib.sha256(TEXT.encode()).hexdigest()))
        db.commit()
        if version >= 23:
            db.executescript((FIXTURES / 'upgrade-probe-schema23.sql').read_text(encoding='utf-8'))
            db.execute('INSERT INTO wiki_observations VALUES(?,?,?,?,?,?,NULL)',
                       ('a' * 64, str(vault), '1' * 32, 2, 3, '2099-01-01T00:00:00Z'))
            db.commit()
        if version in (25, 26):
            db.executescript((FIXTURES / 'upgrade-probe-schema25.sql').read_text(encoding='utf-8'))
        assert db.execute('PRAGMA user_version').fetchone()[0] == (25 if version == 26 else version)
        # No expires-at cleanup or retained-file reconstruction in this fixture.
        assert db.execute('SELECT input_kind,retain_until FROM submitted_sources').fetchall() == [('direct_text', None)]
    database.chmod(0o600)
    if version in (25, 26):
        from .test_data_upgrade_probe_versions import _seed_prior25
        _seed_prior25(database, vault)
        if version == 26:
            from .test_data_upgrade_probe_versions import _upgrade_to26
            _upgrade_to26(database)
    assert database.stat().st_nlink == 1
    assert all(not Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm'))
    return data, database, vault


def _assert_upgraded(state):
    current = _snapshot(state.database, state.before['columns'])
    assert current['version'] == 27
    assert current['rows'] == state.before['rows']
    assert current['typed_rows'] == state.before['typed_rows']
    assert current['foreign_keys'] == state.before['foreign_keys']
    assert _vault_tree(state.vault) == state.vault_before
    # Reuse the accepted fixed DDL/default/index contract (independently checked
    # against 45 primary AST expressions and the parent transform), alongside
    # the independent typed-row/FK oracle.
    # This neither runs the probe nor constructs a target DB as its own oracle.
    with _readonly(state.database) as db:
        approved_probe._check_after(db, state.prior)
        assert db.execute('SELECT content FROM source_media').fetchone()[0] == BODY
        assert db.execute('SELECT legacy_material_id FROM media_lifecycle').fetchone()[0] == 40


def _assert_old_readable(state):
    # Synthetic old-version compatibility contract, NOT a real old executable.
    assert _snapshot(state.database) == state.before
    assert _vault_tree(state.vault) == state.vault_before
    assert state.database.read_bytes() == state.backup_bytes
    assert (state.target / 'version').read_text() == 'old'


def _case(tmp_path, monkeypatch, version):
    root = _private(tmp_path / 'isolated-install')
    data, database, vault = _sql_fixture(root, version)
    target = _private(root / 'Knowledge.app')
    _private(target / 'Contents')
    _private(target / 'Contents/MacOS')
    (target / 'version').write_text('old', encoding='utf-8')
    (target / 'Contents/MacOS/KnowledgeDistiller').write_bytes(b'synthetic-never-executed')
    updates = _private(data / 'updates')
    # Temporary signing key only; no signing configuration/secure store access.
    key = ECC.generate(curve='Ed25519')
    def sign(content):
        return base64.b64encode(eddsa.new(key, 'rfc8032').sign(content)).decode()
    feed = ('<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">'
            '<channel><item><sparkle:version>2</sparkle:version>'
            f'<enclosure url="synthetic.zip" length="7" sparkle:edSignature="{sign(b"archive")}" />'
            '</item></channel></rss>\n').encode()
    (updates / 'appcast.xml').write_bytes(feed +
        f'<!-- sparkle-signatures:\nedSignature: {sign(feed)}\nlength: {len(feed)}\n-->\n'.encode())
    plan = updates / 'install-plan.json'
    (data / '.desktop-instance.json').write_text(json.dumps({'pid': PARENT_PID}))
    plan.write_text(json.dumps({
        'data_root': str(data), 'version': '2', 'parent_pid': PARENT_PID, 'no_open': True,
        'info': {'version': '1', 'display_version': '1', 'bundle': str(target),
                 'public_key': base64.b64encode(key.public_key().export_key(format='raw')).decode(),
                 'feed_url': 'https://example.invalid/never-requested.xml'},
    }))
    state = SimpleNamespace(root=root, data=data, database=database, vault=vault,
        target=target, updates=updates, plan=plan, version=version, events=[],
        previous=target.with_name('.knowledge-distiller-update-backup.app'),
        backup=updates / 'before-install.sqlite3', before=_snapshot(database),
        vault_before=_vault_tree(vault), backup_bytes=None, startup_failure=False,
        stopped=False, candidate_started=False, candidate_alive=False)
    with _readonly(database) as db:
        state.prior = approved_probe._freeze(db, version)

    class Server:
        server_port = FAKE_PORT
        def __init__(self, address, handler):
            assert address == ('127.0.0.1', 0)
        def serve_forever(self):
            pass  # Existing helper thread exits without a socket or request.
        def shutdown(self):
            pass
        def server_close(self):
            pass

    def download(updater, asset):
        assert updater.root == updates
        assert asset['name'] == 'synthetic.zip'
        state.events.append('download')
        path = updates / asset['name']
        path.write_bytes(b'archive')
        return path

    def kill(pid, signal):
        assert pid == PARENT_PID
        if signal == 0:
            assert state.stopped
            raise ProcessLookupError()
        assert signal == installer.signal.SIGTERM
        state.stopped = True
        state.events.append('stop-old')

    def external_run(command, **kwargs):
        name = Path(command[0]).name
        if name == 'ditto':
            assert command == ['ditto', str(target), str(state.previous)]
            assert kwargs == {'check': True}
            shutil.copytree(target, state.previous, dirs_exist_ok=True)
            state.events.append('copy-old-bundle')
        elif name == 'codesign':
            assert command[:4] == ['codesign', '--verify', '--deep', '--strict']
            assert len(command) == 5 and Path(command[4]) in (target, state.previous)
            assert kwargs == {'check': True}
        elif name == 'update-cli':
            assert Path(command[0]) == target / 'Contents/Helpers/Updater.app/Contents/MacOS/update-cli'
            assert command[1:4] == [str(target), '--application', str(target)]
            assert len(command) == 11
            assert command[4] == '--feed-url'
            assert command[5].startswith(f'http://127.0.0.1:{FAKE_PORT}/')
            assert command[5].endswith('/appcast.xml')
            assert command[6:] == ['--check-immediately', '--interactive', '--user-agent-name', 'KnowledgeDistiller', '--verbose']
            assert state.stopped and state.backup.is_file()
            (target / 'version').write_text('new')
            state.events.append('swap-candidate')
        elif name == 'KnowledgeDistiller':
            assert command == [str(target / 'Contents/MacOS/KnowledgeDistiller'),
                               '--data-dir', str(data), '--check-runtime', str(updates / 'candidate-runtime.json')]
            assert kwargs['check'] is True and kwargs['timeout'] == 180
            state.events.append('fake-runtime-only')
        else:
            pytest.fail(f'unexpected process command: {name}')
        return SimpleNamespace(returncode=0)

    class Process:
        pid = CANDIDATE_PID
        def poll(self):
            return None if state.candidate_alive else 0
        def terminate(self):
            state.events.append('terminate-candidate')
            state.candidate_alive = False
        def wait(self, **kwargs):
            assert kwargs == {'timeout': 60}
            assert not state.candidate_alive
            state.events.append('wait-candidate')
            return 0

    def popen(arguments, **kwargs):
        assert kwargs == {'start_new_session': True}
        executable = str(target / 'Contents/MacOS/KnowledgeDistiller')
        if '--update-handshake' not in arguments:
            assert arguments == [executable, '--data-dir', str(data), '--no-open']
            assert not state.candidate_alive
            _assert_old_readable(state)
            state.events.append('old-readonly-reopen')
            return SimpleNamespace(pid=PARENT_PID)
        assert arguments == [executable, '--data-dir', str(data),
                             '--update-handshake', str(updates / 'startup-handshake'), '--no-open']
        assert state.stopped and not state.candidate_started
        assert state.backup.stat().st_mode & 0o777 == 0o600
        assert _snapshot(state.backup) == state.before
        state.backup_bytes = state.backup.read_bytes()
        state.events.append('verified-real-backup')
        state.candidate_started = True
        try:
            Store(database, runtime_root=root / 'runtime').initialize()
        except RuntimeError as error:
            if str(error) == 'synthetic-migration-abort':
                assert _snapshot(database) == state.before
                state.events.append('verified-migration-rollback')
            raise
        _assert_upgraded(state)
        state.committed_bytes = database.read_bytes()
        state.events.append('store-committed-27')
        state.candidate_alive = True
        (data / '.desktop-instance.json').write_text(json.dumps({'pid': CANDIDATE_PID, 'port': FAKE_PORT}))
        return Process()

    def get(url, **kwargs):
        assert kwargs == {'timeout': 2}
        assert url in (f'http://127.0.0.1:{FAKE_PORT}/settings/updates/status',
                       f'http://127.0.0.1:{FAKE_PORT}/')
        assert state.candidate_alive
        if state.startup_failure:
            state.events.append('synthetic-startup-failure')
            raise RuntimeError('synthetic-startup-abort')
        return SimpleNamespace(status_code=200, json=lambda: {'version': '2', 'phase': 'installing'})

    original_copy2 = shutil.copy2
    def copy2(source, destination, *args, **kwargs):
        if Path(source) == state.backup:
            assert not state.candidate_alive
            destination = Path(destination)
            assert destination.parent == data and destination != database
            assert destination.name.startswith('.kd-update-restore-')
            assert destination.stat().st_mode & 0o777 == 0o600
            assert destination.stat().st_dev == database.stat().st_dev
            assert (target / 'version').read_text() == 'new'
            assert (state.previous / 'version').read_text() == 'old'
            state.events.append('copy-backup-to-stage')
        return original_copy2(source, destination, *args, **kwargs)

    original_replace, original_rename, original_fsync = os.replace, Path.rename, os.fsync
    def replace(source, destination, *args, **kwargs):
        if Path(destination) == database:
            assert not state.candidate_alive
            assert Path(source).parent == data and Path(source).name.startswith('.kd-update-restore-')
            assert _snapshot(Path(source)) == state.before
            assert Path(source).read_bytes() == state.backup_bytes
            assert (target / 'version').read_text() == 'new'
            assert (state.previous / 'version').read_text() == 'old'
            result = original_replace(source, destination, *args, **kwargs)
            assert database.read_bytes() == state.backup_bytes
            state.events.append('database-backup-published')
            return result
        return original_replace(source, destination, *args, **kwargs)

    def fsync(descriptor):
        result = original_fsync(descriptor)
        if os.fstat(descriptor).st_ino == data.stat().st_ino and os.fstat(descriptor).st_dev == data.stat().st_dev:
            assert 'database-backup-published' in state.events
            state.events.append('database-parent-synced')
        else:
            assert 'database-backup-published' not in state.events
            state.events.append('restore-stage-synced')
        return result

    def rename(path, destination):
        if path == state.previous:
            assert Path(destination) == target
            assert 'database-parent-synced' in state.events
            assert _snapshot(database) == state.before
            assert database.read_bytes() == state.backup_bytes
            state.events.append('old-bundle-restored')
        return original_rename(path, destination)

    monkeypatch.setattr(installer, 'ThreadingHTTPServer', Server)
    monkeypatch.setattr(installer.Updates, 'download', download)
    monkeypatch.setattr(installer.os, 'kill', kill)
    monkeypatch.setattr(installer.subprocess, 'run', external_run)
    monkeypatch.setattr(installer.subprocess, 'Popen', popen)
    monkeypatch.setattr(installer.httpx, 'get', get)
    monkeypatch.setattr(installer.shutil, 'copy2', copy2)
    monkeypatch.setattr(installer.os, 'replace', replace)
    monkeypatch.setattr(installer.os, 'fsync', fsync)
    monkeypatch.setattr(Path, 'rename', rename)
    return state


def _assert_result(state, exit_code):
    assert json.loads((state.updates / 'install-result.json').read_text()) == {
        'version': '2', 'exit_code': exit_code}
    assert _vault_tree(state.vault) == state.vault_before


@pytest.mark.parametrize('version', VERSIONS)
def test_real_store_and_backup_are_accepted_without_changing_originals(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    assert installer.run(state.plan) == 0
    _assert_upgraded(state)
    _assert_result(state, 0)
    assert (state.target / 'version').read_text() == 'new'
    assert (state.updates / 'startup-handshake').read_text() == 'accepted'
    assert not state.previous.exists() and not state.backup.exists()
    assert not (state.updates / 'synthetic.zip').exists()
    assert 'old-readonly-reopen' not in state.events
    assert 'terminate-candidate' not in state.events
    assert state.events.index('verified-real-backup') < state.events.index('store-committed-27')


@pytest.mark.parametrize('version', VERSIONS)
def test_real_migration_transaction_failure_restores_backup_and_old_schema(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    original = database_module.migrate_v27
    def abort_after_ddl(db):
        original(db)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='ingestion_events'").fetchone()
        raise RuntimeError('synthetic-migration-abort')
    monkeypatch.setattr(database_module, 'migrate_v27', abort_after_ddl)
    assert installer.run(state.plan) == 1
    _assert_old_readable(state)
    _assert_result(state, 1)
    assert 'verified-migration-rollback' in state.events
    assert 'store-committed-27' not in state.events
    assert state.events[-1] == 'old-readonly-reopen'
    assert not (state.updates / 'startup-handshake').exists()
    assert not state.previous.exists()
    # Mac helper retains this real DB snapshot on pre-acceptance failure.
    assert state.backup.read_bytes() == state.backup_bytes


@pytest.mark.parametrize('version', VERSIONS)
def test_committed_store_then_startup_failure_restores_prior_database(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    state.startup_failure = True
    assert installer.run(state.plan) == 1
    _assert_old_readable(state)
    _assert_result(state, 1)
    assert not (state.updates / 'startup-handshake').exists()
    assert not state.previous.exists()
    assert state.backup.read_bytes() == state.backup_bytes
    events = state.events
    assert events.index('store-committed-27') < events.index('synthetic-startup-failure')
    assert events.index('terminate-candidate') < events.index('wait-candidate') < events.index('copy-backup-to-stage')
    assert events.index('copy-backup-to-stage') < events.index('restore-stage-synced') < events.index('database-backup-published')
    assert events.index('database-backup-published') < events.index('database-parent-synced') < events.index('old-bundle-restored')
    assert events.index('old-bundle-restored') < events.index('old-readonly-reopen')


@pytest.mark.parametrize('version', VERSIONS)
def test_restore_copy_failure_is_not_reported_as_old_database_recovered(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    state.startup_failure = True
    original = shutil.copy2
    def refuse_restore(source, destination, *args, **kwargs):
        if Path(source) == state.backup:
            stage = Path(destination)
            assert stage.parent == state.data and stage != state.database
            assert stage.stat().st_mode & 0o777 == 0o600
            stage.write_bytes(b'partial-copy-failed')
            state.events.append('restore-copy-refused')
            raise PermissionError('synthetic-restore-copy-refused')
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(installer.shutil, 'copy2', refuse_restore)
    with pytest.raises(PermissionError, match='synthetic-restore-copy-refused'):
        installer.run(state.plan)
    _assert_result(state, 1)
    _assert_upgraded(state)  # DB is still 27; this is NOT a restored old schema.
    assert state.database.read_bytes() == state.committed_bytes
    assert (state.target / 'version').read_text() == 'new'
    assert (state.previous / 'version').read_text() == 'old'
    assert state.backup.read_bytes() == state.backup_bytes
    assert _snapshot(state.backup) == state.before
    assert 'old-readonly-reopen' not in state.events
    assert not (state.updates / 'startup-handshake').exists()
    assert not (state.updates / 'install-error.txt').exists()  # Exception precedes this write.
    assert state.events.index('wait-candidate') < state.events.index('restore-copy-refused')
    assert 'database-backup-published' not in state.events and 'old-bundle-restored' not in state.events
    assert list(state.data.glob('.kd-update-restore-*')) == []


def test_schema23_restore_replace_failure_preserves_both_bundles_and_backup(tmp_path, monkeypatch):
    state = _case(tmp_path, monkeypatch, 23)
    state.startup_failure = True
    original = installer.os.replace
    def refuse_database_replace(source, destination, *args, **kwargs):
        if Path(destination) == state.database:
            stage = Path(source)
            assert stage.parent == state.data and stage.name.startswith('.kd-update-restore-')
            assert _snapshot(stage) == state.before
            assert stage.read_bytes() == state.backup_bytes
            assert 'restore-stage-synced' in state.events
            state.events.append('restore-replace-refused')
            raise PermissionError('synthetic-restore-replace-refused')
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(installer.os, 'replace', refuse_database_replace)
    with pytest.raises(PermissionError, match='synthetic-restore-replace-refused'):
        installer.run(state.plan)
    _assert_result(state, 1)
    _assert_upgraded(state)
    assert state.database.read_bytes() == state.committed_bytes
    assert (state.target / 'version').read_text() == 'new'
    assert (state.previous / 'version').read_text() == 'old'
    assert state.backup.read_bytes() == state.backup_bytes
    assert _snapshot(state.backup) == state.before
    assert 'old-readonly-reopen' not in state.events and 'old-bundle-restored' not in state.events
    assert 'database-backup-published' not in state.events
    assert not (state.updates / 'startup-handshake').exists()
    assert list(state.data.glob('.kd-update-restore-*')) == []


@pytest.mark.parametrize('version', VERSIONS)
def test_accepted_cleanup_failure_preserves_new_schema_and_new_work(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    original_replace, original_rmtree, original_copy = Path.replace, shutil.rmtree, shutil.copy2
    def accepted_work(path, destination):
        result = original_replace(path, destination)
        if path == state.updates / 'startup-accept.tmp':
            assert Path(destination) == state.updates / 'startup-handshake'
            with database_module.connect(state.database) as db:
                db.execute('INSERT INTO settings VALUES(?,?)', ('synthetic-after-accept', TEXT))
            state.events.append('accepted-new-work')
        return result
    def refuse_cleanup(path, *args, **kwargs):
        if Path(path) == state.previous:
            raise PermissionError('synthetic-cleanup-refused')
        return original_rmtree(path, *args, **kwargs)
    def forbid_restore(source, destination, *args, **kwargs):
        if Path(source) == state.backup:
            pytest.fail('accepted data must never be restored from the old backup')
        return original_copy(source, destination, *args, **kwargs)
    monkeypatch.setattr(Path, 'replace', accepted_work)
    monkeypatch.setattr(installer.shutil, 'rmtree', refuse_cleanup)
    monkeypatch.setattr(installer.shutil, 'copy2', forbid_restore)
    assert installer.run(state.plan) == 0
    _assert_result(state, 0)
    assert (state.updates / 'startup-handshake').read_text() == 'accepted'
    assert (state.target / 'version').read_text() == 'new'
    assert (state.previous / 'version').read_text() == 'old'
    assert state.backup.read_bytes() == state.backup_bytes
    assert (state.updates / 'cleanup-error.txt').read_text() == 'synthetic-cleanup-refused'
    assert 'old-readonly-reopen' not in state.events and 'terminate-candidate' not in state.events
    assert 'accepted-new-work' in state.events
    with _readonly(state.database) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        assert db.execute('SELECT value FROM settings WHERE key=?', ('synthetic-after-accept',)).fetchone() == (TEXT,)
        approved_probe._check_after(db, state.prior)
    current = _snapshot(state.database, state.before['columns'])['rows']
    assert set(current['settings']) == set(state.before['rows']['settings']) | {('synthetic-after-accept', TEXT)}
    assert {key: value for key, value in current.items() if key != 'settings'} == {
        key: value for key, value in state.before['rows'].items() if key != 'settings'}
    final = _snapshot(state.database, state.before['columns'])
    assert final['foreign_keys'] == state.before['foreign_keys']
    assert {k: v for k, v in final['typed_rows'].items() if k != 'settings'} == {
        k: v for k, v in state.before['typed_rows'].items() if k != 'settings'}


@pytest.mark.parametrize('version', (21, 22, 23))
def test_actual_v25_postcheck_failure_restores_backup_before_old_bundle(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    actual = database_module._check_v25_preservation
    def interrupted(db, *args):
        actual(db, *args)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='distill_items_raw_terminal_proof'").fetchone()
        raise RuntimeError('synthetic-migration-abort')
    monkeypatch.setattr(database_module, '_check_v25_preservation', interrupted)
    assert installer.run(state.plan) == 1
    _assert_old_readable(state)
    _assert_result(state, 1)
    assert 'verified-migration-rollback' in state.events
    assert 'store-committed-27' not in state.events
    assert not (state.updates / 'startup-handshake').exists()
    assert state.backup.read_bytes() == state.backup_bytes
    assert not state.previous.exists()
    assert state.events.index('restore-stage-synced') < state.events.index('database-backup-published')
    assert state.events.index('database-backup-published') < state.events.index('database-parent-synced')
    assert state.events.index('database-parent-synced') < state.events.index('old-bundle-restored')
    assert state.events[-1] == 'old-readonly-reopen'


@pytest.mark.parametrize('version', VERSIONS)
def test_real27_postcommit_initializer_failure_restores_data_before_old_bundle(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    actual = database_module._schema27_enable_foreign_keys
    observed = {}
    def fail(db):
        actual(db)
        assert not db.in_transaction and db.execute('PRAGMA user_version').fetchone()[0] == 27
        observed['committed'] = state.database.read_bytes()
        state.events.append('verified-postcommit27')
        raise RuntimeError('synthetic-aftercommit-error')
    monkeypatch.setattr(database_module, '_schema27_enable_foreign_keys', fail)
    assert installer.run(state.plan) == 1
    assert observed['committed'] != state.database.read_bytes()
    _assert_old_readable(state)
    _assert_result(state, 1)
    assert state.backup.read_bytes() == state.backup_bytes
    assert state.events.index('verified-postcommit27') < state.events.index('copy-backup-to-stage')
    assert state.events.index('database-parent-synced') < state.events.index('old-bundle-restored')
    assert 'verified-migration-rollback' not in state.events
    assert not (state.updates / 'startup-handshake').exists()


@pytest.mark.parametrize('version', VERSIONS)
def test_accepted_publication_ack_failure_keeps27_and_new_work(tmp_path, monkeypatch, version):
    state = _case(tmp_path, monkeypatch, version)
    actual_replace, actual_copy = Path.replace, shutil.copy2
    def accepted(path, destination):
        result = actual_replace(path, destination)
        if path == state.updates / 'startup-accept.tmp':
            with database_module.connect(state.database) as db:
                db.execute('INSERT INTO settings VALUES(?,?)', ('synthetic-ack-work', TEXT))
            state.events.append('accepted-ack-new-work')
            raise RuntimeError('synthetic-accepted-ack-failure')
        return result
    def forbid_restore(source, destination, *args, **kwargs):
        if Path(source) == state.backup: pytest.fail('accepted27 work must never be restored over')
        return actual_copy(source, destination, *args, **kwargs)
    monkeypatch.setattr(Path, 'replace', accepted)
    monkeypatch.setattr(installer.shutil, 'copy2', forbid_restore)
    with pytest.raises(RuntimeError, match='synthetic-accepted-ack-failure'):
        installer.run(state.plan)
    _assert_result(state, 1)
    assert (state.updates / 'startup-handshake').read_text() == 'accepted'
    assert (state.target / 'version').read_text() == 'new'
    assert (state.previous / 'version').read_text() == 'old'
    assert state.backup.read_bytes() == state.backup_bytes
    assert 'old-readonly-reopen' not in state.events and 'terminate-candidate' not in state.events
    with _readonly(state.database) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
        assert db.execute('SELECT value FROM settings WHERE key=?', ('synthetic-ack-work',)).fetchone() == (TEXT,)
        approved_probe._check_after(db, state.prior)
    current = _snapshot(state.database, state.before['columns'])
    assert set(current['rows']['settings']) == set(state.before['rows']['settings']) | {('synthetic-ack-work', TEXT)}
    assert {k:v for k,v in current['typed_rows'].items() if k!='settings'} == {
        k:v for k,v in state.before['typed_rows'].items() if k!='settings'}
    assert current['foreign_keys'] == state.before['foreign_keys']
