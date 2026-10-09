"""Synthetic pure R16 tests, authored here but not executed by this agent.

Fake HTTP is injected; no DB/Vault/profile store/keys/service/raw/UI is used.
"""
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from knowledge_distiller.v1.capture_identity_context import (
    BoundModelReply, CaptureSource, IdentityContextError, ReferenceEvidence,
    TargetCandidate, UserDeclaration, prepare_identity_context, resolve_identity_candidate,
)
from knowledge_distiller.v1.decision_client import (
    DecisionClient, DecisionError, DecisionProfile, JEV_ENDPOINT, clef_template_upper_bound,
)


def source(text='这篇重点看后半段'):
    data = text.encode('utf-8')
    return CaptureSource('synthetic-app', 'capture-message', 'capture-1', 'source-v1',
                         data, hashlib.sha256(data).hexdigest())


def target(part='part-1', version='v1', **kwargs):
    return TargetCandidate('synthetic-app', 'delivery-message', part, version,
                           f'synthetic:receipt:{part}:{version}', title='同一个标题', **kwargs)


def referenced(s, t, kind='reply_metadata', **kwargs):
    e = ReferenceEvidence(kind, s.sha256, t.candidate_id, t.version, 'synthetic:reference', **kwargs)
    return replace(t, evidence=(e,))


def profile(provider='clef', budget=16384):
    return DecisionProfile(provider,
        'http://127.0.0.1:8198/v1/systemone' if provider == 'clef' else JEV_ENDPOINT,
        'synthetic-model', auth_ref=None if provider == 'clef' else 'synthetic-secret-ref', token_budget=budget)


def declaration(s, **kwargs):
    return UserDeclaration(s.sha256, 'synthetic:user', 'synthetic:decision', **kwargs)


def prepare(s, targets=(), **kwargs):
    return prepare_identity_context(s, targets, profile=profile(), profile_version='synthetic-profile-v1', **kwargs)


def fake_reply(prepared, author='self', relation='independent'):
    request = prepared.request()
    assert request is not None
    state, questions = request
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs['json'])
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        chosen = {'author_identity': author, 'relation_target': relation}
        answers = {}
        for qid, question in kwargs['json']['questions'].items():
            probabilities = {key: float(key == chosen[qid]) for key in question['criteria']}
            answers[qid] = {'type': 'choice', 'choice': chosen[qid], 'probabilities': probabilities,
                            'confidence': 1.0 if prepared.profile.provider == 'clef' else .61}
        class Response:
            status_code = 200
            def json(self):
                return {'model': prepared.profile.model, 'answers': answers,
                        'usage': {'input_tokens': 10, 'output_tokens': 2}}
        return Response()
    client = DecisionClient(prepared.profile, post=post, secret=lambda ref: 'synthetic-fake-key')
    result = client.ask(state, questions)
    assert len(calls) == 1
    assert set(calls[0]['questions']) == {'author_identity', 'relation_target'}
    assert calls[0]['state']['message'].encode('utf-8') == prepared.source.text_utf8
    return BoundModelReply(prepared.context_sha256, prepared.source.sha256,
                          prepared.profile_version, result)


def test_long_annotation_exact_crlf_unicode_bytes_and_user_complete():
    original = '  本人附言\r\n' + '# 格式不定作者 🙂\r\n' * 120 + '\t结尾  '
    s, t = source(original), target()
    user = declaration(s, author='self', relation='target', target_id=t.candidate_id, target_version=t.version)
    p = prepare(s, (t,), user_override=user)
    assert p.source.text_utf8 == original.encode('utf-8')
    assert json.loads(p.state_json)['message'] == original
    assert p.request() is None
    result = resolve_identity_candidate(p, p.snapshot())
    assert result.author == 'self' and result.relation == 'target'
    assert result.legacy_projection == 'annotation' and not result.proposal_only
    assert not result.needs_confirmation and result.supersede is None


def test_source_hash_and_utf8_validate_before_any_request():
    s = source()
    with pytest.raises(IdentityContextError, match='source_hash_mismatch'):
        prepare(replace(s, text_utf8=s.text_utf8 + b'x'))
    bad = b'\xff'
    with pytest.raises(IdentityContextError, match='source_not_utf8'):
        prepare(replace(s, text_utf8=bad, sha256=hashlib.sha256(bad).hexdigest()))
    with pytest.raises(FrozenInstanceError):
        s.version = 'changed'


@pytest.mark.parametrize('author', ['self', 'third_party', 'mixed', 'unknown'])
def test_author_and_relation_separate_and_models_never_confirm(author):
    s = source('用户发送：“他人的引用”。\n我的说明。')
    t = referenced(s, target())
    p = prepare(s, (t,))
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, author, t.candidate_id))
    assert result.author == author and result.relation == 'target'
    assert result.target_id == t.candidate_id
    assert result.legacy_projection is None and result.needs_confirmation and result.proposal_only
    assert result.supersede is None


@pytest.mark.parametrize('kind', ['adjacency', 'topic'])
def test_recent_or_topic_nomination_cannot_become_annotation(kind):
    s = source()
    t = referenced(s, target(), kind)
    p = prepare(s, (t,))
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, 'self', t.candidate_id))
    assert result.relation == 'unknown' and result.target_id is None
    assert 'target_nomination_without_bound_reference' in result.reasons
    assert result.model_result.answers['relation_target'].choice == t.candidate_id
    assert result.legacy_projection is None


@pytest.mark.parametrize('changed', ['source', 'version', 'id'])
def test_reference_must_bind_source_target_and_version(changed):
    s = source()
    t = referenced(s, target())
    e = t.evidence[0]
    if changed == 'source':
        e = replace(e, source_sha256='0' * 64)
    elif changed == 'version':
        e = replace(e, target_version='other-version')
    else:
        e = replace(e, target_id=target('other-part').candidate_id)
    t = replace(t, evidence=(e,))
    p = prepare(s, (t,))
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, 'self', t.candidate_id))
    assert result.relation == 'unknown'


def test_literal_reference_uses_exact_character_range_not_topic_name():
    literal = 'https://example.invalid/exact-material'
    s = source('附言：' + literal + '\r\n原字')
    t = target(literal_refs=(literal,))
    t = referenced(s, t, 'literal', char_start=3, char_end=3 + len(literal))
    p = prepare(s, (t,))
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, 'self', t.candidate_id))
    assert result.relation == 'target'
    bad = replace(t, evidence=(replace(t.evidence[0], char_start=0),))
    p = prepare(s, (bad,))
    assert resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, 'self', t.candidate_id)).relation == 'unknown'
    with pytest.raises(IdentityContextError, match='literal_is_not_stable_reference'):
        prepare(s, (replace(t, literal_refs=('这篇',)),))


def test_multipart_delivery_and_same_titles_do_not_share_identity():
    targets = (target('part-1'), target('part-2'), target('part-1', 'v2'))
    p = prepare(source(), targets)
    assert len({t.candidate_id for t in p.targets}) == 3
    assert len({t.title for t in p.targets}) == 1
    assert all(json.loads(p.state_json)['targets'][n]['summary'] is None for n in range(3))


def test_valid_prior_user_decision_prevents_model_request_and_preserves_identity():
    s = source('# long formatted third-party or self cannot be inferred\n' * 40)
    prior = declaration(s, author='third_party', relation='independent', event_id='event-1')
    p = prepare(s, prior_user=prior)
    assert p.request() is None
    result = resolve_identity_candidate(p, p.snapshot())
    assert result.legacy_projection == 'third_party' and result.supersede is None
    assert not result.needs_confirmation


def test_partial_user_override_locks_only_author_and_does_not_force_independent():
    s, t = source(), target()
    p = prepare(s, (t,), user_override=declaration(s, author='self'))
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, 'third_party', 'unknown'))
    assert result.author == 'self' and result.relation == 'unknown'
    assert result.needs_confirmation and result.legacy_projection is None
    assert result.model_result.answers['author_identity'].choice == 'third_party'
    assert 'model_conflicts_with_user_author' in result.reasons


def test_partial_relation_lock_survives_model_independent_proposal():
    s, t = source(), target()
    user = declaration(s, relation='target', target_id=t.candidate_id, target_version=t.version)
    p = prepare(s, (t,), user_override=user)
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, 'unknown', 'independent'))
    assert result.relation == 'target' and result.target_id == t.candidate_id
    assert result.author == 'unknown' and result.legacy_projection is None
    assert 'model_conflicts_with_user_relation' in result.reasons


@pytest.mark.parametrize('bad', ['source', 'prior', 'target'])
def test_stale_override_pending_and_valid_prior_preserved(bad):
    s, t = source(), target()
    prior = declaration(s, author='self', relation='independent', event_id='event-1')
    override = declaration(s, author='self', relation='target', target_id=t.candidate_id,
                           target_version=t.version, expected_prior_event_id='event-1')
    if bad == 'source':
        override = replace(override, source_sha256='0' * 64)
    elif bad == 'prior':
        override = replace(override, expected_prior_event_id='old-event')
    else:
        override = replace(override, target_version='old-target-version')
    p = prepare(s, (t,), prior_user=prior, user_override=override)
    result = resolve_identity_candidate(p, p.snapshot())
    assert p.request() is None
    assert result.author == 'self' and result.relation == 'independent'
    assert result.needs_confirmation and result.supersede is None


def test_same_annotation_identity_target_correction_has_only_supersede_plan():
    s, old, new = source(), target('old-part'), target('new-part')
    prior = declaration(s, author='self', relation='target', target_id=old.candidate_id,
                        target_version=old.version, event_id='event-1')
    override = declaration(s, relation='target', target_id=new.candidate_id,
                           target_version=new.version, expected_prior_event_id='event-1')
    p = prepare(s, (old, new), prior_user=prior, user_override=override)
    result = resolve_identity_candidate(p, p.snapshot())
    assert result.legacy_projection == 'annotation' and not result.needs_confirmation
    assert result.supersede.prior_event_id == 'event-1'
    assert result.supersede.old_target_id == old.candidate_id and result.supersede.new_target_id == new.candidate_id
    assert result.supersede.old_author == result.supersede.new_author == 'self'
    assert s.text_utf8 == source().text_utf8


def test_candidate_limit_and_incomplete_scope_prohibit_model_independent():
    p = prepare(source(), tuple(target(str(n)) for n in range(10)))
    assert len(p.targets) == 8 and len(p.omitted_target_ids) == 2 and not p.scope_complete
    assert json.loads(p.state_json)['scope']['omitted_count'] == 2
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p))
    assert result.relation == 'unknown' and 'independent_requires_complete_scope' in result.reasons
    assert result.legacy_projection is None


def test_explicit_user_target_prioritized_and_user_independent_not_blocked_by_scope():
    s = source()
    targets = tuple(target(str(n)) for n in range(10))
    chosen = max(targets, key=lambda t: t.candidate_id)
    user = declaration(s, author='self', relation='target', target_id=chosen.candidate_id, target_version=chosen.version)
    p = prepare(s, targets, user_override=user)
    assert chosen in p.targets
    assert resolve_identity_candidate(p, p.snapshot()).legacy_projection == 'annotation'
    p = prepare(s, targets, user_override=declaration(s, author='self', relation='independent'))
    assert resolve_identity_candidate(p, p.snapshot()).legacy_projection == 'my_thought'


def test_missing_profile_is_pending_and_does_not_create_request():
    p = prepare_identity_context(source())
    assert p.request() is None and 'no_profile' in p.reasons
    result = resolve_identity_candidate(p, p.snapshot())
    assert result.author == result.relation == 'unknown' and result.legacy_projection is None


def test_budget_reuses_clef_client_function_with_all_state_questions_and_options():
    p = prepare(source(), (target(summary='existing summary', summary_provenance='synthetic:summary'),))
    state, questions = p.request()
    body = {'model': p.profile.model, 'state': state,
            'questions': {qid: q.wire() for qid, q in questions.items()}, 'truncate': False}
    assert p.budget == clef_template_upper_bound(body)
    s = source('长附言\r\n' * 10000)
    summary = '完整已有摘要\r\n' * 100
    p = prepare(s, (target(summary=summary, summary_provenance='synthetic:summary'),))
    assert p.request() is None and 'decision_budget_exceeded' in p.reasons
    assert p.source.text_utf8 == s.text_utf8
    assert json.loads(p.state_json)['targets'][0]['summary'] == summary
    assert resolve_identity_candidate(p, p.snapshot()).legacy_projection is None


def test_complete_user_decision_needs_no_model_budget_even_for_very_long_text():
    s = source('原字\r\n' * 20000)
    p = prepare(s, user_override=declaration(s, author='self', relation='independent'))
    assert p.request() is None and p.budget is None
    assert resolve_identity_candidate(p, p.snapshot()).legacy_projection == 'my_thought'


def test_jev_budget_is_deferred_to_existing_client_and_blocks_before_fake_http():
    p = prepare_identity_context(source('原字' * 10000), profile=profile('jev'), profile_version='synthetic-v1')
    assert p.budget is None
    state, questions = p.request()
    def forbidden(*args, **kwargs):
        pytest.fail('existing client admission must block before network')
    with pytest.raises(DecisionError, match='decision_budget_exceeded'):
        DecisionClient(p.profile, post=forbidden, secret=lambda ref: 'synthetic-fake-key').ask(state, questions)
    result = resolve_identity_candidate(p, p.snapshot(), error_code='decision_budget_exceeded')
    assert result.needs_confirmation and result.legacy_projection is None
    assert 'decision_budget_exceeded' in result.reasons


@pytest.mark.parametrize('provider,semantics,confidence', [
    ('jev', 'jev-normalized-concentration', .61), ('clef', 'clef-max-probability', 1.0),
])
def test_provider_probabilities_and_semantics_retained_without_thresholds(provider, semantics, confidence):
    p = prepare_identity_context(source(), profile=profile(provider), profile_version='synthetic-v1')
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p))
    answer = result.model_result.answers['author_identity']
    assert answer.confidence_semantics == semantics and answer.confidence == confidence
    assert answer.probability == 1.0 and result.proposal_only and result.legacy_projection is None


@pytest.mark.parametrize('stale', ['source', 'event', 'target'])
def test_resolution_snapshot_stale_never_produces_projection_or_supersede(stale):
    s, t = source(), target()
    p = prepare(s, (t,), user_override=declaration(s, author='self', relation='independent'))
    snapshot = p.snapshot()
    if stale == 'source':
        snapshot = replace(snapshot, source_sha256='0' * 64)
    elif stale == 'event':
        snapshot = replace(snapshot, prior_event_id='new-user-event')
    else:
        snapshot = replace(snapshot, target_versions=((t.candidate_id, 'changed'),))
    result = resolve_identity_candidate(p, snapshot)
    assert result.needs_confirmation and result.supersede is None and result.legacy_projection is None


def test_model_reply_binds_context_source_and_profile_and_mutable_request_is_detached():
    p = prepare(source())
    reply = fake_reply(p)
    for changed in (replace(reply, context_sha256='other'), replace(reply, source_sha256='other'),
                    replace(reply, profile_version='other')):
        result = resolve_identity_candidate(p, p.snapshot(), reply=changed)
        assert result.model_result is None and result.legacy_projection is None
        assert 'model_reply_stale' in result.reasons
    state, questions = p.request()
    state['message'] = 'changed'
    questions['author_identity'].criteria.clear()
    assert p.request()[0]['message'] == source().text()
    assert len(p.request()[1]['author_identity'].criteria) == 4


def test_cross_app_same_version_changed_context_and_summary_provenance_rejected():
    s, t = source(), target()
    with pytest.raises(IdentityContextError, match='cross_app_target'):
        prepare(s, (replace(t, app_id='other-app'),))
    with pytest.raises(IdentityContextError, match='target_version_conflict'):
        prepare(s, (t, replace(t, title='changed same-version title')))
    with pytest.raises(IdentityContextError, match='invalid_reference'):
        prepare(s, (replace(t, summary='existing but unowned'),))


def test_unrequested_model_error_cannot_displace_complete_user_decision():
    s = source()
    p = prepare(s, user_override=declaration(s, author='self', relation='independent'))
    result = resolve_identity_candidate(p, p.snapshot(), error_code='decision_timeout')
    assert result.legacy_projection == 'my_thought' and not result.needs_confirmation


def test_scoped_unknown_and_invalid_model_question_sets_cannot_create_raw_projection():
    p = prepare(source())
    reply = fake_reply(p, 'unknown', 'unknown')
    result = resolve_identity_candidate(p, p.snapshot(), reply=reply)
    assert result.author == result.relation == 'unknown' and result.legacy_projection is None
    assert result.supersede is None
    answers = dict(reply.result.answers)
    answers.pop('relation_target')
    with pytest.raises(IdentityContextError, match='model_result_binding_invalid'):
        resolve_identity_candidate(p, p.snapshot(), reply=replace(reply, result=replace(reply.result, answers=answers)))


def test_immutable_targets_validation_is_at_prepare_entry_and_target_cases_stay():
    s, t = source(), target()
    with pytest.raises(IdentityContextError, match='targets_must_be_immutable_tuple'):
        prepare_identity_context(s, [t])
    # This nonempty tuple exercises _strong_reference during actual sorting;
    # it must not be removed to hide a NameError in that helper.
    t = referenced(s, t)
    p = prepare(s, (t,))
    result = resolve_identity_candidate(p, p.snapshot(), reply=fake_reply(p, 'self', t.candidate_id))
    assert result.relation == 'target' and result.target_id == t.candidate_id
    assert result.proposal_only and result.legacy_projection is None


@pytest.mark.parametrize('field,value', [
    ('version', 'same-bytes-v2'), ('message_id', 'different-message'),
    ('app_id', 'different-app'), ('capture_id', 'different-capture'),
])
def test_same_bytes_different_source_identity_or_version_invalidates_old_plan(field, value):
    s = source()
    prior = declaration(s, author='third_party', relation='independent', event_id='prior-event')
    override = declaration(s, author='self', expected_prior_event_id='prior-event')
    old = prepare(s, prior_user=prior, user_override=override)
    assert resolve_identity_candidate(old, old.snapshot()).supersede is not None
    changed = replace(s, **{field: value})
    assert changed.text_utf8 == s.text_utf8 and changed.sha256 == s.sha256
    current = prepare(changed, prior_user=prior, user_override=override)
    result = resolve_identity_candidate(old, current.snapshot())
    assert result.needs_confirmation and result.legacy_projection is None and result.supersede is None
    assert 'resolution_snapshot_stale' in result.reasons


@pytest.mark.parametrize('change', ['version', 'message', 'app', 'capture'])
def test_explicit_snapshot_source_fields_are_checked_even_if_hashes_are_copied(change):
    s = source()
    p = prepare(s, user_override=declaration(s, author='self', relation='independent'))
    snapshot = p.snapshot()
    if change == 'version':
        snapshot = replace(snapshot, source_version='other-version')
    else:
        identity = list(snapshot.source_identity)
        identity[{'app': 0, 'message': 1, 'capture': 2}[change]] = 'changed'
        snapshot = replace(snapshot, source_identity=tuple(identity))
    result = resolve_identity_candidate(p, snapshot)
    assert result.legacy_projection is None and result.supersede is None and result.needs_confirmation


@pytest.mark.parametrize('change', ['selected', 'scope', 'excluded', 'omitted', 'summary', 'evidence'])
def test_current_selected_and_scope_context_changes_invalidate_old_user_plan(change):
    s, t = source(), target()
    user = declaration(s, author='self', relation='independent')
    old = prepare(s, (t,), user_override=user, max_candidates=1)
    kwargs = {'user_override': user, 'max_candidates': 1}
    targets = (t,)
    if change == 'selected':
        targets = (target('new-selected-part'),)
    elif change == 'scope':
        kwargs['scope_complete'] = False
    elif change == 'excluded':
        kwargs['excluded_count'] = 1
    elif change == 'omitted':
        targets = (t, target('new-part'))
    elif change == 'summary':
        targets = (replace(t, summary='new existing summary', summary_provenance='synthetic:summary'),)
    else:
        targets = (referenced(s, t),)
    current = prepare(s, targets, **kwargs)
    assert old.context_sha256 != current.context_sha256
    result = resolve_identity_candidate(old, current.snapshot())
    assert result.needs_confirmation and result.legacy_projection is None and result.supersede is None


def test_snapshot_requires_complete_selected_set_but_not_caller_iteration_order():
    s = source()
    p = prepare(s, (target('1'), target('2')),
                user_override=declaration(s, author='self', relation='independent'))
    snapshot = p.snapshot()
    reordered = replace(snapshot, target_versions=snapshot.target_versions[::-1])
    assert resolve_identity_candidate(p, reordered).legacy_projection == 'my_thought'
    for entries in (snapshot.target_versions[:1], snapshot.target_versions + snapshot.target_versions[:1]):
        stale = replace(snapshot, target_versions=entries)
        result = resolve_identity_candidate(p, stale)
        assert result.needs_confirmation and result.legacy_projection is None and result.supersede is None
