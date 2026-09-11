from io import BytesIO
import hashlib
import json
import sqlite3

from PIL import Image
import pytest

from knowledge_distiller.v1.domain import CapturedMaterial, SourceFact
from knowledge_distiller.v1.image_confirmation import pending_review, resolve_review, crop_original
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app


@pytest.fixture
def review(tmp_path):
    store=Store(tmp_path/'db.sqlite3');store.initialize()
    path=tmp_path/'media';path.write_bytes(b'fixture')
    captured=CapturedMaterial('fixture','123','https://example.org/123','https://example.org/123',{},path,1)
    item=store.create_item(captured.submitted_url);material=store.attach_material(item,captured)
    output=BytesIO();Image.new('RGB',(240,800),'white').save(output,format='PNG');content=output.getvalue()
    with sqlite3.connect(store.path) as c:
        c.execute('INSERT INTO source_media VALUES (?,?,?,?,?,?)',(material,'image-1',0,'image/png',hashlib.sha256(content).hexdigest(),content))
    fact=SourceFact('甲句\n乙句',tuple({'by':'ocr','status':'unresolved','start':s,'end':s+2,'text':t,'member_id':'image-1'} for s,t in [(0,'甲句'),(3,'乙句')]))
    lineage={'image_ocr':[{'member_id':'image-1','lines':[
        {'start':s,'end':s+2,'text':t,'confidence':.5,'polygon':[[20,y],[200,y],[200,y+20],[20,y+20]]}
        for s,t,y in [(0,'甲句',100),(3,'乙句',140)]]}]}
    return store,item,material,captured,fact,lineage


def confirm_all(store,item):
    while store.item_bundle(item)['state']=='waiting_user':
        row=store.item_bundle(item);pending=json.loads(row['confirmation_json']);concern=pending['concerns'][0]
        resolve_review(store,row,pending,'manual',concern['text'],concern['audio_name'])


def test_legacy_recovery_preserves_frozen_fact_and_reuses_review_revision(review):
    store,item,material,captured,fact,lineage=review
    original=store.establish_source_fact(material,fact,lineage=lineage)
    store.mark_failed(item,'distilling','knowledge_evidence_invalid')
    assert store.prepare_image_review(item)
    row=store.item_bundle(item);revision=row['material_id']
    assert revision!=material and row['source_fact_id'] is None
    pending=json.loads(row['confirmation_json'])
    resolve_review(store,row,pending,'manual','甲句已纠正','ocr-1')
    row=store.item_bundle(item);pending=json.loads(row['confirmation_json'])
    assert pending['concerns'][0]['start']==6
    assert pending['lineage']['image_ocr'][0]['lines'][1]['start']==6
    store=Store(store.path)  # Resume from persisted decisions in a fresh owner.
    confirm_all(store,item)
    row=store.item_bundle(item)
    assert row['snapshot']=='甲句已纠正\n乙句'
    from knowledge_distiller.v1.reading import build_reading
    reading = build_reading(row['snapshot'], json.loads(row['lineage_json']))
    assert reading[0].text == '甲句已纠正'
    assert reading[1].start == 6
    assert reading[1].text == '乙句'
    assert json.loads(row['lineage_json'])['image_ocr'][0]['lines'][0]['original_text']=='甲句'
    with sqlite3.connect(store.path) as c:
        assert c.execute('SELECT snapshot FROM source_facts WHERE source_fact_id=?',(original,)).fetchone()[0]==fact.snapshot
        with pytest.raises(sqlite3.IntegrityError):
            c.execute('DELETE FROM source_facts WHERE source_fact_id=?',(original,))
    again=store.create_item(captured.submitted_url);store.attach_material(again,captured)
    assert store.prepare_image_review(again)
    assert store.item_bundle(again)['material_id']==revision
    assert store.item_bundle(again)['source_fact_id']==row['source_fact_id']


def test_crop_is_local_and_stale_urls_do_not_return_media(review):
    store,item,material,_,fact,lineage=review
    pending=pending_review(fact,lineage);store.mark_waiting(item,pending)
    row=store.item_bundle(item);pending=json.loads(row['confirmation_json'])
    client=create_app(store,object()).test_client()
    url=f"/items/{item}/confirmation-image/ocr-1?token={pending['token']}"
    response=client.get(url)
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    image=Image.open(BytesIO(response.data))
    assert image.height<100 and image.width<240
    assert b'<audio' not in client.get('/').data
    resolve_review(store,row,pending,'manual','甲句','ocr-1')
    assert client.get(url).status_code==404
    assert client.get(f'/items/{item}/confirmation-image/ocr-2?token=wrong').status_code==404


def test_legacy_vision_score_requalification_preserves_original_and_raw_score(review):
    store,item,material,_,fact,lineage=review
    lineage['image_ocr'][0]['engine']='apple_vision'
    uncertainties=tuple({**u,'reason':'图片文字识别置信度较低'} for u in fact.uncertainties)
    original=store.establish_source_fact(material,SourceFact(fact.snapshot,uncertainties),lineage=lineage)
    store.mark_failed(item,'distilling','knowledge_evidence_invalid')
    assert store.prepare_image_review(item)
    row=store.item_bundle(item)
    assert row['state']=='queued' and row['source_fact_id']!=original
    assert all(u['status']=='advisory' for u in json.loads(row['uncertainties_json']))
    assert json.loads(row['lineage_json'])['image_ocr'][0]['lines'][0]['confidence']==.5
    with sqlite3.connect(store.path) as c:
        old=json.loads(c.execute('SELECT uncertainties_json FROM source_facts WHERE source_fact_id=?',(original,)).fetchone()[0])
        assert all(u['status']=='unresolved' for u in old)
