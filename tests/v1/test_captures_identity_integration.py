"""R16: temporary SQLite and synthetic text; injected HTTP only.

No UI, real Vault, profile store, credentials, native models or pipeline runs.
Select these tests by exact node; do not expand to unrelated document tests.
"""
import json
from types import SimpleNamespace as NS

import pytest

from knowledge_distiller.v1.captures import Captures, JevIdentityJudge, rule_judgment
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.decision_client import DecisionClient, DecisionProfile, JEV_ENDPOINT
from knowledge_distiller.v1.jev import ActiveDecisionAdapter, JevClient
from knowledge_distiller.v1.store import Store


@pytest.fixture
def capture_world(tmp_path):
    store = Store(tmp_path / 'isolated-data' / 'capture.sqlite3')
    store.initialize()
    with connect(store.path) as db:
        db.execute('INSERT INTO feishu_binding VALUES(?,?,?,?,?,?)',
                   ('synthetic-app', 'bot', 'owner', 'private', 0, 0))
        cid = db.execute('''INSERT INTO captures(app_id,message_id,message_type,created_ms,received_ms,text,raw_id)
            VALUES(?,?,?,?,?,?,?)''', ('synthetic-app', 'note', 'text', 2000, 2000,
                '这篇的附言\r\n' + '# 我自己的判断 🙂\r\n' * 60, 'R-20261008-0001')).lastrowid
        db.execute('INSERT INTO capture_state(capture_id) VALUES(?)', (cid,))
    captures = Captures(store)
    return NS(store=store, captures=captures, capture=captures.get(cid), cid=cid)


def delivery(world, message='delivery', position=0, title='真实已有标题', summary=None, gap_seconds=1):
    item = world.store.create_item(f'https://example.invalid/{message}/{position}', title=title)
    with connect(world.store.path) as db:
        db.execute('''INSERT OR IGNORE INTO feishu_receipts(app_id,message_id,created_ms,raw_json,text,
            same_topic,content_kind,state) VALUES(?,?,?,?,?,?,?,?)''',
            ('synthetic-app', message, 1000, '{}', 'synthetic delivery', 0, 'links', 'accepted'))
        db.execute('INSERT INTO feishu_parts(app_id,message_id,position,item_id) VALUES(?,?,?,?)',
                   ('synthetic-app', message, position, item))
        db.execute('INSERT OR IGNORE INTO delivery_adjacency VALUES(?,?,?,?)',
                   ('synthetic-app', 'note', message, gap_seconds))
        if summary is not None:
            material = db.execute('''INSERT INTO materials(source_kind,source_key,submitted_url,canonical_url,
                metadata_json,created_at) VALUES(?,?,?,?,?,?)''',
                ('synthetic', str(item), 'synthetic', 'synthetic', '{}', '2026-10-08')).lastrowid
            db.execute('UPDATE distill_items SET material_id=? WHERE item_id=?', (material, item))
            fact = db.execute('''INSERT INTO source_facts(material_id,snapshot,uncertainties_json,created_at)
                VALUES(?,?,?,?)''', (material, 'synthetic original', '[]', '2026-10-08')).lastrowid
            db.execute('INSERT INTO knowledge_results(source_fact_id,payload_json,created_at) VALUES(?,?,?)',
                (fact, json.dumps({'title': title, 'summary': summary}), '2026-10-08'))
    return item


def transport(provider='jev', author='self', relation='independent', *, hook=None, confidence=.37,
              author_probability=1.0, relation_probability=1.0):
    calls = []
    def post(url, **kwargs):
        body = kwargs['json']
        calls.append(body)
        if hook:
            hook()
        selected = {'author_identity': author, 'relation_target': relation}
        answers = {}
        for key, question in body['questions'].items():
            choice = selected[key]
            if choice == 'target':
                choice = body['state']['targets'][0]['id']
            probability = author_probability if key == 'author_identity' else relation_probability
            probabilities = {name: probability if name == choice else
                             (1 - probability) / (len(question['criteria']) - 1)
                             for name in question['criteria']}
            answers[key] = {'type': 'choice', 'choice': choice, 'probabilities': probabilities,
                'confidence': max(probabilities.values()) if provider == 'clef' else confidence}
        return NS(status_code=200, json=lambda: {'model': 'synthetic-model', 'answers': answers,
            'usage': {'input_tokens': 1, 'output_tokens': 1}})
    if provider == 'legacy':
        return JevClient(secret=lambda: 'synthetic-key', model='synthetic-model', post=post), calls
    profile = DecisionProfile(provider,
        JEV_ENDPOINT if provider == 'jev' else 'http://127.0.0.1:8198/v1/systemone',
        'synthetic-model', auth_ref='synthetic-key' if provider == 'jev' else None)
    return ActiveDecisionAdapter(DecisionClient(profile, post=post, secret=lambda ref: 'synthetic-key'),
                                 'synthetic-profile'), calls


def test_long_formatted_text_and_long_annotation_do_not_trigger_length_rules(capture_world):
    w = capture_world
    original = w.capture['text']
    assert len(original) > 300
    assert rule_judgment(w.capture, recent_delivery='delivery') is None
    assert w.captures.judge(w.capture)['result'] == 'pending'
    assert w.captures.get(w.cid)['text'] == original
    assert w.captures.get(w.cid)['item_id'] is None


@pytest.mark.parametrize('provider', ['legacy', 'jev', 'clef'])
def test_real_captures_judge_uses_active_or_legacy_ask_with_bounded_actual_context(capture_world, provider):
    w = capture_world
    item = delivery(w, summary='已有摘要\r\n' + '内容' * 600)
    client, calls = transport(provider, relation='target')
    w.captures.jev = lambda: client
    event = w.captures.judge(w.capture)
    assert event['result'] == ('pending' if provider == 'clef' else 'annotation')
    assert event['target_message_id'] == (None if provider == 'clef' else 'delivery')
    assert len(calls) == 1
    state = calls[0]['state']
    assert state['message'] == w.capture['text']
    assert set(calls[0]['questions']) == {'author_identity', 'relation_target'}
    target, = state['targets']
    assert target['message_id'] == 'delivery' and target['part_id'] == '0' and target['item_id'] == item
    assert target['title'] == '真实已有标题' and target['summary'] == ('已有摘要\r\n' + '内容' * 600)[:1024]
    assert target['summary_provenance'].endswith(':summary:prefix1024')
    assert w.captures.identity_context(w.capture)[1][0].candidate_id == target['id']
    assert w.captures.judge(w.capture) == event and len(calls) == 1


@pytest.mark.parametrize('provider,semantics,confidence', [
    ('jev', 'jev-normalized-concentration', .37), ('clef', 'clef-max-probability', 1.0)])
def test_author_and_relation_answers_preserve_provider_probability_semantics(capture_world, provider, semantics, confidence):
    w = capture_world
    delivery(w)
    client, calls = transport(provider, author='third_party', relation='target')
    source, targets, complete = w.captures.identity_context(w.capture)
    judge = JevIdentityJudge(client)
    assert judge.judge(source, targets=targets, scope_complete=complete) is None
    assert judge.answers['author_identity'].choice == 'third_party'
    assert judge.answers['relation_target'].choice == calls[0]['state']['targets'][0]['id']
    assert judge.last.provider == provider and judge.last.confidence_semantics == semantics
    assert judge.last.probability == 1.0 and judge.last.confidence == confidence


def test_multiple_messages_and_multipart_candidates_are_distinct_and_never_pick_latest(capture_world, monkeypatch):
    w = capture_world
    delivery(w, 'one', 0, gap_seconds=50)
    delivery(w, 'one', 1, gap_seconds=50)
    delivery(w, 'two', 0)
    source, targets, complete = w.captures.identity_context(w.capture)
    assert complete and len(targets) == len({t.candidate_id for t in targets}) == 3
    assert len({t.item_id for t in targets}) == 3
    assert w.captures.recent_delivery(w.capture) == 'two'  # Existing UI default survives.
    client, calls = transport(author='self', relation='target')
    w.captures.jev = lambda: client
    assert w.captures.judge(w.capture)['result'] == 'pending'
    assert len(calls) == 1
    monkeypatch.setattr(w.captures, 'advance', lambda capture: None)
    w.captures.decide(w.cid, 'annotation', target='one')
    assert w.captures.identity(w.cid)['target_message_id'] == 'one'  # Explicit user target wins.


def test_candidate_scope_limit_and_missing_summary_are_explicit(capture_world):
    w = capture_world
    for position in range(10):
        delivery(w, position=position)
    source, targets, complete = w.captures.identity_context(w.capture)
    assert len(targets) == 8 and not complete
    assert all(t.summary is None and t.summary_provenance is None for t in targets)
    client, calls = transport()
    w.captures.jev = lambda: client
    assert w.captures.judge(w.capture)['result'] == 'pending'
    assert calls[0]['state']['scope']['complete'] is False
    assert calls[0]['state']['scope']['omitted_count'] >= 1


@pytest.mark.parametrize('result,basis', [('my_thought', '用户'), ('pending', 'old unknown')])
def test_existing_user_or_unknown_event_is_never_rejudged(capture_world, result, basis):
    w = capture_world
    w.captures._event(w.cid, result, basis)
    def forbidden():
        pytest.fail('existing events must not call the client factory')
    w.captures.jev = forbidden
    assert w.captures.judge(w.capture)['result'] == result
    assert len(w.captures.events(w.cid)) == 1


def test_user_decide_during_model_request_wins_without_late_model_event(capture_world, monkeypatch):
    w = capture_world
    monkeypatch.setattr(w.captures, 'advance', lambda capture: None)
    client, calls = transport(author='third_party', hook=lambda: w.captures.decide(w.cid, 'my_thought'))
    w.captures.jev = lambda: client
    event = w.captures.judge(w.capture)
    assert len(calls) == 1 and event['result'] == 'my_thought' and event['basis'] == '用户'
    assert len(w.captures.events(w.cid)) == 1


@pytest.mark.parametrize('provider', ['jev', 'clef'])
def test_settings_active_client_factory_reaches_real_captures_judge(capture_world, monkeypatch, provider):
    from knowledge_distiller.v1.settings import SettingsService
    w = capture_world
    adapter, calls = transport(provider)
    service = SettingsService(w.store, chrome=NS(), decision_profiles_factory=lambda: None)
    # Replace only profile selection, not jev_client, adapter.ask or DecisionClient.ask.
    monkeypatch.setattr(service, 'decision_client', lambda: ('synthetic-profile', adapter.client))
    w.captures.jev = service.jev_client
    assert w.captures.judge(w.capture)['result'] == ('my_thought' if provider == 'jev' else 'pending')
    assert len(calls) == 1 and calls[0]['state']['source']['app_id'] == 'synthetic-app'


def test_unavailable_active_client_factory_records_pending_without_transport(capture_world, monkeypatch):
    from knowledge_distiller.v1.settings import SettingsService, SettingsError
    w = capture_world
    service = SettingsService(w.store, chrome=NS(), decision_profiles_factory=lambda: None)
    def unavailable():
        raise SettingsError('decision_store_invalid')
    monkeypatch.setattr(service, 'decision_client', unavailable)
    w.captures.jev = service.jev_client
    event = w.captures.judge(w.capture)
    assert event['result'] == 'pending' and event['basis'] == 'Jev 失败·decision_store_invalid'


def test_user_annotation_target_correction_uses_existing_supersede_path(capture_world, monkeypatch):
    w = capture_world
    delivery(w, 'one')
    delivery(w, 'two')
    monkeypatch.setattr(w.captures, 'advance', lambda capture: None)
    w.captures.decide(w.cid, 'annotation', target='one')
    monkeypatch.setattr(w.captures, '_written', lambda cid: {'identity': '本人附言', 'raw_id': 'synthetic-written'})
    supersedes = []
    monkeypatch.setattr(w.captures, '_supersede', lambda *args: supersedes.append(args))
    w.captures.decide(w.cid, 'annotation', target='two')
    events = w.captures.events(w.cid)
    assert [e['target_message_id'] for e in events] == ['one', 'two']
    assert all(e['basis'] == '用户' for e in events) and len(supersedes) == 1
    assert w.captures.get(w.cid)['text'] == w.capture['text']


@pytest.mark.parametrize('failure', ['invalid', 'timeout'])
def test_invalid_response_and_transport_failure_remain_pending(capture_world, failure):
    w = capture_world
    client, calls = transport(author='not-an-option')
    if failure == 'timeout':
        import httpx
        def fail(*args, **kwargs):
            calls.append('timeout')
            raise httpx.TimeoutException('synthetic')
        client.client.post = fail
    w.captures.jev = lambda: client
    assert w.captures.judge(w.capture)['result'] == 'pending'
    assert w.captures.identity(w.cid)['basis'].startswith('Jev 失败·')
    assert len(w.captures.events(w.cid)) == 1 and w.captures.get(w.cid)['item_id'] is None


@pytest.mark.parametrize('author,relation,author_p,relation_p,expected', [
    ('self', 'independent', .9, .95, 'my_thought'),
    ('self', 'independent', .899, .95, 'pending'),
    ('third_party', 'independent', .8, .95, 'third_party'),
    ('third_party', 'independent', .799, .95, 'pending'),
    ('self', 'target', .8, .8, 'annotation'),
    ('self', 'target', .799, .95, 'pending'),
    ('self', 'target', .95, .799, 'pending'),
    ('self', 'unknown', 1., 1., 'pending'),
    ('unknown', 'independent', 1., 1., 'pending'),
    ('mixed', 'independent', 1., 1., 'pending'),
    ('self', 'independent', .95, .7, 'pending'),
])
def test_jev_consumes_two_dimensions_at_approved_probability_boundaries(capture_world, author, relation,
                                                                       author_p, relation_p, expected):
    w = capture_world
    delivery(w)
    adapter, calls = transport(author=author, relation=relation, author_probability=author_p,
                               relation_probability=relation_p, confidence=.01)
    w.captures.jev = lambda: adapter
    event = w.captures.judge(w.capture)
    assert event['result'] == expected  # Selected probability, not normalized confidence.
    assert event['target_message_id'] == ('delivery' if expected == 'annotation' else None)
    assert len(calls) == 1 and w.captures.get(w.cid)['text'] == w.capture['text']


def test_changed_target_version_before_commit_cannot_accept_model_nomination(capture_world):
    w = capture_world
    item = delivery(w)
    def change():
        with connect(w.store.path) as db:
            db.execute('UPDATE distill_items SET submitted_title=? WHERE item_id=?', ('已更新的真实标题', item))
    adapter, calls = transport(relation='target', hook=change)
    w.captures.jev = lambda: adapter
    event = w.captures.judge(w.capture)
    assert len(calls) == 1 and event['result'] == 'pending' and event['target_message_id'] is None
    current = w.captures.identity_context(w.capture)[1][0]
    assert current.candidate_id != calls[0]['state']['targets'][0]['id']
