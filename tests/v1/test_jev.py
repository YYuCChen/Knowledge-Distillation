"""TypeSafe Jev client and the quick-note identity judge on it (synthetic transport only)."""
import httpx
import pytest

from knowledge_distiller.v1.captures import JevIdentityJudge
from knowledge_distiller.v1.jev import ENDPOINT, JevClient, JevError


class Response:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def answer(choice, probabilities, confidence=0.8, model='jev-1.13.0'):
    return Response(200, {'model': model, 'answers': {'answer': {
        'type': 'choice', 'choice': choice, 'probabilities': probabilities, 'confidence': confidence}},
        'usage': {'input_tokens': 120, 'output_tokens': 8}})


def client(*responses, key='test-key'):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        posts = sum(1 for call in calls if call[0] != 'sleep')
        result = responses[min(posts, len(responses)) - 1]
        if isinstance(result, Exception):
            raise result
        return result
    return JevClient(lambda: key, post=post, sleep=lambda seconds: calls.append(('sleep', seconds))), calls


def test_request_follows_the_documented_shape():
    jev, calls = client(answer('my_thought', {'my_thought': 0.95, 'third_party': 0.03, 'unknown': 0.02}))
    decided = JevIdentityJudge(jev).judge('我觉得周会可以隔周开', recent_delivery=None)
    url, kwargs = calls[0]
    assert url == ENDPOINT == 'https://api.typesafe.ai/v1/systemone'
    assert kwargs['headers']['Authorization'] == 'Bearer test-key'
    body = kwargs['json']
    assert body['model'] == 'jev-latest' and body['state'] == {'message': '我觉得周会可以隔周开',
                                                               'just_after_a_delivery': False}
    question = body['questions']['answer']
    assert question['type'] == 'choice' and set(question['criteria']) == {'my_thought', 'third_party', 'unknown'}
    assert decided == ('my_thought', 'Jev·jev-1.13.0', 0.95, None)


@pytest.mark.parametrize('choice,probability,target,expected', [
    ('my_thought', 0.89, None, None),                 # "Mine" needs the highest bar.
    ('third_party', 0.8, None, 'third_party'),
    ('third_party', 0.79, None, None),
    ('annotation', 0.85, 'om_link', 'annotation'),
    ('unknown', 0.99, 'om_link', None),               # Undecided never becomes anyone's words.
])
def test_thresholds_use_the_calibrated_probability(choice, probability, target, expected):
    rest = round(1 - probability, 4)
    jev, _ = client(answer(choice, {choice: probability, 'unknown' if choice != 'unknown' else 'my_thought': rest},
                           confidence=0.99))
    decided = JevIdentityJudge(jev).judge('这篇重点看后半段', recent_delivery=target)
    assert (decided[0] if decided else None) == expected
    if expected == 'annotation':
        assert decided[3] == 'om_link'


def test_annotation_is_offered_only_right_after_a_delivery():
    jev, calls = client(answer('annotation', {'annotation': 0.9, 'unknown': 0.1}))
    with pytest.raises(JevError, match='jev_response_invalid'):  # Not an offered option.
        JevIdentityJudge(jev).judge('重点看后半段', recent_delivery=None)
    assert 'annotation' not in calls[0][1]['json']['questions']['answer']['criteria']


@pytest.mark.parametrize('status,code', [(401, 'jev_unauthorized'), (403, 'jev_unauthorized'),
                                         (422, 'jev_request_invalid'), (500, 'jev_request_failed')])
def test_errors_are_stable_codes(status, code):
    jev, _ = client(Response(status, {'error': 'x'}))
    with pytest.raises(JevError, match=code):
        jev.choose('x', instructions='?', options={'a': 'A', 'b': 'B'})


def test_busy_service_is_retried_with_backoff_then_reported():
    jev, calls = client(Response(429), Response(529), answer('a', {'a': 1.0}))
    assert jev.choose('x', instructions='?', options={'a': 'A', 'b': 'B'}).choice == 'a'
    assert [c for c in calls if c[0] == 'sleep'] == [('sleep', 1), ('sleep', 2)]
    jev, _ = client(Response(529))
    with pytest.raises(JevError, match='jev_busy'):
        jev.choose('x', instructions='?', options={'a': 'A'})


@pytest.mark.parametrize('body', [
    ValueError('not json'), {'answers': []}, {'answers': {'answer': {'choice': 'c', 'probabilities': {'c': 1},
                                                                    'confidence': 1}}},
    {'answers': {'answer': {'choice': 'a', 'probabilities': {'a': 0.5, 'b': 0.2}, 'confidence': 0.5}}},
    {'answers': {'answer': {'choice': 'a', 'probabilities': {'a': 'high'}, 'confidence': 1}}},
])
def test_malformed_answers_are_rejected_not_guessed(body):
    jev, _ = client(Response(200, body))
    with pytest.raises(JevError, match='jev_response_invalid'):
        jev.choose('x', instructions='?', options={'a': 'A', 'b': 'B'})


def test_missing_key_and_network_failure():
    jev, calls = client(answer('a', {'a': 1.0}), key='')
    with pytest.raises(JevError, match='jev_secret_unavailable'):
        jev.choose('x', instructions='?', options={'a': 'A'})
    assert calls == []  # Nothing is sent without a key.
    jev, _ = client(httpx.ConnectError('offline'))
    with pytest.raises(JevError, match='jev_request_failed'):
        jev.choose('x', instructions='?', options={'a': 'A'})
