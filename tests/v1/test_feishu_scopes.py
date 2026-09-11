from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from .test_collections import setup
from .test_feishu_inbox import message
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import FeishuInbox,history_message
from knowledge_distiller.v1.feishu_intake import FeishuIntake
from knowledge_distiller.v1.feishu_cards import FeishuCards
from knowledge_distiller.v1.feishu_scopes import request,items
from knowledge_distiller.v1.collections import Collections


def scope_receipt(setup):
    store,collections,discovery,root=setup
    inbox=FeishuInbox(store,'app-new')
    inbox.bind(bot_open_id='ou_bot',user_open_id='ou_owner',chat_id='oc_private',start_ms=100000)
    inbox.receive(history_message(message(text='https://www.douyin.com/collection/900')),history=True)
    def submit(url,**kwargs):return collections.preview([url],durable=True)
    intake=FeishuIntake(inbox,SimpleNamespace(submit=submit,collections=collections))
    assert intake.process('om_1')=='waiting_input'
    return inbox,intake,collections,discovery


def scope_command(inbox,collections):
    card=FeishuCards(inbox,None,None,collections).card('om_1')
    return card['body']['elements'][-1]['behaviors'][0]['value']


def test_scope_command_survives_restart_and_maps_member_confirmation(setup):
    inbox,intake,collections,discovery=scope_receipt(setup)
    command=scope_command(inbox,collections)
    request(inbox,'om_1',command)
    restored=Collections(inbox.store,discovery)
    assert restored._draft(command['scope_token'])['durable'] is True
    intake.links.collections=restored
    assert intake.process('om_1')=='accepted'
    assert len(items(inbox,'om_1'))==2
    request(inbox,'om_1',command)
    assert intake.process('om_1')=='accepted'
    with connect(inbox.store.path) as db:
        assert db.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM distill_items').fetchone()[0]==2
    item=items(inbox,'om_1')[0]['item_id']
    inbox.store.mark_waiting(item,{'snapshot':'已核对原文','concerns':[],'review_required':True})
    card=FeishuCards(inbox,None,None,restored).card('om_1')
    assert card['header']['title']['content']=='有内容待你确认'


def test_changed_scope_requires_new_confirmation_and_updates_both_entrypoints(setup):
    inbox,intake,collections,discovery=scope_receipt(setup)
    command=scope_command(inbox,collections)
    request(inbox,'om_1',command)
    discovery.scope=replace(discovery.scope,members=discovery.scope.members[:1])
    assert intake.process('om_1')=='waiting_input'
    fresh=scope_command(inbox,collections)
    assert fresh['scope_token']!=command['scope_token']
    assert items(inbox,'om_1')==[]
    with pytest.raises(ValueError,match='范围已更新'):request(inbox,'om_1',command)
    request(inbox,'om_1',fresh)
    assert intake.process('om_1')=='accepted'
    assert len(items(inbox,'om_1'))==1


def test_scope_request_rejects_other_receipt_token(setup):
    inbox,intake,collections,discovery=scope_receipt(setup)
    command=scope_command(inbox,collections)
    other=collections.preview(['another'],durable=True)
    command['scope_token']=other['token']
    with pytest.raises(ValueError):request(inbox,'om_1',command)
    assert items(inbox,'om_1')==[]
