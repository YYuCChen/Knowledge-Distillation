"""Host-only P0/Q0/D0 -> unverified P1/D1, actual current-schema self captures.

Real Ingestion, locked SourceProof, installed kit and validate_staging are used.
Only Q0 transport is a bounded fake CLI. Its semantic verdict is not model QA.
No Worker, publication, acceptance, recovery or repair budget is simulated.
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
from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback, SourceProofError
from knowledge_distiller.v1.wiki_staging import prepare_staging, validate_staging
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore
from .test_wiki_staging import KIT, _install, _process
from .test_wiki_application_input import make_runner, configure, call_count

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='actual proof/transport requires POSIX')
PAGE = 'wiki/来源/完整候选.md'


def validate(h):
    return validate_staging(h['snapshot'], 1, h['batch_paths'],
                            python_executable=sys.executable, source_kit_root=KIT)


def freeze(h, **options):
    args = dict(task=h['task'], batch_no=1, proposal=h['p0'], check_result=h['q0'],
                validated=h['d0'], source_proof=h['proof'])
    args.update(options)
    return t.freeze_outcome_stage(h['snapshot'], h['runtime'], **args)


def refresh(h, stage=None, validated=None, **options):
    args = dict(task=h['task'], batch_no=1, stage=stage or h['stage'],
                validated=validated or validate(h), source_proof=h['proof'])
    args.update(options)
    return t.refresh_outcome_documents(h['snapshot'], h['runtime'], **args)


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


@pytest.fixture
def h(tmp_path, request):
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite3'); store.initialize()
    vault, runtime = root / 'vault', root / 'runtime'
    vault.mkdir(); runtime.mkdir(mode=0o700)
    _install(vault)
    if getattr(request, 'param', None) == 'large_protected':
        # Before task/staging freezing, so this is a real protected baseline.
        (vault / '.graph/queries.jsonl').write_bytes(b'{"\xe6\x97\xa5\xe6\x9c\x9f":"2026-10-01","\xe9\xa1\xb5\xe9\x9d\xa2":[]}\n' * 8192)
    store.set_setting('vault_path', str(vault))
    ingestion, captures = Ingestion(store), []
    # Six real sources give actual persisted B1=5, C=6; no handcrafted task DB.
    for index in range(6):
        app, message = f'isolated-self-{index}', f'message-{index}'
        with connect(store.path) as db:
            record_capture(db, app, message, message_type='text',
                created_ms=1790000000000, received_ms=1790000000000,
                text=f'合成完整自述 {index}，仅向接收者表示感谢。', vault=vault)
        cid = ingestion.captures.for_message(app, message)['capture_id']
        ingestion.captures.decide(cid, 'my_thought')
        ingestion.capture(cid, vault)
        captures.append(cid)
    tasks = WikiTaskStore(store.path, kit_root=KIT, python_executable=sys.executable)
    task = tasks.create_or_reuse(vault, request_kind='all', trigger_source='local_web',
                                backend='codex_cli', model='fake', effort='medium')
    assert len(task.raw) == 6 and task.batch_count == 2
    assert [r.batch_no for r in task.raw] == [1] * 5 + [2]
    with VaultWriteLock.acquire(vault) as lock:
        snapshot = prepare_staging(vault, runtime, task.task_id, task.raw,
            python_executable=sys.executable, source_kit_root=KIT, lock=lock)
        actual, observations = trusted_source_callback(store, lock), []
        def proof(**kwargs):
            observations.append(tuple(r.raw_id for r, _ in kwargs['context']))
            return actual(**kwargs)  # Actual source qualification, not a synthetic hash.
        page = snapshot.workspace / PAGE
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text('---\n主题: [商业]\n作者: 本人\n身份: 本人\n'
                        '平台: 合成自述\n发布日期: 2026-09-21\n素材类型: 其他\n' +
                        f'原始文件: {task.raw[0].relative_path}\n---\n\n'
                        '# 完整候选\n\n## 摘要\n合成原文仅表示感谢。\n'
                        '## 核心论点\n无独立论点；保留原文依据：\n' +
                        f'[{task.raw[0].raw_id}]({task.raw[0].relative_path}#^source-1)\n'
                        '## 引发的想法\n无独立想法。\n')
        baseline_page = next(f.relative_path for f in snapshot.files
                             if f.relative_path.startswith('wiki/主题/') and f.relative_path.endswith('.md'))
        theme = snapshot.workspace / baseline_page
        theme.write_bytes(theme.read_bytes() + '\n合成旧D0主题补充。\n'.encode())
        batch_paths = [r.relative_path for r in task.raw if r.batch_no == 1]
        _process(snapshot.workspace, runtime, batch_paths)
        h = dict(root=root, store=store, ingestion=ingestion, captures=captures,
                 vault=vault, runtime=runtime, task=task, snapshot=snapshot,
                 batch_paths=batch_paths, proof=proof, observations=observations,
                 baseline_page=baseline_page)
        d0 = validate(h)
        changes = {c.relative_path: c.after_sha256 for c in d0.changes if c.relative_path.startswith('wiki/')}
        binding, rows, payload = t.freeze_input(task, snapshot, 1, proof, runtime_root=runtime)
        value = dict(contract=t.CONTRACT, schema_revision=1, binding=binding, outcomes=[dict(
            raw_id=r.raw_id, content_sha256=r.content_sha256, ordinal=r.ordinal,
            status='processed_no_knowledge', reason_code='non_substantive',
            reason='完整投递仅向接收者表示感谢，不包含独立定义、操作方法或检索线索。',
            documents=[dict(path=PAGE, sha256=changes[PAGE])]) for r, _ in rows])
        # Deliberately preserve noncanonical P0 whitespace for exact-byte reuse.
        p0 = json.dumps(value, ensure_ascii=False, indent=2).encode()
        context = payload['context_raw'][-1]
        reviews = []
        for r, _ in rows:
            dimensions = [dict(dimension=n, status='absent',
                reason='Controlled complete greeting contains no separate claim in this dimension.',
                evidence=[], related_raw_ids=[]) for n in t.DIMENSIONS]
            dimensions[-1].update(status='support_only', related_raw_ids=[task.raw[-1].raw_id],
                evidence=[dict(raw_id=task.raw[-1].raw_id, content_sha256=task.raw[-1].content_sha256,
                               start=0, end=30, text=context['full_raw'][:30])])
            reviews.append(dict(raw_id=r.raw_id, content_sha256=r.content_sha256, status='verified',
                reason='Controlled greeting supplies neither a distinct claim nor an independent method.',
                source_check=dict(status='complete', reason='Complete retained self-text; not platform-wide verification.',
                                  evidence_sha256=payload['source_proof_sha256']), dimensions=dimensions))
        final = dict(contract=t.CHECK_CONTRACT, schema_revision=1, binding=binding,
            proposal_sha256=t.digest(p0), changes_sha256=t.digest(t.encoded(t.checked_documents(snapshot, changes))),
            reviews=reviews)
        runner, probes = make_runner(root)
        configure(root, final)
        q0 = runner.check_json(snapshot, runtime, task=task, batch_no=1, model='fake', effort='medium',
            proposal=p0, changes=changes, source_proof=proof, input_policy=t.APPLICATION_UTF8_POLICY)
        assert q0.succeeded
        h.update(d0=d0, p0=p0, q0=q0, final=final, runner=runner, probes=probes)
        h['stage'] = freeze(h)
        assert observations and all(c == tuple(r.raw_id for r in task.raw) for c in observations)
        yield h


def mutate_page(h):
    path = h['snapshot'].workspace / PAGE
    path.write_bytes(path.read_bytes() + '补充的候选文本仍待独立检查。\n'.encode())


def reseal(stage, **kwargs):
    """Resign host hashes only to exercise cross-validation, never SourceProof."""
    stage = replace(stage, **kwargs)
    receipt = t._stage_receipt(t._stage_decode(stage.binding_json), stage.proposal, stage.check_result,
                              stage.changes_json, stage.documents_json, stage.tree_json, stage.source_sha256)
    return replace(stage, receipt_json=receipt)


def test_refresh_only_existing_hashes_retains_originals_and_requires_final_check(h):
    original = h['stage']
    saved = original.proposal, original.check_result.final_bytes, original.changes_json, original.documents_json, original.tree_json
    mutate_page(h)
    d1 = validate(h)
    before = tree_bytes(h['snapshot'].workspace), tree_bytes(h['vault']), call_count(h['root']), len(h['probes'])
    result = refresh(h, validated=d1)
    assert result.reusable_check is None
    assert (tree_bytes(h['snapshot'].workspace), tree_bytes(h['vault']), call_count(h['root']), len(h['probes'])) == before
    old, new = json.loads(original.proposal), json.loads(result.proposal)
    assert old['binding'] == new['binding'] and len(new['outcomes']) == 5
    for a, b in zip(old['outcomes'], new['outcomes']):
        assert a['documents'][0]['path'] == b['documents'][0]['path'] == PAGE
        assert a['documents'][0]['sha256'] != b['documents'][0]['sha256']
        b['documents'][0]['sha256'] = a['documents'][0]['sha256']
    assert old == new  # Including classification, reason, order, ordinals, B and C binding.
    assert saved == (original.proposal, original.check_result.final_bytes, original.changes_json,
                     original.documents_json, original.tree_json)
    receipt = t._stage_decode(result.receipt_json)
    assert receipt['parent_stage_sha256'] == t.digest(original.receipt_json)
    assert receipt['final_changes_sha256'] == t.digest(result.changes_json)
    assert receipt['before_documents_sha256'] == t.digest(original.documents_json)
    assert receipt['parent_proposal_sha256'] == t.digest(original.proposal)
    assert len(receipt['document_mapping']) == 5
    assert not hasattr(result, 'accepted') and not hasattr(result, 'published')


def test_same_d_preserves_exact_p0_and_q0_object_but_rechecks_c_and_tree(h):
    before = len(h['observations']), call_count(h['root']), len(h['probes'])
    result = refresh(h)
    assert result.proposal is h['stage'].proposal
    assert result.reusable_check is h['q0']
    assert result.proposal != t.encoded(json.loads(result.proposal))
    assert len(h['observations']) >= before[0] + 2
    assert (call_count(h['root']), len(h['probes'])) == before[1:]
    assert result.tree_json == h['stage'].tree_json


@pytest.mark.parametrize('path', ['wiki/log.md', 'wiki/体检报告.md', 'wiki/主题/合成新主题.md', '.graph/graph.json'])
def test_uncited_health_theme_log_and_graph_are_whole_d_not_reference_subset(h, path):
    target = h['snapshot'].workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if path == 'wiki/主题/合成新主题.md':
        target.write_bytes(('---\n类型: 主题\n---\n\n# 合成新主题\n\n'
                            '## 概览\nsynthetic final D\n\n'
                            '## 核心认知（自动）\n## 核心方法（自动）\n'
                            '## 常用概念（自动）\n## 状况（自动）\n').encode('utf-8'))
    else:
        target.write_bytes((target.read_bytes() if target.exists() else b'') +
                           (b'\n' if path == '.graph/graph.json' else b'\n# synthetic final D\n'))
    # Actual kit validation, not a fabricated health_due boolean. No health is executed.
    result = refresh(h, validated=validate(h))
    assert result.reusable_check is None
    assert json.loads(result.proposal) == json.loads(h['p0'])
    changes = {c['path']: c for c in t._stage_decode(result.changes_json)}
    assert path in changes
    assert changes[path]['after_sha256'] == t.digest(target.read_bytes())
    assert path in {r[0] for r in t._stage_decode(result.tree_json)}


@pytest.mark.parametrize('operation', ['missing', 'rename', 'revert'])
def test_cited_path_disappearance_or_reversion_cannot_select_another_path(h, operation):
    page = h['snapshot'].workspace / PAGE
    if operation == 'missing':
        page.unlink()
    elif operation == 'rename':
        page.rename(page.with_name('新路径.md'))
    else:
        # A cited baseline file that returns to its exact baseline is no longer D1.
        path = h['baseline_page']
        docs = json.loads(h['p0'])
        sha = next(c.after_sha256 for c in h['d0'].changes if c.relative_path == path)
        for o in docs['outcomes']:
            o['documents'] = [dict(path=path, sha256=sha)]
        p0 = t.encoded(docs)
        final = {**h['final'], 'proposal_sha256': t.digest(p0)}
        configure(h['root'], final)
        q0 = h['runner'].check_json(h['snapshot'], h['runtime'], task=h['task'], batch_no=1,
            model='fake', effort='medium', proposal=p0,
            changes={c.relative_path:c.after_sha256 for c in h['d0'].changes if c.relative_path.startswith('wiki/')},
            source_proof=h['proof'], input_policy=t.APPLICATION_UTF8_POLICY)
        h['stage'] = freeze(h, proposal=p0, check_result=q0)
        (h['snapshot'].workspace / path).write_bytes((h['vault'] / path).read_bytes())
    before = call_count(h['root'])
    with pytest.raises(t.TypedError):
        refresh(h)
    assert call_count(h['root']) == before


@pytest.mark.parametrize('damage', ['body', 'size', 'before', 'duplicate', 'omitted_wiki', 'omitted_graph', 'protected_tree'])
def test_d0_evidence_cross_validation_even_after_host_hashes_are_resigned(h, damage):
    stage = h['stage']
    docs, changes, tree = map(t._stage_decode, (stage.documents_json, stage.changes_json, stage.tree_json))
    if damage == 'body':
        docs[0]['content'] += 'wrong old body'
    elif damage == 'size':
        changes[0]['byte_count'] += 1
    elif damage == 'before':
        changes[0]['before_sha256'] = '0' * 64
    elif damage == 'duplicate':
        tree.append(tree[0])
    elif damage == 'omitted_wiki':
        docs.pop()
    elif damage == 'omitted_graph':
        changes = [c for c in changes if not c['path'].startswith('.graph/')]
    else:
        raw = next(r for r in tree if r[0] == h['task'].raw[-1].relative_path)
        raw[1] = '0' * 64
    bad = reseal(stage, documents_json=t.encoded(docs), changes_json=t.encoded(changes), tree_json=t.encoded(tree))
    # Exercise the old audit independently of Q0's documents-hash veto too.
    with pytest.raises(t.TypedError):
        t._old_stage_evidence(h['snapshot'], bad, json.loads(stage.binding_json))
    mutate_page(h)  # Correct new body cannot repair corrupt old evidence.
    with pytest.raises(t.TypedError):
        refresh(h, bad)


@pytest.mark.parametrize('field', ['binding_json', 'changes_json', 'documents_json', 'tree_json', 'receipt_json'])
def test_host_json_must_be_canonical_and_reject_duplicate_keys(h, field):
    bad = replace(h['stage'], **{field: b' ' + getattr(h['stage'], field)})
    with pytest.raises(t.TypedError):
        refresh(h, bad)
    if field == 'receipt_json':
        original = h['stage'].receipt_json
        duplicate = b'{"contract":"forged",' + original[1:]
        with pytest.raises(t.TypedError):
            refresh(h, replace(h['stage'], receipt_json=duplicate))


@pytest.mark.parametrize('damage', ['not_result', 'final_sha', 'schema', 'proposal_binding', 'unknown_check', 'unsupported_check', 'unknown_proposal'])
def test_q0_is_actual_bound_result_not_a_status_or_caller_flag(h, damage):
    q = h['q0']
    p = h['p0']
    if damage == 'not_result':
        q = {'succeeded': True}
    elif damage == 'final_sha':
        q = replace(q, final_sha256='0' * 64)
    elif damage == 'schema':
        q = replace(q, input_binding=replace(q.input_binding, schema_sha256='0' * 64))
    elif damage == 'proposal_binding':
        p = t.encoded({**json.loads(p), 'binding': {**json.loads(p)['binding'], 'input_sha256':'0'*64}})
    elif damage in {'unknown_check', 'unsupported_check'}:
        final = json.loads(q.final_bytes)
        final['reviews'][0]['status'] = 'unknown' if damage == 'unknown_check' else 'unsupported'
        data = t.encoded(final); q = replace(q, final_bytes=data, final_sha256=t.digest(data))
    else:
        value = json.loads(p)
        value['outcomes'][0].update(status='unknown', reason_code='context_incomplete', documents=[])
        p = t.encoded(value)
    with pytest.raises(t.TypedError):
        freeze(h, proposal=p, check_result=q)


@pytest.mark.parametrize('damage', ['missing_b', 'extra_b', 'wrong_ordinal'])
def test_outcomes_remain_exact_ordered_b_even_with_whole_c_evidence(h, damage):
    value = json.loads(h['p0'])
    if damage == 'missing_b':
        value['outcomes'].pop()
    elif damage == 'extra_b':
        raw = h['task'].raw[-1]
        value['outcomes'].append({**value['outcomes'][0], 'raw_id': raw.raw_id,
                                 'content_sha256': raw.content_sha256, 'ordinal': raw.ordinal})
    else:
        value['outcomes'][0]['ordinal'] = True
    with pytest.raises(t.TypedError, match='typed_coverage_invalid'):
        freeze(h, proposal=t.encoded(value))


@pytest.mark.parametrize('damage', ['omit_change', 'duplicate_change', 'wrong_batch', 'wrong_pending'])
def test_validated_batch_must_match_full_actual_tree_and_b_boundary(h, damage):
    actual = validate(h)
    if damage == 'omit_change':
        actual = replace(actual, changes=actual.changes[:-1])
    elif damage == 'duplicate_change':
        actual = replace(actual, changes=actual.changes + actual.changes[:1])
    elif damage == 'wrong_batch':
        actual = replace(actual, batch_no=2)
    else:
        actual = replace(actual, pending_after=())
    with pytest.raises(t.TypedError):
        refresh(h, validated=actual)


def test_unreferenced_c_source_tail_and_actual_latest_decision_cannot_reuse_ready(h):
    path = h['snapshot'].workspace / h['task'].raw[-1].relative_path
    body = path.read_bytes()
    path.write_bytes(body + b'drift')
    with pytest.raises((t.TypedError, SourceProofError)):
        refresh(h, validated=h['d0'])
    path.write_bytes(body)
    h['ingestion'].captures.decide(h['captures'][-1], 'my_thought')
    with pytest.raises((t.TypedError, SourceProofError)):
        refresh(h, validated=h['d0'])


@pytest.mark.parametrize('path', ['tools/kb.py', '.graph/queries.jsonl', 'new-protected.bin'])
def test_protected_closed_set_drift_rejected_without_cli(h, path):
    target = h['snapshot'].workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((target.read_bytes() if target.exists() else b'') + b'X')
    before = call_count(h['root'])
    with pytest.raises((t.TypedError, SourceProofError)):
        refresh(h, validated=h['d0'])
    assert call_count(h['root']) == before


def test_symlink_to_valid_candidate_bytes_cannot_pass_host_readback(h):
    page = h['snapshot'].workspace / PAGE
    other = h['root'] / 'same-bytes.md'
    other.write_bytes(page.read_bytes())
    page.unlink(); page.symlink_to(other)
    with pytest.raises(t.TypedError):
        refresh(h, validated=h['d0'])


@pytest.mark.parametrize('h', ['large_protected'], indirect=True)
def test_protected_content_bytes_are_hashed_not_charged_to_host_metadata(h, monkeypatch):
    stage = h['stage']
    parts = (stage.binding_json, stage.proposal, stage.check_result.final_bytes,
             stage.changes_json, stage.documents_json, stage.tree_json, stage.receipt_json)
    limit = sum(map(len, parts)) + 8192
    path = '.graph/queries.jsonl'
    protected = h['snapshot'].workspace / path
    assert protected.stat().st_size > limit
    monkeypatch.setattr(t, 'STAGE_METADATA_LIMIT', limit)
    result = refresh(h)
    observed = next(row for row in t._stage_decode(result.tree_json) if row[0] == path)
    assert observed[1:] == [t.digest(protected.read_bytes()), protected.stat().st_size]
    assert result.reusable_check is h['q0']
    data = protected.read_bytes()
    protected.write_bytes(data[:-1] + b'X')
    with pytest.raises(t.TypedError):
        refresh(h, validated=h['d0'])


def test_input_changes_during_host_observation_are_rejected(h, monkeypatch):
    original = t._input_digest
    fired = []
    def digest_then_change(root, path):
        result = original(root, path)
        if path == PAGE and not fired:
            fired.append(True)
            mutate_page(h)
        return result
    monkeypatch.setattr(t, '_input_digest', digest_then_change)
    with pytest.raises(t.TypedError):
        refresh(h, validated=h['d0'])
    assert fired


def test_independent_host_metadata_limit_is_aggregate_and_not_model_policy(h, monkeypatch):
    stage = h['stage']
    parts = (stage.binding_json, stage.proposal, stage.check_result.final_bytes,
             stage.changes_json, stage.documents_json, stage.tree_json, stage.receipt_json)
    total = sum(map(len, parts))
    assert t.STAGE_METADATA_LIMIT == 32 * 1024 * 1024
    input_limit, measurement, before = t.INPUT_LIMIT, h['q0'].input_binding, call_count(h['root'])
    monkeypatch.setattr(t, 'STAGE_METADATA_LIMIT', total)
    assert freeze(h).receipt_json == stage.receipt_json
    monkeypatch.setattr(t, 'STAGE_METADATA_LIMIT', total - 1)
    with pytest.raises(t.TypedError, match='typed_input_limit'):
        freeze(h)
    assert t.INPUT_LIMIT == input_limit and h['q0'].input_binding == measurement
    assert call_count(h['root']) == before
