import json
import pytest
from knowledge_distiller.v1.knowledge_model import AnthropicKnowledgeModel, KnowledgeModelError
from knowledge_distiller.v1.knowledge_presentation import display_line
from knowledge_distiller.v1.llm import LLMRequestError
from .test_knowledge_model import knowledge_payload

SOURCE = '持续切换会带来额外损耗。'


def payload():
    value = json.loads(knowledge_payload())
    value['evidence'] = [{'id': 'e1', 'start_segment': 's1', 'end_segment': 's1'}]
    return value


class Client:
    model = 'test-model'
    base_url = 'fixture://local'
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value, ensure_ascii=False)


def test_softwrap_is_display_only_and_does_not_touch_code():
    assert display_line('一句内容。\n另一句内容。') == '一句内容。 另一句内容。'
    assert display_line('```py\n  x = 1\n```') == '```py\n  x = 1\n```'
    assert display_line('code\n    indented') == 'code\n    indented'
    assert display_line('a\tb\nc\td') == 'a\tb\nc\td'


def test_plain_summary_newlines_do_not_require_regenerating_knowledge(tmp_path):
    value = payload()
    value['summary'] = '主动设定边界。\n减少注意力损耗。'
    client = Client([value])
    knowledge = AnthropicKnowledgeModel(client, tmp_path).derive(SOURCE)
    assert knowledge.summary == '主动设定边界。 减少注意力损耗。'
    assert knowledge.core_points[0].argument == value['core_points'][0]['argument']
    assert len(client.calls) == 1
    assert list(tmp_path.glob('*/presentation-result.json'))


def test_failed_field_retry_preserves_original_candidate_across_restart(tmp_path):
    value = payload()
    value['subtitle'] = value['title']
    first = Client([value, LLMRequestError('llm_request_failed')])
    with pytest.raises(KnowledgeModelError, match='knowledge_presentation_incomplete'):
        AnthropicKnowledgeModel(first, tmp_path).derive(SOURCE)
    assert len(first.calls) == 2
    pending = next(tmp_path.glob('*/pending.json'))
    assert pending.exists()
    second = Client([{'subtitle': '从持续切换的损耗理解注意力边界。'}])
    result = AnthropicKnowledgeModel(second, tmp_path).derive(SOURCE)
    assert len(second.calls) == 1
    assert json.loads(second.calls[0]['user'])['fields'] == ['subtitle']
    assert result.title == value['title']
    assert result.core_points[0].argument == value['core_points'][0]['argument']
    assert result.evidence[0].text == SOURCE
    assert not pending.exists()


def test_field_response_cannot_change_points_or_evidence(tmp_path):
    value = payload()
    value['subtitle'] = value['title']
    client = Client([value, {'subtitle': '补充场景', 'core_points': []}])
    with pytest.raises(KnowledgeModelError, match='knowledge_presentation_incomplete'):
        AnthropicKnowledgeModel(client, tmp_path).derive(SOURCE)
    assert next(tmp_path.glob('*/pending.json')).exists()


def test_shared_parser_normalizes_display_without_modifying_evidence():
    from knowledge_distiller.v1.knowledge_model import parse_knowledge
    value = json.loads(knowledge_payload())
    value['evidence'][0]['occurrence'] = 0
    value['summary'] = '主动设定边界。\n减少注意力损耗。'
    result = parse_knowledge(SOURCE, json.dumps(value, ensure_ascii=False))
    assert result.summary == '主动设定边界。 减少注意力损耗。'
    assert result.evidence[0].text == value['evidence'][0]['text']
    assert result.core_points[0].argument == value['core_points'][0]['argument']


def test_pipeline_retry_retains_item_candidate_and_cleans_after_commit(tmp_path):
    from knowledge_distiller.v1.pipeline import Distiller
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.domain import CapturedMaterial, SourceFact
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    item = store.create_item('https://www.douyin.com/video/123')
    media = tmp_path / 'fixture'
    media.write_bytes(b'fixture')
    material = store.attach_material(item, CapturedMaterial('douyin', '123', 'url', 'url', {}, media, 1))
    store.establish_source_fact(material, SourceFact(SOURCE))
    runtime = tmp_path / 'runtime'
    value = payload()
    value['subtitle'] = value['title']
    first = Client([value, LLMRequestError('llm_request_failed')])
    def pipeline(client):
        return Distiller(store=store, source=None, normalizer=None, recognizer=None,
            reviewer=None, confirmation_clipper=None, knowledge_model=AnthropicKnowledgeModel(client),
            runtime_root=runtime, vault=None, ocr=object())
    assert pipeline(first).run(item).state == 'failed'
    checkpoint = runtime / 'items' / str(item) / 'knowledge'
    assert next(checkpoint.glob('*/pending.json')).exists()
    second = Client([{'subtitle': '从持续切换的损耗理解注意力边界。'}])
    # Publication intentionally has no Vault; the knowledge commit still ends
    # checkpoint ownership and the ordinary item cleanup must run.
    assert pipeline(second).run(item).state == 'failed'
    assert store.item_bundle(item)['knowledge_result_id'] is not None
    assert store.item_bundle(item)['error_code'] == 'vault_not_configured'
    assert len(second.calls) == 1
    assert not checkpoint.exists()
