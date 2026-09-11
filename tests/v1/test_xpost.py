import copy
import json
import shutil

import pytest

from knowledge_distiller.v1.chrome import ChromeSessionError
from knowledge_distiller.v1.domain import Evidence,Knowledge,Point
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.settings import SettingsService
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.worker import SingleWorker
from knowledge_distiller.v1.xpost import XPostSource,XPostSourceError,connection_authority,qualify_post,xpost_identity
from .test_xiaohongshu import image

KEY='2095890075447964086';URL='https://x.com/person/status/'+KEY
CANONICAL='https://x.com/i/status/'+KEY


def tweet(key=KEY,text='第一段\n第二段。',media=None):
    legacy={'id_str':key,'full_text':text,'entities':{}}
    if media is not None:legacy['extended_entities']={'media':media}
    return {'__typename':'Tweet','rest_id':key,'legacy':legacy}


def response(*posts):
    return {'contextId':'context','loggedIn':True,'requestedId':KEY,'raw':{'data':{
        'threaded_conversation_with_injections_v2':{'instructions':[{'entries':[
            {'content':{'itemContent':{'tweet_results':{'result':p}}}} for p in posts]}]}}}}


@pytest.fixture
def store(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    store.save_connection('x',None,browser_context='context')
    return store


@pytest.mark.parametrize('url',[URL,CANONICAL,'https://twitter.com/person/status/'+KEY+'?s=20'])
def test_exact_post_identity(url):assert xpost_identity(url)==(KEY,CANONICAL)


@pytest.mark.parametrize('url',['https://x.com/person','https://x.com/i/lists/123',
    'https://x.com/person/article/123','https://x.com.evil.test/person/status/'+KEY,
    'https://user:secret@x.com/person/status/'+KEY,URL+'/photo/1'])
def test_excluded_locators(url):
    with pytest.raises(ValueError):xpost_identity(url)


def test_selects_submitted_post_not_parent_and_excludes_quote():
    target=tweet();target['quoted_status_result']={'result':tweet('999','不得引入引用正文')}
    state=response(tweet('123','父帖'),target,tweet('456','回复'))
    _,text,media=qualify_post(state,KEY)
    assert text=='第一段\n第二段。' and media==[]


@pytest.mark.parametrize('media_type',['video','animated_gif'])
def test_rejects_video_and_gif_instead_of_using_cover(media_type):
    with pytest.raises(XPostSourceError,match='input_unsupported'):
        qualify_post(response(tweet(media=[{'type':media_type,'media_url_https':'https://pbs.twimg.com/media/cover.jpg'}])),KEY)


@pytest.mark.parametrize('field',['article','retweeted_status_result'])
def test_rejects_native_article_and_nested_only_retweet(field):
    post=tweet();post[field]={'result':{'text':'非主帖'}}
    with pytest.raises(XPostSourceError):qualify_post(response(post),KEY)


def test_long_post_uses_complete_note_text_and_rejects_truncated_fallback():
    post=tweet(text='截断…');post['legacy']['truncated']=True
    with pytest.raises(XPostSourceError):qualify_post(response(post),KEY)
    post['note_tweet']={'note_tweet_results':{'result':{'text':'完整\n长帖正文'}}}
    assert qualify_post(response(post),KEY)[1]=='完整\n长帖正文'


def test_missing_raw_media_types_is_not_text_only_success():
    post=tweet();post['legacy']['entities']['media']=[{'type':'photo'}]
    with pytest.raises(XPostSourceError,match='media_incomplete'):qualify_post(response(post),KEY)


class Session:
    def __init__(self,state):self.state=state;self.calls=0
    def verify(self):return 'context'
    def read(self,url,context):
        assert url==CANONICAL and context=='context'
        self.calls+=1;return copy.deepcopy(self.state)


def test_image_pipeline_reuses_source_fact_media_model_and_publisher(store,image,tmp_path):
    post=tweet(media=[{'type':'photo','media_url_https':'https://pbs.twimg.com/media/test.png'}])
    session=Session(response(post))
    def download(url,path):
        assert 'name=orig' in url
        shutil.copyfile(image,path)
    adapter=XPostSource(store,session,downloader=download)
    class Model:
        def derive(self,snapshot,uncertainties):
            assert snapshot.startswith('第一段\n第二段。')
            return Knowledge('标题','副标题','摘要',(Point('p1','观点','论证',('e1',)),),(),(Evidence('e1',snapshot.index('图片文字'),snapshot.index('图片文字')+4,'图片文字'),))
    vault=tmp_path/'vault';vault.mkdir()
    service=Distiller(store=store,source=None,normalizer=None,recognizer=None,reviewer=None,confirmation_clipper=None,
        knowledge_model=Model(),runtime_root=tmp_path/'runtime',vault=vault,xpost_source=adapter,ocr=FakeOcr())
    client=create_app(store,service,SettingsService(store,xpost=session)).test_client()
    assert client.post('/submissions',data={'content':URL}).status_code==302
    SingleWorker(store,service).run_one();row=store.item_bundle(1)
    assert row['state']=='succeeded',row['error_code']
    assert row['snapshot']=='第一段\n第二段。\n\n[图片 image-1 OCR]\n图片文字'
    assert row['source_kind']=='x'
    assert 'X' in client.get('/').get_data(as_text=True)
    assert len(list((vault/'知识蒸馏器').glob('附件/*/*.png')))==0
    assert not (tmp_path/'runtime/items/1/x-media').exists()


def test_bound_connection_change_prevents_capture(store,tmp_path):
    authority=connection_authority(store.connection('x'))
    store.save_connection('x',None,browser_context='replacement')
    with pytest.raises(ChromeSessionError,match='connection_changed'):
        XPostSource(store,Session(response(tweet()))).capture(URL,tmp_path,expected_authority=authority)


def test_last_image_failure_cannot_create_partial_source(store,image,tmp_path):
    media=[{'type':'photo','media_url_https':'https://pbs.twimg.com/media/'+x+'.png'} for x in ['one','two']]
    def download(url,path):
        if '/two.' in url:raise XPostSourceError('x_media_incomplete')
        shutil.copyfile(image,path)
    adapter=XPostSource(store,Session(response(tweet(media=media))),downloader=download)
    with pytest.raises(XPostSourceError):adapter.capture(URL,tmp_path/'attempt',expected_authority=connection_authority(store.connection('x')))
    assert not (tmp_path/'attempt/x-media').exists()

from .test_xiaohongshu import FakeOcr
