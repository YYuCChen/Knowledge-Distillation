"""Deliver persisted messages through the same intake used by Home."""
import json

from .chrome import ChromeSessionError
from .database import connect
from .douyin_collections import CollectionError
from .file_sources import prepare_direct_text
from .intake import needs_content_choice, links_in, platform_for_url


class FeishuIntake:
    def __init__(self, inbox, links, *, wake=None, api=None):
        self.inbox, self.links, self.wake = inbox, links, wake
        self.api = api

    def choose(self,message_id,content_kind):
        if content_kind not in {'links','text'}:
            raise ValueError('请选择处理链接或完整文本。')
        key=(self.inbox.app_id,message_id)
        with connect(self.inbox.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM feishu_receipts WHERE app_id=? AND message_id=?',key).fetchone()
            if row is None:
                raise ValueError('投递不存在。')
            if row['content_kind'] is not None:
                if row['content_kind']!=content_kind:
                    raise ValueError('已经保存另一种处理方式，请刷新。')
                return
            if row['state']!='waiting_input':
                raise ValueError('这条投递当前不需要选择处理方式。')
            if row['same_topic'] and content_kind=='text':
                raise ValueError('@ 同题不能同时作为完整文本，请重新投递。')
            db.execute("UPDATE feishu_receipts SET content_kind=?,state='received' WHERE app_id=? AND message_id=?",(content_kind,*key))


    def process(self, message_id, *, content_kind=None, cancelled=lambda: False):
        if cancelled():raise InterruptedError('feishu_stopping')
        key = (self.inbox.app_id, message_id)
        with connect(self.inbox.store.path) as db:
            row = db.execute('SELECT * FROM feishu_receipts WHERE app_id=? AND message_id=?', key).fetchone()
        if row is None:
            raise ValueError('这条飞书投递不存在。')
        if row['state'] not in {'received','waiting_input'}:
            return row['state']
        raw=json.loads(row['raw_json'])
        from .feishu_inbox import event_message, history_message
        message=event_message(raw) if 'event' in raw else history_message(raw)
        if message.message_type in {'image','post'}:
            existing=self._part(key,0)
            if existing and existing['item_id'] is not None:
                self._state(key,'accepted')
                if self.wake:self.wake()
                return 'accepted'
            from .feishu_images import blocks, prepare
            from .feishu_api import FeishuAPIError
            import httpx
            try:
                source=prepare(self.inbox.app_id,message_id,blocks(message.message_type,message.content),self.api)
                self.inbox.store.submit_source(source,receipt_key=(*key,0))
            except (FeishuAPIError,httpx.HTTPError,OSError,ValueError) as error:
                denied=isinstance(error,FeishuAPIError) and str(error.code) in {'http_403','234003','234008'}
                detail='机器人暂无下载这张原图的权限，请检查消息资源权限后重试。' if denied else '原图下载或校验未完成，收件已保留，可重试下载。'
                return self._state(key,'needs_desktop',detail)
            self._state(key,'accepted')
            if self.wake:self.wake()
            return 'accepted'
        text, urls = row['text'], links_in(row['text'])
        if content_kind not in {None,'text','links'}:
            raise ValueError('请选择处理链接或完整文本。')
        if row['content_kind'] and content_kind and row['content_kind'] != content_kind:
            raise ValueError('这条投递已选择另一种处理方式，请刷新。')
        content_kind = row['content_kind'] or content_kind
        if row['same_topic'] and (len(urls)<2 or len({platform_for_url(url) for url in urls}) != 1 or not platform_for_url(urls[0])):
            return self._state(key,'rejected','同题处理需要至少两条同一平台的内容链接，不能跨平台。')
        if needs_content_choice(text) and content_kind is None:
            return self._state(key,'waiting_input')
        if row['same_topic'] and content_kind=='text':
            return self._state(key,'rejected','@ 同题与完整文本的意图冲突，请重新投递。')
        chosen = content_kind or ('links' if urls else 'text')
        with connect(self.inbox.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT content_kind,state FROM feishu_receipts WHERE app_id=? AND message_id=?',key).fetchone()
            if current['content_kind'] and current['content_kind'] != chosen:
                raise ValueError('这条投递已选择另一种处理方式，请刷新。')
            if current['state'] not in {'received','waiting_input'}:
                return current['state']
            db.execute('UPDATE feishu_receipts SET content_kind=? WHERE app_id=? AND message_id=?',(chosen,*key))
        if not urls or content_kind=='text':
            self.inbox.store.submit_source(prepare_direct_text(text), receipt_key=(*key,0))
        elif row['same_topic']:
            if not self._part(key,0):
                try:
                    preview=self.links.collections.preview_same_topic(urls,submitted_text=text,durable=True)
                    self._preview(key,0,preview)
                except (ValueError,ChromeSessionError,CollectionError) as error:
                    message=self.links.errors.get(str(error),str(error))
                    with connect(self.inbox.store.path) as db:
                        db.execute('''INSERT INTO feishu_parts(app_id,message_id,position,error)
                            VALUES (?,?,0,?) ON CONFLICT(app_id,message_id,position) DO NOTHING''',(*key,message))
        else:
            for position,url in enumerate(urls):
                if cancelled():raise InterruptedError('feishu_stopping')
                if self._part(key,position):
                    continue
                try:
                    result=self.links.submit(url,receipt_key=(*key,position),durable=True)
                    if isinstance(result,dict):
                        self._preview(key,position,result)
                except (ValueError,ChromeSessionError,CollectionError) as error:
                    message=self.links.errors.get(str(error),str(error))
                    with connect(self.inbox.store.path) as db:
                        db.execute('''INSERT INTO feishu_parts(app_id,message_id,position,error)
                            VALUES (?,?,?,?) ON CONFLICT(app_id,message_id,position) DO NOTHING''',(*key,position,message))
        with connect(self.inbox.store.path) as db:
            parts=db.execute('SELECT * FROM feishu_parts WHERE app_id=? AND message_id=?',key).fetchall()
        from .feishu_scopes import process as process_scopes, items
        waiting=process_scopes(self.inbox,self.links.collections,message_id,cancelled=cancelled) if any(p['preview_json'] for p in parts) else False
        state='waiting_input' if waiting else 'needs_desktop' if any(p['error'] for p in parts) else 'accepted'
        self._state(key,state)
        if self.wake and items(self.inbox,message_id):
            self.wake()
        return state

    def _part(self,key,position):
        with connect(self.inbox.store.path) as db:
            return db.execute('SELECT * FROM feishu_parts WHERE app_id=? AND message_id=? AND position=?',(*key,position)).fetchone()

    def _preview(self,key,position,preview):
        with connect(self.inbox.store.path) as db:
            db.execute('''INSERT INTO feishu_parts(app_id,message_id,position,preview_json)
                VALUES (?,?,?,?) ON CONFLICT(app_id,message_id,position) DO NOTHING''',
                (*key,position,json.dumps({'token':preview['token']})))

    def _state(self,key,state,error=None):
        with connect(self.inbox.store.path) as db:
            db.execute('''UPDATE feishu_receipts SET state=?,error=?
                WHERE app_id=? AND message_id=? AND state IN ('received','waiting_input')''', (state,error,*key))
        return state
