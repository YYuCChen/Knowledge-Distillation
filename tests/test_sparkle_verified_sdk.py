import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
import tarfile
import pytest


def test_attach_uses_exact_archive_instead_of_flattened_or_changed_tree(tmp_path):
    spec = importlib.util.spec_from_file_location('verified_sparkle', Path(__file__).parents[1] / 'packaging/sparkle.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = BytesIO()
    with tarfile.open(fileobj=data, mode='w:xz') as tar:
        entry = tarfile.TarInfo('Sparkle.framework/Versions/B/Sparkle')
        content = b'verified runtime'
        entry.size = len(content)
        tar.addfile(entry, BytesIO(content))
        link = tarfile.TarInfo('Sparkle.framework/Sparkle')
        link.type = tarfile.SYMTYPE
        link.linkname = 'Versions/B/Sparkle'
        tar.addfile(link)
    archive = tmp_path / 'Sparkle-2.9.6.tar.xz'
    archive.write_bytes(data.getvalue())
    module.SDK_SHA256 = hashlib.sha256(data.getvalue()).hexdigest()
    flat = tmp_path / 'Sparkle.framework/Sparkle'
    flat.parent.mkdir()
    flat.write_bytes(b'changed flattened SDK')
    def attached(app, sdk, config, project):
        assert sdk != tmp_path
        path = sdk / 'Sparkle.framework/Sparkle'
        assert path.is_symlink() and path.read_bytes() == content
        return 'checked'
    module._attach_verified = attached
    assert module.attach(tmp_path / 'candidate.app', tmp_path, {}, tmp_path) == 'checked'
    assert flat.read_bytes() == b'changed flattened SDK'
    archive.write_bytes(b'wrong archive')
    with pytest.raises(ValueError, match='checksum'):
        module.attach(tmp_path / 'candidate.app', tmp_path, {}, tmp_path)
