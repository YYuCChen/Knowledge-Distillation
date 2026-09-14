"""Resource/command contracts; these tests never build or sign a native app."""
import hashlib
import json
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


def test_mac_installer_command_keeps_identity_icon_and_resources(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT/'scripts/build_component_installer.py'))
    output = tmp_path/'output'; tools = tmp_path/'tools'; tools.mkdir()
    (tools/'BinaryDelta').write_bytes(b'fixture-not-executable')
    monkeypatch.setattr(module['sys'], 'argv', ['build', '--output', str(output), '--tools', str(tools)])
    monkeypatch.setattr(module['sys'], 'platform', 'darwin')
    monkeypatch.setattr(module['platform'], 'machine', lambda: 'arm64')
    commands = []
    original_run_path = runpy.run_path
    def run_path(path):
        if str(path).endswith('mac_signing.py'):
            return {'sign_bundle': lambda *a, **kw: {'synthetic': True}}
        return original_run_path(path)
    monkeypatch.setattr(runpy, 'run_path', run_path)
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw: commands.append(command))
    monkeypatch.setattr(subprocess, 'check_output', lambda command, **kw: 'a'*40 if 'rev-parse' in command else '')
    module['main']()
    command = next(c for c in commands if 'PyInstaller' in c)
    assert command[command.index('--icon')+1].endswith('installer-icon.icns')
    assert command[command.index('--osx-bundle-identifier')+1] == 'local.knowledge-distiller.installer'
    assert any('installer_assets/page.html' in value for value in command)
    assert any('installer_assets/installer-logo.svg' in value for value in command)
    assert 'AppKit' in command and 'Foundation' in command
    assert 'knowledge_distiller.v1.component_attempt' in command
