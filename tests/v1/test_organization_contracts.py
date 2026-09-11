import json
from copy import deepcopy
from types import SimpleNamespace
import pytest
from jsonschema import Draft202012Validator, ValidationError
from knowledge_distiller.v1.organization_contracts import topic_contract, topic_plan, recall_contract, growth_contract, growth_plan
from knowledge_distiller.v1.structured_calls import StructuredCalls
from knowledge_distiller.v1.llm import LLMRequestError
from knowledge_distiller.organization_models import parse_growth_plan
from tests.fixtures.growth import insight_payload, relation_payload, source_participant, accepted_participant


def test_each_point_requires_explicit_assignment_and_program_builds_ids():
    points = [SimpleNamespace(knowledge_result_id=2, point_id=f'p{i}') for i in range(1,4)]
    schema = topic_contract(points, [])
    value = {'topics': [{'key':'pressure', 'topic_id':None, 'name':'压力作用的边界', 'scope':'压力与恢复的条件'}],
        'decisions': {'0':[{'topic_key':'pressure','position':1}], '1':[{'topic_key':'pressure','position':0}], '2':[]}}
    Draft202012Validator(schema).validate(value)
    result = topic_plan(value, points)
    assert result['unassigned_points'] == [{'knowledge_result_id':2,'point_id':'p3'}]
    assert result['topics'][0]['members'][0]['point_id'] == 'p2'
    del value['decisions']['2']
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(value)


def test_recall_rejects_input_codec_and_out_of_boundary_ids():
    schema = recall_contract({'eligible_history':[{'knowledge_result_id':1}], 'accepted_current':[], 'relation_current':[], 'reconsideration_hints':[]})
    value = {key: [] for key in schema['properties']}
    Draft202012Validator(schema).validate(value)
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate({**value, 'codec':'historical-recall-input-v1'})
    value['source_knowledge_ids'] = [999]
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(value)


def test_growth_reviews_cover_all_new_inputs_without_model_generated_ids():
    schema = growth_contract({'frozen_new_ids':[4,5]})
    value = {key: [] for key in schema['properties']}
    value['new_input_reviews'] = {'4':{'outcome':'considered_no_formal_result','reason_text':'无跨来源依据'}}
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(value)
    value['new_input_reviews']['5'] = value['new_input_reviews']['4']
    Draft202012Validator(schema).validate(value)
    assert [r['knowledge_result_id'] for r in growth_plan(value)['new_input_reviews']] == [4,5]


def test_diagnostics_and_only_accepted_exact_inputs_reuse(tmp_path):
    class Client:
        model='fixture'; base_url='fixture://local'
        calls=0
        def complete(self, **kwargs):
            self.calls += 1
            return '{"ok":true}'
    client=Client(); calls=StructuredCalls(client,tmp_path)
    schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    calls.complete('stage','system',{},schema,32)
    calls.complete('stage','system',{},schema,32)
    assert client.calls == 2  # Not accepted yet.
    calls.accept('stage')
    calls.complete('stage','system',{},schema,32)
    assert client.calls == 2
    calls.complete('stage','system',{'changed':True},schema,32)
    assert client.calls == 3
    assert (tmp_path/'stage.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('kind,field,payload_factory', [
    ('relation', 'new_relations', relation_payload),
    ('insight', 'candidate_versions', insight_payload),
])
def test_growth_participant_order_is_built_by_program(kind, field, payload_factory):
    schema = growth_contract({'frozen_new_ids': [4]})
    value = {key: [] for key in schema['properties']}
    value['new_input_reviews'] = {'4': {'outcome': 'participated', 'reason_text': 'Contributes a premise'}}
    payload = payload_factory()
    del payload['codec']
    if kind == 'insight':
        payload['scan_tags'] = ['适用边界', '前提条件', '来源比较']
    participants = [source_participant('a', 4, position=1), accepted_participant('b', 5, position=2)]
    for participant in participants:
        del participant['position']
    value[field] = [{f'new_{kind}_key': 'new', 'target_kind': 'create_identity',
        'payload': payload, 'participants': participants, 'used_relations': []}]
    Draft202012Validator(schema).validate(value)
    parsed = parse_growth_plan(growth_plan(value))
    assert [p.position for p in getattr(parsed, field)[0].participants] == [0, 1]
    assert [p.participant_key for p in getattr(parsed, field)[0].participants] == ['a', 'b']
    assert all('position' not in p for p in participants)
    # The model cannot reintroduce an alternative numbering convention.
    participants[0]['position'] = 1
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(value)


def test_growth_schema_change_invalidates_accepted_old_position_cache(tmp_path):
    schema = growth_contract({'frozen_new_ids': [4]})
    old_schema = deepcopy(schema)
    for variant in old_schema['$defs']['participant']['anyOf']:
        variant['properties']['position'] = {'type': 'integer'}
        variant['required'].append('position')
    value = {key: [] for key in schema['properties']}
    value['new_input_reviews'] = {'4': {'outcome': 'considered_no_formal_result', 'reason_text': 'No increment'}}
    class Client:
        model = 'fixture'
        base_url = 'fixture://local'
        calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            return json.dumps(value)
    client = Client()
    calls = StructuredCalls(client, tmp_path)
    calls.complete('growth', 'system', {}, old_schema, 32)
    calls.accept('growth')
    calls.complete('growth', 'system', {}, schema, 32)
    assert client.calls == 2
    calls.accept('growth')
    calls.complete('growth', 'system', {}, schema, 32)
    assert client.calls == 2


def test_only_exact_schema_echo_is_unwrapped():
    from knowledge_distiller.v1.structured_calls import _response_value
    schema={'type':'object','properties':{'ids':{'type':'array','items':{'type':'integer'}}},'required':['ids'],'additionalProperties':False}
    assert _response_value(json.dumps({**schema,'ids':[1]}),schema)=={'ids':[1]}
    tampered={**schema,'additionalProperties':True,'ids':[1]}
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(_response_value(json.dumps(tampered),schema))
