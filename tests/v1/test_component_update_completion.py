"""An accepted helper run does not become an installation failure on UI errors."""
import json
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace


def test_helper_open_failure_keeps_accepted_exit_and_safe_message(tmp_path,monkeypatch):
    from knowledge_distiller.v1 import component_update_helper as module
    root=tmp_path/'data';(root/'updates').mkdir(parents=True)
    (root/'updates/component-release.json').write_bytes(b'fixture')
    (root/'.desktop-instance.json').write_text(json.dumps({'pid':123,'port':48999}))
    target=tmp_path/'app';(target/'Contents').mkdir(parents=True);(target/'_internal').mkdir()
    (target/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleVersion':'1'}))
    (target/'_internal/windows-version.json').write_text(json.dumps({'version':'1'}))
    adapters=tmp_path/'module/adapters';adapters.mkdir(parents=True)
    (adapters/'update_config.json').write_text(json.dumps({'public_key':'fixture'}))
    monkeypatch.setattr(module,'__file__',str(tmp_path/'module/helper.py'))
    class Assembly:
        def __init__(self,*a,**k):pass
        def prepare(self,*a,**k):return {'version':'2','target_identity':'fixture'},None
        def assemble(self,*a,**k):return tmp_path/'candidate',{}
        def capability_for(self,*a):return None
    outcome={'accepted':True,'activation':{'status':'ready'},'warnings':[]}
    monkeypatch.setattr(module,'ComponentAssembly',Assembly)
    monkeypatch.setattr(module,'request_exit',lambda *a:None)
    monkeypatch.setattr(module,'install',lambda *a,**k:outcome)
    monkeypatch.setattr(module,'finalize_install',lambda result,**k:result)
    monkeypatch.setattr(module,'confirm_activation',lambda *a:None)
    def fail(*a):raise OSError('fixture browser unavailable')
    monkeypatch.setattr(module.webbrowser,'open',fail)
    plan=tmp_path/'plan.json';plan.write_text(json.dumps({'data_root':str(root),'info':{'bundle':str(target)},'version':'2'}))
    assert module.run(plan)==0
    message=(root/'updates/install-error.txt').read_text(encoding='utf-8')
    assert '安装已接受' in message and '程序未被替换' not in message
