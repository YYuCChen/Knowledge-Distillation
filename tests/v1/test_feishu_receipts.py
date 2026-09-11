import time

import pytest

from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_receipts import FeishuReceipts, ReceiptUnknown
from .test_feishu_inbox import inbox, message
from knowledge_distiller.v1.feishu_inbox import history_message


class API:
    def __init__(self):
        self.sent=[];self.patched=[];self.messages=[];self.fail_after_send=False;self.fail_patch=False
    def reply_card(self,parent,card,*,delivery_id):
        self.sent.append(delivery_id)
        self.messages=[{'message_id':'om_reply','chat_id':'oc_private','parent_id':parent,
                        'sender':{'id':'app-new','id_type':'app_id','sender_type':'app'},'msg_type':'interactive'}]
        if self.fail_after_send:raise OSError('response lost')
        return 'om_reply'
    def history(self,params):return {'items':self.messages,'has_more':False}
    def update_card(self,mid,card):
        self.patched.append((mid,card))
        if self.fail_patch:raise OSError('patch failed')


def test_lost_send_response_recovers_original_card_without_requeue(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/101',receipt_key=(inbox.app_id,'om_1',0))
    api=API();api.fail_after_send=True
    receipts=FeishuReceipts(inbox,api)
    with pytest.raises(OSError):receipts.synchronize('om_1',{'text':'已接收'})
    api.fail_after_send=False
    assert FeishuReceipts(inbox,api).synchronize('om_1',{'text':'完成'})=='om_reply'
    assert len(api.sent)==1
    assert len(inbox.store.recent_items())==1
    assert inbox.store.item_bundle(item)['state']=='queued'


def test_failed_card_update_preserves_saved_result_and_retries_only_patch(inbox):
    inbox.receive(history_message(message()),history=True)
    api=API();receipts=FeishuReceipts(inbox,api)
    receipts.synchronize('om_1',{'text':'已接收'})
    api.fail_patch=True
    with pytest.raises(OSError):receipts.synchronize('om_1',{'text':'完成'})
    api.fail_patch=False
    receipts.synchronize('om_1',{'text':'完成'})
    assert len(api.sent)==1
    count=len(api.patched)
    receipts.synchronize('om_1',{'text':'完成'})
    assert len(api.patched)==count


def test_send_uuid_expiry_does_not_cause_blind_duplicate(inbox):
    inbox.receive(history_message(message()),history=True)
    with connect(inbox.store.path) as db:
        db.execute('UPDATE feishu_receipts SET card_attempted_ms=?',(int(time.time()*1000)-7200000,))
    api=API()
    with pytest.raises(ReceiptUnknown):FeishuReceipts(inbox,api).synchronize('om_1',{'text':'已接收'})
    assert api.sent==[]


def test_other_bot_or_root_only_match_cannot_hijack_receipt(inbox):
    inbox.receive(history_message(message()),history=True)
    api=API();api.messages=[{'message_id':'om_other','chat_id':'oc_private','root_id':'om_1',
                          'sender':{'id':'other-app','id_type':'app_id','sender_type':'app'},'msg_type':'interactive'}]
    with connect(inbox.store.path) as db:
        db.execute('UPDATE feishu_receipts SET card_attempted_ms=?',(int(time.time()*1000)-7200000,))
    with pytest.raises(ReceiptUnknown):FeishuReceipts(inbox,api).synchronize('om_1',{})
    assert api.patched==[]


def test_explicit_renewal_has_new_uuid_and_replayed_click_does_not_send(inbox):
    inbox.receive(history_message(message()),history=True)
    class RenewAPI(API):
        def reply_card(self,parent,card,*,delivery_id):
            super().reply_card(parent,card,delivery_id=delivery_id)
            self.messages[-1]['message_id']='om_reply'+str(len(self.sent))
            return self.messages[-1]['message_id']
    api=RenewAPI();receipts=FeishuReceipts(inbox,api)
    old=receipts.synchronize('om_1',{'text':'旧待办'})
    new=receipts.renew('om_1',old,{'text':'当前待办'})
    assert old!=new and len(set(api.sent))==2
    with pytest.raises(ValueError):receipts.renew('om_1',old,{'text':'当前待办'})
    assert len(api.sent)==2
    assert receipts.synchronize('om_1',{'text':'当前待办'})==new
    assert len(api.sent)==2
