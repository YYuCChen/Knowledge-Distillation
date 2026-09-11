import json
import subprocess
from pathlib import Path

import pytest

from knowledge_distiller.v1 import codex
from knowledge_distiller.v1.llm import LLMRequestError
from knowledge_distiller.v1.settings import SettingsError, SettingsService
from knowledge_distiller.v1.store import Store


MODELS = [{'model': 'chosen-model', 'label': 'Chosen', 'efforts': ['low', 'high'],
           'default_effort': 'low', 'default': True, 'modalities': ['text', 'image'], 'fast_supported': True}]


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'knowledge.sqlite3')
    result.initialize()
    result.set_settings({'llm_provider': 'anthropic', 'llm_model': 'old-model',
                         'llm_base_url': 'https://example.com', 'llm_secret_account': 'old',
                         'llm_state': 'configured'})
    return result


class Client:
    calls = []
    fail = False

    def __init__(self, model, effort='', mark_unavailable=lambda: None, service_tier=''):
        self.model, self.effort, self.service_tier = model, effort, service_tier

    def complete(self, **kwargs):
        self.calls.append((self.model, self.effort))
        if self.fail:
            raise LLMRequestError('llm_request_failed')
        return '{"connected":true}'


def test_discovery_does_not_activate_or_read_keys(store):
    service = SettingsService(store, codex_probe=lambda: MODELS, codex_client=Client)
    service.refresh_codex()
    assert store.setting('llm_model') == 'old-model'
    assert service.view()['llm']['codex_models'] == MODELS


def test_activate_is_atomic_and_runtime_uses_selected_binding(store, monkeypatch):
    service = SettingsService(store, codex_probe=lambda: MODELS, codex_client=Client)
    monkeypatch.setattr(Client, 'fail', True)
    with pytest.raises(SettingsError, match='codex_connection_failed'):
        service.activate_codex('chosen-model', 'high')
    assert store.setting('llm_model') == 'old-model'
    assert store.setting('llm_provider') == 'anthropic'
    monkeypatch.setattr(Client, 'fail', False)
    service.activate_codex('chosen-model', 'high')
    assert service.view()['llm']['provider'] == 'Codex'
    client = service.llm_client()
    assert (client.model, client.effort) == ('chosen-model', 'high')
    assert store.setting('llm_secret_account') == 'old'


def test_custom_model_does_not_guess_efforts(store):
    service = SettingsService(store, codex_probe=lambda: MODELS, codex_client=Client)
    with pytest.raises(SettingsError, match='codex_effort_invalid'):
        service.activate_codex('custom-model', 'high')
    with pytest.raises(SettingsError, match='codex_effort_invalid'):
        service.activate_codex('chosen-model', 'ultra')
    service.activate_codex('custom-model', '')
    assert store.setting('llm_model') == 'custom-model'


def test_exec_uses_subscription_and_translates_images_without_credentials(monkeypatch):
    monkeypatch.setattr(codex, 'subscription_models', lambda: MODELS)
    monkeypatch.setattr(codex, 'executable', lambda: '/codex')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-inherit')
    monkeypatch.setenv('CODEX_API_KEY', 'must-not-inherit')
    paths = []
    def run(command, **kwargs):
        assert '--ignore-user-config' in command and '--ephemeral' in command
        assert 'forced_login_method="chatgpt"' in command
        assert 'model_provider="knowledge_subscription"' in command
        assert 'model_providers.knowledge_subscription.requires_openai_auth=true' in command
        assert 'model_providers.knowledge_subscription.supports_websockets=false' in command
        assert not any('base_url=' in argument or 'env_key=' in argument for argument in command)
        assert '--model' in command and 'chosen-model' in command
        assert 'model_reasoning_effort="high"' in command
        assert 'OPENAI_API_KEY' not in kwargs['env'] and 'CODEX_API_KEY' not in kwargs['env']
        image = Path(command[command.index('--image')+1])
        paths.append(image)
        assert image.read_bytes() == b'image'
        assert 'member_id=pic1' in kwargs['input']
        Path(command[command.index('-o')+1]).write_text('{"result":"ok"}')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(codex.subprocess, 'run', run)
    value = codex.CodexSubscriptionClient('chosen-model', 'high').complete(system='instructions',
        user=[{'type':'text','text':'member_id=pic1'}, {'type':'image','source':
              {'type':'base64','media_type':'image/png','data':'aW1hZ2U='}}], max_tokens=100)
    assert json.loads(value) == {'result':'ok'}
    assert not paths[0].exists()


@pytest.mark.parametrize('failure,unavailable', [('llm_request_failed',False), ('llm_config_unavailable',True)])
def test_transient_probe_does_not_invalidate_binding(monkeypatch, failure, unavailable):
    def probe():
        raise LLMRequestError(failure)
    monkeypatch.setattr(codex, 'subscription_models', probe)
    marked=[]
    with pytest.raises(LLMRequestError, match=failure):
        codex.CodexSubscriptionClient('chosen-model', mark_unavailable=lambda: marked.append(True)).complete(
            system='',user='',max_tokens=1)
    assert bool(marked) is unavailable


def test_timeout_never_returns_partial_result(monkeypatch):
    monkeypatch.setattr(codex, 'subscription_models', lambda: MODELS)
    monkeypatch.setattr(codex, 'executable', lambda: '/codex')
    def run(command, **kwargs):
        assert kwargs['timeout'] == 900
        Path(command[command.index('-o')+1]).write_text('partial')
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(codex.subprocess, 'run', run)
    with pytest.raises(LLMRequestError, match='llm_request_timeout'):
        codex.CodexSubscriptionClient('chosen-model').complete(system='',user='',max_tokens=1)


def test_fast_binding_is_atomic_persists_and_can_be_disabled(store, monkeypatch):
    service = SettingsService(store, codex_probe=lambda: MODELS, codex_client=Client)
    monkeypatch.setattr(Client, 'fail', True)
    with pytest.raises(SettingsError):
        service.activate_codex('chosen-model', 'high', 'fast')
    assert store.setting('llm_service_tier') is None
    monkeypatch.setattr(Client, 'fail', False)
    service.activate_codex('chosen-model', 'high', 'fast')
    assert service.llm_client().service_tier == 'fast'
    assert service.view()['llm']['service_tier'] == 'fast'
    service.activate_codex('chosen-model', 'high', '')
    assert service.llm_client().service_tier == ''
    with pytest.raises(SettingsError, match='codex_fast_unavailable'):
        service.activate_codex('custom-model', '', 'fast')
    with pytest.raises(SettingsError, match='codex_fast_unavailable'):
        service.activate_codex('chosen-model', 'high', 'ultrafast')


@pytest.mark.parametrize('tier', ['', 'fast'])
def test_exec_fast_is_independent_of_reasoning(monkeypatch, tier):
    monkeypatch.setattr(codex, 'subscription_models', lambda: MODELS)
    monkeypatch.setattr(codex, 'executable', lambda: '/codex')
    def run(command, **kwargs):
        assert 'model_reasoning_effort="high"' in command
        assert ('service_tier="fast"' in command) == bool(tier)
        Path(command[command.index('-o')+1]).write_text('{}')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(codex.subprocess, 'run', run)
    codex.CodexSubscriptionClient('chosen-model', 'high', service_tier=tier).complete(
        system='', user='', max_tokens=1)


def test_exec_rejects_fast_when_model_no_longer_advertises_it(monkeypatch):
    monkeypatch.setattr(codex, 'subscription_models', lambda: [dict(MODELS[0], fast_supported=False)])
    with pytest.raises(LLMRequestError, match='llm_fast_unavailable'):
        codex.CodexSubscriptionClient('chosen-model', service_tier='fast').complete(
            system='', user='', max_tokens=1)


def test_diagnostics_allowlist_usage_and_report_unenforced_budget(monkeypatch, caplog):
    monkeypatch.setattr(codex, 'subscription_models', lambda: MODELS)
    monkeypatch.setattr(codex, 'executable', lambda: '/codex')
    def run(command, **kwargs):
        assert '--json' in command
        assert 'features.plugins=false' in command
        assert 'approval_policy="never"' in command
        assert 'skills.max_context_tokens=1' in command
        Path(command[command.index('-o')+1]).write_text('{}')
        events = [{'type':'item.completed','item':{'text':'PRIVATE OUTPUT'}},
            {'type':'turn.completed','usage':{'input_tokens':30,'output_tokens':40,
                'reasoning_output_tokens':35,'private':'PRIVATE USAGE'}}]
        return subprocess.CompletedProcess(command, 0, '\n'.join(map(json.dumps, events)), 'PRIVATE ERROR')
    monkeypatch.setattr(codex.subprocess, 'run', run)
    with caplog.at_level('INFO'):
        codex.CodexSubscriptionClient('chosen-model','high').complete(
            system='PRIVATE PROMPT',user='PRIVATE SOURCE',max_tokens=32)
    assert 'PRIVATE' not in caplog.text
    data = json.loads(caplog.records[-1].message.removeprefix('codex_call '))
    assert data['usage'] == {'input_tokens':30,'output_tokens':40,'reasoning_output_tokens':35}
    assert data['max_tokens_enforced'] is False
    assert data['status'] == 'completed'


def test_usage_ignores_malformed_events_and_non_numeric_counters():
    assert codex._usage('broken\n[]\n' + json.dumps({'type':'turn.completed',
        'usage':{'output_tokens':True, 'input_tokens':-2}})) == {}


def test_transport_diagnostics_keep_counts_without_error_content():
    raw = b'PRIVATE stream disconnected - retrying sampling request (1/5) idle timeout waiting for websocket'
    assert codex._transport_diagnostics(raw) == {'stream_retry_count':1,'websocket_idle_timeout':True}
    assert codex._transport_diagnostics(None) == {'stream_retry_count':0,'websocket_idle_timeout':False}
