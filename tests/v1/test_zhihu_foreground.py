import json
from pathlib import Path
import shutil
import subprocess

import pytest

from knowledge_distiller.v1.chrome import ChromeSessionError
from knowledge_distiller.v1.settings import SettingsService
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.zhihu_session import ZhihuForegroundSession


class Owned:
    def __init__(self):self.discarded=[];self.reads=[]
    def discard(self,context):self.discarded.append(context)
    def close(self):pass
    def read(self,url,context):self.reads.append((url,context));return {'contextId':context}


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'test.sqlite3');store.initialize()
    owned=Owned();calls=[]
    def reader(*args):
        calls.append(args)
        return {'loggedIn':True,'contextId':'daily-context','body':'native'}
    session=ZhihuForegroundSession(store,tmp_path/'profiles',owned=owned,reader=reader)
    return store,session,owned,calls,SettingsService(store,zhihu=session)


def test_connect_is_explicit_success_changes_generation_and_other_platforms_stay(setup):
    store,session,owned,calls,settings=setup
    store.save_connection('zhihu',None,browser_context='owned:'+'a'*32)
    store.save_connection('x',None,browser_context='owned:'+'b'*32)
    before=dict(store.connection('zhihu'));other=dict(store.connection('x'))
    assert settings.view()['platforms'][3]['connection_mode']=='owned'
    assert dict(store.connection('zhihu'))==before
    settings.connect_zhihu()
    row=store.connection('zhihu')
    assert row['browser_context']=='foreground:daily-context'
    assert row['generation']==before['generation']+1
    assert dict(store.connection('x'))==other
    assert owned.discarded==[before['browser_context']]
    assert calls==[('zhihu','zhihu','https://www.zhihu.com/',None)]
    assert settings.view()['platforms'][3]['state']=='connected'
    assert settings.view()['platforms'][3]['connection_mode']=='foreground'


@pytest.mark.parametrize('code',['zhihu_browser_unavailable','zhihu_login_required','zhihu_source_unavailable'])
def test_failed_foreground_verification_preserves_old_owned_connection(setup,code):
    store,session,owned,calls,settings=setup
    store.save_connection('zhihu',None,browser_context='owned:'+'a'*32)
    before=dict(store.connection('zhihu'))
    def fail(*args):raise ChromeSessionError(code)
    session.reader=fail
    with pytest.raises(ChromeSessionError,match=code):settings.connect_zhihu()
    assert dict(store.connection('zhihu'))==before
    assert owned.discarded==[]


def test_save_failure_keeps_old_profile(setup,monkeypatch):
    store,session,owned,calls,settings=setup
    store.save_connection('zhihu',None,browser_context='owned:'+'a'*32)
    before=dict(store.connection('zhihu'))
    def fail(*args,**kwargs):raise RuntimeError('disk failure')
    monkeypatch.setattr(store,'save_connection',fail)
    with pytest.raises(RuntimeError,match='disk failure'):settings.connect_zhihu()
    assert dict(store.connection('zhihu'))==before
    assert owned.discarded==[]


def test_read_dispatch_preserves_owned_until_explicit_switch(setup):
    store,session,owned,calls,settings=setup
    context='owned:'+'a'*32
    store.save_connection('zhihu',None,browser_context=context)
    session.read('url',context)
    assert owned.reads==[('url',context)] and not calls
    settings.connect_zhihu();calls.clear()
    result=session.read('url','foreground:daily-context')
    assert result['contextId']=='foreground:daily-context'
    assert calls==[('zhihu','zhihu','url','daily-context')]
    with pytest.raises(ChromeSessionError,match='connection_changed'):session.read('url',context)


@pytest.mark.parametrize('failed',[False,True])
def test_clear_during_read_never_relabels_cleared_connection(setup,failed):
    store,session,owned,calls,settings=setup
    settings.connect_zhihu()
    def read(*args):
        store.clear_connection('zhihu')
        if failed:raise ChromeSessionError('zhihu_login_required')
        return {'contextId':'daily-context'}
    session.reader=read
    with pytest.raises(ChromeSessionError,match='connection_changed'):session.read('url')
    assert store.connection('zhihu')['state']=='unconfigured'


def test_same_context_reconnected_during_read_rejects_stale_generation(setup):
    store,session,owned,calls,settings=setup
    settings.connect_zhihu()
    def read(*args):
        store.save_connection('zhihu',None,browser_context='foreground:daily-context')
        return {'contextId':'daily-context'}
    session.reader=read
    with pytest.raises(ChromeSessionError,match='connection_changed'):session.read('url')


def test_mismatched_browser_result_is_rejected(setup):
    store,session,owned,calls,settings=setup
    settings.connect_zhihu();session.reader=lambda *args:{'contextId':'other'}
    with pytest.raises(ChromeSessionError,match='connection_changed'):session.read('url')


def test_foreground_capture_qualifies_native_payload_with_bound_authority(setup,tmp_path):
    from knowledge_distiller.v1.zhihu import ZhihuSource,connection_authority
    store,session,owned,calls,settings=setup
    settings.connect_zhihu()
    url='https://zhuanlan.zhihu.com/p/683929346'
    native={'id':683929346,'type':'article','title':'原文标题','content':'<p>完整正文</p>'}
    session.reader=lambda *args:{'loggedIn':True,'contextId':'daily-context','requestedId':'683929346',
        'kind':'article','format':'initial_state','pageUrl':url,
        'body':json.dumps({'initialState':{'entities':{'articles':{'683929346':native}}}})}
    authority=connection_authority(store.connection('zhihu'))
    capture=ZhihuSource(store,session).capture(url,tmp_path/'unused',expected_authority=authority)
    assert capture.source_key=='article:683929346'
    assert capture.metadata['original_description']=='原文标题\n\n完整正文'
    assert capture.metadata['session_authority']==authority
    assert capture.metadata['session_authority']['browser_context']=='foreground:daily-context'


def test_clearing_foreground_does_not_delete_daily_browser_or_other_platform(setup):
    store,session,owned,calls,settings=setup
    settings.connect_zhihu();settings.clear_zhihu()
    assert store.connection('zhihu')['state']=='unconfigured'
    assert owned.discarded==[]


def test_settings_reconnects_and_keeps_failed_connection(setup):
    from knowledge_distiller.v1.web import create_app
    store,session,owned,calls,settings=setup
    store.save_connection('zhihu',None,browser_context='owned:'+'a'*32)
    client=create_app(store,None,settings).test_client()
    html=client.get('/settings').get_data(as_text=True)
    assert '重新连接' in html
    response=client.post('/settings/zhihu/connect',follow_redirects=True)
    html=response.get_data(as_text=True)
    assert '知乎已连接。' in html and '重新连接' in html
    assert '切换为前台读取' not in html


def test_only_zhihu_old_reader_requests_foreground_window(tmp_path):
    if not shutil.which('node'):pytest.skip('Node runtime unavailable')
    root=tmp_path/'fake-opencli';base=root/'dist/src/browser';base.mkdir(parents=True)
    (root/'package.json').write_text('{"type":"module"}')
    (base/'page.js').write_text('export class Page {constructor(...args){this.args=args}}')
    (base/'daemon-transport.js').write_text('export async function fetchDaemonStatus(){return {extensionConnected:true,contextId:"daily"}}')
    module=(Path(__file__).parents[2]/'src/knowledge_distiller/v1/adapters/reader-page.mjs').as_uri()
    code=f'''import {{readerPage}} from {json.dumps(module)};
const results=[];for(const p of ['zhihu','x','weibo','xiaohongshu']){{const r=await readerPage({json.dumps(str(root))},p,'daily');results.push(r.page.args[3]);}}
console.log(JSON.stringify(results));'''
    result=subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True,timeout=15,check=True)
    assert json.loads(result.stdout)==['foreground','background','background','foreground']
