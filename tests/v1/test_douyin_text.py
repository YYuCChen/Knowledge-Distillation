import copy
import json

import pytest

from knowledge_distiller.v1.douyin_text import DouyinTextError, parse_douyin_text


def article(markdown='## 原生标题\n\n第一段[来源](https://example.org/page)。\n\n![配图](image-key width=800 height=600)\n\n第二段。'):
    return {'aweme_type': 163, 'aweme_id': '12345', 'desc': '截断摘要不能替代全文',
            'article_info': {'article_title': '文章标题',
                             'article_content': json.dumps({'markdown': markdown}),
                             'fe_data': json.dumps({'image_list': [{
                                 'markdown_url': 'image-key',
                                 'origin_image_url': 'https://p3.douyinpic.com/original.jpg',
                                 'high_image_url': 'https://p3.douyinpic.com/high.jpg',
                                 'ai_high_image_url': 'https://p3.douyinpic.com/ai.jpg'}]})}}


def gallery():
    return {'aweme_type': 68, 'aweme_id': '12345', 'desc': '图文正文\n保持顺序',
            'images': [{'uri': 'second', 'url_list': ['https://p3.douyinpic.com/2?token=1']},
                       {'uri': 'first', 'url_list': ['https://p3.douyinpic.com/1']}]}


def test_article_full_text_uses_original_images_and_exact_occurrence_order():
    value = parse_douyin_text(article())
    assert value.kind == 'article'
    assert value.canonical_url == 'https://www.douyin.com/article/12345'
    assert value.title == '文章标题'
    assert value.body == '原生标题\n\n第一段来源。\n\n〔图片 1〕配图\n\n第二段。'
    assert '截断摘要' not in value.body
    assert 'https://' not in value.body
    assert value.images[0].url.endswith('/original.jpg')
    assert value.images[0].markdown_url == 'image-key'
    assert 'width=800' in value.original_markdown


def test_article_repeated_inline_reference_keeps_each_occurrence():
    value = parse_douyin_text(article('![a](image-key)\n\n![a](image-key)'))
    assert [i.member_id for i in value.images] == ['image-1', 'image-2']
    assert value.body == '〔图片 1〕a\n\n〔图片 2〕a'


def test_article_reference_style_images_and_code():
    value = parse_douyin_text(article('文字 **加粗**。\n\n![图][ref]\n\n[ref]: image-key\n\n```python\nx = 3\n```'))
    assert value.body == '文字 加粗。\n\n〔图片 1〕图\n\nx = 3'


def test_pure_text_article_without_fe_images():
    detail = article('正文全文')
    detail['article_info']['fe_data'] = '{}'
    value = parse_douyin_text(detail)
    assert value.body == '正文全文'
    assert value.images == ()


def test_gallery_keeps_caption_and_original_image_order():
    value = parse_douyin_text(gallery())
    assert value.kind == 'gallery'
    assert value.title == ''
    assert value.body == '图文正文\n保持顺序'
    assert value.canonical_url == 'https://www.douyin.com/note/12345'
    assert [i.identity for i in value.images] == ['second', 'first']


def test_version_tracks_native_edits_and_order_but_not_gallery_signed_url():
    detail = gallery()
    original = parse_douyin_text(detail).native_content_version
    detail['images'][0]['url_list'][0] = 'https://p3.douyinpic.com/2?token=2'
    assert parse_douyin_text(detail).native_content_version == original
    detail['images'].reverse()
    assert parse_douyin_text(detail).native_content_version != original
    detail = article()
    original = parse_douyin_text(detail).native_content_version
    detail['article_info']['article_title'] = '新标题'
    assert parse_douyin_text(detail).native_content_version != original


@pytest.mark.parametrize('mutate', [
    lambda d: d['article_info'].update(article_content='{broken'),
    lambda d: d['article_info'].update(article_content='[]'),
    lambda d: d['article_info'].update(fe_data='null'),
    lambda d: d['article_info'].update(article_title=''),
    lambda d: d['article_info'].update(fe_data='{"image_list": []}'),
    lambda d: d['article_info'].update(article_content='{"markdown": "只有正文、没有图片"}'),
    lambda d: d['article_info'].update(article_content='{"markdown": "![图](unknown)"}'),
    lambda d: d.update(aweme_id='１２３'),
])
def test_incomplete_article_is_never_silently_reduced_to_caption(mutate):
    detail = article()
    mutate(detail)
    with pytest.raises(DouyinTextError, match='douyin_text_invalid'):
        parse_douyin_text(detail)


@pytest.mark.parametrize('change', [
    {'origin_image_url': None}, {'origin_image_url': 'javascript:alert(1)'},
    {'origin_image_url': 'https://user:pass@example.org/pic'}, {'markdown_url': ''},
])
def test_never_substitutes_ai_or_high_resolution_image(change):
    detail = article()
    data = json.loads(detail['article_info']['fe_data'])
    data['image_list'][0].update(change)
    detail['article_info']['fe_data'] = json.dumps(data)
    with pytest.raises(DouyinTextError):
        parse_douyin_text(detail)


def test_ambiguous_article_mapping_is_rejected():
    detail = article()
    data = json.loads(detail['article_info']['fe_data'])
    data['image_list'].append(copy.deepcopy(data['image_list'][0]))
    detail['article_info']['fe_data'] = json.dumps(data)
    with pytest.raises(DouyinTextError):
        parse_douyin_text(detail)


@pytest.mark.parametrize('html', ['<img src="image-key">', 'before <img src="image-key"> after'])
def test_html_is_explicit_unsupported_not_dropped(html):
    with pytest.raises(DouyinTextError, match='douyin_input_unsupported'):
        parse_douyin_text(article(html))


@pytest.mark.parametrize('key', ['video', 'video_play_addr', 'video_download_addr'])
def test_live_photo_keeps_static_original_and_records_omitted_motion(key):
    detail = gallery()
    detail['images'][0][key] = {'url_list': ['https://example.org/video']}
    value = parse_douyin_text(detail)
    assert value.kind == 'gallery'
    assert value.motion_omitted == ('image-1',)
    assert value.images == parse_douyin_text(gallery()).images
    assert value.body == gallery()['desc']
    assert value.native_content_version != parse_douyin_text(gallery()).native_content_version


@pytest.mark.parametrize('bad', [[], [None], [{'uri': 'a', 'url_list': []}],
                                [{'uri': '', 'url_list': ['https://example.org/pic']}],
                                [{'uri': 'a', 'url_list': ['https://example.org/pic', None]}]])
def test_malformed_gallery_cannot_partially_succeed(bad):
    detail = gallery()
    detail['images'] = bad
    with pytest.raises(DouyinTextError):
        parse_douyin_text(detail)


def test_normal_video_stays_in_old_pipeline_but_unknown_gallery_does_not():
    assert parse_douyin_text({'aweme_type': 0, 'video': {'vid': 'a'}}) is None
    for detail in ({'aweme_type': 150}, {'aweme_type': 2}, {'aweme_type': 999, 'images': [{}]}):
        with pytest.raises(DouyinTextError, match='douyin_input_unsupported'):
            parse_douyin_text(detail)


def test_article_asset_replacement_changes_version_but_expiring_signature_does_not():
    detail = article()
    data = json.loads(detail['article_info']['fe_data'])
    original = parse_douyin_text(detail).native_content_version
    data['image_list'][0]['origin_image_url'] += '?token=new'
    detail['article_info']['fe_data'] = json.dumps(data)
    assert parse_douyin_text(detail).native_content_version == original
    data['image_list'][0]['origin_image_url'] = 'https://p3.douyinpic.com/replacement.jpg'
    detail['article_info']['fe_data'] = json.dumps(data)
    assert parse_douyin_text(detail).native_content_version != original


def test_malformed_image_syntax_cannot_become_text_evidence():
    detail = article('图片 ![x](image-key width=oops)')
    detail['article_info']['fe_data'] = '{}'
    with pytest.raises(DouyinTextError):
        parse_douyin_text(detail)


@pytest.mark.parametrize('br', ['<br/>', '<br>', '<br />', '<BR/>'])
def test_native_article_breaks_are_preserved(br):
    detail = article(f'第一行{br}第二行')
    detail['article_info']['fe_data'] = '{}'
    assert parse_douyin_text(detail).body == '第一行\n第二行'


def test_native_break_block_and_code_are_distinct():
    detail = article('第一段\n\n<br/>\n\n第二段\n\n```html\n<br/>\n```')
    detail['article_info']['fe_data'] = '{}'
    body = parse_douyin_text(detail).body
    assert '第一段' in body and '第二段' in body
    assert body.endswith('<br/>')


@pytest.mark.parametrize('html', ['<br onclick="alert(1)">', '<br/><img src="image-key">', '<div><br/></div>'])
def test_br_support_does_not_allow_other_html(html):
    with pytest.raises(DouyinTextError, match='douyin_input_unsupported'):
        parse_douyin_text(article(html))


def test_partial_article_is_rejected_even_when_text_and_images_parse():
    detail = article()
    detail['article_info']['has_more'] = True
    with pytest.raises(DouyinTextError, match='douyin_article_incomplete'):
        parse_douyin_text(detail)
    detail['article_info']['has_more'] = False
    assert len(parse_douyin_text(detail).images) == 1


def test_real_shape_signed_markdown_image_url_rotation_is_not_native_edit():
    detail = article()
    info = detail['article_info']
    info['has_more'] = False
    data = json.loads(info['fe_data'])
    old_url = 'https://p3.douyinpic.com/tos-cn-image/abc~tplv.webp?x-expires=123&x-signature=old'
    new_url = 'https://p3.douyinpic.com/tos-cn-image/abc~tplv.webp?x-expires=456&x-signature=new'
    data['image_list'][0]['markdown_url'] = old_url
    info['fe_data'] = json.dumps(data)
    original_markdown = f'完整正文<br/>下一行\n\n![原生图片]({old_url} width=1080 height=720)'
    info['article_content'] = json.dumps({'markdown': original_markdown})
    before = parse_douyin_text(detail)
    data['image_list'][0]['markdown_url'] = new_url
    data['image_list'][0]['origin_image_url'] += '?x-expires=456&x-signature=new'
    info['fe_data'] = json.dumps(data)
    info['article_content'] = json.dumps({'markdown': original_markdown.replace(old_url, new_url)})
    after = parse_douyin_text(detail)
    assert before.native_content_version == after.native_content_version
    assert before.body == after.body
    assert before.original_markdown == original_markdown
    assert new_url in after.original_markdown
    assert before.images[0].markdown_url == old_url
    assert after.images[0].markdown_url == new_url


def test_browser_type_zero_does_not_turn_a_complete_article_into_video():
    # Reuse the established article fixture shape, changing only endpoint type.
    source={'aweme_id':'123','aweme_type':0,'article_info':{
        'article_title':'标题','article_content':json.dumps({'markdown':'完整正文'}),
        'fe_data':json.dumps({'image_list':[]}), 'has_more':False}}
    result=parse_douyin_text(source)
    assert result.kind=='article' and result.body=='完整正文'


def test_real_cdn_shard_rotation_preserves_article_identity():
    old='https://p3-sign.douyinpic.com/tos-cn-i/asset.jpeg?signature=a'
    new='https://p26-sign.douyinpic.com/tos-cn-i/asset.jpeg?signature=b'
    def source(url):
        return {'aweme_id':'123','aweme_type':163,'article_info':{'article_title':'标题',
            'article_content':json.dumps({'markdown':f'正文\n\n![图片]({url})'}),
            'fe_data':json.dumps({'image_list':[{'markdown_url':url,'origin_image_url':url}]}),'has_more':False}}
    assert parse_douyin_text(source(old)).native_content_version==parse_douyin_text(source(new)).native_content_version


def test_browser_type_zero_gallery_equals_http_type_68():
    detail = gallery()
    expected = parse_douyin_text(detail)
    detail['aweme_type'] = 0
    assert parse_douyin_text(detail) == expected


@pytest.mark.parametrize('kind', [0, 68])
def test_platform_explicit_truncated_gallery_description_is_not_complete(kind):
    detail = gallery()
    detail['aweme_type'] = kind
    detail['desc'] = '每一个资深P人，大概都懂这种感受……版本过低，升级后可展示全部信息'
    with pytest.raises(DouyinTextError, match='douyin_gallery_incomplete'):
        parse_douyin_text(detail)


def test_ordinary_ellipsis_or_long_gallery_caption_is_not_incompleteness_signal():
    detail = gallery()
    detail['desc'] = '原生长正文……' * 300
    assert parse_douyin_text(detail).body == detail['desc']


def test_browser_type_zero_gallery_requires_all_static_originals_even_with_motion():
    detail = gallery()
    detail['aweme_type'] = 0
    detail['images'].append({'uri': 'missing', 'url_list': []})
    with pytest.raises(DouyinTextError):
        parse_douyin_text(detail)
    detail['images'].pop()
    detail['images'][0]['video'] = {'vid': 'live'}
    value = parse_douyin_text(detail)
    assert len(value.images) == 2
    assert value.motion_omitted == ('image-1',)
