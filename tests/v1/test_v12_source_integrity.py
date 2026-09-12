import json
import wave
from dataclasses import replace
from types import SimpleNamespace
import pytest

from knowledge_distiller.review_validation import validate_response
from knowledge_distiller.primary import PrimaryRecovery, PrimaryRecognition, PrimaryFailure, PrimaryChunk, StandardAudio


def proposal(text, repairs=(), issues=()):
    return json.dumps({'candidate_text':text,'repairs':list(repairs),'issues':list(issues)})


def repair(original, replacement, evidence, **kw):
    return dict(original_text=original,replacement=replacement,evidence=evidence,reason='source spelling',
                source_occurrence=0,occurrence=0,meaning_may_change=False,**kw)


@pytest.mark.parametrize('source,candidate',[
    ('Do not take 15 units at -5 degrees.','Do take 15 units at -5 degrees.'),
    ('Take 15 units.','Take 50 units.'),('Set -5 degrees.','Set +5 degrees.'),
    ('How we doing today?','How are we doing today?'),('a != b','a = b'),
])
def test_undeclared_edits_never_enter_source(source,candidate):
    result=validate_response(source,proposal(candidate))
    assert result.text==source and not result.repairs
    assert result.diagnostics[0]['code']=='unaccepted_candidate_changes'


@pytest.mark.parametrize('basis', ['How we doing', 'other unrelated sentence', 'How...today'])
def test_grammar_addition_is_rejected_even_with_declared_repair(basis):
    source='How we doing today? other unrelated sentence'
    result=validate_response(source,proposal(source.replace('How we','How are we'),[
        repair('How we','How are we',basis)]))
    assert result.text==source and not result.repairs and result.diagnostics


def test_mixed_operations_keep_good_edit_and_reject_bad_evidence():
    source='transcripton transcription. other typo stays.'
    rows=[repair('transcripton','transcription','transcripton transcription.'),
          repair('typo','type','not in the source')]
    proposed=source.replace('transcripton','transcription').replace('typo','type')
    result=validate_response(source,proposal(proposed,rows))
    assert result.text=='transcription transcription. other typo stays.'
    assert len(result.repairs)==1
    assert result.repairs[0]['source_sha256']
    assert any(d['operation']==1 for d in result.diagnostics)


def test_duplicate_and_overlapping_edits_both_revert():
    source='transcripton transcription.'
    row=repair('transcripton','transcription',source)
    result=validate_response(source,proposal(source.replace('transcripton','transcription'),[row,row]))
    assert result.text==source and not result.repairs
    assert sum(d['code']=='overlap' for d in result.diagnostics)==2


def test_evidence_quote_normalization_does_not_erase_signs():
    from knowledge_distiller.review_validation import evidence_present
    assert evidence_present('He said “yes”.','He said "yes".')
    assert not evidence_present('temperature -5','temperature 5')
    assert not evidence_present('one two three four','one...four')


def test_bad_json_returns_exact_baseline_and_replay_diagnostic():
    result=validate_response('unchanged original','{broken')
    assert result.text=='unchanged original' and not result.repairs
    assert result.diagnostics[0]['field']=='response'


def test_critical_issue_survives_invalid_proposal_mapping():
    result=validate_response('take 15 units',proposal('take 50 units', issues=[{
        'issue_text':'50','occurrence':0,'reason':'number unclear','meaning_may_change':True}]))
    assert result.text=='take 15 units' and result.concerns[0].meaning_may_change


def test_retry_is_bounded_and_cache_keeps_raw_baseline(tmp_path):
    from knowledge_distiller.v1.reviewer import build_reviewer
    calls=[]
    class Client:
        def complete(self,**kw):
            calls.append(kw)
            return '{broken'
    reviewer=build_reviewer(Client())
    recovery=PrimaryRecovery('baseline','en',())
    for _ in range(2):
        result=reviewer.review_in_directory(recovery,tmp_path)
        assert result.candidate.text=='baseline'
    assert len(calls)==2
    assert 'invalid_json_or_shape' in calls[1]['system']
    assert json.loads((tmp_path/'review-response.json').read_text())['primary_text']=='baseline'
    assert (tmp_path/'review-response.validation.json').exists()


def test_complete_text_without_timeline_is_cached(tmp_path):
    from knowledge_distiller.v1.primary_cache import recognize_cached
    audio=StandardAudio(tmp_path/'audio.wav',10);audio.path.write_bytes(b'audio')
    calls=[]
    class Recognizer:
        def recognize(self,audio):
            calls.append(1);return PrimaryRecognition.succeeded(PrimaryRecovery('original','en',()))
    for _ in range(2):
        assert recognize_cached(Recognizer(),audio,tmp_path).recovery.text=='original'
    assert len(calls)==1


def test_long_audio_resumes_only_failed_segment(tmp_path):
    from knowledge_distiller.v1.primary_cache import recognize_segmented
    path=tmp_path/'input.wav'
    with wave.open(str(path),'wb') as f:
        f.setnchannels(1); f.setsampwidth(2);f.setframerate(100)
        f.writeframes(b'\0\0'*700)
    audio=StandardAudio(path,7,100)
    calls=[]
    class Recognizer:
        def recognize(self,piece):
            index=int(piece.path.parent.name);calls.append(index)
            if index==1 and calls.count(1)==1:
                return PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
            text=f'part{index}'
            return PrimaryRecognition.succeeded(PrimaryRecovery(text,'en',(PrimaryChunk(text,0,piece.duration_seconds),)))
    first=recognize_segmented(Recognizer(),audio,tmp_path,segment_seconds=3)
    assert first.failure==PrimaryFailure.INCOMPLETE
    assert json.loads((tmp_path/'asr-segments/status.json').read_text())['failures'][0]['segment']==1
    second=recognize_segmented(Recognizer(),audio,tmp_path,segment_seconds=3)
    assert second.recovery.text=='part0\npart1\npart2'
    assert calls==[0,1,2,1]
    assert second.recovery.chunks[-1].start_seconds==6


def captured(track,**metadata):
    return SimpleNamespace(source_key='test-video',duration_seconds=10,
        metadata={'original_language':'en','captions':[{'source_key':'test-video','language':'en',
            'translated':False,'kind':'automatic',**track}],**metadata})


VTT='WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world\n\n00:00:05.000 --> 00:00:10.000\nHello world\n'


def test_subtitles_keep_nonoverlapping_repetition_and_provenance():
    from knowledge_distiller.v1.subtitle_baseline import select_subtitle
    result,lineage=select_subtitle(captured({'text':VTT}))
    assert result.text=='Hello world\nHello world'
    assert len(lineage['subtitle_baseline']['cue_map'])==2
    assert lineage['subtitle_baseline']['verification']=='structural_only_not_listened'


@pytest.mark.parametrize('track,metadata',[
    ({'language':'zh'},{}),({'translated':True},{}),({'source_key':'other'},{}),
    ({},{'original_language':None}),({'text':'WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello'},{}),
    ({'text':'WEBVTT\n\n00:00:00.000 --> 00:00:10.000\n'},{}),
])
def test_invalid_subtitle_is_explicit_fallback(track,metadata):
    from knowledge_distiller.v1.subtitle_baseline import select_subtitle
    result,lineage=select_subtitle(captured({'text':VTT,**track},**metadata))
    assert result is None and lineage['caption_selection']


def test_ocr_invalid_operation_retains_other_decisions():
    from knowledge_distiller.v1.ocr_review_policy import review_ocr
    from knowledge_distiller.v1.domain import SourceFact
    fact=SourceFact('take 15\nother',(
        {'by':'ocr','status':'unresolved','start':0,'end':7,'text':'take 15','member_id':'img'},
        {'by':'ocr','status':'unresolved','start':8,'end':13,'text':'other','member_id':'img'}))
    answer={'decisions':[{'index':0,'reliable':True,'affects_core':False,'replacement':'take 50','reason':'guess','evidence':'take 15'},
        {'index':1,'reliable':False,'affects_core':False,'replacement':'other','reason':'noncore','evidence':''}]}
    result,trace=review_ocr(fact,{'image_ocr':[]},SimpleNamespace(complete=lambda **kw:json.dumps(answer)))
    assert result.snapshot==fact.snapshot
    assert [u['status'] for u in result.uncertainties]==['unresolved','advisory']
    assert trace['ocr_review_diagnostics'][0]['operation']==0


def test_waiting_view_matches_scheduler_after_retry_and_timestamp_tie(tmp_path):
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.web import _home_context
    from knowledge_distiller.v1.database import connect
    store=Store(tmp_path/'isolated.sqlite3'); store.initialize()
    ids=[store.create_item(f'https://v.douyin.com/{i}/') for i in range(3)]
    with connect(store.path) as db:
        for item,stamp in zip(ids, ['2026-09-01T00:00:02+00:00','2026-09-01T00:00:01+00:00','2026-09-01T00:00:01+00:00']):
            db.execute('UPDATE distill_items SET queued_at=?,updated_at=? WHERE item_id=?',(stamp,stamp,item))
    waiting=_home_context(store, selected=None)['waiting']
    assert [r['id'] for r in waiting]==[ids[1],ids[2],ids[0]]
    assert store.claim_next_item()==waiting[0]['id']
    store.mark_failed(ids[1],'reviewing','test_failure'); store.retry_item(ids[1])
    assert [r['id'] for r in _home_context(store, selected=None)['waiting']]==[ids[2],ids[0],ids[1]]


def test_failed_audio_clip_retains_question_and_full_audio(tmp_path):
    from tests.v1.test_pipeline import distiller
    from knowledge_distiller.faithful_review import ReviewConcern
    from knowledge_distiller.v1.confirmation import ConfirmationAudioError
    class Clipper:
        def clip(self,*args): raise ConfirmationAudioError('test')
    service,store,*_=distiller(tmp_path,concerns=(ReviewConcern(0,4,'持续切换','unclear',True),))
    service.confirmation_clipper=Clipper()
    item=store.create_item('https://v.douyin.com/test/')
    result=service.run(item)
    assert result.state=='waiting_user'
    pending=json.loads(store.item_bundle(item)['confirmation_json'])
    assert pending['concerns'] and pending['lineage']['primary_asr']
    audio=service.runtime_root/'items'/str(item)/'audio'/'standard.wav'
    audio.parent.mkdir(parents=True,exist_ok=True);audio.write_bytes(b'synthetic original audio')
    assert service.confirmation_audio(item).name=='standard.wav'


def test_subtitle_pipeline_does_not_require_full_asr(tmp_path):
    from tests.v1.test_pipeline import distiller
    from knowledge_distiller.v1.reviewer import build_reviewer
    class Client:
        def complete(self,**kw):
            text=json.loads(kw['user'].split('\n',1)[1])
            return proposal(text)
    service,store,source,*_=distiller(tmp_path,reviewer=build_reviewer(Client()))
    original=source.capture
    def capture(*args,**kwargs):
        value=original(*args,**kwargs)
        vtt=VTT.replace('00:00:10.000',f'00:00:{int(value.duration_seconds):02d}.000')
        return replace(value,metadata={**value.metadata,'original_language':'en','captions':[{
            'source_key':value.source_key,'language':'en','kind':'automatic','translated':False,'text':vtt}]})
    source.capture=capture
    service.recognizer=SimpleNamespace(recognize=lambda _:pytest.fail('subtitles must bypass full ASR'))
    item=store.create_item('https://v.douyin.com/test/')
    service.run(item)
    row=store.item_bundle(item)
    assert row['source_fact_id'] is not None
    lineage=json.loads(row['lineage_json'])
    assert 'primary_subtitle' in lineage and 'primary_asr' not in lineage


def test_final_confirmation_preserves_full_lineage(tmp_path):
    from tests.v1.test_transcript_correction import sample
    service,store,_,item,_=sample(tmp_path)
    pending=json.loads(store.item_bundle(item)['confirmation_json'])
    expected={**pending['lineage'],'source_test_marker':{'revision':'synthetic-v1'}}
    pending.update(concerns=[],lineage=expected,review_required=True)
    store.mark_waiting(item,pending)
    token=json.loads(store.item_bundle(item)['confirmation_json'])['token']
    service.finish_transcript(item,token=token)
    assert json.loads(store.item_bundle(item)['lineage_json'])==expected


def test_multiple_evidence_spans_checked_independently():
    source='transcripton here. Far away we say transcription.'
    spans=[{'start':0,'end':12,'text':source[:12]},
           {'start':source.index('transcription'),'end':len(source)-1,'text':'transcription'}]
    row=repair('transcripton','transcription','',evidence_spans=spans)
    result=validate_response(source,proposal(source.replace('transcripton','transcription'),[row]))
    assert len(result.repairs)==1
    spans[1]['start']-=1
    result=validate_response(source,proposal(source.replace('transcripton','transcription'),[row]))
    assert result.text==source and not result.repairs


def test_critical_repair_cannot_silently_become_advisory():
    source='take 15 units'
    row=repair('15','50',source);row['meaning_may_change']=True
    result=validate_response(source,proposal('take 50 units',[row]))
    assert result.text==source and result.concerns[0].text=='15'
    assert result.concerns[0].meaning_may_change
