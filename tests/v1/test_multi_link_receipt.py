"""Synthetic receipt/wire contracts, independent of the product database."""
import hashlib
import json
from dataclasses import replace

import pytest

from knowledge_distiller.v1 import multi_link_receipt as adapter


def url(n):
    return f'https://www.zhihu.com/question/1/answer/{n}'


def receipt(text='', *, post=None, topic=False, mentions=None, history=False):
    content = json.dumps({'text': text} if post is None else post, ensure_ascii=False)
    message = {'message_id': 'fake-message', 'mentions': mentions or []}
    if history:
        raw = {**message, 'msg_type': 'text' if post is None else 'post', 'body': {'content': content}}
    else:
        raw = {'header': {'app_id': 'fake-app'}, 'event': {'message': {
            **message, 'message_type': 'text' if post is None else 'post', 'content': content}}}
    return {'app_id': 'fake-app', 'message_id': 'fake-message', 'same_topic': int(topic),
            'raw_json': json.dumps(raw, ensure_ascii=False, sort_keys=True),
            'text': '剥mention后的旧投影不能作为输入'}


def replay(row, frozen, **kwargs):
    return adapter.prepare_receipt_snapshot(row, wire_utf8=frozen.wire_utf8,
        wire_sha256=frozen.wire_sha256, **kwargs)


def changed_wire(frozen, edit):
    payload = json.loads(frozen.wire_utf8)
    edit(payload)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return raw, hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('history', [False, True])
def test_original_text_bytes_path_and_snapshot_not_native_version(history):
    original = '@_user_1 阅读清单\r\n' + url(101) + '\r\n\r\n' + url(2)
    row = receipt(original, history=history)
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    assert frozen.route == 'frozen' and frozen.reason is None
    assert frozen.receipt_utf8 == row['raw_json'].encode()
    assert frozen.candidates.source.raw_utf8 == original.encode()
    assert frozen.candidates.source.namespace == ('feishu', 'fake-app', 'fake-message')
    assert frozen.candidates.source.version.startswith('receipt_snapshot_v1:')
    payload = json.loads(frozen.wire_utf8)
    expected_path = ['body', 'content', '$json', 'text'] if history else ['event', 'message', 'content', '$json', 'text']
    assert payload['source']['input_path'] == expected_path
    assert payload['markdown_version'] == '4.2.0' and payload['wire_version'] == 1
    assert [o.position for o in frozen.candidates.occurrences] == [0, 1]
    assert replay(row, frozen).candidates == frozen.candidates


def test_same_topic_from_explicit_receipt_metadata_and_real_bot_mention():
    text = '@_user_1\n' + url(101) + '\n' + url(102)
    mentions = [{'key': '@_user_1', 'id': {'open_id': 'fake-bot'}}]
    row = receipt(text, topic=True, mentions=mentions)
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True, bot_open_id='fake-bot')
    assert frozen.route == 'frozen' and frozen.candidates.group_eligibility == 'eligible'
    assert frozen.candidates.source.raw_utf8 == text.encode()
    evidence = json.loads(frozen.wire_utf8)['source']['intent_evidence']
    assert evidence == [{'mention_index': 0, 'key': '@_user_1', 'bot_open_id': 'fake-bot'}]
    assert replay(row, frozen, bot_open_id='fake-bot').route == 'frozen'
    assert replay(row, frozen).reason == 'same_topic_evidence_required'
    ordinary = adapter.prepare_receipt_snapshot(receipt('这两条同题\n' + url(101) + '\n' + url(102)), new_receipt=True)
    assert ordinary.candidates.mode == 'independent'
    forged = adapter.prepare_receipt_snapshot(receipt(text, topic=True), new_receipt=True, bot_open_id='fake-bot')
    assert forged.reason == 'same_topic_evidence_required'
    assert not frozen.candidates.collection_succeeded


@pytest.mark.parametrize('history', [False, True])
def test_post_uses_original_json_value_and_anchor_path(history):
    post = {'zh_cn': {'title': '完整标题', 'content': [[
        {'tag': 'text', 'text': '旁边原话。'}, {'tag': 'a', 'href': url(101), 'text': '参考'}]]}}
    row = receipt(post=post, history=history)
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    assert frozen.route == 'frozen'
    assert frozen.candidates.source.raw_utf8 == json.dumps(post, ensure_ascii=False).encode()
    anchor = frozen.candidates.occurrences[0]
    assert anchor.evidence.json_path == ('zh_cn', 'content', 0, 1)
    assert anchor.href == url(101) and anchor.label == '参考'
    assert replay(row, frozen).candidates == frozen.candidates


def test_post_with_image_remains_other_route_no_anchor_only_upgrade():
    row = receipt(post={'content': [[{'tag': 'a', 'href': url(101), 'text': '参考'},
                                    {'tag': 'img', 'image_key': 'synthetic'}]]})
    result = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    assert result.route == 'rejected' and result.reason == 'post_media_route_required'
    assert result.receipt_utf8 == row['raw_json'].encode()


@pytest.mark.parametrize('flags', [{}, {'new_receipt': True, 'part_positions': (0,)},
                                  {'new_receipt': True, 'has_capture': True}])
def test_old_unwired_or_bound_receipts_stay_legacy_without_parser(monkeypatch, flags):
    monkeypatch.setattr(adapter, 'prepare_multi_link_input', lambda *a, **k: pytest.fail('legacy reparsed'))
    result = adapter.prepare_receipt_snapshot(receipt(url(101)), **flags)
    assert result.route == 'legacy' and result.reason == 'legacy_receipt_not_upgraded'
    assert result.candidates is None and result.wire_utf8 is None


def test_replay_preserves_invalid_and_duplicate_positions_without_parser(monkeypatch):
    row = receipt('\n'.join([url(101), 'https://x.com/u/status/not-an-id', url(102), url(101)]))
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    assert frozen.route == 'frozen'
    monkeypatch.setattr(adapter, 'prepare_multi_link_input', lambda *a, **k: pytest.fail('replay reparsed'))
    restored = replay(row, frozen, part_positions=(0, 2))
    assert restored.route == 'frozen' and restored.wire_utf8 is frozen.wire_utf8
    assert [o.position for o in restored.candidates.occurrences] == [0, 1, 2, 3]
    assert restored.candidates.occurrences[1].reason == 'platform_locator_invalid'
    assert restored.candidates.occurrences[3].duplicate_of == 0
    assert replay(row, frozen, part_positions=(4,)).reason == 'wire_part_position_mismatch'


@pytest.mark.parametrize('field', ['input_path', 'input_sha256', 'input_b64', 'namespace', 'snapshot_version', 'receipt_sha256'])
def test_even_rehashed_wire_source_fields_must_match_original_receipt(field):
    row = receipt(url(101))
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    raw, sha = changed_wire(frozen, lambda payload: payload['source'].__setitem__(field, ['wrong'] if field in {'namespace', 'input_path'} else 'wrong'))
    result = adapter.prepare_receipt_snapshot(row, wire_utf8=raw, wire_sha256=sha)
    assert result.route == 'rejected' and result.reason == 'wire_source_binding_mismatch'
    assert result.receipt_utf8 == row['raw_json'].encode()


@pytest.mark.parametrize('change', ['omit', 'duplicate_position', 'renumber', 'count'])
def test_even_rehashed_occurrence_coverage_rejects_missing_duplicate_or_compressed_positions(change):
    row = receipt(url(101) + '\nhttps://x.com/u/status/bad\n' + url(102))
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    def edit(payload):
        result = payload['result']
        if change == 'omit':
            result['occurrences'].pop(1)
        elif change == 'duplicate_position':
            result['occurrences'][2]['position'] = 0
        elif change == 'renumber':
            result['occurrences'][1]['position'] = 2
        else:
            result['position_count'] = 2
    raw, sha = changed_wire(frozen, edit)
    result = adapter.prepare_receipt_snapshot(row, wire_utf8=raw, wire_sha256=sha)
    assert result.reason == 'wire_position_coverage_invalid' and result.candidates is None


def test_trusted_original_digest_rejects_rebuilt_empty_candidate_result():
    row = receipt(url(101))
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    def edit(payload):
        payload['result']['occurrences'] = []
        payload['result']['position_count'] = 0
    raw, _ = changed_wire(frozen, edit)
    result = adapter.prepare_receipt_snapshot(row, wire_utf8=raw, wire_sha256=frozen.wire_sha256)
    assert result.reason == 'wire_digest_mismatch'


@pytest.mark.parametrize('field', ['wire_version', 'r06_contract', 'markdown_version'])
def test_unknown_wire_contract_is_not_reparsed_or_upgraded(monkeypatch, field):
    row = receipt(url(101))
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    raw, sha = changed_wire(frozen, lambda payload: payload.__setitem__(field, 'future'))
    monkeypatch.setattr(adapter, 'prepare_multi_link_input', lambda *a, **k: pytest.fail('unknown wire reparsed'))
    assert adapter.prepare_receipt_snapshot(row, wire_utf8=raw, wire_sha256=sha).reason == 'wire_version_unsupported'


def test_changed_receipt_app_message_and_bytes_never_reuses_candidate():
    row = receipt(url(101))
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    assert replay({**row, 'app_id': 'other-app'}, frozen).reason == 'receipt_app_mismatch'
    assert replay({**row, 'message_id': 'other-message'}, frozen).reason == 'receipt_message_mismatch'
    assert replay(receipt(url(102)), frozen).reason == 'wire_source_binding_mismatch'
    assert replay({**row, 'text': '任意变化的旧投影'}, frozen).route == 'frozen'


def test_post_evidence_path_and_value_are_verified_against_real_anchor():
    row = receipt(post={'content': [[{'tag': 'a', 'text': 'A', 'href': url(101)},
                                     {'tag': 'a', 'text': 'B', 'href': url(102)}]]})
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    def edit(payload):
        payload['result']['occurrences'][0]['evidence']['json_path'] = ['content', 0, 1]
    raw, sha = changed_wire(frozen, edit)
    assert adapter.prepare_receipt_snapshot(row, wire_utf8=raw, wire_sha256=sha).reason == 'wire_evidence_value_mismatch'


def test_pending_statuses_survive_roundtrip_not_failed_or_successful_capture():
    row = receipt('\n'.join([url(101), 'https://unknown.test/article',
        'https://v.douyin.com/TEST123/', 'https://space.bilibili.com/123/video']))
    frozen = adapter.prepare_receipt_snapshot(row, new_receipt=True)
    restored = replay(row, frozen)
    assert [o.status for o in restored.candidates.occurrences] == ['valid', 'pending_route', 'needs_resolution', 'needs_scope']
    assert not restored.candidates.collection_succeeded


def test_fixed_failure_code_never_arbitrary_exception_text(monkeypatch):
    secret_text = 'synthetic confidential source body'
    def broken(*args, **kwargs):
        raise RuntimeError(secret_text)
    monkeypatch.setattr(adapter, 'prepare_multi_link_input', broken)
    result = adapter.prepare_receipt_snapshot(receipt(url(101)), new_receipt=True)
    assert result.reason == 'adapter_internal_error' and secret_text not in result.reason
    assert result.candidates is None and result.wire_utf8 is None


def test_detailed_parser_diagnostic_is_not_copied_into_wire_reason(monkeypatch):
    original_parser = adapter.prepare_multi_link_input
    def diagnostic_parser(*args, **kwargs):
        original = original_parser(*args, **kwargs)
        diagnostic = adapter.Diagnostic('input_admission_failed:synthetic private body', adapter.Evidence('whole_input'))
        return replace(original, diagnostics=(diagnostic,))
    monkeypatch.setattr(adapter, 'prepare_multi_link_input', diagnostic_parser)
    result = adapter.prepare_receipt_snapshot(receipt(url(101)), new_receipt=True)
    assert result.route == 'frozen'
    assert result.candidates.diagnostics[0].reason == 'input_admission_failed'
    assert b'synthetic private body' not in result.wire_utf8
