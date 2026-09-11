"""Authenticate card ownership before entering the shared confirmation path."""
import json

from .database import connect


class FeishuActions:
    def __init__(self,inbox,intake,distiller,*,wake=None):
        self.inbox,self.intake,self.distiller,self.wake=inbox,intake,distiller,wake

    def handle(self,payload):
        binding=self.inbox.binding()
        event=payload.get('event',{})
        context=event.get('context',{})
        if (event.get('operator',{}).get('open_id')!=binding['user_open_id']
                or context.get('open_chat_id')!=binding['chat_id']
                or payload.get('header',{}).get('app_id',self.inbox.app_id)!=self.inbox.app_id):
            return self._toast('error','这张卡片不属于当前绑定会话。')
        with connect(self.inbox.store.path) as db:
            receipt=db.execute('SELECT * FROM feishu_receipts WHERE app_id=? AND card_id=?',
                (self.inbox.app_id,context.get('open_message_id'))).fetchone()
        if receipt is None:
            return self._toast('error','这张卡片已失效，请打开最新待办。')
        action=event.get('action',{})
        value=action.get('value') or {}
        if not isinstance(value,dict):
            return self._toast('error','无法识别这个操作。')
        try:
            if value.get('kind')=='retry_receipt':
                with connect(self.inbox.store.path) as db:
                    db.execute("UPDATE feishu_receipts SET state='received',error=NULL WHERE app_id=? AND message_id=? AND state='needs_desktop' AND error IS NOT NULL",(self.inbox.app_id,receipt['message_id']))
                return self._toast('success','已接收重试，将重新下载原图。')
            if value.get('kind')=='content_choice':
                self.intake.choose(receipt['message_id'],value.get('choice'))
                return self._toast('success','已保存处理方式。')
            if value.get('kind')=='scope':
                from .feishu_scopes import request
                value={**value,'selection_text':(action.get('form_value') or {}).get('scope_selection')}
                request(self.inbox,receipt['message_id'],value)
                return self._toast('success','已保存范围操作，正在核对。')
            if value.get('kind')=='scope_page':
                from .feishu_views import write
                with connect(self.inbox.store.path) as db:
                    part=db.execute('SELECT preview_json FROM feishu_parts WHERE app_id=? AND message_id=? AND position=?',
                                    (self.inbox.app_id,receipt['message_id'],value.get('position'))).fetchone()
                current=json.loads(part[0]) if part and part[0] else {}
                if current.get('token')!=value.get('scope_token') or type(value.get('page')) is not int or not 0<=value['page']<=100000:
                    return self._toast('error','范围已更新，请使用最新卡片。')
                write(self.inbox,receipt['message_id'],{'token':current['token'],'page':value['page']})
                return self._toast('success','已切换范围段落。')
            if value.get('kind')!='source_confirmation':
                return self._toast('error','无法识别这个操作。')
            item_id=value.get('item_id')
            if type(item_id) is not int:
                return self._toast('error','待办标识无效。')
            from .feishu_scopes import items
            if not any(row['item_id']==item_id for row in items(self.inbox,receipt['message_id'])):
                return self._toast('error','这项待办不属于该投递。')
            row=self.inbox.store.item_bundle(item_id)
            if row['state']!='waiting_user' or not row['confirmation_json']:
                return self._toast('info','该待办已处理，请查看最新状态。')
            pending=json.loads(row['confirmation_json'])
            if pending.get('token')!=value.get('token') and not value.get('concern_revision'):
                return self._toast('error','待办已更新，请使用最新卡片。')
            from .feishu_views import chunks,read,write
            concern=next((c for c in pending.get('concerns',[]) if c.get('audio_name')==value.get('concern_id')),None)
            if value.get('action') in {'page','draft','submit_draft'}:
                if pending['concerns'] and concern is None:
                    return self._toast('error','疑点已更新，请使用最新卡片。')
                pages=chunks(concern['text']) if concern else chunks(pending['snapshot'],2000)
                from .confirmation_revision import revision
                draft_token=revision(pending,concern) if concern else pending['token']
                view=read(self.inbox,receipt['message_id'],draft_token)
                if value['action'] in {'page','draft'}:
                    page=value.get('page')
                    if type(page) is not int or not 0<=page<len(pages):
                        return self._toast('error','段落位置无效。')
                    if value['action']=='draft':
                        draft=(action.get('form_value') or {}).get('correction','')
                        if concern is None or not isinstance(draft,str) or not draft.strip() or len(draft)>1000:
                            return self._toast('error','请输入本段文字（最多1000字）。')
                        view.setdefault('edits',{})[str(page)]=draft
                    view['page']=page
                    write(self.inbox,receipt['message_id'],view)
                    return self._toast('success','本段草稿已保存，尚未提交核对。' if value['action']=='draft' else '已切换段落。')
                if concern is None:return self._toast('error','请使用完成核对。')
                if not view.get('edits'):return self._toast('error','没有可提交的分段修改，请先保存草稿。')
                value={**value,'action':'manual','value':''.join(view.get('edits',{}).get(str(n),part) for n,part in enumerate(pages))}
                action={**action,'form_value':{'correction':value['value']}}
            if value.get('action')=='finish_transcript':
                if pending.get('kind')=='image' or pending.get('concerns'):
                    return self._toast('error','请先完成当前疑点确认。')
                engine=self.distiller() if callable(self.distiller) else self.distiller
                engine.finish_transcript(item_id,token=value['token'])
                if self.wake:self.wake()
                return self._toast('success','已完成核对。')
            if not any(c.get('audio_name')==value.get('concern_id') for c in pending.get('concerns',[])):
                return self._toast('info','这项疑点已处理，请查看剩余待办。')
            submitted=value.get('value','')
            if value.get('action')=='candidate' and 'candidate_index' in value:
                index=value['candidate_index']
                if concern is None or type(index) is not int or not 0<=index<len(concern.get('candidates',[])):
                    return self._toast('error','候选文字已更新。')
                submitted=concern['candidates'][index]
            if value.get('action')=='manual':
                submitted=(action.get('form_value') or {}).get('correction','')
            if not isinstance(submitted,str):
                raise ValueError('请输入正确文字。')
            engine=self.distiller() if callable(self.distiller) else self.distiller
            engine.resolve(item_id,value.get('action'),submitted,token=value.get('token',''),concern_id=value.get('concern_id',''), **({'concern_revision':value['concern_revision']} if value.get('concern_revision') else {}))
            if self.wake:self.wake()
            return self._toast('success','已保存。')
        except ValueError as error:
            return self._toast('error',str(error))

    @staticmethod
    def _toast(kind,text):
        return {'toast':{'type':kind,'content':text}}
