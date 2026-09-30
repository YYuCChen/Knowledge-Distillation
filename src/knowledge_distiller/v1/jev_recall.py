"""Historical recall on Jev (user decision 2026-09-30).

Organization's recall step only selects which frozen history items the next
role expands; it writes no text. With a Jev key saved, each candidate becomes
one yes/no (noul) question in a single request, and the answers are turned
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
                answers, _model = self.client.ask({'new_materials': input_payload['frozen_new'], 'history': history},
                                                  questions)
            except JevError as error:
                logger.error('historical recall on Jev failed (%s)', error)
                raise GrowthRuntimeFailed() from error
            for key, (field, identity) in owners.items():
                answer = answers.get(key)
                value = answer.get('noul') if isinstance(answer, dict) else None
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    logger.error('historical recall on Jev returned an invalid answer')
                    raise GrowthRuntimeFailed()
                if value >= THRESHOLD:
                    selection[field].append(identity)
        return GrowthRuntimeResult(json.dumps({'codec': 'historical-recall-v1', **selection}), 'end_turn')
