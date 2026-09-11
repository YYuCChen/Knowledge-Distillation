import json
from io import BytesIO
from types import SimpleNamespace
from PIL import Image
from .test_feishu_inbox import inbox,message
from knowledge_distiller.v1.feishu_inbox import history_message
from knowledge_distiller.v1.feishu_intake import FeishuIntake


def test_image_and_rich_post_persist_and_replay_without_second_download(inbox):
    out=BytesIO();Image.new('RGB',(40,40),'white').save(out,format='PNG')
    calls=[]
    api=SimpleNamespace(download_message_image=lambda mid,key:(calls.append(key) or out.getvalue()))
    raw=message(msg_type='post',body={'content':json.dumps({'zh_cn':{'title':'来源标题','content':[
        [{'tag':'text','text':'图片说明'},{'tag':'img','image_key':'img_one'}],
        [{'tag':'img','image_key':'img_two'}]]}})})
    row=inbox.receive(history_message(raw),history=True)
    assert row['state']=='received'
    intake=FeishuIntake(inbox,None,api=api)
    assert intake.process('om_1')=='accepted'
    assert intake.process('om_1')=='accepted'
    assert calls==['img_one','img_two']
    source=inbox.store.submitted_source(1)
    assert source.source_kind=='image'
    assert b'img_one' in source.content


def test_image_download_failure_keeps_receipt_and_no_fake_item(inbox):
    from knowledge_distiller.v1.feishu_api import FeishuAPIError
    raw=message(msg_type='image',body={'content':json.dumps({'image_key':'img_one'})})
    inbox.receive(history_message(raw),history=True)
    def fail(*args):raise FeishuAPIError('http_403')
    assert FeishuIntake(inbox,None,api=SimpleNamespace(download_message_image=fail)).process('om_1')=='needs_desktop'
    from knowledge_distiller.v1.database import connect
    with connect(inbox.store.path) as db:
        row=db.execute('SELECT * FROM feishu_receipts').fetchone()
        assert '权限' in row['error']
        assert db.execute('SELECT count(*) FROM distill_items').fetchone()[0]==0


def test_image_source_uses_shared_ocr_review_and_releases_media_only_after_done(inbox):
    from knowledge_distiller.v1.feishu_images import prepare
    from knowledge_distiller.v1.file_sources import parse_submitted_source
    from knowledge_distiller.v1.ocr import OcrLine,OcrResult
    from knowledge_distiller.v1.image_confirmation import resolve_review
    from knowledge_distiller.v1.media_lifecycle import release_completed
    from knowledge_distiller.v1.database import connect
    out=BytesIO();Image.new('RGB',(40,40),'white').save(out,format='PNG')
    source=prepare(inbox.app_id,'om_1',[{'tag':'img','image_key':'img_one'}],
                   SimpleNamespace(download_message_image=lambda *args:out.getvalue()))
    item=inbox.store.submit_source(source)
    parsed=parse_submitted_source(source,ocr=SimpleNamespace(recognize_bytes=lambda *args:OcrResult(40,40,
        (OcrLine('待确认文字',((0,0),(20,0),(20,20),(0,20)),.5),))))
    inbox.store.establish_submitted_fact(item,source,parsed)
    row=inbox.store.item_bundle(item)
    assert row['state']=='waiting_user' and row['source_fact_id'] is None
    assert release_completed(inbox.store.path)==0
    pending=json.loads(row['confirmation_json']);concern=pending['concerns'][0]
    resolve_review(inbox.store,row,pending,'manual','确认文字',concern['audio_name'])
    assert inbox.store.item_bundle(item)['source_fact_id'] is not None
    inbox.store.mark_succeeded(item)
    assert release_completed(inbox.store.path)==len(out.getvalue())
    with connect(inbox.store.path) as db:
        assert db.execute('SELECT length(content) FROM source_media').fetchone()[0]==0


def test_restart_after_local_image_commit_does_not_download_again(inbox):
    from knowledge_distiller.v1.feishu_images import prepare
    from knowledge_distiller.v1.database import connect
    out=BytesIO();Image.new('RGB',(40,40),'white').save(out,format='PNG')
    raw=message(msg_type='image',body={'content':json.dumps({'image_key':'img_one'})})
    inbox.receive(history_message(raw),history=True)
    source=prepare(inbox.app_id,'om_1',[{'tag':'img','image_key':'img_one'}],SimpleNamespace(download_message_image=lambda *args:out.getvalue()))
    item=inbox.store.submit_source(source,receipt_key=(inbox.app_id,'om_1',0))
    def forbidden(*args):raise AssertionError('must use durable local source')
    assert FeishuIntake(inbox,None,api=SimpleNamespace(download_message_image=forbidden)).process('om_1')=='accepted'
    assert inbox.store.item_bundle(item)['state']=='queued'
