"""B consumption / C evidence / whole D support; fake CLI is not semantic QA.

The historical schema25 first-pass evidence is sealed separately. The current
actual-source case uses retained self-text captures; schema26 compatibility
still awaits its own run, without lowering a database or fabricating proof.
"""
from dataclasses import replace
import json
import os
import shutil

import pytest

from knowledge_distiller.v1 import wiki_typed as typed, wiki_support as ws
from knowledge_distiller.v1.wiki_outcomes import (
    CONTRACT, Outcome, OutcomeError, WikiOutcomes, full_frozen_context,
)
from knowledge_distiller.v1.wiki_tasks import WikiBatch, FrozenRaw, _boundary
from knowledge_distiller.v1.wiki_staging import SnapshotFile
from .test_wiki_support_runner import h as files, envelope
from .test_wiki_application_input import make_runner, configure, call_count

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='typed transport requires POSIX')
POLICY = typed.APPLICATION_UTF8_POLICY


def contents(h):
    return {r.raw_id: (h['snapshot'].workspace / r.relative_path).read_bytes() for r in h['task'].raw}


def observer(h):
    calls = []
    def capability(*, task, snapshot, context):
        assert tuple(r for r, _ in context) == task.raw
        for r, body in context:
            assert body == (snapshot.workspace / r.relative_path).read_bytes()
            assert typed.digest(body) == r.content_sha256
        calls.append(tuple(r.raw_id for r, _ in context))
        return typed.digest(b'only-synthetic-byte-observer-not-source-qualification')
    return capability, calls


def proposal(h, batch, capability, *, status='processed_with_knowledge'):
    binding, rows, payload = typed.freeze_input(h['task'], h['snapshot'], batch, capability,
                                               runtime_root=h['runtime'])
    value = dict(contract=typed.CONTRACT, schema_revision=1, binding=binding, outcomes=[
        dict(raw_id=r.raw_id, content_sha256=r.content_sha256, ordinal=r.ordinal,
             status=status, reason_code='knowledge_proposed' if status == 'processed_with_knowledge' else 'non_substantive',
             reason='Synthetic complete retained greeting with no independent definition or method.',
             documents=[dict(path=c.path, sha256=c.after_sha256) for c in h['registry'].changes])
        for r, _ in rows])
    return value, rows, payload


def check_value(h, value, rows, payload, *, no_knowledge=False, evidence_from=None):
    documents = typed.checked_documents(h['snapshot'],
        {c.path: c.after_sha256 for c in h['registry'].changes})
    reviews = []
    for raw, _body in rows:
        dims = [dict(dimension=n, status='absent',
                     reason='Synthetic dimension check; model semantics untested.', evidence=[], related_raw_ids=[])
                for n in typed.DIMENSIONS]
        if not no_knowledge:
            body = contents(h)[raw.raw_id].decode()
            dims[0].update(status='present', evidence=[dict(raw_id=raw.raw_id,
                content_sha256=raw.content_sha256, start=0, end=len(body), text=body)])
        if evidence_from is not None:
            rid = evidence_from.raw_id
            body = contents(h)[rid].decode()
            dims[-1].update(status='present', related_raw_ids=[rid], evidence=[
                dict(raw_id=rid, content_sha256=evidence_from.content_sha256,
                     start=0, end=len(body), text=body)])
        reviews.append(dict(raw_id=raw.raw_id, content_sha256=raw.content_sha256,
            status='verified' if no_knowledge else 'unsupported',
            reason='Synthetic retained greeting lacks independent definition, method and retrieval lead.',
            source_check=dict(status='complete',
                reason='Synthetic transport example, not universal platform completeness.',
                evidence_sha256=payload['source_proof_sha256']), dimensions=dims))
    return dict(contract=typed.CHECK_CONTRACT, schema_revision=1, binding=value['binding'],
        proposal_sha256=typed.digest(typed.encoded(value)), changes_sha256=typed.digest(typed.encoded(documents)),
        reviews=reviews)


def checked(h, batch, capability, value, rows, payload, **options):
    r, probes = make_runner(h['tmp'])
    final = check_value(h, value, rows, payload, **options)
    configure(h['tmp'], final)
    result = r.check_json(h['snapshot'], h['runtime'], task=h['task'], batch_no=batch,
        model='fake', effort='medium', proposal=typed.encoded(value),
        changes={c.path: c.after_sha256 for c in h['registry'].changes},
        source_proof=capability, input_policy=POLICY)
    return r, result, probes, final


@pytest.mark.parametrize('batch', [1, 2])
def test_generation_sends_whole_c_but_only_consumes_ordered_b(files, batch):
    cap, calls = observer(files)
    value, rows, payload = proposal(files, batch, cap)
    assert tuple(r for r, _ in rows) == (files['task'].raw[batch - 1],)
    assert [c['frozen']['raw_id'] for c in payload['context_raw']] == [r.raw_id for r in files['task'].raw]
    r, _ = make_runner(files['tmp'])
    configure(files['tmp'], value)
    result = r.run_outcomes(files['snapshot'], files['runtime'], task=files['task'], batch_no=batch,
        model='fake', effort='medium', source_proof=cap, input_policy=POLICY)
    assert result.succeeded
    prompt = (files['tmp'] / 'input.bin').read_bytes()
    sent = json.loads(prompt[prompt.index(b'{"binding"'):])['input']
    assert [c['frozen']['raw_id'] for c in sent['raw']] == [files['task'].raw[batch - 1].raw_id]
    for raw in files['task'].raw:
        row = next(c for c in sent['context_raw'] if c['frozen']['raw_id'] == raw.raw_id)
        assert row['full_raw'].encode() == contents(files)[raw.raw_id]
    assert calls and set(calls) == {tuple(r.raw_id for r in files['task'].raw)}


@pytest.mark.parametrize('damage', ['missing', 'extra', 'wrong_ordinal'])
def test_c_is_not_additional_outcome_consumption(files, damage):
    cap, _ = observer(files)
    value, rows, _ = proposal(files, 2, cap)
    if damage == 'missing':
        value['outcomes'].clear()
    elif damage == 'extra':
        other, _, _ = proposal(files, 1, cap)
        value['outcomes'].extend(other['outcomes'])
    else:
        value['outcomes'][0]['ordinal'] = 1
    with pytest.raises(typed.TypedError, match='typed_coverage_invalid'):
        typed.parse_proposal(typed.encoded(value), value['binding'], rows)


@pytest.mark.parametrize('damage', ['missing', 'extra', 'bytes', 'invalid_batch', 'ordinal'])
def test_explicit_c_checks_entire_boundary_not_just_current_batch(files, damage):
    task, all_bytes = files['task'], contents(files)
    if damage == 'missing':
        all_bytes.pop(task.raw[0].raw_id)
    elif damage == 'extra':
        all_bytes['R-20261008-9999'] = b'unfrozen'
    elif damage == 'bytes':
        all_bytes[task.raw[0].raw_id] += b'changed'
    elif damage == 'invalid_batch':
        task = replace(task, raw=(replace(task.raw[0], batch_no=3), task.raw[1]))
    else:
        task = replace(task, raw=(replace(task.raw[0], ordinal=2), task.raw[1]))
    with pytest.raises(OutcomeError):
        full_frozen_context(task, all_bytes)


def test_b2_checker_can_use_b1_full_range_without_emitting_b1_review(files):
    cap, calls = observer(files)
    value, rows, payload = proposal(files, 2, cap)
    _runner, result, _probes, final = checked(files, 2, cap, value, rows, payload,
                                            evidence_from=files['task'].raw[0])
    assert result.succeeded and len(final['reviews']) == 1
    assert final['reviews'][0]['raw_id'] == files['task'].raw[1].raw_id
    whole = full_frozen_context(files['task'], contents(files))
    assert typed.parse_check(result.final_bytes, value['binding'], rows,
        proposal_sha256=final['proposal_sha256'], changes_sha256=final['changes_sha256'],
        source_proof_sha256=payload['source_proof_sha256'], full_context=whole) == final
    assert set(calls) == {tuple(r.raw_id for r in files['task'].raw)}


@pytest.mark.parametrize('damage', ['outside_c', 'related_outside_c', 'range', 'text', 'hash', 'extra_review'])
def test_full_c_reference_is_exact_not_historical_page_self_evidence(files, damage):
    cap, _ = observer(files)
    value, rows, payload = proposal(files, 2, cap)
    final = check_value(files, value, rows, payload, evidence_from=files['task'].raw[0])
    evidence = final['reviews'][0]['dimensions'][-1]['evidence'][0]
    if damage == 'outside_c':
        evidence['raw_id'] = 'R-20261008-9999'
    elif damage == 'related_outside_c':
        final['reviews'][0]['dimensions'][-1]['related_raw_ids'] = ['R-20261008-9999']
    elif damage == 'range':
        evidence['end'] += 1
    elif damage == 'text':
        evidence['text'] += 'wrong'
    elif damage == 'hash':
        evidence['content_sha256'] = '0' * 64
    else:
        final['reviews'].append(final['reviews'][0])
    with pytest.raises(typed.TypedError):
        typed.parse_check(typed.encoded(final), value['binding'], rows,
            proposal_sha256=final['proposal_sha256'], changes_sha256=final['changes_sha256'],
            source_proof_sha256=payload['source_proof_sha256'],
            full_context=full_frozen_context(files['task'], contents(files)))


def test_uncited_c_drift_after_spawn_rejects_checker_result(files):
    cap, _ = observer(files)
    value, rows, payload = proposal(files, 1, cap)
    r, _ = make_runner(files['tmp'])
    final = check_value(files, value, rows, payload)
    uncited = files['snapshot'].workspace / files['task'].raw[1].relative_path
    configure(files['tmp'], final, mutate=f'Path({str(uncited)!r}).write_bytes({contents(files)[files["task"].raw[1].raw_id] + b"changed"!r})\n')
    result = r.check_json(files['snapshot'], files['runtime'], task=files['task'], batch_no=1,
        model='fake', effort='medium', source_proof=cap, input_policy=POLICY,
        proposal=typed.encoded(value), changes={c.path: c.after_sha256 for c in files['registry'].changes})
    assert not result.succeeded and call_count(files['tmp']) == 1


def test_unreferenced_c_missing_before_generation_has_zero_probe_and_spawn(files):
    cap, _ = observer(files)
    (files['snapshot'].workspace / files['task'].raw[1].relative_path).unlink()
    r, probes = make_runner(files['tmp'])
    result = r.run_outcomes(files['snapshot'], files['runtime'], task=files['task'], batch_no=1,
        model='fake', effort='medium', source_proof=cap, input_policy=POLICY)
    assert not result.succeeded and probes == [] and call_count(files['tmp']) == 0


def outcomes(value, task):
    return tuple(Outcome(o['raw_id'], o['content_sha256'], CONTRACT, task.boundary_sha256,
        o['status'], o['reason_code'], o['reason'], tuple((d['path'], d['sha256']) for d in o['documents']))
        for o in value['outcomes'])


@pytest.mark.parametrize('damage', ['none', 'registry_b', 'missing_gate', 'boolean_check', 'all_no_knowledge'])
def test_explicit_outcomes_require_full_registry_and_real_check_not_bool(files, tmp_path, damage):
    cap, _ = observer(files)
    value, rows, payload = proposal(files, 1, cap,
        status='processed_no_knowledge' if damage == 'all_no_knowledge' else 'processed_with_knowledge')
    r, result, _, _ = checked(files, 1, cap, value, rows, payload, no_knowledge=damage == 'all_no_knowledge')
    assert result.succeeded
    registry = files['registry']
    if damage == 'registry_b':
        registry = ws.build_registry(files['snapshot'].workspace, registry.changes, registry.raws[:1])
    client = r.support_client(files['snapshot'], files['runtime'], task=files['task'], registry=files['registry'],
        model='fake', effort='medium', source_proof=cap, input_policy=POLICY)
    gate = ws.WikiSupportGate(tmp_path / 'gate', 'same-batch', registry, client.model_config_hash)
    candidate = WikiOutcomes(tmp_path / 'candidate.sqlite3')
    candidate.initialize()
    configure(files['tmp'], envelope(client))
    args = dict(checker=None, support_gate=None if damage == 'missing_gate' else gate,
        support_client=client, full_contents=contents(files), proposal=typed.encoded(value),
        check_result=True if damage == 'boolean_check' else result)
    before = call_count(files['tmp'])
    if damage != 'none':
        with pytest.raises(OutcomeError):
            candidate.validate(files['task'], 1, {rows[0][0].raw_id: rows[0][1]}, outcomes(value, files['task']), **args)
        assert call_count(files['tmp']) == before
        assert not (gate.directory / 'state.json').exists()
    else:
        receipt = candidate.validate(files['task'], 1, {rows[0][0].raw_id: rows[0][1]}, outcomes(value, files['task']), **args)
        saved = candidate.get(receipt)
        assert len(saved['raw']) == 1 and len(saved['context_raw']) == 2
        assert saved['support'] and saved['check_sha256'] == result.final_sha256
        assert call_count(files['tmp']) == before + 1


@pytest.mark.parametrize('damage', ['missing_review', 'unsupported_claim'])
def test_unlisted_d_document_is_not_omitted_from_support_gate(files, tmp_path, damage):
    cap, _ = observer(files)
    old = files['registry']
    extra = 'wiki/额外健康结论.md'
    body = ('作者甲认为仅低温数值不是12而是10。（' + old.raws[0].path + '#^source-1）\n').encode()
    (files['snapshot'].workspace / extra).write_bytes(body)
    files['registry'] = ws.build_registry(files['snapshot'].workspace,
        old.changes + (ws.DocumentChange(extra, None, None, body, typed.digest(body)),), old.raws)
    assert any(c.block.path == extra for c in files['registry'].claims)
    value, rows, payload = proposal(files, 1, cap)
    # Outcome only cites the original document; the checker/support inputs still include D.
    value['outcomes'][0]['documents'] = [d for d in value['outcomes'][0]['documents'] if d['path'] != extra]
    r, result, _, _ = checked(files, 1, cap, value, rows, payload)
    assert result.succeeded
    client = r.support_client(files['snapshot'], files['runtime'], task=files['task'], registry=files['registry'],
        model='fake', effort='medium', source_proof=cap, input_policy=POLICY)
    final = envelope(client)
    hidden_ids = {c.block.claim_id for c in files['registry'].claims if c.block.path == extra}
    if damage == 'missing_review':
        final['checks'] = [c for c in final['checks'] if c['claim_id'] not in hidden_ids]
    else:
        for c in final['checks']:
            if c['claim_id'] in hidden_ids:
                c.update(status='unsupported', issues=[dict(field='text', category='condition', reason='Synthetic unsupported extra claim.')])
    configure(files['tmp'], final)
    gate = ws.WikiSupportGate(tmp_path / 'gate', 'whole-d', files['registry'], client.model_config_hash)
    candidate = WikiOutcomes(tmp_path / 'candidate.sqlite3'); candidate.initialize()
    with pytest.raises(OutcomeError, match='source_support_failed'):
        candidate.validate(files['task'], 1, {rows[0][0].raw_id: rows[0][1]}, outcomes(value, files['task']),
            checker=None, support_gate=gate, support_client=client, full_contents=contents(files),
            proposal=typed.encoded(value), check_result=result)
    assert (gate.directory / 'state.json').exists()


def test_technical_unknown_check_cannot_be_normalized_by_supported_gate(files, tmp_path):
    cap, _ = observer(files)
    value, rows, payload = proposal(files, 1, cap)
    final = check_value(files, value, rows, payload)
    final['reviews'][0]['status'] = 'unknown'
    r, _ = make_runner(files['tmp']); configure(files['tmp'], final)
    result = r.check_json(files['snapshot'], files['runtime'], task=files['task'], batch_no=1,
        model='fake', effort='medium', source_proof=cap, input_policy=POLICY, proposal=typed.encoded(value),
        changes={c.path: c.after_sha256 for c in files['registry'].changes})
    assert result.succeeded  # valid unknown candidate, not a normal result
    client = r.support_client(files['snapshot'], files['runtime'], task=files['task'], registry=files['registry'],
        model='fake', effort='medium', source_proof=cap, input_policy=POLICY)
    gate = ws.WikiSupportGate(tmp_path / 'gate', 'unknown', files['registry'], client.model_config_hash)
    candidate = WikiOutcomes(tmp_path / 'candidate.sqlite3'); candidate.initialize()
    before = call_count(files['tmp'])
    with pytest.raises(OutcomeError, match='outcome_classification_unknown'):
        candidate.validate(files['task'], 1, {rows[0][0].raw_id: rows[0][1]}, outcomes(value, files['task']),
            checker=None, support_gate=gate, support_client=client, full_contents=contents(files),
            proposal=typed.encoded(value), check_result=result)
    assert call_count(files['tmp']) == before and not (gate.directory / 'state.json').exists()


def test_legacy_default_no_knowledge_keeps_batch_context_without_new_gate(files, tmp_path):
    from .test_wiki_outcomes import Checker
    cap, _ = observer(files)
    value, rows, _ = proposal(files, 2, cap, status='processed_no_knowledge')
    checker = Checker()
    candidate = WikiOutcomes(tmp_path / 'candidate.sqlite3'); candidate.initialize()
    receipt = candidate.validate(files['task'], 2, {rows[0][0].raw_id: rows[0][1]},
                                 outcomes(value, files['task']), checker=checker)
    assert checker.calls[0][2] == rows
    assert 'context_raw' not in candidate.get(receipt) and candidate.get(receipt)['support'] is None


def test_actual_callback_receives_whole_c_and_all_no_knowledge_still_checks_d(tmp_path):
    # Actual existing writers/events/lock/proof; no SQL proof or complete bool.
    from knowledge_distiller.v1.database import connect
    from knowledge_distiller.v1.captures import record_capture
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.ingestion import Ingestion
    from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback
    from knowledge_distiller.v1.wiki_lock import VaultWriteLock, vault_key
    from knowledge_distiller.v1.wiki_staging import StagingSnapshot
    from knowledge_distiller.v1.wiki_tasks import WikiTask
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite3'); store.initialize()
    vault = root / 'vault'; vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    ingestion, records = Ingestion(store), []
    for index, text in enumerate(('独立完整原文第一条。', '完整投递文本仅表示感谢。')):
        app, message = f'synthetic-self-{index}', f'synthetic-message-{index}'
        with connect(store.path) as db:
            record_capture(db, app, message, message_type='text',
                created_ms=1790000000000, received_ms=1790000000000,
                text=text, vault=vault)
        cid = ingestion.captures.for_message(app, message)['capture_id']
        ingestion.captures.decide(cid, 'my_thought')  # actual synthetic user action
        receipt = ingestion.capture(cid, vault)
        records.append(ingestion.ledger.record(receipt.raw_id))
    runtime = root / 'runtime'
    task_root = runtime / 'wiki-tasks' / ('a' * 32) / 'attempts' / ('d' * 32)
    workspace, control, backup = [task_root / name for name in ('workspace', 'control', 'backup')]
    for path in (workspace, control, backup):
        path.mkdir(parents=True, mode=0o700); path.chmod(0o700)
    shutil.copytree(vault / 'raw', workspace / 'raw')
    rows = tuple(FrozenRaw(rec['relative_path'], rec['raw_id'], rec['identity'], len(rec['content'].encode()),
        rec['content_sha256'], n, n) for n, rec in enumerate(records, 1))
    baseline = tuple(SnapshotFile(r.relative_path, 'raw', r.byte_count, r.content_sha256) for r in rows)
    task = WikiTask('a' * 32, str(vault), vault_key(vault), 'all', 'local_web', 'codex_cli', 'fake', 'medium',
        'synthetic', 'c' * 64, _boundary(((rows[0],), (rows[1],))), 'running', 2, 2, 1, None,
        'not_needed', 'none', '', '', (WikiBatch(1, 'succeeded', 1, None), WikiBatch(2, 'running', 1, None)), rows)
    snapshot = StagingSnapshot(task.task_id, task_root, workspace, control, backup, baseline,
                               tuple(r.relative_path for r in rows))
    path = 'wiki/来源/完整候选.md'
    after = ('独立完整原文第一条。（' + rows[0].relative_path + '#^source-1）\n').encode()
    (workspace / path).parent.mkdir(parents=True); (workspace / path).write_bytes(after)
    all_bytes = {r.raw_id: (workspace / r.relative_path).read_bytes() for r in rows}
    registry = ws.build_registry(workspace, (ws.DocumentChange(path, None, None, after, typed.digest(after)),),
        tuple(ws.FrozenRaw(r.relative_path, r.raw_id, all_bytes[r.raw_id], r.content_sha256) for r in rows))
    h = dict(tmp=root, runtime=runtime, task=task, snapshot=snapshot, registry=registry)
    with VaultWriteLock.acquire(vault) as lock:
        cap = trusted_source_callback(store, lock)
        value, b, payload = proposal(h, 2, cap, status='processed_no_knowledge')
        qualification = cap.verify(task=task, snapshot=snapshot, context=full_frozen_context(task, all_bytes))
        assert qualification.digest == payload['source_proof_sha256']
        assert all('declared_capture_verified' in s['capabilities'] for s in qualification.manifest['sources'])
        assert all(s['identity'] == '本人' and s['scope']['kind'] == 'retained_literal'
                   and s['scope']['platform_total_verified'] is False
                   for s in qualification.manifest['sources'])
        r, checked_result, _, _ = checked(h, 2, cap, value, b, payload, no_knowledge=True)
        assert checked_result.succeeded
        client = r.support_client(snapshot, runtime, task=task, registry=registry, model='fake', effort='medium',
                                  source_proof=cap, input_policy=POLICY)
        gate = ws.WikiSupportGate(root / 'gate', 'all-no-knowledge', registry, client.model_config_hash)
        candidate = WikiOutcomes(root / 'candidate.sqlite3'); candidate.initialize()
        # An unsupported claim in D cannot be ignored just because all B are noKnowledge.
        configure(root, envelope(client, status='unsupported'))
        before = call_count(root)
        with pytest.raises(OutcomeError, match='source_support_failed'):
            candidate.validate(task, 2, {b[0][0].raw_id: b[0][1]}, outcomes(value, task), checker=None,
                support_gate=gate, support_client=client, full_contents=all_bytes,
                proposal=typed.encoded(value), check_result=checked_result)
        assert call_count(root) == before + 1 and (gate.directory / 'state.json').is_file()
