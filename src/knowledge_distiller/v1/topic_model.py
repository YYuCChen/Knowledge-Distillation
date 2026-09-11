"""Topic organization uses the configured LLM transport, never a page GET."""
from dataclasses import asdict
import json

from knowledge_distiller.topic_indexing import (
    TOPIC_SYSTEM_PROMPT, TopicIndexing, TopicIndexFailure, parse_topic_plan,
)
from .llm import LLMRequestError
from .structured_calls import StructuredCalls
from .organization_contracts import topic_contract, topic_plan


class TopicModel:
    def __init__(self, client, calls=None):
        self.client = client
        self.calls = calls or StructuredCalls(client)

    def is_available(self):
        return bool(self.client.base_url.strip() and self.client.model.strip())

    def organize(self, points, existing_topics):
        payload = {
            'points': [dict(input_key=str(n), **{key: value for key, value in asdict(point).items() if key != 'source_fact_id'}) for n, point in enumerate(points)],
            'existing_topics': [asdict(topic) for topic in existing_topics],
        }
        system = TOPIC_SYSTEM_PROMPT.split("只返回一个JSON对象")[0]
        system += "\n输出topics定义主题：key是本次局部标识；复用主题填写topic_id，新主题填null。decisions必须逐一覆盖points的input_key。每项为所属主题的topic_key和在该主题内的position（从0连续编号），明确不归入任何主题时填写空数组。不要输出codec、members或unassigned_points；程序会根据完整决定构造它们。"
        try:
            value = self.calls.complete('topics', system, payload, topic_contract(points, existing_topics), 8192)
            text = json.dumps(topic_plan(value, points), ensure_ascii=False)
        except LLMRequestError:
            return TopicIndexing.failed(TopicIndexFailure.RUNTIME_FAILED)
        except (ValueError, KeyError, TypeError):
            return TopicIndexing.failed(TopicIndexFailure.INVALID_OUTPUT)
        plan = parse_topic_plan(text, points, existing_topics)
        if plan is not None:
            self.calls.accept('topics')
        return TopicIndexing.succeeded(plan) if plan is not None else TopicIndexing.failed(TopicIndexFailure.INVALID_OUTPUT)
