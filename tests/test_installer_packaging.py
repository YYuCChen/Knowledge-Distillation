"""Resource/command contracts; these tests never build or sign a native app."""
import hashlib
import json
import plistlib
from pathlib import Path
import runpy
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def resources():
    return runpy.run_path(str(ROOT/'packaging/resources.py'))


def test_installer_runtime_files_are_in_package_and_native_resource_manifests():
    api = resources()
    required = set(api['installer_datas'](ROOT))
    assert len(required) == 2
    assert required <= set(api['application_datas'](ROOT))
    assert {Path(source).name for source, _ in required} == {'page.html', 'installer-logo.svg'}


def test_missing_installer_html_fails_manifest_instead_of_building_broken_ui(tmp_path):
    folder = tmp_path/'src/knowledge_distiller/v1/installer_assets'
    folder.mkdir(parents=True)
    (folder/'installer-logo.svg').write_text('<svg/>')
    with pytest.raises(RuntimeError, match='page.html'):
        resources()['installer_datas'](tmp_path)


def test_platform_bridges_include_lazy_com_and_new_transaction_dependencies():
    api = resources()['installer_hiddenimports']
    common = {'knowledge_distiller.v1.component_attempt',
              'knowledge_distiller.v1.installer_platform', 'knowledge_distiller.v1.install_problem'}
    assert common <= set(api('win32')) and common <= set(api('darwin'))
    assert {'pythoncom', 'pywintypes', 'win32gui', 'win32com.shell.shell',
            'win32com.shell.shellcon', 'win32com.client', 'win32job'} <= set(api('win32'))
    assert {'AppKit', 'Foundation'} <= set(api('darwin'))
    assert 'pythoncom' not in api('darwin')
    with pytest.raises(ValueError): api('linux')


def test_checked_in_icon_containers_match_the_reviewed_svg():
    assets = ROOT/'packaging/assets'
    manifest = json.loads((assets/'installer-icon.json').read_text())
    assert hashlib.sha256((ROOT/manifest['source']).read_bytes()).hexdigest() == manifest['source_sha256']
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((assets/name).read_bytes()).hexdigest() == expected
    icns = (assets/'installer-icon.icns').read_bytes()
    assert icns[:4] == b'icns' and struct.unpack('>I', icns[4:8])[0] == len(icns)
    ico = (assets/'installer-icon.ico').read_bytes()
    assert ico[:4] == b'\x00\x00\x01\x00'
    count = struct.unpack('<H', ico[4:6])[0]
    assert {ico[6+i*16] or 256 for i in range(count)} >= {16,32,48,64,128,256}


@pytest.mark.parametrize('platform_name', ['darwin', 'win32'])
def test_installer_command_keeps_version_identity_icon_and_resources(tmp_path, monkeypatch, platform_name):
    module = runpy.run_path(str(ROOT/'scripts/build_component_installer.py'))
    output = tmp_path/'output'; tools = tmp_path/'tools'; tools.mkdir()
    (tools/('BinaryDelta' if platform_name == 'darwin' else 'hpatchz.exe')).write_bytes(b'fixture-not-executable')
    monkeypatch.setattr(module['sys'], 'argv', ['build', '--output', str(output), '--tools', str(tools), '--version', '2026.09.14.3', '--product-version', '1.3'])
    monkeypatch.setattr(module['sys'], 'platform', platform_name)
    monkeypatch.setattr(module['platform'], 'machine', lambda: 'arm64')
    commands = []
    original_run_path = runpy.run_path
    def run_path(path):
        if str(path).endswith('mac_signing.py'):
            result = original_run_path(path)
            def sign(app, config):
                info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
                assert info['CFBundleVersion'] == '2026.09.14.3'
                assert info['CFBundleShortVersionString'] == '1.3'
                return {'synthetic': True}
            result['sign_bundle'] = sign
            return result
        return original_run_path(path)
    monkeypatch.setattr(runpy, 'run_path', run_path)
    def execute(command, **kw):
        commands.append(command)
        if 'PyInstaller' in command and platform_name == 'darwin':
            folder = output/'dist/KnowledgeDistillerInstaller.app/Contents'
            folder.mkdir(parents=True)
            (folder/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'local.knowledge-distiller.installer'}))
    monkeypatch.setattr(subprocess, 'run', execute)
    monkeypatch.setattr(subprocess, 'check_output', lambda command, **kw: 'a'*40 if 'rev-parse' in command else '')
    module['main']()
    command = next(c for c in commands if 'PyInstaller' in c)
    suffix = 'icns' if platform_name == 'darwin' else 'ico'
    assert command[command.index('--icon')+1].endswith('installer-icon.'+suffix)
    if platform_name == 'darwin':
        assert command[command.index('--osx-bundle-identifier')+1] == 'local.knowledge-distiller.installer'
        assert 'AppKit' in command and 'Foundation' in command
    else:
        version_text = Path(command[command.index('--version-file')+1]).read_text()
        assert "StringStruct('FileVersion', '2026.09.14.3')" in version_text
        assert "StringStruct('ProductVersion', '1.3')" in version_text
        assert 'filevers=(2026, 9, 14, 3)' in version_text
        assert 'pythoncom' in command
    manifest = json.loads((output/'build-manifest.json').read_text())
    assert manifest['version'] == '2026.09.14.3' and manifest['product_version'] == '1.3'
    assert any('installer_assets/page.html' in value.replace('\\', '/') for value in command)
    assert any('installer_assets/installer-logo.svg' in value.replace('\\', '/') for value in command)
    assert 'knowledge_distiller.v1.component_attempt' in command


def test_invalid_installer_versions_fail_before_native_build():
    api = runpy.run_path(str(ROOT/'packaging/version_metadata.py'))
    with pytest.raises(ValueError): api['native_versions']('1.3', '2026.02.31.2')
    with pytest.raises(ValueError): api['windows_version_resource']('1.3', '2026.09.14.65536')


def test_dual_dispatch_installer_uses_app_request_versions():
    api = runpy.run_path(str(ROOT/'scripts/dual_build.py'))
    config = dict(mac_python='/python', windows_python='C:/python.exe', signing_config='/signing.json',
                  sparkle_sdk='/sdk', windows_cache='C:/cache', output_root='/builds', windows_root='C:/builds')
    requests = api['make_requests'](config, 'a'*40, '2026.09.14.3', '1.3', Path('/source'), 'C:/source')
    for request in requests:
        command = next(step['command'] for step in request['steps'] if step['name'] == 'installer')
        assert command[command.index('--version')+1] == request['version']
        assert command[command.index('--product-version')+1] == request['product_version']
        assert 'installer/build-manifest.json' in request['artifacts']
