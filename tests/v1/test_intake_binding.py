"""Local-only synthetic descriptors; no Store, file parser, model or network."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from knowledge_distiller.v1.file_sources import SubmittedSource, prepare_direct_text, prepare_file
from knowledge_distiller.v1.intake_binding import (
    IntakeBindingError, build_local_binding, validate_local_binding,
)


def text_source():
    return SubmittedSource('direct_text',
        '2410fb68ebf080c17ab2e23d4e5d12ad2bb3381969d8ef5782f3a8158be13247',
        '直接文本', '重复🍎\r\n重复🍎\r\n'.encode('utf-8'), {'user_declared': {'author': '甲'}})


def file_source(kind='markdown', label='note.md', content=b'body\r\nbody\r\n'):
    return SubmittedSource(kind, hashlib.sha256(content).hexdigest(), label, content, {})


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def rehashed(value):
    """An attacker can recompute hashes; descriptors still must match the source."""
    for key in ('source', 'relation'):
        value[key + '_binding_sha256'] = hashlib.sha256(serialized(value[key]).encode()).hexdigest()
    return serialized(value)


def test_golden_exact_unicode_crlf_duplicate_body_and_original_key():
    source = text_source()
    before = deepcopy(source)
    proposal = build_local_binding(source)
    assert proposal.source_json == (
        '{"contract":"intake-source-v1","delivery":{"channel":"local_web","receipt":null},'
        '"input":{"content_byte_count":24,"content_sha256":'
        '"939b14ce33d1c021c6100a5fc5a7839a7f8e6268c2207fc23d5ca0739c379e8c",'
        '"input_key":"2410fb68ebf080c17ab2e23d4e5d12ad2bb3381969d8ef5782f3a8158be13247",'
        '"input_kind":"direct_text","input_label":"直接文本",'
        '"metadata":{"user_declared":{"author":"甲"}},"type":"submitted_bytes"},"owner_selector":null}')
    assert proposal.relation_json == (
        '{"adjacency":{"rows":[],"status":"not_applicable"},"contract":"intake-relation-v1",'
        '"intent":{"annotation_target":null,"identity":"unresolved","kind":"standalone"},'
        '"range":{"mode":"single","selectors":[{"ordinal":0,"selector_sha256":'
        '"c7a8b2036ac0d148fb89dc83f2327b68f611b994d9a30b2611ed4e7d9295812d"}]},'
        '"resolution":{"raw_ids":[],"status":"unresolved"}}')
    assert proposal.source_binding_sha256 == '0e7a3e2eb8e499e472a625237143fe4fbd6a76b744fc0696340b8b361b9a9c91'
    assert proposal.relation_binding_sha256 == 'e70cb54aeafed2dfb64f7602d8ab6e781df24378e642cd6763ae48a43c26caaa'
    assert validate_local_binding(source, proposal.envelope_json.encode('utf-8')) == proposal
    assert validate_local_binding(source, ' \n' + proposal.envelope_json + '\t\r\n') == proposal
    assert source == before
    assert '重复🍎' not in proposal.envelope_json
    assert 'source_complete' not in proposal.envelope_json
    with pytest.raises(FrozenInstanceError):
        proposal.source_binding_sha256 = 'replacement'


@pytest.mark.parametrize('kind,label', [('markdown', 'note.MD'), ('pdf', 'note.pdf'), ('epub', 'note.epub')])
def test_file_identity_is_exact_unparsed_bytes_and_frozen_label(kind, label):
    # Binary input is not a JSON string. This only tests identity, not PDF/EPUB
    # qualification, conversion or source completeness.
    source = file_source(kind, label, b'\x00synthetic\xff\r\n')
    proposal = build_local_binding(source)
    assert validate_local_binding(source, proposal.envelope_json) == proposal
    body = json.loads(proposal.source_json)['input']
    assert body['content_byte_count'] == 13
    assert body['input_key'] == source.source_key == body['content_sha256']
    renamed = replace(source, label='renamed.' + label.rsplit('.', 1)[1])
    assert renamed.source_key == source.source_key
    other = build_local_binding(renamed)
    assert other.source_binding_sha256 != proposal.source_binding_sha256
    assert other.relation_binding_sha256 != proposal.relation_binding_sha256
    with pytest.raises(IntakeBindingError):
        validate_local_binding(renamed, proposal.envelope_json)


def test_declarations_are_exact_input_claims_not_author_identity():
    a = prepare_direct_text('同一正文', {'author': '甲', 'origin': '出处'})
    b = prepare_direct_text('同一正文', {'origin': '出处', 'author': '甲'})
    c = prepare_direct_text('同一正文', {'author': '乙', 'origin': '出处'})
    assert build_local_binding(a) == build_local_binding(b)
    assert a.content == c.content and a.source_key != c.source_key
    proposal = build_local_binding(a)
    assert build_local_binding(c).source_binding_sha256 != proposal.source_binding_sha256
    assert json.loads(proposal.relation_json)['intent']['identity'] == 'unresolved'
    with pytest.raises(IntakeBindingError):
        validate_local_binding(c, proposal.envelope_json)


def test_actual_file_colon_basename_is_preserved_as_label_data():
    source = prepare_file('Chapter:1.md', b'synthetic')
    proposal = build_local_binding(source)
    assert source.label == 'Chapter:1.md'
    assert json.loads(proposal.source_json)['input']['input_label'] == 'Chapter:1.md'
    assert validate_local_binding(source, proposal.envelope_json) == proposal
    renamed = prepare_file('Chapter:2.md', b'synthetic')
    assert renamed.content == source.content and renamed.source_key == source.source_key
    other = build_local_binding(renamed)
    assert other.source_binding_sha256 != proposal.source_binding_sha256
    assert other.relation_binding_sha256 != proposal.relation_binding_sha256
    with pytest.raises(IntakeBindingError):
        validate_local_binding(renamed, proposal.envelope_json)


@pytest.mark.parametrize('texts', [('é', 'e\u0301'), ('x\r\nx\r\n', 'x\nx\n'), ('x\nx\n', 'x\n')])
def test_no_unicode_line_ending_or_repeated_block_normalization(texts):
    left, right = [build_local_binding(prepare_direct_text(t)) for t in texts]
    assert left.source_binding_sha256 != right.source_binding_sha256
    assert left.relation_binding_sha256 != right.relation_binding_sha256


@pytest.mark.parametrize('kind', ['image', 'link', 'feishu_voice', 'voice', 'group'])
def test_nonlocal_kinds_are_explicitly_unsupported(kind):
    with pytest.raises(IntakeBindingError, match='^intake_binding_unsupported$'):
        build_local_binding(replace(text_source(), source_kind=kind))


@pytest.mark.parametrize('case', [
    'wrong_key', 'changed_bytes', 'mutable_bytes', 'metadata_unknown', 'declaration_unknown',
    'declaration_bool', 'metadata_null', 'declaration_null', 'metadata_float',
    'label_nul', 'label_surrogate', 'label_path', 'label_backslash_path', 'text_nul', 'text_invalid_utf8',
    'declaration_surrogate', 'empty_text', 'file_metadata',
])
def test_invalid_source_cannot_create_a_descriptor(case):
    source = text_source()
    if case == 'wrong_key': source = replace(source, source_key='0'*64)
    elif case == 'changed_bytes': source = replace(source, content=source.content + b'changed')
    elif case == 'mutable_bytes': source = replace(source, content=bytearray(source.content))
    elif case == 'metadata_unknown': source = replace(source, metadata={'user_declared': {}, 'identity': 'my_thought'})
    elif case == 'declaration_unknown': source = replace(source, metadata={'user_declared': {'identity': 'my_thought'}})
    elif case == 'declaration_bool': source = replace(source, metadata={'user_declared': {'author': True}})
    elif case == 'metadata_null': source = replace(source, metadata=None)
    elif case == 'declaration_null': source = replace(source, metadata={'user_declared': {'author': None}})
    elif case == 'metadata_float': source = replace(source, metadata={'user_declared': {'author': 1.0}})
    elif case == 'label_nul': source = replace(source, label='bad\x00label')
    elif case == 'label_surrogate': source = replace(source, label='\ud800')
    elif case == 'label_path': source = replace(file_source(), label='/synthetic/note.md')
    elif case == 'label_backslash_path': source = replace(file_source(), label='synthetic\\note.md')
    elif case == 'text_nul': source = replace(source, content=b'text\x00')
    elif case == 'text_invalid_utf8': source = replace(source, content=b'\xff')
    elif case == 'declaration_surrogate': source = replace(source, metadata={'user_declared': {'author': '\ud800'}})
    elif case == 'empty_text': source = replace(source, content=b'')
    elif case == 'file_metadata': source = replace(file_source(), metadata={'author': 'unapproved'})
    with pytest.raises(IntakeBindingError):
        build_local_binding(source)


@pytest.mark.parametrize('case', [
    'ordinal_bool', 'ordinal_null', 'ordinal_float', 'byte_count_bool', 'two_selectors',
    'no_selector', 'unknown_source_key', 'unknown_relation_key', 'metadata_unknown',
    'identity_my_thought', 'raw_id', 'receipt', 'owner', 'digest_null', 'digest_wrong', 'source_null',
])
def test_rehashed_persisted_envelope_cannot_override_actual_source_or_single_range(case):
    source = text_source()
    value = json.loads(build_local_binding(source).envelope_json)
    selector = value['relation']['range']['selectors'][0]
    if case == 'ordinal_bool': selector['ordinal'] = False
    elif case == 'ordinal_null': selector['ordinal'] = None
    elif case == 'ordinal_float': selector['ordinal'] = 0.0
    elif case == 'byte_count_bool': value['source']['input']['content_byte_count'] = True
    elif case == 'two_selectors': value['relation']['range']['selectors'].append(deepcopy(selector))
    elif case == 'no_selector': value['relation']['range']['selectors'] = []
    elif case == 'unknown_source_key': value['source']['verified'] = True
    elif case == 'unknown_relation_key': value['relation']['verified'] = True
    elif case == 'metadata_unknown': value['source']['input']['metadata']['identity'] = 'my_thought'
    elif case == 'identity_my_thought': value['relation']['intent']['identity'] = 'my_thought'
    elif case == 'raw_id': value['relation']['resolution']['raw_ids'] = ['synthetic-id']
    elif case == 'receipt': value['source']['delivery']['receipt'] = {'synthetic': True}
    elif case == 'owner': value['source']['owner_selector'] = 1
    elif case == 'source_null': value['source'] = None
    payload = rehashed(value)
    if case in {'digest_null', 'digest_wrong'}:
        value['source_binding_sha256'] = None if case == 'digest_null' else '0'*64
        payload = serialized(value)
    with pytest.raises(IntakeBindingError):
        validate_local_binding(source, payload)


@pytest.mark.parametrize('case', ['trailing', 'double', 'duplicate_top', 'duplicate_nested',
    'nan', 'infinity', 'negative_infinity', 'surrogate', 'nul', 'invalid_utf8', 'top_list'])
def test_strict_persisted_json_rejects_ambiguous_or_illegal_data(case):
    source = text_source()
    payload = build_local_binding(source).envelope_json
    if case == 'trailing': payload += ' trailing'
    elif case == 'double': payload += payload
    elif case == 'duplicate_top': payload = '{"contract":"discarded",' + payload[1:]
    elif case == 'duplicate_nested': payload = payload.replace('"ordinal":0', '"ordinal":1,"ordinal":0')
    elif case in {'nan', 'infinity', 'negative_infinity'}:
        number = {'nan': 'NaN', 'infinity': 'Infinity', 'negative_infinity': '-Infinity'}[case]
        payload = payload.replace('"ordinal":0', '"ordinal":' + number)
    elif case == 'surrogate': payload = payload.replace('"甲"', '"\\ud800"')
    elif case == 'nul': payload = payload.replace('"甲"', '"\\u0000"')
    elif case == 'invalid_utf8': payload = payload.encode() + b'\xff'
    elif case == 'top_list': payload = '[]'
    with pytest.raises(IntakeBindingError):
        validate_local_binding(source, payload)


@pytest.mark.parametrize('contract', ['legacy', 'intake-binding-proposal-v2', 'source', 'relation'])
def test_unknown_or_legacy_contract_never_rebinds(contract):
    source = text_source()
    value = json.loads(build_local_binding(source).envelope_json)
    if contract in {'source', 'relation'}: value[contract]['contract'] = 'legacy'
    else: value['contract'] = contract
    with pytest.raises(IntakeBindingError, match='^intake_binding_contract_unsupported$'):
        validate_local_binding(source, serialized(value))


def test_returned_strings_cannot_share_mutable_metadata_or_retain_body():
    source = text_source()
    proposal = build_local_binding(source)
    source.metadata['user_declared']['author'] = 'changed after acceptance'
    assert json.loads(proposal.source_json)['input']['metadata']['user_declared'] == {'author': '甲'}
    with pytest.raises(IntakeBindingError):
        validate_local_binding(source, proposal.envelope_json)


def test_persisted_binding_rejects_a_new_valid_content_revision():
    first = prepare_direct_text('original body')
    second = prepare_direct_text('changed body')
    proposal = build_local_binding(first)
    assert build_local_binding(second).source_binding_sha256 != proposal.source_binding_sha256
    with pytest.raises(IntakeBindingError):
        validate_local_binding(second, proposal.envelope_json)
