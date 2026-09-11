"""Pure filesystem/command regressions; no Keychain, TCC or real signing."""
from pathlib import Path
import plistlib
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]


def api():
    return runpy.run_path(str(ROOT/'packaging/mac_signing.py'))


def test_nested_inventory_is_inside_out_and_deduplicates_symlinks(tmp_path):
    app = tmp_path/'Test.app'
    nested = app/'Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices/Installer.xpc'
    executable = nested/'Contents/MacOS/Installer'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'\xcf\xfa\xed\xfe'+b'fixture')
    (app/'Contents/Frameworks/Sparkle.framework/Versions/Current').symlink_to('B')
    plan = api()['components'](app)
    assert plan.index(executable) < plan.index(nested) < plan.index(app)
    assert len(plan) == len(set(p.resolve() for p in plan))


def test_stable_requirement_binds_identifier_and_certificate():
    requirement = api()['requirement']('local.knowledge-distiller.app', 'ab'*20)
    assert 'identifier "local.knowledge-distiller.app"' in requirement
    assert 'certificate leaf = H"' + 'ab'*20 + '"' in requirement
    with pytest.raises(ValueError):
        api()['requirement']('bad"identifier', 'ab'*20)
    with pytest.raises(ValueError):
        api()['requirement']('valid', 'not-a-certificate')


def test_sign_preserves_entitlements_and_verifies_each_component(tmp_path):
    app = tmp_path/'Test.app'; (app/'Contents').mkdir(parents=True)
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'local.test'}))
    calls = []
    entitlements = plistlib.dumps({'com.apple.security.cs.allow-jit': True})
    def run(command, **kwargs):
        calls.append(command)
        from subprocess import CompletedProcess
        if '--entitlements' in command and '--display' in command:
            return CompletedProcess(command, 0, entitlements, b'')
        if '--display' in command:
            return CompletedProcess(command, 0, b'', b'Identifier=local.test\n')
        if '--sign' in command:
            values = plistlib.loads(Path(command[command.index('--entitlements')+1]).read_bytes())
            assert values['com.apple.security.cs.allow-jit'] is True
            assert values['com.apple.security.cs.disable-library-validation'] is True
        return CompletedProcess(command, 0, b'', b'')
    api()['sign_bundle'](app, {'identity': 'Test Identity', 'certificate_sha1': 'ab'*20}, run=run)
    signing = [c for c in calls if '--sign' in c]
    assert signing and all('--deep' not in c for c in signing)
    assert '--requirements' in signing[0] and '--options' in signing[0]
    assert signing[0][signing[0].index('--requirements')+1].startswith('=designated => ')
    verification = next(c for c in calls if '--test-requirement' in c)
    assert verification[verification.index('--test-requirement')+1].startswith('=identifier ')
    assert any('--verify' in c and '--strict' in c for c in calls)


def test_product_and_build_versions_are_separate_and_monotonic():
    values = api()['version_info']('1.1', '2026.09.11.1')
    assert values['CFBundleShortVersionString'] == '1.1'
    assert values['CFBundleVersion'] == '2026.09.11.1'
    with pytest.raises(ValueError):
        api()['version_info']('1.1', '1.1')
    with pytest.raises(ValueError):
        api()['version_info']('1.1', '2026.09.09.11')


@pytest.mark.parametrize('file_type,certificate,expected,node', [(2, True, True, False), (6, True, False, False), (2, False, False, False), (2, True, True, True)])
def test_library_validation_exception_only_for_local_signed_hosts(tmp_path, file_type, certificate, expected, node):
    import struct
    import subprocess
    app = tmp_path/'Test.app'; app.mkdir()
    binary = app/('Contents/Frameworks/bin/node' if node else 'binary')
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b'\xcf\xfa\xed\xfe' + struct.pack('<III', 0, 0, file_type))
    captured = []
    def run(command, **kwargs):
        if '--display' in command:
            return subprocess.CompletedProcess(command, 0, b'', b'Identifier=local.test\n')
        if '--sign' in command and command[-1] == str(binary):
            captured.append(plistlib.loads(Path(command[command.index('--entitlements')+1]).read_bytes())
                            if '--entitlements' in command else {})
        return subprocess.CompletedProcess(command, 0, b'', b'')
    config = {'identity':'Test', 'certificate_sha1':'ab'*20} if certificate else {}
    api()['sign_bundle'](app, config, run=run)
    assert bool(captured[0].get('com.apple.security.cs.disable-library-validation')) is expected
    assert bool(captured[0].get('com.apple.security.cs.allow-jit')) is node


def test_signing_failure_stops_before_parent_and_does_not_hide_error(tmp_path):
    import subprocess
    app = tmp_path/'Test.app'; app.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if '--display' in command:
            return subprocess.CompletedProcess(command, 0, b'', b'')
        raise subprocess.CalledProcessError(1, command)
    with pytest.raises(subprocess.CalledProcessError):
        api()['sign_bundle'](app, run=run)
    assert not any('--verify' in c for c in calls)


@pytest.mark.parametrize('metadata', [b'code object is not signed at all', b'Identifier=libc++.1\n'])
def test_unsigned_or_unsafe_identifier_gets_stable_identifier(tmp_path, metadata):
    import subprocess
    app = tmp_path/'Test.app'; app.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if '--display' in command:
            return subprocess.CompletedProcess(command, 1 if b'not signed' in metadata else 0, b'',
                                               b'' if '--entitlements' in command and b'Identifier' in metadata else metadata)
        return subprocess.CompletedProcess(command, 0, b'', b'')
    api()['sign_bundle'](app, {'identity':'Test', 'certificate_sha1':'ab'*20}, run=run)
    signed = next(c for c in calls if '--sign' in c)
    assert signed[signed.index('--identifier')+1].startswith('local.knowledge-distiller.code.')
    assert any('--test-requirement' in c for c in calls)


def test_release_update_policy_requires_stable_identity_and_signed_feed():
    policy = api()['update_policy']
    stable = {'identity': 'local-certificate', 'certificate_sha1': 'ab'*20}
    feed = {'public_key': 'key', 'feed_url': 'https://example.test/appcast.xml'}
    assert policy(stable, feed)['KDManualUpdateOnly'] is False
    assert policy({}, feed)['KDManualUpdateOnly'] is True
    assert policy(stable, {})['KDManualUpdateOnly'] is True
    assert policy(stable, feed, manual=True)['KDManualUpdateOnly'] is True
    with pytest.raises(ValueError, match='签名公钥'):
        policy(stable, {'feed_url': feed['feed_url']})
    with pytest.raises(ValueError):
        policy({'identity': 'local-certificate'}, feed)


def test_update_helper_identity_ignores_pyinstaller_build_uuid(tmp_path):
    import subprocess
    app = tmp_path/'Test.app'
    helper = app/'Contents/MacOS/update-helper'
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b'\xcf\xfa\xed\xfe'+b'fixture')
    identifiers = []
    for uuid in ('aaaa', 'bbbb'):
        def run(command, **kwargs):
            if '--display' in command:
                return subprocess.CompletedProcess(command, 0, b'', f'Identifier=update-helper-{uuid}\n'.encode())
            if '--sign' in command and command[-1] == str(helper):
                identifiers.append(command[command.index('--identifier')+1])
            return subprocess.CompletedProcess(command, 0, b'', b'')
        api()['sign_bundle'](app, {'identity':'Test', 'certificate_sha1':'ab'*20}, run=run)
    assert identifiers == ['local.knowledge-distiller.update-helper'] * 2
