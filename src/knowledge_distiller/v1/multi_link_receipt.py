"""R06 receipt snapshot wire; no database, product dispatch or authentication."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import Mapping

from .multi_link_candidates import (Diagnostic, Evidence, LinkInput, LinkOccurrence,
                                   MultiLinkCandidates, prepare_multi_link_input)

WIRE_VERSION = 1
R06_CONTRACT = 'r06-multi-link-candidates-v1'
MARKDOWN_VERSION = '4.2.0'
SNAPSHOT_KIND = 'receipt_snapshot_v1'
_STATUSES = {'valid', 'invalid', 'ambiguous', 'needs_resolution', 'needs_scope', 'pending_route'}
_KINDS = {'bare', 'markdown', 'autolink', 'platform_id', 'post_anchor', 'post_text'}
_REASONS = {
    'url_control_character', 'url_structure_invalid', 'url_userinfo_forbidden',
    'url_port_or_literal_host_forbidden', 'url_host_invalid', 'unknown_host_requires_route',
    'short_link_unresolved', 'native_range_requires_scope', 'local_validator_unavailable',
    'platform_locator_invalid', 'label_href_identity_conflict', 'label_href_identity_unverified',
    'bare_destination_unbalanced', 'adjacent_locators_without_boundary',
    'malformed_markdown_block', 'numeric_tail_after_markdown_link',
    'post_row_invalid', 'post_entry_invalid', 'post_anchor_fields_invalid',
    'malformed_markdown_post_text', 'post_entry_not_plain_text_or_anchor',
    'html_inline_block_not_submitted', 'non_submission_token:code_inline',
    'non_submission_token:html_inline', 'non_submission_token:image',
    'input_admission_failed', 'candidate_diagnostic',
}


class ReceiptWireError(ValueError):
    """Only fixed adapter reason codes are raised; never source/error strings."""


@dataclass(frozen=True)
class ReceiptSnapshot:
    route: str  # frozen / legacy / rejected, never collection/capture success.
    reason: str | None
    receipt_utf8: bytes
    candidates: MultiLinkCandidates | None = None
    wire_utf8: bytes | None = None
    wire_sha256: str | None = None


def _fail(code):
    raise ReceiptWireError(code)


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('json_duplicate_key')
        result[key] = value
    return result


def _load(value):
    return json.loads(value, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: _fail('json_nonfinite_value'))


def _safe_reason(value):
    if value is None:
        return None
    if value.startswith('platform_locator_invalid:'):
        return 'platform_locator_invalid'
    if value.startswith('input_admission_failed:'):
        return 'input_admission_failed'
    return value if value in _REASONS else 'candidate_diagnostic'


def _extract(receipt, raw, bot_open_id):
    app, message_id = receipt.get('app_id'), receipt.get('message_id')
    if not all(type(v) is str and v for v in (app, message_id)):
        _fail('receipt_identity_invalid')
    data = _load(raw)
    if type(data) is not dict:
        _fail('receipt_json_invalid')
    if 'event' in data:
        message = data['event']['message']
        path = ('event', 'message', 'content')
        message_type = message['message_type']
        if 'header' in data and data['header'].get('app_id', app) != app:
            _fail('receipt_app_mismatch')
    else:
        message = data
        path = ('body', 'content')
        message_type = message['msg_type']
    if message['message_id'] != message_id:
        _fail('receipt_message_mismatch')
    content = message['content'] if 'event' in data else message['body']['content']
    if type(content) is not str:
        _fail('message_content_invalid')
    parsed = _load(content)
    if message_type == 'text':
        if type(parsed) is not dict or type(parsed.get('text')) is not str:
            _fail('message_text_invalid')
        input_bytes = parsed['text'].encode('utf-8')
        path = (*path, '$json', 'text')
        kind = 'markdown'
    elif message_type == 'post':
        post = parsed
        if type(post) is not dict:
            _fail('post_structure_invalid')
        if 'content' not in post:
            locale = next((v for v in ('zh_cn', 'en_us') if v in post), None)
            if locale is None:
                _fail('post_structure_invalid')
            post = post[locale]
        if type(post) is not dict or type(post.get('content')) is not list:
            _fail('post_structure_invalid')
        for row in post['content']:
            if type(row) is not list:
                _fail('post_structure_invalid')
            if any(type(v) is not dict for v in row):
                _fail('post_structure_invalid')
            if any(v.get('tag') in {'img', 'media'} for v in row):
                _fail('post_media_route_required')
        input_bytes, kind = content.encode('utf-8'), 'feishu_post'
    else:
        _fail('message_type_not_link_input')
    same_topic = receipt.get('same_topic')
    if type(same_topic) not in (int, bool) or same_topic not in (0, 1):
        _fail('same_topic_metadata_invalid')
    intent = []
    if same_topic:
        if kind != 'markdown' or type(bot_open_id) is not str or not bot_open_id:
            _fail('same_topic_evidence_required')
        mentions = message.get('mentions') or []
        if type(mentions) is not list:
            _fail('same_topic_evidence_required')
        for index, mention in enumerate(mentions):
            if type(mention) is not dict:
                continue
            identity = mention.get('id')
            actual = identity.get('open_id') if type(identity) is dict else (
                identity if mention.get('id_type', 'open_id') == 'open_id' else None)
            key = mention.get('key')
            if actual == bot_open_id and type(key) is str and key and key in parsed['text']:
                intent.append({'mention_index': index, 'key': key, 'bot_open_id': bot_open_id})
        if not intent:
            _fail('same_topic_evidence_required')
    source = LinkInput(('feishu', app, message_id), SNAPSHOT_KIND + ':' + _digest(raw),
                       input_bytes, _digest(input_bytes), kind)
    binding = {'namespace': list(source.namespace), 'snapshot_version': source.version,
               'snapshot_kind': SNAPSHOT_KIND, 'input_kind': kind, 'input_path': list(path),
               'receipt_sha256': _digest(raw), 'input_sha256': source.sha256,
               'input_b64': base64.b64encode(input_bytes).decode('ascii'),
               'same_topic': bool(same_topic), 'intent_evidence': intent}
    return source, binding


def _evidence(value, source):
    if type(value) is not dict or set(value) != set(Evidence.__dataclass_fields__):
        _fail('wire_evidence_invalid')
    line, path = value['line_range'], value['json_path']
    granularity = value['granularity']
    if granularity == 'block_lines':
        if (source.kind != 'markdown' or type(line) is not list or len(line) != 2
                or any(type(v) is not int for v in line)
                or not 0 <= line[0] < line[1] <= len(source.raw_utf8.decode().splitlines())
                or path is not None):
            _fail('wire_evidence_invalid')
    elif granularity == 'json_path':
        if (source.kind != 'feishu_post' or line is not None or type(path) is not list
                or not path or any(type(v) not in (str, int) or type(v) is int and v < 0 for v in path)):
            _fail('wire_evidence_invalid')
        node = _load(source.raw_utf8)
        for part in path:
            node = node[part]
    elif granularity != 'whole_input' or line is not None or path is not None:
        _fail('wire_evidence_invalid')
    for name in ('href_value', 'label_value'):
        if value[name] is not None and type(value[name]) is not str:
            _fail('wire_evidence_invalid')
    return Evidence(granularity, tuple(line) if line is not None else None,
                    tuple(path) if path is not None else None, value['href_value'], value['label_value'])


def _decode_result(value, source, binding):
    if type(value) is not dict or set(value) != {'position_count', 'occurrences', 'diagnostics',
                                               'mode', 'group_eligibility', 'group_reasons', 'collection_succeeded'}:
        _fail('wire_result_invalid')
    rows = value['occurrences']
    if (type(rows) is not list or type(value['position_count']) is not int
            or value['position_count'] != len(rows)):
        _fail('wire_position_coverage_invalid')
    occurrences, first, native = [], {}, {}
    for position, row in enumerate(rows):
        if type(row) is not dict or set(row) != set(LinkOccurrence.__dataclass_fields__):
            _fail('wire_occurrence_invalid')
        if type(row['position']) is not int or row['position'] != position:
            _fail('wire_position_coverage_invalid')
        if row['kind'] not in _KINDS or row['status'] not in _STATUSES:
            _fail('wire_occurrence_invalid')
        for name in ('href', 'label', 'reason', 'platform', 'native_id', 'transport_url'):
            if row[name] is not None and type(row[name]) is not str:
                _fail('wire_occurrence_invalid')
        if row['reason'] is not None and row['reason'] not in _REASONS:
            _fail('wire_reason_invalid')
        if row['status'] == 'valid' and not all(row[v] for v in ('href', 'platform', 'native_id', 'transport_url')):
            _fail('wire_occurrence_invalid')
        ev = _evidence(row['evidence'], source)
        if row['href'] != ev.href_value or row['label'] != ev.label_value:
            _fail('wire_evidence_value_mismatch')
        if row['kind'] == 'post_anchor' and row['href'] is not None:
            node = _load(source.raw_utf8)
            for part in ev.json_path or ():
                node = node[part]
            if (type(node) is not dict or node.get('tag') != 'a'
                    or node.get('href') != row['href'] or node.get('text', '') != row['label']):
                _fail('wire_evidence_value_mismatch')
        transport = row['transport_url']
        key = (row['platform'], row['native_id'])
        expected_related = native.get(key, []) if row['native_id'] is not None else []
        if (row['duplicate_of'] != first.get(transport) or type(row['related_to']) is not list
                or any(type(v) is not int or not 0 <= v < position for v in row['related_to'])
                or row['related_to'] != expected_related
                or row['duplicate_of'] is not None and type(row['duplicate_of']) is not int):
            _fail('wire_duplicate_binding_invalid')
        occurrences.append(LinkOccurrence(**{**row, 'evidence': ev, 'related_to': tuple(row['related_to'])}))
        if transport is not None:
            first.setdefault(transport, position)
        if row['native_id'] is not None:
            native.setdefault(key, []).append(position)
    diagnostics = []
    if type(value['diagnostics']) is not list:
        _fail('wire_result_invalid')
    for row in value['diagnostics']:
        if type(row) is not dict or set(row) != {'reason', 'evidence'} or row['reason'] not in _REASONS:
            _fail('wire_reason_invalid')
        diagnostics.append(Diagnostic(row['reason'], _evidence(row['evidence'], source)))
    mode = 'explicit_same_topic' if binding['same_topic'] else 'independent'
    reasons = []
    if binding['same_topic']:
        if diagnostics:
            reasons.append('input_has_diagnostics')
        if any(o.status != 'valid' for o in occurrences):
            reasons.append('members_not_all_valid_leaves')
        if len({(o.platform, o.native_id) for o in occurrences if o.status == 'valid'}) < 2:
            reasons.append('fewer_than_two_distinct_native_candidates')
        if len({o.platform for o in occurrences}) != 1:
            reasons.append('same_platform_required')
    eligible = ('blocked' if reasons else 'eligible') if binding['same_topic'] else 'not_requested'
    if (value['mode'] != mode or value['group_reasons'] != reasons
            or value['group_eligibility'] != eligible or value['collection_succeeded'] is not False):
        _fail('wire_group_contract_invalid')
    return MultiLinkCandidates(source, tuple(occurrences), tuple(diagnostics), mode, eligible, tuple(reasons))


def prepare_receipt_snapshot(receipt: Mapping, *, new_receipt: bool = False,
                             wire_utf8: bytes | None = None, wire_sha256: str | None = None,
                             part_positions: tuple[int, ...] = (), has_capture: bool = False,
                             bot_open_id: str | None = None) -> ReceiptSnapshot:
    """Freeze only explicitly new unbound receipts; replay never re-parses."""
    raw = b''
    try:
        if type(receipt.get('raw_json')) is not str:
            _fail('receipt_json_invalid')
        raw = receipt['raw_json'].encode('utf-8')
        if (type(new_receipt) is not bool or type(has_capture) is not bool
                or type(part_positions) is not tuple or any(type(v) is not int or v < 0 for v in part_positions)
                or len(set(part_positions)) != len(part_positions)):
            _fail('receipt_call_contract_invalid')
        if wire_utf8 is None:
            if wire_sha256 is not None:
                _fail('wire_missing')
            if not new_receipt or part_positions or has_capture:
                return ReceiptSnapshot('legacy', 'legacy_receipt_not_upgraded', raw)
            source, binding = _extract(receipt, raw, bot_open_id)
            result = prepare_multi_link_input(source, explicit_same_topic=binding['same_topic'])
            result = replace(result,
                occurrences=tuple(replace(o, reason=_safe_reason(o.reason)) for o in result.occurrences),
                diagnostics=tuple(replace(d, reason=_safe_reason(d.reason)) for d in result.diagnostics))
            payload = {'wire_version': WIRE_VERSION, 'r06_contract': R06_CONTRACT,
                       'markdown_version': MARKDOWN_VERSION, 'source': binding,
                       'result': {'position_count': len(result.occurrences),
                           'occurrences': [asdict(o) for o in result.occurrences],
                           'diagnostics': [asdict(d) for d in result.diagnostics],
                           'mode': result.mode, 'group_eligibility': result.group_eligibility,
                           'group_reasons': list(result.group_reasons), 'collection_succeeded': False}}
            wire = _json(payload)
            # Validate the wire shape before returning a freeze candidate.
            _decode_result(_load(wire)['result'], source, binding)
            return ReceiptSnapshot('frozen', None, raw, result, wire, _digest(wire))
        if has_capture:
            _fail('wire_capture_route_conflict')
        if type(wire_utf8) is not bytes or type(wire_sha256) is not str or _digest(wire_utf8) != wire_sha256:
            _fail('wire_digest_mismatch')
        payload = _load(wire_utf8)
        if type(payload) is not dict or set(payload) != {'wire_version', 'r06_contract', 'markdown_version', 'source', 'result'}:
            _fail('wire_envelope_invalid')
        if (type(payload['wire_version']) is not int or payload['wire_version'] != WIRE_VERSION
                or payload['r06_contract'] != R06_CONTRACT or payload['markdown_version'] != MARKDOWN_VERSION):
            _fail('wire_version_unsupported')
        source, binding = _extract(receipt, raw, bot_open_id)
        if _json(payload['source']) != _json(binding):
            _fail('wire_source_binding_mismatch')
        result = _decode_result(payload['result'], source, binding)
        if any(v >= len(result.occurrences) for v in part_positions):
            _fail('wire_part_position_mismatch')
        return ReceiptSnapshot('frozen', None, raw, result, wire_utf8, wire_sha256)
    except ReceiptWireError as error:
        return ReceiptSnapshot('rejected', error.args[0], raw)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError):
        return ReceiptSnapshot('rejected', 'receipt_or_wire_invalid', raw)
    except Exception:
        return ReceiptSnapshot('rejected', 'adapter_internal_error', raw)
