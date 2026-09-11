import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from .test_feishu_inbox import inbox, message
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import history_message
from knowledge_distiller.v1.feishu_intake import FeishuIntake
from knowledge_distiller.v1.feishu_runtime import FeishuRuntime


def test_shutdown_waits_for_owned_intake_before_returning(inbox):
    entered,release,stopped=threading.Event(),threading.Event(),threading.Event()
    def backfill(*args,**kwargs):
        entered.set()
        assert release.wait(3)
        if kwargs['cancelled']():raise InterruptedError()
    inbox.backfill=backfill
    sync=Mock()
    runtime=FeishuRuntime(inbox,SimpleNamespace(history=Mock()),Mock(),Mock(),sync)
    runtime._socket_loop=lambda:runtime._stop.wait()
    runtime.start()
    assert entered.wait(2)
    shutdown=threading.Thread(target=lambda:(runtime.stop(),stopped.set()))
    shutdown.start()
    assert runtime._stop.wait(2)
    assert not stopped.is_set()
    release.set();shutdown.join(2)
    assert stopped.is_set()
    assert all(not t.is_alive() for t in runtime._threads)
    assert runtime.state=='stopped' and runtime.error is None
    sync.assert_not_called()


def test_cancelled_history_does_not_advance_checkpoint(inbox):
    before=inbox.binding()['history_until_ms']
    cancelled=threading.Event()
    def history(params):
        cancelled.set()
        return {'items':[message()], 'has_more':False}
    with pytest.raises(InterruptedError):
        inbox.backfill(history,until_ms=before+5000,cancelled=cancelled.is_set)
    assert inbox.binding()['history_until_ms']==before
    assert inbox.pending()==[]


def test_partial_message_shutdown_resumes_without_duplicate_item(inbox):
    inbox.receive(history_message(message(text='https://www.douyin.com/video/101\nhttps://www.douyin.com/video/102')),history=True)
    stopped=threading.Event()
    calls=[]
    def submit(url,*,receipt_key,durable):
        calls.append(url)
        item=inbox.store.create_item(url,receipt_key=receipt_key)
        stopped.set()
        return item
    intake=FeishuIntake(inbox,SimpleNamespace(submit=submit))
    with pytest.raises(InterruptedError):intake.process('om_1',cancelled=stopped.is_set)
    assert len(inbox.pending())==1
    assert intake.process('om_1')=='accepted'
    assert len(calls)==2
    with connect(inbox.store.path) as db:
        assert db.execute('SELECT count(*) FROM feishu_parts').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM distill_items').fetchone()[0]==2


def test_history_outage_does_not_block_committed_live_message(inbox):
    inbox.receive(history_message(message()),history=True)
    intake=FeishuIntake(inbox,None)
    api=SimpleNamespace(history=Mock(side_effect=OSError('offline')))
    synchronizations=[]
    def synchronize():
        synchronizations.append(True)
        if len(synchronizations)==2:runtime._stop.set()
    runtime=FeishuRuntime(inbox,api,intake,None,synchronize)
    runtime._intake_loop()
    assert inbox.pending()==[]
    assert inbox.store.item_bundle(1)['input_kind']=='direct_text'
    assert runtime.error=='feishu_history_failed'


def test_receipt_is_synchronized_before_slow_discovery(inbox):
    inbox.receive(history_message(message()),history=True)
    sequence=[]
    def process(*args,**kwargs):sequence.append('intake')
    def synchronize():
        sequence.append('receipt')
        if len(sequence)>1:runtime._stop.set()
    runtime=FeishuRuntime(inbox,SimpleNamespace(history=lambda params:{'items':[],'has_more':False}),
                          SimpleNamespace(process=process),None,synchronize)
    runtime._intake_loop()
    assert sequence==['receipt','intake','receipt']
