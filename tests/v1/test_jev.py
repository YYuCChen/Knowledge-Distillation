"""TypeSafe Jev client and the quick-note identity judge on it (synthetic transport only)."""
import httpx
import hashlib
import pytest

from knowledge_distiller.v1.captures import JevIdentityJudge
from knowledge_distiller.v1.capture_identity_context import CaptureSource, TargetCandidate
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


def identity_source(text):
    data = text.encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    return CaptureSource('synthetic-app', 'synthetic-message', 'capture-1', digest, data, digest)


def identity_answer(author, relation, author_probability=.95, relation_probability=.95, *, target=None):
    answers = {}
    for key, choice, probability, options in (
        ('author_identity', author, author_probability, ('self', 'third_party', 'mixed', 'unknown')),
        ('relation_target', relation, relation_probability,
         ('independent', 'unknown') + ((target.candidate_id,) if target else ())),
    ):
        answers[key] = {'type': 'choice', 'choice': choice,
            'probabilities': {option: probability if option == choice else
                             (1 - probability) / (len(options) - 1) for option in options},
            'confidence': .99}
    return Response(200, {'model': 'jev-1.13.0', 'answers': answers})


def test_request_follows_the_documented_shape():
    jev, calls = client(identity_answer('self', 'independent'))
    source = identity_source('我觉得周会可以隔周开')
    decided = JevIdentityJudge(jev).judge(source)
    url, kwargs = calls[0]
    assert url == ENDPOINT == 'https://api.typesafe.ai/v1/systemone'
    assert kwargs['headers']['Authorization'] == 'Bearer test-key'
    body = kwargs['json']
    assert body['model'] == 'jev-latest' and body['state']['message'] == source.text()
    assert body['state']['source']['sha256'] == source.sha256 and body['state']['targets'] == []
    assert set(body['questions']) == {'author_identity', 'relation_target'}
    question = body['questions']['author_identity']
    assert question['type'] == 'choice' and set(question['criteria']) == {'self', 'third_party', 'mixed', 'unknown'}
    assert set(body['questions']['relation_target']['criteria']) == {'independent', 'unknown'}
    assert decided == ('my_thought', 'Jev·jev-1.13.0', 0.95, None)


@pytest.mark.parametrize('choice,probability,target,expected', [
    ('my_thought', 0.89, None, None),                 # "Mine" needs the highest bar.
    ('third_party', 0.8, None, 'third_party'),
    ('third_party', 0.79, None, None),
    ('annotation', 0.85, 'om_link', 'annotation'),
    ('unknown', 0.99, 'om_link', None),               # Undecided never becomes anyone's words.
])
def test_thresholds_use_the_calibrated_probability(choice, probability, target, expected):
    candidate = (TargetCandidate('synthetic-app', target, 'part-1', 'target-v1',
                                 'synthetic:receipt', item_id=1) if target else None)
    author = {'my_thought': 'self', 'annotation': 'self', 'third_party': 'third_party'}.get(choice, 'unknown')
    relation = candidate.candidate_id if choice == 'annotation' else 'independent'
    jev, _ = client(identity_answer(author, relation, probability,
                                    probability if choice == 'annotation' else .95, target=candidate))
    decided = JevIdentityJudge(jev).judge(identity_source('这篇重点看后半段'),
                                         targets=(candidate,) if candidate else ())
    assert (decided[0] if decided else None) == expected
    if expected == 'annotation':
        assert decided[3] == 'om_link'


def test_annotation_is_offered_only_right_after_a_delivery():
    unoffered = TargetCandidate('synthetic-app', 'other-message', 'part-1', 'target-v1',
                               'synthetic:receipt', item_id=1)
    jev, calls = client(identity_answer('self', unoffered.candidate_id, target=unoffered))
    with pytest.raises(JevError, match='decision_response_invalid'):  # Not a stable offered target.
        JevIdentityJudge(jev).judge(identity_source('重点看后半段'))
    assert set(calls[0][1]['json']['questions']['relation_target']['criteria']) == {'independent', 'unknown'}


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


# ───────────────────────── Historical recall on Jev ─────────────────────────

def recall_payload():
    return {'codec': 'historical-recall-input-v1', 'event_id': 1,
            'frozen_new': [{'knowledge_result_id': 9, 'title': '新材料', 'summary': '反馈周期', 'points': []}],
            'eligible_history': [{'knowledge_result_id': 3, 'title': '旧材料甲', 'summary': '', 'points': []},
                                 {'knowledge_result_id': 4, 'title': '旧材料乙', 'summary': '', 'points': []}],
            'accepted_current': [{'insight_version_id': 7, 'claim': '已接受的新知'}],
            'relation_current': [{'relation_version_id': 5, 'payload': {}}],
            'reconsideration_hints': [{'relation_version_id': 5, 'payload': {}}]}


class RecallJev:
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers or {}, error, []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise JevError(self.error)
        return self.answers, 'jev-1.13.0'


def test_recall_asks_one_yes_no_question_per_candidate_in_one_request():
    import json
    from knowledge_distiller.v1.jev_recall import JevRecallRuntime
    jev = RecallJev({'s3': {'type': 'noul', 'noul': 0.82}, 's4': {'type': 'noul', 'noul': 0.12},
                     'i7': {'type': 'noul', 'noul': 0.5}, 'r5': {'type': 'noul', 'noul': 0.49},
                     'h5': {'type': 'noul', 'noul': 0.91}})
    result = JevRecallRuntime(jev).complete(system_prompt='ignored', input_payload=recall_payload(), max_tokens=10)
    (state, questions), = jev.calls
    assert set(questions) == {'s3', 's4', 'i7', 'r5', 'h5'}
    assert all(q['type'] == 'noul' and q['instructions'].startswith(f'state.history["{key}"]')
               for key, q in questions.items())
    assert state['new_materials'][0]['knowledge_result_id'] == 9 and set(state['history']) == set(questions)
    assert result.stop_reason == 'end_turn'
    assert json.loads(result.text) == {'codec': 'historical-recall-v1', 'source_knowledge_ids': [3],
                                       'accepted_insight_version_ids': [7], 'current_relation_version_ids': [],
                                       'reconsideration_hint_version_ids': [5]}


def test_recall_output_passes_the_existing_parser():
    from knowledge_distiller.growth_modeling import HistoricalRecallAdapter
    from knowledge_distiller.v1.jev_recall import JevRecallRuntime
    from types import SimpleNamespace
    boundary = SimpleNamespace(
        event_id=1, frozen_new=(), current_relations=(), reconsideration_hints=(),
        eligible_history=(SimpleNamespace(knowledge_result_id=3, title='甲', summary='', points=()),),
        accepted_current=())
    jev = RecallJev({'s3': {'type': 'noul', 'noul': 0.9}})
    planning = HistoricalRecallAdapter(JevRecallRuntime(jev)).recall(boundary)
    assert planning.selection is not None and planning.selection.source_knowledge_ids == (3,)


def test_recall_without_candidates_asks_nothing_and_failures_fail_the_step():
    import json
    from knowledge_distiller.growth_modeling import GrowthRuntimeFailed
    from knowledge_distiller.v1.jev_recall import JevRecallRuntime
    empty = {**recall_payload(), 'eligible_history': [], 'accepted_current': [], 'relation_current': [],
             'reconsideration_hints': []}
    jev = RecallJev()
    assert json.loads(JevRecallRuntime(jev).complete(system_prompt='', input_payload=empty, max_tokens=1).text)[
        'source_knowledge_ids'] == [] and jev.calls == []
    for broken in (RecallJev(error='jev_busy'), RecallJev({'s3': {'noul': 'yes'}})):
        with pytest.raises(GrowthRuntimeFailed):  # Reported as a failed step, never an LLM or guessed fallback.
            JevRecallRuntime(broken).complete(system_prompt='', input_payload=recall_payload(), max_tokens=1)


def test_organization_uses_jev_for_recall_only_when_configured(monkeypatch):
    import knowledge_distiller.v1.organization as module
    from knowledge_distiller.v1.jev_recall import JevRecallRuntime
    from knowledge_distiller.v1.llm import OpenAIResponsesClient
    captured = {}
    monkeypatch.setattr(module, 'build_organization', lambda store, **kwargs: captured.update(kwargs))
    llm = OpenAIResponsesClient('https://api.example.com/v1', 'model', lambda: 'k')
    module.configured_organization(None, llm, jev=RecallJev())
    assert isinstance(captured['recall_planner'].binding, JevRecallRuntime)
    assert not isinstance(captured['relation_insight_planner'].binding, JevRecallRuntime)  # Writing stays on the LLM.
    module.configured_organization(None, llm)
    assert not isinstance(captured['recall_planner'].binding, JevRecallRuntime)


# ───────────────────────── Settings ─────────────────────────

@pytest.fixture
def settings(tmp_path):
    from knowledge_distiller.v1.settings import SettingsService
    from knowledge_distiller.v1.store import Store
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    probes = []

    def probe(secret):
        probes.append(secret)
        if secret == 'rejected':
            raise JevError('jev_unauthorized')
        if secret == 'offline':
            raise JevError('jev_request_failed')
    service = SettingsService(store, qwen_probe=lambda: True, jev_probe=probe)
    return service, probes


def test_key_is_checked_before_it_is_saved(settings):
    from knowledge_distiller.v1.settings import SettingsError
    service, probes = settings
    assert service.jev_state() == 'unconfigured' and service.jev_client() is None
    for secret, code in (('rejected', 'jev_key_invalid'), ('offline', 'jev_unreachable'), ('  ', 'jev_key_invalid')):
        with pytest.raises(SettingsError, match=code):
            service.save_jev_key(secret)
        assert service.jev_state() == 'unconfigured'
    service.save_jev_key(' good-key ')
    assert probes[-1] == 'good-key' and service.jev_state() == 'configured'
    assert service.jev_client() is not None and service.jev_client().secret() == 'good-key'


def test_a_rejected_key_at_run_time_marks_jev_unavailable(settings):
    service, _ = settings
    service.save_jev_key('good-key')
    client = service.jev_client()
    client.post = lambda url, **kwargs: Response(401, {'error': 'invalid key'})
    with pytest.raises(JevError, match='jev_unauthorized'):
        client.choose('x', instructions='?', options={'a': 'A'})
    assert service.jev_state() == 'unavailable' and service.jev_client() is not None  # Still used: errors stay visible.
    service.save_jev_key('new-key')
    assert service.jev_state() == 'configured'


def test_settings_page_has_approved_decision_actions_on_one_line(settings, tmp_path):
    from pathlib import Path
    import threading
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    from knowledge_distiller.v1.web import create_app
    service, _ = settings
    app = create_app(service.store, object(), service)
    page_html = app.test_client().get('/settings?open=models').get_data(as_text=True)
    assert '决策模型' in page_html and 'Jev · 云端' in page_html and 'jev-latest' in page_html and '未配置' in page_html
    assert '随手记云端判断' not in page_html  # The old toggle is gone (user decision 2026-09-30).
    response = app.test_client().post('/settings/jev', data={'api_key': 'good-key'})
    assert response.status_code == 302 and 'jev_saved' in response.headers['Location']
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.goto(f'http://127.0.0.1:{server.server_port}/settings?open=models')
            row = page.locator('.jev-setting > summary')
            assert '已配置' in row.inner_text() and '更换模型' in row.inner_text()
            row.click()
            for width in (1280, 360):
                page.set_viewport_size({'width': width, 'height': 900})
                check = page.locator('#decision-check').bounding_box()
                enable = page.locator('#decision-enable').bounding_box()
                field = page.locator('#decision-key').bounding_box()
                assert abs((check['y'] + check['height'] / 2) - (enable['y'] + enable['height'] / 2)) < 1
                assert abs(enable['x'] + enable['width'] - field['x'] - field['width']) < 1
                assert page.locator('[name="timeout_seconds"], [name="token_budget"]').count() == 0
                assert page.locator('#decision-enable').is_disabled()
                page.locator('.jev-setting').screenshot(path=str(tmp_path / f'decision-{width}.png'))
            page.locator('#decision-provider').select_option('clef')
            assert not page.locator('#decision-key').get_attribute('required')
            assert page.locator('#decision-endpoint').input_value() == 'http://127.0.0.1:18765/v1/systemone'
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
