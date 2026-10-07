from unittest.mock import Mock
import threading
import pytest
from .test_feishu_inbox import inbox
from knowledge_distiller.v1.feishu_service import FeishuService
from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate


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


def test_configuration_does_not_replace_a_runtime_that_failed_to_stop(
    inbox, monkeypatch, tmp_path
):
    credential = Mock()
    api = Mock()
    api.bot_info.return_value = {'open_id': inbox.binding()['bot_open_id']}
    backend = Mock(return_value=credential)
    monkeypatch.setattr('knowledge_distiller.v1.local_secrets.LocalSecrets', lambda *a: backend)
    monkeypatch.setattr('knowledge_distiller.v1.feishu_service.FeishuAPI', lambda *a, **k: api)
    service = FeishuService(inbox.store, None, None, tmp_path)
    service.stop = Mock(return_value=False)
    service.start = Mock()

    with pytest.raises(ValueError, match='feishu_configuration_failed'):
        service.configure(inbox.app_id, 'new-secret')

    credential.save_validated.assert_not_called()
    service.start.assert_not_called()


@pytest.mark.parametrize("later_action", ["reserve", "stop"])
def test_timed_out_stop_recovery_does_not_restart_after_later_lifecycle_intent(
    inbox, tmp_path, later_action
):
    gate = WorkAdmissionGate()
    service = FeishuService(inbox.store, None, None, tmp_path, admission_gate=gate)
    entered = threading.Event()
    release = threading.Event()

    class Runtime:
        def stop(self, timeout=15):
            if threading.current_thread().name == 'feishu-stop-recovery':
                entered.set()
                assert release.wait(5)
            return True

    service.runtime = Runtime()
    service.api = Mock()
    service.start = Mock(return_value=True)
    recovery = service.recover_after_failed_stop()
    assert entered.wait(2)
    if later_action == "reserve":
        assert gate.reserve() is True
    else:
        assert service.stop() is True
    release.set()
    recovery.join(2)
    assert not recovery.is_alive()
    service.start.assert_not_called()
    if later_action == "reserve":
        assert service.runtime is None
        gate.release_reservation()


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
