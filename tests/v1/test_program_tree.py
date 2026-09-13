from pathlib import Path
import plistlib
import runpy
import zipfile
import pytest

from knowledge_distiller.v1.program_tree import identity, extract_mac_base
from knowledge_distiller.v1.updates import UpdateError


def test_mac_base_roundtrip_preserves_signed_tree_inputs(tmp_path):
    root = tmp_path / 'test.app'
    (root / 'Contents/MacOS').mkdir(parents=True)
    binary = root / 'Contents/MacOS/KnowledgeDistiller'
    binary.write_bytes(b'synthetic executable bytes')
    binary.chmod(0o755)
    (root / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleVersion': '2'}))
    (root / 'Contents/current').symlink_to('MacOS')
    package = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/package_platform_base.py'))['package']
    report = package(root, tmp_path / 'assets', 'macos-arm64')
    stage = tmp_path / 'restored.app'
    extract_mac_base(tmp_path / 'assets' / report['archive'], stage,
        expected_identity=report['identity'], maximum_bytes=report['unpacked_size'])
    assert identity(root, 'macos-arm64') == identity(stage, 'macos-arm64')
    assert (stage / 'Contents/current').is_symlink()
    assert (stage / 'Contents/MacOS/KnowledgeDistiller').stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'a\\b'])
def test_mac_base_paths_cannot_escape(tmp_path, name):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr(name, b'x')
    with pytest.raises(UpdateError):
        extract_mac_base(archive, tmp_path / 'stage', expected_identity='0'*64, maximum_bytes=10)
    assert not (tmp_path / 'stage').exists()
