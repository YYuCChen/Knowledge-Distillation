import json
from pathlib import Path
import pytest
from knowledge_distiller.v1.platform_sessions import PLATFORMS, PlatformOwnedSession
from knowledge_distiller.v1.chrome import ChromeSessionError
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.settings import SettingsService
class Secrets:
    def __init__(self):
        self.values = {}
    def __call__(self, account):
        owner = self
        class Secret:
            def save(self, value): owner.values[account] = value
            def load(self):
                if account not in owner.values: raise KeychainError('missing')
                return owner.values[account]
            def clear(self): owner.values.pop(account, None)
            def set_label(self, label): pass
        return Secret()


@pytest.mark.parametrize('platform', list(PLATFORMS))
def test_all_platforms_reuse_only_their_persistent_login(platform,tmp_path,monkeypatch):
    store=Store(tmp_path/'test.sqlite3');store.initialize()
    secrets=Secrets();identity='a'*32
    session=PlatformOwnedSession(store,tmp_path/platform,platform,secret_factory=secrets)
    data=[{'name':'SID','value':'test','domain':'.youtube.com','path':'/'}] if platform=='youtube' else {'test_cookie':'test'}
    secrets(platform+'-session-'+identity).save(json.dumps(data))
    store.save_connection(platform,None,browser_context='owned:'+identity)
    monkeypatch.setattr(session,'_launch',lambda *a,**k:pytest.fail('cookie read must not launch browser'))
    assert session.cookies()==data
    fresh=PlatformOwnedSession(store,tmp_path/platform,platform,secret_factory=secrets)
    assert fresh.cookies()==data
    store.clear_connection(platform)
    with pytest.raises(ChromeSessionError):fresh.cookies()


def test_production_settings_defaults_use_owned_sessions(tmp_path):
    store=Store(tmp_path/'test.sqlite3');store.initialize()
    settings=SettingsService(store)
    for attr in ['youtube','xpost','weibo']:
        assert isinstance(getattr(settings,attr),PlatformOwnedSession)
    from knowledge_distiller.v1.zhihu_session import ZhihuForegroundSession
    assert isinstance(settings.zhihu,ZhihuForegroundSession)
    from knowledge_distiller.v1.foreground_session import PlatformForegroundSession
    assert isinstance(settings.xiaohongshu,PlatformForegroundSession)


@pytest.mark.parametrize('platform',['xiaohongshu','x','zhihu','weibo'])
def test_raw_readers_are_bound_to_owned_endpoint(platform,tmp_path,monkeypatch):
    import knowledge_distiller.v1.platform_sessions as module
    store=Store(tmp_path/'test.sqlite3');store.initialize()
    secrets=Secrets();identity='b'*32
    session=PlatformOwnedSession(store,tmp_path/platform,platform,secret_factory=secrets)
    secrets(platform+'-session-'+identity).save('{"session":"test"}')
    store.save_connection(platform,None,browser_context='owned:'+identity)
    port=tmp_path/'DevToolsActivePort';port.write_text('12345\n/devtools/browser/test\n')
    monkeypatch.setattr(session,'_launch',lambda *a,**k:port)
    calls=[]
    monkeypatch.setattr(module,'read_opencli',lambda *a,**k:calls.append((a,k)) or {'contextId':'owned:'+identity})
    session.read(PLATFORMS[platform][1],'owned:'+identity)
    assert calls[0][1]['endpoint']=='ws://127.0.0.1:12345/devtools/browser/test'
    with pytest.raises(ChromeSessionError):session.read(PLATFORMS[platform][1],'owned:'+'c'*32)
    assert len(calls)==1


def test_clear_during_read_is_connection_change_not_login_failure(tmp_path,monkeypatch):
    import knowledge_distiller.v1.platform_sessions as module
    store=Store(tmp_path/'test.sqlite3');store.initialize()
    secrets=Secrets();identity='c'*32
    session=PlatformOwnedSession(store,tmp_path/'zhihu','zhihu',secret_factory=secrets)
    secrets('zhihu-session-'+identity).save('{"session":"test"}')
    store.save_connection('zhihu',None,browser_context='owned:'+identity)
    port=tmp_path/'port';port.write_text('12345\n/devtools/browser/test\n')
    monkeypatch.setattr(session,'_launch',lambda *a,**k:port)
    def read(*a,**k):
        store.clear_connection('zhihu')
        return {'contextId':'owned:'+identity}
    monkeypatch.setattr(module,'read_opencli',read)
    with pytest.raises(ChromeSessionError,match='zhihu_connection_changed'):
        session.read('https://www.zhihu.com/')
    assert store.connection('zhihu')['state']=='unconfigured'


@pytest.mark.parametrize('platform',['youtube','x'])
def test_manual_login_has_no_debugging_and_requires_confirmation(platform,tmp_path,monkeypatch):
    import subprocess
    from types import SimpleNamespace
    store=Store(tmp_path/'test.sqlite3');store.initialize()
    session=PlatformOwnedSession(store,tmp_path/platform,platform,secret_factory=Secrets())
    calls=[]
    monkeypatch.setattr(subprocess,'Popen',lambda args,**kwargs:calls.append(args) or SimpleNamespace(poll=lambda:0))
    monkeypatch.setattr(session,'_wait_login',lambda *a,**k:pytest.fail('Do not inspect during manual login'))
    with pytest.raises(ChromeSessionError,match=platform+'_login_pending'):
        session.verify()
    assert store.connection(platform) is None
    assert store.setting(platform+'_pending_login')
    assert len(calls)==1
    assert not any(arg.startswith(('--remote-debugging', '--headless', '--enable-automation')) for arg in calls[0])
    session.cancel_login()
    assert not store.setting(platform+'_pending_login')


@pytest.mark.parametrize('vue_uid',[True,False])
def test_weibo_probe_handles_config_without_uid(vue_uid):
    import shutil,subprocess
    if not shutil.which('node'):pytest.skip('Node runtime unavailable')
    probe=PLATFORMS['weibo'][3]
    code='''const calls=[];
globalThis.document={querySelector:()=>({__vue_app__:{config:{globalProperties:{$store:{state:{config:{config:{uid:VUE_UID}}}}}}}})};
globalThis.fetch=async path=>{calls.push(path);if(path==='/ajax/config/get_config')return {ok:true,json:async()=>({ok:1,data:{uid:'123'}})};if(path==='/ajax/profile/info?uid=123')return {ok:true,json:async()=>({data:{user:{id:123}}})};throw Error('wrong endpoint')};
const result=await (PROBE);console.log(JSON.stringify({result,calls}));'''.replace('VUE_UID',"'123'" if vue_uid else 'null').replace('PROBE',probe)
    result=subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True,check=True)
    value=json.loads(result.stdout)
    assert value['result'] is True
    assert value['calls'][-1]=='/ajax/profile/info?uid=123'
    assert ('/ajax/config/get_config' in value['calls']) is not vue_uid
