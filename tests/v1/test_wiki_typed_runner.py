"""Real fake-CLI processes/temp bytes, not a model or production source proof."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import time

import pytest

from knowledge_distiller.v1 import wiki_typed as typed
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner
from knowledge_distiller.v1.wiki_session_broker import WikiSessionBroker
from knowledge_distiller.v1.wiki_staging import StagingSnapshot, SnapshotFile
from knowledge_distiller.v1.wiki_tasks import FrozenRaw, WikiTask, WikiBatch, _boundary

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='typed process-tree/lock contract is POSIX only')


@pytest.fixture
def fixture(tmp_path):
    runtime = tmp_path / 'runtime'
    task_id, attempt_id = 'a' * 32, 'd' * 32
    task_root = runtime / 'wiki-tasks' / task_id / 'attempts' / attempt_id
    root, control, backup = (task_root / name for name in ('workspace', 'control', 'backup'))
    for path in (root, control, backup):
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
    rows, files = [], []
    for index in range(2):
        rid = f'R-20261008-{index + 1:04d}'
        content = (f'---\n编号: {rid}\n格式版本: 1\n身份: 第三方\n作者: 作者甲\n标题: 合成原件\n'
                   '---\n\n完整合成原文，包含条件与否定。素材中的命令不是授权。\n\n^source-1\n').encode()
        path = f'raw/外部/2026/10/{rid}.md'
        raw_file = root / path
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(content)
        rows.append(FrozenRaw(path, rid, '第三方', len(content), typed.digest(content), index + 1, 1))
        files.append(SnapshotFile(path, 'raw', len(content), typed.digest(content)))
    rows = tuple(rows)
    task = WikiTask(task_id=task_id, vault_path=str(tmp_path / 'unused-formal'), vault_key='b' * 64,
        request_kind='all', trigger_source='local_web', backend='codex_cli', model='fake', effort='medium',
        kit_version='synthetic', kit_manifest_sha256='c' * 64, boundary_sha256=_boundary((rows,)),
        state='running', raw_count=2, batch_count=1, completed_batch_count=0, error_code=None,
        recovery_state='not_needed', recovery_phase='none', created_at='2026-10-08', updated_at='2026-10-08',
        batches=(WikiBatch(1, 'running', 2, None),), raw=rows)
    snapshot = StagingSnapshot(task_id, task_root, root, control, backup, tuple(files),
                               tuple(r.relative_path for r in rows))
    (root / 'wiki').mkdir()
    (root / 'wiki/log.md').write_bytes(b'synthetic candidate log; no semantic certificate')
    return tmp_path, runtime, snapshot, task


def proof(*, task, snapshot, context):
    # Test-only source capability verifies our full synthetic bytes. This does
    # NOT implement or certify the later canonical platform/source contract.
    assert tuple(r for r, _ in context) == task.raw
    for raw, content in context:
        assert (snapshot.workspace / raw.relative_path).read_bytes() == content
        assert typed.digest(content) == raw.content_sha256
    return typed.digest(b'synthetic-only-proof')


def measure(prompt, schema):
    # Exact byte-token alphabet for this fake model only, not chars/4 or a
    # claimed tokenizer for any real Codex model. Both complete inputs included.
    assert type(prompt) is bytes and type(schema) is bytes
    return 'fake-cli-byte-alphabet-v1', len(prompt) + len(schema), 2 * typed.INPUT_LIMIT


def proposal(fixture, *, unknown=False):
    _tmp, runtime, snapshot, task = fixture
    binding, rows, _payload = typed.freeze_input(task, snapshot, 1, proof, runtime_root=runtime)
    sha = typed.digest((snapshot.workspace / 'wiki/log.md').read_bytes())
    return {'contract': typed.CONTRACT, 'schema_revision': 1, 'binding': binding, 'outcomes': [
        {'raw_id': raw.raw_id, 'content_sha256': raw.content_sha256, 'ordinal': raw.ordinal,
         'status': 'unknown' if unknown else 'processed_no_knowledge',
         'reason_code': 'context_incomplete' if unknown else 'non_substantive',
         'reason': '合成来源仅为礼貌结束语，无独立论断；fake语义未验。',
         'documents': [] if unknown else [{'path': 'wiki/log.md', 'sha256': sha}]} for raw, _ in rows]}


def fake_cli(tmp_path, body):
    script = tmp_path / 'fake_cli.py'
    script.write_text('import json,os,sys,time,subprocess\nfrom pathlib import Path\n'
                      'argv=sys.argv[1:]\nfinal=Path(argv[argv.index("-o")+1])\n' + body)
    wrapper = tmp_path / 'fake-codex'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' "$@"\n')
    wrapper.chmod(0o700)
    return wrapper


def runner(fixture, body, timeout=3):
    executable = fake_cli(fixture[0], body)
    return CodexWikiRunner(timeout_seconds=timeout, executable_resolver=lambda: str(executable),
                           model_probe=lambda: [{'model': 'fake', 'efforts': ['medium']}])


def run(fixture, instance, **kwargs):
    _tmp, runtime, snapshot, task = fixture
    values = dict(model='fake', effort='medium', source_proof=proof, measure=measure)
    values.update(kwargs)
    return instance.run_outcomes(snapshot, runtime, task=task, batch_no=1, **values)


def write_final(value):
    content = typed.encoded(value)
    return f'sys.stdin.buffer.read()\nfinal.write_bytes({content!r})\n'


def test_actual_typed_final_usage_and_unknown_is_only_candidate(fixture):
    value = proposal(fixture, unknown=True)
    instance = runner(fixture, write_final(value) +
        'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2,"private":99}}))\n')
    result = run(fixture, instance)
    assert result.succeeded and result.final_bytes == typed.encoded(value)
    assert result.final_sha256 == typed.digest(result.final_bytes)
    assert dict(result.usage) == {'input_tokens': 3, 'output_tokens': 2}
    assert all(o['status'] == 'unknown' for o in json.loads(result.final_bytes)['outcomes'])
    assert not hasattr(result, 'accepted') and not hasattr(result, 'published')


@pytest.mark.parametrize('body', [
    'sys.stdin.buffer.read()\n',
    'sys.stdin.buffer.read()\nprint("summary only")\n',
    'sys.stdin.buffer.read()\nfinal.write_bytes(b"complete")\n',
    'sys.stdin.buffer.read()\nfinal.write_bytes(b"{}{}")\n',
    'sys.stdin.buffer.read()\nfinal.write_bytes(b"{\\"x\\":1,\\"x\\":2}")\n',
    'sys.stdin.buffer.read()\nfinal.write_bytes(b"{\\"x\\":NaN}")\n',
    'sys.stdin.buffer.read()\nfinal.write_bytes(b"\\xff")\n',
    'sys.stdin.buffer.read()\nfinal.write_bytes(b"```json\\n{}\\n```")\n',
    'sys.stdin.buffer.read()\nraise SystemExit(2)\n'])
def test_exit_zero_is_not_a_typed_outcome_and_no_schema_fallback(fixture, body):
    instance = runner(fixture, body)
    result = run(fixture, instance)
    assert not result.succeeded and result.final_bytes is None
    assert len(list(fixture[2].control.iterdir())) == 1
    assert instance._active_process is None


@pytest.mark.parametrize('damage', ['binding', 'missing', 'duplicate', 'ordinal', 'sha', 'path', 'document_sha', 'reason'])
def test_actual_final_binding_coverage_and_document_readback(fixture, damage):
    value = proposal(fixture)
    if damage == 'binding': value['binding']['attempt_id'] = 'e' * 32
    elif damage == 'missing': value['outcomes'].pop()
    elif damage == 'duplicate': value['outcomes'][1] = value['outcomes'][0]
    elif damage == 'ordinal': value['outcomes'][0]['ordinal'] = True
    elif damage == 'sha': value['outcomes'][0]['content_sha256'] = '0' * 64
    elif damage == 'path': value['outcomes'][0]['documents'][0]['path'] = 'wiki/../outside.md'
    elif damage == 'document_sha': value['outcomes'][0]['documents'][0]['sha256'] = '0' * 64
    else: value['outcomes'][0]['reason'] = '太短'
    result = run(fixture, runner(fixture, write_final(value)))
    assert not result.succeeded and result.final_bytes is None


def test_hard_budget_and_source_capabilities_required_before_probe_or_spawn(fixture):
    calls = []
    instance = CodexWikiRunner(executable_resolver=lambda: calls.append('resolver'), model_probe=lambda: calls.append('probe'))
    assert run(fixture, instance, source_proof=None).error_code == 'source_proof_unavailable'
    assert run(fixture, instance, measure=None).error_code == 'input_budget_unavailable'
    assert run(fixture, instance, measure=lambda p, s: ('fixed-test', 11, 10)).error_code == 'group_over_budget'
    assert run(fixture, instance, measure=lambda p, s: ('fixed-test', True, 10)).error_code == 'input_budget_unavailable'
    def unavailable(**_kwargs):
        raise RuntimeError('synthetic private source proof not available')
    result = run(fixture, instance, source_proof=unavailable)
    assert result.error_code == 'typed_input_invalid' and 'private' not in result.error_code
    def unsafe_code(**_kwargs):
        raise typed.TypedError('external private raw body')
    assert run(fixture, instance, source_proof=unsafe_code).error_code == 'typed_input_invalid'
    assert calls == [] and list(fixture[2].control.iterdir()) == []


@pytest.mark.parametrize('channel', ['out', 'err', 'line', 'final'])
def test_limits_terminate_actual_process_without_truncation(fixture, monkeypatch, channel):
    monkeypatch.setattr(typed, 'STDOUT_LIMIT', 2048)
    monkeypatch.setattr(typed, 'STDERR_LIMIT', 2048)
    monkeypatch.setattr(typed, 'LINE_LIMIT', 1024)
    monkeypatch.setattr(typed, 'FINAL_LIMIT', 4096)
    body = 'sys.stdin.buffer.read()\n'
    if channel == 'out': body += 'os.write(1,b"x\\n"*2048)\n'
    elif channel == 'err': body += 'os.write(2,b"x"*4096)\n'
    elif channel == 'line': body += 'os.write(1,b"x"*1500)\n'
    else: body += 'final.write_bytes(b"x"*5000)\n'
    body += 'time.sleep(30)\n'
    instance = runner(fixture, body)
    result = run(fixture, instance)
    assert result.error_code == 'runner_output_limit' and result.final_bytes is None
    assert instance._active_process is None


def test_blocked_stdin_timeout_has_bounded_writer(fixture):
    instance = runner(fixture, 'time.sleep(30)\n', timeout=.15)
    # The complete fake task's input is small; enlarge actual source bytes to
    # exceed a pipe, with a new legitimate frozen byte/hash binding.
    _tmp, runtime, snapshot, task = fixture
    content = (snapshot.workspace / task.raw[0].relative_path).read_bytes() + b'x' * 200000
    (snapshot.workspace / task.raw[0].relative_path).write_bytes(content)
    changed = replace(task.raw[0], byte_count=len(content), content_sha256=typed.digest(content))
    task = replace(task, raw=(changed, task.raw[1]), boundary_sha256=_boundary(((changed, task.raw[1]),)))
    snapshot = replace(snapshot, files=(replace(snapshot.files[0], byte_count=len(content),
                                                sha256=typed.digest(content)), snapshot.files[1]))
    result = instance.run_outcomes(snapshot, runtime, task=task, batch_no=1, model='fake', effort='medium',
                                   source_proof=proof, measure=measure)
    assert result.error_code == 'runner_timeout' and instance._active_process is None


def test_cancel_during_process_and_preflight_does_not_start_another(fixture):
    entered, release, started = threading.Event(), threading.Event(), []
    def resolve():
        entered.set()
        assert release.wait(3)
        started.append('resolved')
        return str(fake_cli(fixture[0], 'time.sleep(30)\n'))
    instance = CodexWikiRunner(executable_resolver=resolve, model_probe=lambda: [{'model': 'fake', 'efforts': ['medium']}])
    result = []
    thread = threading.Thread(target=lambda: result.append(run(fixture, instance)))
    thread.start()
    assert entered.wait(3)
    instance.cancel()
    release.set()
    thread.join(5)
    assert not thread.is_alive() and result[0].error_code == 'interrupted'
    assert instance._active_process is None
    instance.reset_cancellation()
    instance = runner(fixture, 'Path("process.ready").write_text("ready")\ntime.sleep(30)\n')
    result = []
    thread = threading.Thread(target=lambda: result.append(run(fixture, instance)))
    thread.start()
    deadline = time.monotonic() + 3
    while not (fixture[2].workspace / 'process.ready').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (fixture[2].workspace / 'process.ready').exists()
    instance.cancel()
    thread.join(5)
    assert not thread.is_alive() and result[0].error_code == 'interrupted'


def test_exited_leader_with_stubborn_descendant_pipe_is_killed(fixture):
    child_code = ('import os,signal,time\nfrom pathlib import Path\n'
                  'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                  'Path("child.pid").write_text(str(os.getpid()))\n'
                  'Path("child.ready").write_text("ignore-installed")\n'
                  'time.sleep(30)\n')
    body = ('sys.stdin.buffer.read()\n'
            f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}])\n'
            'deadline=time.monotonic()+1.5\n'
            'while not Path("child.ready").exists():\n'
            ' if time.monotonic()>=deadline: raise SystemExit(3)\n'
            ' time.sleep(.01)\n'
            'raise SystemExit(0)\n')
    instance = runner(fixture, body, timeout=3)
    result = []
    thread = threading.Thread(target=lambda: result.append(run(fixture, instance)))
    started = time.monotonic()
    total_deadline = started + 7.5
    thread.start()
    try:
        startup_deadline = started + 2.5
        while True:
            process = instance._active_process
            if process is not None and process.poll() == 0:
                break
            assert thread.is_alive(), 'runner ended before exited-leader/held-pipe boundary'
            assert time.monotonic() < startup_deadline, 'finite handler/leader startup deadline'
            time.sleep(.01)
        assert (fixture[2].workspace / 'child.ready').read_text() == 'ignore-installed'
        child = int((fixture[2].workspace / 'child.pid').read_text())
        os.kill(child, 0)
        assert thread.is_alive(), 'descendant must still hold inherited PIPE after leader exits'
        # Leave the original terminator's 2s grace inside the total deadline
        # even if the runner fails this assertion and cleanup must cancel it.
        thread.join(max(0, started + 5.5 - time.monotonic()))
        assert not thread.is_alive(), 'bounded whole-group timeout did not finish'
        assert result[0].error_code == 'runner_timeout'
    finally:
        if thread.is_alive():
            instance.cancel()
            thread.join(max(0, total_deadline - time.monotonic()))
    assert instance._active_process is None
    child = int((fixture[2].workspace / 'child.pid').read_text())
    # A reparented zombie may remain briefly; it must not still execute writes.
    import subprocess
    while True:
        remaining = total_deadline - time.monotonic()
        assert remaining > 0, 'finite post-kill observation deadline'
        status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True,
                                text=True, timeout=remaining).stdout.strip()
        if not status or status.startswith('Z'):
            break
        assert time.monotonic() < total_deadline, 'descendant survived process-group termination'
        time.sleep(.01)
    assert time.monotonic() - started < 8


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'hardlink', 'public_mode'])
def test_actual_final_must_be_private_regular_single_link(fixture, kind):
    body = 'sys.stdin.buffer.read()\n'
    if kind == 'symlink': body += 'final.symlink_to(Path.cwd()/"wiki/log.md")\n'
    elif kind == 'fifo': body += 'os.mkfifo(final)\n'
    elif kind == 'hardlink': body += 'os.link(Path.cwd()/"wiki/log.md",final)\n'
    else: body += 'final.write_text("{}")\nfinal.chmod(0o644)\n'
    result = run(fixture, runner(fixture, body))
    assert not result.succeeded and result.final_bytes is None


@pytest.mark.parametrize('name', ['C:/absolute', 'C:drive-relative', '../outside', '/absolute', 'x\\y', ''])
def test_read_final_rejects_unsafe_names_before_open(fixture, name):
    with pytest.raises(typed.TypedError):
        typed.read_final(fixture[2].control, name)


def test_named_file_replaced_between_lstat_and_open_never_reads_fifo(fixture, monkeypatch):
    path = fixture[2].control / 'final.json'
    path.write_bytes(b'{}')
    path.chmod(0o600)
    real_open, real_read = os.open, os.read
    reads = []
    def replaced_open(target, flags, *args, **kwargs):
        if Path(target) == path:
            path.unlink()
            os.mkfifo(path, 0o600)
        return real_open(target, flags, *args, **kwargs)
    def observe_read(*args):
        reads.append(args)
        return real_read(*args)
    monkeypatch.setattr(typed.os, 'open', replaced_open)
    monkeypatch.setattr(typed.os, 'read', observe_read)
    with pytest.raises(typed.TypedError):
        typed.read_final(path.parent, path.name)
    assert reads == []


def test_unrelated_sibling_creation_does_not_invalidate_regular_file(fixture, monkeypatch):
    path = fixture[2].control / 'final.json'
    path.write_bytes(b'{}')
    path.chmod(0o600)
    real_read = os.read
    def read(*args):
        (path.parent / 'unrelated').write_bytes(b'sibling')
        return real_read(*args)
    monkeypatch.setattr(typed.os, 'read', read)
    assert typed.read_final(path.parent, path.name) == b'{}'


def test_checker_is_independent_readonly_without_second_broker_or_tokens(fixture, monkeypatch):
    _tmp, runtime, snapshot, task = fixture
    value = proposal(fixture)
    changes = {'wiki/log.md': typed.digest((snapshot.workspace / 'wiki/log.md').read_bytes())}
    documents = typed.checked_documents(snapshot, changes)
    check = {'contract': typed.CHECK_CONTRACT, 'schema_revision': 1, 'binding': value['binding'],
             'proposal_sha256': typed.digest(typed.encoded(value)), 'changes_sha256': typed.digest(typed.encoded(documents)),
             'reviews': [{'raw_id': r.raw_id, 'content_sha256': r.content_sha256, 'status': 'unknown',
                 'reason': 'Synthetic checker cannot certify real semantics.',
                 'source_check': {'status': 'unknown', 'reason': 'Source completeness is unknown.',
                                  'evidence_sha256': typed.digest(b'synthetic-only-proof')},
                 'dimensions': [{'dimension': name, 'status': 'unknown', 'reason': 'Not semantically tested.',
                                 'evidence': [], 'related_raw_ids': []} for name in typed.DIMENSIONS]} for r in task.raw]}
    body = ('prompt=sys.stdin.buffer.read()\n'
            'Path("observed.json").write_text(json.dumps({"argv":argv,"prompt":prompt.decode(),'
            '"has_token":"KD_WIKI_LOCK_TOKEN" in os.environ,"has_key":"OPENAI_API_KEY" in os.environ}))\n'
            f'final.write_bytes({typed.encoded(check)!r})\n')
    instance = runner(fixture, body)
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-copy')
    with WikiSessionBroker(snapshot.workspace, runtime):
        result = instance.check_json(snapshot, runtime, task=task, batch_no=1, model='fake', effort='medium',
            proposal=typed.encoded(value), changes=changes, source_proof=proof, measure=measure)
    assert result.succeeded
    observed = json.loads((snapshot.workspace / 'observed.json').read_text())
    assert not observed['has_token'] and not observed['has_key']
    assert 'reference_lead须核对来源有价值的人物/作品—概念关系与名称是否在候选遗漏；无关修辞不强留，来源未给书名不得补。' in observed['prompt']
    argv = observed['argv']
    effective = dict(v.split('=', 1) for i, v in enumerate(argv) if i and argv[i - 1] == '-c')
    assert effective['features.shell_tool'] == effective['features.code_mode'] == 'false'
    assert effective['project_doc_max_bytes'] == '0'
    assert effective['shell_environment_policy.set'] == '{}'
    assert 'filesystem=' not in effective['permissions.wiki_staging']
    assert 'unix_sockets={}' in effective['permissions.wiki_staging']
    assert '--add-dir' not in argv and '--output-schema' in argv and '-o' in argv
    assert '完整合成原文' in observed['prompt']
    # The fake CLI writes its diagnostic; the model tool command has no write
    # permission. This does not claim an actual Codex sandbox has been tested.


@pytest.mark.parametrize('damage', ['dimension', 'source', 'status', 'evidence', 'coverage', 'reason'])
def test_strict_checker_rejects_fake_green_structure(fixture, damage):
    _tmp, runtime, snapshot, task = fixture
    binding, rows, _payload = typed.freeze_input(task, snapshot, 1, proof, runtime_root=runtime)
    source_hash = typed.digest(b'synthetic-only-proof')
    check = {'contract': typed.CHECK_CONTRACT, 'schema_revision': 1, 'binding': binding,
        'proposal_sha256': '1' * 64, 'changes_sha256': '2' * 64,
        'reviews': [{'raw_id': r.raw_id, 'content_sha256': r.content_sha256, 'status': 'verified',
            'reason': 'Synthetic structured candidate, not semantic proof.',
            'source_check': {'status': 'complete', 'reason': 'Synthetic fixture only.', 'evidence_sha256': source_hash},
            'dimensions': [{'dimension': n, 'status': 'absent', 'reason': 'Synthetic end greeting only.',
                'evidence': [], 'related_raw_ids': []} for n in typed.DIMENSIONS]} for r in task.raw]}
    first = check['reviews'][0]
    if damage == 'dimension': first['dimensions'][1]['dimension'] = 'definition'
    elif damage == 'source': first['source_check']['status'] = True
    elif damage == 'status': first['dimensions'][0]['status'] = 'present'
    elif damage == 'evidence': first['dimensions'][0]['evidence'] = [{'raw_id': task.raw[0].raw_id,
        'content_sha256': task.raw[0].content_sha256, 'start': 0, 'end': 3, 'text': 'wrong'}]
    elif damage == 'reason': first['reason'] = '太短'
    else: check['reviews'].pop()
    with pytest.raises(typed.TypedError):
        typed.parse_check(typed.encoded(check), binding, rows, proposal_sha256='1' * 64,
                          changes_sha256='2' * 64, source_proof_sha256=source_hash)


def test_windows_typed_rejected_before_probe_or_spawn(fixture, monkeypatch):
    instance = CodexWikiRunner(executable_resolver=lambda: pytest.fail('must not spawn'))
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert run(fixture, instance).error_code == 'runner_unsupported'


@pytest.mark.parametrize('damage', ['outside_control', 'symlink_root', 'raw_bytes', 'baseline', 'missing_batch', 'config'])
def test_input_paths_and_complete_binding_reject_before_capability_or_process(fixture, damage):
    tmp, runtime, snapshot, task = fixture
    called = []
    def source_proof(**kwargs):
        called.append('source')
        return proof(**kwargs)
    values = dict(model='fake', effort='medium')
    if damage == 'outside_control': snapshot = replace(snapshot, control=tmp)
    elif damage == 'symlink_root':
        alias = tmp / 'alias'
        alias.symlink_to(snapshot.workspace, target_is_directory=True)
        snapshot = replace(snapshot, workspace=alias)
    elif damage == 'raw_bytes':
        path = snapshot.workspace / task.raw[0].relative_path
        content = path.read_bytes()
        path.write_bytes(content.replace('完整'.encode(), '缺失'.encode(), 1))
    elif damage == 'baseline': snapshot = replace(snapshot, files=snapshot.files[1:])
    elif damage == 'missing_batch': task = replace(task, batch_count=2)
    else: values['model'] = 'other'
    instance = CodexWikiRunner(executable_resolver=lambda: pytest.fail('must not probe or spawn'))
    result = instance.run_outcomes(snapshot, runtime, task=task, batch_no=1,
                                   source_proof=source_proof, measure=measure, **values)
    assert not result.succeeded and result.final_bytes is None and called == []
    assert list(fixture[2].control.iterdir()) == []


@pytest.mark.parametrize('stream', [1, 2])
def test_invalid_pipe_utf8_is_not_a_final_candidate(fixture, stream):
    body = write_final(proposal(fixture, unknown=True)) + f'os.write({stream},b"\\xff")\ntime.sleep(30)\n'
    instance = runner(fixture, body)
    result = run(fixture, instance)
    assert result.error_code == 'typed_output_invalid' and result.final_bytes is None
    assert instance._active_process is None


def test_schema_unsupported_is_one_failed_invocation_without_fallback(fixture):
    instance = runner(fixture, 'sys.stdin.buffer.read()\nprint("unsupported --output-schema",file=sys.stderr)\nraise SystemExit(2)\n')
    result = run(fixture, instance)
    assert result.error_code == 'agent_failed' and result.final_bytes is None
    assert len(list(fixture[2].control.iterdir())) == 1


@pytest.mark.parametrize('damage', ['empty', 'generic_reason', 'extra', 'reorder', 'input', 'revision'])
def test_strict_candidate_json_never_accepts_partial_or_unbound_output(fixture, damage):
    value = proposal(fixture, unknown=True)
    if damage == 'empty': value['outcomes'] = []
    elif damage == 'generic_reason':
        value = proposal(fixture)
        value['outcomes'][0]['reason'] = '无知识'
    elif damage == 'extra': value['accepted'] = True
    elif damage == 'reorder': value['outcomes'].reverse()
    elif damage == 'input': value['binding']['input_sha256'] = '0' * 64
    else: value['schema_revision'] = True
    result = run(fixture, runner(fixture, write_final(value)))
    assert not result.succeeded and result.final_bytes is None


def test_complete_fourteen_raw_batch_is_bound_without_truncation(fixture):
    tmp, runtime, snapshot, task = fixture
    rows, files = list(task.raw), list(snapshot.files)
    original = (snapshot.workspace / rows[0].relative_path).read_bytes()
    for ordinal in range(3, 15):
        rid = f'R-20261008-{ordinal:04d}'
        path = f'raw/外部/2026/10/{rid}.md'
        content = original.replace(rows[0].raw_id.encode(), rid.encode())
        (snapshot.workspace / path).write_bytes(content)
        rows.append(FrozenRaw(path, rid, '第三方', len(content), typed.digest(content), ordinal, 1))
        files.append(SnapshotFile(path, 'raw', len(content), typed.digest(content)))
    task = replace(task, raw=tuple(rows), raw_count=14, boundary_sha256=_boundary((tuple(rows),)),
                   batches=(WikiBatch(1, 'running', 14, None),))
    snapshot = replace(snapshot, files=tuple(files), pending_before=tuple(r.relative_path for r in rows))
    full_fixture = tmp, runtime, snapshot, task
    value = proposal(full_fixture, unknown=True)
    result = run(full_fixture, runner(full_fixture, write_final(value)))
    assert result.succeeded
    assert [(r['raw_id'], r['ordinal']) for r in json.loads(result.final_bytes)['outcomes']] == [
        (r.raw_id, r.ordinal) for r in rows]


def test_generator_uses_existing_broker_gate(fixture):
    instance = runner(fixture, 'Path("must-not-start").write_text("bad")\n')
    with WikiSessionBroker(fixture[2].workspace, fixture[1]):
        result = run(fixture, instance)
    assert result.error_code == 'vault_busy' and result.final_bytes is None
    assert not (fixture[2].workspace / 'must-not-start').exists()
    assert instance._active_process is None


def test_final_growth_after_all_pipes_closed_is_still_bounded(fixture, monkeypatch):
    monkeypatch.setattr(typed, 'FINAL_LIMIT', 4096)
    body = ('sys.stdin.buffer.read()\nos.close(1)\nos.close(2)\n'
            'time.sleep(.1)\nfinal.write_bytes(b"x"*5000)\ntime.sleep(30)\n')
    instance = runner(fixture, body)
    result = run(fixture, instance)
    assert result.error_code == 'runner_output_limit' and result.final_bytes is None
    assert instance._active_process is None


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan'), True, 901])
def test_typed_timeout_cannot_disable_bounded_transport(fixture, timeout):
    instance = CodexWikiRunner(timeout_seconds=timeout,
                              executable_resolver=lambda: pytest.fail('must not probe or spawn'))
    result = run(fixture, instance)
    assert result.error_code == 'typed_input_invalid' and result.final_bytes is None
    assert list(fixture[2].control.iterdir()) == []
