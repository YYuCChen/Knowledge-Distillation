"""Repair only missing scan labels; preserve the complete candidate and lineage."""
from copy import deepcopy
import hashlib
import json
import re

from .llm import LLMRequestError


def valid_tags(value):
    return (isinstance(value, list) and len(value) == 3
            and all(isinstance(tag, str) and re.fullmatch(r'[\u3400-\u9fff]{4}', tag) for tag in value)
            and len(set(value)) == 3)


def prepare_labels(value, calls):
    result = deepcopy(value)
    attempted = 0
    try:
        for candidate in result['candidate_versions']:
            payload = candidate['payload']
            if valid_tags(payload.get('scan_tags')):
                continue
            # The label request cannot replace any claim, premise, participant,
            # reason, identity or prior published snapshot.
            identity = hashlib.sha256(json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            stage = 'insight_labels_' + identity[:24]
            retained = calls.records.get('growth', {}).get('presentation_labels', {}).get(identity)
            if valid_tags(retained):
                payload['scan_tags'] = retained
                continue
            if attempted >= 4:
                raise LLMRequestError('insight_labels_incomplete')
            attempted += 1
            fields = {k: payload[k] for k in ('claim', 'short_discussion', 'connection_reasons')}
            schema = {'type': 'object', 'properties': {'scan_tags': {'type': 'array',
                'items': {'type': 'string'}, 'minItems': 3, 'maxItems': 3}},
                'required': ['scan_tags'], 'additionalProperties': False}
            fixed = calls.complete(stage,
                '只为给定候选准备三个互不相同、各四个汉字的阅读标签，辨认实际场景、对象和话题。'
                '根据现有候选表达命名，不新增事实，不改候选观点，不用通用流程词凑数。只输出scan_tags字段。',
                fields, schema, 512)
            if not valid_tags(fixed['scan_tags']):
                raise LLMRequestError('insight_labels_incomplete')
            payload['scan_tags'] = fixed['scan_tags']
            calls.accept(stage)
            if 'growth' in calls.records:
                calls.records['growth'].setdefault('presentation_labels', {})[identity] = fixed['scan_tags']
                calls.save('growth')
    except LLMRequestError:
        if 'growth' in calls.records:
            calls.records['growth']['field_recovery_pending'] = True
            calls.save('growth')
        raise
    if 'growth' in calls.records:
        calls.records['growth'].pop('field_recovery_pending', None)
        calls.save('growth')
    return result
