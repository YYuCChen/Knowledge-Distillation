import re
from knowledge_distiller.v1.component_bootstrap import create_installer


def test_picker_is_narrow_authenticated_and_preserves_cancel(tmp_path,monkeypatch):
    import knowledge_distiller.v1.component_bootstrap as module
    app=create_installer(target=tmp_path/'app',data_root=tmp_path/'data',platform='windows-x86_64',public_key='x',manifest_url='https://example.com')
    client=app.test_client(); body=client.get('/').text
    token=re.search(r'name="token" value="([^"]+)"',body).group(1)
    assert client.post('/picker',json={'kind':'folder_data'}).status_code==403
    assert client.post('/picker',json={'token':token,'kind':'run_command'}).status_code==400
    assert client.post('/picker',json={'token':token,'kind':'folder_data'},headers={'Origin':'https://other'}).status_code==403
    monkeypatch.setattr(module,'pick_path',lambda kind:{'status':'cancelled'})
    assert client.post('/picker',json={'token':token,'kind':'folder_data'}).json=={'status':'cancelled'}
    assert str(tmp_path/'data') in client.get('/').text
    assert not (tmp_path/'data').exists()
    assert 'location.reload' not in body
    for label in ['应用安装位置','知识与设置位置','检查安装位置','准备所需文件','验证安装内容','安装应用','启动检查','完成','打开知识蒸馏器']:
        assert label in body
    assert client.get('/installer-logo.svg').status_code==200


def test_status_get_does_not_create_data(tmp_path):
    app=create_installer(target=tmp_path/'app',data_root=tmp_path/'data',platform='windows-x86_64',public_key='x',manifest_url='https://example.com')
    state=app.test_client().get('/status').json
    assert state['steps']==['pending']*6 and not state['accepted']
    assert not (tmp_path/'data').exists()


def test_acceptance_cleans_owned_attempt_and_projects_real_steps(tmp_path,monkeypatch):
    import json,time
    from types import SimpleNamespace
    import knowledge_distiller.v1.component_bootstrap as module
    from knowledge_distiller.v1.component_attempt import create_attempt,bind_candidate
    from knowledge_distiller.v1.component_install import install as real_install
    from knowledge_distiller.v1.program_tree import identity
    attempts=[]
    class Assembly:
        def __init__(self,*a,**k):pass
        def prepare(self,*a,**k):return {'version':'2','target_identity':'pending'},SimpleNamespace(download_bytes=0)
        def assemble(self,release,plan,root,**kwargs):
            self.cap=create_attempt(root,excluded=[]);attempts.append(root)
            candidate=root/'candidate';(candidate/'_internal').mkdir(parents=True)
            (candidate/'KnowledgeDistiller.exe').write_bytes(b'fixture')
            (candidate/'_internal/windows-version.json').write_text(json.dumps({'version':'2'}))
            release['target_identity']=identity(candidate,'windows-x86_64')
            bind_candidate(self.cap,candidate,release['target_identity'])
            kwargs['event']('prepare',asset='fixture',bytes_done=5,bytes_total=5)
            kwargs['event']('verify')
            return candidate,{}
        def capability_for(self,candidate):return self.cap
    def install(*a,**k):
        return real_install(*a,**k,launcher=lambda *a:SimpleNamespace(poll=lambda:0),
            acceptance=lambda *a:None,activation=lambda *a:None)
    monkeypatch.setattr(module,'ComponentAssembly',Assembly)
    monkeypatch.setattr(module,'install',install)
    monkeypatch.setattr(module,'create_shortcut',lambda *a:{'status':'pending','reason':'fixture shortcut unavailable'})
    manifest=tmp_path/'release.json';manifest.write_text('{}')
    app=create_installer(target=tmp_path/'app',data_root=tmp_path/'data',platform='windows-x86_64',public_key='unused',manifest_url='unused')
    client=app.test_client();token=re.search(r'name="token" value="([^"]+)"',client.get('/').text).group(1)
    def post(action,**kwargs):
        client.post('/',data={'token':token,'action':action,**kwargs})
        deadline=time.monotonic()+3
        while client.get('/status').json['busy']:
            assert time.monotonic()<deadline;time.sleep(.01)
        return client.get('/status').json
    assert post('prepare',target=str(tmp_path/'app'),data_root=str(tmp_path/'data'),manifest_path=str(manifest))['ready']
    result=post('install')
    assert result['accepted'] and result['complete'],result
    assert result['steps']==['complete']*6
    assert not attempts[0].exists()
    assert any('fixture shortcut unavailable' in warning for warning in result['warnings'])
