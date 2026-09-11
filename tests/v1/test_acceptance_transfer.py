from pathlib import Path
import runpy
import pytest
from .test_publisher import prepared
from knowledge_distiller.v1.feishu_inbox import FeishuInbox,history_message
from .test_feishu_inbox import message
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.publisher import publish

transfer=runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/import_verified_feishu_result.py'))['transfer']


def test_transfer_completed_receipt_preserves_fact_and_is_idempotent(tmp_path):
    root=tmp_path/'source';root.mkdir()
    source,item,vault=prepared(root)
    inbox=FeishuInbox(source,'app-new');inbox.bind(bot_open_id='ou_bot',user_open_id='ou_owner',chat_id='oc_private',start_ms=100000)
    inbox.receive(history_message(message()),history=True)
    with connect(source.path) as db:
        db.execute("UPDATE feishu_receipts SET state='accepted'")
        db.execute('INSERT INTO feishu_parts(app_id,message_id,position,item_id) VALUES (?,?,0,?)',(inbox.app_id,'om_1',item))
    target=Store(tmp_path/'target.sqlite3');target.initialize()
    with pytest.raises(ValueError,match='pending'):transfer(source.path,target.path,'om_1')
    source.mark_succeeded(item)
    copied=transfer(source.path,target.path,'om_1')
    assert target.item_bundle(copied)['snapshot']==source.item_bundle(item)['snapshot']
    assert target.item_bundle(copied)['payload_json']==source.item_bundle(item)['payload_json']
    assert transfer(source.path,target.path,'om_1')==copied
    output=tmp_path/'formal-vault';output.mkdir()
    result=publish(target,copied,output)
    assert (output/result.relative_path).is_file()
    with connect(target.path) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert db.execute('SELECT count(*) FROM distill_items').fetchone()[0]==1
