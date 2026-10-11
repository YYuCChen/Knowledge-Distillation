"""Large synthetic recall inputs: coverage, transport limits and atomic failure."""
import copy
import json
import math

import pytest

from knowledge_distiller.growth_modeling import GrowthRuntimeFailed
from knowledge_distiller.v1.jev import JevClient, JevError
from knowledge_distiller.v1.jev_recall import JevRecallRuntime, STATE_BYTES, REQUEST_BYTES, _bytes
from tests.v1.test_jev import Response, recall_payload


def large_payload():
    payload = recall_payload()
    payload['frozen_new'] = [dict(knowledge_result_id=100+i, title=f'新材料{i}',
        summary='这是合成材料，讨论不同证据的联系。'*100, points=[]) for i in range(20)]
    payload['eligible_history'] = [dict(knowledge_result_id=i, title=f'历史材料{i}',
        summary='这些也是合成材料，没有私人知识内容。'*80, points=[]) for i in range(55)]
    payload['accepted_current'] = [dict(insight_version_id=i, claim='合成新知'*80) for i in range(2)]
    payload['relation_current'] = [dict(relation_version_id=i, payload={'claim':'合成关系'*80}) for i in range(11)]
    payload['reconsideration_hints'] = []
    return payload


def complete(client, payload):
    return JevRecallRuntime(client).complete(system_prompt='', input_payload=payload, max_tokens=10)


def test_large_matrix_preserves_every_record_and_pair_and_stable_union():
    payload = large_payload()
    original = copy.deepcopy(payload)
    pairs = []
    calls = []

    class Transport:
        def ask(self, state, questions):
            calls.append(state)
            size = _bytes(state)
            assert size + max(map(_bytes, questions.values())) <= STATE_BYTES
            assert size + _bytes(questions) <= REQUEST_BYTES
            assert set(state['history']) == set(questions)
            new_ids = [row['knowledge_result_id'] for row in state['new_materials']]
            for key in questions:
                pairs.extend((n, key) for n in new_ids)
            # A match only present in the final new-material batch must survive
            # earlier negatives and must be returned once, in original ID order.
            return {key: {'noul': .8 if 119 in new_ids and key in {'s54', 'i1', 'r10'} else .1}
                    for key in questions}, 'synthetic'

    result = json.loads(complete(Transport(), payload).text)
    expected_keys = {*(f's{i}' for i in range(55)), 'i0', 'i1', *(f'r{i}' for i in range(11))}
    assert len(calls) > 1
    assert set(pairs) == {(100+i, key) for i in range(20) for key in expected_keys}
    assert len(pairs) == len(set(pairs))
    assert result['source_knowledge_ids'] == [54]
    assert result['accepted_insight_version_ids'] == [1]
    assert result['current_relation_version_ids'] == [10]
    assert result['reconsideration_hint_version_ids'] == []
    assert payload == original


def test_service_context_error_splits_further_without_hiding_other_errors():
    seen = []

    def post(url, **kwargs):
        body = kwargs['json']; seen.append(body)
        if len(body['questions']) > 1 or len(body['state']['new_materials']) > 1:
            return Response(400, {'detail': {'error_type': 'max_tokens_exceeded'}})
        return Response(200, {'answers': {key: {'noul': .8} for key in body['questions']}})

    jev = JevClient(lambda: 'synthetic-test-key', post=post)
    result = json.loads(complete(jev, recall_payload()).text)
    assert result['source_knowledge_ids'] == [3, 4]
    assert result['reconsideration_hint_version_ids'] == [5]
    assert len(seen) > 1


@pytest.mark.parametrize('failure', ['auth', 'network', 'missing', 'nan', 'bool'])
def test_a_later_batch_failure_never_returns_partial_selection(failure):
    calls = 0

    class Transport:
        def ask(self, state, questions):
            nonlocal calls
            calls += 1
            if calls > 1:
                if failure in {'auth', 'network'}:
                    raise JevError('jev_unauthorized' if failure == 'auth' else 'jev_request_failed')
                return {key: {'noul': math.nan if failure == 'nan' else True}
                        for key in questions} if failure != 'missing' else {}, 'synthetic'
            return {key: {'noul': .9} for key in questions}, 'synthetic'

    with pytest.raises(GrowthRuntimeFailed):
        complete(Transport(), large_payload())
    assert calls == 2


def test_one_unsplittable_record_is_rejected_without_truncation_or_guess():
    payload = recall_payload()
    payload['frozen_new'] = [dict(knowledge_result_id=9, summary='合成内容'*STATE_BYTES)]
    payload['eligible_history'] = [payload['eligible_history'][0]]
    payload['accepted_current'] = payload['relation_current'] = payload['reconsideration_hints'] = []

    class Transport:
        def ask(self, *args):
            pytest.fail('Oversized indivisible input must not be sent or truncated')

    with pytest.raises(GrowthRuntimeFailed):
        complete(Transport(), payload)


def test_non_size_http_400_stays_a_failure():
    client = JevClient(lambda: 'synthetic-key', post=lambda *a, **k: Response(400, {'detail': {'error_type':'other'}}))
    with pytest.raises(JevError, match='jev_request_failed'):
        client.ask('合成内容', {})
