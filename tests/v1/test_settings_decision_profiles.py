"""R17 backend only: disposable profile files, memory keys, fake HTTP, no DB."""
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1.decision_client import ChoiceQuestion, DecisionError, DecisionProfile, JEV_ENDPOINT
from knowledge_distiller.v1.decision_profiles import DecisionProfiles
from knowledge_distiller.v1.settings import SettingsError, SettingsService


class MemorySecrets:
    def __init__(self, root=None):
        self.values, self.events = {}, []
        self.fail_save = self.fail_load = False

    def __call__(self, ref):
        owner = self

        class Secret:
            def save(self, value):
                owner.events.append(('save', ref))
                if owner.fail_save:
                    raise RuntimeError('SENSITIVE-backend-error')
                assert ref not in owner.values
                owner.values[ref] = value

            def save_validated(self, value):
                owner.values[ref] = value

            def load(self):
                owner.events.append(('load', ref))
                if owner.fail_load or ref not in owner.values:
                    raise RuntimeError('SENSITIVE-backend-error')
                return owner.values[ref]

        return Secret()

    def status(self, ref):
        return 'validated' if ref in self.values else 'missing'


class StoreFacade:
    def __init__(self, path):
        self.path, self.values = path, {}

    def setting(self, name):
        return self.values.get(name)

    def set_setting(self, name, value):
        self.values[name] = value


class FakeHTTP:
    def __init__(self):
        self.events, self.status, self.bad = [], 200, False

    def get(self, url, **kwargs):
        self.events.append(('get', url, kwargs))
        return SimpleNamespace(status_code=self.status,
                               json=lambda: {'status': 'ok', 'model': 'synthetic-clef'})

    def post(self, url, **kwargs):
        self.events.append(('post', url, kwargs))
        answers = {}
        provider = 'clef' if url.startswith('http:') else 'jev'
        for key, question in kwargs['json']['questions'].items():
            if question['type'] == 'noul':
                answers[key] = {'type': 'noul', 'noul': .9}
            else:
                answers[key] = {'type': 'choice', 'choice': 'match',
                                'probabilities': {'match': .9, 'other': .1},
                                'confidence': .9 if provider == 'clef' else .8}
        if self.bad:
            answers = {}
        return SimpleNamespace(status_code=self.status, json=lambda: {
            'model': 'synthetic-clef' if provider == 'clef' else 'synthetic-jev-resolved',
            'answers': answers, 'usage': {'input_tokens': 220, 'output_tokens': 0}})


def fields(provider='clef'):
    return asdict(DecisionProfile(provider,
                  JEV_ENDPOINT if provider == 'jev' else 'http://127.0.0.1:8198/v1/systemone',
                  'jev-latest' if provider == 'jev' else 'synthetic-clef',
                  auth_ref='decision-key-' + 'a' * 32 if provider == 'jev' else None))


@pytest.fixture
def backend(tmp_path, monkeypatch):
    import knowledge_distiller.v1.local_secrets as module
    memory = MemorySecrets()
    monkeypatch.setattr(module, 'LocalSecrets', lambda root: memory)
    root = tmp_path.resolve()
    store, http = StoreFacade(root / 'unused.sqlite'), FakeHTTP()
    inert = object()
    service = SettingsService(store, chrome=inert, youtube=inert, xiaohongshu=inert,
        xpost=inert, zhihu=inert, weibo=inert, qwen_component=inert,
        codex_probe=lambda: None, codex_client=inert,
        jev_probe=lambda key: None, decision_root=root / 'profiles',
        decision_secret_factory=memory, decision_post=http.post, decision_get=http.get)
    yield service, memory, http
    assert not store.path.exists()
    assert not (root / 'credentials').exists()


def test_constructor_has_no_new_profile_credential_or_transport_io(backend):
    service, memory, http = backend
    assert not Path(service._decision_root).exists()
    assert memory.events == http.events == []
    assert service.store.values == {}


def test_empty_active_never_invokes_legacy_factory_or_check(backend):
    service, memory, http = backend
    service.jev_client = lambda: pytest.fail('legacy fallback')
    assert service.decision_client() is None
    assert service.decision_state() == {'active': None}
    assert memory.events == http.events == []


def test_draft_check_activate_restart_and_typed_scope(backend):
    service, memory, http = backend
    identity = service.save_decision_draft(fields())
    assert http.events == []
    assert service.decision_state(identity)['draft']['checked'] is False
    with pytest.raises(SettingsError, match='decision_profile_unvalidated'):
        service.activate_decision_profile(identity, expected_active_id=None)
    check = service.check_decision_draft(identity)
    assert check['checked'] and 'answers' not in check
    assert service.decision_client() is None
    assert [event[0] for event in http.events] == ['get', 'post']
    active = service.activate_decision_profile(identity, expected_active_id=None)
    assert active['profile_id'] == identity
    count = len(http.events)
    inert = object()
    restarted = SettingsService(service.store, chrome=inert, youtube=inert, xiaohongshu=inert,
        xpost=inert, zhihu=inert, weibo=inert, qwen_component=inert,
        codex_probe=lambda: None, codex_client=inert,
        decision_root=service._decision_root, decision_secret_factory=memory,
        decision_post=http.post, decision_get=http.get)
    version, client = restarted.decision_client()
    assert version == identity and client.profile.model == 'synthetic-clef'
    assert len(http.events) == count and memory.events == []
    assert service.decision_state(identity)['draft']['active'] is True


@pytest.mark.parametrize('edit', [
    {'model': 'edited-model'}, {'endpoint': 'http://127.0.0.1:8199/v1/systemone'},
    {'timeout_seconds': 120}, {'token_budget': 8000},
])
def test_edits_are_unchecked_new_drafts_preserving_active(backend, edit):
    service, _, http = backend
    old = service.save_decision_draft(fields())
    service.check_decision_draft(old)
    service.activate_decision_profile(old, expected_active_id=None)
    new = service.save_decision_draft(fields() | edit)
    assert new != old and service.decision_state(new)['draft']['checked'] is False
    with pytest.raises(SettingsError, match='decision_profile_unvalidated'):
        service.activate_decision_profile(new, expected_active_id=old)
    assert service.decision_state()['active']['profile_id'] == old
    assert len(http.events) == 2


def test_key_edit_new_ref_no_plaintext_and_no_key_gc(backend):
    service, memory, http = backend
    old = service.save_decision_draft(fields('jev'), api_key='SYNTHETIC-secret-A')
    service.check_decision_draft(old)
    service.activate_decision_profile(old, expected_active_id=None)
    new = service.save_decision_draft(fields('jev'), api_key='SYNTHETIC-secret-B')
    old_ref = service.decision_state(old)['draft']['profile']['auth_ref']
    new_ref = service.decision_state(new)['draft']['profile']['auth_ref']
    assert old_ref != new_ref and len(memory.values) == 2
    assert service.decision_state(new)['draft']['checked'] is False
    serialized = (Path(service._decision_root) / 'decision-profiles.json').read_text()
    assert 'SYNTHETIC-secret' not in serialized
    assert 'SYNTHETIC-secret' not in json.dumps(service.decision_state(new))
    assert service.store.values == {} and len(http.events) == 1


@pytest.mark.parametrize('status,code', [(401, 'decision_unauthorized'), (413, 'decision_budget_exceeded'),
                                       (503, 'decision_request_failed')])
def test_failed_check_clears_eligibility_preserves_old_active(backend, status, code):
    service, _, http = backend
    old = service.save_decision_draft(fields())
    service.check_decision_draft(old)
    service.activate_decision_profile(old, expected_active_id=None)
    new = service.save_decision_draft(fields('jev'), api_key='SYNTHETIC-key')
    service.check_decision_draft(new)
    http.status = status
    with pytest.raises(SettingsError, match=code):
        service.check_decision_draft(new)
    assert not service.decision_state(new)['draft']['checked']
    assert service.decision_state()['active']['profile_id'] == old
    assert service.store.values == {}


def test_missing_explicit_transport_never_checks(backend):
    service, _, http = backend
    identity = service.save_decision_draft(fields())
    service._decision_get = None
    with pytest.raises(SettingsError, match='decision_probe_transport_required'):
        service.check_decision_draft(identity)
    assert http.events == []


def test_missing_transport_recheck_invalidates_previous_check(backend):
    service, _, http = backend
    identity = service.save_decision_draft(fields())
    service.check_decision_draft(identity)
    count = len(http.events)
    service._decision_post = None
    with pytest.raises(SettingsError, match='decision_probe_transport_required'):
        service.check_decision_draft(identity)
    assert not service.decision_state(identity)['draft']['checked']
    assert len(http.events) == count + 1  # Explicit health GET, no inference POST.


def test_constructor_records_factories_without_calling_them(backend):
    service, memory, http = backend
    def forbidden(*args, **kwargs):
        pytest.fail('construction must not invoke injected dependencies')
    inert = object()
    SettingsService(service.store, chrome=inert, youtube=inert, xiaohongshu=inert,
        xpost=inert, zhihu=inert, weibo=inert, qwen_component=inert,
        codex_probe=lambda: None, codex_client=inert,
        decision_root=service._decision_root, decision_profiles_factory=forbidden,
        decision_secret_factory=forbidden, decision_post=forbidden, decision_get=forbidden)
    assert not Path(service._decision_root).exists()
    assert memory.events == http.events == []


@pytest.mark.parametrize('edit', [
    {'endpoint': 'https://example.invalid/v1/systemone'}, {'auth_ref': 'path:secret'},
    {'api_key': 'SENSITIVE-should-not-persist'}, {'token_budget': 0},
])
def test_invalid_fields_never_save_keys_or_contact_services(backend, edit):
    service, memory, http = backend
    with pytest.raises(SettingsError, match='decision_profile_invalid'):
        service.save_decision_draft(fields() | edit)
    assert memory.events == http.events == []


def test_failed_current_provider_never_checks_another_provider(backend):
    service, _, http = backend
    identity = service.save_decision_draft(fields())
    http.bad = True
    with pytest.raises(SettingsError, match='decision_response_invalid'):
        service.check_decision_draft(identity)
    assert [event[0] for event in http.events] == ['get', 'post']
    assert all(event[1].startswith('http://127.0.0.1:') for event in http.events)
    assert service.decision_client() is None


def test_settings_activation_cas_rejects_stale_expected_active(backend):
    service, _, _ = backend
    old = service.save_decision_draft(fields())
    service.check_decision_draft(old)
    service.activate_decision_profile(old, expected_active_id=None)
    new = service.save_decision_draft(fields())
    service.check_decision_draft(new)
    with pytest.raises(SettingsError, match='decision_active_conflict'):
        service.activate_decision_profile(new, expected_active_id=None)
    assert service.decision_state()['active']['profile_id'] == old
    service.activate_decision_profile(new, expected_active_id=old)
    assert service.decision_state()['active']['profile_id'] == new


def test_committed_activation_snapshot_survives_next_legal_activation(backend):
    service, _, _ = backend
    first = service.save_decision_draft(fields())
    second = service.save_decision_draft(fields() | {'token_budget': 8000})
    service.check_decision_draft(first)
    service.check_decision_draft(second)

    class InterleavedProfiles(DecisionProfiles):
        def activate(self, identity, *, expected_active_id):
            super().activate(identity, expected_active_id=expected_active_id)
            # Another independent writer legally commits after the first lock
            # release, before the first SettingsService operation can return.
            other = DecisionProfiles(self.root)
            other.activate(second, expected_active_id=first)

        def active(self):
            pytest.fail('activation receipt must not reread current active')

    service._decision_profiles_factory = InterleavedProfiles
    receipt = service.activate_decision_profile(first, expected_active_id=None)
    assert receipt == {'profile_id': first, 'profile': fields()}
    service._decision_profiles_factory = None
    current = service.decision_state()['active']
    assert current['profile_id'] == second and current['profile']['token_budget'] == 8000


def test_bad_config_is_error_not_legacy_fallback(backend):
    service, _, http = backend
    service.decision_state()
    path = Path(service._decision_root) / 'decision-profiles.json'
    path.write_text('{broken', encoding='utf-8')
    path.chmod(0o600)
    service.jev_client = lambda: pytest.fail('legacy fallback')
    with pytest.raises(SettingsError, match='decision_store_invalid'):
        service.decision_client()
    assert http.events == []


def test_secret_failures_use_fixed_codes_and_do_not_echo_backend(backend):
    service, memory, http = backend
    memory.fail_save = True
    with pytest.raises(SettingsError) as caught:
        service.save_decision_draft(fields('jev'), api_key='SYNTHETIC-key')
    assert str(caught.value) == 'decision_secret_save_failed'
    assert caught.value.__suppress_context__
    memory.fail_save = False
    identity = service.save_decision_draft(fields('jev'), api_key='SYNTHETIC-key')
    memory.fail_load = True
    with pytest.raises(SettingsError) as caught:
        service.check_decision_draft(identity)
    assert str(caught.value) == 'decision_secret_unavailable' and http.events == []
    with pytest.raises(DecisionError, match='^decision_secret_unavailable$'):
        service._decision_secret('jev-api-key')


def test_active_client_resolves_key_lazily_and_failure_has_no_fallback(backend):
    service, memory, http = backend
    identity = service.save_decision_draft(fields('jev'), api_key='SYNTHETIC-key')
    service.check_decision_draft(identity)
    service.activate_decision_profile(identity, expected_active_id=None)
    memory.events.clear()
    count = len(http.events)
    version, client = service.decision_client()
    assert version == identity and memory.events == []
    memory.fail_load = True
    with pytest.raises(DecisionError, match='^decision_secret_unavailable$'):
        client.ask({'marker': 'synthetic'}, {'choice': ChoiceQuestion('Choose match',
                   {'match': 'synthetic', 'other': 'different'})})
    assert len(http.events) == count
    assert service.decision_state()['active']['profile_id'] == identity


@pytest.mark.parametrize('error_type', [RuntimeError, SettingsError])
def test_unexpected_backend_error_never_echoes_raw_error(backend, error_type):
    service, _, _ = backend
    def broken(*args, **kwargs):
        raise error_type('SENSITIVE-error-path-key-response')
    service._decision_profiles_factory = broken
    with pytest.raises(SettingsError) as caught:
        service.decision_state()
    assert str(caught.value) == 'decision_operation_failed'
    assert caught.value.__suppress_context__


def test_windows_is_stable_unsupported_without_factory_or_fallback(backend, monkeypatch):
    import knowledge_distiller.v1.settings as module
    service, memory, http = backend
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    for call in (service.decision_state, service.decision_client,
                 lambda: service.save_decision_draft(fields())):
        with pytest.raises(SettingsError, match='^decision_profiles_unsupported$'):
            call()
    assert memory.events == http.events == []


def test_legacy_jev_semantics_and_new_profile_are_independent(backend):
    service, memory, http = backend
    assert service.jev_state() == 'unconfigured' and service.jev_client() is None
    service.save_jev_key('SYNTHETIC-legacy')
    assert service.jev_client().secret() == 'SYNTHETIC-legacy'
    service.mark_jev_unavailable()
    assert service.jev_state() == 'unavailable' and service.jev_client() is not None
    identity = service.save_decision_draft(fields())
    assert service.decision_client() is None
    assert service.decision_state(identity)['draft']['checked'] is False
    assert service.jev_state() == 'unavailable' and http.events == []
