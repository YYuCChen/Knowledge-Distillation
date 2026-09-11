"""Installer failure boundaries; no real processes, signing tools, or HTTP calls."""
import fcntl
import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
import pytest

from knowledge_distiller.v1 import update_installer as installer


@pytest.fixture
def installation(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    root = data / 'updates'
    root.mkdir(parents=True)
    target = tmp_path / 'Knowledge.app'
    target.mkdir()
    (target / 'version').write_text('old')
    database = data / 'knowledge.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE knowledge (value TEXT)')
        db.execute("INSERT INTO knowledge VALUES ('existing')")
    key = ECC.generate(curve='Ed25519')
    sign = lambda content: base64.b64encode(eddsa.new(key, 'rfc8032').sign(content)).decode()
    feed = ('<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">'
            '<channel><item><sparkle:version>2</sparkle:version>'
            f'<enclosure url="update.zip" length="7" sparkle:edSignature="{sign(b"archive")}" />'
            '</item></channel></rss>\n').encode()
    (root / 'appcast.xml').write_bytes(feed +
        f'<!-- sparkle-signatures:\nedSignature: {sign(feed)}\nlength: {len(feed)}\n-->\n'.encode())
    plan = root / 'install-plan.json'
    (data / '.desktop-instance.json').write_text(json.dumps({'pid': os.getppid()}))
    plan.write_text(json.dumps({
        'data_root': str(data), 'version': '2', 'parent_pid': os.getppid(),
        'no_open': True,
        'info': {'version': '1', 'display_version': '1', 'bundle': str(target),
                 'public_key': base64.b64encode(key.public_key().export_key(format='raw')).decode(),
                 'feed_url': 'https://example.test/appcast.xml'},
    }))
    events = []
    process = SimpleNamespace(pid=987654321)
    process.poll = lambda: None
    process.terminate = lambda: events.append('terminate-candidate')
    process.wait = lambda **kwargs: events.append('wait-candidate')

    class Server:
        server_port = 12345
        def __init__(self, *args): pass
        def serve_forever(self): pass
        def shutdown(self): pass
        def server_close(self): pass

    def download(self, asset):
        events.append('download')
        return root / asset['name']

    def kill(pid, sig):
        assert pid == os.getppid()
        if sig == 0:
            raise ProcessLookupError()
        assert sig == installer.signal.SIGTERM
        events.append('stop-parent')

    def run(command, **kwargs):
        name = Path(command[0]).name
        events.append(name)
        if name == 'ditto':
            shutil.copytree(command[1], command[2], dirs_exist_ok=True)
        elif name == 'update-cli':
            assert '--defer-install' not in command
            (target / 'version').write_text('new')
        elif name == 'KnowledgeDistiller':
            assert '--check-runtime' in command
            assert command[command.index('--data-dir') + 1] == str(data)
        else:
            assert name == 'codesign'
        return SimpleNamespace(returncode=0)

    def popen(arguments, **kwargs):
        events.append('start-candidate')
        assert arguments[arguments.index('--data-dir') + 1] == str(data)
        assert '--update-handshake' in arguments
        (data / '.desktop-instance.json').write_text(json.dumps({'pid': process.pid, 'port': 12345}))
        return process

    monkeypatch.setattr(installer, 'ThreadingHTTPServer', Server)
    monkeypatch.setattr(installer.Updates, 'download', download)
    monkeypatch.setattr(installer.os, 'kill', kill)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    monkeypatch.setattr(installer.subprocess, 'Popen', popen)
    monkeypatch.setattr(installer.httpx, 'get', lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {'version': '2', 'phase': 'installing'}))
    return SimpleNamespace(data=data, root=root, target=target, database=database,
                           previous=target.with_name('.knowledge-distiller-update-backup.app'),
                           plan=plan, events=events)


def test_changed_requesting_instance_is_rejected_without_stopping_app(installation):
    state = installation
    (state.data / '.desktop-instance.json').write_text(json.dumps({'pid': 123456789}))

    assert installer.run(state.plan) == 1
    assert state.events == []
    assert (state.target / 'version').read_text() == 'old'


def test_delta_failure_does_not_fetch_full_package_without_confirmation(installation, monkeypatch):
    state=installation
    original_parse=installer.parse_feed
    def parse(*args):
        release=original_parse(*args)
        release['selected']={'name':'small.delta','size':2,'signature':'test'}
        return release
    monkeypatch.setattr(installer,'parse_feed',parse)
    handlers=[]
    class Server:
        server_port=12345
        def __init__(self,address,handler):handlers.append(handler)
        def serve_forever(self):pass
        def shutdown(self):pass
        def server_close(self):pass
    monkeypatch.setattr(installer,'ThreadingHTTPServer',Server)
    original_run=installer.subprocess.run
    refused=[]
    def run(command,**kwargs):
        if Path(command[0]).name=='update-cli':
            from urllib.parse import urlsplit
            feed_url=command[command.index('--feed-url')+1]
            request=SimpleNamespace(path=urlsplit(feed_url).path.replace('appcast.xml','update.zip'),
                                    send_error=lambda *args:refused.append(args))
            handlers[0].do_GET(request)
            return SimpleNamespace(returncode=1)
        return original_run(command,**kwargs)
    monkeypatch.setattr(installer.subprocess,'run',run)
    monkeypatch.setattr(installer.subprocess,'Popen',lambda *args,**kwargs:state.events.append('restart-old'))
    assert installer.run(state.plan)==1
    assert refused[0][0]==409
    assert state.events.count('download')==1  # Only the already-selected delta.
    assert json.loads((state.root/'full-update-required.json').read_text())['version']=='2'
    assert (state.target/'version').read_text()=='old'
    assert 'restart-old' in state.events


def test_unknown_previous_is_preserved_without_stopping_app(installation):
    state = installation
    state.previous.mkdir()
    unknown = state.previous / 'user-owned-marker'
    unknown.write_bytes(b'do not remove')

    assert installer.run(state.plan) == 1

    assert unknown.read_bytes() == b'do not remove'
    assert (state.target / 'version').read_text() == 'old'
    assert state.events == []
    assert not (state.root / 'before-install.sqlite3').exists()


def test_accepted_cleanup_and_error_record_failures_never_rollback(installation, monkeypatch):
    state = installation
    original_replace = Path.replace
    original_write = Path.write_text
    original_rmtree = shutil.rmtree

    def publish_acceptance(path, target):
        result = original_replace(path, target)
        if path == state.root / 'startup-accept.tmp':
            # Once the handshake is published, real new user work may exist.
            with sqlite3.connect(state.database) as db:
                db.execute("INSERT INTO knowledge VALUES ('accepted-new-work')")
        return result

    def fail_cleanup(path, *args, **kwargs):
        if Path(path) == state.previous:
            raise PermissionError('cannot remove previous application')
        return original_rmtree(path, *args, **kwargs)

    def fail_error_record(path, *args, **kwargs):
        if path.name in ('cleanup-error.txt', 'install-result.json'):
            raise OSError('cannot write recovery record')
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'replace', publish_acceptance)
    monkeypatch.setattr(shutil, 'rmtree', fail_cleanup)
    monkeypatch.setattr(Path, 'write_text', fail_error_record)

    with pytest.raises(OSError, match='cannot write recovery record'):
        installer.run(state.plan)

    assert (state.root / 'startup-handshake').read_text() == 'accepted'
    assert 'terminate-candidate' not in state.events
    assert (state.target / 'version').read_text() == 'new'
    assert (state.previous / 'version').read_text() == 'old'
    with sqlite3.connect(state.database) as db:
        assert db.execute('SELECT value FROM knowledge ORDER BY rowid').fetchall() == [
            ('existing',), ('accepted-new-work',)]


def test_installation_lock_blocks_second_helper_without_app_or_db_mutation(installation):
    state = installation
    before = state.database.read_bytes()
    # The first helper still owns this lock after the old desktop has exited.
    # Keep previous absent so removing the flock gate cannot pass via that guard.
    assert not state.previous.exists()
    with (state.data / '.update.lock').open('a') as first_helper:
        fcntl.flock(first_helper, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert installer.run(state.plan) == 1
        assert state.events == []
        assert not state.previous.exists()
        assert state.database.read_bytes() == before
        assert (state.target / 'version').read_text() == 'old'
        # A rejected helper must not release the first helper's lock.
        with (state.data / '.update.lock').open('a') as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
