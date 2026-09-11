import hashlib
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('dual_build',Path(__file__).resolve().parents[1]/'scripts/dual_build.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_corrupt_transfer_preserves_previously_verified_candidate(tmp_path):
    target=tmp_path/'candidate.zip';target.write_bytes(b'previous-good')
    partial=tmp_path/'candidate.zip.part';partial.write_bytes(b'broken')
    expected={'size':len(b'new-good'),'sha256':hashlib.sha256(b'new-good').hexdigest()}
    with pytest.raises(ValueError,match='mismatch'):
        module.accept_download(partial,target,expected)
    assert target.read_bytes()==b'previous-good'
    partial.write_bytes(b'new-good')
    module.accept_download(partial,target,expected)
    assert target.read_bytes()==b'new-good' and not partial.exists()
