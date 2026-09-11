import json
from types import SimpleNamespace
import pytest
from knowledge_distiller.primary import StandardAudio, PrimaryRecovery
from knowledge_distiller.v1.primary_cache import _identity
from knowledge_distiller.v1.reviewer import build_reviewer


def test_real_qwen_wrapper_identity_tracks_selection_and_component_revision(tmp_path, monkeypatch):
    from knowledge_distiller.v1.app import ConfiguredQwenRecognizer
    from knowledge_distiller.v1 import qwen_component as component
    audio=StandardAudio(tmp_path/'audio.wav',1);audio.path.write_bytes(b'fake')
    settings=SimpleNamespace(qwen_component=object())
    active=ConfiguredQwenRecognizer(component.QWEN_MODEL_ID,settings)
    disabled=ConfiguredQwenRecognizer(None,settings)
    original=_identity(active,audio)
    assert original != _identity(disabled,audio)
    monkeypatch.setattr(component,'QWEN_MODEL_REVISION','test-new-revision')
    assert original != _identity(active,audio)
    revised=_identity(active,audio)
    monkeypatch.setattr(component,'QWEN_RUNTIME_VERSION','test-new-runtime')
    assert revised != _identity(active,audio)


def test_doubao_identity_tracks_resource_and_request_options_without_secrets(tmp_path,monkeypatch):
    from knowledge_distiller.v1 import doubao_asr as module
    from knowledge_distiller import secondary
    audio=StandardAudio(tmp_path/'audio.wav',1);audio.path.write_bytes(b'fake')
    def forbidden():pytest.fail('must never read credentials')
    recognizer=module.DoubaoRecognizer(forbidden,forbidden,forbidden,'region','bucket')
    before=_identity(recognizer,audio)
    monkeypatch.setattr(secondary,'SEED_RESOURCE_ID','different-test-resource')
    assert before != _identity(recognizer,audio)
    before=_identity(recognizer,audio)
    monkeypatch.setitem(module.SEED_REQUEST_OPTIONS,'enable_auto_lang',False)
    assert before != _identity(recognizer,audio)


@pytest.mark.parametrize('prefix',['', '普通内容。'*520], ids=['short', 'segmented'])
def test_repair_offsets_preserve_full_original_leading_whitespace(tmp_path,prefix):
    original=' \n  '+prefix+'今天在公圆散步。'
    class Client:
        def complete(self, **kwargs):
            text=json.loads(kwargs['user'].split('\n',1)[1]);repairs=[]
            if '公圆' in text:
                repairs=[{'original_text':'公圆','source_occurrence':0,'replacement':'公园','occurrence':0,
                          'reason':'散步地点支持同音字修复','evidence':'公圆散步','meaning_may_change':False}]
            return json.dumps({'candidate_text':text.replace('公圆','公园'),'issues':[],'repairs':repairs})
    reviewer=build_reviewer(Client())
    for _ in range(2):
        result=reviewer.review_in_directory(PrimaryRecovery(original,'zh',()),tmp_path)
        repair,=result.candidate.repairs
        assert original[repair['source_start']:repair['source_end']]=='公圆'
        assert result.candidate.text[repair['start']:repair['end']]=='公园'
