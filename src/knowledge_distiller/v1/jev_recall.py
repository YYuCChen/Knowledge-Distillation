"""Historical recall on Jev (user decision 2026-09-30).

Organization's recall step only selects which frozen history items the next
role expands; it writes no text. With a Jev key saved, each candidate becomes
one yes/no (noul) question in bounded requests, and the answers are turned
into the same historical-recall-v1 selection the LLM used to return. A Jev
failure fails the step; it is never replaced by the LLM or a guess.
"""
from __future__ import annotations

import json
import logging

from knowledge_distiller.growth_modeling import GrowthRuntimeFailed, GrowthRuntimeResult

from .jev import JevError

logger = logging.getLogger(__name__)
THRESHOLD = 0.5  # Calibrated: selected when "worth expanding" is more likely than not.
# UTF-8 bytes are a conservative packing proxy, not a Jev tokenizer. Leave
# headroom below the documented 32k state/longest-question and 64k total limits.
STATE_BYTES = 24_000
REQUEST_BYTES = 48_000


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def _split(new, entries):
    """Partition one axis; the Cartesian product still evaluates every pair."""
    if len(entries) > 1 and (len(new) <= 1 or _bytes(entries) >= _bytes(new)):
        middle = len(entries) // 2
        return ((new, entries[:middle]), (new, entries[middle:]))
    if len(new) > 1:
        middle = len(new) // 2
        return ((new[:middle], entries), (new[middle:], entries))
    # Never truncate one oversized record or guess a recall result.
    raise JevError('jev_context_exceeded')


def _recall(client, new, entries, selected, metrics):
    state = {'new_materials': new, 'history': {key: row for key, row, _ in entries}}
    questions = {key: question for key, _, question in entries}
    state_bytes = _bytes(state)
    bounded = (state_bytes + max((_bytes(q) for q in questions.values()), default=0) <= STATE_BYTES
               and state_bytes + _bytes(questions) <= REQUEST_BYTES)
    if bounded:
        try:
            answers, _model = client.ask(state, questions)
        except JevError as error:
            if str(error) != 'jev_context_exceeded':
                raise
            # Service/tokenizer limits can change: split further only on its
            # explicit size error. Auth/network/invalid answers fail the event.
        else:
            metrics['batches'] += 1
            metrics['max_state_bytes'] = max(metrics['max_state_bytes'], state_bytes)
            for key in questions:
                answer = answers.get(key)
                value = answer.get('noul') if isinstance(answer, dict) else None
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    raise JevError('jev_response_invalid')
                if value >= THRESHOLD:
                    selected.add(key)
            return
    for part_new, part_entries in _split(new, entries):
        _recall(client, part_new, part_entries, selected, metrics)

_LISTS = (  # (key prefix, input list, identity field, output field, question)
    ('s', 'eligible_history', 'knowledge_result_id', 'source_knowledge_ids',
     '这份历史素材是否值得在本次整理中展开？它和 state.new_materials 讨论同一对象或问题，可能相互支持、相互矛盾，'
     '或放在一起能看出单篇看不出的新认识时为是；只是领域相近、没有具体关联时为否。'),
    ('i', 'accepted_current', 'insight_version_id', 'accepted_insight_version_ids',
     '这条已接受的新知是否值得在本次整理中展开？state.new_materials 可能支持、反驳、细化或扩展它时为是；'
     '没有具体关联时为否。'),
    ('r', 'relation_current', 'relation_version_id', 'current_relation_version_ids',
     '这条现有关系是否值得在本次整理中展开？state.new_materials 涉及它的参与者、可能加强或改变它时为是；否则为否。'),
    ('h', 'reconsideration_hints', 'relation_version_id', 'reconsideration_hint_version_ids',
     '这条待重新审查的关系是否因为 state.new_materials 需要在本次整理中重新审查？新材料与它直接相关时为是；否则为否。'),
)


class JevRecallRuntime:
    """A GrowthRuntimeBinding for HistoricalRecallAdapter only."""

    def __init__(self, client):
        self.client = client

    def is_available(self) -> bool:
        return self.client is not None

    def complete(self, *, system_prompt, input_payload, max_tokens):
        if input_payload.get('codec') != 'historical-recall-input-v1':
            raise GrowthRuntimeFailed()
        history, questions, owners = {}, {}, {}
        for prefix, source, identity_key, field, question in _LISTS:
            for row in input_payload[source]:
                key = f'{prefix}{row[identity_key]}'
                history[key] = row
                questions[key] = {'type': 'noul', 'instructions': f'state.history["{key}"]：{question}'}
                owners[key] = (field, row[identity_key])
        selection = {field: [] for _, _, _, field, _ in _LISTS}
        if questions:
            try:
                selected = set()
                metrics = {'batches': 0, 'max_state_bytes': 0}
                _recall(self.client, input_payload['frozen_new'],
                        [(key, row, questions[key]) for key, row in history.items()], selected, metrics)
            except JevError as error:
                logger.error('historical recall on Jev failed (%s)', error)
                raise GrowthRuntimeFailed() from error
            logger.info('Jev recall completed: batches=%s max_state_bytes=%s',
                        metrics['batches'], metrics['max_state_bytes'])
            for key, (field, identity) in owners.items():
                if key in selected:
                    selection[field].append(identity)
        return GrowthRuntimeResult(json.dumps({'codec': 'historical-recall-v1', **selection}), 'end_turn')
