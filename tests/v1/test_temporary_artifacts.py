from datetime import UTC,datetime,timedelta
import json
from pathlib import Path

import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.temporary_artifacts import TemporaryArtifacts,MARKER
from knowledge_distiller.v1.domain import CapturedMaterial,SourceFact
from knowledge_distiller.v1.database import connect


def setup(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    item=store.create_item('https://www.douyin.com/video/123')
    cleaner=TemporaryArtifacts(store,tmp_path/'runtime');cleaner.prepare(item)
    target=tmp_path/'runtime'/'items'/str(item)
    (target/'raw').write_text('临时敏感正文')
    return store,item,cleaner,target


def test_retry_material_survives_age_until_explicit_dismissal(tmp_path):
    store,item,c,target=setup(tmp_path)
    before=(target/MARKER).read_bytes();c.prepare(item)
    assert (target/MARKER).read_bytes()==before
    retained=datetime.fromisoformat(json.loads(before)['retained_at'])
    store.mark_failed(item,'collecting','temporary')
    c.clean_item(item,now=retained+timedelta(hours=71));assert (target/'raw').exists()
    c.clean_item(item,now=retained+timedelta(days=30));assert target.exists()
    store.dismiss_item(item)
    c.clean_item(item,now=retained+timedelta(days=30));assert not target.exists()
    assert store.item_bundle(item)['error_code']=='temporary'


def test_working_and_pending_confirmation_protect_exact_audio(tmp_path):
    store,item,c,target=setup(tmp_path)
    future=datetime.now(UTC)+timedelta(days=4)
    store.mark_working(item,'reviewing');c.clean_item(item,now=future);assert target.exists()
    store.mark_waiting(item,{'snapshot':'待确认正文','concerns':[]})
    store.mark_failed(item,'reviewing','temporary');c.clean_item(item,now=future)
    assert target.exists()


def test_formal_source_cleanup_preserves_fact_media_and_outcome(tmp_path,monkeypatch):
    store,item,c,target=setup(tmp_path)
    material=store.attach_material(item,CapturedMaterial('douyin','123','url','url',{},target/'raw',1))
    fact=store.establish_source_fact(material,SourceFact('正式来源'))
    store.mark_failed(item,'distilling','llm_request_failed')
    import knowledge_distiller.v1.temporary_artifacts as module
    remove=module.shutil.rmtree
    monkeypatch.setattr(module.shutil,'rmtree',lambda path:(_ for _ in ()).throw(PermissionError('private detail')))
    c.clean_item(item)
    assert store.setting(f'temporary_cleanup_item_{item}')=='PermissionError'
    assert store.item_bundle(item)['source_fact_id']==fact and store.item_bundle(item)['error_code']=='llm_request_failed'
    monkeypatch.setattr(module.shutil,'rmtree',remove);c.clean_item(item)
    assert not target.exists() and store.setting(f'temporary_cleanup_item_{item}') is None
    assert store.item_bundle(item)['snapshot']=='正式来源'


def test_symlink_and_other_runtime_are_never_deleted(tmp_path):
    store,item,c,target=setup(tmp_path)
    other=tmp_path/'user-files';other.mkdir();(other/'keep').write_text('用户内容')
    import shutil
    shutil.rmtree(target);target.symlink_to(other,target_is_directory=True)
    c.clean_item(item,now=datetime.now(UTC)+timedelta(days=5))
    assert (other/'keep').read_text()=='用户内容' and target.is_symlink()
    assert store.setting(f'temporary_cleanup_item_{item}')=='OSError'


def test_expired_database_body_cannot_become_fact_and_reacquisition_restores_it(tmp_path):
    from knowledge_distiller.v1.source_versions import SourceVersionError
    store,item,c,target=setup(tmp_path)
    old=(datetime.now(UTC)-timedelta(hours=73)).isoformat()
    cap=CapturedMaterial('douyin','123','url','url',{'original_description':'临时正文','captured_at':old},target/'raw',1)
    material=store.attach_material(item,cap);store.mark_failed(item,'reviewing','temporary')
    c.sweep()
    assert json.loads(store.item_bundle(item)['metadata_json'])['original_description']=='临时正文'
    store.dismiss_item(item)
    c.sweep()
    assert json.loads(store.item_bundle(item)['metadata_json'])=={'temporary_expired':True}
    with pytest.raises(SourceVersionError,match='source_capture_expired'):
        store.establish_source_fact(material,SourceFact('不能凭过期缓存成立'))
    cap.metadata['captured_at']=datetime.now(UTC).isoformat()
    assert store.attach_material(item,cap)==material
    store.establish_source_fact(material,SourceFact('重新采集后成立'))
    c.sweep();assert store.item_bundle(item)['snapshot']=='重新采集后成立'


def test_worker_runs_expiry_while_application_remains_open(tmp_path):
    import threading
    from knowledge_distiller.v1.worker import SingleWorker
    store,item,c,target=setup(tmp_path)
    value=json.loads((target/MARKER).read_text());value['retained_at']=(datetime.now(UTC)-timedelta(hours=73)).isoformat()
    (target/MARKER).write_text(json.dumps(value));store.mark_failed(item,'collecting','temporary')
    ran=threading.Event()
    def maintenance():c.sweep();ran.set()
    class Idle:
        def run(self,item):raise AssertionError('failed item must not run')
    worker=SingleWorker(store,Idle(),maintenance=maintenance)
    worker.start()
    try:
        assert ran.wait(2)
        assert target.exists() and store.item_bundle(item)['state']=='failed'
    finally:worker.stop()


def test_remote_asr_identity_survives_local_expiry_until_cleanup(tmp_path):
    store,item,cleaner,target=setup(tmp_path)
    store.mark_failed(item,'reviewing','asr_runtime_failed')
    state=target/'seed-asr-owned.json'
    state.write_text(json.dumps({'task_id':'owned','cleaned':False}))
    store.dismiss_item(item)
    future=datetime.now(UTC)+timedelta(days=4)
    cleaner.clean_item(item,now=future)
    assert state.exists()
    marker=json.loads((target/MARKER).read_text())
    marker['retained_at']=(datetime.now(UTC)-timedelta(days=4)).isoformat()
    (target/MARKER).write_text(json.dumps(marker))
    cleaner.prepare(item)
    assert state.exists()  # retry can reach the recognizer with its remote identity
    assert store.setting(f'temporary_cleanup_item_{item}')=='OSError'
    state.write_text(json.dumps({'task_id':'owned','cleaned':True}))
    cleaner.clean_item(item,now=future)
    assert not target.exists()


def test_prepare_keeps_old_retry_bytes_and_shared_material_survives_one_dismissal(tmp_path):
    store,item,c,target=setup(tmp_path)
    old=(datetime.now(UTC)-timedelta(days=30)).isoformat()
    marker=json.loads((target/MARKER).read_text());marker['retained_at']=old
    (target/MARKER).write_text(json.dumps(marker))
    capture=CapturedMaterial('douyin','123','url','url',{'captured_at':old,'original_description':'保存正文'},target/'raw',1)
    material=store.attach_material(item,capture)
    other=store.create_item('https://www.douyin.com/video/123')
    store.attach_material(other,capture)
    store.mark_failed(item,'reviewing','asr_unavailable')
    store.mark_failed(other,'reviewing','asr_unavailable')
    c.prepare(item)
    assert (target/'raw').read_text()=='临时敏感正文'
    store.dismiss_item(item);store.expire_platform_media()
    assert json.loads(store.item_bundle(other)['metadata_json'])['original_description']=='保存正文'
    store.dismiss_item(other);store.expire_platform_media()
    assert json.loads(store.item_bundle(other)['metadata_json'])=={'temporary_expired':True}


def test_completed_runtime_is_retained_for_shared_pending_owner(tmp_path):
    store,item,cleaner,target=setup(tmp_path)
    material=store.attach_material(item,CapturedMaterial('douyin','123','url','url',{},target/'raw',1))
    store.establish_source_fact(material,SourceFact('共享事实'))
    store.mark_succeeded(item)
    shared=store.create_item('https://www.douyin.com/video/456')
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET material_id=?,state='waiting_user',confirmation_json='{}' WHERE item_id=?",(material,shared))
    cleaner.clean_item(item,now=datetime.now(UTC)+timedelta(days=30))
    assert (target/'raw').exists()
    store.mark_succeeded(shared)
    cleaner.clean_item(item)
    assert not target.exists()
