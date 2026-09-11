"""Project receipt and pending state; never publish derived knowledge to Feishu."""
import json
import re

from .database import connect
from .image_confirmation import crop_original


def text(value):
    return {'tag':'div','text':{'tag':'plain_text','content':value}}


def button(label,name,value,*,submit=False):
    result={'tag':'button','name':name,'text':{'tag':'plain_text','content':label},
            'type':'primary' if submit else 'default',
            'behaviors':[{'type':'callback','value':value}]}
    if submit:result['form_action_type']='submit'
    return result


def option_rows(options):
    """Keep short choices together without squeezing long source quotations."""
    rows=[];short=[]
    def flush():
        if short:
            rows.append({'tag':'column_set','flex_mode':'flow','columns':[
                {'tag':'column','width':'auto','elements':[option]} for option in short]})
            short.clear()
    for option in options:
        if len(option['text']['content'])>12:
            flush();rows.append(option)
        else:
            short.append(option)
            if len(short)==3:flush()
    flush()
    return rows


def concern_context(snapshot,concern):
    start,end=concern.get('start'),concern.get('end')
    if not (isinstance(start,int) and isinstance(end,int) and
            0<=start<end<=len(snapshot) and snapshot[start:end]==concern['text']):
        # Older pending records may lack offsets. Never select an ambiguous match.
        if not concern['text'] or snapshot.count(concern['text'])!=1:return None
        start=snapshot.index(concern['text']);end=start+len(concern['text'])
    from .confirmation_display import local_choices
    display=local_choices({**concern,'start':start,'end':end})
    start,end=display['start'],display['end']
    left=max(0,start-12);right=min(len(snapshot),end+12)
    target=snapshot[start:end]
    if len(target)>120:target=target[:60]+'…'+target[-60:]
    excerpt=('…' if left else '')+snapshot[left:start]+'【'+target+'】'+snapshot[end:right]+('…' if right<len(snapshot) else '')
    return re.sub(r'\s+', ' ', excerpt).strip()


class FeishuCards:
    def __init__(self,inbox,distiller,media,collections=None):
        self.inbox,self.distiller,self.media=inbox,distiller,media
        self.collections=collections

    def card(self,message_id):
        with connect(self.inbox.store.path) as db:
            receipt=db.execute('SELECT * FROM feishu_receipts WHERE app_id=? AND message_id=?',
                (self.inbox.app_id,message_id)).fetchone()
            parts=db.execute('SELECT * FROM feishu_parts WHERE app_id=? AND message_id=? ORDER BY position',
                (self.inbox.app_id,message_id)).fetchall()
        if receipt is None:raise ValueError('unknown receipt')
        elements=[text('已收到这条投递。')]
        title='知识蒸馏器'
        if receipt['state']=='needs_desktop' and receipt['error']:
            elements=[text(receipt['error']),button('重试下载','retry_receipt',{'kind':'retry_receipt'})]
        elif receipt['state']=='rejected':
            elements=[text(receipt['error'])]
        elif receipt['state']=='waiting_input' and receipt['content_kind'] is None:
            title='请选择处理方式'
            elements=[text('这条消息同时包含正文和链接，请选择本次要处理的内容。'),
                button('处理链接','choose_links',{'kind':'content_choice','choice':'links'}),
                button('完整文本','choose_text',{'kind':'content_choice','choice':'text'})]
        else:
            from .feishu_scopes import items as receipt_items
            items=receipt_items(self.inbox,message_id)
            scope_elements=self._scope(parts,message_id)
            waiting=[r for r in items if r['state']=='waiting_user' and r['confirmation_json']]
            if waiting:
                title='有内容待你确认'
                # One decision at a time keeps the form within client limits.
                # Every update reprojects the authoritative pending token.
                from .confirmation_display import concern_total
                pending=json.loads(waiting[0]['confirmation_json'])
                total=concern_total(pending)
                remaining=len(pending['concerns'])
                elements=[text(f'待确认 {total-remaining+1} / {total}' if remaining else '请核对全文。')]
                elements.extend(self._pending(waiting[0],message_id))
            elif scope_elements:
                title='请确认内容范围'
                elements.extend(scope_elements)
            elif items and all(r['state']=='failed' and r['error_code']=='knowledge_not_qualified' for r in items):
                title='未生成知识'
                elements.append(text('内容已保存，但不足以形成有依据的知识，因此没有生成知识笔记。可投递包含具体观点或事实的正文或链接。'))
            elif receipt['state']=='needs_desktop' or any(r['state']=='failed' for r in items):
                for part in parts:
                    if part['error']:
                        elements.append(text(f"第 {part['position'] + 1} 项：{part['error']}"))
                if any(p['error'] for p in parts):
                    elements.append(text('这些项目尚未建立处理任务。请按上述原因处理后重新投递对应内容；已成功接收的项目无需重投。'))
                if any(r['state']=='failed' for r in items):
                    elements.append(text('已建立的任务保留，可在电脑查看失败原因并重试。'))
            elif items and all(r['state']=='succeeded' for r in items):
                elements.append(text('这条投递已处理完成，可在电脑查看。'))
            else:
                elements.append(text('内容已保存，将由电脑继续处理。'))
        with connect(self.inbox.store.path) as db:
            latest=db.execute('SELECT result FROM feishu_action_queue WHERE app_id=? AND message_id=? ORDER BY id DESC LIMIT 1',
                              (self.inbox.app_id,message_id)).fetchone()
        if latest:
            result=json.loads(latest['result']) if latest['result'] else None
            elements.append(text(result.get('toast',{}).get('content','操作已处理。') if result else '已接收操作，正在保存。'))
        return {'schema':'2.0','config':{'update_multi':True,'enable_forward':False},
                'header':{'title':{'tag':'plain_text','content':title}},'body':{'elements':elements}}

    def _pending(self,row,message_id):
        pending=json.loads(row['confirmation_json'])
        base={'kind':'source_confirmation','item_id':row['item_id'],'token':pending['token']}
        from .confirmation_display import english_assistance
        from .feishu_views import chunks,read,controls
        view=read(self.inbox,message_id,pending['token'])
        if not pending['concerns']:
            pages=chunks(pending['snapshot'],2000)
            page=min(view.get('page',0),len(pages)-1)
            return [text(pages[page]),*controls(base,page,len(pages)),button('完成核对并继续','finish_transcript',
                {**base,'action':'finish_transcript'})]
        concern=pending['concerns'][0]
        base['concern_id']=concern['audio_name']
        from .confirmation_revision import revision
        base['concern_revision']=revision(pending,concern)
        view=read(self.inbox,message_id,base['concern_revision'])
        pages=chunks(concern['text'])
        page=min(view.get('page',0),len(pages)-1)
        current=view.get('edits',{}).get(str(page),pages[page])
        context=concern_context(pending['snapshot'],concern)
        elements=[text(context)] if context else [text('待核对：'+current)]
        if len(pages)>1:elements.extend(controls(base,page,len(pages)))
        if pending.get('kind')=='image':
            member=next((m for m in self.inbox.store.media_members(row['material_id'])
                         if m['member_id']==concern['member_id']),None)
            if member is None:raise ValueError('confirmation_image_unavailable')
            elements.append({'tag':'img','img_key':self.media.image(crop_original(member,concern).getvalue()),
                             'alt':{'tag':'plain_text','content':'疑点附近的原图，含上下文'},'preview':True})
        else:
            engine=self.distiller() if callable(self.distiller) else self.distiller
            path=engine.confirmation_audio(row['item_id'],concern['audio_name'])
            if path is None:raise ValueError('confirmation_audio_unavailable')
            elements.append({'tag':'audio','file_key':self.media.audio(path)})
        english=english_assistance(pending.get('snapshot',''))
        options=[]
        candidates=concern.get('candidates',[])
        if english and candidates==[concern['text']]:
            elements.append(text('当前没有其他有依据的听法。请结合原音选择保留原文、自定义修正或无法确认。'))
        for index,candidate in enumerate(candidates):
            if pending.get('kind')=='image' and candidate!=concern['text']:continue
            if english:
                explanation=concern.get('candidate_explanations',{}).get(candidate)
                copy=f'候选 {index+1}\n{candidate}'
                if explanation:copy+='\n'+explanation
                elements.extend(text(part) for part in chunks(copy,2000))
                label='保留原文' if candidate==concern['text'] else f'采用候选 {index+1}'
            else:
                label=candidate[:200]
            options.append(button(label,'candidate_'+str(index),{**base,'action':'candidate','candidate_index':index}))
        elements.extend(option_rows(options))
        if english and concern.get('reason'):
            elements.extend(text(part) for part in chunks('需要审核的原因\n'+concern['reason'],2000))
        actions=[button('保存本段草稿' if len(pages)>1 else '保存修改','save_correction',
                        {**base,'action':'draft' if len(pages)>1 else 'manual','page':page},submit=True)]
        if pending.get('kind')!='image':
            unable=button('无法确认','unable',{**base,'action':'unable'},submit=True)
            unable['type']='default'
            actions.append(unable)
        elements.append({'tag':'form','name':'correction_form','elements':[
            {'tag':'input','name':'correction','default_value':view.get('edits',{}).get(str(page),''),
             'placeholder':{'tag':'plain_text','content':'自定义（可选）' if english else '自定义输入…'},'input_type':'multiline_text' if '\n' in current else 'text',
             'rows':1,'auto_resize':True,'max_rows':4,
             'max_length':1000,'required':False},
            *option_rows(actions)]})
        if len(pages)>1:
            elements.append(text('分段修改保存在本机；保存当前段后，点击“提交全部修改”才会完成本项核对。'))
            elements.append(button('提交全部修改','submit_draft',{**base,'action':'submit_draft'}))
        return elements


    def _scope(self,parts,message_id):
        for part in parts:
            if not part['preview_json']:continue
            value=json.loads(part['preview_json']);token=value['token']
            with connect(self.inbox.store.path) as db:
                if db.execute('SELECT 1 FROM collection_confirmations WHERE token=?',(token,)).fetchone():continue
            if value.get('request'):return [text('已保存你的选择，正在核对内容范围。')]
            if self.collections is None:return [text('等待确认内容范围。')]
            draft=self.collections._draft(token)
            base={'kind':'scope','position':part['position'],'scope_token':token}
            if draft['scopes']:
                lines=[]
                for scope in draft['scopes']:
                    lines.append(scope.title+' · 共 '+str(len(scope.members))+' 条')
                    lines.extend(str(n+1)+'. '+(m.title or m.item_id)+( '' if m.supported else '（暂不支持）') for n,m in enumerate(scope.members))
                elements=self._scope_pages(message_id,token,'\n'.join(lines),base)
                same_topic=any(s.kind=='same_topic' for s in draft['scopes'])
                elements.append(button('确认同题范围并开始' if same_topic else '确认范围并开始','confirm_scope',
                    {**base,'command':'confirm','same_topic':same_topic}))
                return elements
            choices=draft.get('choices',[])
            elements=[text(draft.get('profile_title','选择处理范围')),
                button('主页全部内容（含未归类作品）','full_profile',{**base,'command':'select','selected':['full_profile']})]
            if choices:
                elements.extend([button('所有合集','all_collections',{**base,'command':'select','selected':['all_collections']}),
                    *self._scope_pages(message_id,token,'\n'.join(str(n+1)+'. '+choice['title'] for n,choice in enumerate(choices)),base),
                    {'tag':'form','name':'scope_form','elements':[
                        {'tag':'input','name':'scope_selection','placeholder':{'tag':'plain_text','content':'输入合集序号，例如 1,3'},
                         'required':True,'max_length':1000},
                        button('预览所选合集','select_scopes',{**base,'command':'select'},submit=True)]}])
            return elements
        return []

    def _scope_pages(self,message_id,token,content,base):
        from .feishu_views import chunks,read,controls
        pages=chunks(content,2000)
        page=min(read(self.inbox,message_id,token).get('page',0),len(pages)-1)
        return [text(pages[page]),*controls({**base,'kind':'scope_page'},page,len(pages))]
