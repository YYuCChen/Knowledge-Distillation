import pytest
from knowledge_distiller.v1.chrome import ChromeSessionError
from knowledge_distiller.v1.foreground_session import PlatformForegroundSession
from knowledge_distiller.v1.settings import SettingsService
from knowledge_distiller.v1.store import Store
from .test_zhihu_foreground import Owned


def test_foreground_xhs_connection_and_failed_reconnect_preserve_authority(tmp_path):
    store=Store(tmp_path/'test.sqlite3');store.initialize()
    owned=Owned();calls=[]
    def reader(*args):
        calls.append(args)
        return {'loggedIn':True,'contextId':'daily','note':{'title':'native'}}
    session=PlatformForegroundSession(store,tmp_path/'profiles','xiaohongshu',owned=owned,reader=reader)
    settings=SettingsService(store,xiaohongshu=session)
    old='owned:'+'a'*32
    store.save_connection('xiaohongshu',None,browser_context=old)
    assert session.read('url',old)=={'contextId':old}
    settings.connect_xiaohongshu()
    assert calls[0]==('xiaohongshu','xiaohongshu','https://www.xiaohongshu.com/explore',None)
    result=session.read('note','foreground:daily')
    assert result['contextId']=='foreground:daily'
    before=dict(store.connection('xiaohongshu'))
    def fail(*args):raise ChromeSessionError('xiaohongshu_security_restricted')
    session.reader=fail
    with pytest.raises(ChromeSessionError,match='security_restricted'):settings.connect_xiaohongshu()
    assert dict(store.connection('xiaohongshu'))==before
    def stale(*args):
        store.save_connection('xiaohongshu',None,browser_context='foreground:daily')
        return {'contextId':'daily'}
    session.reader=stale
    with pytest.raises(ChromeSessionError,match='connection_changed'):session.read('note')
