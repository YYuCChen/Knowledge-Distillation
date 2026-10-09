import json
from types import SimpleNamespace
from unittest.mock import Mock

from .test_feishu_inbox import inbox, message
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import history_message
from knowledge_distiller.v1.feishu_actions import FeishuActions
from knowledge_distiller.v1.feishu_intake import FeishuIntake


def setup_action(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/101',receipt_key=(inbox.app_id,'om_1',0))
    with connect(inbox.store.path) as db:
        db.execute("UPDATE feishu_receipts SET card_id='om_card'")
    inbox.store.mark_waiting(item,{'token':'pending-1','snapshot':'原文','concerns':[{'audio_name':'c1'}]})
    token=json.loads(inbox.store.item_bundle(item)['confirmation_json'])['token']
    payload={'event':{'operator':{'open_id':'ou_owner'},'context':{'open_chat_id':'oc_private','open_message_id':'om_card'},
                     'action':{'value':{'kind':'source_confirmation','item_id':item,'token':token,'concern_id':'c1',
                                        'action':'manual'},'form_value':{'correction':'核对文字'}}}}
    return item,payload


def test_replayed_card_after_save_does_not_resolve_twice(inbox):
    item,payload=setup_action(inbox)
    engine=SimpleNamespace(resolve=Mock(side_effect=lambda *a,**k: inbox.store.resolve_confirmation(
        item,inbox.store.item_bundle(item)['confirmation_json'],next_confirmation={'snapshot':'核对文字','concerns':[]})))
    actions=FeishuActions(inbox,None,engine)
    assert actions.handle(payload)['toast']['type']=='success'
    assert actions.handle(payload)['toast']['type']=='info'
    engine.resolve.assert_called_once_with(item,'manual','核对文字',token=payload['event']['action']['value']['token'],concern_id='c1',actor='feishu')


def test_other_sender_card_or_item_cannot_act(inbox):
    item,payload=setup_action(inbox)
    engine=SimpleNamespace(resolve=Mock())
    actions=FeishuActions(inbox,None,engine)
    payload['event']['operator']['open_id']='ou_other'
    assert actions.handle(payload)['toast']['type']=='error'
    payload['event']['operator']['open_id']='ou_owner'
    payload['event']['context']['open_message_id']='om_other'
    assert actions.handle(payload)['toast']['type']=='error'
    payload['event']['context']['open_message_id']='om_card'
    payload['event']['action']['value']['item_id']=inbox.store.create_item('https://www.douyin.com/video/102')
    assert actions.handle(payload)['toast']['type']=='error'
    engine.resolve.assert_not_called()


def test_desktop_saved_confirmation_closes_old_feishu_button(inbox):
    item,payload=setup_action(inbox)
    row=inbox.store.item_bundle(item)
    inbox.store.resolve_confirmation(item,row['confirmation_json'],next_confirmation={'concerns':[],'snapshot':'电脑确认'})
    engine=SimpleNamespace(resolve=Mock())
    assert FeishuActions(inbox,None,engine).handle(payload)['toast']['type']=='info'
    engine.resolve.assert_not_called()


def test_choice_is_durable_and_conflicting_late_click_cannot_change_it(inbox):
    import pytest
    inbox.receive(history_message(message(text='正文\nhttps://example.test/reference')),history=True)
    intake=FeishuIntake(inbox,None)
    assert intake.process('om_1')=='waiting_input'
    intake.choose('om_1','text')
    with pytest.raises(ValueError):intake.choose('om_1','links')
    assert FeishuIntake(inbox,None).process('om_1')=='accepted'
    assert inbox.store.item_bundle(1)['input_kind']=='direct_text'


def test_callback_durably_receives_without_running_slow_action_and_replays(inbox):
    from knowledge_distiller.v1.feishu_action_queue import ActionQueue
    _,payload=setup_action(inbox)
    calls=[]
    queue=ActionQueue(inbox,SimpleNamespace(handle=lambda value:(calls.append(value) or {'toast':{'type':'success','content':'已保存。'}})))
    assert queue.receive(payload)['toast']['content']=='已接收，正在保存；请查看卡片结果。'
    assert calls==[]
    queue.receive(payload)
    queue.process_one()
    assert len(calls)==1
    assert queue.receive(payload)['toast']['content']=='已保存。'
    assert queue.process_one() is False


def test_malformed_or_failed_action_does_not_strand_next_action(inbox):
    from knowledge_distiller.v1.feishu_action_queue import ActionQueue
    _,payload=setup_action(inbox)
    calls=[]
    def handle(value):
        calls.append(value)
        if len(calls)==1:raise RuntimeError('simulated failure')
        return {'toast':{'type':'success','content':'已保存。'}}
    queue=ActionQueue(inbox,SimpleNamespace(handle=handle))
    malformed=json.loads(json.dumps(payload));malformed['event']['action']='invalid'
    assert queue.receive(malformed)['toast']['type']=='error'
    queue.receive(payload)
    other=json.loads(json.dumps(payload));other['event']['action']['form_value']['correction']='下一项'
    queue.receive(other)
    assert queue.process_one() and queue.process_one()
    assert len(calls)==2
    assert '已接收' in queue.receive(payload)['toast']['content']
    assert queue.process_one()


def test_concern_draft_survives_other_concern_token_update(inbox):
    from knowledge_distiller.v1.confirmation_revision import revision
    from knowledge_distiller.v1.feishu_views import write
    item,payload=setup_action(inbox)
    original='原句'*500
    inbox.store.mark_waiting(item,{'snapshot':original,'concerns':[{'audio_name':'c1','text':original,'candidates':[original]}]})
    row=inbox.store.item_bundle(item);pending=json.loads(row['confirmation_json'])
    rev=revision(pending,pending['concerns'][0])
    write(inbox,'om_1',{'token':rev,'page':0,'edits':{'0':'修改后的第一段'}})
    value=payload['event']['action']['value']
    value.update(token=pending['token'],concern_revision=rev,action='submit_draft')
    inbox.store.update_confirmation_suggestions(item,row['confirmation_json'],pending)
    engine=SimpleNamespace(resolve=Mock())
    result=FeishuActions(inbox,None,engine).handle(payload)
    assert result['toast']['type']=='success'
    assert engine.resolve.call_args.args[2]=='修改后的第一段'+original[800:]


def test_busy_database_returns_prompt_retry_without_false_receipt(inbox):
    import sqlite3,time
    from knowledge_distiller.v1.feishu_action_queue import ActionQueue
    _,payload=setup_action(inbox)
    blocker=sqlite3.connect(inbox.store.path)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        started=time.monotonic()
        result=ActionQueue(inbox,None).receive(payload)
        assert time.monotonic()-started<1.5
        assert result['toast']['type']=='error'
    finally:blocker.rollback();blocker.close()
    with connect(inbox.store.path) as db:
        assert db.execute('SELECT count(*) FROM feishu_action_queue').fetchone()[0]==0


def test_group_replay_reaches_shared_ledger_before_stale_token_rejection(inbox):
    item,payload=setup_action(inbox)
    payload['event']['action']['value']={
        'kind':'group_confirmation','item_id':item,'token':'previous-successful-token',
        'action':'keep','request_id':'recorded-request','group_id':'recorded-group',
        'group_revision':'recorded-revision','selected_member_uids':['member-a']}
    engine=SimpleNamespace(resolve_group=Mock())
    assert FeishuActions(inbox,None,engine).handle(payload)['toast']['type']=='success'
    engine.resolve_group.assert_called_once_with(item,'keep','',token='previous-successful-token',
        request_id='recorded-request',group_id='recorded-group',group_revision='recorded-revision',
        selected_member_uids=['member-a'],actor='feishu')
