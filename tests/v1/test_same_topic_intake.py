import json
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1.collections import Collections
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.douyin_collections import CollectionError
from knowledge_distiller.v1.same_topic_discovery import CAPTURE_ON_PROCESSING
from knowledge_distiller.v1.source_versions import SourceVersionError
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.xiaohongshu import CapturedNote


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'isolated.sqlite3')
    value.initialize()
    for platform in ['youtube', 'x', 'zhihu', 'weibo', 'xiaohongshu']:
        value.save_connection(platform, None, browser_context='context')
    return value


def accept(c, urls):
    preview = c.preview_same_topic(urls, durable=True)
    operation = c.confirm(preview['token'], [s.signature for s in preview['scopes']], same_topic=True)[0]
    return operation, preview


@pytest.mark.parametrize('urls,platform', [
    (['https://youtu.be/aaaaaaaaaaa', 'https://youtu.be/abcdefghijk'], 'youtube'),
    (['https://x.com/person/status/101', 'https://twitter.com/other/status/102'], 'x'),
    (['https://www.zhihu.com/question/1/answer/101', 'https://zhuanlan.zhihu.com/p/102'], 'zhihu'),
])
def test_same_platform_explicit_leaves_persist_authority_and_do_not_assume_content_version(store, urls, platform):
    c = Collections(store)
    operation, preview = accept(c, urls)
    info = c.detail(operation)
    assert len(info['members']) == 2 and info['manifest']['authority']['platform'] == platform
    assert all(m['native_version'] == CAPTURE_ON_PROCESSING for m in info['members'])
    for member in info['members']:
        row = store.item_bundle(member['item_id'])
        assert json.loads(row['platform_authority_json'])['class'] == 'EXISTING_BROWSER_OWNED'
    assert c.confirm(preview['token'], [], same_topic=True) == [operation]
    # A new explicit capture cannot silently reuse a former unknown content version.
    assert accept(c, urls)[0] != operation


@pytest.mark.parametrize('urls', [
    ['https://x.com/a/status/101', 'https://youtu.be/abcdefghijk'],
    ['https://x.com/a/status/101', 'https://twitter.com/b/status/101'],
    ['https://youtube.com/playlist?list=PL1', 'https://youtu.be/abcdefghijk'],
    ['https://x.com/a', 'https://x.com/a/status/101'],
])
def test_rejects_cross_platform_duplicate_identity_and_nonleaf_without_queue(store, urls):
    with pytest.raises((ValueError, CollectionError)):
        Collections(store).preview_same_topic(urls)
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM distill_items').fetchone()[0] == 0


def test_first_capture_pins_snapshot_before_sourcefact_and_keeps_original(store):
    c = Collections(store)
    op, _ = accept(c, ['https://x.com/a/status/101', 'https://x.com/a/status/102'])
    item = c.detail(op)['members'][0]['item_id']
    row = store.item_bundle(item)
    metadata = {'source_title': '', 'original_description': 'original', 'media_members': [],
                'session_authority': json.loads(row['platform_authority_json'])}
    source = CapturedNote('101', row['submitted_url'], row['submitted_url'], metadata, (), source_kind='x')
    material_id = store.attach_material(item, source)
    assert store.attach_material(item, source) == material_id
    changed = CapturedNote('101', row['submitted_url'], row['submitted_url'],
                           {**metadata, 'original_description': 'changed'}, (), source_kind='x')
    with pytest.raises(SourceVersionError, match='source_snapshot_changed'):
        store.attach_material(item, changed)
    wrong = CapturedNote('999', row['submitted_url'], row['submitted_url'], metadata, (), source_kind='x')
    with pytest.raises(SourceVersionError, match='collection_member_changed'):
        store.attach_material(item, wrong)
    assert store.item_bundle(item)['material_id'] == material_id


def test_connection_change_requires_preview_again(store):
    c = Collections(store)
    p = c.preview_same_topic(['https://x.com/a/status/101', 'https://x.com/a/status/102'])
    store.save_connection('x', None, browser_context='different')
    from knowledge_distiller.v1.collections import PreviewChanged
    with pytest.raises(PreviewChanged):
        c.confirm(p['token'], [p['scopes'][0].signature], same_topic=True)


def test_xhs_shortlinks_and_weibo_native_aliases_resolve_before_membership(store, monkeypatch):
    from knowledge_distiller.v1 import xiaohongshu as xhs, weibo
    monkeypatch.setattr(xhs.XiaohongshuSession, 'read', lambda self, url, context: {
        'contextId': context, 'pageUrl': 'https://www.xiaohongshu.com/explore/' + ('a' if url.endswith('/one') else 'b') * 24})
    monkeypatch.setattr(xhs, 'qualify_note', lambda state, key: ({}, []))
    op, _ = accept(Collections(store), ['https://xhslink.com/one', 'https://xhslink.com/two'])
    assert [m['native_id'] for m in Collections(store).detail(op)['members']] == ['a' * 24, 'b' * 24]
    monkeypatch.setattr(weibo.WeiboSession, 'read', lambda self, url, context: {'contextId': context})
    monkeypatch.setattr(weibo, 'qualify_weibo', lambda state, key, uid: ({}, '101' if key == 'abc' else '102', '正文', 'https://m.weibo.cn/detail/' + ('101' if key == 'abc' else '102')))
    op, _ = accept(Collections(store), ['https://weibo.com/123/abc', 'https://weibo.com/123/def'])
    assert [m['native_id'] for m in Collections(store).detail(op)['members']] == ['101', '102']


def test_bilibili_groups_only_leaf_previews_and_retains_native_versions(store):
    from knowledge_distiller.v1.bilibili import BilibiliMember
    from knowledge_distiller.v1.douyin_collections import Scope
    def discover(urls, selected=None):
        return {'scopes': [Scope('bilibili_range', str(n), url, None,
            (BilibiliMember(str(n), url, True, 0, 'native-' + str(n), url),), '',
            {'platform': 'bilibili', 'class': 'PUBLIC'}) for n, url in enumerate(urls)], 'choices': []}
    c = Collections(store, SimpleNamespace(discover=discover))
    op, _ = accept(c, ['https://www.bilibili.com/video/BV111', 'https://www.bilibili.com/video/BV222'])
    assert [m['native_version'] for m in c.detail(op)['members']] == ['native-0', 'native-1']


def test_real_bilibili_discovery_rejects_range_mixed_with_leaf(store):
    from knowledge_distiller.v1.bilibili import BilibiliDiscovery
    from .test_bilibili import video, playlist, KEY
    discovery = BilibiliDiscovery(downloader=lambda url: (playlist(video(), video(KEY + '_p2')), None))
    with pytest.raises(CollectionError, match='单个视频'):
        Collections(store, discovery).preview_same_topic([
            'https://www.bilibili.com/video/' + KEY, 'https://www.bilibili.com/video/' + KEY + '?p=2'])


@pytest.mark.parametrize('platform,urls', [
    ('x', ['https://x.com/a/status/101', 'https://x.com/a/status/102']),
    ('youtube', ['https://youtu.be/aaaaaaaaaaa', 'https://youtu.be/abcdefghijk']),
    ('zhihu', ['https://www.zhihu.com/question/1/answer/101', 'https://zhuanlan.zhihu.com/p/102']),
    ('xiaohongshu', ['https://xhslink.com/one', 'https://xhslink.com/two']),
    ('weibo', ['https://weibo.com/123/101', 'https://weibo.com/123/102']),
    ('douyin', ['https://www.douyin.com/video/101', 'https://www.douyin.com/video/102']),
    ('bilibili', ['https://www.bilibili.com/video/BV1T1bQ6vEDt', 'https://www.bilibili.com/video/BV1T1bQ6vEDt?p=2']),
])
def test_feishu_same_platform_survives_preview_restart_and_confirmation(store, monkeypatch, platform, urls):
    from .test_feishu_inbox import message
    from knowledge_distiller.v1.feishu_inbox import FeishuInbox, history_message
    from knowledge_distiller.v1.feishu_intake import FeishuIntake
    from knowledge_distiller.v1.feishu_scopes import request, items
    from knowledge_distiller.v1.feishu_cards import FeishuCards
    inbox = FeishuInbox(store, 'app-new')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=100000)
    raw = message(text='@_user_1 ' + '\n'.join(urls))
    raw['mentions'] = [{'key':'@_user_1', 'id':'ou_bot', 'id_type':'open_id'}]
    inbox.receive(history_message(raw), history=True)
    discovery = None
    if platform == 'xiaohongshu':
        from knowledge_distiller.v1 import xiaohongshu as mod
        monkeypatch.setattr(mod.XiaohongshuSession, 'read', lambda self, url, ctx: {'contextId':ctx, 'pageUrl':'https://www.xiaohongshu.com/explore/' + ('a' if url.endswith('one') else 'b') * 24})
        monkeypatch.setattr(mod, 'qualify_note', lambda state, key: ({'title':'图文笔记'}, []))
    elif platform == 'weibo':
        from knowledge_distiller.v1 import weibo as mod
        monkeypatch.setattr(mod.WeiboSession, 'read', lambda self, url, ctx: {'contextId':ctx})
        monkeypatch.setattr(mod, 'qualify_weibo', lambda state, key, uid: ({}, key, '微博正文', 'https://m.weibo.cn/detail/' + key))
    elif platform == 'bilibili':
        from knowledge_distiller.v1.bilibili import BilibiliDiscovery
        from .test_bilibili import video, KEY
        discovery = BilibiliDiscovery(downloader=lambda url: (video(KEY + '_p2' if '?p=2' in url else KEY), None))
    elif platform == 'douyin':
        from knowledge_distiller.v1.douyin_collections import Scope, Member, connection_authority
        store.save_connection('douyin', None)
        scope = Scope('same_topic', 'works', '抖音内容', None, tuple(Member(str(n), '作品', True, 0, 'v1') for n in [101,102]), '', connection_authority(store))
        discovery = SimpleNamespace(discover=lambda urls: {'scopes':[scope], 'choices':[]})
    c = Collections(store, discovery)
    intake = FeishuIntake(inbox, SimpleNamespace(collections=c))
    assert intake.process('om_1') == 'waiting_input'
    command = FeishuCards(inbox, None, None, c).card('om_1')['body']['elements'][-1]['behaviors'][0]['value']
    c = Collections(store, discovery)
    request(inbox, 'om_1', command)
    intake.links.collections = c
    assert intake.process('om_1') == 'accepted'
    assert len(items(inbox, 'om_1')) == 2
    assert intake.process('om_1') == 'accepted'
    assert len(c.list()) == 1


@pytest.mark.parametrize('platform', ['xiaohongshu', 'weibo', 'bilibili'])
def test_native_preview_failures_keep_source_error_code(store, monkeypatch, platform):
    if platform == 'bilibili':
        from knowledge_distiller.v1.bilibili import BilibiliSourceError
        def fail(urls):
            raise BilibiliSourceError('bilibili_timeout')
        discovery = SimpleNamespace(discover=fail)
        urls = ['https://www.bilibili.com/video/BV1T1bQ6vEDt', 'https://www.bilibili.com/video/BV1T1bQ6vEDt?p=2']
        code = 'bilibili_timeout'
    else:
        from knowledge_distiller.v1 import xiaohongshu, weibo
        mod = xiaohongshu if platform == 'xiaohongshu' else weibo
        error_type = mod.XiaohongshuSourceError if platform == 'xiaohongshu' else mod.WeiboSourceError
        code = platform + '_snapshot_unknown'
        def fail(*args):
            raise error_type(code)
        monkeypatch.setattr(mod.XiaohongshuSession if platform == 'xiaohongshu' else mod.WeiboSession, 'read', fail)
        discovery = None
        urls = ['https://xhslink.com/one', 'https://xhslink.com/two'] if platform == 'xiaohongshu' else ['https://weibo.com/123/101', 'https://weibo.com/123/102']
    with pytest.raises(CollectionError, match=code):
        Collections(store, discovery).preview_same_topic(urls)


def test_feishu_discovery_failure_is_durable_and_does_not_leave_received_loop(store):
    from .test_feishu_inbox import message
    from knowledge_distiller.v1.feishu_inbox import FeishuInbox, history_message
    from knowledge_distiller.v1.feishu_intake import FeishuIntake
    inbox = FeishuInbox(store, 'app-new')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=100000)
    raw = message(text='@_user_1 https://x.com/a/status/101\nhttps://x.com/a/status/102')
    raw['mentions'] = [{'key':'@_user_1','id':'ou_bot','id_type':'open_id'}]
    inbox.receive(history_message(raw), history=True)
    def fail(*args, **kwargs):
        raise CollectionError('collection_connection_changed')
    service = SimpleNamespace(preview_same_topic=fail)
    intake = FeishuIntake(inbox, SimpleNamespace(collections=service, errors={'collection_connection_changed':'来源连接已变化'}))
    assert intake.process('om_1') == 'needs_desktop'
    assert intake.process('om_1') == 'needs_desktop'
    with connect(store.path) as db:
        assert db.execute('SELECT error FROM feishu_parts').fetchone()[0] == '来源连接已变化'
        assert db.execute('SELECT count(*) FROM distill_items').fetchone()[0] == 0
