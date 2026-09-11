"""Persist authenticated callbacks before acknowledging; execute outside SDK IO."""
import hashlib
import json
import logging
import time
import sqlite3
from .database import connect


class ActionQueue:
    def __init__(self, inbox, actions):
        self.inbox, self.actions = inbox, actions

    def receive(self, payload):
        try:
            return self._receive(payload)
        except sqlite3.Error:
            return {'toast':{'type':'error','content':'暂未确认接收，请稍后重试；重复操作不会重复应用。'}}

    def _receive(self, payload):
        started=time.monotonic()
        with connect(self.inbox.store.path,timeout=.2) as db:
            binding=db.execute('SELECT * FROM feishu_binding WHERE app_id=?',(self.inbox.app_id,)).fetchone()
        if binding is None:return {'toast':{'type':'error','content':'请先完成飞书绑定。'}}
        event=payload.get('event', {})
        context=event.get('context', {})
        if (event.get('operator', {}).get('open_id')!=binding['user_open_id']
                or context.get('open_chat_id')!=binding['chat_id']
                or payload.get('header', {}).get('app_id',self.inbox.app_id)!=self.inbox.app_id):
            return {'toast':{'type':'error','content':'这张卡片不属于当前绑定会话。'}}
        action=event.get('action')
        if not isinstance(action,dict) or not isinstance(action.get('value'),dict) or not isinstance(action.get('form_value',{}),dict):
            return {'toast':{'type':'error','content':'无法识别这个操作，请使用最新卡片。'}}
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        if len(raw)>65536:
            return {'toast':{'type':'error','content':'操作内容过长，请缩短后重试。'}}
        key=hashlib.sha256(json.dumps([self.inbox.app_id,event],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with connect(self.inbox.store.path,timeout=.3) as db:
            db.execute('BEGIN IMMEDIATE')
            receipt=db.execute('SELECT message_id FROM feishu_receipts WHERE app_id=? AND card_id=?',
                               (self.inbox.app_id,context.get('open_message_id'))).fetchone()
            if receipt is None:
                return {'toast':{'type':'error','content':'这张卡片已失效，请打开最新待办。'}}
            db.execute('INSERT OR IGNORE INTO feishu_action_queue(action_key,app_id,message_id,payload) VALUES (?,?,?,?)',
                       (key,self.inbox.app_id,receipt['message_id'],raw))
            row=db.execute('SELECT result FROM feishu_action_queue WHERE action_key=?',(key,)).fetchone()
        logging.getLogger(__name__).info('Feishu action received key=%s persisted=true elapsed_ms=%d',key[:12],(time.monotonic()-started)*1000)
        result=json.loads(row['result']) if row['result'] else None
        if result and result.get('retryable'):
            with connect(self.inbox.store.path,timeout=.3) as db:
                db.execute('UPDATE feishu_action_queue SET result=NULL WHERE action_key=?',(key,))
            result=None
        return result or {'toast':{'type':'success','content':'已接收，正在保存；请查看卡片结果。'}}

    def process_one(self):
        with connect(self.inbox.store.path) as db:
            row=db.execute('SELECT * FROM feishu_action_queue WHERE app_id=? AND result IS NULL ORDER BY id LIMIT 1',
                           (self.inbox.app_id,)).fetchone()
        if row is None:return False
        started=time.monotonic()
        try:
            result=self.actions.handle(json.loads(row['payload']))
        except Exception as error:
            logging.getLogger(__name__).error('Feishu action failed key=%s error=%s',row['action_key'][:12],type(error).__name__)
            result={'toast':{'type':'error','content':'这项操作尚未完成，请重按原操作重试；其他待办仍可处理。'},'retryable':True}
        with connect(self.inbox.store.path) as db:
            db.execute('UPDATE feishu_action_queue SET result=? WHERE id=?',
                       (json.dumps(result,ensure_ascii=False),row['id']))
        logging.getLogger(__name__).info('Feishu action finished key=%s elapsed_ms=%d',row['action_key'][:12],(time.monotonic()-started)*1000)
        return True
