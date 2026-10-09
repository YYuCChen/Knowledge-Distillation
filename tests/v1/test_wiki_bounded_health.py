"""Owned child text transport/resource tests, NOT real health/model qualification.

No mocked Popen/read/write/kill, source capabilities, accounts or HOME changes.
Layout-only transport snapshots cannot certify source or health eligibility.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import pytest

from knowledge_distiller.v1 import wiki_typed as typed
from knowledge_distiller.v1.wiki_exec_recording import ExecRecordingV1, RecordingError
from knowledge_distiller.v1.wiki_lock import VaultWriteLock, vault_key
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner, _command
from knowledge_distiller.v1.wiki_staging import StagingSnapshot
from knowledge_distiller.v1.wiki_tasks import WikiTask, WikiBatch, FrozenRaw

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='owned POSIX child groups')
PROMPT = '完整health合成文字\r\n否定 é 😀\n'.encode()
USAGE = b'{"type":"turn.completed","usage":{"input_tokens":19,"output_tokens":2}}\n'


@pytest.fixture
def layout(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    runtime, evidence, vault = (root / n for n in ('runtime', 'evidence', 'vault'))
    for p in (runtime, evidence, vault):
        p.mkdir(mode=0o700)
    tid, aid = 'a' * 32, 'd' * 32
    task_root = runtime / 'wiki-tasks' / tid / 'attempts' / aid
    workspace, control, backup = (task_root / n for n in ('workspace', 'control', 'backup'))
    for p in (workspace, control, backup):
        p.mkdir(parents=True, mode=0o700)
        p.chmod(0o700)
    snapshot = StagingSnapshot(tid, task_root, workspace, control, backup, (), ())
    binding = dict(task_id=tid, attempt_id=aid, batch_no=1,
                   boundary_sha256='b' * 64, input_sha256='c' * 64)
    return root, runtime, evidence, vault, snapshot, binding


def child(layout, body):
    root = layout[0]
    script = root / 'owned_child.py'
    script.write_text('import os,sys,time,subprocess,signal\nfrom pathlib import Path\n'
        'argv=sys.argv[1:]\nfinal=Path(argv[argv.index("-o")+1])\n'
        'Path("child.ready").write_text(str(os.getpid()))\n' + body)
    script.chmod(0o600)
    wrapper = root / 'owned-child'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -I -B '
                       + shlex.quote(str(script)) + ' "$@"\n')
    wrapper.chmod(0o700)
    return wrapper


def runner(layout, body, recording):
    return CodexWikiRunner(recording=recording, executable_resolver=lambda: str(child(layout, body)),
                          model_probe=lambda: pytest.fail('no model discovery'))


def session(layout, **kwargs):
    return ExecRecordingV1(layout[2], workspace_root=layout[1], runtime_root=layout[1],
                           total_timeout_seconds=12, per_exec_timeout_seconds=4, **kwargs)


def transport(layout, instance, *, prompt=PROMPT, timeout=4):
    # Direct transport unit only: does not call or fake health/source admission.
    return instance._run_typed(layout[4], layout[1], model='owned-text-transport', effort='medium',
        binding=layout[5], schema=None, schema_absent=True, prompt=prompt,
        parse=lambda data: data.decode('utf-8', errors='strict'), measure=None,
        check_only=True, timeout=timeout, skip_preflight=True,
        input_policy=typed.APPLICATION_UTF8_POLICY, max_application_input_bytes=2 * 1024 * 1024)


def terminal(layout):
    paths = sorted(layout[2].iterdir())
    assert paths
    path = paths[-1]
    return path, json.loads((path / 'terminal.json').read_bytes())


def await_condition(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'owned handshake timed out'
        time.sleep(.005)


def threaded(layout, instance, action, *, prompt=PROMPT):
    results, errors = [], []
    def work():
        try:
            results.append(transport(layout, instance, prompt=prompt))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=work)
    thread.start()
    process = None
    try:
        await_condition(lambda: instance._active_process is not None)
        process = instance._active_process
        action(thread)
        thread.join(6)
        assert not thread.is_alive() and not errors and len(results) == 1
        assert process.returncode is not None
        assert all(p.closed for p in (process.stdin, process.stdout, process.stderr))
        return results[0]
    finally:
        if thread.is_alive():
            instance.cancel()
            thread.join(6)
        assert not thread.is_alive()


@pytest.mark.parametrize('final', [b'', '实际health文字\n'.encode(), b'not JSON'])
def test_actual_text_final_full_stdin_empty_schema_eof_and_exit(layout, final):
    body = ('data=sys.stdin.buffer.read()\nPath("stdin.seen").write_bytes(data)\n'
            + f'os.write(1,{USAGE!r})\nfinal.write_bytes({final!r})\n')
    with session(layout) as recording:
        result = transport(layout, runner(layout, body, recording))
        assert result.succeeded and result.final_bytes == final
        path, audit = terminal(layout)
        assert (path / 'schema.json').read_bytes() == b''
        assert (path / 'stdin.utf8').read_bytes() == (layout[4].workspace / 'stdin.seen').read_bytes() == PROMPT
        assert audit['stdin_written'] == audit['stdin_size'] == len(PROMPT)
        assert audit['returncode'] == 0 and audit['actual_spawned']
        assert audit['complete_stdout_eof'] and audit['complete_stderr_eof']
        assert audit['usage'] == dict(result.usage) == {'input_tokens': 19, 'output_tokens': 2}
        assert result.input_binding.schema_bytes == 0
        assert result.input_binding.schema_sha256 == typed.digest(b'')
        argv = json.loads((path / 'argv.json').read_bytes())
        assert '--output-schema' not in argv and argv.count('-o') == 1
        assert Path(argv[argv.index('-o') + 1]).read_bytes() == final


@pytest.mark.parametrize('damage,code', [('utf8_eof', 'typed_output_invalid'),
                                       ('exit', 'agent_failed'), ('out', 'runner_output_limit')])
def test_real_text_failure_keeps_raw_usage_and_actual_exit(layout, damage, code):
    line = b'{"padding":"' + b'x' * (16 * 1024) + b'"}\n'
    count = typed.STDOUT_LIMIT // len(line) + 1
    bad = {'utf8_eof': 'os.write(1,b"\\xe4")\nraise SystemExit(0)\n',
           'exit': 'raise SystemExit(7)\n',
           'out': f'os.write(1,{line!r}*{count})\ntime.sleep(30)\n'}[damage]
    body = ('sys.stdin.buffer.read()\n' + f'os.write(1,{USAGE!r})\n'
            + 'while not Path("release.damage").exists(): time.sleep(.005)\n' + bad)
    with session(layout) as recording:
        instance = runner(layout, body, recording)
        def release(_thread):
            await_condition(lambda: recording._active is not None
                            and recording._active.usage.get('input_tokens') == 19)
            (layout[4].workspace / 'release.damage').touch(mode=0o600)
        result = threaded(layout, instance, release)
        assert result.error_code == code and result.final_bytes is None
        path, audit = terminal(layout)
        assert audit['usage'] == {'input_tokens': 19, 'output_tokens': 2}
        assert audit['returncode'] is not None and audit['actual_spawned']
        raw = (path / 'stdout.jsonl.raw').read_bytes()
        assert raw.startswith(USAGE)
        if damage == 'utf8_eof':
            assert raw == USAGE + b'\xe4' and audit['complete_stdout_eof']
        if damage == 'out':
            assert raw == (USAGE + line * count)[:typed.STDOUT_LIMIT]
            assert audit['truncated_due_to_overflow']
            assert audit['observed_bytes']['out'] > audit['retained_bytes']['out'] == typed.STDOUT_LIMIT


def test_actual_partial_stdin_not_success(layout):
    with session(layout) as recording:
        result = transport(layout, runner(layout, 'os.read(0,8)\nos.close(0)\ntime.sleep(30)\n',
                                          recording), prompt=b'x' * (1024 * 1024))
        assert result.error_code == 'agent_failed' and not result.succeeded
        _, audit = terminal(layout)
        assert 0 < audit['stdin_written'] < audit['stdin_size']


@pytest.mark.parametrize('cancel', [False, True])
def test_actual_cancel_or_timeout_never_returns_written_text(layout, cancel):
    with session(layout) as recording:
        instance = runner(layout, 'sys.stdin.buffer.read()\nfinal.write_bytes(b"done")\ntime.sleep(30)\n',
                          recording)
        def action(_thread):
            await_condition(lambda: (layout[4].workspace / 'child.ready').exists())
            if cancel:
                instance.cancel()
        result = threaded(layout, instance, action)
        assert result.error_code == ('interrupted' if cancel else 'runner_timeout')
        assert result.final_bytes is None
        _, audit = terminal(layout)
        assert audit['cancelled'] == cancel and audit['timed_out'] == (not cancel)
        assert audit['returncode'] is not None


def test_exited_leader_actual_descendant_is_stopped(layout):
    descendant = ('import os,signal,time\nfrom pathlib import Path\n'
                  'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                  'Path("desc.pid").write_text(str(os.getpid()))\ntime.sleep(30)\n')
    body = ('sys.stdin.buffer.read()\n' + f'subprocess.Popen([sys.executable,"-I","-B","-c",{descendant!r}])\n'
            + 'while not Path("desc.pid").exists(): time.sleep(.005)\nraise SystemExit(0)\n')
    with session(layout) as recording:
        instance = runner(layout, body, recording)
        def observe(thread):
            await_condition(lambda: instance._active_process is not None
                            and instance._active_process.poll() == 0)
            assert thread.is_alive()
        result = threaded(layout, instance, observe)
        assert result.error_code == 'runner_timeout' and result.final_bytes is None
        _, audit = terminal(layout)
        assert audit['returncode'] == 0 and audit['timed_out']
        pid = int((layout[4].workspace / 'desc.pid').read_text())
        def stopped():
            result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='],
                                    capture_output=True, text=True, timeout=1)
            state = result.stdout.strip()
            return not state or state.startswith('Z')
        await_condition(stopped, timeout=2)


def test_empty_schema_measurement_and_unchanged_default_null_semantics():
    observed = []
    def measure(prompt, schema):
        observed.append((prompt, schema))
        return 'synthetic-full-input', len(prompt) + len(schema), 10000
    admitted = typed.admit_input(PROMPT, None, measure, schema_absent=True, schema_bytes=b'')
    assert observed == [(PROMPT, b'')] and admitted.schema_bytes == 0
    assert admitted.count == len(PROMPT) and admitted.schema_sha256 == typed.digest(b'')
    old = typed.admit_input(PROMPT, None, measure)
    assert observed[-1] == (PROMPT, b'null') and old.schema_bytes == 4


@pytest.mark.parametrize('flag,schema', [(1, None), (None, None), (True, {})])
def test_absent_schema_requires_actual_none_and_bool(flag, schema):
    with pytest.raises(typed.TypedError, match='^typed_input_invalid$'):
        typed.admit_input(PROMPT, schema, None, input_policy=typed.APPLICATION_UTF8_POLICY,
                          schema_absent=flag)
    with pytest.raises(typed.TypedError, match='^typed_input_invalid$'):
        typed.require_budget(PROMPT, schema, None, schema_absent=flag)


def test_empty_schema_has_no_null_or_measure_fallback():
    with pytest.raises(typed.TypedError, match='^typed_binding_invalid$'):
        typed.admit_input(PROMPT, None, None, input_policy=typed.APPLICATION_UTF8_POLICY,
                          schema_absent=True, schema_bytes=b'null')
    with pytest.raises(typed.TypedError, match='^input_budget_unavailable$'):
        typed.admit_input(PROMPT, None, None, schema_absent=True)


@pytest.mark.parametrize('maximum', [None, 11])
def test_resource_default_ninth_or_formal_twelfth_refused(layout, maximum):
    options = {} if maximum is None else {'max_attempts': maximum}
    limit = 8 if maximum is None else 11
    with session(layout, **options) as recording:
        deadline = recording._deadline
        instance = runner(layout, 'sys.stdin.buffer.read()\nfinal.write_bytes(b"done")\n', recording)
        for number in range(limit):
            assert transport(layout, instance).succeeded
            assert recording._deadline == deadline
            assert recording.actual_spawned == number + 1
        refused = transport(layout, instance)
        assert refused.error_code == 'agent_failed' and refused.final_bytes is None
        assert recording.counts == {'attempts': limit + 1, 'reserved': limit, 'actual_spawned': limit}
        assert len(list(layout[2].iterdir())) == limit


@pytest.mark.parametrize('maximum', [True, 0, 12, 8.0])
def test_resource_option_strict_finite_int(layout, maximum):
    with pytest.raises(RecordingError, match='^recording_invalid_input$'):
        session(layout, max_attempts=maximum)
    assert list(layout[2].iterdir()) == []


def test_failed_callback_consumes_attempt_and_cannot_refund(layout):
    def reject(**_kwargs):
        raise RuntimeError('private synthetic failure')
    with session(layout, max_attempts=11, before_spawn=reject) as recording:
        result = transport(layout, runner(layout, 'raise SystemExit(0)\n', recording))
        assert result.error_code == 'agent_failed'
        assert recording.counts == {'attempts': 1, 'reserved': 0, 'actual_spawned': 0}
        with pytest.raises(RecordingError, match='^recording_stopped$'):
            recording.begin(argv=('not-spawned',), stdin_bytes=b'', schema_bytes=b'', timeout_seconds=1)
        assert recording.attempts == 1


def test_shared_deadline_expired_callback_rejects_before_spawn(layout):
    def delay(**_kwargs):
        time.sleep(1.1)
    with ExecRecordingV1(layout[2], workspace_root=layout[1], runtime_root=layout[1],
                         total_timeout_seconds=1, before_spawn=delay, max_attempts=11) as recording:
        deadline = recording._deadline
        result = transport(layout, runner(layout, 'raise SystemExit(0)\n', recording))
        assert result.error_code == 'runner_timeout' and not result.succeeded
        assert recording._deadline == deadline
        assert recording.counts == {'attempts': 1, 'reserved': 0, 'actual_spawned': 0}
        _, audit = terminal(layout)
        assert audit['error_code'] == 'recording_deadline'


def test_command_o_only_and_old_both_or_neither(layout):
    root, _, _, _, snapshot, _ = layout
    env = {'KD_WIKI_LOCK_SOCKET': 'private-synthetic-socket'}
    common = ('not-executed', snapshot.workspace, 'synthetic-model', 'medium', env)
    old = _command(*common)
    assert '--output-schema' not in old and '-o' not in old
    final, schema = root / 'final.txt', root / 'schema.json'
    text = _command(*common, final_path=final)
    assert text == old[:-1] + ['-o', str(final), '-']
    typed_argv = _command(*common, final_schema=schema, final_path=final)
    assert typed_argv == old[:-1] + ['--output-schema', str(schema), '-o', str(final), '-']


@pytest.mark.parametrize('last_batch', [False, True])
def test_real_entry_nonfinal_or_invalid_actual_staging_refuses_zero_spawn(layout, last_batch):
    # Real metadata classes + actual lock, no SourceProof/ValidBatch mock.
    # Last-B case runs actual validate_staging with missing pending boundary;
    # neither negative case constitutes a healthy source/kit qualification.
    raw = FrozenRaw('raw/自述/2026/10/R-20261008-0001.md', 'R-20261008-0001', '本人', 1, 'e' * 64, 1, 1)
    batches = (WikiBatch(1, 'running', 1, None),)
    if not last_batch:
        batches += (WikiBatch(2, 'queued', 0, None),)
    task = WikiTask(layout[4].task_id, str(layout[3]), vault_key(layout[3]),
        'all', 'cli', 'codex_cli', 'synthetic-model', 'medium', 'synthetic-kit', 'f' * 64,
        'b' * 64, 'running', 1, len(batches), 0, None, 'none', 'none',
        'synthetic-time', 'synthetic-time', batches, (raw,))
    with session(layout) as recording, VaultWriteLock.acquire(layout[3]) as lock:
        instance = CodexWikiRunner(recording=recording,
            executable_resolver=lambda: pytest.fail('entry must reject before resolver'),
            model_probe=lambda: pytest.fail('entry must reject before model probe'))
        result = instance.run_health_bounded(layout[4], layout[1], task=task, batch_no=1,
            lock=lock, source_proof=None, model=task.model, effort=task.effort,
            skip_preflight=True, input_policy=typed.APPLICATION_UTF8_POLICY)
        assert result.error_code == ('typed_binding_invalid' if last_batch else 'typed_input_invalid')
        assert recording.counts == {'attempts': 0, 'reserved': 0, 'actual_spawned': 0}
        assert list(layout[2].iterdir()) == []
