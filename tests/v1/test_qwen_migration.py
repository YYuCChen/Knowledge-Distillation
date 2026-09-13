import hashlib
import json
import threading

import pytest

from knowledge_distiller.v1 import qwen_component as module


@pytest.fixture
def legacy(tmp_path,monkeypatch):
    component=module.QwenComponent(tmp_path/'qwen')
    monkeypatch.setattr(module.QwenComponent,'supported',property(lambda _:True))
    monkeypatch.setattr(module.QwenComponent,'windows',property(lambda _:False))
    model=component.active/'model';model.mkdir(parents=True)
    data={'config.json':b'{}','model.safetensors':b'synthetic weights'}
    for name,value in data.items():(model/name).write_bytes(value)
    python=component.python_path(component.active);python.parent.mkdir(parents=True);python.write_bytes(b'python')
    version,revision=component.identity()
    original={'version':version,'revision':revision,'files':{k:len(v) for k,v in data.items()}}
    (component.active/'component.json').write_text(json.dumps(original))
    assets=tmp_path/'assets';assets.mkdir()
    (assets/'qwen-model-manifests.json').write_text(json.dumps({'mac':{'revision':revision,'files':{
        name:{'size':len(value),'sha256':hashlib.sha256(value).hexdigest()} for name,value in data.items()}}}))
    monkeypatch.setattr(module,'ASSETS',assets)
    monkeypatch.setattr(component,'_probe_python',lambda *a,**k:{'version':module.PYTHON_VERSION,'implementation':'CPython'})
    monkeypatch.setattr(component,'_dependencies_match',lambda:True)
    monkeypatch.setattr(component,'_verify_runtime',lambda root:None)
    monkeypatch.setattr(component,'_download_python',lambda _:pytest.fail('migration cannot download'))
    return component,original


def finish(component):
    component._thread.join(3)
    assert not component._thread.is_alive()


def test_legacy_qualification_is_async_locked_and_preserves_original(legacy,monkeypatch):
    component,original=legacy
    entered,release=threading.Event(),threading.Event()
    def verify(root):
        entered.set();assert release.wait(2)
    monkeypatch.setattr(component,'_verify_runtime',verify)
    component.begin_legacy_validation()
    assert entered.wait(1)
    first_thread=component._thread
    component.begin_legacy_validation()
    assert component._thread is first_thread
    assert component.status()['state']=='validating_existing'
    assert module._read(component.active/'component.json')==original
    release.set();finish(component)
    assert component.ready()
    assert module._read(component.active/'component.json')['migration']=='offline-legacy-v1'
    assert [json.loads(p.read_text()) for p in (component.root/'legacy-manifests').glob('*.json')]==[original]


@pytest.mark.parametrize('failure',['same_size_corruption','dependencies','selftest','concurrent_manifest'])
def test_failed_qualification_never_marks_old_component_ready(legacy,monkeypatch,failure):
    component,original=legacy
    if failure=='same_size_corruption':
        p=component.active/'model/model.safetensors';p.write_bytes(b'x'*p.stat().st_size)
    elif failure=='dependencies':monkeypatch.setattr(component,'_dependencies_match',lambda:False)
    elif failure=='selftest':
        def verify(root):raise module.ComponentError('selftest_failed')
        monkeypatch.setattr(component,'_verify_runtime',verify)
    else:
        def verify(root):(component.active/'component.json').write_text(json.dumps({**original,'concurrent':'change'}))
        monkeypatch.setattr(component,'_verify_runtime',verify)
    component.begin_legacy_validation();finish(component)
    after=module._read(component.active/'component.json')
    assert 'self_test_passed' not in after and not component.ready()
    if failure!='concurrent_manifest':assert after==original
    assert (component.active/'model/model.safetensors').exists()
    assert component.status()['state'] in ('failed','needs_upgrade')
