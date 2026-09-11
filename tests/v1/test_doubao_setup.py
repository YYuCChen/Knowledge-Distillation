import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge_distiller.v1 import doubao_setup as setup
from knowledge_distiller.v1.settings import SettingsService
from knowledge_distiller.v1.store import Store
from .test_settings import Secrets


@pytest.fixture
def service(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize()
    return SettingsService(store,keychain_factory=Secrets().factory,qwen_probe=lambda:True)


def test_discovery_reads_metadata_only_and_does_not_trust_endpoint():
    client=Mock()
    client.list_buckets.return_value=SimpleNamespace(buckets=[SimpleNamespace(name='my-audio',location='cn-shanghai',extranet_endpoint='https://untrusted.invalid')])
    factory=Mock(return_value=client)
    assert setup.discover(' ak ', ' sk ',factory=factory)==[{'name':'my-audio','region':'cn-shanghai'}]
    assert factory.call_args.args[2]=='https://tos-cn-beijing.volces.com'
    assert [c[0] for c in client.method_calls]==['list_buckets','close']


@pytest.mark.parametrize('code,status,expected',[
    ('InvalidAccessKeyId',403,'doubao_storage_credentials_invalid'),
    ('SignatureDoesNotMatch',403,'doubao_storage_credentials_invalid'),
    ('AccessDenied',403,'doubao_storage_list_denied'),
    ('RequestTimeTooSkewed',400,'doubao_storage_clock_invalid'),
    ('',None,'doubao_storage_unavailable')])
def test_errors_are_actionable_without_private_response(code,status,expected):
    client=Mock();error=RuntimeError('PRIVATE remote contents')
    error.code=code;error.status_code=status
    client.list_buckets.side_effect=error
    with pytest.raises(setup.SetupError,match=expected) as caught:
        setup.discover('ak','sk',factory=lambda *a,**kw:client)
    assert 'PRIVATE' not in str(caught.value)
    client.close.assert_called_once()


def test_selection_persists_region_keeps_active_and_rejects_stale_list(service,monkeypatch):
    service.save_doubao_key('api')
    service.save_doubao_tos('cn-beijing','old-bucket','old-ak','old-sk')
    service.activate_doubao()
    active=service.store.setting('asr_seed_tos_account')
    monkeypatch.setattr(setup,'discover',lambda *a:[{'name':'new-bucket','region':'cn-shanghai'}])
    assert setup.save_discovery(service,'new-ak','new-sk')
    data=service.view()['asr']
    assert not data['tos_saved']
    token=data['storage_discovery']['id']
    setup.select_bucket(service.store,token,'new-bucket')
    assert service.view()['asr']['region']=='cn-shanghai'
    assert service.store.setting('asr_seed_tos_account')==active
    assert 'new-sk' not in repr(service.store.settings())
    setup.save_discovery(service)
    with pytest.raises(setup.SetupError,match='selection_expired'):
        setup.select_bucket(service.store,token,'new-bucket')
    service.save_doubao_tos('cn-beijing','manual-bucket','manual-ak','manual-sk')
    assert not service.view()['asr']['storage_discovery']


def test_failed_new_discovery_does_not_replace_existing_credentials(service,monkeypatch):
    service.save_doubao_tos('cn-beijing','old-bucket','old-ak','old-sk')
    old=service.store.settings()
    def fail(*a):raise setup.SetupError('doubao_storage_credentials_invalid')
    monkeypatch.setattr(setup,'discover',fail)
    with pytest.raises(setup.SetupError):setup.save_discovery(service,'wrong-ak','wrong-sk')
    assert service.store.settings()==old


def test_empty_account_can_retry_saved_keys_without_marking_storage_ready(service,monkeypatch):
    spy=Mock(return_value=[]);monkeypatch.setattr(setup,'discover',spy)
    assert not setup.save_discovery(service,'ak','sk')
    assert service.view()['asr']['tos_key_saved'] and not service.view()['asr']['tos_saved']
    setup.save_discovery(service)
    assert spy.call_args.args==('ak','sk')


def test_forms_restore_step_and_reject_cross_list_selection(service,monkeypatch):
    from knowledge_distiller.v1.web import create_app
    from .test_settings_web import Distiller
    app=create_app(service.store,Distiller(),service);app.config.update(TESTING=True)
    client=app.test_client()
    service.save_doubao_key('api')
    monkeypatch.setattr(setup,'discover',lambda *a:[{'name':'audio-bucket','region':'cn-beijing'}])
    response=client.post('/settings/asr/storage/discover',data={'access_key':'private-ak','secret_key':'private-sk'})
    assert 'asr_step=3' in response.location
    page=client.get(response.location).text
    assert 'audio-bucket · cn-beijing' in page and 'private-sk' not in page+response.location
    token=service.view()['asr']['storage_discovery']['id']
    response=client.post('/settings/asr/storage/select',data={'discovery_id':'old','bucket':'audio-bucket'})
    assert 'selection_expired' in response.location
    response=client.post('/settings/asr/storage/select',data={'discovery_id':token,'bucket':'audio-bucket'})
    assert 'asr_step=4' in response.location
    assert service.view()['asr']['tos_saved']
    assert service.store.setting('asr_model') is None


def test_async_failure_returns_safe_message_without_redirect_or_credentials(service,monkeypatch):
    from knowledge_distiller.v1.web import create_app
    from .test_settings_web import Distiller
    app=create_app(service.store,Distiller(),service);app.config.update(TESTING=True)
    def denied(*a):raise setup.SetupError('doubao_storage_list_denied')
    monkeypatch.setattr(setup,'discover',denied)
    response=app.test_client().post('/settings/asr/storage/discover',data={'access_key':'private-ak','secret_key':'private-sk'},headers={'Accept':'application/json'})
    assert response.status_code==400
    assert '手动填写' in response.json['error']
    assert 'private' not in response.text
    assert not service.store.setting('seed_draft_tos_account')
