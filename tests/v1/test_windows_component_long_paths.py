"""Program assets need extended paths even when the Python host is long-path aware."""
import json
import os
from pathlib import Path
import shutil
import zipfile

import pytest

from knowledge_distiller.v1.windows_platform import filesystem_path
from knowledge_distiller.v1.windows_delta import build_payload, stage_payload, inventory
from knowledge_distiller.v1.component_install import install
from knowledge_distiller.v1.program_tree import identity
from knowledge_distiller.v1.updates import UpdateError

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Win32 extended path contract')


def program(root, version):
    root = filesystem_path(root)
    (root / '_internal').mkdir(parents=True)
    (root / 'KnowledgeDistiller.exe').write_bytes(b'fixture-' + version.encode())
    (root / '_internal/windows-version.json').write_text(json.dumps({'version': version}))
    relative = '_internal/' + '/'.join(['nested-package-' + 'x' * 70] * 3) + '/retained.js'
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b'unchanged nested dependency')
    assert len(str(path)) > 260
    return relative


def require_extended_open(monkeypatch):
    original = Path.open
    def checked(path, *args, **kwargs):
        if len(str(path)) >= 260:
            assert str(path).startswith('\\\\?\\'), 'ordinary long path reached file I/O'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', checked)


@pytest.mark.parametrize('protocol', [0, 1, 2])
def test_deep_program_base_and_delta_rebuild(tmp_path, monkeypatch, protocol):
    old, new, stage = [tmp_path / name for name in ('old', 'new', 'stage')]
    relative = program(old, '1');program(new, '2')
    require_extended_open(monkeypatch)
    archive = tmp_path / 'asset.zip'
    if protocol == 0:
        with zipfile.ZipFile(archive, 'w') as package:
            for name in inventory(new):
                package.write(filesystem_path(new) / name, '知识蒸馏器/' + name)
    else:
        build_payload(new, archive, old, format_version=protocol)
    before = inventory(old)
    stage_payload(archive, old, stage, version='2', current='1')
    assert relative in inventory(stage)
    assert inventory(stage) == inventory(new)
    assert inventory(old) == before


@pytest.mark.parametrize('reject', [False, True])
def test_deep_install_copy_cleanup_and_rollback(tmp_path, monkeypatch, reject):
    target, candidate = tmp_path / 'installed', tmp_path / 'candidate'
    relative = program(target, '1');program(candidate, '2')
    require_extended_open(monkeypatch)
    before = identity(target, 'windows-x86_64')
    expected = identity(candidate, 'windows-x86_64')
    class Process:
        def poll(self):return 0
    def acceptance(*args):
        if reject:raise UpdateError('fixture rejection')
    kwargs = dict(platform='windows-x86_64', version='2', target_identity=expected,
                  launcher=lambda *args:Process(), acceptance=acceptance, activation=lambda *args:None)
    if reject:
        with pytest.raises(UpdateError, match='fixture rejection'):
            install(candidate, target, tmp_path / 'data', **kwargs)
    else:
        assert install(candidate, target, tmp_path / 'data', **kwargs)['accepted']
    assert identity(target, 'windows-x86_64') == (before if reject else expected)
    assert relative in inventory(target)
    assert not (tmp_path / 'installed.component-previous').exists()
    assert not (tmp_path / 'installed.component-stage').exists()


def test_native_binary_patch_with_deep_input_and_output(tmp_path, monkeypatch):
    tools = os.environ.get('KD_TEST_HDIFFPATCH')
    if not tools:
        pytest.skip('Explicit native HDiffPatch tools required')
    import random
    old, new = tmp_path / 'old', tmp_path / 'new'
    relative = program(old, '1');program(new, '2')
    data = random.Random(719).randbytes(2 * 1024**2)
    (filesystem_path(old) / relative).write_bytes(data)
    (filesystem_path(new) / relative).write_bytes(data[:900000] + b'changed code' + data[900000:])
    require_extended_open(monkeypatch)
    archive = tmp_path / 'patch.zip'
    result = build_payload(new, archive, old, format_version=2, tools_dir=tools)
    assert result['operations'][relative]['kind'] == 'patch'
    stage_payload(archive, old, tmp_path / 'stage', version='2', current='1', tools_dir=tools)
    assert inventory(tmp_path / 'stage') == inventory(new)
