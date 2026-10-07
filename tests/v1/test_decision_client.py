"""R17 text decision contract, entirely synthetic and no real HTTP."""
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from knowledge_distiller.v1.decision_client import (
    ChoiceAnswer, ChoiceQuestion, DecisionClient, DecisionError, DecisionProfile,
    JEV_ENDPOINT, NoulAnswer, NoulQuestion, clef_template_upper_bound,
)


def profile(provider='clef', **changes):
    return DecisionProfile(provider, JEV_ENDPOINT if provider == 'jev' else
                           'http://127.0.0.1:8197/v1/systemone',
                           'jev-latest' if provider == 'jev' else 'clef-4bit',
                           auth_ref='synthetic-key-ref' if provider == 'jev' else None, **changes)


def questions():
    return {'c': ChoiceQuestion({'question': 'Choose a.'}, {'a': 'A', 'b': None}),
            'n': NoulQuestion('Is this synthetic?', {'true': 'yes', 'false': 'no'})}


def payload(provider='clef'):
    return {'model': 'clef-4bit' if provider == 'clef' else 'jev-1.13.0',
            'answers': {'c': {'type': 'choice', 'choice': 'a', 'probabilities': {'a': .8, 'b': .2},
                              'confidence': .8 if provider == 'clef' else .6},
                        'n': {'type': 'noul', 'noul': .9}},
            'usage': {'input_tokens': 190, 'output_tokens': 0}}


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return deepcopy(self.data)


def client(provider='clef', *, data=None, status=200, key='synthetic-key', error=None):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if error:
            raise error
        return Response(payload(provider) if data is None else data, status)
    return DecisionClient(profile(provider), secret=lambda ref: key, post=post), calls


@pytest.mark.parametrize('provider', ['clef', 'jev'])
def test_native_contract_and_confidence_remain_distinct(provider):
    api, calls = client(provider)
    result = api.ask({'text': '合成文本'}, questions())
    url, request = calls[0]
    assert url == api.profile.endpoint and request['follow_redirects'] is False
    assert request['trust_env'] is False
    assert request['timeout'] == 20
    assert set(request['json']['questions']) == {'c', 'n'}
    assert result.provider == provider and result.model == payload(provider)['model']
    assert isinstance(result.answers['c'], ChoiceAnswer)
    assert result.answers['c'].probabilities == {'a': .8, 'b': .2}
    assert result.answers['c'].probability == .8
    assert result.answers['c'].confidence == (.8 if provider == 'clef' else .6)
    assert result.answers['c'].confidence_semantics == (
        'clef-max-probability' if provider == 'clef' else 'jev-normalized-concentration')
    assert isinstance(result.answers['n'], NoulAnswer) and result.answers['n'].noul == .9
    assert not hasattr(result.answers['n'], 'confidence')
    assert result.budget_check.exact is False
    if provider == 'clef':
        assert request['json']['truncate'] is False
        assert 'Authorization' not in request['headers']
    else:
        assert 'truncate' not in request['json']
        assert request['headers']['Authorization'] == 'Bearer synthetic-key'


@pytest.mark.parametrize('url', [
    'http://localhost:8000/v1/systemone', 'http://127.1/v1/systemone',
    'http://127.0.0.2/v1/systemone', 'http://[::1]/v1/systemone',
    'https://127.0.0.1/v1/systemone', 'http://127.0.0.1/v1',
    'http://127.0.0.1/v1/systemone/', 'http://127.0.0.1:0/v1/systemone',
    'http://127.0.0.1:65536/v1/systemone', 'http://127.0.0.1:bad/v1/systemone',
    'http://127.0.0.1:08000/v1/systemone', 'http://key@127.0.0.1/v1/systemone',
    'http://127.0.0.1/v1/systemone?redirect=cloud', 'http://127.0.0.1/v1/systemone#x',
])
def test_only_literal_loopback_full_endpoint_is_accepted(url):
    with pytest.raises(DecisionError, match='decision_profile_invalid'):
        replace(profile(), endpoint=url)


@pytest.mark.parametrize('changes', [
    {'provider': 'chat'}, {'protocol': 'openai-chat'}, {'model': ''}, {'model': 'bad\nmodel'},
    {'timeout_seconds': True}, {'timeout_seconds': float('nan')}, {'timeout_seconds': 0},
    {'token_budget': True}, {'token_budget': 0}, {'token_budget': 16385},
    {'auth_ref': 'secret with spaces'},
])
def test_invalid_profiles(changes):
    with pytest.raises(DecisionError, match='decision_profile_invalid'):
        replace(profile(), **changes)


def test_jev_requires_reference_and_resolved_key():
    with pytest.raises(DecisionError, match='decision_profile_invalid'):
        replace(profile('jev'), auth_ref=None)
    for key in ('', 'with\nnewline', ' key ', None, '非ASCII'):
        api, calls = client('jev', key=key)
        with pytest.raises(DecisionError, match='decision_secret_unavailable'):
            api.ask('synthetic', questions())
        assert not calls
    api, calls = client('jev')
    api.secret = lambda ref: (_ for _ in ()).throw(RuntimeError('sensitive'))
    with pytest.raises(DecisionError, match='^decision_secret_unavailable$') as caught:
        api.ask('synthetic', questions())
    assert caught.value.__suppress_context__ and not calls


@pytest.mark.parametrize('status,code', [
    (301, 'decision_redirect_refused'), (302, 'decision_redirect_refused'),
    (307, 'decision_redirect_refused'), (308, 'decision_redirect_refused'),
    (413, 'decision_budget_exceeded'), (401, 'decision_unauthorized'),
    (403, 'decision_unauthorized'), (400, 'decision_request_invalid'),
    (422, 'decision_request_invalid'), (429, 'decision_busy'), (529, 'decision_busy'),
    (500, 'decision_request_failed'), (204, 'decision_request_failed'),
])
def test_failures_no_retry_or_cloud_fallback(status, code):
    api, calls = client(status=status)
    with pytest.raises(DecisionError, match=f'^{code}$'):
        api.ask('synthetic', questions())
    assert len(calls) == 1 and calls[0][0].startswith('http://127.0.0.1:8197/')


@pytest.mark.parametrize('error,code', [
    (httpx.ConnectError('private body'), 'decision_request_failed'),
    (httpx.ReadTimeout('private body'), 'decision_timeout'),
])
def test_transport_failure_does_not_expose_request(error, code):
    api, calls = client(error=error)
    with pytest.raises(DecisionError, match=f'^{code}$') as caught:
        api.ask('sensitive synthetic state', questions())
    assert caught.value.__suppress_context__ and len(calls) == 1


def malformed_cases():
    cases = []
    def changed(mutator):
        data = payload()
        mutator(data)
        cases.append(data)
    changed(lambda d: d['answers'].pop('n'))
    changed(lambda d: d['answers'].update(extra={'type': 'noul', 'noul': .9}))
    changed(lambda d: d['answers']['c'].pop('type'))
    changed(lambda d: d['answers']['c'].update(type='noul'))
    changed(lambda d: d['answers']['c']['probabilities'].pop('b'))
    changed(lambda d: d['answers']['c']['probabilities'].update(extra=0))
    for value in (True, '0.8', None, float('nan'), float('inf'), -.1, 1.1):
        changed(lambda d, v=value: d['answers']['c']['probabilities'].update(a=v))
        changed(lambda d, v=value: d['answers']['c'].update(confidence=v))
        changed(lambda d, v=value: d['answers']['n'].update(noul=v))
    changed(lambda d: d['answers']['c'].update(choice='b'))
    changed(lambda d: d['answers']['c'].update(choice='extra'))
    changed(lambda d: d['answers']['c']['probabilities'].update(a=.7))
    changed(lambda d: d['answers']['c'].update(confidence=.6))
    changed(lambda d: d['answers']['n'].update(confidence=.8))
    changed(lambda d: d['usage'].update(input_tokens=True))
    changed(lambda d: d.pop('usage'))
    changed(lambda d: d.pop('model'))
    return cases


@pytest.mark.parametrize('data', malformed_cases())
def test_complete_strict_finite_response_contract(data):
    api, _ = client(data=data)
    with pytest.raises(DecisionError, match='decision_response_invalid'):
        api.ask('synthetic', questions())


def test_wrong_model_and_malformed_json():
    data = payload()
    data['model'] = 'chat-model'
    api, _ = client(data=data)
    with pytest.raises(DecisionError, match='decision_model_mismatch'):
        api.ask('synthetic', questions())
    api, _ = client(data=ValueError('secret'))
    with pytest.raises(DecisionError, match='decision_response_invalid'):
        api.ask('synthetic', questions())


def test_rounding_tolerance_preserves_all_options_and_raw_values():
    data = payload()
    data['answers']['c'].update(probabilities={'a': .3333, 'b': .3333, 'c': .3333}, confidence=.3333)
    api, _ = client(data=data)
    result = api.ask('synthetic', {'c': ChoiceQuestion('Choose.', {'a': 'A', 'b': 'B', 'c': 'C'}),
                                  'n': NoulQuestion('Synthetic?')})
    assert sum(result.answers['c'].probabilities.values()) == pytest.approx(.9999)


def test_template_budget_covers_state_questions_ids_options_and_never_truncates():
    api, calls = client()
    body = {'state': 'synthetic', 'questions': {k: q.wire() for k, q in questions().items()}}
    bound = clef_template_upper_bound(body)
    assert bound.exact is False and bound.units > len('synthetic')
    larger = deepcopy(body)
    larger['questions']['n']['instructions'] += '界' * 100
    assert clef_template_upper_bound(larger).units == bound.units + 300
    api.profile = replace(api.profile, token_budget=bound.units)
    assert api.ask('synthetic', questions()).budget_check == bound
    api.profile = replace(api.profile, token_budget=bound.units - 1)
    with pytest.raises(DecisionError, match='decision_budget_exceeded'):
        api.ask('synthetic', questions())
    assert len(calls) == 1 and calls[0][1]['json']['state'] == 'synthetic'
    api.profile = profile()
    with pytest.raises(DecisionError, match='decision_budget_exceeded'):
        api.ask('x' * 20000, questions())
    assert len(calls) == 1


@pytest.mark.parametrize('state,qs', [
    (True, questions()), ({'bad': float('nan')}, questions()), ('synthetic', {}),
    ('synthetic', {'x': {'type': 'noul'}}),
    ('synthetic', {'x': ChoiceQuestion('?', {'only': 'one'})}),
    ('synthetic', {'x': NoulQuestion('?', {'invalid': 'bad'})}),
])
def test_invalid_inputs_fail_before_transport(state, qs):
    api, calls = client()
    with pytest.raises(DecisionError, match='decision_request_invalid'):
        api.ask(state, qs)
    assert not calls


def test_default_transport_disables_environment_proxy_and_redirects(monkeypatch):
    observations = {}
    class FakeHTTPClient:
        def __init__(self, **kwargs):
            observations['config'] = kwargs
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, url, **kwargs):
            observations['url'] = url
            return Response(payload())
    monkeypatch.setattr(httpx, 'Client', FakeHTTPClient)
    DecisionClient(profile()).ask('synthetic', questions())
    assert observations['config'] == {'follow_redirects': False, 'trust_env': False}


def test_empty_json_state_is_valid_and_recursive_state_is_rejected():
    api, calls = client()
    for state in ('', {}, []):
        api.ask(state, questions())
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(DecisionError, match='decision_request_invalid'):
        api.ask(cyclic, questions())
    assert len(calls) == 3


def test_reported_total_usage_above_budget_is_not_success():
    data = payload()
    data['usage']['input_tokens'] = 16385
    api, _ = client(data=data)
    with pytest.raises(DecisionError, match='decision_budget_exceeded'):
        api.ask('synthetic', questions())


def test_explicit_health_probe_checks_served_name_not_post_echo():
    api, calls = client()
    get_calls = []
    def get(url, **kwargs):
        get_calls.append((url, kwargs))
        return Response({'status': 'ok', 'model': 'wrong-loaded-model'})
    api.get = get
    with pytest.raises(DecisionError, match='decision_model_mismatch'):
        api.verify_local_model()
    assert not calls and get_calls[0][0] == 'http://127.0.0.1:8197/health'
    assert get_calls[0][1]['follow_redirects'] is False and get_calls[0][1]['trust_env'] is False
    # Demonstrate why the explicit GET is needed: upstream can still return
    # a completely valid response echoing the request model.
    assert api.ask('synthetic', questions()).model == 'clef-4bit'


@pytest.mark.parametrize('data', [{'status': 'ok'}, {'status': 'failed', 'model': 'clef-4bit'},
                                 [], ValueError('private error')])
def test_malformed_health_probe_has_safe_response_error(data):
    api, calls = client()
    api.get = lambda *args, **kwargs: Response(data)
    with pytest.raises(DecisionError, match='decision_response_invalid'):
        api.verify_local_model()
    assert not calls


def test_direct_httpx_post_injection_cannot_inherit_environment_proxy(monkeypatch):
    observations = []
    request_options = []
    monkeypatch.setenv('HTTP_PROXY', 'http://synthetic-proxy.invalid:9199')
    monkeypatch.setenv('HTTPS_PROXY', 'http://synthetic-proxy.invalid:9199')
    original_client = httpx.Client
    original_request = original_client.request
    def observed_request(self, *args, **kwargs):
        request_options.append(kwargs)
        return original_request(self, *args, **kwargs)
    def fake_http_client(*args, **kwargs):
        # This factory sees what the real httpx.post helper passes to Client;
        # MockTransport prevents all network, including any environment proxy.
        observations.append(kwargs)
        kwargs['transport'] = httpx.MockTransport(lambda request: httpx.Response(200, json=payload()))
        return original_client(*args, **kwargs)
    # httpx's public helper closes over its own module-level Client alias.
    monkeypatch.setitem(httpx.request.__globals__, 'Client', fake_http_client)
    monkeypatch.setattr(original_client, 'request', observed_request)
    DecisionClient(profile(), post=httpx.post).ask('synthetic', questions())
    assert len(observations) == 1
    assert observations[0]['trust_env'] is False
    assert request_options[0]['follow_redirects'] is False
