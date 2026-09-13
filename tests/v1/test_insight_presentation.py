from copy import deepcopy
import pytest
from knowledge_distiller.v1.insight_presentation import prepare_labels
from knowledge_distiller.v1.llm import LLMRequestError


class Calls:
    def __init__(self, fail=False):
        self.records = {'growth': {'text': 'full original response'}}
        self.fail = fail
        self.inputs = []
    def complete(self, stage, system, payload, schema, max_tokens):
        self.inputs.append(payload)
        if self.fail:
            raise LLMRequestError('offline')
        return {'scan_tags': ['渠道管理', '品牌定位', '经营风险']}
    def accept(self, stage):
        self.records[stage] = {'accepted': True}
    def save(self, stage):
        pass


def candidate(number=1):
    return {'candidate_versions': [{'new_insight_key': str(n), 'participants': [{'id': 5}],
        'payload': {'claim': '现有判断', 'short_discussion': '现有论证，不新增内容。',
                    'connection_reasons': ['理由一', '理由二'], 'scan_tags': ['短标签']}}
        for n in range(number)]}


def test_labels_only_repair_preserves_candidate_and_input():
    value = candidate()
    original = deepcopy(value)
    result = prepare_labels(value, Calls())
    assert value == original
    tags = result['candidate_versions'][0]['payload'].pop('scan_tags')
    expected = deepcopy(original)
    expected['candidate_versions'][0]['payload'].pop('scan_tags')
    assert result == expected and len(tags) == 3


def test_failed_label_attempt_retains_original_and_resumes_only_remaining_fields():
    value = candidate(6)
    original = deepcopy(value)
    calls = Calls()
    with pytest.raises(LLMRequestError, match='insight_labels_incomplete'):
        prepare_labels(value, calls)
    assert len(calls.inputs) == 4
    assert calls.records['growth']['field_recovery_pending']
    assert calls.records['growth']['text'] == 'full original response'
    result = prepare_labels(value, calls)
    assert len(calls.inputs) == 6
    assert value == original
    assert all(len(row['payload']['scan_tags']) == 3 for row in result['candidate_versions'])
    assert 'field_recovery_pending' not in calls.records['growth']
