"""Literal check quotations: isolated synthetic bytes, no model or database."""
from copy import deepcopy

import pytest

from knowledge_distiller.v1 import wiki_typed as t
from knowledge_distiller.v1.wiki_tasks import FrozenRaw


def candidate(text, quote, start, end):
    data = text.encode('utf-8')
    raw = FrozenRaw('raw/外部/2026/10/R-20261008-0001.md',
                    'R-20261008-0001', '第三方', len(data), t.digest(data))
    binding = dict(task_id='a' * 32, attempt_id='b' * 32, batch_no=1,
                   boundary_sha256='c' * 64, input_sha256='d' * 64)
    evidence = dict(raw_id=raw.raw_id, content_sha256=raw.content_sha256,
                    start=start, end=end, text=quote)
    value = dict(contract=t.CHECK_CONTRACT, schema_revision=1, binding=binding,
        proposal_sha256='e' * 64, changes_sha256='f' * 64,
        reviews=[dict(raw_id=raw.raw_id, content_sha256=raw.content_sha256,
            status='unsupported', reason='完整合成来源包含明确方法，不能判为无知识。',
            source_check=dict(status='complete', reason='完整合成文本原样留存。',
                              evidence_sha256='1' * 64),
            dimensions=[dict(dimension=name, status='present' if i == 0 else 'absent',
                reason='依据完整合成来源逐字核验该维度。',
                evidence=[evidence] if i == 0 else [], related_raw_ids=[])
                for i, name in enumerate(t.DIMENSIONS)])])
    args = dict(binding=binding, rows=((raw, data),), proposal_sha256='e' * 64,
                changes_sha256='f' * 64, source_proof_sha256='1' * 64)
    return value, evidence, args


@pytest.mark.parametrize('text,quote,start,end,expected', [
    ('开头；唯一原文；结束。', '唯一原文', 0, 2, (3, 7)),
    ('原文；原文。', '原文', 3, 5, (3, 5)),
    ('aaaa', 'aaa', 1, 4, (1, 4)),
])
def test_only_misplaced_unique_quote_is_normalized_without_changing_original(
        tmp_path, text, quote, start, end, expected):
    value, _evidence, args = candidate(text, quote, start, end)
    original = deepcopy(value)
    final = tmp_path / 'synthetic-final.json'
    final.write_bytes(t.encoded(value))
    before = final.read_bytes()
    result = t.parse_check(before, **args)
    evidence = result['reviews'][0]['dimensions'][0]['evidence'][0]
    assert (evidence['start'], evidence['end']) == expected
    assert evidence['text'] == text[evidence['start']:evidence['end']]
    assert value == original and final.read_bytes() == before


@pytest.mark.parametrize('damage', [
    'zero', 'multiple', 'overlap', 'negative', 'past_end', 'bool',
    'hash', 'raw', 'other_raw', 'unicode', 'trim', 'binding',
])
def test_quote_resolution_never_repairs_invalid_or_ambiguous_evidence(tmp_path, damage):
    text, quote = '前文；唯一原文；后文。', '唯一原文'
    if damage == 'multiple': text, quote = '原文；原文。', '原文'
    if damage == 'overlap': text, quote = 'aaaa', 'aaa'
    if damage == 'unicode': text, quote = '前文；e\u0301；后文。', 'é'
    value, evidence, args = candidate(text, quote, 0, 1)
    if damage == 'zero': evidence['text'] += '改字'
    elif damage == 'negative': evidence['start'] = -1
    elif damage == 'past_end': evidence['end'] = len(text) + 1
    elif damage == 'bool': evidence['start'] = False
    elif damage == 'hash': evidence['content_sha256'] = '0' * 64
    elif damage == 'raw': evidence['raw_id'] = 'R-20261008-9999'
    elif damage == 'trim': evidence['text'] = ' ' + quote + ' '
    elif damage == 'binding':
        args['binding'] = deepcopy(args['binding'])
        value['binding']['input_sha256'] = '0' * 64
    elif damage == 'other_raw':
        other = b'Only another frozen raw has this quotation.'
        evidence['text'] = other.decode()
        raw = FrozenRaw('raw/外部/2026/10/R-20261008-0002.md',
                        'R-20261008-0002', '第三方', len(other), t.digest(other))
        args['full_context'] = (*args['rows'], (raw, other))
    final = tmp_path / 'synthetic-invalid-final.json'
    final.write_bytes(t.encoded(value))
    before = final.read_bytes()
    with pytest.raises(t.TypedError, match='typed_binding_invalid|typed_coverage_invalid'):
        t.parse_check(before, **args)
    assert final.read_bytes() == before


def test_normalization_deep_copies_decoded_candidate(tmp_path, monkeypatch):
    value, evidence, args = candidate('前文；唯一原文；后文。', '唯一原文', 0, 1)
    original = deepcopy(value)
    # Exercise object ownership explicitly; the real decoder also creates a
    # fresh object, and the bytes/file test above protects the actual entry.
    monkeypatch.setattr(t, 'strict_json', lambda _content: value)
    result = t.parse_check(b'synthetic-object-ownership', **args)
    assert value == original and evidence['start'] == 0
    assert result['reviews'][0]['dimensions'][0]['evidence'][0]['start'] == 3
