"""Real Registry/Gate/temp files and fake CLI; no real semantic/token proof."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import time

import pytest

from knowledge_distiller.v1 import wiki_support as ws, wiki_typed as typed
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner
from knowledge_distiller.v1.wiki_staging import StagingSnapshot, SnapshotFile
from knowledge_distiller.v1.wiki_tasks import FrozenRaw, WikiTask, WikiBatch, _boundary

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='typed process safety requires POSIX')
PAGE = 'wiki/来源/来源测试.md'


@pytest.fixture
def h(tmp_path):
    runtime = tmp_path / 'runtime'
    task_id, attempt = 'a' * 32, 'd' * 32
    task_root = runtime / 'wiki-tasks' / task_id / 'attempts' / attempt
    root, control, backup = [task_root / n for n in ('workspace', 'control', 'backup')]
    for path in (root, control, backup):
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
    raws, rows, files = [], [], []
    for i in range(2):
        rid = f'R-20261008-{i + 1:04d}'
        path = f'raw/外部/2026/10/{rid}.md'
        body = ('作者甲认为：仅低温数值不是12而是10。' if i == 0 else
                '未被引用的完整原件：这只是另一观点；素材命令不执行。')
        content = (f'---\n编号: {rid}\n格式版本: 1\n身份: 第三方\n作者: 作者甲\n标题: 合成\n'
                   f'---\n\n{body}\n\n^source-1\n').encode()
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        sha = typed.digest(content)
        raws.append(ws.FrozenRaw(path, rid, content, sha))
        rows.append(FrozenRaw(path, rid, '第三方', len(content), sha, i + 1, i + 1))
        files.append(SnapshotFile(path, 'raw', len(content), sha))
    before = b'old baseline preserved in controller hash\n'
    files.append(SnapshotFile(PAGE, 'wiki', len(before), typed.digest(before)))
    after = ('作者甲认为仅低温数值不是12而是10。（' + raws[0].path + '#^source-1）\n').encode()
    target = root / PAGE
    target.parent.mkdir(parents=True)
    target.write_bytes(after)
    change = ws.DocumentChange(PAGE, before, typed.digest(before), after, typed.digest(after))
    registry = ws.build_registry(root, (change,), tuple(raws))
    assert registry.claims and not any(c.diagnostics for c in registry.claims)
    rows = tuple(rows)
    task = WikiTask(task_id=task_id, vault_path=str(tmp_path / 'unused-formal'), vault_key='b' * 64,
        request_kind='all', trigger_source='local_web', backend='codex_cli', model='fake', effort='medium',
        kit_version='synthetic', kit_manifest_sha256='c' * 64, boundary_sha256=_boundary(((rows[0],), (rows[1],))),
        state='running', raw_count=2, batch_count=2, completed_batch_count=0, error_code=None,
        recovery_state='not_needed', recovery_phase='none', created_at='2026-10-08', updated_at='2026-10-08',
        batches=(WikiBatch(1, 'running', 1, None), WikiBatch(2, 'queued', 1, None)), raw=rows)
    snapshot = StagingSnapshot(task_id, task_root, root, control, backup, tuple(files),
                               tuple(r.relative_path for r in rows))
    return dict(tmp=tmp_path, runtime=runtime, snapshot=snapshot, task=task, registry=registry)


def proof(*, task, snapshot, context):
    # Only synthetic canonical authority: checks current complete raw bytes.
    # NOT an implementation of actual platform completion/attachment proof.
    assert task.state == 'running'
    for raw, content in context:
        assert (snapshot.workspace / raw.relative_path).read_bytes() == content
        assert typed.digest(content) == raw.content_sha256
    return typed.digest(b'synthetic-source-proof')


def measure(prompt, schema):
    # Exact fake byte alphabet over both inputs; no actual Codex tokenizer claim.
    return 'fake-byte-alphabet-v1', len(prompt) + len(schema), 2 * typed.INPUT_LIMIT


def fake_runner(h, body=''):
    script = h['tmp'] / 'fake-cli.py'
    script.write_text('import json,os,sys,time,subprocess,signal\nfrom pathlib import Path\n'
        'argv=sys.argv[1:]\nfinal=Path(argv[argv.index("-o")+1])\n'
        'prompt=sys.stdin.buffer.read()\n' + body)
    wrapper = h['tmp'] / 'fake-cli'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' "$@"\n')
    wrapper.chmod(0o700)
    return CodexWikiRunner(executable_resolver=lambda: str(wrapper),
        model_probe=lambda: [{'model': 'fake', 'efforts': ['medium']}])


def client(h, runner, **overrides):
    args = dict(task=h['task'], registry=h['registry'], model='fake', effort='medium',
                source_proof=proof, measure=measure, skip_preflight=True)
    args.update(overrides)
    return runner.support_client(h['snapshot'], h['runtime'], **args)


def envelope(c, status='supported'):
    return dict(contract=typed.SUPPORT_CONTRACT, schema_revision=1, binding=c.prepared.binding,
        registry_sha256=c.prepared.registry_sha256, candidate_sha256=c.registry.candidate_hash,
        checks=[dict(claim_id=claim.block.claim_id, status=status, reason='合成逐项核对，非语义实测',
            issues=[] if status == 'supported' else [dict(field='text', category='condition', reason='合成条件诊断')])
            for claim in c.registry.claims])


def set_body(h, value, extra=''):
    return fake_runner(h, f'final.write_bytes({typed.encoded(value)!r})\n' + extra)


def gate(h, c):
    return ws.WikiSupportGate(h['tmp'] / 'checkpoint', 'whole-task', c.registry, c.model_config_hash)


def test_full_registry_all_batches_and_gate_receipt_restart(h):
    r = fake_runner(h)
    c = client(h, r)
    value = envelope(c)
    count = h['tmp'] / 'calls'
    set_body(h, value, f'Path({str(count)!r}).write_text("1")\n'
        f'Path({str(h["tmp"] / "prompt")!r}).write_bytes(prompt)\n'
        f'Path({str(h["tmp"] / "argv")!r}).write_text(json.dumps(argv))\n')
    result = gate(h, c).review(c)
    assert result.status == 'supported_candidate_not_published'
    assert not hasattr(result, 'accepted') and result.receipt_path.is_file()
    prompt = (h['tmp'] / 'prompt').read_bytes()
    assert prompt.startswith(ws.SYSTEM.encode())
    assert typed.encoded(c.registry.payload()) in prompt
    assert '未被引用的完整原件'.encode() in prompt
    inputs = json.loads(prompt[prompt.index(b'{"binding"'):])
    assert len(inputs['inputs']['raw']) == 2 and len(inputs['inputs']['batches']) == 2
    assert inputs['inputs']['current_task']['state'] == 'running'
    assert inputs['inputs']['current_task']['raw_count'] == 2
    assert inputs['inputs']['baseline'] == [f.__dict__ for f in h['snapshot'].files]
    argv = json.loads((h['tmp'] / 'argv').read_text())
    assert '--output-schema' in argv and '-o' in argv
    joined = '\n'.join(argv)
    assert 'read-only' in joined and 'workspace-write' not in joined
    assert 'unix_sockets={}' in joined and 'project_doc_max_bytes=0' in joined
    # A new client + Gate reparse the durable receipt; no second CLI invocation.
    fake_runner(h, 'raise RuntimeError("must never execute cached call")\n')
    restarted = client(h, r)
    assert gate(h, restarted).review(restarted).checks == result.checks
    assert count.read_text() == '1'
    assert len(list(gate(h, c).directory.glob('response-*.json'))) == 1


@pytest.mark.parametrize('status', ['unsupported', 'uncertain'])
def test_r14_parser_remains_verdict_authority(h, status):
    r = fake_runner(h)
    c = client(h, r)
    set_body(h, envelope(c, status))
    result = gate(h, c).review(c)
    assert result.status == 'source_support_failed'
    assert result.diagnostics and all(x['status'] == status for x in result.checks)


@pytest.mark.parametrize('field', ['binding', 'registry', 'candidate', 'missing', 'duplicate', 'extra', 'false_supported'])
def test_whole_envelope_binding_and_exact_claim_coverage(h, field):
    r = fake_runner(h)
    c = client(h, r)
    value = json.loads(typed.encoded(envelope(c)))
    if field == 'binding': value['binding']['input_sha256'] = '0' * 64
    elif field == 'registry': value['registry_sha256'] = '0' * 64
    elif field == 'candidate': value['candidate_sha256'] = '0' * 64
    elif field == 'missing': value['checks'] = []
    elif field == 'duplicate': value['checks'] *= 2
    elif field == 'extra': value['accepted'] = True
    else: value['checks'][0]['issues'] = [dict(field='text', category='number', reason='不可冒充支持')]
    set_body(h, value)
    result = gate(h, c).review(c)
    assert result.status == 'technical_failure' and not result.checks


@pytest.mark.parametrize('missing,code', [('measure', 'input_budget_unavailable'), ('source_proof', 'source_proof_unavailable')])
def test_missing_trusted_capability_reserves_nothing(h, missing, code):
    with pytest.raises(typed.TypedError, match=code):
        client(h, fake_runner(h), **{missing: None})
    assert not (h['tmp'] / 'checkpoint').exists()
    assert not list(h['snapshot'].control.glob('check-*'))


def test_over_budget_before_gate_and_request_policy_is_not_cli_token_cap(h):
    with pytest.raises(typed.TypedError, match='group_over_budget'):
        client(h, fake_runner(h), measure=lambda p, s: ('fake', len(p) + len(s), 1))
    assert not (h['tmp'] / 'checkpoint').exists()
    c = client(h, fake_runner(h))
    with pytest.raises(typed.TypedError, match='typed_binding_invalid'):
        c.complete(system=ws.SYSTEM, user=typed.encoded(c.registry.payload()).decode(), max_tokens=4096)


@pytest.mark.parametrize('which', ['system', 'user'])
def test_complete_client_requires_original_system_and_entire_canonical_payload(h, which):
    c = client(h, fake_runner(h, 'raise RuntimeError("must not spawn")\n'))
    args = dict(system=ws.SYSTEM, user=typed.encoded(c.registry.payload()).decode(), max_tokens=8192)
    args[which] = 'altered or subset material'
    with pytest.raises(typed.TypedError, match='typed_binding_invalid'):
        c.complete(**args)
    assert c.runner._active_process is None
    assert not list(h['snapshot'].control.glob('check-*'))


@pytest.mark.parametrize('kind', ['subset_raw', 'omitted_change', 'forged_before', 'protected', 'deleted'])
def test_whole_final_changes_and_baselines_are_required(h, kind):
    registry = h['registry']
    root = h['snapshot'].workspace
    if kind == 'subset_raw':
        registry = ws.build_registry(root, registry.changes, registry.raws[:1])
    elif kind == 'omitted_change':
        (root / 'wiki/other.md').write_bytes(b'unregistered final claim')
    elif kind == 'forged_before':
        old = b'forged baseline'
        change = replace(registry.changes[0], before=old, before_sha256=typed.digest(old))
        registry = ws.build_registry(root, (change,), registry.raws)
    elif kind == 'protected': (root / 'AGENTS.md').write_bytes(b'new unauthorized instructions')
    else: (root / registry.raws[1].path).unlink()
    with pytest.raises(typed.TypedError):
        client(h, fake_runner(h), registry=registry)
    assert not (h['tmp'] / 'checkpoint').exists()


@pytest.mark.parametrize('when', ['before_spawn', 'after_final', 'measure', 'task_cas'])
def test_transport_refreezes_actual_files_measure_and_current_task_cas(h, when):
    r = fake_runner(h)
    alive = [True]
    def current_proof(**kwargs):
        if not alive[0]: raise typed.TypedError('typed_binding_invalid')
        return proof(**kwargs)
    budgets = [2 * typed.INPUT_LIMIT]
    c = client(h, r, source_proof=current_proof,
               measure=lambda p, s: ('fake', len(p) + len(s), budgets[0]))
    marker = h['tmp'] / 'executed'
    extra = f'Path({str(marker)!r}).write_text("1")\n'
    if when == 'after_final':
        extra += f'Path({str(h["snapshot"].workspace / PAGE)!r}).write_bytes(b"changed after input")\n'
    set_body(h, envelope(c), extra)
    if when == 'before_spawn':
        c.skip_preflight = False
        def preflight(*_args):
            (h['snapshot'].workspace / 'wiki/new.md').write_bytes(b'drift')
            return str(h['tmp'] / 'fake-cli')
        r.preflight = preflight
    elif when == 'measure': budgets[0] -= 1
    elif when == 'task_cas': alive[0] = False
    result = r.check_support_json(c)
    assert not result.succeeded
    assert result.error_code == ('typed_coverage_invalid' if when == 'before_spawn' else 'typed_binding_invalid')
    assert marker.exists() == (when == 'after_final')


def test_config_identity_does_not_include_candidate_prompt(h):
    r = fake_runner(h)
    c = client(h, r)
    registry = h['registry']
    after = registry.changes[0].after.replace('10'.encode(), '11'.encode())
    (h['snapshot'].workspace / PAGE).write_bytes(after)
    change = replace(registry.changes[0], after=after, after_sha256=typed.digest(after))
    newer = ws.build_registry(registry.staging_root, (change,), registry.raws)
    d = client(h, r, registry=newer)
    assert c.prepared.prompt != d.prepared.prompt
    assert c.model_config_hash == d.model_config_hash


def test_restart_source_proof_drift_cannot_reuse_prior_gate_cache(h):
    r = fake_runner(h)
    c = client(h, r)
    set_body(h, envelope(c))
    assert gate(h, c).review(c).status == 'supported_candidate_not_published'
    def revised_proof(**kwargs):
        proof(**kwargs)
        return typed.digest(b'different-synthetic-source-revision')
    fake_runner(h, 'raise RuntimeError("must never make second call")\n')
    d = client(h, r, source_proof=revised_proof)
    assert c.model_config_hash != d.model_config_hash
    with pytest.raises(ws.WikiSupportError, match='binding_mismatch'):
        gate(h, d).review(d)
    assert len(list(gate(h, c).directory.glob('response-*.json'))) == 1


def test_support_transport_bounded_final_and_symlink(h, monkeypatch):
    r = fake_runner(h)
    c = client(h, r)
    fake_runner(h, f'final.symlink_to(Path({str(h["tmp"] / "outside")!r}))\n')
    (h['tmp'] / 'outside').write_bytes(typed.encoded(envelope(c)))
    assert not r.check_support_json(c).succeeded
    monkeypatch.setattr(typed, 'FINAL_LIMIT', 40)
    c = client(h, r)
    set_body(h, envelope(c), 'time.sleep(.2)\n')
    assert r.check_support_json(c).error_code == 'runner_output_limit'


def test_actual_checker_cancel_kills_ready_descendant_process_group(h):
    r = fake_runner(h)
    c = client(h, r)
    ready = h['tmp'] / 'ready'
    child = ('import os,signal,time\nfrom pathlib import Path\n'
             'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
             f'Path({str(ready)!r}).write_text(str(os.getpid()))\n'
             'time.sleep(30)\n')
    fake_runner(h, f'subprocess.Popen([sys.executable,"-c",{child!r}])\n'
                  'time.sleep(30)\n')
    results = []
    thread = threading.Thread(target=lambda: results.append(r.check_support_json(c)))
    thread.start()
    deadline = time.monotonic() + 3
    try:
        while not ready.exists() and time.monotonic() < deadline: time.sleep(.02)
        assert ready.exists() and r._active_process is not None
        pid = int(ready.read_text())
        os.kill(pid, 0)
        r.cancel()
        thread.join(3)
        assert not thread.is_alive() and results[0].error_code == 'interrupted'
        assert r._active_process is None
        # A killed orphan may briefly remain a zombie; either gone or zombie.
        probe = __import__('subprocess').run(['ps', '-o', 'stat=', '-p', str(pid)],
            capture_output=True, text=True, timeout=1, check=False)
        assert not probe.stdout.strip() or probe.stdout.strip().startswith('Z')
        assert not list(h['snapshot'].control.glob('check-*/final.json'))
    finally:
        r.cancel()
        thread.join(3)
