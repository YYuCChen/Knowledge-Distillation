import json
from types import SimpleNamespace
import pytest
from knowledge_distiller.faithful_review import _parse_candidate
from knowledge_distiller.v1.llm import LLMRequestError
from knowledge_distiller.v1.reviewer import suggest_candidates
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.store import Store


CONCERN={'audio_name':'one.wav','text':'when','start':7,'end':11,'reason':'疑似听写错误','candidates':['when']}
CHOICES=[{'text':'when','meaning_zh':'当……时；保留当前原文'}, {'text':'well','meaning_zh':'嗯、那么；可能是口语衔接词'}]
class Client:
    def __init__(self, choices=CHOICES):self.choices=choices
    def complete(self, **kwargs):
        assert 'snapshot' in json.loads(kwargs['user'])
        return json.dumps({'suggestions':[{'id':'one.wav','choices':self.choices}]})


def test_suggestions_keep_original_and_chinese_support():
    assert suggest_candidates(Client(), 'So sad when it happens.', [CONCERN])['one.wav']==CHOICES


@pytest.mark.parametrize('choices', [CHOICES[1:], [{'text':'when','meaning_zh':'English only'}], [*CHOICES,CHOICES[0]]])
def test_invalid_suggestions_do_not_silently_drop_original(choices):
    with pytest.raises(LLMRequestError):suggest_candidates(Client(choices), 'So sad when it happens.', [CONCERN])


def test_review_parses_explanations_without_replacing_candidate_text():
    text='So sad when it happens.'
    candidate=_parse_candidate(json.dumps({'candidate_text':text,'issues':[{
        'issue_text':'when','occurrence':0,'reason':'可能是衔接词','meaning_may_change':True,
        'candidate_readings':['when','well'],'candidate_explanations':[
            {'reading':c['text'],'meaning_zh':c['meaning_zh']} for c in CHOICES]}]}))
    assert candidate.text==text
    assert dict(candidate.concerns[0].candidate_explanations)['well'].startswith('嗯')


def test_saving_options_preserves_pending_snapshot_and_state_and_rejects_race(tmp_path):
    store=Store(tmp_path/'db');store.initialize()
    store.save_connection('youtube',None,browser_context='owned:'+'a'*32)
    item=store.create_item('https://www.youtube.com/watch?v=aaaaaaaaaaa')
    store.mark_waiting(item,{'snapshot':'So sad when it happens.','concerns':[CONCERN],'review_required':False,'resolved':[]})
    def suggest(snapshot,concerns):return {'one.wav':CHOICES}
    service=Distiller(store=store,source=None,normalizer=None,recognizer=None,
        reviewer=SimpleNamespace(suggest_candidates=suggest),confirmation_clipper=None,
        knowledge_model=None,runtime_root=tmp_path,vault=None)
    old=store.item_bundle(item);token=json.loads(old['confirmation_json'])['token']
    service.suggest_candidates(item,token=token)
    new=store.item_bundle(item);pending=json.loads(new['confirmation_json'])
    assert pending['snapshot']==json.loads(old['confirmation_json'])['snapshot']
    assert new['state']=='waiting_user' and new['source_fact_id'] is None
    assert pending['resolved']==[] and pending['concerns'][0]['candidates']==['when','well']
    assert pending['token']!=token
    with pytest.raises(ValueError):service.suggest_candidates(item,token=token)
    def racing(snapshot, concerns):
        store.mark_waiting(item,{'snapshot':'Changed by user','concerns':[]})
        return {'one.wav':CHOICES}
    service.reviewer=SimpleNamespace(suggest_candidates=racing)
    with pytest.raises(ValueError):service.suggest_candidates(item,token=pending['token'])
    assert json.loads(store.item_bundle(item)['confirmation_json'])['snapshot']=='Changed by user'


def test_suggestions_http_never_wakes_worker_and_full_transcript_routes_are_gone(tmp_path):
    from knowledge_distiller.v1.web import create_app
    store=Store(tmp_path/'db');store.initialize()
    item=store.create_item('https://v.douyin.com/a/')
    store.mark_waiting(item,{'snapshot':'So sad when it happens.','concerns':[CONCERN],'review_required':False})
    service=Distiller(store=store,source=None,normalizer=None,recognizer=None,
        reviewer=SimpleNamespace(suggest_candidates=lambda snapshot,concerns:{'one.wav':CHOICES}),
        confirmation_clipper=None,knowledge_model=None,runtime_root=tmp_path,vault=None)
    wakes=[];client=create_app(store,service,wake_worker=lambda:wakes.append(True)).test_client()
    token=json.loads(store.item_bundle(item)['confirmation_json'])['token']
    assert client.post(f'/items/{item}/suggestions',data={'token':token,'concern_id':'one.wav'}).status_code==302
    assert client.post(f'/items/{item}/suggestions',data={'token':token,'concern_id':'one.wav'}).status_code==409
    assert store.item_bundle(item)['state']=='waiting_user' and not wakes
    assert client.post(f'/items/{item}/transcript',data={'token':token,'action':'finish'}).status_code==404
    assert client.get(f'/items/{item}/transcript-audio').status_code==404
    assert client.get(f'/items/{item}/transcript-location').status_code==404
