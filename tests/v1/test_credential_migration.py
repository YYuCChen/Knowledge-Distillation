import sqlite3
import pytest
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.local_secrets import LocalSecrets
from knowledge_distiller.v1.credential_migration import accounts,migrate
from knowledge_distiller.v1.keychain import KeychainError


def test_dry_run_never_reads_keychain_or_changes_old_database(tmp_path):
    store=Store(tmp_path/'old.sqlite3');store.initialize()
    store.set_setting('llm_secret_account','saved-model')
    with sqlite3.connect(store.path) as db:db.execute('PRAGMA user_version=16')
    before=store.path.read_bytes()
    result=migrate(store.path,tmp_path/'credentials',legacy_factory=lambda _:pytest.fail('Keychain read'))
    assert result=={'planned':1,'imported':0,'already_local':0,'failed':0,'executed':False}
    assert store.path.read_bytes()==before
    assert not (tmp_path/'credentials').exists()


def test_partial_import_resumes_without_overwriting_or_deleting_legacy(tmp_path):
    store=Store(tmp_path/'old.sqlite3');store.initialize()
    store.set_settings({'llm_secret_account':'a','llm_draft_secret_account':'b','seed_draft_api_account':'a'})
    before=store.path.read_bytes();calls=[];fail=[True]
    class Legacy:
        def __init__(self,account):self.account=account
        def load(self):
            calls.append(self.account)
            if self.account=='b' and fail[0]:raise KeychainError('must not expose secret')
            return 'fake-'+self.account
        def clear(self):pytest.fail('legacy must remain')
    first=migrate(store.path,tmp_path/'credentials',execute=True,legacy_factory=Legacy)
    assert first['imported']==1 and first['failed']==1 and first['planned']==2
    LocalSecrets(tmp_path/'credentials')('a').save('new-local-value')
    fail[0]=False
    second=migrate(store.path,tmp_path/'credentials',execute=True,legacy_factory=Legacy)
    assert second['imported']==1 and second['already_local']==1 and second['failed']==0
    assert calls==['a','b','b']
    assert LocalSecrets(tmp_path/'credentials')('a').load()=='new-local-value'
    assert store.path.read_bytes()==before
    assert 'secret' not in repr(first)


def test_inventory_includes_owned_sessions_and_bound_or_pairing_bots(tmp_path):
    store=Store(tmp_path/'old.sqlite3');store.initialize()
    store.set_setting('feishu_pairing','{"app_id":"pairing-app"}')
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO source_connections VALUES ('douyin','connected',1,NULL,'now','owned:session-id')")
        db.execute("INSERT INTO source_connections VALUES ('weibo','connected',1,NULL,'now','external-context')")
        db.execute("INSERT INTO feishu_binding VALUES ('bound-app','bot','user','chat',0,0)")
    assert accounts(store.path)==['douyin-session-session-id','feishu-app-bound-app','feishu-app-pairing-app']
