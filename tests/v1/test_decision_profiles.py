"""Private profile persistence in pytest's explicitly disposable tmp root."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from threading import Barrier

import pytest

from knowledge_distiller.v1.decision_client import DecisionError, DecisionProfile, JEV_ENDPOINT
from knowledge_distiller.v1.decision_profiles import DecisionProfileError, DecisionProfiles


def profile(provider='clef'):
    return DecisionProfile(provider,
                           JEV_ENDPOINT if provider == 'jev' else 'http://127.0.0.1:8198/v1/systemone',
                           'jev-latest' if provider == 'jev' else 'clef-4bit',
                           auth_ref='synthetic-ref' if provider == 'jev' else None)


class FakeService:
    def __init__(self):
        self.status, self.invalid, self.calls = 200, None, []
        self.served_model, self.health_status, self.health_calls = 'clef-4bit', 200, []
        self.events = []

    def get(self, url, **kwargs):
        self.health_calls.append((url, kwargs))
        self.events.append('GET')
        response = type('FakeHealth', (), {})()
        response.status_code = self.health_status
        response.json = lambda: {'status': 'ok', 'model': self.served_model}
        return response

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.events.append('POST')
        provider = 'clef' if url.startswith('http://127.0.0.1:') else 'jev'
        data = {'model': 'clef-4bit' if provider == 'clef' else 'jev-1.13.0',
                'answers': {'choice': {'type': 'choice', 'choice': 'match',
                                       'probabilities': {'match': .9, 'other': .1},
                                       'confidence': .9 if provider == 'clef' else .8},
                            'noul': {'type': 'noul', 'noul': .9}},
                'usage': {'input_tokens': 220, 'output_tokens': 0}}
        if self.invalid:
            self.invalid(data)
        response = type('FakeResponse', (), {})()
        response.status_code = self.status
        response.json = lambda: data
        return response


@pytest.fixture
def service(tmp_path):
    # Resolve pytest's canonical path; product persistence itself never resolves
    # aliases or silently follows caller symlinks.
    fake = FakeService()
    root = tmp_path.resolve() / 'synthetic-private-profiles'
    return DecisionProfiles(root, secret=lambda ref: 'synthetic-injected-key', post=fake, get=fake.get), fake


def test_successful_probe_does_not_activate_and_restart_preserves_active(service):
    store, fake = service
    identity = store.add_draft(profile())
    assert store.active() is None
    with pytest.raises(DecisionProfileError, match='decision_profile_unvalidated'):
        store.activate(identity)
    result = store.validate_draft(identity)
    assert result.provider == 'clef' and store.active() is None
    request = fake.calls[0][1]['json']
    assert set(request['questions']) == {'choice', 'noul'} and request['truncate'] is False
    assert fake.events == ['GET', 'POST']
    assert fake.health_calls[0][0] == 'http://127.0.0.1:8198/health'
    assert fake.health_calls[0][1]['follow_redirects'] is False
    assert fake.health_calls[0][1]['trust_env'] is False
    store.activate(identity)
    restarted = DecisionProfiles(store.root, post=fake, get=fake.get)
    assert restarted.active() == (identity, profile())
    assert restarted.client(identity).profile == profile()


def test_new_draft_failure_preserves_old_active_and_success_still_requires_activation(service):
    store, fake = service
    old = store.add_draft(profile('jev'))
    store.validate_draft(old)
    store.activate(old)
    new = store.add_draft(profile())
    before = store.active()
    fake.status = 413
    with pytest.raises(DecisionError, match='decision_budget_exceeded'):
        store.validate_draft(new)
    assert store.active() == before
    with pytest.raises(DecisionProfileError, match='decision_profile_unvalidated'):
        store.activate(new)
    fake.status = 200
    store.validate_draft(new)
    assert store.active() == before
    store.activate(new)
    assert store.active() == (new, profile())
    assert store.get(old) == profile('jev')


@pytest.mark.parametrize('mutator', [
    lambda d: d['answers'].pop('noul'),
    lambda d: d['answers']['choice']['probabilities'].pop('other'),
    lambda d: d['answers']['choice'].update(type='score'),
    lambda d: d['answers']['noul'].update(noul=True),
    lambda d: d['answers']['noul'].update(noul=.1),
    lambda d: d['answers']['choice'].update(choice='other', probabilities={'match': .1, 'other': .9}),
    lambda d: d.update(model='chat-model'),
])
def test_http200_without_correct_choice_noul_inference_is_not_validation(service, mutator):
    store, fake = service
    identity = store.add_draft(profile())
    fake.invalid = mutator
    with pytest.raises(DecisionError):
        store.validate_draft(identity)
    with pytest.raises(DecisionProfileError, match='decision_profile_unvalidated'):
        store.activate(identity)
    assert store.active() is None


def test_failed_revalidation_clears_draft_eligibility(service):
    store, fake = service
    identity = store.add_draft(profile())
    store.validate_draft(identity)
    fake.status = 500
    with pytest.raises(DecisionError):
        store.validate_draft(identity)
    with pytest.raises(DecisionProfileError, match='decision_profile_unvalidated'):
        store.activate(identity)


def test_active_profile_cannot_be_revalidated_or_overwritten(service):
    store, fake = service
    identity = store.add_draft(profile())
    store.validate_draft(identity)
    store.activate(identity)
    before = (store.root / 'decision-profiles.json').read_bytes()
    with pytest.raises(DecisionProfileError, match='decision_active_immutable'):
        store.validate_draft(identity)
    assert (store.root / 'decision-profiles.json').read_bytes() == before
    assert len(fake.calls) == 1


def test_private_permissions_and_no_plaintext_secret(service):
    store, fake = service
    identity = store.add_draft(profile('jev'))
    store.validate_draft(identity)
    for path, mode in [(store.root, 0o700), (store.root / 'decision-profiles.json', 0o600),
                       (store.root / '.decision-profiles.lock', 0o600)]:
        assert stat.S_IMODE(path.stat().st_mode) == mode
    content = (store.root / 'decision-profiles.json').read_text()
    assert 'synthetic-ref' in content and 'synthetic-injected-key' not in content
    assert 'synthetic-injected-key' not in repr(store.client(identity))
    assert fake.calls[0][1]['headers']['Authorization'] == 'Bearer synthetic-injected-key'


@pytest.mark.parametrize('text', ['not-json', '{}', '{"version":1,"version":1}',
                                 '{"version":true,"active":null,"profiles":{}}',
                                 '{"version":1,"active":"missing","profiles":{}}',
                                 '{"version":1,"active":null,"profiles":{},"secret":"key"}',
                                 '{"version":NaN,"active":null,"profiles":{}}'])
def test_corrupt_store_is_rejected_without_replacement(service, text):
    store, _ = service
    path = store.root / 'decision-profiles.json'
    path.write_text(text)
    path.chmod(0o600)
    before = path.read_bytes()
    for action in (store.active, lambda: store.add_draft(profile())):
        with pytest.raises(DecisionProfileError, match='decision_store_invalid'):
            action()
    assert path.read_bytes() == before


def test_invalid_persisted_profile_not_silently_defaulted(service):
    store, _ = service
    identity = store.add_draft(profile())
    path = store.root / 'decision-profiles.json'
    body = json.loads(path.read_text())
    body['profiles'][identity]['profile']['endpoint'] = 'http://localhost:8198/v1/systemone'
    path.write_text(json.dumps(body))
    with pytest.raises(DecisionProfileError, match='decision_store_invalid'):
        store.get(identity)


@pytest.mark.parametrize('name', ['decision-profiles.json', '.decision-profiles.lock'])
def test_symlink_store_and_lock_are_rejected_without_touching_target(service, tmp_path, name):
    store, _ = service
    target = tmp_path / 'synthetic-outside'
    target.write_text('keep this synthetic outside file')
    target.chmod(0o600)
    (store.root / name).symlink_to(target)
    with pytest.raises(DecisionProfileError):
        store.add_draft(profile())
    assert target.read_text() == 'keep this synthetic outside file'
    assert (store.root / name).is_symlink()


def test_hardlink_and_loose_file_permissions_are_rejected(service, tmp_path):
    store, _ = service
    store.add_draft(profile())
    path = store.root / 'decision-profiles.json'
    path.chmod(0o644)
    with pytest.raises(DecisionProfileError, match='decision_store_unsafe'):
        store.active()
    path.chmod(0o600)
    os.link(path, tmp_path / 'synthetic-hardlink')
    with pytest.raises(DecisionProfileError, match='decision_store_unsafe'):
        store.active()


def test_root_and_ancestor_symlinks_relative_root_and_public_directory_rejected(tmp_path):
    root = tmp_path.resolve()
    target = root / 'private'
    target.mkdir(mode=0o700)
    alias = root / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    for candidate in (alias, alias / 'child'):
        with pytest.raises(DecisionProfileError):
            DecisionProfiles(candidate)
    assert not (target / 'child').exists()
    with pytest.raises(DecisionProfileError, match='decision_store_root_invalid'):
        DecisionProfiles(Path('relative'))
    with pytest.raises(DecisionProfileError, match='decision_store_root_invalid'):
        DecisionProfiles(Path('/'))
    public = root / 'public'
    public.mkdir()
    public.chmod(0o755)
    with pytest.raises(DecisionProfileError, match='decision_store_unsafe'):
        DecisionProfiles(public)


def test_replace_failure_leaves_old_atomic_file_and_active(service, monkeypatch):
    store, _ = service
    old = store.add_draft(profile())
    store.validate_draft(old)
    store.activate(old)
    path = store.root / 'decision-profiles.json'
    before = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(DecisionProfileError, match='decision_store_io_failed'):
        store.add_draft(profile('jev'))
    assert path.read_bytes() == before and store.active() == (old, profile())
    assert not list(store.root.glob('*.tmp'))


@pytest.mark.parametrize('existing_lock', [False, True])
def test_concurrent_distinct_service_instances_do_not_lose_drafts(service, existing_lock):
    store, fake = service
    before = 0
    if existing_lock:
        store.add_draft(profile())
        before = 1
    gate = Barrier(4)
    def add(index):
        other = DecisionProfiles(store.root, post=fake)
        gate.wait(timeout=10)
        return other.add_draft(replace(profile(), model=f'synthetic-model-{index}'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(add, range(12)))
    assert len(set(identities)) == 12
    assert len(json.loads((store.root / 'decision-profiles.json').read_text())['profiles']) == 12 + before
    assert store.active() is None


def test_losing_exclusive_creator_reopens_existing_lock_without_weakening_flags(service, monkeypatch):
    store, _ = service
    original_open = os.open
    attempts = []
    def competing_open(path, flags, *args, **kwargs):
        if path == '.decision-profiles.lock':
            attempts.append(flags)
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            if len(attempts) == 1:
                raise FileNotFoundError(2, 'synthetic initial absence')
            if flags & os.O_CREAT:
                assert flags & os.O_EXCL
                # Another process wins creation before this O_EXCL attempt.
                winner = original_open(path, flags, *args, **kwargs)
                os.close(winner)
                raise FileExistsError(17, 'synthetic competing creator')
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', competing_open)
    identity = store.add_draft(profile())
    assert store.get(identity) == profile()
    assert len(attempts) >= 3
    assert not attempts[0] & os.O_CREAT and not attempts[2] & os.O_CREAT


@pytest.mark.parametrize('replacement', ['symlink', 'regular'])
def test_lock_replaced_while_waiting_is_rejected_before_writing(service, tmp_path, monkeypatch, replacement):
    import fcntl
    store, _ = service
    target = tmp_path / 'synthetic-lock-target'
    target.write_text('synthetic unchanged target')
    target.chmod(0o600)
    original_flock = fcntl.flock
    def swap_after_acquisition(fd, operation):
        original_flock(fd, operation)
        lock = store.root / '.decision-profiles.lock'
        lock.rename(store.root / 'synthetic-retained-lock')
        if replacement == 'symlink':
            lock.symlink_to(target)
        else:
            lock.write_text('synthetic replacement lock')
            lock.chmod(0o600)
    monkeypatch.setattr(fcntl, 'flock', swap_after_acquisition)
    with pytest.raises(DecisionProfileError, match='decision_store_unsafe'):
        store.add_draft(profile())
    assert not (store.root / 'decision-profiles.json').exists()
    assert target.read_text() == 'synthetic unchanged target'


def test_separate_processes_share_the_same_lock_and_preserve_all_drafts(service):
    store, _ = service
    program = '''
import sys, time
from pathlib import Path
from knowledge_distiller.v1.decision_client import DecisionProfile
from knowledge_distiller.v1.decision_profiles import DecisionProfiles
store = DecisionProfiles(Path(sys.argv[1]))
profile = DecisionProfile('clef', 'http://127.0.0.1:8198/v1/systemone', 'synthetic-process')
for _ in range(8):
    store.add_draft(profile)
    time.sleep(.01)
'''
    environment = {'PATH': '/usr/bin:/bin', 'TZ': 'Asia/Taipei',
                   'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src'),
                   'TMPDIR': str(store.root.parent)}
    workers = [subprocess.Popen([sys.executable, '-c', program, str(store.root)],
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for _ in range(3)]
    try:
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stderr
            assert stdout == ''
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()
    state = json.loads((store.root / 'decision-profiles.json').read_text())
    assert len(state['profiles']) == 24 and state['active'] is None


def test_missing_identity_has_stable_code(service):
    store, _ = service
    for action in (lambda: store.get('missing'), lambda: store.activate('missing'),
                   lambda: store.validate_draft('missing')):
        with pytest.raises(DecisionProfileError, match='decision_profile_missing'):
            action()


def test_echoed_requested_model_cannot_hide_wrong_served_model_and_old_active_survives(service):
    store, fake = service
    old = store.add_draft(profile('jev'))
    store.validate_draft(old)
    store.activate(old)
    new = store.add_draft(profile())
    # POST, if reached, is a valid SystemOne answer echoing the requested
    # clef-4bit. /health correctly says a different model is actually served.
    fake.served_model = 'clef-flash-4bit'
    calls_before = len(fake.calls)
    with pytest.raises(DecisionError, match='^decision_model_mismatch$'):
        store.validate_draft(new)
    assert len(fake.calls) == calls_before  # Rejected before the misleading echo.
    assert store.active() == (old, profile('jev'))
    with pytest.raises(DecisionProfileError, match='decision_profile_unvalidated'):
        store.activate(new)
    fake.served_model = 'clef-4bit'
    store.validate_draft(new)
    assert store.active() == (old, profile('jev'))
    validation = json.loads((store.root / 'decision-profiles.json').read_text())['profiles'][new]['validation']
    assert validation['served_model'] == 'clef-4bit'


def test_local_probe_requires_explicit_get_transport_never_auto_contacts_service(service):
    original, fake = service
    store = DecisionProfiles(original.root, post=fake)
    identity = store.add_draft(profile())
    with pytest.raises(DecisionError, match='decision_probe_transport_required'):
        store.validate_draft(identity)
    assert fake.calls == [] and fake.health_calls == [] and store.active() is None


@pytest.mark.parametrize('status,code', [(302, 'decision_redirect_refused'),
                                       (500, 'decision_request_failed'), (401, 'decision_unauthorized')])
def test_health_failure_cannot_validate_draft(service, status, code):
    store, fake = service
    identity = store.add_draft(profile())
    fake.health_status = status
    with pytest.raises(DecisionError, match=code):
        store.validate_draft(identity)
    assert not fake.calls
    with pytest.raises(DecisionProfileError, match='decision_profile_unvalidated'):
        store.activate(identity)
