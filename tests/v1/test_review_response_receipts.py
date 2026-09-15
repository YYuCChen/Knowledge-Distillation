import json
from types import SimpleNamespace
import pytest
from knowledge_distiller.v1.review_responses import complete_review_response, review_receipts
from .test_model_json import wrap


def binding(tmp_path, raw, **extra):
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return raw
    return SimpleNamespace(record_path=tmp_path / 'review.json', feedback=(), parent_response_hash=None,
        identity=lambda source: source, client=SimpleNamespace(complete=complete), calls=calls, **extra)


@pytest.mark.parametrize('shell', [lambda s: s, wrap, lambda s: wrap(s, '\r\n')])
def test_review_received_response_reparsed_with_same_request_contract(tmp_path, shell):
    raw = shell(json.dumps({'candidate_text': '来源。', 'issues': []}))
    b = binding(tmp_path, raw)
    first = complete_review_response(b, '来源。', system='fixture', user='fixture', max_tokens=4096)
    assert complete_review_response(b, '来源。', system='fixture', user='fixture', max_tokens=4096) == first
    assert len(b.calls) == 1


@pytest.mark.parametrize('raw', ['bad-json', '{}'])
def test_review_same_parser_and_schema_failure_moves_to_new_request(tmp_path, raw):
    b = binding(tmp_path, raw)
    complete_review_response(b, '来源。')
    complete_review_response(b, '来源。')
    assert len(b.calls) == 2


def test_review_feedback_needs_proven_parent_to_reuse(tmp_path):
    b = binding(tmp_path, '{"candidate_text":"来源。","issues":[]}')
    b.feedback = ({'code': 'fixture_feedback'},)
    complete_review_response(b, '来源。')
    complete_review_response(b, '来源。')
    assert len(b.calls) == 2
    b.parent_response_hash = 'a' * 64
    complete_review_response(b, '来源。')
    complete_review_response(b, '来源。')
    assert len(b.calls) == 3


def test_review_without_pointer_never_selects_legacy_response(tmp_path):
    b = binding(tmp_path, '{"candidate_text":"新响应。","issues":[]}')
    b.record_path.write_text(json.dumps({'identity': '来源。', 'text': '{"candidate_text":"旧响应。","issues":[]}'}))
    assert '新响应' in complete_review_response(b, '来源。')
    assert len(b.calls) == 1
