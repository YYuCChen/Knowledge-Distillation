import json
from copy import deepcopy
import pytest
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.domain import CapturedMaterial
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.confirmation_groups import form_groups
from knowledge_distiller.v1.confirmation_revision import ConfirmationConflict
from .test_confirmation_groups import sample


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    item=store.create_item('https://www.douyin.com/video/123')
    media=tmp_path/'synthetic.mp4';media.write_bytes(b'synthetic-media')
    store.attach_material(item,CapturedMaterial('douyin','123','fixture','fixture',{},media,1))
    store.mark_waiting(item,form_groups(sample(),item))
    service=Distiller(store=store,source=None,normalizer=None,recognizer=None,reviewer=None,
        confirmation_clipper=None,knowledge_model=None,runtime_root=tmp_path/'runtime',vault=None,ocr=object())
    return store,item,service


def args(store,item,*,selection=None,request='request-1'):
    pending=store.confirmation_view(item);group=pending['groups'][0]
    return dict(token=pending['token'],request_id=request,group_id=group['group_id'],group_revision=group['group_revision'],
                selected_member_uids=selection or group['member_uids'])


def test_eight_once_one_fact_one_ledger_and_replay(setup):
    store,item,service=setup;request=args(store,item)
    assert service.resolve_group(item,'candidate','识神',**request).state=='queued'
    assert service.resolve_group(item,'candidate','识神',**request).state=='queued'
    row=store.item_bundle(item)
    assert row['snapshot'].count('识神')==8 and row['source_fact_id'] is not None
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM source_facts').fetchone()[0]==1
        saved=db.execute('SELECT * FROM group_decisions').fetchall()
        assert len(saved)==1 and len(json.loads(saved[0]['audit_json']))==8
    assert not store.manual_cards()


def test_partial_selection_same_fifo_and_remaining_version_changes(setup):
    store,item,service=setup;pending=store.confirmation_view(item)
    before=store.manual_cards()[0];request=args(store,item,selection=[pending['concerns'][1]['concern_uid']])
    assert service.resolve_group(item,'manual','更长的确认字词',**request).state=='waiting_user'
    remaining=store.confirmation_view(item)
    assert len(remaining['concerns'])==7
    assert store.manual_cards()[0]['enqueue_seq']==before['enqueue_seq']
    assert remaining['groups'][0]['group_revision']!=request['group_revision']
    assert service.resolve_group(item,'manual','更长的确认字词',**request).state=='waiting_user'
    assert len(store.confirmation_view(item)['concerns'])==7


def test_unselected_changed_choice_causes_zero_partial_writes(setup):
    store,item,service=setup;pending=store.confirmation_view(item)
    request=args(store,item,selection=[pending['concerns'][0]['concern_uid']])
    pending['concerns'][1]['candidates'].append('新候选')
    store.update_confirmation_suggestions(item,store.item_bundle(item)['confirmation_json'],pending)
    request['token']=store.confirmation_view(item)['token']
    with pytest.raises(ConfirmationConflict,match='group_revision_conflict'):
        service.resolve_group(item,'candidate','识神',**request)
    assert store.confirmation_view(item)['snapshot'].count('识神')==0
    with connect(store.path) as db:assert db.execute('SELECT count(*) FROM group_decisions').fetchone()[0]==0


def test_expired_token_not_replaced_by_group_revision(setup):
    store,item,service=setup;request=args(store,item);request['token']='wrong'
    with pytest.raises(ConfirmationConflict,match='group_token_stale'):
        service.resolve_group(item,'candidate','识神',**request)


def test_unable_group_path_never_runs_partial_fact_escape(setup,monkeypatch):
    store,item,service=setup
    monkeypatch.setattr(service,'_finish_partial_source',lambda *args:pytest.fail('group bypass'))
    assert service.resolve_group(item,'unable',**args(store,item)).state=='waiting_user'
    pending=store.confirmation_view(item)
    assert len(pending['deferred_concerns'])==8 and pending['review_required']
    assert store.item_bundle(item)['source_fact_id'] is None


def test_transaction_interruption_rolls_back_all_members_and_ledger(setup):
    store,item,service=setup;before=store.item_bundle(item)['confirmation_json']
    with connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_group BEFORE INSERT ON group_decisions BEGIN SELECT RAISE(ABORT,'injected group interruption'); END")
    with pytest.raises(Exception,match='injected group interruption'):
        service.resolve_group(item,'candidate','识神',**args(store,item))
    assert store.item_bundle(item)['confirmation_json']==before
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM source_facts').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM group_decisions').fetchone()[0]==0
    assert len(store.manual_cards())==1


def test_same_request_different_value_rejected_after_success(setup):
    store,item,service=setup;request=args(store,item)
    service.resolve_group(item,'candidate','识神',**request)
    with pytest.raises(ConfirmationConflict,match='payload_conflict'):
        service.resolve_group(item,'manual','另一个值',**request)


def test_unrelated_cas_rebases_once_using_original_member_identity(setup,monkeypatch):
    store,item,service=setup;request=args(store,item)
    actual=store.resolve_confirmation;calls=[]
    def race(item_id,expected_json,**kwargs):
        calls.append(True)
        if len(calls)==1:
            other=store.confirmation_view(item)
            other['snapshot']='前。'+other['snapshot']
            for c in other['concerns']:c['start']+=2;c['end']+=2
            store.update_confirmation_suggestions(item,expected_json,other)
        return actual(item_id,expected_json,**kwargs)
    monkeypatch.setattr(store,'resolve_confirmation',race)
    assert service.resolve_group(item,'candidate','识神',**request).state=='queued'
    assert len(calls)==2
    assert store.item_bundle(item)['snapshot'].startswith('前。这里的识神')
    with connect(store.path) as db:
        audit=json.loads(db.execute('SELECT audit_json FROM group_decisions').fetchone()[0])
        assert audit[0]['submitted_span'][0]==audit[0]['original_span'][0]+2


def test_four_unrelated_conflicts_are_bounded(setup,monkeypatch):
    store,item,service=setup;request=args(store,item);calls=[]
    def busy(*args,**kwargs):
        calls.append(True);raise ConfirmationConflict('synthetic busy')
    monkeypatch.setattr(store,'resolve_confirmation',busy)
    with pytest.raises(ConfirmationConflict,match='group_save_busy'):
        service.resolve_group(item,'candidate','识神',**request)
    assert len(calls)==4 and store.item_bundle(item)['source_fact_id'] is None


def test_simultaneous_same_group_only_one_result(setup):
    from concurrent.futures import ThreadPoolExecutor
    store,item,service=setup;request=args(store,item)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.resolve_group(item,'candidate','识神',**request),range(2)))
    assert [r.state for r in results]==['queued','queued']
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM group_decisions').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM source_facts').fetchone()[0]==1


def audio_files(service,item,pending):
    import wave
    root=service.runtime_root/'items'/str(item)/'confirmation';root.mkdir(parents=True)
    for c in pending['concerns']:
        with wave.open(str(root/c['audio_name']),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b'\0\0'*16000)


def test_member_recognition_reference_preserves_others_and_moves_only_selected_to_tail(setup):
    from knowledge_distiller.primary import PrimaryRecognition,PrimaryRecovery
    store,item,service=setup;pending=store.confirmation_view(item);uid=pending['concerns'][0]['concern_uid']
    audio_files(service,item,pending);calls=[]
    class Recognizer:
        def recognize(self,audio):
            calls.append(audio.path)
            assert audio.path.is_file() and audio.duration_seconds==1
            return PrimaryRecognition.succeeded(PrimaryRecovery('局部上下文识神参考。','zh',()))
    service.recognizer=Recognizer();request=args(store,item,selection=[uid])
    seq=store.manual_cards()[0]['enqueue_seq']
    assert service.rerecognize_group(item,**request).state=='waiting_user'
    result=store.confirmation_view(item)
    assert result['snapshot']==pending['snapshot']
    assert result['concerns'][0]['recognition_reference']['text']=='局部上下文识神参考。'
    assert 'recognition_reference' not in result['concerns'][1]
    assert result['concerns'][0]['decision_revision']!=pending['concerns'][0]['decision_revision']
    assert result['concerns'][1]['decision_revision']==pending['concerns'][1]['decision_revision']
    assert [len(g['member_uids']) for g in result['groups']]==[7,1]
    assert store.manual_cards()[0]['enqueue_seq']==seq and store.manual_cards()[1]['enqueue_seq']>seq
    assert service.rerecognize_group(item,**request).state=='waiting_user'
    assert len(calls)==1 and not calls[0].exists()
    assert service.confirmation_audio(item,pending['concerns'][0]['audio_name']).exists()


def test_member_recognition_failure_preserves_original_queue_and_pending(setup):
    from types import SimpleNamespace
    from knowledge_distiller.primary import PrimaryRecognition,PrimaryRecovery
    store,item,service=setup;pending=store.confirmation_view(item);audio_files(service,item,pending)
    before=store.item_bundle(item)['confirmation_json'];cards=store.manual_cards();calls=[]
    class Recognizer:
        def recognize(self,audio):
            calls.append(True)
            return (PrimaryRecognition.succeeded(PrimaryRecovery('成功参考。','zh',())) if len(calls)==1
                    else SimpleNamespace(failure='synthetic_failure',recovery=None))
    service.recognizer=Recognizer()
    with pytest.raises(ValueError,match='member_recognition_unavailable'):
        service.rerecognize_group(item,**args(store,item,selection=[c['concern_uid'] for c in pending['concerns'][:2]]))
    assert store.item_bundle(item)['confirmation_json']==before
    assert store.manual_cards()==cards
    with connect(store.path) as db:assert db.execute('SELECT count(*) FROM group_decisions').fetchone()[0]==0


def test_real_pipeline_publication_then_group_resolve_has_eight_lineages(tmp_path):
    from .test_pipeline import distiller
    from knowledge_distiller.primary import PrimaryRecognition,PrimaryRecovery
    from knowledge_distiller.faithful_review import ReviewConcern
    raw=sample(8,'无关背景。'*50)
    concerns=tuple(ReviewConcern(c['start'],c['end'],c['text'],c['reason'],True,('识神',)) for c in raw['concerns'])
    service,store,_,_,_=distiller(tmp_path,concerns=concerns)
    class Recognizer:
        def recognize(self,audio):return PrimaryRecognition.succeeded(PrimaryRecovery(raw['snapshot'],'zh',()))
    service.recognizer=Recognizer()
    item=store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state=='waiting_user'
    pending=store.confirmation_view(item)
    assert len(pending['concerns'])==8 and len(pending['groups'])==1
    assert len(store.manual_cards())==1
    service.resolve_group(item,'candidate','识神',**args(store,item))
    assert store.item_bundle(item)['snapshot'].count('识神')==8
    assert len(json.loads(store.item_bundle(item)['uncertainties_json']))==8


def test_group_deferred_cannot_bypass_via_old_finish_or_worker_and_can_resume(setup):
    store,item,service=setup;original=store.confirmation_view(item)
    seq=store.manual_cards()[0]['enqueue_seq']
    service.resolve_group(item,'unable',**args(store,item))
    pending=store.confirmation_view(item);snapshot=pending['snapshot']
    with pytest.raises(ValueError,match='group_unresolved_members_require_review'):
        service.finish_transcript(item,token=pending['token'])
    assert service.run(item).state=='waiting_user'
    assert store.item_bundle(item)['source_fact_id'] is None
    service.restore_group_deferred(item,token=pending['token'])
    restored=store.confirmation_view(item)
    assert len(restored['concerns'])==8 and not restored['deferred_concerns']
    assert restored['snapshot']==snapshot and restored['review_round_id']==original['review_round_id']
    assert store.manual_cards()[0]['enqueue_seq']==seq
    # Explicitly retaining a recovered member restores its original ASR text,
    # never promotes the unknown marker into a resolved fact.
    uid=restored['concerns'][0]['concern_uid']
    service.resolve_group(item,'keep',**args(store,item,selection=[uid],request='keep-restored'))
    assert store.confirmation_view(item)['snapshot'].count('[听辨不清]')==7


def test_first_card_entry_syncs_group_audit_and_replay(setup):
    from knowledge_distiller.v1.confirmation_revision import revision
    store,item,service=setup
    pending=store.confirmation_view(item);member=pending['concerns'][0]
    request=dict(token=pending['token'],concern_id=member['audio_name'],
                 concern_revision=revision(pending,member))
    assert service.resolve(item,'candidate','识神',**request).state=='queued'
    assert service.resolve(item,'candidate','识神',**request).state=='queued'
    after=store.confirmation_view(item)
    assert store.item_bundle(item)['snapshot'].count('识神')==8
    with connect(store.path) as db:
        rows=db.execute('SELECT audit_json FROM group_decisions').fetchall()
        assert len(rows)==1 and len(json.loads(rows[0][0]))==8


def test_legacy_unable_on_new_group_cannot_escape_through_finish(setup):
    store,item,service=setup
    pending=store.confirmation_view(item)
    service.resolve(item,'unable',token=pending['token'],concern_id=pending['concerns'][0]['audio_name'])
    pending=store.confirmation_view(item)
    with pytest.raises(ValueError,match='group_unresolved_members_require_review'):
        service.finish_transcript(item,token=pending['token'])
    assert store.item_bundle(item)['source_fact_id'] is None
    assert len(pending['deferred_concerns'])==8


@pytest.mark.parametrize('action,value', [('candidate','识神'),('manual','自填'),('keep',''),('unable','')])
def test_first_visible_card_submits_all_eight_members(setup, action, value):
    """2026-09-15 user correction: one ordinary card, one decision for all."""
    from html.parser import HTMLParser
    from werkzeug.datastructures import MultiDict
    from knowledge_distiller.v1.web import create_app
    class Forms(HTMLParser):
        def __init__(self):
            super().__init__(); self.forms=[]; self.current=None; self.audio=0
        def handle_starttag(self, tag, attrs):
            attrs=dict(attrs)
            if tag=='audio': self.audio+=1
            if tag=='form':
                self.current={'action':attrs.get('action',''),'fields':[]};self.forms.append(self.current)
            if tag=='input' and self.current is not None and attrs.get('type')=='hidden':
                self.current['fields'].append((attrs['name'],attrs.get('value','')))
        def handle_endtag(self, tag):
            if tag=='form': self.current=None
    store,item,service=setup
    client=create_app(store,service).test_client()
    html=client.get('/').get_data(as_text=True);parsed=Forms();parsed.feed(html)
    assert html.count('data-confirmation-card=')==1 and parsed.audio==1
    assert '同类疑点共' not in html and 'type="checkbox"' not in html
    form=next(f for f in parsed.forms if f['action']==f'/items/{item}/confirm')
    payload=MultiDict(form['fields']);assert not payload.getlist('selected_member_uids')
    payload['action']='candidate' if action=='keep' else action
    payload['value']='食神' if action=='keep' else value
    assert client.post(form['action'],data=payload).status_code==302
    assert client.post(form['action'],data=payload).status_code==302
    with connect(store.path) as db:
        rows=db.execute('SELECT audit_json FROM group_decisions').fetchall()
        assert len(rows)==1 and len(json.loads(rows[0][0]))==8
    if action in {'candidate','manual'}:
        assert store.item_bundle(item)['snapshot'].count(value)==8
    elif action=='keep':assert store.item_bundle(item)['snapshot'].count('食神')==8
    else:
        assert len(store.confirmation_view(item)['deferred_concerns'])==8
        assert store.item_bundle(item)['source_fact_id'] is None


def test_first_card_rejects_stale_other_member_change(setup):
    from knowledge_distiller.v1.confirmation_revision import revision
    store,item,service=setup;pending=store.confirmation_view(item);member=pending['concerns'][0]
    prior_revision=revision(pending,member)
    pending['concerns'][-1]['candidates'].append('变化后的候选')
    store.update_confirmation_suggestions(item,store.item_bundle(item)['confirmation_json'],pending)
    with pytest.raises(ValueError,match='另一端更新'):
        service.resolve(item,'candidate','识神',token=pending['token'],concern_id=member['audio_name'],concern_revision=prior_revision)
    assert store.confirmation_view(item)['snapshot'].count('识神')==0
