"""Pair only after possession of a short-lived code shown on this computer."""
import json
import secrets
import time
from .database import connect


def pending(store, *, include_expired=False):
    raw=store.setting('feishu_pairing')
    value=json.loads(raw) if raw else {}
    return value if include_expired or value.get('expires',0)>time.time() else {}


def begin(store,app_id,bot_open_id):
    value={'app_id':app_id,'bot_open_id':bot_open_id,
           'code':'绑定KD-'+secrets.token_hex(4).upper(),'expires':time.time()+600}
    store.set_setting('feishu_pairing',json.dumps(value))
    return value


def is_bound(inbox):
    with connect(inbox.store.path) as db:
        return db.execute('SELECT 1 FROM feishu_binding WHERE app_id=?',(inbox.app_id,)).fetchone() is not None


def accept(inbox,message):
    config=pending(inbox.store)
    if (config.get('app_id')!=inbox.app_id or message.chat_type!='p2p'
            or message.sender_type!='user' or message.message_type!='text' or message.deleted):return False
    try:content=json.loads(message.content)
    except (ValueError,TypeError):return False
    if not isinstance(content,dict) or content.get('text','').strip()!=config['code']:return False
    inbox.bind(bot_open_id=config['bot_open_id'],user_open_id=message.sender_id,
               chat_id=message.chat_id,start_ms=message.created_ms+1)
    with connect(inbox.store.path) as db:db.execute("DELETE FROM settings WHERE key='feishu_pairing'")
    return True
