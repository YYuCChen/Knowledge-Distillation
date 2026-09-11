from unittest.mock import Mock
import pytest
from .test_feishu_inbox import inbox
from knowledge_distiller.v1.feishu_service import FeishuService


def test_invalid_credentials_keep_running_connection_and_local_credentials(inbox,monkeypatch,tmp_path):
    credential=Mock()
    api=Mock()
    api.bot_info.return_value={'open_id':'wrong_bot'}
    backend=Mock(); backend.return_value=credential
    monkeypatch.setattr('knowledge_distiller.v1.local_secrets.LocalSecrets',lambda *a:backend)
    monkeypatch.setattr('knowledge_distiller.v1.feishu_service.FeishuAPI',lambda *a,**k:api)
    service=FeishuService(inbox.store,None,None,tmp_path)
    service.stop=Mock();service.start=Mock()
    with pytest.raises(ValueError):service.configure(inbox.app_id,'new-secret')
    credential.save_validated.assert_not_called();service.stop.assert_not_called()
    api.close.assert_called_once()
    api.bot_info.return_value={'open_id':inbox.binding()['bot_open_id']}
    service.configure(inbox.app_id,'new-secret')
    credential.save_validated.assert_called_once_with('new-secret')
    service.stop.assert_called_once();service.start.assert_called_once()
    with pytest.raises(ValueError):service.configure('cli_other','secret')


def test_blank_secret_keeps_existing_credential(inbox,monkeypatch,tmp_path):
    credential=Mock();api=Mock()
    api.bot_info.return_value={'open_id':inbox.binding()['bot_open_id']}
    backend=Mock(); backend.return_value=credential
    monkeypatch.setattr('knowledge_distiller.v1.local_secrets.LocalSecrets',lambda *a:backend)
    monkeypatch.setattr('knowledge_distiller.v1.feishu_service.FeishuAPI',lambda *a,**k:api)
    service=FeishuService(inbox.store,None,None,tmp_path)
    service.stop=Mock();service.start=Mock()
    service.configure(inbox.app_id)
    credential.save_validated.assert_not_called()
    service.start.assert_called_once()


def test_expired_onboarding_retains_app_id_and_can_renew_without_reentering_secret(tmp_path,monkeypatch):
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.feishu_pairing import begin
    store=Store(tmp_path/'new-user.sqlite3');store.initialize()
    initial=begin(store,'cli_new','ou_new')
    monkeypatch.setattr('knowledge_distiller.v1.feishu_pairing.time.time',lambda:initial['expires']+1)
    service=FeishuService(store,None,None,tmp_path)
    status=service.status()
    assert status['state']=='pairing_expired'
    assert status['pairing']['app_id']=='cli_new'
    api=Mock();api.bot_info.return_value={'open_id':'ou_new'}
    credential=Mock()
    backend=Mock(); backend.return_value=credential
    monkeypatch.setattr('knowledge_distiller.v1.local_secrets.LocalSecrets',lambda *a:backend)
    monkeypatch.setattr('knowledge_distiller.v1.feishu_service.FeishuAPI',lambda *a,**kw:api)
    service.start=Mock();service.configure('cli_new')
    assert service.status()['state']=='pairing'
    assert service.status()['pairing']['code']!=initial['code']
    credential.save_validated.assert_not_called()


def test_start_passes_authenticated_api_to_image_intake(inbox,monkeypatch,tmp_path):
    api=Mock(app_id=inbox.app_id);runtime=Mock()
    monkeypatch.setattr('knowledge_distiller.v1.feishu_service.FeishuAPI',lambda *a,**kw:api)
    runtime_factory=Mock(return_value=runtime)
    monkeypatch.setattr('knowledge_distiller.v1.feishu_service.FeishuRuntime',runtime_factory)
    links=Mock()
    service=FeishuService(inbox.store,links,Mock(),tmp_path)
    service.start()
    intake=runtime_factory.call_args.args[2]
    assert intake.api is api
    runtime.start.assert_called_once()
    service.stop()
