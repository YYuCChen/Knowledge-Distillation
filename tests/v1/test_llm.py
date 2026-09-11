from types import SimpleNamespace

import httpx
import pytest

from knowledge_distiller.v1.llm import AnthropicMessagesClient, LLMRequestError


class Response:
    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self.payload = payload or {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "result"}],
        }

    def json(self):
        return self.payload


def test_client_sends_one_role_specific_request(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: calls.append((args, kwargs)) or Response())
    client = AnthropicMessagesClient(
        "https://models.example.com",
        "model-1",
        lambda: "private",
    )

    result = client.complete(system="system role", user="source", max_tokens=100)

    assert result == "result"
    args, options = calls[0]
    assert args == ("https://models.example.com/v1/messages",)
    assert options["headers"]["x-api-key"] == "private"
    assert options["json"]["system"] == "system role"
    assert options["json"]["messages"] == [{"role": "user", "content": "source"}]
    assert options["json"]["thinking"] == {"type": "disabled"}


def test_missing_binding_does_not_read_keychain() -> None:
    client = AnthropicMessagesClient("", "", lambda: pytest.fail("must not read"))

    with pytest.raises(LLMRequestError) as failure:
        client.complete(system="system", user="source", max_tokens=10)

    assert failure.value.args == ("llm_not_configured",)


def test_confirmed_bad_configuration_is_marked_unavailable(monkeypatch) -> None:
    marks = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response(401))
    client = AnthropicMessagesClient(
        "https://models.example.com",
        "bad-model",
        lambda: "private",
        mark_unavailable=lambda: marks.append(True),
    )

    with pytest.raises(LLMRequestError) as failure:
        client.complete(system="system", user="source", max_tokens=10)

    assert failure.value.args == ("llm_config_unavailable",)
    assert marks == [True]


def test_transient_transport_failure_does_not_relabel_configuration(monkeypatch) -> None:
    marks = []

    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", fail)
    client = AnthropicMessagesClient(
        "https://models.example.com",
        "model-1",
        lambda: "private",
        mark_unavailable=lambda: marks.append(True),
    )

    with pytest.raises(LLMRequestError) as failure:
        client.complete(system="system", user="source", max_tokens=10)

    assert failure.value.args == ("llm_request_failed",)
    assert marks == []


@pytest.mark.parametrize(
    "payload",
    [
        {"stop_reason": "max_tokens", "content": []},
        {"stop_reason": "end_turn", "content": "not-a-list"},
        {"stop_reason": "end_turn", "content": []},
    ],
)
def test_incomplete_or_empty_output_is_rejected(monkeypatch, payload) -> None:
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response(payload=payload))
    client = AnthropicMessagesClient(
        "https://models.example.com", "model-1", lambda: "private"
    )

    with pytest.raises(LLMRequestError):
        client.complete(system="system", user="source", max_tokens=10)


@pytest.mark.parametrize('base', ['https://api.openai.com', 'https://api.openai.com/v1'])
def test_openai_responses_protocol_with_text_and_image(monkeypatch, base):
    from knowledge_distiller.v1.llm import OpenAIResponsesClient
    calls = []
    payload = {'status': 'completed', 'output': [
        {'type': 'reasoning', 'summary': []},
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: calls.append((a, kw)) or Response(payload=payload))
    client=OpenAIResponsesClient(base, 'selected-model', lambda:'test-secret')
    result=client.complete(system='Return JSON',user=[{'type':'text','text':'source'},
        {'type':'image','source':{'type':'base64','media_type':'image/png','data':'aW1hZ2U='}}],max_tokens=100)
    assert result == '{"ok":true}'
    args,options=calls[0]
    assert args == ('https://api.openai.com/v1/responses',)
    assert options['headers']['Authorization']=='Bearer test-secret'
    assert 'x-api-key' not in options['headers']
    assert options['json']=={'model':'selected-model','instructions':'Return JSON',
        'input':[{'role':'user','content':[{'type':'input_text','text':'source'},
            {'type':'input_image','image_url':'data:image/png;base64,aW1hZ2U='}]}],
        'max_output_tokens':100,'store':False}


@pytest.mark.parametrize('status,payload,code,mark', [
    (401,{},'llm_config_unavailable',True),
    (429,{},'llm_request_failed',False),
    (500,{},'llm_request_failed',False),
    (200,{'status':'incomplete','output':[]},'llm_response_incomplete',False),
    (200,{'status':'completed','output':[]},'llm_response_invalid',False),
    (200,{'status':'completed','output':None},'llm_response_invalid',False),
])
def test_openai_failure_never_returns_success(monkeypatch,status,payload,code,mark):
    from knowledge_distiller.v1.llm import OpenAIResponsesClient
    monkeypatch.setattr(httpx,'post',lambda *a,**kw:Response(status,payload))
    marks=[]
    with pytest.raises(LLMRequestError,match=code):
        OpenAIResponsesClient('https://api.openai.com','model',lambda:'key',lambda:marks.append(True)).complete(
            system='',user='text',max_tokens=100)
    assert bool(marks) is mark


@pytest.mark.parametrize('code,stop_reason', [
    ('llm_response_incomplete', 'incomplete'),
    ('llm_response_invalid', 'end_turn'),
])
def test_review_keeps_response_failure_category(code, stop_reason, caplog):
    from knowledge_distiller.v1.reviewer import ReviewBinding
    class Client:
        def complete(self, **kwargs):
            raise LLMRequestError(code)
    result = ReviewBinding(Client()).complete('private transcript')
    assert result.text == ''
    assert result.stop_reason == stop_reason
    assert code in caplog.text
    assert 'private transcript' not in caplog.text


@pytest.mark.parametrize('base,model,disabled', [
    ('https://api.deepseek.com', 'deepseek-v4-flash', True),
    ('https://api.deepseek.com/v1', 'deepseek-v4-pro', True),
    ('https://api.openai.com', 'selected-model', False),
    ('https://other.example', 'deepseek-v4-flash', False),
])
def test_source_review_sets_deepseek_non_thinking_without_changing_shared_client(monkeypatch, base, model, disabled):
    from knowledge_distiller.v1.llm import OpenAIResponsesClient
    from knowledge_distiller.v1.reviewer import build_reviewer
    from knowledge_distiller.primary import PrimaryRecovery
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs['json'])
        if disabled and kwargs['json'].get('reasoning') != {'effort': 'none'}:
            return Response(payload={'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}, 'output': []})
        return Response(payload={'status': 'completed', 'output': [{'type': 'message',
            'role': 'assistant', 'content': [{'type': 'output_text',
            'text': '{"candidate_text":"这是完整原文。","issues":[]}'}]}]})
    monkeypatch.setattr(httpx, 'post', post)
    client = OpenAIResponsesClient(base, model, lambda: 'private')
    result = build_reviewer(client).review(PrimaryRecovery('这是完整原文。', None, ()))
    assert result.candidate is not None
    assert (calls[0].get('reasoning') == {'effort': 'none'}) == disabled
    assert client.reasoning_effort is None
    assert len(calls) == 1


def test_review_records_and_reuses_only_matching_valid_result(tmp_path):
    import json
    from dataclasses import dataclass
    from knowledge_distiller.v1.reviewer import build_reviewer
    from knowledge_distiller.primary import PrimaryRecovery
    calls = []
    @dataclass
    class Client:
        model: str = 'model-a'
        def complete(self, **kwargs):
            calls.append(kwargs)
            return json.dumps({'candidate_text': '这是完整原文。', 'issues': []}, ensure_ascii=False)
    source = PrimaryRecovery('这是完整原文。', None, ())
    reviewer = build_reviewer(Client())
    assert reviewer.review_in_directory(source, tmp_path).candidate
    assert reviewer.review_in_directory(source, tmp_path).candidate
    assert len(calls) == 1
    path = tmp_path / 'review-response.json'
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    record['text'] = 'invalid result'
    path.write_text(json.dumps(record))
    assert reviewer.review_in_directory(source, tmp_path).candidate
    assert len(calls) == 2
    assert build_reviewer(Client('model-b')).review_in_directory(source, tmp_path).candidate
    assert len(calls) == 3
