"""Synthetic R06 contracts; no real data, source capture or HTTP fixtures."""
from dataclasses import replace
import hashlib
import json
import socket

import httpx
import pytest

from knowledge_distiller.v1.multi_link_candidates import LinkInput, prepare_multi_link_input


def source(text, *, kind='markdown', namespace=('feishu', 'fake-app', 'fake-message'), version='1'):
    raw = text.encode('utf-8')
    return LinkInput(namespace, version, raw, hashlib.sha256(raw).hexdigest(), kind)


def zhihu(number):
    return f'https://www.zhihu.com/question/1/answer/{number}'


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_fourteen_in_order_exact_original_input_and_block_evidence(newline):
    text = '我的阅读清单：' + newline + newline.join(zhihu(n) for n in range(101, 115))
    original = source(text)
    result = prepare_multi_link_input(original)
    assert result.source is original
    assert result.source.raw_utf8 == text.encode('utf-8')
    assert result.source.sha256 == hashlib.sha256(text.encode('utf-8')).hexdigest()
    assert [o.position for o in result.occurrences] == list(range(14))
    assert [o.native_id for o in result.occurrences] == [f'answer:{n}' for n in range(101, 115)]
    assert all(o.status == 'valid' and o.evidence.granularity == 'block_lines' for o in result.occurrences)
    assert all(o.evidence.line_range == (0, 15) for o in result.occurrences)
    assert result.mode == 'independent' and result.group_eligibility == 'not_requested'
    assert not result.collection_succeeded
    assert result == prepare_multi_link_input(original)


def test_invalid_last_of_fourteen_retains_earlier_members_and_no_first_platform_inheritance():
    urls = [zhihu(n) for n in range(101, 114)] + ['https://x.com/u/status/not-a-number']
    result = prepare_multi_link_input(source('\n'.join(urls)))
    assert len(result.occurrences) == 14
    assert all(o.status == 'valid' for o in result.occurrences[:13])
    last = result.occurrences[13]
    assert last.position == 13 and last.platform == 'x' and last.status == 'invalid'
    assert last.reason.startswith('platform_locator_invalid:')
    group = prepare_multi_link_input(source('\n'.join(urls)), explicit_same_topic=True)
    assert group.group_eligibility == 'blocked' and not group.collection_succeeded


def test_markdown_description_real_href_and_conflicting_url_label():
    text = f'[我的阅读参考]({zhihu(101)})\n\n[{zhihu(102)}]({zhihu(103)})'
    result = prepare_multi_link_input(source(text))
    assert len(result.occurrences) == 2
    good, conflict = result.occurrences
    assert good.href == zhihu(101) and good.label == '我的阅读参考' and good.status == 'valid'
    assert good.evidence.line_range == (0, 1)
    assert conflict.position == 1 and conflict.reason == 'label_href_identity_conflict'
    assert conflict.href == zhihu(103) and conflict.label == zhihu(102)
    assert conflict.evidence.line_range == (2, 3)


def test_numeric_tail_not_spliced_and_normal_prose_not_rejected():
    text = f'[参考]({zhihu(101)})12345\n\n[参考]({zhihu(102)}) 我的补充意见 12345'
    result = prepare_multi_link_input(source(text))
    first, second = result.occurrences
    assert first.href == zhihu(101) and first.native_id == 'answer:101'
    assert first.reason == 'numeric_tail_after_markdown_link' and first.status == 'ambiguous'
    assert second.status == 'valid' and result.source.raw_utf8 == text.encode()


def test_malformed_block_never_regex_falls_back_but_next_block_survives():
    text = f'[{zhihu(101)}]({zhihu(102)}\n\n{zhihu(103)}'
    result = prepare_multi_link_input(source(text))
    assert len(result.occurrences) == 2
    assert result.occurrences[0].href is None
    assert result.occurrences[0].reason == 'malformed_markdown_block'
    assert result.occurrences[1].href == zhihu(103) and result.occurrences[1].status == 'valid'
    assert result.diagnostics[0].evidence.line_range == (0, 1)


def test_exact_transport_duplicates_only_marked_and_query_variants_related():
    first, second = zhihu(101) + '?signature=one', zhihu(101) + '?signature=two'
    text = '\n'.join([first, zhihu(2), first, second])
    result = prepare_multi_link_input(source(text))
    assert [o.position for o in result.occurrences] == [0, 1, 2, 3]
    assert [o.duplicate_of for o in result.occurrences] == [None, None, 0, None]
    assert result.occurrences[3].related_to == (0, 2)
    assert [o.transport_url for o in result.occurrences] == [first, zhihu(2), first, second]


def test_native_id_and_balanced_query_parentheses_not_rstripped():
    url = zhihu(101) + '?signature=(a(b))&other=你好。'
    text = 'BV1T1bQ6vEDt\n\n' + url
    result = prepare_multi_link_input(source(text))
    identifier, locator = result.occurrences
    assert identifier.kind == 'platform_id' and identifier.native_id == 'BV1T1bQ6vEDt'
    assert identifier.transport_url == 'https://www.bilibili.com/video/BV1T1bQ6vEDt/'
    assert locator.href == url and locator.transport_url == url and locator.status == 'valid'
    assert prepare_multi_link_input(source('12345 我的想法')).occurrences == ()


def test_markdown_entity_query_autolink_reference_and_consumed_code():
    query = zhihu(101) + '?signature=(a(b))&other=z'
    text = (f'[参考]({query.replace("&", "&amp;")})\n\n<{zhihu(102)}>\n\n'
            f'[材料][ref]\n\n[ref]: {zhihu(103)}\n\n'
            f'`{zhihu(104)}`\n\n```text\n{zhihu(105)}\n```\n\n'
            f'<span>{zhihu(106)}</span>')
    result = prepare_multi_link_input(source(text))
    assert [o.href for o in result.occurrences] == [query, zhihu(102), zhihu(103)]
    assert [o.kind for o in result.occurrences] == ['markdown', 'autolink', 'markdown']
    assert result.occurrences[2].label == '材料'
    assert not result.collection_succeeded


@pytest.mark.parametrize('value,reason', [
    ('https://u@www.zhihu.com/question/1/answer/101', 'url_userinfo_forbidden'),
    ('https://www.zhihu.com:0/question/1/answer/101', 'url_port_or_literal_host_forbidden'),
    ('https://www.zhihu.com:443/question/1/answer/101', 'url_port_or_literal_host_forbidden'),
    ('https://www.zhihu.com:/question/1/answer/101', 'url_port_or_literal_host_forbidden'),
    ('https://bad-.test/article', 'url_host_invalid'),
    (zhihu(101) + '?x=%0a', 'url_control_character'),
])
def test_url_safety_before_platform_validator(value, reason):
    occurrence = prepare_multi_link_input(source(value)).occurrences[0]
    assert occurrence.status == 'invalid' and occurrence.reason == reason


def test_short_scope_and_unknown_routes_remain_pending_not_capture_success():
    text = '\n'.join(['https://v.douyin.com/TEST123/', 'https://xhslink.com/a/TEST123',
        'https://b23.tv/TEST123', 'https://space.bilibili.com/123/video',
        'https://unknown.test/article', 'https://douyin.com.evil.test/video/123'])
    result = prepare_multi_link_input(source(text))
    assert [o.status for o in result.occurrences] == [
        'needs_resolution', 'needs_resolution', 'needs_resolution', 'needs_scope', 'pending_route', 'pending_route']
    assert all(o.native_id is None for o in result.occurrences[:3])
    assert not result.collection_succeeded


def test_post_anchor_structured_href_and_prose_do_not_flatten_or_guess_identity():
    payload = {'zh_cn': {'title': '我的清单', 'content': [[
        {'tag': 'text', 'text': '我的补充观点：'},
        {'tag': 'a', 'text': '参考', 'href': zhihu(101)},
        {'tag': 'a', 'text': zhihu(102), 'href': zhihu(103)},
        {'tag': 'text', 'text': ' 完整保留这里的12345。'}]]}}
    text = json.dumps(payload, ensure_ascii=False)
    result = prepare_multi_link_input(source(text, kind='feishu_post'))
    assert len(result.occurrences) == 2 and result.source.raw_utf8 == text.encode()
    assert result.occurrences[0].evidence.json_path == ('zh_cn', 'content', 0, 1)
    assert result.occurrences[0].evidence.href_value == zhihu(101)
    assert result.occurrences[1].reason == 'label_href_identity_conflict'
    assert result.mode == 'independent'


def test_post_bad_local_entry_retains_neighbor_anchor_and_whole_payload():
    text = json.dumps({'content': [[{'tag': 'a', 'href': None},
                                  {'tag': 'a', 'text': '参考', 'href': zhihu(101)}]]})
    result = prepare_multi_link_input(source(text, kind='feishu_post'))
    assert [o.status for o in result.occurrences] == ['ambiguous', 'valid']
    assert result.source.raw_utf8 == text.encode()


def test_explicit_group_eligibility_is_pure_and_native_duplicates_not_two_members():
    text = f'{zhihu(100)}\n{zhihu(2)}\n{zhihu(10)}'
    ordinary = prepare_multi_link_input(source(text))
    group = prepare_multi_link_input(source(text), explicit_same_topic=True)
    assert ordinary.group_eligibility == 'not_requested' and group.group_eligibility == 'eligible'
    assert [o.native_id for o in group.occurrences] == ['answer:100', 'answer:2', 'answer:10']
    assert not group.collection_succeeded
    duplicate = prepare_multi_link_input(source(zhihu(101) + '\n' + zhihu(101)), explicit_same_topic=True)
    assert duplicate.group_eligibility == 'blocked'
    assert 'fewer_than_two_distinct_native_candidates' in duplicate.group_reasons
    mixed = prepare_multi_link_input(source(zhihu(101) + '\nhttps://x.com/u/status/102'), explicit_same_topic=True)
    assert 'same_platform_required' in mixed.group_reasons


@pytest.mark.parametrize('change', ['hash', 'version', 'namespace', 'utf8', 'control', 'kind'])
def test_admission_failure_retains_original_bytes_and_diagnostics(change):
    original = source(zhihu(101))
    if change == 'hash':
        original = replace(original, sha256='wrong')
    elif change == 'version':
        original = replace(original, version='')
    elif change == 'namespace':
        original = replace(original, namespace=())
    elif change in {'utf8', 'control'}:
        raw = b'\xff' if change == 'utf8' else b'\x00'
        original = replace(original, raw_utf8=raw, sha256=hashlib.sha256(raw).hexdigest())
    else:
        original = replace(original, kind='invented')
    result = prepare_multi_link_input(original, explicit_same_topic=True)
    assert result.source is original and result.occurrences == ()
    assert result.diagnostics[0].reason.startswith('input_admission_failed:')
    assert result.group_eligibility == 'blocked' and not result.collection_succeeded


def test_no_network_or_source_capture_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('pure candidate attempted network')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setattr(httpx, 'get', forbidden)
    urls = [zhihu(101), 'https://v.douyin.com/TEST123/',
            'https://www.douyin.com/video/123', 'https://youtu.be/abcdefghijk',
            'https://www.xiaohongshu.com/explore/' + 'a' * 24 + '?xsec_token=(fake)',
            'https://m.weibo.cn/detail/123', 'https://x.com/u/status/123',
            'https://www.bilibili.com/video/BV1T1bQ6vEDt/?p=2']
    result = prepare_multi_link_input(source('\n'.join(urls)))
    assert [o.status for o in result.occurrences] == ['valid', 'needs_resolution', *['valid'] * 6]
    assert [o.native_id for o in result.occurrences][-2:] == ['123', 'BV1T1bQ6vEDt_p2']
