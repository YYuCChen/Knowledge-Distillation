"""Approved U06 routes and real business APIs; temporary profiles and fake HTTP only."""
from pathlib import Path
import re

import pytest
from flask import Flask

from tests.v1.test_settings_decision_profiles import backend, fields
from tests.v1.test_settings_web import Settings
from knowledge_distiller.v1.settings_web import settings_blueprint
from knowledge_distiller.v1.jev import JevError
from knowledge_distiller.v1.captures import JevIdentityJudge
from knowledge_distiller.v1.capture_identity_context import CaptureSource
from knowledge_distiller.v1.jev_recall import JevRecallRuntime
from knowledge_distiller.growth_modeling import GrowthRuntimeFailed


@pytest.fixture
def browser(backend):
    service, memory, http = backend
    view = Settings().view()
    view['vault']['path'] = ''
    service.view = lambda: view
    root = Path(__file__).resolve().parents[2] / 'src/knowledge_distiller/v1'
    app = Flask('synthetic-decision', template_folder=str(root / 'templates'), static_folder=str(root / 'static'))
    app.config['TESTING'] = True
    app.register_blueprint(settings_blueprint(service))
    # Only inert URL targets required by the unchanged full settings template.
    for endpoint in ('topics.index', 'home', 'insights.index', 'mutate_wiki_kit', 'mutate_wiki_style'):
        app.add_url_rule('/synthetic/' + endpoint, endpoint, lambda: '')
    app.context_processor(lambda: dict(wiki_kit_status=dict(dot='unconfigured', text='', action=None, stopped_label=''),
        wiki_style_status=dict(dot='unconfigured', text='', action=None, stopped_label=''),
        update=dict(attention=False, state='idle', display_version='0-synthetic', configured=False, releases=[])))
    return app.test_client(), service, memory, http


def form(client):
    html = client.get('/settings?open=models&message=jev_decision_check').get_data(as_text=True)
    fields = {k: re.search(r'name="' + k + r'" value="([^"]*)"', html).group(1)
              for k in ('return_to', 'candidate_id', 'candidate_revision', 'form_nonce')}
    fields.update(provider='clef', model='synthetic-clef', endpoint='http://127.0.0.1:8198/v1/systemone', api_key='')
    return fields, html


def post(client, action, data, **kwargs):
    return client.post('/settings/decision/' + action, data=data,
                       headers=kwargs.pop('headers', {'Origin': 'http://localhost'}), **kwargs)


def test_approved_form_and_explicit_activation_reaches_business_apis(browser):
    client, service, _, http = browser
    data, html = form(client)
    assert '决策模型' in html and 'decision-actions' in html
    assert 'name="timeout_seconds"' not in html and 'name="token_budget"' not in html
    assert html.index('id="decision-check"') < html.index('id="decision-enable"')
    assert post(client, 'check', data).status_code == 303
    assert service.decision_client() is None
    data, html = form(client)
    assert '检查通过，尚未启用。' in html and data['candidate_id']
    assert post(client, 'enable', data).status_code == 303
    assert '方案已启用。' in form(client)[1]
    assert service.decision_client()[1].profile.timeout_seconds == 20
    assert service.decision_client()[1].profile.token_budget == 16384

    # Exercise actual identity and historical-recall consumers, not just a profile read.
    calls = []
    def business_post(url, **kwargs):
        calls.append((url, kwargs['json']))
        body = kwargs['json']
        answers = {}
        for qid, question in body['questions'].items():
            if question['type'] == 'choice':
                options = question['criteria']
                choice = 'self' if qid == 'author_identity' else 'independent'
                probabilities = {k: .92 if k == choice else .08 / (len(options) - 1) for k in options}
                answers[qid] = dict(type='choice', choice=choice, probabilities=probabilities, confidence=0.92)
            else:
                answers[qid] = dict(type='noul', noul=0.7)
        from types import SimpleNamespace
        return SimpleNamespace(status_code=200, json=lambda: dict(model=body['model'], answers=answers,
            usage=dict(input_tokens=100, output_tokens=3)))
    service._decision_post = business_post
    judge = JevIdentityJudge(service.jev_client())
    import hashlib
    original = '合成短消息'.encode('utf-8')
    digest = hashlib.sha256(original).hexdigest()
    source = CaptureSource('synthetic-app', 'synthetic-message', 'capture-1', digest, original, digest)
    assert judge.judge(source) is None  # Clef does not inherit Jev automatic acceptance.
    # Transport integration only. Clef auto-acceptance/threshold policy belongs
    # to the captures owner, and must not inherit a Jev calibration assertion.
    assert judge.last.choice == 'self' and judge.last.probability == 0.92
    assert judge.answers['relation_target'].choice == 'independent'
    assert calls[0][1]['state']['message'] == source.text()
    assert set(calls[0][1]['questions']) == {'author_identity', 'relation_target'}
    assert judge.last.provider == 'clef' and judge.last.confidence_semantics == 'clef-max-probability'
    payload = dict(codec='historical-recall-input-v1', frozen_new=[{'text': '合成新素材'}],
        eligible_history=[{'knowledge_result_id': 3}], accepted_current=[], relation_current=[], reconsideration_hints=[])
    recall = JevRecallRuntime(service.jev_client()).complete(system_prompt='', input_payload=payload, max_tokens=100)
    import json
    assert json.loads(recall.text)['source_knowledge_ids'] == [3]
    assert len(calls) == 2 and all(url.startswith('http://127.0.0.1:8198/') for url, _ in calls)
    assert all(body['truncate'] is False for _, body in calls)


def test_edit_failure_extra_defaults_and_stale_receipt_never_switch(browser):
    client, service, _, http = browser
    data, _ = form(client)
    post(client, 'check', data)
    checked, _ = form(client)
    post(client, 'enable', checked | {'model': 'edited'})
    assert service.decision_client() is None
    post(client, 'check', data)
    checked, _ = form(client)
    other = service.save_decision_draft(fields())
    service.check_decision_draft(other)
    service.activate_decision_profile(other, expected_active_id=None)
    post(client, 'enable', checked)
    assert service.decision_state()['active']['profile_id'] == other
    post(client, 'check', data | {'timeout_seconds': '120'})
    assert not form(client)[0]['candidate_id']
    http.status = 503
    post(client, 'check', data)
    assert '检查未通过；原方案保留。' in form(client)[1]
    assert service.decision_state()['active']['profile_id'] == other


def test_origin_nonce_cross_browser_and_key_not_echoed(browser):
    client, service, memory, http = browser
    data, _ = form(client)
    for headers in ({}, {'Origin': 'null'}, {'Origin': 'http://evil.invalid'}):
        assert post(client, 'check', data, headers=headers).status_code == 403
    assert post(client, 'check', data | {'form_nonce': 'bad'}).status_code == 403
    other = client.application.test_client()
    assert post(other, 'check', data).status_code == 403
    assert http.events == memory.events == []
    cloud = data | dict(provider='jev', model='jev-latest', endpoint='https://api.typesafe.ai/v1/systemone', api_key='SYNTHETIC-only-key')
    post(client, 'check', cloud)
    html = form(client)[1]
    assert 'SYNTHETIC-only-key' not in html
    assert 'SYNTHETIC-only-key' not in repr(client.application.view_functions)
    assert service.decision_client() is None


def test_real_transports_are_supplied_only_on_explicit_check(browser, monkeypatch):
    client, service, _, http = browser
    service._decision_post = service._decision_get = None
    import httpx
    monkeypatch.setattr(httpx, 'post', http.post)
    monkeypatch.setattr(httpx, 'get', http.get)
    data, _ = form(client)
    assert http.events == []
    post(client, 'check', data)
    assert [event[0] for event in http.events] == ['get', 'post']
    assert service._decision_post is service._decision_get is None


@pytest.mark.parametrize('provider', ['jev', 'clef'])
def test_adapter_preserves_provider_semantics_and_never_falls_back(backend, provider):
    service, memory, http = backend
    identity = service.save_decision_draft(fields(provider), api_key='SYNTHETIC-only' if provider == 'jev' else None)
    service.check_decision_draft(identity)
    service.activate_decision_profile(identity, expected_active_id=None)
    answer = service.jev_client().choose('合成', instructions='Choose match', options={'match': 'yes', 'other': 'no'})
    assert answer.provider == provider and answer.probability == 0.9
    assert answer.confidence == (0.9 if provider == 'clef' else 0.8)
    assert answer.confidence_semantics == ('clef-max-probability' if provider == 'clef' else 'jev-normalized-concentration')
    http.status = 503
    count = len(http.events)
    with pytest.raises(JevError, match='decision_request_failed'):
        service.jev_client().choose('合成', instructions='Choose match', options={'match': 'yes', 'other': 'no'})
    assert len(http.events) == count + 1
    assert service.decision_state()['active']['profile_id'] == identity


def test_corrupt_active_factory_error_is_reported_inside_business_boundary(backend):
    service, _, http = backend
    service.decision_state()
    path = Path(service._decision_root) / 'decision-profiles.json'
    path.write_text('{broken'); path.chmod(0o600)
    with pytest.raises(JevError, match='decision_store_invalid'):
        service.jev_client().choose('合成', instructions='Choose', options={'a': 'A', 'b': 'B'})
    assert http.events == []
