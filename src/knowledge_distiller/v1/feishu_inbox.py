"""Durable Feishu intake. Call only with authenticated API/SDK payloads.

The binding is explicit; receiving an arbitrary message never binds its sender.
Realtime and history share message IDs, not event IDs or text hashes.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from .database import connect


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def prior_item(db, key):
    """Called inside the *same* write transaction that creates a queue item."""
    if key is None:
        return None
    row = db.execute('SELECT * FROM feishu_parts WHERE app_id=? AND message_id=? AND position=?', key).fetchone()
    if row and row['item_id'] is not None:
        return row['item_id']
    receipt = db.execute('SELECT state FROM feishu_receipts WHERE app_id=? AND message_id=?', key[:2]).fetchone()
    if receipt is None or receipt['state'] not in {'received','waiting_input'}:
        raise ValueError('飞书投递已处理或不存在，不能重复创建任务。')
    if row and row['error']:
        raise ValueError('这条投递已处理，不能覆盖结果。')
    return None


def bind_item(db, key, item_id):
    if key is not None:
        db.execute('''INSERT INTO feishu_parts(app_id,message_id,position,item_id)
            VALUES (?,?,?,?) ON CONFLICT(app_id,message_id,position)
            DO UPDATE SET item_id=excluded.item_id,preview_json=NULL''', (*key,item_id))


@dataclass(frozen=True)
class Message:
    message_id: str
    chat_id: str
    sender_id: str
    sender_type: str
    chat_type: str | None
    created_ms: int
    message_type: str
    content: str
    mentions: tuple
    raw: dict
    deleted: bool = False


def _id(value, id_type='open_id'):
    if isinstance(value, dict):
        return value.get('open_id', '')
    return value if id_type == 'open_id' and isinstance(value, str) else ''


def event_message(payload):
    event = payload['event']
    message, sender = event['message'], event['sender']
    return Message(message['message_id'], message['chat_id'],
                   _id(sender.get('sender_id')), sender.get('sender_type', ''),
                   message.get('chat_type'), int(message['create_time']),
                   message['message_type'], message.get('content', ''),
                   tuple(message.get('mentions') or ()), payload)


def history_message(message):
    sender = message['sender']
    return Message(message['message_id'], message['chat_id'],
                   _id(sender.get('id'), sender.get('id_type')), sender.get('sender_type', ''),
                   message.get('chat_type'), int(message['create_time']),
                   message['msg_type'], message.get('body', {}).get('content', ''),
                   tuple(message.get('mentions') or ()), message, bool(message.get('deleted')))


class FeishuInbox:
    def __init__(self, store, app_id):
        self.store, self.app_id = store, app_id

    def bind(self, *, bot_open_id, user_open_id, chat_id, start_ms):
        if not all(isinstance(v, str) and v for v in
                   (self.app_id, bot_open_id, user_open_id, chat_id)) or start_ms < 0:
            raise ValueError('飞书绑定信息不完整。')
        if bot_open_id == user_open_id:
            raise ValueError('不能将机器人自身绑定为用户。')
        values = (self.app_id, bot_open_id, user_open_id, chat_id, start_ms, start_ms)
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM feishu_binding WHERE app_id=?', (self.app_id,)).fetchone()
            if old:
                if tuple(old)[:5] != values[:5]:
                    raise ValueError('该应用已绑定另一会话或起点，不能覆盖已有投递。')
                return
            db.execute('INSERT INTO feishu_binding VALUES (?,?,?,?,?,?)', values)

    def binding(self):
        with connect(self.store.path) as db:
            row = db.execute('SELECT * FROM feishu_binding WHERE app_id=?', (self.app_id,)).fetchone()
            if row is None:
                raise ValueError('请先在电脑绑定飞书私聊。')
            return dict(row)

    def receive(self, message: Message, *, history=False):
        binding = self.binding()
        if (message.sender_type != 'user' or message.sender_id != binding['user_open_id']
                or message.chat_id != binding['chat_id'] or message.deleted
                or message.created_ms < binding['start_ms']
                or (message.chat_type != 'p2p' and not (history and message.chat_type is None))):
            return None
        if not message.message_id:
            raise ValueError('飞书消息缺少稳定标识，不能推进补收进度。')
        text, same_topic, error = '', False, None
        if message.message_type in {'image','post'}:
            try:
                from .feishu_images import blocks
                blocks(message.message_type,message.content)
            except (ValueError,TypeError,KeyError,StopIteration):
                error = '图片消息无法完整读取，请重新发送原图或支持的图文消息。'
        elif message.message_type != 'text':
            error = '支持文字、链接和图片，暂不接收文件或音频。'
        else:
            try:
                text = json.loads(message.content)['text']
                if not isinstance(text, str):
                    raise ValueError()
                for mention in message.mentions:
                    if not isinstance(mention,dict):raise ValueError()
                    key = mention.get('key')
                    if (_id(mention.get('id'), mention.get('id_type', 'open_id')) == binding['bot_open_id']
                            and isinstance(key, str) and key and key in text):
                        same_topic = True
                        text = text.replace(key, '')
                if not text.strip():
                    error = '请输入需要处理的文字或链接。'
            except (ValueError, TypeError, KeyError):
                error = '这条消息的文字格式无法读取，请重新发送。'
        with connect(self.store.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO feishu_receipts
                (app_id,message_id,created_ms,raw_json,text,same_topic,state,error)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(app_id,message_id) DO NOTHING''',
                (self.app_id, message.message_id, message.created_ms, _json(message.raw),
                 text, int(same_topic), 'rejected' if error else 'received', error))
            return dict(db.execute('SELECT * FROM feishu_receipts WHERE app_id=? AND message_id=?',
                                  (self.app_id, message.message_id)).fetchone())

    def pending(self):
        from .intake import needs_content_choice
        with connect(self.store.path) as db:
            return [dict(row) for row in db.execute('''SELECT * FROM feishu_receipts
                WHERE app_id=? AND (state='received' OR (state='waiting_input' AND content_kind IS NULL))
                ORDER BY created_ms,message_id''', (self.app_id,))
                if row['state']=='received' or not needs_content_choice(row['text'])]

    def backfill(self, fetch_page, *, until_ms, cancelled=lambda: False):
        """Checkpoint only a fully traversed window; restart safely replays pages.

        fetch_page receives Feishu list-message query parameters and returns
        its unwrapped `data`. An API/shape failure leaves the checkpoint intact.
        """
        binding = self.binding()
        if until_ms < binding['history_until_ms']:
            raise ValueError('补收结束时间早于已核实进度。')
        # API times are seconds. Overlap the boundary second and deduplicate IDs.
        params = {'container_id_type': 'chat', 'container_id': binding['chat_id'],
                  'start_time': str(max(binding['start_ms'], binding['history_until_ms'] - 1000) // 1000),
                  'end_time': str((until_ms + 999) // 1000),
                  'sort_type': 'ByCreateTimeAsc', 'page_size': 50}
        tokens = set()
        while True:
            if cancelled():raise InterruptedError('feishu_stopping')
            page = fetch_page(dict(params))
            if not isinstance(page, dict) or not isinstance(page.get('items'), list) or not isinstance(page.get('has_more'), bool):
                raise ValueError('飞书历史消息响应不完整，未推进补收进度。')
            for raw in page['items']:
                if cancelled():raise InterruptedError('feishu_stopping')
                message = history_message(raw)
                if message.created_ms <= until_ms:
                    self.receive(message, history=True)
            if not page['has_more']:
                break
            token = page.get('page_token')
            if not isinstance(token, str) or not token or token in tokens:
                raise ValueError('飞书历史分页未完整结束，未推进补收进度。')
            tokens.add(token)
            params['page_token'] = token
        with connect(self.store.path) as db:
            db.execute('UPDATE feishu_binding SET history_until_ms=MAX(history_until_ms,?) WHERE app_id=?',
                       (until_ms, self.app_id))
