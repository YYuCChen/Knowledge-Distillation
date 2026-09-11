import copy
import json

import pytest

from knowledge_distiller.v1.zhihu import ZhihuSource,ZhihuSourceError,connection_authority,primary_text,qualify_zhihu,zhihu_identity
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.domain import Evidence,Knowledge,Point
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.worker import SingleWorker

KEY='1234567890123456789';QID='1234567890';URL=f'https://www.zhihu.com/question/{QID}/answer/{KEY}'


def state(kind='answer',key=KEY,content='<p>第一段。<br>第二行。</p><p>第二段。</p>'):
    return {'loggedIn':True,'contextId':'context','requestedId':key,'kind':kind,'body':json.dumps(
        {'id':int(key),'type':kind,'question':{'id':int(QID),'title':'问题标题'},'title':'文章标题','content':content,'content_need_truncated':False},ensure_ascii=False)}


@pytest.mark.parametrize('url,expected',[(URL,('answer',KEY,QID)),('https://www.zhihu.com/answer/'+KEY,('answer',KEY,None)),
    ('https://zhuanlan.zhihu.com/p/'+KEY,('article',KEY,None)),('https://www.zhihu.com/pin/'+KEY,('pin',KEY,None))])
def test_exact_kind_identity(url,expected):assert zhihu_identity(url)==expected


@pytest.mark.parametrize('url',['https://www.zhihu.com/question/'+QID,'https://www.zhihu.com/people/author',
    'https://www.zhihu.com/collection/123','https://www.zhihu.com/zvideo/123',URL.replace('www.zhihu.com','www.zhihu.com.evil.test')])
def test_excluded_locators(url):
    with pytest.raises(ValueError):zhihu_identity(url)


def test_large_ids_stay_exact_and_question_context_must_match():
    _,text,_,url=qualify_zhihu(state(),'answer',KEY,QID)
    assert text=='第一段。\n第二行。\n\n第二段。' and url==URL
    with pytest.raises(ZhihuSourceError):qualify_zhihu(state(),'answer',KEY,'999')


def test_visual_payload_excluded_but_captions_and_text_order_preserved():
    html='<p>前文&amp;原话</p><figure><img src="https://example.test/x.png" alt="不是正文"><figcaption>图的文字说明</figcaption></figure><video><p>视频fallback</p></video><p>后文</p>'
    text=primary_text(html)
    assert text=='前文&原话\n\n图的文字说明\n\n后文'
    assert '不是正文' not in text and 'fallback' not in text


@pytest.mark.parametrize('change',[{'content':None},{'content':'<img src="x">'},{'content_need_truncated':True},{'is_deleted':True},{'id':123}])
def test_no_excerpt_or_incomplete_success(change):
    response=state();data=json.loads(response['body']);data.update(change);response['body']=json.dumps(data)
    with pytest.raises(ZhihuSourceError):qualify_zhihu(response,'answer',KEY)


def test_pin_keeps_every_primary_text_member_in_order():
    response=state('pin',content=[{'type':'text','content':'<p>一</p>'},{'type':'image','url':'unused'},
        {'type':'text','content':'<p>二</p>'},{'type':'quote','content':'引用不可混入'}])
    assert qualify_zhihu(response,'pin',KEY)[1]=='一\n\n二'


def test_article_title_and_body_are_preserved():
    assert qualify_zhihu(state('article'),'article',KEY)[1]=='文章标题\n\n第一段。\n第二行。\n\n第二段。'


def test_platform_restriction_preserves_connected_identity(tmp_path):
    from knowledge_distiller.v1.chrome import ChromeSessionError
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    store.save_connection('zhihu',None,browser_context='context')
    before=dict(store.connection('zhihu'))
    class Session:
        def read(self,url,context):raise ChromeSessionError('zhihu_source_unavailable')
    with pytest.raises(ChromeSessionError,match='zhihu_source_unavailable'):
        ZhihuSource(store,Session()).capture(URL,tmp_path/'capture',expected_authority=connection_authority(before))
    assert dict(store.connection('zhihu'))==before
    assert not (tmp_path/'capture').exists()


def test_text_only_single_worker_uses_no_audio_or_images(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize();store.save_connection('zhihu',None,browser_context='context')
    class Session:
        def read(self,url,context):assert (url,context)==(URL,'context');return state()
    class Model:
        def derive(self,snapshot,uncertainties):
            assert snapshot=='第一段。\n第二行。\n\n第二段。'
            return Knowledge('标题','副标题','摘要',(Point('p1','观点','论证',('e1',)),),(),(Evidence('e1',0,4,'第一段。'),))
    vault=tmp_path/'vault';vault.mkdir()
    service=Distiller(store=store,source=None,normalizer=None,recognizer=None,reviewer=None,confirmation_clipper=None,
        knowledge_model=Model(),runtime_root=tmp_path/'runtime',vault=vault,zhihu_source=ZhihuSource(store,Session()))
    item=store.create_item(URL);SingleWorker(store,service).run_one();row=store.item_bundle(item)
    assert row['state']=='succeeded',row['error_code']
    assert row['source_key']=='answer:'+KEY
    assert json.loads(row['metadata_json'])['scope']=='TEXT_ONLY/VISUAL_EXCLUDED'
    assert '图片与视频不在此来源范围内' in (vault/row['published_path']).read_text()


def test_article_initial_state_requires_exact_key_route_and_full_body():
    native=json.loads(state('article')['body'])
    response={'loggedIn':True,'requestedId':KEY,'kind':'article','format':'initial_state',
        'pageUrl':'https://zhuanlan.zhihu.com/p/'+KEY,'body':json.dumps({'initialState':{'entities':{'articles':{KEY:native}}}})}
    assert qualify_zhihu(response,'article',KEY)[1].startswith('文章标题\n\n')
    response['pageUrl']='https://zhuanlan.zhihu.com/p/123'
    with pytest.raises(ZhihuSourceError):qualify_zhihu(response,'article',KEY)
    response['pageUrl']='https://zhuanlan.zhihu.com/p/'+KEY
    native['contentNeedTruncated']=True
    response['body']=json.dumps({'initialState':{'entities':{'articles':{KEY:native}}}})
    with pytest.raises(ZhihuSourceError):qualify_zhihu(response,'article',KEY)


def test_pin_nested_or_app_only_payload_does_not_become_primary_text():
    for change in ({'source_pin_id':123},{'is_guide_app':True}):
        response=state('pin',content=[{'type':'text','content':'正文'}]);native=json.loads(response['body'])
        native.update(change);response['body']=json.dumps(native)
        with pytest.raises(ZhihuSourceError):qualify_zhihu(response,'pin',KEY)


def test_zhihu_setting_intake_and_reconnect_are_platform_bound(tmp_path):
    from knowledge_distiller.v1.web import create_app
    from knowledge_distiller.v1.settings import SettingsService
    store=Store(tmp_path/'isolated.sqlite3')
    class Browser:
        def verify(self):return 'context'
    client=create_app(store,None,SettingsService(store,zhihu=Browser())).test_client()
    assert client.post('/submissions',data={'content':URL}).status_code==400
    assert client.post('/settings/zhihu/connect').status_code==302
    assert client.post('/submissions',data={'content':URL}).status_code==302
    assert json.loads(store.item_bundle(1)['platform_authority_json'])==connection_authority(store.connection('zhihu'))
    assert client.post('/settings/zhihu/clear').status_code==302
    assert store.connection('zhihu')['state']=='unconfigured'
