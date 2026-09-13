import hashlib
import json
import zipfile

import pytest

from knowledge_distiller.v1.docling_component import DoclingComponent, DoclingComponentError


def fixture(tmp_path):
    source = tmp_path / 'old-app-models'
    (source / 'family').mkdir(parents=True)
    data = b'fixed model bytes'
    (source / 'family/model.bin').write_bytes(data)
    manifest = {'docling_version': '2.126.0', 'files': {'family/model.bin': {
        'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}
    return source, DoclingComponent(tmp_path / 'components', manifest=manifest)


def test_import_checks_bytes_without_trusting_source_manifest(tmp_path):
    source, component = fixture(tmp_path)
    (source / 'manifest.json').write_text('{"files":{}}')
    target = component.import_existing(source)
    assert target == component.active
    assert component.verify() == target
    assert json.loads((target / 'manifest.json').read_text()) == component.manifest
    (source / 'family/model.bin').write_bytes(b'changed old application')
    assert component.verify() == target
    assert not list(component.root.glob('.import-*'))


def test_corruption_does_not_activate_or_modify_source(tmp_path):
    source, component = fixture(tmp_path)
    (source / 'family/model.bin').write_bytes(b'corrupt')
    with pytest.raises(DoclingComponentError, match='corrupt'):
        component.import_existing(source)
    assert not component.active.exists()
    assert (source / 'family/model.bin').read_bytes() == b'corrupt'


def test_failed_copy_keeps_previous_component_and_cleans_stage(tmp_path, monkeypatch):
    source, component = fixture(tmp_path)
    old = component.root / 'previous-version'
    old.mkdir(parents=True)
    (old / 'model').write_bytes(b'keep')
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr('knowledge_distiller.v1.docling_component.shutil.copyfileobj', fail)
    with pytest.raises(OSError, match='disk full'):
        component.import_existing(source)
    assert not component.active.exists()
    assert (old / 'model').read_bytes() == b'keep'
    assert not list(component.root.glob('.import-*'))


def test_symlink_model_is_rejected(tmp_path):
    source, component = fixture(tmp_path)
    model = source / 'family/model.bin'
    outside = tmp_path / 'outside'
    model.rename(outside)
    try:
        model.symlink_to(outside)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(DoclingComponentError, match='unsafe_path'):
        component.import_existing(source)


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'a\\b', 'C:drive', 'a//b'])
def test_invalid_inventory_paths(tmp_path, name):
    with pytest.raises(DoclingComponentError, match='invalid_inventory'):
        DoclingComponent(tmp_path, manifest={'files': {name: {'size': 0, 'sha256': '0'*64}}})


@pytest.mark.parametrize('bad', [None, 'extra', 'duplicate', 'corrupt'])
def test_archive_activation_accepts_only_trusted_bytes(tmp_path, bad):
    source, component = fixture(tmp_path)
    archive = tmp_path / 'component.zip'
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr('family/model.bin', b'bad' if bad == 'corrupt' else
                        (source / 'family/model.bin').read_bytes())
        if bad == 'extra':
            target.writestr('../escape', b'bad')
        if bad == 'duplicate':
            target.writestr('family/model.bin', b'bad')
    if bad:
        with pytest.raises(DoclingComponentError):
            component.import_archive(archive)
        assert not component.active.exists()
    else:
        assert component.import_archive(archive) == component.active
        assert component.verify() == component.active
    assert not list(component.root.glob('.import-*'))
