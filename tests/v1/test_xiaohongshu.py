import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from knowledge_distiller.v1.chrome import ChromeSessionError
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.domain import Evidence, Knowledge, Point, SourceFact
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.xiaohongshu import (
    XiaohongshuSource, XiaohongshuSourceError, connection_authority,
    qualify_note, xiaohongshu_identity,
)

KEY = '123456789012345678901234'
URL = f'https://www.xiaohongshu.com/explore/{KEY}?xsec_token=route&xsec_source=pc_feed'


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    store.save_connection('xiaohongshu', None, browser_context='test-context')
    return store


@pytest.fixture
def state():
    return {'loggedIn': True, 'pageUrl': URL, 'contextId': 'test-context', 'note': {
        'noteId': KEY, 'type': 'normal', 'title': ' 原始标题 ', 'desc': '\n第一段\r\n第二段\n',
        'imageList': [{'fileId': 'one', 'urlDefault': 'https://sns-img.xhscdn.com/one'},
                      {'fileId': 'two', 'urlDefault': 'https://sns-img.xhscdn.com/two'}]}}


@pytest.fixture
def image(tmp_path):
    path = tmp_path / 'image.png'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32',
                    '-frames:v', '1', str(path)], check=True)
    return path


class Session:
    def __init__(self, state): self.state = state; self.calls = 0
    def read(self, url, context):
        self.calls += 1
        assert url == URL and context == 'test-context'
        return copy.deepcopy(self.state)


def source(store, state, image, download=None):
    return XiaohongshuSource(store, Session(state),
        downloader=download or (lambda url, path: shutil.copyfile(image, path)))


def capture(source, store, root):
    return source.capture(URL, root, expected_authority=connection_authority(store.connection('xiaohongshu')))


@pytest.mark.parametrize('url', [URL, f'https://www.xiaohongshu.com/discovery/item/{KEY}',
    f'https://www.xiaohongshu.com/search_result/{KEY}'])
def test_exact_identity_excludes_route_token(url):
    assert xiaohongshu_identity(url) == (KEY, f'https://www.xiaohongshu.com/explore/{KEY}')


@pytest.mark.parametrize('url', ['https://www.xiaohongshu.com/user/profile/'+KEY,
    'https://www.xiaohongshu.com/explore', URL.replace('www.xiaohongshu.com', 'www.xiaohongshu.com.evil.test'),
    URL.replace('www.', 'user:secret@www.'), URL.replace(KEY, 'invalid')])
def test_non_note_inputs_fail(url):
    with pytest.raises(ValueError): xiaohongshu_identity(url)


@pytest.mark.parametrize('mutation', [lambda s:s.update(loggedIn=False),
    lambda s:s.update(loginPrompt=True), lambda s:s['note'].update(noteId='other'),
    lambda s:s['note'].update(type='long'), lambda s:s['note'].update(isLongNote=True),
    lambda s:s['note'].update(desc=None), lambda s:s['note']['imageList'][1].update(urlDefault=''),
    lambda s:s['note']['imageList'][1].update(urlDefault='http://127.0.0.1/private')])
def test_rejects_incomplete_or_wrong_note(state, mutation):
    mutation(state)
    with pytest.raises((ChromeSessionError, XiaohongshuSourceError)): qualify_note(state, KEY)


def test_complete_capture_preserves_text_order_and_actual_encoding(store, state, image, tmp_path):
    captured = capture(source(store, state, image), store, tmp_path / 'attempt')
    assert captured.metadata['original_description'] == state['note']['desc']
    assert captured.metadata['source_title'] == state['note']['title']
    assert [m.member_id for m in captured.members] == ['image-1', 'image-2']
    assert all(m.mime_type == 'image/png' for m in captured.members)
    assert 'xhscdn' not in json.dumps(captured.metadata)


def test_missing_last_image_cannot_attach_partial_source(store, state, image, tmp_path):
    def download(url, path):
        if url.endswith('/two'): raise XiaohongshuSourceError('xiaohongshu_media_incomplete')
        shutil.copyfile(image, path)
    with pytest.raises(XiaohongshuSourceError):
        capture(source(store, state, image, download), store, tmp_path / 'attempt')
    assert not (tmp_path / 'attempt/xhs-media').exists()
    with connect(store.path) as db: assert db.execute('SELECT COUNT(*) FROM source_facts').fetchone()[0] == 0


def test_generation_change_during_capture_fails(store, state, image, tmp_path):
    def download(url, path):
        shutil.copyfile(image, path)
        store.save_connection('xiaohongshu', None, browser_context='new-context')
    with pytest.raises(ChromeSessionError, match='connection_changed'):
        capture(source(store, state, image, download), store, tmp_path / 'attempt')


def test_reuse_checks_hash_expiry_and_frozen_authority(store, state, image, tmp_path):
    adapter = source(store, state, image)
    captured = capture(adapter, store, tmp_path / 'attempt')
    options = dict(source_key=KEY, submitted_url=URL, canonical_url=captured.canonical_url,
        metadata=captured.metadata, work_dir=tmp_path/'attempt', expected_authority=connection_authority(store.connection('xiaohongshu')))
    assert adapter.reuse_retained(**options).members == captured.members
    expired = {**captured.metadata, 'captured_at': (datetime.now(UTC)-timedelta(hours=73)).isoformat()}
    assert adapter.reuse_retained(**{**options, 'metadata': expired}) is None
    captured.members[0].path.write_bytes(b'bad media')
    with pytest.raises(XiaohongshuSourceError): adapter.reuse_retained(**options)


def test_fact_media_are_atomic_immutable_and_generation_checked(store, state, image, tmp_path):
    item = store.create_item(URL)
    captured = capture(source(store, state, image), store, tmp_path/'attempt')
    material = store.attach_material(item, captured)
    store.save_connection('xiaohongshu', None, browser_context='new-context')
    with pytest.raises(ChromeSessionError): store.establish_source_fact(material, SourceFact('正文'))
    with connect(store.path) as db: assert db.execute('SELECT COUNT(*) FROM source_facts').fetchone()[0] == 0


def test_single_worker_image_fact_knowledge_and_no_clobber_publish(store, state, image, tmp_path):
    class Model:
        def derive(self, snapshot, uncertainties):
            assert snapshot.startswith(state['note']['title']+'\n\n'+state['note']['desc'])
            return Knowledge('标题', '不同副标题', '这是一段摘要',
                (Point('p1','图片观点','图中红色方块支持这一描述',('e1',)),), (),
                (Evidence('e1',snapshot.index('图片文字'),snapshot.index('图片文字')+4,'图片文字'),))
    vault = tmp_path/'vault'; vault.mkdir()
    service = Distiller(store=store, source=None, normalizer=None, recognizer=None, reviewer=None,
        confirmation_clipper=None, knowledge_model=Model(), runtime_root=tmp_path/'runtime', vault=vault,
        xiaohongshu_source=source(store,state,image), ocr=FakeOcr())
    item=store.create_item(URL)
    from knowledge_distiller.v1.worker import SingleWorker
    SingleWorker(store,service).run_one()
    assert store.item_bundle(item)['state'] == 'succeeded'
    row=store.item_bundle(item)
    assert len(store.media_members(row['material_id'])) == 2
    publication=vault/row['published_path']
    content=publication.read_bytes()
    assert b'^image-1' in content
    assert len(list(publication.parent.glob('附件/*/*.png'))) == 0
    for command in ('UPDATE source_media SET content = x\'00\'', 'DELETE FROM source_media'):
        with pytest.raises(sqlite3.IntegrityError), connect(store.path) as db: db.execute(command)
    assert service.run(store.create_item(URL)).state == 'succeeded'
    assert publication.read_bytes() == content
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_facts').fetchone()[0] == 1
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_media_write_failure_rolls_back_material_and_item(store, state, image, tmp_path):
    captured = capture(source(store,state,image),store,tmp_path/'attempt')
    item = store.create_item(URL)
    with connect(store.path) as db:
        db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON source_media WHEN NEW.position=1 BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): store.attach_material(item,captured)
    assert store.item_bundle(item)['material_id'] is None
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM materials').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_media').fetchone()[0] == 0


def test_http_intake_preserves_route_and_settings_binding(store, state):
    from knowledge_distiller.v1.web import create_app
    from knowledge_distiller.v1.settings import SettingsService
    class Browser:
        def verify(self): return 'test-context'
        def read(self, url, context):
            assert url == URL and context == 'test-context'
            return state
    client=create_app(store,None,SettingsService(store,xiaohongshu=Browser())).test_client()
    client.post('/settings/xiaohongshu/clear')
    response=client.post('/submissions',data={'content':URL})
    assert response.status_code == 400
    assert '小红书' in response.get_data(as_text=True)
    assert client.post('/settings/xiaohongshu/connect').status_code == 302
    response=client.post('/submissions',data={'content':URL})
    assert response.status_code == 302
    row=store.item_bundle(1)
    assert row['submitted_url'] == URL
    assert row['submitted_title'] == '原始标题'
    assert '原始标题' in client.get('/').text
    assert json.loads(row['platform_authority_json']) == connection_authority(store.connection('xiaohongshu'))
    store.mark_failed(1,'collecting','xiaohongshu_upstream_failed')
    store.save_connection('xiaohongshu',None,browser_context='new-context')
    store.retry_item(1)
    assert json.loads(store.item_bundle(1)['platform_authority_json'])['browser_context'] == 'new-context'


def test_video_uses_same_worker_and_retains_native_text_and_timed_evidence(store, state, tmp_path):
    from knowledge_distiller.primary import FFmpegAudioNormalizer
    from knowledge_distiller.v1.worker import SingleWorker
    from .test_vertical_smoke import Recognizer, Reviewer, UnusedClipper
    video=tmp_path/'fixture.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=32x32:r=2',
        '-f','lavfi','-i','sine=frequency=400','-t','2','-c:v','libx264','-c:a','aac',str(video)],check=True)
    state['note'].update(type='video',video={'media':{'video':{'drmType':0},'stream':{'EF4':[
        {'format':'mp4','duration':2000,'audioDuration':2000,'size':video.stat().st_size,'masterUrl':'https://sns-video.xhscdn.com/video'}]}}})
    class Model:
        def derive(self,snapshot,uncertainties):
            text='持续切换会带来额外损耗。';start=snapshot.index(text)
            assert snapshot.startswith(state['note']['title']+'\n\n'+state['note']['desc'])
            return Knowledge('标题','副标题','完整摘要',(Point('p1','观点','论证',('e1',)),),(),
                (Evidence('e1',start,start+len(text),text),))
    vault=tmp_path/'vault';vault.mkdir()
    service=Distiller(store=store,source=None,normalizer=FFmpegAudioNormalizer(),recognizer=Recognizer(),reviewer=Reviewer(),
        confirmation_clipper=UnusedClipper(),knowledge_model=Model(),runtime_root=tmp_path/'runtime',vault=vault,
        xiaohongshu_source=source(store,state,video))
    item=store.create_item(URL);SingleWorker(store,service).run_one()
    assert store.item_bundle(item)['state'] == 'succeeded'
    row=store.item_bundle(item)
    assert row['state']=='succeeded',row['error_code']
    evidence=json.loads(row['payload_json'])['evidence'][0]
    assert evidence['member_id']=='video-1'
    assert 0 <= evidence['start_seconds'] < evidence['end_seconds'] <= 2.1
    assert len(list((vault/'知识蒸馏器').glob('附件/*/*.mp4'))) == 0


def test_unknown_image_member_is_rejected_by_model_parser():
    from knowledge_distiller.v1.knowledge_model import parse_knowledge,KnowledgeModelError
    payload={'qualified':True,'title':'标题','subtitle':'副标题','summary':'摘要',
        'core_points':[{'id':'p1','statement':'观点','argument':'论证','evidence_ids':['e1']}],
        'other_points':[],'evidence':[{'id':'e1','member_id':'image-2','text':'可见依据'}]}
    with pytest.raises(KnowledgeModelError): parse_knowledge('正文',json.dumps(payload),image_ids={'image-1'})


def test_expired_candidate_media_are_removed_but_formal_media_survive(store,state,image,tmp_path):
    captured=capture(source(store,state,image),store,tmp_path/'attempt')
    captured.metadata['captured_at']=(datetime.now(UTC)-timedelta(hours=73)).isoformat()
    item=store.create_item(URL);material=store.attach_material(item,captured)
    store.mark_failed(item,'reviewing','llm_request_failed');store.expire_platform_media()
    assert store.media_members(material)
    store.dismiss_item(item);store.expire_platform_media()
    assert store.media_members(material)==[]
    from knowledge_distiller.v1.source_versions import SourceVersionError
    with pytest.raises(SourceVersionError,match='source_capture_expired'): store.establish_source_fact(material,SourceFact('正文'))


def test_shortlink_is_bound_at_intake_and_resolved_inside_capture(store,state,image,tmp_path):
    from knowledge_distiller.v1.xiaohongshu import xiaohongshu_input
    short='https://xhslink.com/a/abc123'
    assert xiaohongshu_input(short)==(None,None)
    class ShortSession:
        def read(self,url,context):
            assert url==short and context=='test-context'
            return state
    item=store.create_item(short)
    adapter=XiaohongshuSource(store,ShortSession(),downloader=lambda url,path:shutil.copyfile(image,path))
    cap=adapter.capture(short,tmp_path/'attempt',expected_authority=json.loads(store.item_bundle(item)['platform_authority_json']))
    assert cap.source_key==KEY and cap.submitted_url==short


def test_v8_migration_preserves_existing_facts_and_connections(tmp_path,image):
    from knowledge_distiller.v1.domain import CapturedMaterial
    path=tmp_path/'v8.sqlite3';store=Store(path);store.initialize()
    store.save_connection('douyin','原连接')
    item=store.create_item('https://www.douyin.com/video/123')
    material=store.attach_material(item,CapturedMaterial('douyin','123','https://www.douyin.com/video/123',
        'https://www.douyin.com/video/123',{},image,1))
    fact=store.establish_source_fact(material,SourceFact('原有来源正文'))
    with connect(path) as db:
        for table in ('media_lifecycle','feishu_parts','feishu_receipts','feishu_binding','collection_previews','collection_confirmations','collection_events','collection_results','collection_members','collection_operations'):
            db.execute('DROP TABLE '+table)
        db.execute('DROP TABLE source_media')
        db.execute('ALTER TABLE source_connections DROP COLUMN browser_context')
        db.execute('PRAGMA user_version=8')
    store.initialize()
    assert store.item_bundle(item)['snapshot']=='原有来源正文'
    assert store.item_bundle(item)['source_fact_id']==fact
    assert store.connection('douyin')['account_label']=='原连接'
    assert store.connection('douyin')['browser_context'] is None
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==18
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]


class FakeOcr:
    def recognize_bytes(self, content, mime):
        from knowledge_distiller.v1.ocr import OcrLine, OcrResult
        return OcrResult(64,64,(OcrLine('图片文字',((0,0),(60,0),(60,20),(0,20)),.99),))


def test_ocr_line_failure_retains_diagnostics_and_completed_image_across_retry(store,state,image,tmp_path):
    from PIL import Image
    from knowledge_distiller.v1.ocr import OcrResult, validate_lines
    class Ocr:
        calls = 0
        def recognize_bytes(self, content, mime):
            self.calls += 1
            polygon = ((-1 if self.calls == 2 else 0,0),(30,0),(30,20),(0,20))
            return OcrResult(32,32,validate_lines(['已识别的文字'],
                [.4 if self.calls == 3 else .99],[polygon],32,32))
    def download(url,path):
        Image.new('RGB',(32,32),'red' if url.endswith('one') else 'blue').save(path,'PNG')
    ocr = Ocr()
    service = Distiller(store=store,source=None,normalizer=None,recognizer=None,reviewer=None,
        confirmation_clipper=None,knowledge_model=None,runtime_root=tmp_path/'runtime',vault=tmp_path,
        xiaohongshu_source=source(store,state,image,download),ocr=ocr)
    item = store.create_item(URL)
    assert service.run(item).state == 'failed'
    directory = tmp_path/'runtime/items'/str(item)
    diagnostic = json.loads((directory/'ocr-diagnostic.json').read_text())
    assert diagnostic['line_diagnostics'][0]['text_available']
    assert not diagnostic['line_diagnostics'][0]['evidence_valid']
    assert diagnostic['completed_images'][0]['lines'][0]['text'] == '已识别的文字'
    assert len(list((directory/'ocr').glob('*.json'))) == 1
    assert store.item_bundle(item)['source_fact_id'] is None
    store.retry_item(item)
    assert service.run(item).state == 'waiting_user'
    assert ocr.calls == 3  # Restart recognizes only the unfinished second image.


def test_ocr_failure_retry_and_low_confidence_preserve_source_boundaries(store,state,image,tmp_path):
    from knowledge_distiller.v1.ocr import OcrError, OcrLine, OcrResult
    class Ocr:
        fail = True
        def recognize_bytes(self, content, mime):
            if self.fail: raise OcrError('ocr_model_unavailable')
            return OcrResult(64,64,(OcrLine('无法确认的文字',((0,0),(60,0),(60,20),(0,20)),.4),))
    class Model:
        def derive(self,snapshot,uncertainties):
            start=snapshot.index('无法确认的文字')
            return Knowledge('标题','副标题','摘要',(Point('p1','观点','论证',('e1',)),),(),
                             (Evidence('e1',start,start+7,'无法确认的文字'),))
    ocr=Ocr()
    service=Distiller(store=store,source=None,normalizer=None,recognizer=None,reviewer=None,
        confirmation_clipper=None,knowledge_model=Model(),runtime_root=tmp_path/'runtime',vault=tmp_path,
        xiaohongshu_source=source(store,state,image),ocr=ocr)
    item=store.create_item(URL)
    assert service.run(item).state=='failed'
    row=store.item_bundle(item)
    assert row['error_code']=='ocr_model_unavailable' and row['source_fact_id'] is None
    store.retry_item(item);ocr.fail=False
    assert service.run(item).state=='waiting_user'
    row=store.item_bundle(item)
    assert row['source_fact_id'] is None and row['knowledge_result_id'] is None
    pending=json.loads(row['confirmation_json'])
    assert pending['kind']=='image' and len(pending['concerns'])==2
    first_token=pending['token']
    while row['state']=='waiting_user':
        pending=json.loads(row['confirmation_json']);concern=pending['concerns'][0]
        service.resolve(item,'manual',concern['text'],token=pending['token'],concern_id=concern['audio_name'])
        row=store.item_bundle(item)
    with pytest.raises(ValueError):
        service.resolve(item,'manual','过期修改',token=first_token,concern_id='ocr-1')
    assert service.run(item).state=='succeeded'
    row=store.item_bundle(item)
    assert len(json.loads(row['lineage_json'])['image_ocr'])==2
    assert all(u.get('status')!='unresolved' for u in json.loads(row['uncertainties_json']))


def test_restricted_preview_keeps_input_and_never_queues(store):
    from knowledge_distiller.v1.web import create_app
    from knowledge_distiller.v1.settings import SettingsService
    class Browser:
        def read(self, *args): raise ChromeSessionError('xiaohongshu_security_restricted')
    client = create_app(store, None, SettingsService(store, xiaohongshu=Browser())).test_client()
    response = client.post('/submissions', data={'content': URL})
    assert response.status_code == 400
    assert '切换可靠网络' in response.text
    assert not store.recent_items()


def test_preview_reconnection_cannot_bind_title_to_another_account(store, state):
    from knowledge_distiller.v1.xiaohongshu import preview_title
    class Browser:
        def read(self, *args):
            store.save_connection('xiaohongshu', None, browser_context='changed')
            return state
    with pytest.raises(ChromeSessionError, match='connection_changed'):
        preview_title(store, Browser(), URL)
    assert not store.recent_items()
