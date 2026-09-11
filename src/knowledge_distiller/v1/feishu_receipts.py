"""Send/update one receipt without touching accepted tasks or user decisions."""
import hashlib
import json
import time
import threading
from uuid import NAMESPACE_URL, uuid5

from .database import connect


class ReceiptUnknown(RuntimeError):
    pass


class FeishuReceipts:
    def __init__(self,inbox,api):
        self.inbox,self.api=inbox,api
        self._lock=threading.Lock()

    def synchronize(self,message_id,card):
        with self._lock:
            return self._synchronize(message_id,card)

    def _synchronize(self,message_id,card):
        key=(self.inbox.app_id,message_id)
        signature=hashlib.sha256(json.dumps(card,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with connect(self.inbox.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM feishu_receipts WHERE app_id=? AND message_id=?',key).fetchone()
            if row is None:
                raise ValueError('unknown receipt')
            if row['card_signature']==signature:
                return row['card_id']
            card_id=row['card_id']
            attempted=row['card_attempted_ms']
            if card_id is None and attempted is None:
                attempted=int(time.time()*1000)
                db.execute('UPDATE feishu_receipts SET card_attempted_ms=? WHERE app_id=? AND message_id=?',(attempted,*key))
        if card_id is None:
            if row['card_attempted_ms'] is not None:
                card_id=self._find_reply(message_id,attempted)
            if card_id is None:
                # Feishu only deduplicates a send UUID for one hour. Absence in
                # history isn't proof of non-delivery; never blindly resend later.
                if int(time.time()*1000)-attempted>=55*60*1000:
                    raise ReceiptUnknown('飞书回执结果待核对；投递已保留，未重复发送。')
                card_id=self.api.reply_card(message_id,card,
                    delivery_id=str(uuid5(NAMESPACE_URL,self.inbox.app_id+':'+message_id+self._generation_suffix(message_id))))
            with connect(self.inbox.store.path) as db:
                db.execute('UPDATE feishu_receipts SET card_id=? WHERE app_id=? AND message_id=?',(card_id,*key))
        # Also patch after send/recovery: a prior UUID attempt may have accepted
        # an earlier card version. Only a successful patch acknowledges this one.
        self.api.update_card(card_id,card)
        with connect(self.inbox.store.path) as db:
            db.execute('UPDATE feishu_receipts SET card_signature=? WHERE app_id=? AND message_id=?',(signature,*key))
        return card_id

    def _find_reply(self,message_id,attempted):
        binding=self.inbox.binding()
        params={'container_id_type':'chat','container_id':binding['chat_id'],
                'start_time':str(max(0,attempted-1000)//1000),'page_size':50,'sort_type':'ByCreateTimeAsc'}
        tokens,found=set(),set()
        excluded=set(self._generation(message_id).get('retired',[]))
        while True:
            page=self.api.history(dict(params))
            if not isinstance(page.get('items'),list) or not isinstance(page.get('has_more'),bool):
                raise ReceiptUnknown('飞书回执历史不完整。')
            for message in page['items']:
                sender=message.get('sender',{})
                if (message.get('chat_id')==binding['chat_id'] and message.get('parent_id')==message_id
                        and sender.get('sender_type')=='app' and sender.get('id_type')=='app_id'
                        and sender.get('id')==self.inbox.app_id and message.get('msg_type')=='interactive'
                        and not message.get('deleted') and message['message_id'] not in excluded):
                    found.add(message['message_id'])
            if not page['has_more']:
                break
            token=page.get('page_token')
            if not isinstance(token,str) or not token or token in tokens:
                raise ReceiptUnknown('飞书回执历史分页未完成。')
            tokens.add(token);params['page_token']=token
        if len(found)>1:
            raise ReceiptUnknown('找到多张关联回执，需要在电脑核对，未覆盖任意卡片。')
        return next(iter(found),None)

    def _generation(self,message_id):
        raw=self.inbox.store.setting('feishu_card_generation:'+self.inbox.app_id+':'+message_id)
        return json.loads(raw) if raw else {'number':0,'retired':[]}

    def _generation_suffix(self,message_id):
        number=self._generation(message_id)['number']
        return ':'+str(number) if number else ''

    def renew(self,message_id,expected_card,card):
        """Explicit desktop recovery; replayed clicks never allocate twice."""
        with self._lock:
            with connect(self.inbox.store.path) as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT card_id FROM feishu_receipts WHERE app_id=? AND message_id=?',
                               (self.inbox.app_id,message_id)).fetchone()
                if row is None or not expected_card or row[0]!=expected_card:
                    raise ValueError('回执已经更新，请刷新设置。')
                key='feishu_card_generation:'+self.inbox.app_id+':'+message_id
                raw=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
                value=json.loads(raw[0]) if raw else {'number':0,'retired':[]}
                value['number']+=1;value['retired'].append(expected_card)
                db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (key,json.dumps(value)))
                db.execute('UPDATE feishu_receipts SET card_id=NULL,card_signature=NULL,card_attempted_ms=NULL WHERE app_id=? AND message_id=?',
                           (self.inbox.app_id,message_id))
            return self._synchronize(message_id,card)
