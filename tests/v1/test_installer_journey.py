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
