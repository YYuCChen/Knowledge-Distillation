from dataclasses import replace
import json
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.feishu_inbox import FeishuInbox,Message
from knowledge_distiller.v1.feishu_pairing import begin,pending,accept,is_bound


def test_pairing_requires_current_code_private_user_and_does_not_enqueue_control(tmp_path,monkeypatch):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    inbox=FeishuInbox(store,'cli_own')
    config=begin(store,inbox.app_id,'ou_bot')
    m=Message('om_pair','oc_private','ou_owner','user','p2p',1234,'text',json.dumps({'text':config['code']}),(),{})
    assert not accept(inbox,replace(m,chat_type='group'))
    assert not accept(inbox,replace(m,sender_type='app'))
    assert not accept(inbox,replace(m,content=json.dumps({'text':'wrong'})))
    assert not is_bound(inbox)
    # Persisted challenge survives service restart.
    assert pending(Store(store.path))['code']==config['code']
    assert accept(inbox,m)
    assert inbox.binding()['user_open_id']=='ou_owner'
    assert inbox.binding()['start_ms']==1235
    assert not pending(store)
    assert not inbox.pending()
    assert not accept(inbox,replace(m,sender_id='ou_other'))


def test_expired_pairing_is_not_accepted(tmp_path,monkeypatch):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    inbox=FeishuInbox(store,'cli_own')
    config=begin(store,inbox.app_id,'ou_bot')
    monkeypatch.setattr('knowledge_distiller.v1.feishu_pairing.time.time',lambda:config['expires']+1)
    m=Message('om_pair','oc_private','ou_owner','user','p2p',1234,'text',json.dumps({'text':config['code']}),(),{})
    assert not accept(inbox,m) and not is_bound(inbox)
