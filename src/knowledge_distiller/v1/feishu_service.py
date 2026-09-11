"""Application-owned Feishu connection for an explicitly bound private chat."""
import threading
import re
from .database import connect
from .feishu_api import FeishuAPI
from .feishu_inbox import FeishuInbox
from .feishu_intake import FeishuIntake
from .feishu_actions import FeishuActions
from .feishu_cards import FeishuCards
from .feishu_media import FeishuMedia
from .feishu_receipts import FeishuReceipts
from .feishu_runtime import FeishuRuntime


class FeishuService:
    def __init__(self,store,links,distiller,runtime_root,*,wake=None):
        self.store,self.links,self.distiller=store,links,distiller
        self.root,self.wake=runtime_root,wake
        self.runtime=None
        self.api=None
        self.error=None
        self.delivery_errors={}
        self._configuration_lock=threading.Lock()

    def configure(self,app_id,secret=''):
        """Validate this binding's credentials before replacing or reconnecting."""
        from .local_secrets import LocalSecrets
        with self._configuration_lock:
            with connect(self.store.path) as db:
                bindings=db.execute('SELECT * FROM feishu_binding').fetchall()
            if len(bindings)>1 or (bindings and bindings[0]['app_id']!=app_id):
                raise ValueError('feishu_binding_required')
            if not bindings and not re.fullmatch(r'cli_[a-zA-Z0-9]+',app_id):
                raise ValueError('feishu_configuration_failed')
            if '\n' in secret or '\r' in secret:
                raise ValueError('feishu_configuration_failed')
            credentials=LocalSecrets(self.store.path.parent / 'credentials')
            credential=credentials('feishu-app-'+app_id)
            candidate=FeishuAPI(app_id,secret_loader=(lambda:secret) if secret else credential.load)
            try:
                bot=candidate.bot_info()
                if not bot.get('open_id') or (bindings and bot.get('open_id')!=bindings[0]['bot_open_id']):
                    raise ValueError('feishu_configuration_failed')
            finally:
                candidate.close()
            if secret: credential.save_validated(secret)
            else: credentials.mark_validated('feishu-app-'+app_id)
            self.stop()
            if not bindings:
                from .feishu_pairing import begin
                begin(self.store,app_id,bot['open_id'])
            self.error=None
            self.delivery_errors.clear()
            self.start()

    def start(self):
        if self.runtime is not None:return
        with connect(self.store.path) as db:
            bindings=db.execute('SELECT app_id FROM feishu_binding').fetchall()
        from .feishu_pairing import pending
        pairing=pending(self.store)
        if not bindings and not pairing:return
        if len(bindings)>1:
            self.error='存在多个飞书绑定，请在电脑核对。'
            return
        inbox=FeishuInbox(self.store,bindings[0]['app_id'] if bindings else pairing['app_id'])
        from .local_secrets import LocalSecrets
        credential=LocalSecrets(self.store.path.parent / 'credentials')('feishu-app-'+inbox.app_id)
        self.api=FeishuAPI(inbox.app_id, secret_loader=credential.load)
        intake=FeishuIntake(inbox,self.links,wake=self.wake,api=self.api)
        actions=FeishuActions(inbox,intake,self.distiller,wake=self.wake)
        cards=FeishuCards(inbox,self.distiller,FeishuMedia(self.api,self.root/'feishu-media'),self.links.collections)
        receipts=FeishuReceipts(inbox,self.api)
        self.receipts,self.cards=receipts,cards

        def synchronize():
            with connect(self.store.path) as db:
                messages=[row[0] for row in db.execute('SELECT message_id FROM feishu_receipts WHERE app_id=? ORDER BY created_ms',
                                                     (inbox.app_id,))]
            for message in messages:
                if self.runtime._stop.is_set():return
                try:
                    receipts.synchronize(message,cards.card(message))
                    self.delivery_errors.pop(message,None)
                except Exception:
                    # One bad/expired card must not strand other receipts.
                    self.delivery_errors[message]='回执同步未完成，投递与确认记录已保留。'
        self.runtime=FeishuRuntime(inbox,self.api,intake,actions,synchronize,allow_pairing=not bindings)
        self.runtime.start()

    def stop(self):
        if self.runtime:self.runtime.stop()
        if self.api:self.api.close()
        self.runtime=None
        self.api=None

    def status(self):
        with connect(self.store.path) as db:
            bindings=db.execute('SELECT app_id,chat_id FROM feishu_binding').fetchall()
            recoverable=[dict(r) for r in db.execute('SELECT message_id,card_id FROM feishu_receipts WHERE card_id IS NOT NULL')
                         if r['message_id'] in self.delivery_errors]
        binding=dict(bindings[0]) if len(bindings)==1 else {}
        from .feishu_pairing import pending
        pairing=pending(self.store,include_expired=True) if not binding else {}
        active_pairing=pending(self.store) if not binding else {}
        return {'state':('pairing' if active_pairing else 'pairing_expired') if pairing and not binding else self.runtime.state if self.runtime else 'unbound',
                'transport_state':self.runtime.state if self.runtime else 'stopped',
                'binding':binding,
                'pairing':pairing,
                'recoverable':recoverable,
                'error':self.error or (self.runtime.error if self.runtime else None),
                'delivery_errors':dict(self.delivery_errors)}

    def renew_receipt(self,message_id,card_id):
        with self._configuration_lock:
            if self.runtime is None or message_id not in self.delivery_errors:
                raise ValueError('回执状态已更新，请刷新后查看。')
            self.receipts.renew(message_id,card_id,self.cards.card(message_id))
            self.delivery_errors.pop(message_id,None)
