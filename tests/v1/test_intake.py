import pytest
from knowledge_distiller.v1.intake import platform_for_url
from knowledge_distiller.v1.web import create_app, _item_view
from knowledge_distiller.v1.store import Store

@pytest.fixture
def intake(tmp_path):
    store = Store(tmp_path / 'intake.sqlite3')
    app = create_app(store, object())
    app.config['TESTING'] = True
    return app.test_client(), store

@pytest.mark.parametrize('url,kind', [
    ('https://youtu.be/aaaaaaaaaaa', 'youtube'),
    ('https://www.bilibili.com/video/BV1T1bQ6vEDt/?vd_source=x', 'bilibili'),
    ('https://www.douyin.com/video/123', 'douyin'),
    ('https://x.com/u/status/123', 'x'),
    ('https://www.zhihu.com/question/123/answer/456', 'zhihu'),
    ('https://m.weibo.cn/detail/123', 'weibo'),
    ('https://www.xiaohongshu.com/explore/123', 'xiaohongshu'),
    ('https://unknown.test/?x=douyin.com', None),
    ('https://douyin.com.evil.test/video/123', None),
    ('https://douyin.com@evil.test/video/123', None),
])
def test_source_is_classified_by_host_not_text(url, kind):
    assert platform_for_url(url) == kind


def test_multiple_links_require_choice_before_independent_items(intake):
    client, store = intake
    response = client.post('/submissions', data={'content':
        'https://www.douyin.com/video/101\nhttps://www.douyin.com/video/102'})
    assert response.status_code == 200
    assert 'data-open-on-load' in response.text
    assert len(store.recent_items()) == 0
    response=client.post('/submissions',data={'content':'https://www.douyin.com/video/101\nhttps://www.douyin.com/video/102','processing_mode':'separate'})
    assert response.status_code == 302
    assert len(store.recent_items()) == 2
    assert [store.item_bundle(i)['state'] for i in (1, 2)] == ['queued', 'queued']
    assert client.application.extensions['collections'].list() == []


def test_bad_member_does_not_block_next_or_repeat_success_in_draft(intake):
    client, store = intake
    response = client.post('/submissions', data={'processing_mode':'separate','content':
        'https://bad.test/one\nhttps://www.douyin.com/video/102'})
    assert response.status_code == 400
    assert len(store.recent_items()) == 1
    assert '已接收 1 条' in response.text
    textarea = response.text.split('<textarea',1)[1].split('>',1)[1].split('</textarea>',1)[0]
    assert textarea == 'https://bad.test/one'


def test_mixed_platforms_are_independent_but_cannot_be_same_topic(intake):
    client, store = intake
    store.save_connection('youtube', None)
    value = 'https://www.douyin.com/video/101\nhttps://youtu.be/aaaaaaaaaaa'
    response = client.post('/submissions', data={'content':value,'processing_mode':'same_topic'})
    assert response.status_code == 400 and '不能跨平台' in response.text
    assert store.recent_items() == ()
    response = client.post('/submissions', data={'content':value,'processing_mode':'separate'})
    assert response.status_code == 302 and len(store.recent_items()) == 2


@pytest.mark.parametrize('url, label', [
    ('https://youtu.be/aaaaaaaaaaa', 'YouTube'),
    ('https://www.bilibili.com/video/BV1T1bQ6vEDt/', 'B 站'),
    ('https://example.test/video/123', '未知来源'),
    ('https://www.douyin.com/video/123', '抖音'),
    ('https://www.xiaohongshu.com/explore/123456789012345678901234', '小红书'),
    ('https://x.com/u/status/123', 'X'),
    ('https://www.zhihu.com/question/123/answer/456', '知乎'),
    ('https://m.weibo.cn/detail/123', '微博'),
])
@pytest.mark.parametrize('phase,error,text', [
    ('reviewing', 'asr_runtime_failed', '语音识别'),
    ('reviewing', 'ocr_inference_failed', '识别'),
    ('distilling', 'knowledge_evidence_invalid', '证据'),
    ('distilling', 'llm_request_failed', '语言模型'),
])
def test_error_stage_and_source_remain_independent(intake,url,label,phase,error,text):
    client,store=intake
    for platform in ('youtube','douyin','xiaohongshu','x','zhihu','weibo'):
        store.save_connection(platform,None,browser_context='owned:test')
    item=store.create_item(url)
    store.mark_failed(item,phase,error)
    view=_item_view(store.item_bundle(item),None,store.path.parent)
    assert view['source_type']==label
    assert view['phase']==phase
    assert text in client.get('/').text
    if label != '抖音':
        assert '抖音内容' not in client.get('/').text


def test_explicit_full_text_preserves_links_and_prose(intake):
    client,store=intake
    value='我认为这段论证缺少对照组。\nhttps://www.douyin.com/video/101'
    response=client.post('/submissions',data={'content':value,'content_kind':'text'})
    assert response.status_code==302
    assert store.item_bundle(1)['input_kind']=='direct_text'



def test_native_douyin_share_packaging_is_not_user_prose():
    from knowledge_distiller.v1.intake import has_prose
    share='0.56 复制打开抖音，看看【测试账号的作品】示例科学3.0-测试篇   https://v.douyin.com/TESTshare123/ :0pm LJV:/ 09/12 g@b.At'
    assert not has_prose(share)
    assert has_prose('我认为这里需要补充证据。\n'+share)
    assert has_prose(share+' 我的补充观点')
    assert has_prose('示例科学3.0-测试篇 https://v.douyin.com/TESTshare123/')


@pytest.mark.parametrize('prefix', ['1.00 :5pm rEH:/ 10/13 I@i.cA 一个开源项目 #程序员 ', '任意新格式标题和分享口令 '])
def test_supported_platform_share_never_requests_prose_classification(intake,prefix):
    client,store=intake
    value=prefix+'https://www.douyin.com/video/101'
    response=client.post('/submissions',data={'content':value})
    assert response.status_code==302
    assert store.item_bundle(1)['input_kind'] != 'direct_text'
    assert 'data-open-on-load' not in response.text
    assert '请选择处理链接' not in response.text
    assert 'data-submit-text' not in client.get('/').text
    assert len(store.recent_items())==1


def test_unknown_url_and_prose_can_be_selected_as_complete_text(intake):
    client,store=intake
    value='我的正文 https://example.test/reference'
    first=client.post('/submissions',data={'content':value})
    assert first.status_code==200
    assert 'data-open-on-load' in first.text
    assert '<select name="content_kind"' not in first.text
    assert client.post('/submissions',data={'content':value,'content_kind':'text'}).status_code==302


def test_plain_text_submits_from_the_single_primary_action(intake):
    client,store=intake
    assert client.post('/submissions',data={'content':'这是一段完整正文。'}).status_code==302
    assert store.item_bundle(1)['input_kind']=='direct_text'
    page=client.get('/').text
    assert '按完整文本处理' not in page
    assert 'data-submit-text' not in page
