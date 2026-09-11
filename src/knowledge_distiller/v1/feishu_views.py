"""Small durable card cursor/draft, invalidated by the authoritative token."""
import json
from .database import connect


def key(inbox,message):
    return 'feishu_view:'+inbox.app_id+':'+message


def read(inbox,message,token):
    raw=inbox.store.setting(key(inbox,message))
    value=json.loads(raw) if raw else {}
    return value if value.get('token')==token else {'token':token,'page':0,'edits':{}}


def write(inbox,message,value):
    inbox.store.set_setting(key(inbox,message),json.dumps(value,ensure_ascii=False))


def chunks(value,size=800):
    return [value[n:n+size] for n in range(0,len(value),size)] or ['']


def controls(base,page,count):
    from .feishu_cards import button,text
    result=[text(f'第 {page+1} / {count} 段')]
    for label,offset in [('上一段',-1),('下一段',1)]:
        if 0<=page+offset<count:
            result.append(button(label,'page_'+str(page+offset),{**base,'action':'page','page':page+offset}))
    return result
