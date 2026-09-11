import json
from dataclasses import dataclass
from knowledge_distiller.primary import PrimaryRecovery, PrimaryChunk, StandardAudio, PrimaryRecognition
from knowledge_distiller.v1.reviewer import build_reviewer
from knowledge_distiller.v1.llm import LLMRequestError


def test_long_review_bounds_and_resumes_middle_failure(tmp_path):
    calls=[]
    @dataclass
    class Client:
        model: str = 'fake-model'
        failed: bool = False
        def complete(self, **kwargs):
            text=json.loads(kwargs['user'].split('\n',1)[1])
            calls.append(text)
            assert len(text) <= 2400
            if len(calls)==2 and not self.failed:
                self.failed=True
                raise LLMRequestError('llm_request_timeout')
            return json.dumps({'candidate_text':text,'issues':[]})
    source=PrimaryRecovery(''.join(f'第{i}段，这是测试的原始讲话，保留全部关键事实。' for i in range(400)), 'zh', ())
    reviewer=build_reviewer(Client())
    first=reviewer.review_in_directory(source,tmp_path)
    assert str(first.failure)=='request_timeout'
    result=reviewer.review_in_directory(source,tmp_path)
    assert result.candidate is not None
    assert result.candidate.text.replace('\n','')==source.text
    assert calls.count(calls[0])==1


def test_successful_primary_cache_survives_new_recognizer(tmp_path):
    from knowledge_distiller.v1.primary_cache import recognize_cached
    audio=StandardAudio(tmp_path/'audio.wav',10)
    audio.path.write_bytes(b'fake-audio')
    calls=[]
    class Recognizer:
        def recognize(self,audio):
            calls.append(True)
            return PrimaryRecognition.succeeded(PrimaryRecovery('完整内容','zh',(PrimaryChunk('完整内容',0,10),)))
    assert recognize_cached(Recognizer(),audio,tmp_path).recovery
    assert recognize_cached(Recognizer(),audio,tmp_path).recovery
    assert len(calls)==1
    audio.path.write_bytes(b'new-fake-audio')
    assert recognize_cached(Recognizer(),audio,tmp_path).recovery
    assert len(calls)==2


def test_repair_requires_source_evidence_and_is_retained():
    from knowledge_distiller.faithful_review import FaithfulReviewAdapter, ReviewRuntimeResult
    primary='今天在公圆散步，明天继续。'
    payload={'candidate_text':'今天在公园散步，明天继续。','issues':[], 'repairs':[
        {'original_text':'公圆','source_occurrence':0,'replacement':'公园','occurrence':0,
         'reason':'散步地点语境支持同音字修复','evidence':'在公圆散步','meaning_may_change':False}]}
    class Binding:
        def complete(self,text):return ReviewRuntimeResult(json.dumps(payload),'end_turn')
    result=FaithfulReviewAdapter(Binding()).review(PrimaryRecovery(primary,'zh',()))
    assert result.candidate.repairs[0]['original_text']=='公圆'
    payload['repairs'][0]['evidence']='不存在的依据'
    assert FaithfulReviewAdapter(Binding()).review(PrimaryRecovery(primary,'zh',())).failure


def test_content_relevance_never_becomes_transcription_blocker():
    from knowledge_distiller.faithful_review import FaithfulReviewAdapter, ReviewRuntimeResult
    payload={'candidate_text':'订阅我的频道。','issues':[{'issue_text':'订阅','occurrence':0,
        'reason':'是否属于正文','meaning_may_change':True,'kind':'content_relevance'}]}
    class Binding:
        def complete(self,text):return ReviewRuntimeResult(json.dumps(payload),'end_turn')
    result=FaithfulReviewAdapter(Binding()).review(PrimaryRecovery(payload['candidate_text'],'zh',()))
    assert result.candidate.concerns==()


def test_long_review_rejects_missing_middle_instead_of_publishing(tmp_path):
    calls=[]
    class Client:
        def complete(self, **kwargs):
            text=json.loads(kwargs['user'].split('\n',1)[1]); calls.append(text)
            return json.dumps({'candidate_text':text if len(calls)!=2 else '', 'issues':[]})
    text=''.join(f'第{i}段，具体事实必须完整保留。' for i in range(450))
    result=build_reviewer(Client()).review_in_directory(PrimaryRecovery(text,'zh',()),tmp_path)
    assert result.failure and result.candidate is None
    assert len(calls)==2


def test_clipper_reuses_one_full_alignment_for_all_concerns(tmp_path, monkeypatch):
    import knowledge_distiller.v1.confirmation as module
    from knowledge_distiller.faithful_review import ReviewConcern
    from tests.v1.test_confirmation import Runner
    audio=StandardAudio(tmp_path/'audio.wav',30)
    audio.path.write_bytes(b'fake-audio')
    text='第一处文字。第二处文字。第三处文字。'
    recovery=PrimaryRecovery(text,'zh',(PrimaryChunk(text,0,30),))
    actual=module.alignment_blocks;calls=[]
    def counted(*a, **k):calls.append(True);return actual(*a,**k)
    monkeypatch.setattr(module,'alignment_blocks',counted)
    clipper=module.FFmpegConfirmationClipper(Runner())
    for i, phrase in enumerate(('第一处','第二处','第三处')):
        start=text.index(phrase)
        clipper.clip(audio,recovery,text,ReviewConcern(start,start+len(phrase),phrase,'关键主体',True),tmp_path/f'{i}.wav')
    assert len(calls)==1


def test_pipeline_review_retry_keeps_primary_and_source_lineage(tmp_path):
    from tests.v1.test_pipeline import distiller, FailsOnceReviewer
    service,store,source,_,_=distiller(tmp_path,reviewer=FailsOnceReviewer())
    calls=[]
    class Recognizer:
        def recognize(self,audio):
            calls.append(True)
            text='持续切换会带来额外损耗。'
            return PrimaryRecognition.succeeded(PrimaryRecovery(text,'zh',(PrimaryChunk(text,0,10),)))
    service.recognizer=Recognizer()
    item=store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state=='failed'
    store.retry_item(item)
    assert service.run(item).state=='succeeded'
    assert len(calls)==1
    lineage=json.loads(store.item_bundle(item)['lineage_json'])
    assert lineage['primary_asr']['text']=='持续切换会带来额外损耗。'


def test_corrupt_primary_checkpoint_is_recomputed(tmp_path):
    from knowledge_distiller.v1.primary_cache import recognize_cached
    audio=StandardAudio(tmp_path/'audio.wav',10);audio.path.write_bytes(b'fake')
    calls=[]
    class Recognizer:
        def recognize(self,audio):
            calls.append(True)
            return PrimaryRecognition.succeeded(PrimaryRecovery('来源','zh',(PrimaryChunk('来源',0,10),)))
    recognize_cached(Recognizer(),audio,tmp_path)
    path=tmp_path/'primary-recovery.json';payload=json.loads(path.read_text())
    payload['recovery']['text']='篡改';path.write_text(json.dumps(payload))
    assert recognize_cached(Recognizer(),audio,tmp_path).recovery.text=='来源'
    assert len(calls)==2


def test_review_checkpoint_write_failure_is_visible(tmp_path, monkeypatch):
    import knowledge_distiller.v1.reviewer as module
    class Client:
        def complete(self, **kwargs):return json.dumps({'candidate_text':'完整来源。','issues':[]})
    def fail(*a):raise OSError('test-write-failure')
    monkeypatch.setattr(module.os,'replace',fail)
    result=build_reviewer(Client()).review_in_directory(PrimaryRecovery('完整来源。','zh',()),tmp_path)
    assert str(result.failure)=='checkpoint_unavailable'
    assert not (tmp_path/'review-response.tmp').exists()


def test_pipeline_preserves_ai_repair_and_original_source(tmp_path):
    from tests.v1.test_pipeline import distiller
    primary='持续切换会带来额外损号。'
    candidate='持续切换会带来额外损耗。'
    class Recognizer:
        def recognize(self,audio):return PrimaryRecognition.succeeded(PrimaryRecovery(primary,'zh',(PrimaryChunk(primary,0,10),)))
    class Client:
        def complete(self, **kwargs):return json.dumps({'candidate_text':candidate,'issues':[], 'repairs':[
            {'original_text':'损号','source_occurrence':0,'replacement':'损耗','occurrence':0,
             'reason':'上下文指资源损耗','evidence':'额外损号','meaning_may_change':False}]})
    service,store,_,_,_=distiller(tmp_path,reviewer=build_reviewer(Client()))
    service.recognizer=Recognizer()
    item=store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state=='succeeded'
    row=store.item_bundle(item)
    assert row['snapshot']==candidate
    lineage=json.loads(row['lineage_json'])
    assert lineage['primary_asr']['text']==primary
    assert lineage['ai_repairs'][0]['original_text']=='损号'
    assert json.loads(row['uncertainties_json'])[0]['status']=='repaired'
