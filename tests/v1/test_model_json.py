"""SC-JSON-ENVELOPE: frozen BUG-20260914-04, synthetic model bodies."""
import json
import math
import pytest
from knowledge_distiller.v1.model_json import parse_model_json, ModelJSONError
from knowledge_distiller.review_validation import validate_response
from knowledge_distiller.v1.knowledge_model import parse_knowledge, KnowledgeModelError
from knowledge_distiller.v1.structured_calls import _response_value
from .test_knowledge_model import knowledge_payload


def wrap(raw, newline='\n'):
    return ' \t\n```json' + newline + raw + newline + '```\r\n '


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('value', [{}, [], True, 12, None, {'text': '```json\ncontent\n```'}, '```'])
def test_only_wrapper_changes(newline, value):
    raw = json.dumps(value)
    bare, wrapped = parse_model_json(raw), parse_model_json(wrap(raw, newline))
    assert bare.value == wrapped.value == value
    assert bare.envelope == 'bare'
    assert wrapped.envelope == 'json_fence'
    assert bare.raw_sha256 != wrapped.raw_sha256


@pytest.mark.parametrize('raw', [
    'explanation\n{}', 'explanation\n```json\n{}\n```',
    '```json\n{}\n```\nexplanation', '```\n{}\n```',
    '```JSON\n{}\n```', '```python\n{}\n```', '```json {}\n```',
    '```json\n{} ```', '```json\n{}', '```json\n{}\n``',
    '```json\n{}\n```\n```json\n{}\n```', '{}\n{}',
    '```json\n```json\n{}\n```\n```', '```json\n{"a":}\n```',
    '```json\r{}\r```', 'ordinary **Markdown**',
])
def test_reject_without_content_repair(raw):
    with pytest.raises(ModelJSONError):
        parse_model_json(raw)


def test_existing_json_numeric_and_duplicate_key_policy_is_unchanged():
    for raw in ['{"a":1,"a":2}', 'NaN', 'Infinity', '-Infinity', '1e999']:
        expected = json.loads(raw)
        result = parse_model_json(wrap(raw)).value
        assert result == expected or (isinstance(result, float) and math.isnan(result) and math.isnan(expected))


@pytest.mark.parametrize('shell', [lambda s: s, wrap, lambda s: wrap(s, '\r\n')])
def test_knowledge_schema_and_evidence_are_still_checked(shell):
    source = '持续切换会带来额外损耗。'
    fixture = json.loads(knowledge_payload())
    fixture["evidence"][0]["occurrence"] = 0  # This fixture source has one occurrence.
    original = json.dumps(fixture)
    assert parse_knowledge(source, shell(original)) == parse_knowledge(source, original)
    value = json.loads(original)
    value['evidence'][0]['text'] = '来源没有这句话'
    with pytest.raises(KnowledgeModelError, match='knowledge_structure_invalid'):
        parse_knowledge(source, shell(json.dumps(value)))
    del value['core_points']
    with pytest.raises(KnowledgeModelError):
        parse_knowledge(source, shell(json.dumps(value)))


@pytest.mark.parametrize('shell', [lambda s: s, wrap, lambda s: wrap(s, '\r\n')])
def test_source_schema_and_evidence_still_protect_original(shell):
    source = '今天不用付款。'
    raw = json.dumps({'candidate_text': source, 'issues': []})
    assert validate_response(source, shell(raw)) == validate_response(source, raw)
    bad = json.dumps({'candidate_text': '今天需要付款。', 'issues': [], 'repairs': []})
    assert validate_response(source, shell(bad)).text == source
    assert validate_response(source, shell('{}')).diagnostics[0]['code'] == 'invalid_json_or_shape'


def test_organization_schema_echo_is_not_a_global_policy():
    schema = {'type': 'object', 'properties': {'connected': {'type': 'boolean'}}, 'required': ['connected']}
    echoed = {**schema, 'connected': True}
    raw = wrap(json.dumps(echoed), '\r\n')
    assert _response_value(raw, schema) == {'connected': True}
    assert parse_model_json(raw).value == echoed
    # Knowledge still demands qualified/points/evidence, rather than lifting
    # arbitrary properties out of an echoed request schema.
    with pytest.raises(KnowledgeModelError):
        parse_knowledge('来源。', raw)
