import json
from pathlib import Path
import subprocess
import sys

import pytest
from knowledge_distiller.v1.adapters import python_policy as policy
from knowledge_distiller.v1 import qwen_component as qc


def test_current_interpreter_contract_rejects_other_patch(monkeypatch):
    monkeypatch.setattr(policy.platform, 'python_version', lambda: '3.13.14')
    with pytest.raises(RuntimeError, match='mismatch'):
        policy.check_current()


@pytest.mark.parametrize('name', ['python312.dll','python313.dll','libpython3.12.dylib','x.cpython-313-darwin.so','x.cp312-win_amd64.pyd'])
def test_bundle_rejects_foreign_binary(tmp_path, name):
    (tmp_path/name).touch()
    with pytest.raises(RuntimeError, match='Foreign'):
        policy.bundle_inventory(tmp_path)


def test_probe_executes_actual_interpreter_and_rejects_false_metadata(tmp_path, monkeypatch):
    component = qc.QwenComponent(tmp_path)
    monkeypatch.setattr(component, 'python_path', lambda root: Path(sys.executable))
    result = component._probe_python(tmp_path)
    assert result['version'] == policy.PYTHON_VERSION
    monkeypatch.setattr(qc.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps({'version':'3.12.10','implementation':'CPython','executable':sys.executable})))
    with pytest.raises(qc.ComponentError, match='version_mismatch'):
        component._probe_python(tmp_path, force=True)


def test_wrong_staging_contract_removes_only_python(tmp_path):
    component = qc.QwenComponent(tmp_path)
    staging = tmp_path/'installing'
    (staging/'python').mkdir(parents=True)
    (staging/'python/python312.dll').write_bytes(b'old')
    (staging/'model').mkdir()
    (staging/'model/unique').write_bytes(b'preserve')
    (staging/'python-ready').write_text('old')
    component._prepare_staging(staging)
    assert not (staging/'python').exists()
    assert (staging/'model/unique').read_bytes() == b'preserve'


def test_activation_rolls_back_when_relocated_runtime_fails(tmp_path, monkeypatch):
    component = qc.QwenComponent(tmp_path)
    component.active.mkdir()
    (component.active/'identity').write_text('old')
    staging = tmp_path/'installing';staging.mkdir()
    (staging/'identity').write_text('new')
    monkeypatch.setattr(component,'_runtime_in_use',lambda root:False)
    monkeypatch.setattr(component,'_verify_runtime',lambda root:(_ for _ in ()).throw(qc.ComponentError('failed')))
    with pytest.raises(qc.ComponentError):component._activate(staging)
    assert (component.active/'identity').read_text() == 'old'
    assert (staging/'identity').read_text() == 'new'


def test_active_process_prevents_replacement(tmp_path, monkeypatch):
    component = qc.QwenComponent(tmp_path);component.active.mkdir()
    staging=tmp_path/'installing';staging.mkdir()
    monkeypatch.setattr(component,'_runtime_in_use',lambda root:True)
    with pytest.raises(qc.ComponentError,match='in_use'):component._activate(staging)
    assert staging.exists() and component.active.exists()


def test_retirement_preserves_unique_model(tmp_path, monkeypatch):
    component=qc.QwenComponent(tmp_path)
    old=tmp_path/'previous-1';(old/'python').mkdir(parents=True);(old/'model').mkdir()
    (old/'component.json').write_text('{"version":"old"}')
    (old/'model/unique').write_text('unique')
    monkeypatch.setattr(component,'_runtime_in_use',lambda root:False)
    component._retire_previous()
    assert not (old/'python').exists() and (old/'model/unique').read_text()=='unique'


def test_project_ci_and_build_gates_follow_single_policy():
    import tomllib
    project=Path(__file__).resolve().parents[2]
    assert tomllib.loads((project/'pyproject.toml').read_text())['project']['requires-python'] == '>='+policy.PYTHON_VERSION+',<3.12'
    for name in ('quick-tests.yml','windows-tests.yml'):
        text=(project/'.github/workflows'/name).read_text()
        assert "src/knowledge_distiller/v1/adapters/python-runtime.json" in text
    for name in ('build_mac.py','build_windows.py'):
        assert 'check_current' in (project/'scripts'/name).read_text()
    for name in ('verify_candidate.py','dual_build.py'):
        assert 'python_policy.py' in (project/'scripts'/name).read_text()
