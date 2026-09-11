import hashlib
import json
from pathlib import Path
import runpy
import sys

import pytest
from knowledge_distiller.v1.docling_source import _bundled_artifacts, DoclingSourceError


def test_frozen_documents_require_bundled_models_not_user_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    with pytest.raises(DoclingSourceError):
        _bundled_artifacts()
    model = tmp_path/'docling-models'
    model.mkdir()
    (model/'manifest.json').write_text('{}')
    assert _bundled_artifacts() == model


def test_build_refuses_incomplete_or_changed_model_assets(tmp_path):
    collect = runpy.run_path(str(Path(__file__).resolve().parents[2]/'packaging/docling_models.py'))['model_datas']
    families = ['docling-project--docling-layout-heron','docling-project--docling-models',
                'docling-project--CodeFormulaV2','RapidOcr']
    files = {}
    for family in families:
        path = tmp_path/family/'weights'
        path.parent.mkdir()
        path.write_bytes(b'weight bytes')
        files[str(path.relative_to(tmp_path))]={'size':12,'sha256':hashlib.sha256(b'weight bytes').hexdigest()}
    manifest = tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'docling_version':'2.126.0','files':files}))
    assert len(collect(tmp_path)) == 5
    path.write_bytes(b'changed data')
    with pytest.raises(ValueError, match='checksum'):
        collect(tmp_path)
    path.unlink()
    with pytest.raises(ValueError, match='Missing'):
        collect(tmp_path)
    files.pop(str(path.relative_to(tmp_path)))
    manifest.write_text(json.dumps({'docling_version':'2.126.0','files':files}))
    with pytest.raises(ValueError, match='families'):
        collect(tmp_path)


def test_archive_step_rejects_an_old_app_without_docling_models(tmp_path):
    import subprocess
    app = tmp_path/'old.app'
    binary = app/'Contents/MacOS/KnowledgeDistiller'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'old bundle')
    output = tmp_path/'release'
    script = Path(__file__).resolve().parents[2]/'scripts/package_mac.py'
    result = subprocess.run([sys.executable,str(script),'--app',str(app),
                             '--output',str(output),'--version','test'],capture_output=True,text=True)
    assert result.returncode != 0
    assert 'docling-models' in result.stderr
    assert not output.exists()
