"""User policy: supported material URLs win regardless of surrounding prose."""
import pytest
from knowledge_distiller.v1.intake import content_platform, links_in, needs_content_choice
from knowledge_distiller.v1.link_intake import LinkIntake
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.feishu_intake import FeishuIntake
from knowledge_distiller.v1.feishu_inbox import history_message
from knowledge_distiller.v1.database import connect
from .test_feishu_inbox import inbox, message

URLS = [
    ('douyin', 'https://www.douyin.com/video/101'),
    ('xiaohongshu', 'https://www.xiaohongshu.com/explore/123456789012345678901234?xsec_token=a%2Bb&xsec_source=pc_share'),
    ('bilibili', 'https://www.bilibili.com/video/BV1TEST00001/?p=2'),
    ('youtube', 'https://youtu.be/aaaaaaaaaaa?t=12&si=abc'),
    ('weibo', 'https://m.weibo.cn/detail/123'),
    ('zhihu', 'https://www.zhihu.com/question/123/answer/456'),
    ('x', 'https://x.com/user/status/123?s=46'),
]

@pytest.mark.parametrize('platform,url', URLS)
@pytest.mark.parametrize('wrapper', ['新分享口令和任意标题 {} #标签', '我的完整正文，有观点也有结论。参考：{}\n这是结尾。'])
def test_both_entrypoints_route_material_urls_without_content_choice(tmp_path, monkeypatch, inbox, platform, url, wrapper):
    value = wrapper.format(url)
    assert content_platform(url) == platform
    assert not needs_content_choice(value)
    submitted = []
    def submit(self, value, *, receipt_key=None, **kwargs):
        extracted = links_in(value)
        submitted.append(extracted)
        return self.store.create_item(extracted[0], receipt_key=receipt_key)
    monkeypatch.setattr(LinkIntake, 'submit', submit)
    store = Store(tmp_path/'web.sqlite3')
    app = create_app(store, object())
    for connected_store in (store, inbox.store):
        connected_store.save_connection(platform, None, browser_context='isolated-test')
    response = app.test_client().post('/submissions', data={'content':value})
    assert response.status_code == 302
    assert store.item_bundle(1)['submitted_url'] == url
    inbox.receive(history_message(message(text=value)), history=True)
    processor = FeishuIntake(inbox, LinkIntake(inbox.store, None, None, {}))
    assert processor.process('om_1') == 'accepted'
    assert processor.process('om_1') == 'accepted'
    assert len(inbox.store.recent_items()) == 1
    assert submitted == [[url], [url]]

@pytest.mark.parametrize('value,expected', [
    ('6.15 复制打开抖音，看看【作者的作品】标题 https://v.douyin.com/TESTshort123/:2pm dAG:/', 'https://v.douyin.com/TESTshort123/'),
    ('发布了一篇笔记 http://xhslink.com/a/Abc123，复制本条信息，打开〖小红书〗App查看精彩内容！', 'http://xhslink.com/a/Abc123'),
    ('【【嵌套标题】-哔哩哔哩】https://b23.tv/Abc123。', 'https://b23.tv/Abc123'),
])
def test_short_links_exclude_share_tokens(value, expected):
    assert links_in(value) == [expected]
    assert content_platform(expected)
    assert not needs_content_choice(value)

@pytest.mark.parametrize('url', [
    'https://www.douyin.com/', 'https://www.xiaohongshu.com/',
    'https://www.youtube.com/@someone', 'https://www.zhihu.com/',
    'https://weibo.com/', 'https://x.com/user', 'https://www.bilibili.com/',
    'https://douyin.com.evil.test/video/123', 'https://t.cn/EXAMPLE',
])
def test_host_alone_is_not_supported_material(url):
    assert content_platform(url) is None
    assert needs_content_choice('正文中的网站地址 '+url)


def test_other_url_cannot_force_supported_material_back_to_text():
    assert not needs_content_choice('正文 https://example.test/reference '+URLS[0][1])


def test_feishu_old_undecided_material_resumes_once(inbox):
    inbox.receive(history_message(message(text='完整正文 '+URLS[0][1])), history=True)
    with connect(inbox.store.path) as db:
        db.execute("UPDATE feishu_receipts SET state='waiting_input'")
    assert [r['message_id'] for r in inbox.pending()] == ['om_1']
    intake = FeishuIntake(inbox, LinkIntake(inbox.store, None, None, {}))
    assert intake.process('om_1') == 'accepted'
    assert inbox.pending() == []
    assert len(inbox.store.recent_items()) == 1


def test_link_failure_is_not_saved_as_text(inbox):
    inbox.receive(history_message(message(text='完整正文 '+URLS[3][1])), history=True)
    intake = FeishuIntake(inbox, LinkIntake(inbox.store, None, None, {}))
    assert intake.process('om_1') == 'needs_desktop'
    assert not inbox.store.recent_items()


def test_douyin_shortlink_uses_clean_url_through_collection_intake(tmp_path):
    from types import SimpleNamespace
    calls = []
    preview = {'token': 'owned-test-preview'}
    collections = SimpleNamespace(preview=lambda urls, **kwargs: (calls.append(urls) or preview))
    intake = LinkIntake(Store(tmp_path/'short.sqlite3'), None, collections, {})
    assert intake.submit('6.15 标题 https://v.douyin.com/TESTshort123/:2pm dAG:/') == preview
    assert calls == [['https://v.douyin.com/TESTshort123/']]
