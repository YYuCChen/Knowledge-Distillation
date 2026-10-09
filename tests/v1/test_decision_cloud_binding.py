"""Synthetic profiles/secrets/typed transports only; no SQL or live services."""
import json
import stat
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1 import decision_profiles as persistence
from knowledge_distiller.v1.decision_client import JEV_ENDPOINT
from knowledge_distiller.v1.settings import SettingsService, SettingsError


class Secrets:
    def __init__(self):
        self.values = {'jev-api-key': 'synthetic-old-key'}
        self.events = []

    def __call__(self, ref):
        owner = self

        class Secret:
            def load(self):
                owner.events.append(('load', ref))
                return owner.values[ref]

            def save(self, value):
                assert ref not in owner.values
                owner.events.append(('save', ref))
                owner.values[ref] = value

        return Secret()


class Transport:
    def __init__(self):
        self.events = []
        self.bad = False

    def get(self, url, **kwargs):
        self.events.append(('get', url))
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return SimpleNamespace(status_code=200, json=lambda: {'status': 'ok', 'model': 'synthetic-clef'})

    def post(self, url, **kwargs):
        self.events.append(('post', url))
        assert kwargs['timeout'] == 20.0
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        assert set(kwargs['json']['questions']) == {'choice', 'noul'}
        cloud = url == JEV_ENDPOINT
        bad = self.bad or kwargs.get('headers', {}).get('Authorization') == 'Bearer synthetic-bad-key'
        data = {'model': 'synthetic-jev' if cloud else 'synthetic-clef',
                'answers': {} if bad else {
                    'choice': {'type': 'choice', 'choice': 'match',
                               'probabilities': {'match': .9, 'other': .1},
                               'confidence': .8 if cloud else .9},
                    'noul': {'type': 'noul', 'noul': .9}},
                'usage': {'input_tokens': 100, 'output_tokens': 0}}
        return SimpleNamespace(status_code=200, json=lambda: data)


def fields(provider='jev'):
    return {'provider': provider,
            'endpoint': JEV_ENDPOINT if provider == 'jev' else 'http://127.0.0.1:8198/v1/systemone',
            'model': 'jev-latest' if provider == 'jev' else 'synthetic-clef'}


def assemble(root, memory, http):
    inert = object()
    facade = SimpleNamespace(path=root / 'unused.sqlite')
    return SettingsService(facade, chrome=inert, youtube=inert, xiaohongshu=inert,
        xpost=inert, zhihu=inert, weibo=inert, qwen_component=inert,
        codex_probe=lambda: None, codex_client=inert, jev_probe=lambda key: None,
        decision_root=root / 'profiles', decision_secret_factory=memory,
        decision_post=http.post, decision_get=http.get)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    import knowledge_distiller.v1.local_secrets as secrets_module
    root = tmp_path.resolve()
    memory, http = Secrets(), Transport()
    monkeypatch.setattr(secrets_module, 'LocalSecrets', lambda path: memory)
    service = assemble(root, memory, http)
    yield service, memory, http, root
    assert not (root / 'unused.sqlite').exists()
    assert not (root / 'credentials').exists()


def enable(service, checked):
    return service.activate_decision_candidate(checked['draft_id'],
        expected_active_id=checked['expected_active_id'],
        expected_current_cloud_profile_id=checked['expected_current_cloud_profile_id'])


def file_state(root):
    return json.loads((root / 'profiles' / 'decision-profiles.json').read_bytes())


@pytest.mark.parametrize('active_provider', ['jev', 'clef', None])
def test_v1_read_projection_preserves_bytes_ids_and_all_drafts(backend, active_provider):
    service, memory, http, root = backend
    cloud = service.check_decision_candidate(fields())
    enable(service, cloud)
    local = service.check_decision_candidate(fields('clef'))
    enable(service, local)
    path = root / 'profiles' / 'decision-profiles.json'
    state = file_state(root)
    state.pop('current_cloud_profile_id')
    state['version'] = 1
    state['active'] = cloud['draft_id'] if active_provider == 'jev' else local['draft_id'] if active_provider else None
    # Exact old format with legitimately checked rows, not fake validation.
    path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True))
    path.chmod(0o600)
    before = path.read_bytes()
    memory.events.clear()
    http.events.clear()
    projected = service.decision_state(include_cloud=True)
    expected = cloud['draft_id'] if active_provider == 'jev' else None
    assert (projected['current_cloud']['profile_id'] if projected['current_cloud'] else None) == expected
    assert path.read_bytes() == before
    assert memory.events == http.events == []
    new_id = service.save_decision_draft(fields('clef'))
    after = file_state(root)
    assert after['version'] == 2 and after['current_cloud_profile_id'] == expected
    assert after['active'] == state['active']
    assert all(after['profiles'][key] == row for key, row in state['profiles'].items())
    assert new_id not in state['profiles']


def test_check_copies_old_local_key_only_on_explicit_check_and_never_echoes(backend):
    service, memory, http, root = backend
    assert service.decision_state() == {'active': None}
    assert service.decision_state(include_cloud=True) == {'active': None, 'current_cloud': None}
    assert memory.events == http.events == []
    assert not (root / 'profiles' / 'decision-profiles.json').exists()
    checked = service.check_decision_candidate(fields())
    profile = service.decision_state(checked['draft_id'])['draft']['profile']
    assert profile['timeout_seconds'] == 20.0 and profile['token_budget'] == 16384
    assert profile['auth_ref'] != 'jev-api-key'
    assert memory.values[profile['auth_ref']] == memory.values['jev-api-key']
    assert service.decision_state(include_cloud=True)['active'] is None
    assert file_state(root)['current_cloud_profile_id'] is None
    assert 'synthetic-old-key' not in json.dumps(checked) + json.dumps(file_state(root))
    enable(service, checked)
    assert memory.values['jev-api-key'] == 'synthetic-old-key'


def test_reboot_local_retains_cloud_and_empty_cloud_key_reuses_exact_ref(backend):
    service, memory, http, root = backend
    cloud = service.check_decision_candidate(fields(), api_key='synthetic-new-key')
    enable(service, cloud)
    ref = service.decision_state(include_cloud=True)['current_cloud']['profile']['auth_ref']
    local = service.check_decision_candidate(fields('clef'))
    enable(service, local)
    memory.events.clear()
    http.events.clear()
    restarted = assemble(root, memory, http)
    state = restarted.decision_state(include_cloud=True)
    assert state['active']['profile_id'] == local['draft_id']
    assert state['current_cloud']['profile_id'] == cloud['draft_id']
    assert memory.events == http.events == []
    next_cloud = restarted.check_decision_candidate(fields(), api_key='')
    assert restarted.decision_state(next_cloud['draft_id'])['draft']['profile']['auth_ref'] == ref
    assert not any(event[0] == 'save' for event in memory.events)
    assert ('load', 'jev-api-key') not in memory.events
    assert restarted.decision_state(include_cloud=True) == state  # check did not enable


@pytest.mark.parametrize('override', ['timeout_seconds', 'token_budget', 'auth_ref', 'extra'])
def test_candidate_rejects_host_default_and_ref_overrides_before_io(backend, override):
    service, memory, http, root = backend
    value = {**fields(), override: 1}
    with pytest.raises(SettingsError, match='^decision_profile_invalid$'):
        service.check_decision_candidate(value, api_key='synthetic-new-key')
    assert memory.events == http.events == []
    assert not (root / 'profiles').exists()


def test_bad_key_check_preserves_both_bindings_and_old_key(backend):
    service, memory, http, root = backend
    checked = service.check_decision_candidate(fields())
    enable(service, checked)
    bindings = service.decision_state(include_cloud=True)
    refs = dict(memory.values)
    with pytest.raises(SettingsError, match='^decision_response_invalid$'):
        service.check_decision_candidate(fields(), api_key='synthetic-bad-key')
    assert service.decision_state(include_cloud=True) == bindings
    assert all(memory.values[key] == value for key, value in refs.items())
    assert len(memory.values) == len(refs) + 1  # honest orphan/unaccepted new ref
    assert file_state(root)['profiles'][file_state(root)['active']]['validation'] is not None


def test_joint_cas_rejects_cloud_drift_even_when_active_returns_to_same_id(backend):
    service, memory, http, root = backend
    cloud = service.check_decision_candidate(fields())
    enable(service, cloud)
    local = service.check_decision_candidate(fields('clef'))
    enable(service, local)
    stale = service.check_decision_candidate(fields('clef'))
    next_cloud = service.check_decision_candidate(fields(), api_key='synthetic-other-key')
    enable(service, next_cloud)
    service.activate_decision_candidate(local['draft_id'], expected_active_id=next_cloud['draft_id'],
        expected_current_cloud_profile_id=next_cloud['draft_id'])
    before = (root / 'profiles' / 'decision-profiles.json').read_bytes()
    with pytest.raises(SettingsError, match='^decision_cloud_conflict$'):
        enable(service, stale)
    assert (root / 'profiles' / 'decision-profiles.json').read_bytes() == before
    with pytest.raises(SettingsError, match='^decision_active_conflict$'):
        service.activate_decision_candidate(stale['draft_id'], expected_active_id=None,
            expected_current_cloud_profile_id=next_cloud['draft_id'])
    assert (root / 'profiles' / 'decision-profiles.json').read_bytes() == before


def test_old_activation_api_still_works_but_new_api_requires_both_bases(backend):
    service, memory, http, root = backend
    checked = service.check_decision_candidate(fields())
    service.activate_decision_profile(checked['draft_id'], expected_active_id=None)
    assert service.decision_state(include_cloud=True)['current_cloud']['profile_id'] == checked['draft_id']
    with pytest.raises(TypeError):
        service.activate_decision_candidate(checked['draft_id'], expected_active_id=checked['draft_id'])


def test_retained_cloud_cannot_have_qualification_cleared_by_recheck(backend):
    service, memory, http, root = backend
    cloud = service.check_decision_candidate(fields())
    enable(service, cloud)
    local = service.check_decision_candidate(fields('clef'))
    enable(service, local)
    before = (root / 'profiles' / 'decision-profiles.json').read_bytes()
    with pytest.raises(SettingsError, match='^decision_active_immutable$'):
        service.check_decision_draft(cloud['draft_id'])
    assert (root / 'profiles' / 'decision-profiles.json').read_bytes() == before


def test_replace_failure_preserves_old_file_and_unreferenced_secret_is_not_gc(backend, monkeypatch):
    service, memory, http, root = backend
    cloud = service.check_decision_candidate(fields())
    enable(service, cloud)
    path = root / 'profiles' / 'decision-profiles.json'
    before, old_keys = path.read_bytes(), dict(memory.values)
    def fail_replace(*args, **kwargs):
        raise OSError('synthetic replace failure')
    monkeypatch.setattr(persistence.os, 'replace', fail_replace)
    with pytest.raises(SettingsError, match='^decision_store_io_failed$'):
        service.check_decision_candidate(fields(), api_key='synthetic-orphan-key')
    assert path.read_bytes() == before
    assert all(memory.values[key] == value for key, value in old_keys.items())
    assert len(memory.values) == len(old_keys) + 1
    assert list(path.parent.glob('.decision-profiles-*.tmp')) == []


def test_joint_activation_is_one_replacement_and_postreplace_failure_is_not_rollback(backend, monkeypatch):
    service, memory, http, root = backend
    checked = service.check_decision_candidate(fields())
    original_fsync = persistence.os.fsync
    original_replace = persistence.os.replace
    replacements = []
    def observe_replace(*args, **kwargs):
        result = original_replace(*args, **kwargs)
        replacements.append(args[1])
        return result
    def fail_directory_sync(fd):
        if stat.S_ISDIR(persistence.os.fstat(fd).st_mode):
            raise OSError('synthetic directory sync failure after replace')
        return original_fsync(fd)
    monkeypatch.setattr(persistence.os, 'fsync', fail_directory_sync)
    monkeypatch.setattr(persistence.os, 'replace', observe_replace)
    with pytest.raises(SettingsError, match='^decision_store_io_failed$'):
        enable(service, checked)
    state = file_state(root)
    assert replacements == ['decision-profiles.json']
    assert state['active'] == state['current_cloud_profile_id'] == checked['draft_id']
    # One file exposes the complete pair; durability failure is not old-state restoration.


@pytest.mark.parametrize('cloud_value', ['missing-id', 'local', 'unchecked'])
def test_invalid_v2_cloud_reference_is_rejected_without_repair(backend, cloud_value):
    service, memory, http, root = backend
    local = service.check_decision_candidate(fields('clef'))
    unchecked = service.save_decision_draft(fields(), api_key='synthetic-unchecked-key')
    state = file_state(root)
    state['current_cloud_profile_id'] = {'missing-id': 'f' * 32,
        'local': local['draft_id'], 'unchecked': unchecked}[cloud_value]
    path = root / 'profiles' / 'decision-profiles.json'
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    memory.events.clear()
    http.events.clear()
    with pytest.raises(SettingsError, match='^decision_store_invalid$'):
        service.decision_state(include_cloud=True)
    assert path.read_bytes() == before and memory.events == http.events == []


def test_no_transport_and_missing_old_key_fail_without_binding_changes(backend):
    service, memory, http, root = backend
    memory.values.clear()
    with pytest.raises(SettingsError, match='^decision_secret_unavailable$'):
        service.check_decision_candidate(fields())
    assert service.decision_state(include_cloud=True) == {'active': None, 'current_cloud': None}
    service._decision_post = service._decision_get = None
    with pytest.raises(SettingsError, match='^decision_probe_transport_required$'):
        service.check_decision_candidate(fields('clef'))
    assert http.events == []
    assert file_state(root)['active'] is file_state(root)['current_cloud_profile_id'] is None
