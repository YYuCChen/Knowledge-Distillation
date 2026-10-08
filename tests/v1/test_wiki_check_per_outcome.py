"""Real B5/C6 capture/proof/kit with controlled bounded checker responses.

Unsupported here refutes no-knowledge only; this module does not establish R14
support, model semantics, publication, main acceptance or Worker integration.
"""
from dataclasses import replace
import json
import os
import sys

import pytest

from knowledge_distiller.v1 import wiki_typed as t
from knowledge_distiller.v1.captures import record_capture
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.ingestion import Ingestion
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback
from knowledge_distiller.v1.wiki_staging import prepare_staging, validate_staging
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore
from .test_wiki_staging import KIT, _install, _process
from .test_wiki_application_input import make_runner, configure, call_count

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='actual proof requires POSIX')
PAGE = 'wiki/来源/混合候选.md'
METHOD = '核验方法：先逐字读取原文，再比对附件哈希，最后保留核验记录。'


@pytest.fixture
def mixed(tmp_path):
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite3'); store.initialize()
    vault, runtime = root / 'vault', root / 'runtime'
    vault.mkdir(); runtime.mkdir(mode=0o700)
    _install(vault)
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    for index in range(6):
        app, message = f'mixed-self-{index}', f'message-{index}'
        text = METHOD if index in {0, 2, 5} else '仅向接收者表示感谢，不再附加其他内容。'
        with connect(store.path) as db:
            record_capture(db, app, message, message_type='text',
                created_ms=1790000000000, received_ms=1790000000000,
                text=text, vault=vault)
        cid = ingestion.captures.for_message(app, message)['capture_id']
        ingestion.captures.decide(cid, 'my_thought')
        ingestion.capture(cid, vault)
    task = WikiTaskStore(store.path, kit_root=KIT, python_executable=sys.executable).create_or_reuse(
        vault, request_kind='all', trigger_source='local_web', backend='codex_cli',
        model='fake', effort='medium')
    assert len(task.raw) == 6 and [r.batch_no for r in task.raw] == [1] * 5 + [2]
    with VaultWriteLock.acquire(vault) as lock:
        snapshot = prepare_staging(vault, runtime, task.task_id, task.raw,
            python_executable=sys.executable, source_kit_root=KIT, lock=lock)
        actual, observations = trusted_source_callback(store, lock), []
        def proof(**kwargs):
            observations.append(tuple(r.raw_id for r, _ in kwargs['context']))
            return actual(**kwargs)
        page = snapshot.workspace / PAGE
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text('---\n主题: [商业]\n作者: 本人\n身份: 本人\n平台: 合成自述\n'
            '发布日期: 2026-09-21\n素材类型: 其他\n' +
            f'原始文件: {task.raw[0].relative_path}\n---\n\n# 混合候选\n\n'
            '## 摘要\n合成自述包含核验方法及独立感谢投递。\n## 核心论点\n' + METHOD + '\n' +
            f'[{task.raw[0].raw_id}]({task.raw[0].relative_path}#^source-1)\n'
            '## 引发的想法\n方法仍待独立支持核验。\n', encoding='utf-8')
        paths = [r.relative_path for r in task.raw if r.batch_no == 1]
        _process(snapshot.workspace, runtime, paths)
        validated = validate_staging(snapshot, 1, paths,
            python_executable=sys.executable, source_kit_root=KIT)
        changes = {c.relative_path: c.after_sha256 for c in validated.changes
                   if c.relative_path.startswith('wiki/')}
        binding, rows, payload = t.freeze_input(task, snapshot, 1, proof, runtime_root=runtime)
        assert len(rows) == 5 and len(payload['context_raw']) == 6
        outcomes, reviews = [], []
        context = {c['frozen']['raw_id']: c['full_raw'] for c in payload['context_raw']}
        related = task.raw[-1]
        start = context[related.raw_id].index(METHOD)
        evidence = dict(raw_id=related.raw_id, content_sha256=related.content_sha256,
                        start=start, end=start + len(METHOD), text=METHOD)
        for index, (raw, _data) in enumerate(rows):
            knowledge = index in {0, 2}
            outcomes.append(dict(raw_id=raw.raw_id, content_sha256=raw.content_sha256,
                ordinal=raw.ordinal,
                status='processed_with_knowledge' if knowledge else 'processed_no_knowledge',
                reason_code='knowledge_proposed' if knowledge else 'non_substantive',
                reason=METHOD if knowledge else '完整投递仅表示感谢，不含独立定义、方法或检索线索。',
                documents=[dict(path=PAGE, sha256=changes[PAGE])]))
            dimensions = [dict(dimension=name, status='absent',
                reason='Controlled assessment of full retained input.', evidence=[], related_raw_ids=[])
                for name in t.DIMENSIONS]
            if knowledge:
                dimensions[0].update(status='present', evidence=[dict(evidence)],
                                     related_raw_ids=[related.raw_id])
            reviews.append(dict(raw_id=raw.raw_id, content_sha256=raw.content_sha256,
                status='unsupported' if knowledge else 'verified',
                reason='Controlled response about no-knowledge disposition, not R14 support.',
                source_check=dict(status='complete', reason='Complete retained self-text only.',
                                  evidence_sha256=payload['source_proof_sha256']), dimensions=dimensions))
        proposal = t.encoded(dict(contract=t.CONTRACT, schema_revision=1, binding=binding, outcomes=outcomes))
        final = dict(contract=t.CHECK_CONTRACT, schema_revision=1, binding=binding,
            proposal_sha256=t.digest(proposal),
            changes_sha256=t.digest(t.encoded(t.checked_documents(snapshot, changes))), reviews=reviews)
        runner, _probes = make_runner(root)
        configure(root, final)
        q0 = runner.check_json(snapshot, runtime, task=task, batch_no=1, model='fake', effort='medium',
            proposal=proposal, changes=changes, source_proof=proof, input_policy=t.APPLICATION_UTF8_POLICY)
        assert q0.succeeded and call_count(root) == 1
        yield dict(task=task, snapshot=snapshot, runtime=runtime, paths=paths,
            proof=proof, observations=observations, proposal=proposal, q0=q0, validated=validated,
            final=final, root=root)


def freeze(m, proposal=None, q0=None):
    return t.freeze_outcome_stage(m['snapshot'], m['runtime'], task=m['task'], batch_no=1,
        proposal=m['proposal'] if proposal is None else proposal,
        check_result=m['q0'] if q0 is None else q0,
        validated=m['validated'], source_proof=m['proof'])


def changed_result(m, final):
    data = t.encoded(final)
    return replace(m['q0'], final_bytes=data, final_sha256=t.digest(data))


def test_mixed_b5_c6_freeze_same_d_and_uncited_whole_d_refresh(mixed):
    m = mixed
    stage = freeze(m)
    same = t.refresh_outcome_documents(m['snapshot'], m['runtime'], task=m['task'], batch_no=1,
        stage=stage, validated=m['validated'], source_proof=m['proof'])
    assert same.proposal == m['proposal'] and same.reusable_check is m['q0']
    log = m['snapshot'].workspace / 'wiki/log.md'
    log.write_bytes(log.read_bytes() + '\n合成未引用体检日志变更。\n'.encode())
    d1 = validate_staging(m['snapshot'], 1, m['paths'],
        python_executable=sys.executable, source_kit_root=KIT)
    refreshed = t.refresh_outcome_documents(m['snapshot'], m['runtime'], task=m['task'], batch_no=1,
        stage=stage, validated=d1, source_proof=m['proof'])
    assert refreshed.proposal == m['proposal'] and refreshed.reusable_check is None
    assert 'wiki/log.md' in {c['path'] for c in t._stage_decode(refreshed.changes_json)}
    assert refreshed.tree_json != stage.tree_json
    assert call_count(m['root']) == 1  # Host refresh is not another checker or publication.
    assert m['observations'] and all(ids == tuple(r.raw_id for r in m['task'].raw)
                                    for ids in m['observations'])


@pytest.mark.parametrize('damage', ['with_no_present', 'with_verified', 'with_unknown',
    'no_unsupported', 'no_present', 'no_unknown'])
def test_disposition_and_independent_no_knowledge_check_must_agree(mixed, damage):
    final = json.loads(t.encoded(mixed['final']))
    review = final['reviews'][0 if damage.startswith('with_') else 1]
    if damage == 'with_no_present':
        review['dimensions'][0].update(status='absent', evidence=[], related_raw_ids=[])
    elif damage in {'with_verified', 'with_unknown', 'no_unsupported', 'no_unknown'}:
        review['status'] = damage.split('_', 1)[1]
    else:
        review['dimensions'][0] = json.loads(t.encoded(final['reviews'][0]['dimensions'][0]))
    with pytest.raises(t.TypedError):
        freeze(mixed, q0=changed_result(mixed, final))


@pytest.mark.parametrize('index', [0, 1], ids=['with_knowledge', 'no_knowledge'])
@pytest.mark.parametrize('damage', ['incomplete_source', 'unknown_source', 'unknown_dimension',
                                   'missing_dimension', 'duplicate_dimension'])
def test_every_disposition_requires_complete_source_and_four_decided_dimensions(mixed, index, damage):
    final = json.loads(t.encoded(mixed['final']))
    review = final['reviews'][index]
    if damage.endswith('_source'):
        review['source_check']['status'] = damage.split('_')[0]
    elif damage == 'unknown_dimension':
        review['dimensions'][-1]['status'] = 'unknown'
    elif damage == 'missing_dimension':
        review['dimensions'].pop()
    else:
        review['dimensions'][-1] = dict(review['dimensions'][0])
    with pytest.raises(t.TypedError):
        freeze(mixed, q0=changed_result(mixed, final))


@pytest.mark.parametrize('damage', ['external_raw', 'hash', 'negative_range', 'past_end',
                                   'bool_range', 'text'])
def test_present_evidence_must_match_actual_complete_c_bytes(mixed, damage):
    final = json.loads(t.encoded(mixed['final']))
    evidence = final['reviews'][0]['dimensions'][0]['evidence'][0]
    if damage == 'external_raw':
        evidence['raw_id'] = 'R-outside-frozen-C'
    elif damage == 'hash':
        evidence['content_sha256'] = '0' * 64
    elif damage == 'negative_range':
        evidence['start'] = -1
    elif damage == 'past_end':
        evidence['end'] = 10**9
    elif damage == 'bool_range':
        evidence['start'] = True
    else:
        evidence['text'] += '不在原文'
    with pytest.raises(t.TypedError):
        freeze(mixed, q0=changed_result(mixed, final))


@pytest.mark.parametrize('side', ['proposal', 'review'])
@pytest.mark.parametrize('damage', ['missing_b', 'extra_b', 'reorder', 'wrong_hash', 'binding'])
def test_proposal_and_check_independently_bind_exact_same_ordered_b(mixed, side, damage):
    proposal = json.loads(mixed['proposal'])
    final = json.loads(t.encoded(mixed['final']))
    envelope = proposal if side == 'proposal' else final
    rows = envelope['outcomes' if side == 'proposal' else 'reviews']
    if damage == 'missing_b':
        rows.pop()
    elif damage == 'extra_b':
        rows.append(dict(rows[-1]))
    elif damage == 'reorder':
        rows[0], rows[1] = rows[1], rows[0]
    elif damage == 'wrong_hash':
        rows[0]['content_sha256'] = '0' * 64
    else:
        envelope['binding']['input_sha256'] = '0' * 64
    data = t.encoded(proposal)
    final['proposal_sha256'] = t.digest(data)
    with pytest.raises(t.TypedError):
        freeze(mixed, proposal=data, q0=changed_result(mixed, final))


def test_unknown_proposal_cannot_be_frozen_as_a_decided_candidate(mixed):
    proposal = json.loads(mixed['proposal'])
    proposal['outcomes'][0].update(status='unknown', reason_code='classification_uncertain', documents=[])
    data = t.encoded(proposal)
    final = json.loads(t.encoded(mixed['final']))
    final['proposal_sha256'] = t.digest(data)
    with pytest.raises(t.TypedError):
        freeze(mixed, proposal=data, q0=changed_result(mixed, final))
