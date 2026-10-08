"""Disposable final-artifact checks; never build, publish, or use formal data.

Components exercise signed V2 direct-delta assembly and the actual install
kernel with real frozen startup. They do not simulate a passing online feed
check or the old UI's authenticated request-exit handshake.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys


def require_tmp(path):
    path = path.resolve()
    if not path.is_relative_to(Path('/tmp').resolve()):
        raise ValueError('output must be an explicit disposable /tmp directory')
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def rows(database):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        result = {}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            result[table] = (columns, sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr))
        return result


def preserves(database, before):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        for table, (columns, values) in before.items():
            projection = ','.join(f'"{c}"' for c in columns)
            assert sorted(db.execute(f'SELECT {projection} FROM "{table}"').fetchall(), key=repr) == values, table
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def managed(args, root):
    from verify_candidate import _synthetic_vault, _isolate_mac_home
    import os
    vault = _synthetic_vault(root)
    before = files(vault)
    env = dict(os.environ)
    _isolate_mac_home(env, root)
    command = [str(args.app / 'Contents/MacOS/KnowledgeDistiller'), '--wiki-kit',
               'managed', '--vault-root', str(vault), '--', 'describe-generated']
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=120)
    (root / 'managed.stdout').write_text(result.stdout)
    (root / 'managed.stderr').write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and payload
    assert all(isinstance(row, list) and len(row) == 3 and all(isinstance(v, str) for v in row) for row in payload)
    assert {(r[0], r[1]) for r in payload} >= {('wiki/index.md', '@system'), ('wiki/待确认.md', '@system')}
    assert len({(r[0], r[1]) for r in payload}) == len(payload)
    assert files(vault) == before
    assert not any(root.rglob('knowledge.sqlite3'))
    assert not any(root.rglob('.desktop-instance.json'))
    state_command = command[:-1] + ['describe-state']
    state_result = subprocess.run(state_command, cwd=root, env=env,
                                  capture_output=True, text=True, timeout=120)
    (root / 'managed-state.stdout').write_text(state_result.stdout)
    (root / 'managed-state.stderr').write_text(state_result.stderr)
    assert state_result.returncode == 0, state_result.stderr
    state = json.loads(state_result.stdout)
    assert type(state) is dict and set(state) == {'pages', 'pending', 'query_record'}
    assert type(state['pages']) is list
    for page in state['pages']:
        assert type(page) is dict and set(page) == {'path', 'sha256', 'type', 'confirmed', 'declared_topics'}
        assert all(type(page[key]) is str for key in ('path', 'sha256', 'type'))
        assert len(page['sha256']) == 64 and all(c in '0123456789abcdef' for c in page['sha256'])
        assert type(page['confirmed']) is bool
        assert type(page['declared_topics']) is list and all(type(t) is str for t in page['declared_topics'])
    assert type(state['pending']) is dict and set(state['pending']) == {'外部', '自述'}
    assert all(type(paths) is list and all(type(p) is str for p in paths)
               for paths in state['pending'].values())
    query = state['query_record']
    assert type(query) is dict and set(query) == {'exists', 'sha256'}
    assert type(query['exists']) is bool
    assert (type(query['sha256']) is str and len(query['sha256']) == 64
            and all(c in '0123456789abcdef' for c in query['sha256'])) if query['exists'] else query['sha256'] is None
    assert files(vault) == before
    assert not any(root.rglob('knowledge.sqlite3'))
    assert not any(root.rglob('.desktop-instance.json'))
    return {'command': command, 'state_command': state_command, 'rows': len(payload),
            'state_pages': len(state['pages']), 'vault_unchanged': True, 'app_not_started': True}


def components(args, root):
    from verify_candidate import _isolate_mac_home
    from tests.v1.test_data_upgrade_probe_versions import _prepare
    from knowledge_distiller.v1.component_assembly import ComponentAssembly
    from knowledge_distiller.v1.component_download import ComponentDownloader
    from knowledge_distiller.v1 import component_install as install
    from knowledge_distiller.v1.database import connect
    from knowledge_distiller.v1.program_tree import identity
    from knowledge_distiller.v1.updates import UpdateError
    import os

    baseline_info = plistlib.loads((args.v2 / 'Contents/Info.plist').read_bytes())
    assert baseline_info['CFBundleVersion'] == '2026.09.30.4'
    assert baseline_info['CFBundleShortVersionString'] == '2.0'
    public = json.loads((args.app / 'Contents/Resources/knowledge_distiller/v1/adapters/update_config.json').read_text())['public_key']
    envelope = args.release.read_bytes()
    results = []
    for reject in (False, True):
        lane = root / ('rollback' if reject else 'accepted')
        lane.mkdir(mode=0o700)
        database, vault, _ = _prepare(lane, 21)
        data = database.parent
        # Stop synthetic pre-existing owners, before recording the baseline.
        # No live work is allowed during the accepted-startup probe.
        with connect(database) as db:
            db.execute("UPDATE distill_items SET state='failed',phase='collecting',dismissed_at='synthetic-stopped'")
            db.execute("UPDATE collection_operations SET state='cancelled',cancel_requested=1")
        before_vault = files(vault)
        before_rows = rows(database)
        target = lane / 'installed.app'
        shutil.copytree(args.v2, target, symlinks=True)
        old_id = identity(target, 'macos-arm64')
        assembler = ComponentAssembly(data / 'components', data / 'updates/component-cache',
            platform='macos-arm64', public_key=public, binary_delta=args.sdk / 'bin/BinaryDelta',
            downloader=ComponentDownloader(data / 'updates/component-cache', offline_root=args.assets))
        release, plan = assembler.prepare(envelope, installed=target, current=baseline_info['CFBundleVersion'])
        assert release['version'] == args.version
        assert release['target_identity'] == identity(args.app, 'macos-arm64')
        assert plan.source == 'current', 'must exercise signed V2 direct delta, not base fallback'
        candidate, _ = assembler.assemble(release, plan, lane / 'assembly', installed=target)
        processes = []
        backed_up_bytes = []
        def launcher(bundle, data_root, platform, handshake):
            backed_up_bytes.append((data_root / 'updates/component-before-install.sqlite3').read_bytes())
            env = dict(os.environ)
            _isolate_mac_home(env, lane)
            process = subprocess.Popen([str(bundle / 'Contents/MacOS/KnowledgeDistiller'),
                '--data-dir', str(data_root), '--no-open', '--update-handshake', str(handshake)],
                cwd=lane, env=env, stdout=(lane / 'startup.stdout').open('wb'),
                stderr=(lane / 'startup.stderr').open('wb'), start_new_session=True)
            processes.append(process)
            return process
        def acceptance(process, data_root, version):
            install.accept_startup(process, data_root, version)
            with closing(sqlite3.connect(database)) as db:
                assert db.execute('PRAGMA user_version').fetchone()[0] == 27
            preserves(database, before_rows)
            assert files(vault) == before_vault
            if reject:
                raise UpdateError('synthetic_preaccept_refusal')
        try:
            if reject:
                try:
                    install.install(candidate, target, data, platform='macos-arm64', version=args.version,
                        target_identity=release['target_identity'], launcher=launcher, acceptance=acceptance)
                except UpdateError as error:
                    assert str(error) == 'synthetic_preaccept_refusal', str(error)
                else:
                    raise AssertionError('rollback case unexpectedly accepted')
                assert identity(target, 'macos-arm64') == old_id
                assert database.read_bytes() == backed_up_bytes[0]
                assert rows(database) == before_rows
                with closing(sqlite3.connect(database)) as db:
                    assert db.execute('PRAGMA user_version').fetchone()[0] == 21
                assert files(vault) == before_vault
            else:
                outcome = install.install(candidate, target, data, platform='macos-arm64', version=args.version,
                    target_identity=release['target_identity'], launcher=launcher, acceptance=acceptance)
                assert outcome['accepted'] and outcome['activation']['status'] == 'ready'
                assert outcome['finalization']['status'] == 'complete'
                assert identity(target, 'macos-arm64') == release['target_identity']
            assert not (data / 'updates/component-install-journal.json').exists()
            results.append({'case': 'rollback' if reject else 'accepted', 'source': plan.source,
                            'frozen_startup': True, 'schema21_to27': True, 'vault_unchanged': True})
        finally:
            cleanup = []
            for process in processes:
                if process.poll() is None:
                    # Never trust an outcome PID: recheck this child's actual
                    # command immediately before signalling its private lane.
                    record = {'pid': process.pid, 'status': 'not_signalled'}
                    cleanup.append(record)
                    try:
                        command = subprocess.run(
                            ['/bin/ps', '-ww', '-p', str(process.pid), '-o', 'command='],
                            capture_output=True, text=True, timeout=5)
                        record['command'] = command.stdout.strip()
                        argv = shlex.split(record['command'])
                        expected = [str(target / 'Contents/MacOS/KnowledgeDistiller'),
                                    '--data-dir', str(data), '--no-open', '--update-handshake']
                        if command.returncode != 0 or argv[:len(expected)] != expected:
                            record['status'] = 'ownership_unconfirmed'
                            continue
                        if process.poll() is not None:
                            record['status'] = 'already_exited'
                            continue
                        process.terminate()
                        process.wait(timeout=60)
                        record['status'] = 'terminated'
                    except subprocess.TimeoutExpired:
                        # Keep the lane and logs for diagnosis; no SIGKILL,
                        # group kill, or signalling any replacement PID.
                        record['status'] = 'cleanup_timeout'
                    except (OSError, ValueError) as error:
                        record.update(status='cleanup_failed', error=str(error))
            (lane / 'cleanup.json').write_text(json.dumps(cleanup, indent=2))
            if (sys.exc_info()[0] is None
                    and any(row['status'] not in ('terminated', 'already_exited') for row in cleanup)):
                raise RuntimeError(f'cleanup incomplete; retained diagnostics: {lane / "cleanup.json"}')
    return {'cases': results, 'online_v2_request_exit_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('managed', 'components'))
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new path under /tmp, must not exist')
    parser.add_argument('--v2', type=Path)
    parser.add_argument('--release', type=Path)
    parser.add_argument('--assets', type=Path)
    parser.add_argument('--sdk', type=Path)
    parser.add_argument('--version')
    args = parser.parse_args()
    args.app = args.app.resolve(strict=True)
    if args.mode == 'components' and not all((args.v2, args.release, args.assets, args.sdk, args.version)):
        parser.error('components needs --v2 --release --assets --sdk --version')
    root = require_tmp(args.output)
    report = {'ok': False, 'mode': args.mode}
    try:
        action = {'managed': managed, 'components': components}[args.mode]
        report.update(action(args, root))
        report['ok'] = True
    finally:
        (root / 'result.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    # Test fixtures are read from this final source tree, never a formal DB.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
