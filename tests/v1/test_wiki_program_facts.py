"""Synthetic frozen facts and one batch response; no real model semantics."""
from dataclasses import replace
import json
from pathlib import PurePosixPath

import pytest

from knowledge_distiller.v1 import wiki_support as ws
from .test_wiki_support import h, FakeClient, REF, PAGE, cited


@pytest.mark.parametrize('value', ['2026-10-09', '2026-10-09T12:00:00.000001+00:00',
                                 '2026-02-30T12:00:00.000001+08:00', None])
def test_phase_time_rejects_noncanonical_or_non_taipei_metadata(value):
    from knowledge_distiller.v1.wiki_staging import _validate_phase_started_at, WikiStagingError
    with pytest.raises(WikiStagingError, match='checkpoint_binding_changed'):
        _validate_phase_started_at(value)


@pytest.mark.parametrize('damage', ['bool_count', 'missing_count', 'extra_phase_field', 'bad_time', 'future_start'])
def test_current_observation_schema_is_strict(h, damage):
    ordinary = h.make('当前管理状态。\n', path='wiki/log.md')
    facts = dict(pages=[], pending={'外部': [], '自述': []},
                 query_record={'exists': False, 'sha256': None},
                 completed_phases=[dict(phase='generation', attempt=1, final_sha256='0'*64,
                                       started_at='2026-10-09T12:00:00.000001+08:00')],
                 issue_counts={'错误': 0, '提醒': 1, '信息': 2}, candidate_count=0,
                 scan_observed_at='2026-10-09T12:00:00.000001+08:00')
    if damage == 'bool_count': facts['issue_counts']['提醒'] = True
    if damage == 'missing_count': del facts['candidate_count']
    if damage == 'extra_phase_field': facts['completed_phases'][0]['completed_at'] = 'invented'
    if damage == 'bad_time': facts['completed_phases'][0]['started_at'] = '2026-10-09'
    if damage == 'future_start': facts['completed_phases'][0]['started_at'] = '2026-10-10T12:00:00.000001+08:00'
    content = ws._json(facts).encode()
    with pytest.raises(ws.WikiSupportError, match='input_invalid'):
        ws.build_registry(h.stage, ordinary.changes, ordinary.raws,
                          program_facts=content, program_facts_readback=lambda: content)


def managed(h, text, *, path='wiki/log.md', parent=None, phases=()):
    ordinary = h.make(text, path=path)
    facts = dict(pages=[], pending={'外部': [], '自述': []},
                 query_record={'exists': False, 'sha256': None}, completed_phases=list(phases))
    if PurePosixPath(path).parent.name in {'来源', '主题'}:
        meta, _ = ws._frontmatter(text)
        facts['pages'].append(dict(path=path, sha256=ws.sha256(text.encode()),
            type=PurePosixPath(path).parent.name, confirmed=meta.get('确认') == '已确认',
            declared_topics=meta.get('主题', [])))
    def readback():
        return ws._json(facts).encode()
    mapping = () if parent is None else tuple(ws.ClaimMapping(c.block.claim_id, c.block.path, c.block.position)
                                              for c in parent.claims)
    return ws.build_registry(h.stage, ordinary.changes, ordinary.raws,
        program_facts=readback(), program_facts_readback=readback,
        parent_registry=parent, claim_mapping=mapping)


@pytest.mark.parametrize('phase,activity', [('generation', 'ingest'), ('health', 'lint'),
    ('check', 'check'), ('final-check', 'check'), ('repair-check', 'check')])
def test_host_activity_metadata_keeps_existing_gate_and_hash_binding(h, phase, activity):
    record = dict(phase=phase, attempt=1, final_sha256='0'*64, activity=activity,
                  batch_no=1, raw_ids=[h.raw.stable_id])
    registry = managed(h, '当前本批候选活动。\n', phases=(record,))
    client = Batch()
    result = h.gate(registry).review(client)
    assert result.status == 'supported_candidate_not_published'
    assert client.calls[0]['program_facts']['facts']['completed_phases'] == [record]
    assert client.calls[0]['program_facts']['candidate_hash'] == registry.candidate_hash
    assert '不是accepted或正式发布' in ws.SYSTEM
    assert '不应另要求独立ingest事件证明' in ws.SYSTEM


@pytest.mark.parametrize('damage', ['accepted_activity', 'wrong_activity', 'zero_batch',
    'bool_batch', 'duplicate_raw', 'unknown_raw', 'different_batch', 'partial_fields'])
def test_activity_metadata_rejects_only_consumer_verifiable_errors(h, damage):
    first = dict(phase='generation', attempt=1, final_sha256='0'*64, activity='ingest',
                 batch_no=1, raw_ids=[h.raw.stable_id])
    second = dict(first, phase='health', activity='lint')
    if damage == 'accepted_activity': first['activity'] = 'accepted'
    if damage == 'wrong_activity': first['activity'] = 'lint'
    if damage == 'zero_batch': first['batch_no'] = 0
    if damage == 'bool_batch': first['batch_no'] = True
    if damage == 'duplicate_raw': first['raw_ids'] *= 2
    if damage == 'unknown_raw': first['raw_ids'] = ['R-not-frozen']
    if damage == 'different_batch': second['batch_no'] = 2
    if damage == 'partial_fields': del first['raw_ids']
    with pytest.raises(ws.WikiSupportError, match='input_invalid|binding_mismatch'):
        managed(h, '当前本批候选活动。\n', phases=(first, second))


class Batch:
    def __init__(self, basis='program', status='supported'):
        self.basis, self.status, self.calls = basis, status, []

    def complete(self, *, system, user, max_tokens):
        value = json.loads(user)
        self.calls.append(value)
        assert value['program_facts']['candidate_hash'] == value['candidate_hash']
        return ws._json({'checks': [dict(claim_id=c['claim_id'], basis=self.basis,
            status=self.status, reason='Synthetic batch response, not real semantics.',
            issues=[] if self.status == 'supported' else [dict(field='text', category='missing_context',
                reason='The frozen facts do not establish this assertion.')]) for c in value['claims']]})


def test_uncited_management_fact_uses_same_batch_and_reuses_receipt(h):
    registry = managed(h, '# 变更日志\n\n## [2026-10-08] ingest | 冻结批次 1\n'
                       '当前库没有查询记录，普通知识页面为零。\n')
    assert len(registry.claims) == 1 and registry.claims[0].diagnostics
    client = Batch()
    gate = h.gate(registry)
    result = gate.review(client)
    assert result.status == 'supported_candidate_not_published' and len(client.calls) == 1
    assert gate.review(client).status == result.status and len(client.calls) == 1
    assert client.calls[0]['claims'][0]['deferred_diagnostics'][0]['category'] == 'missing_citation'
    facts = client.calls[0]['program_facts']['facts']
    assert set(facts) == {'pages', 'pending', 'query_record', 'completed_phases'}
    assert facts['pages'] == [] and facts['completed_phases'] == []
    assert result.used_repairs == 0


def test_management_mixed_seed_claim_cannot_bypass_raw_boundary(h):
    registry = managed(h, '# 变更日志\n\n没有查询记录；种子加水10毫升必定发芽。\n')
    client = Batch(basis='mixed')
    result = h.gate(registry).review(client)
    assert len(client.calls) == 1
    assert result.status == 'source_support_failed'
    assert any(d.category == 'missing_citation' for d in result.diagnostics)


def test_mixed_management_and_knowledge_can_pass_with_both_source_kinds(h):
    registry = managed(h, '目前无查询记录；'+cited()+'\n')
    client = Batch(basis='mixed')
    result = h.gate(registry).review(client)
    assert result.status == 'supported_candidate_not_published' and len(client.calls) == 1
    claim = client.calls[0]['claims'][0]
    assert claim['evidence'][0]['stable_id'] == h.raw.stable_id
    assert client.calls[0]['program_facts']['facts']['query_record']['exists'] is False


@pytest.mark.parametrize('text', ['我已完整读完所有页面。', '历史检查已经修复了9个问题。', '种子发芽率必定为80%。'])
def test_unrecorded_or_external_assertions_are_not_program_facts(h, text):
    registry = managed(h, '# 体检报告 2026-10-08\n\n'+text+'\n', path='wiki/体检报告.md')
    client = Batch(status='uncertain')
    result = h.gate(registry).review(client)
    facts = client.calls[0]['program_facts']['facts']
    assert 'read_all' not in ws._json(facts) and facts['completed_phases'] == []
    assert result.status == 'source_support_failed' and result.diagnostics


def test_program_basis_cannot_support_ordinary_knowledge(h):
    registry = managed(h, cited()+'\n', path=PAGE)
    result = h.gate(registry).review(Batch())
    assert result.status == 'source_support_failed'
    assert any(d.reason == 'program_basis_outside_management' for d in result.diagnostics)


@pytest.mark.parametrize('damage', ['facts', 'raw', 'query'])
def test_frozen_facts_and_sources_drift_reject_before_client(h, damage):
    registry = managed(h, '当前管理状态。\n')
    if damage == 'facts':
        changed = json.loads(registry.program_facts)
        changed['completed_phases'] = [dict(phase='health', attempt=1, final_sha256='0'*64)]
        registry = replace(registry, program_facts=ws._json(changed).encode())
    elif damage == 'raw':
        h.write(h.raw.path, h.raw.content+b'changed')
    else:
        h.write('.graph/queries.jsonl', b'{}\n')
    client = Batch()
    with pytest.raises(ws.WikiSupportError):
        h.gate(registry).review(client)
    assert not client.calls


def test_bad_fact_page_type_is_fixed_error_not_attribute_error(h):
    ordinary = h.make('管理状态。\n', path='wiki/log.md')
    facts = ws._json(dict(pages=['not-a-dict'], pending={'外部': [], '自述': []},
        query_record={'exists': False, 'sha256': None}, completed_phases=[])).encode()
    with pytest.raises(ws.WikiSupportError, match='input_invalid'):
        ws.build_registry(h.stage, ordinary.changes, ordinary.raws,
                          program_facts=facts, program_facts_readback=lambda: facts)


def test_management_report_cannot_be_recursive_knowledge_source(h):
    body = cited()+'\n'
    h.write('wiki/体检报告.md', body.encode())
    report = ws.FrozenPage('wiki/体检报告.md', body.encode(), ws.sha256(body.encode()))
    registry = h.make('所有种子都为10。（[[wiki/体检报告.md]]）\n', pages=(report,))
    client = FakeClient()
    result = h.gate(registry).review(client)
    assert result.status == 'source_boundary_failed' and not client.calls
    assert any(d.category == 'management_as_evidence' for d in result.diagnostics)


def test_topic_overview_with_legitimate_raw_chain_is_still_knowledge_source(h):
    path = 'wiki/主题/社科.md'
    body = '## 概览\n'+cited()+'\n'
    h.write(path, body.encode())
    page = ws.FrozenPage(path, body.encode(), ws.sha256(body.encode()))
    registry = h.make('原文只适用于低温。（[[社科#概览]]）\n', pages=(page,))
    assert registry.claims[0].evidence and not registry.claims[0].diagnostics
    assert h.gate(registry).review(FakeClient()).status == 'supported_candidate_not_published'


@pytest.mark.parametrize('ref', ['raw/外部/2026/10/R-20261008-9999.md', 'raw/../unsafe.md',
                                '不存在的管理页面', REF.split('#')[0]+'#broken'])
def test_management_location_cannot_defer_missing_or_damaged_source_path(h, ref):
    registry = managed(h, '更新文件记录。（[['+ref+']]）\n')
    client = Batch()
    result = h.gate(registry).review(client)
    assert result.status == 'source_boundary_failed' and not client.calls


def test_management_feedback_uses_existing_repair_budget_and_stable_binding(h):
    first = managed(h, '# 变更日志\n\n当前普通页面数量为1。\n')
    gate = h.gate(first)
    assert gate.review(Batch(status='unsupported')).status == 'source_support_failed'
    reservation = gate.reserve_repair()
    assert reservation.number == 1
    repaired = managed(h, '# 变更日志\n\n当前普通页面数量为0。\n', parent=first)
    assert repaired.binding_hash == first.binding_hash and repaired.candidate_hash != first.candidate_hash
    result = h.gate(repaired).review(Batch(), reservation=reservation)
    assert result.status == 'supported_candidate_not_published' and result.used_repairs == 1
    assert h.gate(repaired)._state()['used'] == 1 and h.gate(repaired).max_repairs == 2


def test_bare_raw_path_ends_before_chinese_parenthetical_prose(h):
    path = 'raw/外部/2026/10/R-20261008-0002.md'
    h.raw = h.add_raw(path, '第三方', '作者乙', '感谢分享。\n\n^source-1\n')
    text = '- 跳过：'+path+'（仅表达感谢，无可提炼的实质性知识主张）。\n'
    assert ws._citations(text) == (path,)
    registry = managed(h, text)
    assert not any(d.category == 'citation_path_unsafe' for d in registry.claims[0].diagnostics)
    # Synthetic support response isolates the lexer; it does not prove the prose.
    assert h.gate(registry).review(Batch()).status == 'supported_candidate_not_published'
    explicit = 'raw/外部/含（中文）和(括号)/原文.md#^source-1'
    assert ws._citations('[['+explicit+']] [原文]('+explicit+')') == (explicit,)


def test_chinese_suffix_does_not_hide_true_path_traversal(h):
    registry = managed(h, '- 跳过：raw/../unsafe.md（仅表达感谢）。\n')
    client = Batch()
    result = h.gate(registry).review(client)
    assert result.status == 'source_boundary_failed' and not client.calls
    assert any(d.category == 'citation_path_unsafe' for d in result.diagnostics)


@pytest.mark.parametrize('basis', ['program', 'raw', 'mixed'])
def test_management_navigation_requires_independent_program_basis(h, basis):
    registry = managed(h, '当前没有查询记录，详见[[wiki/log.md]]。\n')
    client = Batch(basis=basis)
    result = h.gate(registry).review(client)
    assert len(client.calls) == 1
    assert client.calls[0]['claims'][0]['deferred_diagnostics'][0]['category'] == 'management_as_evidence'
    assert result.status == ('supported_candidate_not_published' if basis == 'program'
                             else 'source_support_failed')


def test_log_navigation_cannot_be_ordinary_knowledge_evidence(h):
    body = '当前没有查询记录。\n'
    h.write('wiki/log.md', body.encode())
    page = ws.FrozenPage('wiki/log.md', body.encode(), ws.sha256(body.encode()))
    registry = h.make('所有种子都会发芽，详见[[wiki/log.md]]。\n', pages=(page,))
    client = FakeClient()
    result = h.gate(registry).review(client)
    assert result.status == 'source_boundary_failed' and not client.calls
    assert any(d.category == 'management_as_evidence' for d in result.diagnostics)
