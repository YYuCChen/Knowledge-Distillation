import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, event_message, history_message


@pytest.fixture
def inbox(tmp_path):
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    inbox = FeishuInbox(store, 'app-new')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=100000)
    return inbox


def message(mid='om_1', text='一段来源文字', **changes):
    value = {'message_id':mid, 'chat_id':'oc_private', 'create_time':'102000',
             'sender':{'id':'ou_owner','id_type':'open_id','sender_type':'user'},
             'msg_type':'text', 'body':{'content':__import__('json').dumps({'text':text})},
             'mentions':[]}
    value.update(changes)
    return value


def event(raw):
    return {'event':{'sender':{'sender_id':{'open_id':raw['sender']['id']},'sender_type':raw['sender']['sender_type']},
            'message':{'message_id':raw['message_id'],'chat_id':raw['chat_id'],'chat_type':'p2p',
                       'create_time':raw['create_time'],'message_type':raw['msg_type'],
                       'content':raw['body']['content'],'mentions':raw['mentions']}}}


def test_live_then_history_restart_and_edited_message_do_not_duplicate_or_replace(inbox):
    original=message()
    inbox.receive(event_message(event(original)))
    restarted=FeishuInbox(inbox.store,inbox.app_id)
    row=restarted.receive(history_message(message(text='编辑后的另一段文字')),history=True)
    assert row['text']=='一段来源文字'
    assert len(restarted.pending())==1
    assert restarted.pending()[0]['raw_json']==inbox.pending()[0]['raw_json']


@pytest.mark.parametrize('changes',[
    {'sender':{'id':'ou_other','id_type':'open_id','sender_type':'user'}},
    {'sender':{'id':'ou_bot','id_type':'open_id','sender_type':'app'}},
    {'chat_id':'oc_other'}, {'chat_type':'group'}, {'deleted':True}, {'create_time':'99999'},
    {'sender':{'id':'ou_owner','id_type':'user_id','sender_type':'user'}},
])
def test_unbound_senders_and_nonprivate_or_prebinding_messages_are_not_received(inbox,changes):
    assert inbox.receive(history_message(message(**changes)),history=True) is None
    assert inbox.pending()==[]


def test_live_requires_explicit_private_chat(inbox):
    payload=event(message())
    del payload['event']['message']['chat_type']
    assert inbox.receive(event_message(payload)) is None


@pytest.mark.parametrize('realtime',[True,False])
def test_only_structured_mention_of_current_bot_sets_same_topic(inbox,realtime):
    raw=message(text='@_user_1 https://www.douyin.com/video/1\nhttps://www.douyin.com/video/2')
    raw['mentions']=[{'key':'@_user_1','id':{'open_id':'ou_bot'} if realtime else 'ou_bot','id_type':'open_id'}]
    row=inbox.receive(event_message(event(raw)) if realtime else history_message(raw),history=not realtime)
    assert row['same_topic']==1 and '@_user_1' not in row['text']
    assert '@_user_1' in row['raw_json']
    spoof=message('om_spoof',text='@知识蒸馏器 https://www.douyin.com/video/1')
    assert inbox.receive(history_message(spoof),history=True)['same_topic']==0


def test_invalid_message_is_recorded_but_does_not_block_later_messages(inbox):
    bad=message(msg_type='image')
    row=inbox.receive(history_message(bad),history=True)
    assert row['state']=='rejected'
    good=inbox.receive(history_message(message('om_2')),history=True)
    assert good['state']=='received'
    assert len(inbox.pending())==1


def test_page_failure_preserves_checkpoint_and_restart_deduplicates_completed_page(inbox):
    calls=[]
    def fail_second(params):
        calls.append(params)
        if 'page_token' in params: raise OSError('offline')
        return {'items':[message()],'has_more':True,'page_token':'next'}
    with pytest.raises(OSError): inbox.backfill(fail_second,until_ms=110000)
    assert inbox.binding()['history_until_ms']==100000
    assert len(inbox.pending())==1
    def succeeds(params):
        if 'page_token' in params:
            return {'items':[message('om_2')],'has_more':False}
        return {'items':[message()],'has_more':True,'page_token':'next'}
    FeishuInbox(inbox.store,inbox.app_id).backfill(succeeds,until_ms=110000)
    assert inbox.binding()['history_until_ms']==110000
    assert len(inbox.pending())==2
    assert calls[0]['container_id']=='oc_private'


def test_repeated_pagination_token_and_missing_shape_do_not_skip_history(inbox):
    with pytest.raises(ValueError,match='分页'):
        inbox.backfill(lambda _: {'items':[],'has_more':True,'page_token':'same'},until_ms=110000)
    with pytest.raises(ValueError,match='不完整'):
        inbox.backfill(lambda _: {},until_ms=110000)
    assert inbox.binding()['history_until_ms']==100000


def test_rebinding_cannot_redirect_existing_receipts(inbox):
    with pytest.raises(ValueError,match='不能覆盖'):
        inbox.bind(bot_open_id='ou_bot',user_open_id='ou_other',chat_id='oc_other',start_ms=100000)
    assert inbox.binding()['user_open_id']=='ou_owner'


def test_item_and_receipt_are_one_transaction_and_failed_item_is_not_requeued(inbox):
    from knowledge_distiller.v1.database import connect
    inbox.receive(history_message(message()),history=True)
    key=(inbox.app_id,'om_1',0)
    item=inbox.store.create_item('https://www.douyin.com/video/101',receipt_key=key)
    inbox.store.mark_failed(item,'collecting','test_failure')
    assert inbox.store.create_item('https://www.douyin.com/video/101',receipt_key=key)==item
    assert inbox.store.item_bundle(item)['state']=='failed'
    with pytest.raises(ValueError):
        inbox.store.create_item('https://www.douyin.com/video/102',receipt_key=(inbox.app_id,'unknown',0))
    assert len(inbox.store.recent_items())==1
    with connect(inbox.store.path) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_text_intake_crash_after_commit_reuses_existing_item(inbox):
    from knowledge_distiller.v1.file_sources import prepare_direct_text
    from knowledge_distiller.v1.feishu_intake import FeishuIntake
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.submit_source(prepare_direct_text('一段来源文字'),receipt_key=(inbox.app_id,'om_1',0))
    # Process died after atomic item+part commit, before receipt summary changed.
    assert FeishuIntake(inbox,None).process('om_1')=='accepted'
    assert len(inbox.store.recent_items())==1
    assert inbox.store.item_bundle(item)['input_kind']=='direct_text'
