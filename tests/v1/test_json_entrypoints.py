"""Current model-body entrypoints preserve their existing consumer contracts."""
import json
from types import SimpleNamespace
import pytest
from knowledge_distiller.v1.ocr_review_policy import review_ocr
from knowledge_distiller.v1.domain import SourceFact
from knowledge_distiller.v1.ocr import OcrError
from knowledge_distiller.v1.collection_model import call_combined, validate_combined
from knowledge_distiller.v1.douyin_collections import CollectionError
from knowledge_distiller.v1.structured_calls import StructuredCalls
from knowledge_distiller.v1.llm import LLMRequestError
from .test_model_json import wrap

SHELLS = [lambda s: s, wrap, lambda s: wrap(s, '\r\n')]


def client(value, shell):
    return SimpleNamespace(model='synthetic', base_url='fixture://local', complete=lambda **kwargs: shell(json.dumps(value)))


@pytest.mark.parametrize('shell', SHELLS)
def test_ocr_body_and_positions(shell):
    fact = SourceFact('字形有疑问。', ({'by': 'ocr', 'status': 'unresolved', 'start': 0, 'end': 2,
                                  'text': '字形', 'member_id': 'image-1'},))
    answer = {'decisions': [{'index': 0, 'affects_core': False, 'reliable': False,
                'replacement': '字形', 'evidence': '', 'reason': '不影响其余来源主旨'}]}
    result, trace = review_ocr(fact, {}, client(answer, shell))
    assert result.snapshot == fact.snapshot
    assert result.uncertainties[0]['status'] == 'advisory'
    answer['decisions'][0]['index'] = 99
    with pytest.raises(OcrError):
        review_ocr(fact, {}, client(answer, shell))


@pytest.mark.parametrize('shell', SHELLS)
def test_collection_body_keeps_reference_validation(shell):
    basis = [{'native_id': 'fixture', 'knowledge_result_id': 1, 'source_fact_id': 1, 'source_url': 'fixture://local',
              'knowledge': {'core_points': [{'id': 'p1', 'evidence_ids': ['e1']}], 'other_points': [],
                            'evidence': [{'id': 'e1', 'text': '来源。'}]}}]
    value = {'qualified': True, 'title': '标题', 'subtitle': '范围', 'summary': '来源综述',
             'points': [{'id': 'c1', 'statement': '来源判断', 'argument': '来源依据',
                         'supports': [{'knowledge_result_id': 1, 'point_id': 'p1'}]}]}
    assert validate_combined(call_combined(client(value, shell), basis), basis)['points'][0]['supports'][0]['evidence'][0]['text'] == '来源。'
    value['points'][0]['supports'][0]['knowledge_result_id'] = 99
    with pytest.raises(CollectionError):
        validate_combined(call_combined(client(value, shell), basis), basis)


@pytest.mark.parametrize('shell', SHELLS)
def test_organization_body_keeps_schema(shell, tmp_path):
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
    assert StructuredCalls(client({'ok': True}, shell), tmp_path).complete('test', 'system', {}, schema, 32) == {'ok': True}
    with pytest.raises(LLMRequestError):
        StructuredCalls(client({'ok': True, 'extra': 1}, shell), tmp_path).complete('test', 'system', {}, schema, 32)


@pytest.mark.parametrize('shell', SHELLS)
def test_codex_connection_body_is_exact(shell):
    from knowledge_distiller.v1.settings import SettingsService, SettingsError
    writes = []
    service = SimpleNamespace(refresh_codex=lambda: [],
        codex_client=lambda *args, **kwargs: client({'connected': True}, shell),
        store=SimpleNamespace(set_settings=writes.append), sync_credential_labels=lambda: None)
    SettingsService.activate_codex(service, 'fixture', '')
    assert writes[-1]['llm_state'] == 'configured'
    service.codex_client = lambda *args, **kwargs: client({'connected': True, 'extra': 1}, shell)
    with pytest.raises(SettingsError, match='codex_connection_failed'):
        SettingsService.activate_codex(service, 'fixture', '')
