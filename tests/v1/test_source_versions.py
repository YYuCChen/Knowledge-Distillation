from knowledge_distiller.v1.database import SCHEMA_VERSION
import json
import sqlite3
from dataclasses import replace

import pytest

from knowledge_distiller.v1.database import SCHEMA, connect
from knowledge_distiller.v1.domain import CapturedMaterial, SourceFact, Knowledge, Point, Evidence
from knowledge_distiller.v1.publisher import publish
from knowledge_distiller.v1.store import Store

URL='https://www.douyin.com/video/123'


def capture(tmp_path, name, text, content=b'original-media', **metadata):
    path=tmp_path/name;path.write_bytes(content)
    return CapturedMaterial('douyin','123',URL,URL,{'original_description':text,**metadata},path,1)


def knowledge(text):
    return Knowledge('来源观点','来源的观点与条件','摘要',(Point('p1','来源观点','论证',('e1',)),),(),(Evidence('e1',0,len(text),text),))


def establish(store, cap, text):
    item=store.create_item(URL);material=store.attach_material(item,cap)
    fact=store.establish_source_fact(material,SourceFact(text))
    store.establish_knowledge(fact,knowledge(text))
    return item,material,fact


def test_changed_source_creates_new_capture_without_rebinding_old_fact_or_file(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize();vault=tmp_path/'vault';vault.mkdir()
    cap=capture(tmp_path,'first.mp4','原始描述')
    first,old_material,old_fact=establish(store,cap,'第一版原文')
    publish(store,first,vault);old_path=vault/store.item_bundle(first)['published_path'];old_bytes=old_path.read_bytes()
    new_cap=capture(tmp_path,'second.mp4','编辑后的描述',b'changed-media')
    second,new_material,new_fact=establish(store,new_cap,'第二版原文')
    publish(store,second,vault)
    assert new_material!=old_material and new_fact!=old_fact
    assert store.item_bundle(first)['source_key']==store.item_bundle(second)['source_key']=='123'
    assert store.item_bundle(first)['snapshot']=='第一版原文'
    assert store.item_bundle(second)['snapshot']=='第二版原文'
    assert old_path.read_bytes()==old_bytes
    assert store.item_bundle(first)['published_path']!=store.item_bundle(second)['published_path']
    replay=store.create_item(URL);assert store.attach_material(replay,replace(cap,metadata={**cap.metadata,'captured_at':'later','author':{'display_name':'新昵称'}}))==old_material
    assert store.item_bundle(replay)['source_fact_id']==old_fact


def test_same_body_changed_media_is_a_new_snapshot(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    a=store.attach_material(store.create_item(URL),capture(tmp_path,'one.mp4','同一描述',b'one'))
    b=store.attach_material(store.create_item(URL),capture(tmp_path,'two.mp4','同一描述',b'two'))
    assert a!=b


def test_bound_source_cannot_be_replaced_or_mutated(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    item,material,fact=establish(store,capture(tmp_path,'one.mp4','原描述'),'原文')
    with pytest.raises(ValueError,match='source_snapshot_changed'):
        store.attach_material(item,capture(tmp_path,'two.mp4','新描述',b'two'))
    with connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError,match='metadata is immutable'):
            db.execute("UPDATE materials SET metadata_json='{}' WHERE material_id=?",(material,))
        with pytest.raises(sqlite3.IntegrityError,match='identity is immutable'):
            db.execute("UPDATE materials SET source_key='456' WHERE material_id=?",(material,))
    assert store.item_bundle(item)['source_fact_id']==fact


def as_v9(path):
    # Exact old parent table, retaining all v9 child tables and source references.
    db=sqlite3.connect(path);db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        for table in ('media_lifecycle','feishu_parts','feishu_receipts','feishu_binding','collection_previews','collection_confirmations','collection_events','collection_results','collection_members','collection_operations'):
            db.execute('DROP TABLE '+table)
        db.execute('DROP TRIGGER source_media_no_update')
        db.execute("""CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media
            WHEN EXISTS(SELECT 1 FROM source_facts WHERE material_id=OLD.material_id)
            BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END""")
        sql='CREATE TABLE materials_v9 '+SCHEMA.split('CREATE TABLE materials ',1)[1].split(';',1)[0]
        db.execute(sql)
        db.execute('INSERT INTO materials_v9 SELECT material_id,source_kind,source_key,submitted_url,canonical_url,metadata_json,created_at FROM materials')
        db.execute('DROP TABLE materials');db.execute('ALTER TABLE materials_v9 RENAME TO materials')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=9');db.commit()
    finally:db.close()


def test_v9_upgrade_preserves_published_fact_and_media_references(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize();vault=tmp_path/'vault';vault.mkdir()
    item=store.create_item(URL);material=store.attach_material(item,capture(tmp_path,'old.mp4','原始描述'))
    with connect(store.path) as db:
        db.execute("INSERT INTO source_media VALUES (?,'image-1',0,'image/png','preserved',X'010203')",(material,))
    fact=store.establish_source_fact(material,SourceFact('原始事实'));store.establish_knowledge(fact,knowledge('原始事实'))
    publish(store,item,vault);before=dict(store.item_bundle(item));path=vault/before['published_path'];content=path.read_bytes()
    as_v9(store.path);store.initialize()
    after=store.item_bundle(item)
    for key in ('material_id','source_fact_id','knowledge_result_id','snapshot','published_path','published_vault'):
        assert after[key]==before[key]
    assert path.read_bytes()==content
    with connect(store.path) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert db.execute('SELECT material_id,content FROM source_media').fetchone()[:]==(material,b'\x01\x02\x03')
        assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert db.execute('PRAGMA user_version').fetchone()[0]== SCHEMA_VERSION
        assert db.execute('SELECT snapshot_key FROM materials').fetchone()[0]=='legacy'
    # A new capture can coexist; a legacy row isn't fabricated into a new hash.
    new=store.attach_material(store.create_item(URL),capture(tmp_path,'new.mp4','新描述'))
    assert new!=material


def test_broken_foreign_key_rolls_back_entire_v9_migration(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize();as_v9(store.path)
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO source_media VALUES (999,'image-1',0,'image/png','hash',X'00')")
    with pytest.raises(RuntimeError,match='broken source references'):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==9
        assert 'snapshot_key' not in {r[1] for r in db.execute('PRAGMA table_info(materials)')}
        assert db.execute("SELECT name FROM sqlite_master WHERE name='materials_v10'").fetchone() is None


def test_pending_confirmation_remains_bound_to_original_capture(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    item=store.create_item(URL);material=store.attach_material(item,capture(tmp_path,'old.mp4','原描述'))
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET state='waiting_user', confirmation_json='{}' WHERE item_id=?",(item,))
    with pytest.raises(ValueError,match='source_snapshot_changed'):
        store.attach_material(item,capture(tmp_path,'new.mp4','新描述',b'new'))
    assert store.item_bundle(item)['material_id']==material
    assert store.item_bundle(item)['confirmation_json']=='{}'
