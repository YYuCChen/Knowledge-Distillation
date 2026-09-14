import json
from types import SimpleNamespace
from unittest.mock import Mock

from .test_feishu_inbox import inbox, message
from knowledge_distiller.v1.feishu_inbox import history_message
from knowledge_distiller.v1.feishu_intake import FeishuIntake
from knowledge_distiller.v1.feishu_cards import FeishuCards
from knowledge_distiller.v1.feishu_actions import FeishuActions
from knowledge_distiller.v1.database import connect


def test_card_choice_then_receipt_does_not_expose_knowledge(inbox):
    inbox.receive(history_message(message(text='说明 https://example.test/reference')),history=True)
    intake=FeishuIntake(inbox,None)
    intake.process('om_1')
    projection=FeishuCards(inbox,None,None)
    card=projection.card('om_1')
    assert card['header']['title']['content']=='待你操作'
    assert {e['behaviors'][0]['value']['choice'] for e in card['body']['elements'] if e['tag']=='button'}=={'text','links'}
    intake.choose('om_1','text');intake.process('om_1')
    assert '说明' not in json.dumps(projection.card('om_1'),ensure_ascii=False)


def test_unqualified_content_explains_outcome_without_exposing_model_output(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    inbox.store.mark_failed(item,'distilling','knowledge_not_qualified',rejection_reason='private model explanation')
    card=FeishuCards(inbox,None,None).card('om_1')
    assert card['header']['title']['content']=='未形成知识'
    assert '没有生成知识笔记' in json.dumps(card,ensure_ascii=False)
    assert 'private model explanation' not in json.dumps(card)
    inbox.store.mark_failed(item,'distilling','model_unavailable')
    assert '未形成知识' != FeishuCards(inbox,None,None).card('om_1')['header']['title']['content']


def test_pending_audio_form_has_current_token_and_whole_text(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    inbox.store.mark_waiting(item,{'snapshot':'需要核对的文字','concerns':[
        {'audio_name':'clip-1','text':'需要核对的文字','candidates':['候选文字']} ]})
    media=SimpleNamespace(audio=Mock(return_value='file_own_bot'))
    engine=SimpleNamespace(confirmation_audio=Mock(return_value='/owned/clip.wav'))
    card=FeishuCards(inbox,engine,media).card('om_1')
    elements=card['body']['elements']
    assert {'tag':'audio','file_key':'file_own_bot'} in elements
    form=next(e for e in elements if e['tag']=='form')
    assert form['elements'][0]['default_value']==''
    assert form['elements'][0]['placeholder']['content']=='自定义输入…'
    action=form['elements'][1]['columns'][0]['elements'][0]['behaviors'][0]['value']
    assert action['token']==json.loads(inbox.store.item_bundle(item)['confirmation_json'])['token']
    assert action['concern_id']=='clip-1'


def test_english_pending_matches_desktop_review_information(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.youtube.com/watch?v=1',receipt_key=(inbox.app_id,'om_1',0))
    original='We are looking at a five thousand share order.'
    alternative='We are looking at a five-thousand-share order.'
    reason='这句话与后文衔接不明确，需要回听原音。'
    explanations={
        original:'意为“我们正在查看一笔五千股的订单”。',
        alternative:'意思相同，但用连字符标出复合定语。',
    }
    inbox.store.mark_waiting(item,{'snapshot':original+' Later English context.','concerns':[
        {'audio_name':'clip-1','text':original,'candidates':[original,alternative],
         'candidate_explanations':explanations,'reason':reason}]})
    engine=SimpleNamespace(confirmation_audio=Mock(return_value='/owned/clip.wav'))
    card=FeishuCards(inbox,engine,SimpleNamespace(audio=lambda p:'file_audio')).card('om_1')
    rendered=json.dumps(card,ensure_ascii=False)
    assert original in rendered
    assert alternative in rendered
    assert explanations[original] in rendered
    assert explanations[alternative] in rendered
    assert reason in rendered
    buttons=[column['elements'][0] for e in card['body']['elements'] if e.get('tag')=='column_set'
             for column in e['columns'] if column['elements'][0].get('name','').startswith('candidate_')]
    assert [b['text']['content'] for b in buttons]==['保留原文','采用候选 2']
    assert [b['behaviors'][0]['value']['candidate_index'] for b in buttons]==[0,1]
    form=next(e for e in card['body']['elements'] if e['tag']=='form')
    assert form['elements'][0]['placeholder']['content']=='自定义（可选）'


def test_single_english_reading_explains_why_there_is_no_fake_alternative(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.youtube.com/watch?v=1',receipt_key=(inbox.app_id,'om_1',0))
    original='We are looking at a five thousand share order.'
    inbox.store.mark_waiting(item,{'snapshot':original,'concerns':[{
        'audio_name':'clip-1','text':original,'candidates':[original],
        'candidate_explanations':{original:'意为一笔五千股的订单。'},'reason':'需要回听。'}]})
    engine=SimpleNamespace(confirmation_audio=Mock(return_value='/owned/clip.wav'))
    card=FeishuCards(inbox,engine,SimpleNamespace(audio=lambda p:'file_audio')).card('om_1')
    rendered=json.dumps(card,ensure_ascii=False)
    assert '当前没有其他有依据的听法' in rendered
    assert '"content": "'+original+'"' not in rendered
    assert '保留原文' in rendered


def test_whole_review_finish_uses_same_desktop_cas_path(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    inbox.store.mark_waiting(item,{'snapshot':'已经逐项核对的原文','concerns':[],'review_required':True})
    with connect(inbox.store.path) as db:db.execute("UPDATE feishu_receipts SET card_id='om_card'")
    card=FeishuCards(inbox,None,None).card('om_1')
    value=next(e for e in card['body']['elements'] if e.get('name')=='finish_transcript')['behaviors'][0]['value']
    engine=SimpleNamespace(finish_transcript=Mock(side_effect=lambda *a,**kw:inbox.store.resolve_confirmation(
        item,inbox.store.item_bundle(item)['confirmation_json'],next_confirmation={'snapshot':'核对完成','concerns':[]})))
    payload={'event':{'operator':{'open_id':'ou_owner'},'context':{'open_chat_id':'oc_private','open_message_id':'om_card'},'action':{'value':value}}}
    actions=FeishuActions(inbox,None,engine)
    assert actions.handle(payload)['toast']['type']=='success'
    assert actions.handle(payload)['toast']['type']=='info'
    engine.finish_transcript.assert_called_once_with(item,token=value['token'])


def test_long_review_is_paginated_and_long_correction_survives_restart(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    original='甲乙丙丁'*800
    inbox.store.mark_waiting(item,{'snapshot':original,'concerns':[
        {'audio_name':'clip-1','text':original,'candidates':[]} ]})
    with connect(inbox.store.path) as db:db.execute("UPDATE feishu_receipts SET card_id='om_card'")
    media=SimpleNamespace(audio=Mock(return_value='file_own_bot'))
    engine=SimpleNamespace(confirmation_audio=Mock(return_value='/owned/clip.wav'),resolve=Mock())
    cards=FeishuCards(inbox,engine,media)
    elements=cards.card('om_1')['body']['elements']
    form=next(e for e in elements if e['tag']=='form')
    assert form['elements'][0]['default_value']==''
    action=form['elements'][1]['columns'][0]['elements'][0]['behaviors'][0]['value']
    payload={'event':{'operator':{'open_id':'ou_owner'},'context':{'open_chat_id':'oc_private','open_message_id':'om_card'},
                      'action':{'value':action,'form_value':{'correction':'已修改第一段'}}}}
    assert FeishuActions(inbox,None,engine).handle(payload)['toast']['type']=='success'
    engine.resolve.assert_not_called()
    # Recreate renderer/handler: the durable draft, not process memory, owns text.
    elements=FeishuCards(inbox,engine,media).card('om_1')['body']['elements']
    form=next(e for e in elements if e['tag']=='form')
    assert form['elements'][0]['default_value']=='已修改第一段'
    value=next(e for e in elements if e.get('name')=='submit_draft')['behaviors'][0]['value']
    payload['event']['action']={'value':value}
    assert FeishuActions(inbox,None,engine).handle(payload)['toast']['type']=='success'
    assert engine.resolve.call_args.args[2]=='已修改第一段'+original[800:]
    assert len(json.dumps(cards.card('om_1'),ensure_ascii=False).encode())<30000
    inbox.store.mark_waiting(item,{'snapshot':'新一轮核对','concerns':[]})
    assert FeishuActions(inbox,None,engine).handle(payload)['toast']['type']=='error'
    assert '已修改第一段' not in json.dumps(cards.card('om_1'),ensure_ascii=False)


def test_whole_snapshot_card_bounded_without_truncating_source(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    snapshot='完整正文'*20000
    inbox.store.mark_waiting(item,{'snapshot':snapshot,'concerns':[],'review_required':True})
    card=FeishuCards(inbox,None,None).card('om_1')
    assert len(json.dumps(card,ensure_ascii=False).encode())<30000
    assert json.loads(inbox.store.item_bundle(item)['confirmation_json'])['snapshot']==snapshot


def test_short_concern_shows_exact_context_single_line_and_parallel_choices(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    snapshot='此前也说到臣辅。现在列举君臣辅佐使，然后解释各自作用。'
    start=snapshot.index('臣辅',8)
    inbox.store.mark_waiting(item,{'snapshot':snapshot,'concerns':[
        {'audio_name':'clip-1','text':'臣辅','start':start,'end':start+2,'candidates':['臣辅','臣']} ]})
    engine=SimpleNamespace(confirmation_audio=Mock(return_value='/owned/clip.wav'))
    card=FeishuCards(inbox,engine,SimpleNamespace(audio=lambda p:'file_audio')).card('om_1')
    elements=card['body']['elements']
    content='\n'.join(e['text']['content'] for e in elements if e['tag']=='div')
    assert '君【臣辅】佐使' in content
    assert content.count('【臣辅】')==1
    columns=next(e for e in elements if e['tag']=='column_set')['columns']
    assert [c['elements'][0]['text']['content'] for c in columns]==['臣辅','臣']
    form=next(e for e in elements if e['tag']=='form')
    assert form['elements'][0]['input_type']=='text'
    assert form['elements'][0]['default_value']==''


def test_context_never_guesses_repeated_text_and_bounds_long_excerpt():
    from knowledge_distiller.v1.feishu_cards import concern_context
    assert concern_context('甲乙甲乙',{'text':'甲乙'}) is None
    original='前'*100+'疑'*10000+'后'*100
    result=concern_context(original,{'text':'疑'*10000,'start':100,'end':10100})
    assert len(result)<230
    assert result.startswith('…'+'前'*48+'【')
    assert result.endswith('】'+'后'*48+'…')


def test_context_matches_shared_full_marked_window():
    from knowledge_distiller.v1.feishu_cards import concern_context
    from knowledge_distiller.v1.confirmation_display import context_window
    phrase='它涉及到君臣辅佐使的配合'
    snapshot='前'*80+phrase+'后'*80
    concern={'text':phrase,'start':80,'end':80+len(phrase),
             'candidates':[phrase,'它涉及到君臣佐使的配合']}
    display=context_window(snapshot, concern)
    assert concern_context(snapshot,concern)==(
        '…'+display['before']+'【'+display['marked']+'】'+display['after']+'…')


def test_review_fraction_tracks_same_material_across_clients_and_restart(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    concerns=[{'audio_name':f'clip-{n}','text':'疑','candidates':[]} for n in range(3)]
    inbox.store.mark_waiting(item,{'snapshot':'疑疑疑','concerns':concerns})
    engine=SimpleNamespace(confirmation_audio=lambda *a:'/owned/clip.wav')
    def progress():
        return FeishuCards(inbox,engine,SimpleNamespace(audio=lambda p:'file')).card('om_1')['body']['elements'][0]['text']['content']
    assert progress()=='待确认 1 / 3'
    row=inbox.store.item_bundle(item);pending=json.loads(row['confirmation_json'])
    # Both clients consume this same pending state; no card click counter.
    inbox.store.resolve_confirmation(item,row['confirmation_json'],next_confirmation={
        **pending,'concerns':concerns[1:],'resolved':[{'text':'疑','replacement':'字','by':'human'}]})
    assert progress()=='待确认 2 / 3'
    assert progress()=='待确认 2 / 3'
    row=inbox.store.item_bundle(item);pending=json.loads(row['confirmation_json'])
    inbox.store.resolve_confirmation(item,row['confirmation_json'],next_confirmation={
        **pending,'concerns':concerns[2:],'deferred_concerns':[concerns[1]]})
    assert progress()=='待确认 3 / 3'


def test_existing_pending_progress_excludes_free_text_edits():
    from knowledge_distiller.v1.confirmation_display import concern_total
    assert concern_total({'concerns':[{}]*25,'resolved':[{}]*4+[{'action':'local_transcription'}]})==29


def test_context_collapses_paragraph_breaks_without_changing_source():
    from knowledge_distiller.v1.feishu_cards import concern_context
    snapshot='前面\n\n疑点，后面\r\n\r\n那…'
    concern={'text':'疑点','start':4,'end':6}
    assert concern_context(snapshot,concern)=='前面 【疑点】，后面 那…'
    assert snapshot=='前面\n\n疑点，后面\r\n\r\n那…'


def test_save_and_unable_share_row_and_blank_input_does_not_block_unable(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/1',receipt_key=(inbox.app_id,'om_1',0))
    inbox.store.mark_waiting(item,{'snapshot':'疑','concerns':[{'audio_name':'clip','text':'疑','candidates':[]}]})
    engine=SimpleNamespace(confirmation_audio=lambda *a:'/owned/clip.wav',resolve=Mock())
    card=FeishuCards(inbox,engine,SimpleNamespace(audio=lambda p:'file')).card('om_1')
    form=next(e for e in card['body']['elements'] if e['tag']=='form')
    assert form['elements'][0]['required'] is False
    buttons=[column['elements'][0] for column in form['elements'][1]['columns']]
    assert [b['text']['content'] for b in buttons]==['保存修改','无法确认']
    assert all(b['form_action_type']=='submit' for b in buttons)
    with connect(inbox.store.path) as db:db.execute("UPDATE feishu_receipts SET card_id='om_card'")
    value=buttons[1]['behaviors'][0]['value']
    payload={'event':{'operator':{'open_id':'ou_owner'},
        'context':{'open_chat_id':'oc_private','open_message_id':'om_card'},
        'action':{'value':value,'form_value':{'correction':''}}}}
    assert FeishuActions(inbox,None,engine).handle(payload)['toast']['type']=='success'
    assert engine.resolve.call_args.args[1:]==('unable','')


def test_pre_item_error_is_actionable_without_a_nonexistent_desktop_task(inbox):
    inbox.receive(history_message(message()), history=True)
    with connect(inbox.store.path) as db:
        db.execute("UPDATE feishu_receipts SET state='needs_desktop' WHERE message_id='om_1'")
        db.execute("INSERT INTO feishu_parts(app_id,message_id,position,error) VALUES (?,?,?,?)",
                   (inbox.app_id, 'om_1', 0, '该视频仍在直播，请结束后重新投递。'))
    rendered = json.dumps(FeishuCards(inbox, None, None).card('om_1'), ensure_ascii=False)
    assert '该视频仍在直播' in rendered
    assert '重新投递' in rendered
    assert '有项目需要在电脑处理' not in rendered


def test_group_card_32_member_capacity_and_visible_callback_scope(inbox):
    inbox.receive(history_message(message()),history=True)
    item=inbox.store.create_item('https://www.douyin.com/video/705',receipt_key=(inbox.app_id,'om_1',0))
    ids=[f'{n:064x}' for n in range(32)]
    inbox.store.mark_waiting(item,{'snapshot':'词'*32,'concerns':[
        {'start':n,'end':n+1,'text':'词','audio_name':str(n),'concern_uid':uid,
         'candidates':['词','字','表达','知识']} for n,uid in enumerate(ids)],
        'groups':[{'group_id':'g'*64,'member_uids':ids,'equivalence_basis':{'kind':'fixture'}}]})
    engine=SimpleNamespace(confirmation_audio=lambda *args:'/synthetic/audio.wav')
    card=FeishuCards(inbox,engine,SimpleNamespace(audio=lambda p:'synthetic-key')).card('om_1')
    encoded=json.dumps(card,ensure_ascii=False).encode('utf-8')
    assert len(encoded)<30000
    assert '同类疑点共 32 处' in encoded.decode()
    assert '待确认组 1 · 待确认位置 32' in encoded.decode()
    def buttons(value):
        if isinstance(value,dict):
            if value.get('tag')=='button':yield value
            for child in value.values():yield from buttons(child)
        elif isinstance(value,list):
            for child in value:yield from buttons(child)
    callbacks=[b['behaviors'][0]['value'] for b in buttons(card) if b.get('behaviors')]
    batch=[c for c in callbacks if c.get('action') in {'candidate','keep','manual'}]
    assert batch and all(c['selected_member_uids']==ids for c in batch)
    assert next(c for c in callbacks if c.get('action')=='unable')['selected_member_uids']==[ids[0]]


def test_long_optional_group_candidates_cannot_overflow_card_or_hide_scope():
    from knowledge_distiller.v1.feishu_cards import fit_card,button,text
    elements=[text('同类疑点共 32 处；作用于全部 32 处'),text('完整上下文 1 / 2 段：内容'),
              button('下段上下文','context_1',{'action':'group_context_page','page':1})]
    elements.extend(button('长候选','group_choice_'+str(n),{'value':'长'*10000}) for n in range(4))
    card=fit_card({'body':{'elements':elements},'header':{'title':{'content':'待你操作'}}})
    value=json.dumps(card,ensure_ascii=False)
    assert len(value.encode())<30000
    assert '作用于全部 32 处' in value
    assert '下段上下文' in value
    assert 'group_choice_' not in value
