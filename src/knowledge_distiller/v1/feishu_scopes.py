"""Queue scope commands durably; discovery never runs in a card callback."""
import json

from .database import connect
from .collections import PreviewChanged


def items(inbox,message_id):
    with connect(inbox.store.path) as db:
        ids=[r[0] for r in db.execute('''
            SELECT item_id FROM feishu_parts WHERE app_id=? AND message_id=? AND item_id IS NOT NULL
            UNION
            SELECT cm.item_id FROM feishu_parts p
            JOIN collection_confirmations cc ON cc.token=json_extract(p.preview_json,'$.token')
            JOIN collection_members cm ON cm.operation_id=cc.operation_id
            WHERE p.app_id=? AND p.message_id=? AND cm.item_id IS NOT NULL
            ORDER BY item_id''',(inbox.app_id,message_id,inbox.app_id,message_id))]
    return [inbox.store.item_bundle(i) for i in ids]


def request(inbox,message_id,value):
    position,token=value.get('position'),value.get('scope_token')
    if type(position) is not int or not isinstance(token,str):raise ValueError('范围标识无效。')
    command=value.get('command')
    if command not in {'confirm','select'}:raise ValueError('范围操作无效。')
    with connect(inbox.store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        part=db.execute('SELECT preview_json FROM feishu_parts WHERE app_id=? AND message_id=? AND position=?',
                        (inbox.app_id,message_id,position)).fetchone()
        current=json.loads(part[0]) if part and part[0] else {}
        if current.get('token')!=token:raise ValueError('范围已更新，请使用最新卡片。')
        draft=db.execute('SELECT preview_json FROM collection_previews WHERE token=?',(token,)).fetchone()
        if draft is None:
            if db.execute('SELECT 1 FROM collection_confirmations WHERE token=?',(token,)).fetchone():return
            raise ValueError('范围已失效，请在电脑核对。')
        draft=json.loads(draft[0])
        if command=='confirm':
            if not draft['scopes']:raise ValueError('请先选择范围。')
            same_topic=any(s['kind']=='same_topic' for s in draft['scopes'])
            if same_topic and value.get('same_topic') is not True:raise ValueError('请确认这些作品属于同一话题。')
            choice={'command':command,'signatures':[s['signature'] for s in draft['scopes']], 'same_topic':same_topic}
        else:
            selected=value.get('selected')
            if value.get('selection_text') is not None:
                import re
                raw=value['selection_text']
                if not isinstance(raw,str) or not re.fullmatch(r'[0-9,，\s]+',raw):
                    raise ValueError('请输入合集序号，用逗号分隔。')
                indices=[int(n) for n in re.split(r'[,，\s]+',raw.strip()) if n]
                choices=draft.get('choices',[])
                if not indices or any(n<1 or n>len(choices) for n in indices):
                    raise ValueError('合集序号不在当前范围内。')
                selected=list(dict.fromkeys(choices[n-1]['key'] for n in indices))
            allowed={'full_profile','all_collections'}|{c['key'] for c in draft.get('choices',[])}
            if (not isinstance(selected,list) or not selected or
                    any(not isinstance(x,str) or x not in allowed for x in selected)):
                raise ValueError('请选择有效的内容范围。')
            choice={'command':command,'selected':selected}
        if current.get('request') and current['request']!=choice:raise ValueError('已提交另一项范围操作，请等待更新。')
        current['request']=choice
        db.execute('UPDATE feishu_parts SET preview_json=? WHERE app_id=? AND message_id=? AND position=?',
                   (json.dumps(current),inbox.app_id,message_id,position))
        db.execute("UPDATE feishu_receipts SET state='received' WHERE app_id=? AND message_id=? AND state='waiting_input'",
                   (inbox.app_id,message_id))


def process(inbox,collections,message_id,*,cancelled=lambda:False):
    with connect(inbox.store.path) as db:
        parts=db.execute('SELECT * FROM feishu_parts WHERE app_id=? AND message_id=? AND preview_json IS NOT NULL',
                         (inbox.app_id,message_id)).fetchall()
    waiting=False
    for part in parts:
        if cancelled():raise InterruptedError('feishu_stopping')
        preview=json.loads(part['preview_json']);token=preview['token']
        with connect(inbox.store.path) as db:
            accepted=db.execute('SELECT expected_count FROM collection_confirmations WHERE token=?',(token,)).fetchall()
        if accepted:
            if len(accepted)!=accepted[0]['expected_count']:
                raise ValueError('范围部分接收，请在电脑查看已保存内容。')
            continue
        command=preview.get('request')
        if command is None:waiting=True;continue
        try:
            if command['command']=='select':
                collections.select(token,command['selected'])
                waiting=True
            else:
                collections.confirm(token,command['signatures'],same_topic=command['same_topic'])
        except PreviewChanged:
            waiting=True
    return waiting
