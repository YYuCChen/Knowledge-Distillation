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


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_fenced_parent_and_field_recovery_preserve_evidence_and_reuse_prepared(tmp_path, newline):
    value = payload()
    value['subtitle'] = value['title']
    class Fenced(Client):
        def complete(self, **kwargs):
            return '```json' + newline + super().complete(**kwargs) + newline + '```'
    client = Fenced([value, {'subtitle': '损耗的来源与边界。'}])
    model = AnthropicKnowledgeModel(client, tmp_path)
    result = model.derive(SOURCE)
    assert result.core_points[0].argument == value['core_points'][0]['argument']
    assert result.evidence[0].text == SOURCE
    assert model.derive(SOURCE) == result
    assert len(client.calls) == 2
    states = [json.loads(p.read_text())['state'] for p in tmp_path.glob('*/responses/pending.json')]
    assert states == ['prepared']


def test_interrupted_presentation_save_resumes_received_field_without_new_request(tmp_path, monkeypatch):
    from knowledge_distiller.v1.knowledge_presentation import PresentationRecord
    value = payload()
    value['subtitle'] = value['title']
    client = Client([value, {'subtitle': '损耗的来源与边界。'}])
    complete = PresentationRecord.complete
    def fail(*args, **kwargs):
        raise OSError('injected result save interruption')
    monkeypatch.setattr(PresentationRecord, 'complete', fail)
    with pytest.raises(KnowledgeModelError, match='knowledge_checkpoint_unavailable'):
        AnthropicKnowledgeModel(client, tmp_path).derive(SOURCE)
    monkeypatch.setattr(PresentationRecord, 'complete', complete)
    result = AnthropicKnowledgeModel(client, tmp_path).derive(SOURCE)
    assert result.evidence[0].text == SOURCE
    assert len(client.calls) == 2


def test_source_or_model_change_starts_new_request_and_preserves_prior_records(tmp_path):
    client = Client([payload(), payload()])
    AnthropicKnowledgeModel(client, tmp_path).derive(SOURCE)
    client.model = 'different-fixture-model'
    AnthropicKnowledgeModel(client, tmp_path).derive(SOURCE)
    assert len(client.calls) == 2
    assert len(list(tmp_path.glob('*/responses/pending.json'))) == 2


def test_two_simultaneous_derivations_cannot_replace_each_others_response(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    started, release = Event(), Event()
    class Slow(Client):
        def complete(self, **kwargs):
            started.set()
            assert release.wait(5)
            return super().complete(**kwargs)
    client = Slow([payload()])
    model = AnthropicKnowledgeModel(client, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(model.derive, SOURCE)
        assert started.wait(5)
        try:
            with pytest.raises(KnowledgeModelError, match='knowledge_checkpoint_unavailable'):
                model.derive(SOURCE)
        finally:
            release.set()
        result = first.result(5)
    assert model.derive(SOURCE) == result
    assert len(client.calls) == 1


def test_long_checkpoint_paths_preserve_field_response_across_restart(tmp_path, monkeypatch):
    """A real >MAX_PATH destination, not a shortened temporary filename/root."""
    from knowledge_distiller.v1.knowledge_presentation import PresentationRecord
    from knowledge_distiller.v1.windows_platform import filesystem_path
    root = tmp_path
    while len(str(root)) < 310:
        root = root / ('checkpoint-' + 'x' * 40)
    value = payload()
    value['subtitle'] = value['title']
    client = Client([value, {'subtitle': '损耗的来源与边界。'}])
    original = PresentationRecord.complete
    def interrupted(*args, **kwargs):
        raise OSError('injected final checkpoint interruption')
    monkeypatch.setattr(PresentationRecord, 'complete', interrupted)
    with pytest.raises(KnowledgeModelError, match='knowledge_checkpoint_unavailable'):
        AnthropicKnowledgeModel(client, root).derive(SOURCE)
    monkeypatch.setattr(PresentationRecord, 'complete', original)
    result = AnthropicKnowledgeModel(client, root).derive(SOURCE)
    assert result.evidence[0].text == SOURCE
    assert len(client.calls) == 2
    retained = list(filesystem_path(root).glob('*/*.json'))
    assert any(len(path.stem) == 64 and len(str(path)) > 400 for path in retained)
    assert all(isinstance(json.loads(path.read_text(encoding='utf-8')), dict) for path in retained)
    assert list(filesystem_path(root).glob('*/presentation-result.json'))
    assert not list(filesystem_path(root).rglob('*.tmp'))
    # The next instance resumes the same physical records, not a new shortened namespace.
    assert AnthropicKnowledgeModel(client, root).derive(SOURCE) == result
    assert len(client.calls) == 2


def test_checkpoint_path_conversion_does_not_resolve_away_symlink_check(tmp_path, monkeypatch):
    from pathlib import Path
    from knowledge_distiller.v1.knowledge_presentation import PresentationRecord
    def forbidden_resolve(*args, **kwargs):
        raise AssertionError('record paths must remain lexical')
    monkeypatch.setattr(Path, 'resolve', forbidden_resolve)
    record = PresentationRecord(tmp_path, {'snapshot': SOURCE})
    record.root.mkdir(parents=True)
    original = type(record.root).is_symlink
    monkeypatch.setattr(type(record.root), 'is_symlink',
        lambda path: True if path == record.root else original(path))
    with pytest.raises(OSError, match='knowledge_checkpoint_unsafe'):
        record.retain('{}')
    assert not list(record.root.glob('*.json'))
