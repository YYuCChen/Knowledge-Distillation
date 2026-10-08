"""Actual owned POSIX children/pipes, not Codex, models or source qualification.

Only environment isolation uses monkeypatch. No substituted Popen/read/write/
kill/wait and no Gate, acceptance, accounts or network. Layout-only snapshots
exercise transport; they do not fabricate a SourceProof or semantic certificate.
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
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner
from knowledge_distiller.v1.wiki_staging import StagingSnapshot

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='owned POSIX process groups')
PROMPT = '完整合成条件与否定\r\né 😀\n'.encode()
SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
          'required': ['ok'], 'additionalProperties': False}
USAGE = b'{"type":"turn.completed","usage":{"input_tokens":17,"output_tokens":3}}\n'
FINAL = b'{"ok":true}'


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    runtime, evidence, home = (root / n for n in ('runtime', 'evidence', 'home'))
    for p in (runtime, evidence, home):
        p.mkdir(mode=0o700)
    # No copying account files or consulting credentials.
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(home))
    tid, aid = 'a' * 32, 'd' * 32
    task_root = runtime / 'wiki-tasks' / tid / 'attempts' / aid
    workspace, control, backup = (task_root / n for n in ('workspace', 'control', 'backup'))
    for p in (workspace, control, backup):
        p.mkdir(parents=True, mode=0o700)
        p.chmod(0o700)
    snapshot = StagingSnapshot(tid, task_root, workspace, control, backup, (), ())
    binding = dict(task_id=tid, attempt_id=aid, batch_no=1,
                   boundary_sha256='b' * 64, input_sha256='c' * 64)
    return root, runtime, evidence, snapshot, binding


def executable(layout, body):
    root = layout[0]
    script = root / 'owned_child.py'
    script.write_text('import os,sys,time,subprocess,signal\nfrom pathlib import Path\n'
                      'argv=sys.argv[1:]\nfinal=Path(argv[argv.index("-o")+1])\n'
                      'Path("child.ready").write_text(str(os.getpid()))\n' + body)
    script.chmod(0o600)
    wrapper = root / 'owned-child'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -I -B ' +
                       shlex.quote(str(script)) + ' "$@"\n')
    wrapper.chmod(0o700)
    return wrapper


def session(layout, **kwargs):
    return ExecRecordingV1(layout[2], workspace_root=layout[1], runtime_root=layout[1],
                           total_timeout_seconds=10, per_exec_timeout_seconds=4, **kwargs)


def instance(layout, body, recording, *, timeout=4, missing=False):
    path = layout[0] / 'missing-child' if missing else executable(layout, body)
    return CodexWikiRunner(timeout_seconds=timeout, recording=recording,
                          executable_resolver=lambda: str(path),
                          model_probe=lambda: pytest.fail('transport must not preflight'))


def run(layout, runner, prompt=PROMPT, timeout=4):
    return runner._run_typed(layout[3], layout[1], model='synthetic-transport', effort='medium',
        binding=layout[4], schema=SCHEMA, prompt=prompt, parse=typed.strict_json, measure=None,
        check_only=True, timeout=timeout, skip_preflight=True,
        input_policy=typed.APPLICATION_UTF8_POLICY, max_application_input_bytes=2 * 1024 * 1024)


def artifacts(layout):
    paths = list(layout[2].iterdir())
    assert len(paths) == 1
    path = paths[0]
    return path, json.loads((path / 'terminal.json').read_bytes())


def await_condition(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'controlled-child handshake timed out'
        time.sleep(.005)


def threaded(layout, runner, recording, action):
    result, errors = [], []
    def work():
        try:
            result.append(run(layout, runner))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        await_condition(lambda: runner._active_process is not None)
        process = runner._active_process
        action(thread)
        thread.join(6)
        assert not thread.is_alive() and not errors
        assert len(result) == 1
        assert process.returncode is not None
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
        return result[0]
    finally:
        if thread.is_alive():
            runner.cancel()
            thread.join(6)
        assert not thread.is_alive(), 'owned child cleanup did not finish'


def successful(*, usage=True):
    return ('data=sys.stdin.buffer.read()\nPath("stdin.seen").write_bytes(data)\n' +
            (f'os.write(1,{USAGE!r})\n' if usage else '') +
            'os.write(2,b"synthetic stderr\\n")\n' + f'final.write_bytes({FINAL!r})\n')


def metadata_warning(model='synthetic-transport'):
    return {'type':'item.completed', 'item':{'id':'item_0', 'type':'error',
        'message':f'Model metadata for `{model}` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.'}}


@pytest.mark.parametrize('after,code', [
    ('success', None), ('error', 'agent_failed'), ('turn.failed', 'agent_failed'),
    ('exit', 'agent_failed'), ('timeout', 'runner_timeout'), ('invalid_final', 'typed_protocol_invalid')])
def test_exact_requested_metadata_warning_is_recorded_then_real_terminal_decides(layout, after, code):
    event=typed.encoded(metadata_warning())+b'\n'
    body='sys.stdin.buffer.read()\n'+f'os.write(1,{event!r})\n'
    if after in {'error','turn.failed'}:
        failure={'type':after,'error':{'message':'controlled genuine failure'}}
        body+=f'os.write(1,{(typed.encoded(failure)+bytes([10]))!r})\n'
    elif after=='exit': body+='sys.exit(7)\n'
    elif after=='timeout': body+='time.sleep(20)\n'
    elif after=='invalid_final': body+="final.write_bytes(b'{invalid schema final')\n"
    else: body+=f'os.write(1,{USAGE!r})\nfinal.write_bytes({FINAL!r})\n'
    with session(layout) as recording:
        result=run(layout,instance(layout,body,recording),timeout=3 if after=='timeout' else 4)
        assert result.error_code==code and result.succeeded==(code is None)
        path,terminal=artifacts(layout)
        assert event in (path/'stdout.jsonl.raw').read_bytes()
        assert terminal['diagnostic']['observed_warnings']==['model_metadata_fallback']
        if after == 'timeout':
            assert terminal['actual_spawned'] and terminal['timed_out']
            assert terminal['diagnostic']['original_transport_error_code'] == 'runner_timeout'
            assert terminal['diagnostic']['cleanup_observation_v1']['term'] == 'sent'
        if code is None:
            assert result.final_bytes==FINAL and terminal['returncode']==0
            assert terminal['complete_stdout_eof'] and terminal['complete_stderr_eof']
            assert terminal['diagnostic']['cleanup_observation_v1']['phase']=='finally'


@pytest.mark.parametrize('damage', ['model','extra_error','item.started','top_error','model_rerouted'])
def test_other_model_or_error_shape_cannot_use_metadata_warning_exception(layout, damage):
    event=metadata_warning()
    if damage=='model': event=metadata_warning('different-requested-model')
    elif damage=='extra_error': event['item']['code']='real_failure'
    elif damage=='item.started': event['type']='item.started'
    elif damage=='top_error': event={'type':'error','message':event['item']['message']}
    else: event['item']['message']='model rerouted: synthetic-transport -> replacement-model (ModelUnavailable)'
    body='sys.stdin.buffer.read()\n'+f'os.write(1,{(typed.encoded(event)+bytes([10]))!r})\nfinal.write_bytes({FINAL!r})\n'
    with session(layout) as recording:
        result=run(layout,instance(layout,body,recording))
        assert result.error_code=='agent_failed' and not result.succeeded
        assert 'observed_warnings' not in artifacts(layout)[1]['diagnostic']


@pytest.mark.parametrize('message', [
    'Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably.',
    'Configuration warning from this controlled CLI fixture.',
    'Deprecation notice from this controlled CLI fixture.',
    'Diagnostic containing words error and turn.failed is not a terminal failure.'])
def test_completed_error_item_is_protocol_diagnostic_not_text_whitelist(layout, message):
    event=metadata_warning(); event['item']['message']=message
    raw=typed.encoded(event)+b'\n'
    body='sys.stdin.buffer.read()\n'+f'os.write(1,{raw!r})\nos.write(1,{USAGE!r})\nfinal.write_bytes({FINAL!r})\n'
    with session(layout) as recording:
        result=run(layout,instance(layout,body,recording))
        assert result.succeeded and result.final_bytes==FINAL
        path,terminal=artifacts(layout)
        assert raw in (path/'stdout.jsonl.raw').read_bytes()
        assert terminal['diagnostic']['observed_warnings']==['cli_item_diagnostic']
        assert terminal['complete_stdout_eof'] and terminal['complete_stderr_eof']


@pytest.mark.parametrize('with_usage', [True, False])
def test_real_success_raw_inputs_usage_eof_and_none_compatibility(layout, with_usage):
    body = successful(usage=with_usage)
    with session(layout) as recording:
        runner = instance(layout, body, recording)
        result = run(layout, runner)
        assert result.succeeded and result.final_bytes == FINAL
        path, terminal = artifacts(layout)
        expected = {'input_tokens': 17, 'output_tokens': 3} if with_usage else {}
        assert terminal['usage'] == dict(result.usage) == expected
        assert terminal['actual_spawned'] and terminal['reserved'] and terminal['returncode'] == 0
        assert terminal['pid'] == int((layout[3].workspace / 'child.ready').read_text())
        assert terminal['stdin_written'] == len(PROMPT)
        assert terminal['complete_stdout_eof'] and terminal['complete_stderr_eof']
        diagnostic = terminal['diagnostic']
        assert diagnostic['original_transport_error_code'] is None
        assert diagnostic['cleanup_observation_v1']['phase'] == 'finally'
        assert diagnostic['cleanup_observation_v1']['first_result'] is True
        assert diagnostic['cleanup_observation_v1']['wait'] == 'completed'
        assert (path / 'stdin.utf8').read_bytes() == PROMPT
        assert (path / 'schema.json').read_bytes() == typed.encoded(SCHEMA)
        assert (path / 'stdout.jsonl.raw').read_bytes() == (USAGE if with_usage else b'')
        assert (path / 'stderr.raw').read_bytes() == b'synthetic stderr\n'
        argv = json.loads((path / 'argv.json').read_bytes())
        assert argv[0] == str(layout[0] / 'owned-child') and '--output-schema' in argv
        assert argv[argv.index('-o') + 1].endswith('/final.json')
        assert recording.counts == {'attempts': 1, 'reserved': 1, 'actual_spawned': 1}
        assert runner._active_process is runner._active_recording_call is None
        prior = sorted(p.name for p in layout[2].iterdir())
        plain = run(layout, instance(layout, body, None))
        assert plain.succeeded and plain.final_bytes == result.final_bytes and plain.usage == result.usage
        assert sorted(p.name for p in layout[2].iterdir()) == prior


@pytest.mark.parametrize('kind,code', [('utf8', 'typed_output_invalid'), ('utf8_eof', 'typed_output_invalid'),
    ('observable', 'agent_failed'), ('exit', 'agent_failed'),
    ('line', 'runner_output_limit'), ('out', 'runner_output_limit'), ('err', 'runner_output_limit')])
def test_failure_preserves_usage_before_raw_decode_or_limits(layout, kind, code):
    # A true producer/consumer handshake proves usage was parsed before damage,
    # rather than relying on OS chunk sizes or sleeps to separate JSONL lines.
    out_line = b'{"padding":"' + b'x' * (16 * 1024) + b'"}\n'
    out_count = typed.STDOUT_LIMIT // len(out_line) + 1
    damage = {'utf8': 'os.write(1,b"\\xff\\n")\n',
        'utf8_eof': 'os.write(1,b"\\xe4")\nraise SystemExit(0)\n',
        'observable': 'os.write(1,b\'{"type":"error","message":"synthetic-private"}\\n\')\n',
        'exit': 'raise SystemExit(7)\n',
        'line': f'os.write(1,b"x"*{typed.LINE_LIMIT + 1})\n',
        'out': f'os.write(1,{out_line!r}*{out_count})\n',
        'err': f'os.write(2,b"z"*{typed.STDERR_LIMIT + 5})\n'}[kind]
    body = ('sys.stdin.buffer.read()\n' + f'os.write(1,{USAGE!r})\n' +
            'while not Path("release.damage").exists(): time.sleep(.005)\n' + damage + 'time.sleep(30)\n')
    with session(layout) as recording:
        runner = instance(layout, body, recording)
        def release(_thread):
            await_condition(lambda: recording._active is not None and recording._active.usage.get('input_tokens') == 17)
            (layout[3].workspace / 'release.damage').touch(mode=0o600)
        result = threaded(layout, runner, recording, release)
        assert result.error_code == code and result.final_bytes is None
        path, terminal = artifacts(layout)
        assert terminal['usage'] == {'input_tokens': 17, 'output_tokens': 3}
        raw = (path / 'stdout.jsonl.raw').read_bytes()
        assert raw.startswith(USAGE)
        if kind == 'utf8':
            assert raw == USAGE + b'\xff\n'
        if kind == 'utf8_eof':
            assert raw == USAGE + b'\xe4' and terminal['complete_stdout_eof']
            diagnostic = terminal['diagnostic']
            assert diagnostic['original_transport_error_code'] == 'typed_output_invalid'
            observed = diagnostic['cleanup_observation_v1']
            assert observed['phase'] == 'pump' and observed['first_result'] is True
            assert observed['wait'] == 'completed' and observed['failure_code'] is None
            if observed['term'] == 'denied' or observed['kill'] == 'denied':
                assert observed['probe'] == 'absent'
        if kind in ('out', 'err'):
            tag, filename, cap = ('out', 'stdout.jsonl.raw', typed.STDOUT_LIMIT) if kind == 'out' else (
                'err', 'stderr.raw', typed.STDERR_LIMIT)
            assert terminal['truncated_due_to_overflow']
            assert terminal['observed_bytes'][tag] > terminal['retained_bytes'][tag] == cap
            expected = (USAGE + out_line * out_count)[:cap] if kind == 'out' else b'z' * cap
            assert (path / filename).read_bytes() == expected
        assert terminal['returncode'] is not None and recording.actual_spawned == 1
        assert runner._active_process is None and recording._active is None
        with pytest.raises(RecordingError, match='^recording_stopped$'):
            recording.begin(argv=('not-run',), stdin_bytes=b'', schema_bytes=b'{}', timeout_seconds=1)


def test_actual_partial_stdin_is_not_reported_complete(layout):
    body = 'os.read(0,8)\nos.close(0)\ntime.sleep(30)\n'
    prompt = b'x' * (1024 * 1024)
    with session(layout) as recording:
        result = run(layout, instance(layout, body, recording), prompt=prompt)
        assert result.error_code == 'agent_failed' and result.final_bytes is None
        path, terminal = artifacts(layout)
        assert 0 < terminal['stdin_written'] < len(prompt) == terminal['stdin_size']
        assert (path / 'stdin.utf8').read_bytes() == prompt
        assert terminal['usage'] == {} and terminal['actual_spawned']


def test_recording_scope_mismatch_rejects_before_begin_or_spawn(layout):
    other = layout[0] / 'other-workspace'
    other.mkdir(mode=0o700)
    with ExecRecordingV1(layout[2], workspace_root=other, runtime_root=layout[1]) as recording:
        result = run(layout, instance(layout, successful(), recording))
        assert result.error_code == 'agent_failed' and result.final_bytes is None
        assert recording.counts == {'attempts': 0, 'reserved': 0, 'actual_spawned': 0}
        assert list(layout[2].iterdir()) == []
        assert not (layout[3].workspace / 'child.ready').exists()


@pytest.mark.parametrize('phase', ['before', 'spawn', 'after'])
def test_callback_and_popen_failures_have_distinct_actual_counts(layout, phase):
    def reject(**_kwargs):
        raise RuntimeError('synthetic-private-callback-material')
    callbacks = {'before_spawn': reject} if phase == 'before' else {'after_finish': reject} if phase == 'after' else {}
    with session(layout, **callbacks) as recording:
        runner = instance(layout, successful(), recording, missing=phase == 'spawn')
        result = run(layout, runner)
        assert result.error_code == 'agent_failed' and result.final_bytes is None
        _path, terminal = artifacts(layout)
        assert terminal['error_code'] == ('recording_spawn_failed' if phase == 'spawn' else 'recording_callback_failed')
        assert recording.counts == dict(attempts=1, reserved=int(phase != 'before'), actual_spawned=int(phase == 'after'))
        assert terminal['actual_spawned'] == (phase == 'after')
        if phase != 'after':
            assert not (layout[3].workspace / 'child.ready').exists()
            assert terminal['returncode'] is None
        else:
            assert terminal['returncode'] == 0 and terminal['usage']['input_tokens'] == 17
        assert 'synthetic-private' not in result.error_code
        assert recording._fd is None and runner._active_process is None


@pytest.mark.parametrize('mode', ['deadline', 'cancel'])
def test_real_deadline_and_cancel_close_pipes_and_never_return_written_final(layout, mode):
    body = 'sys.stdin.buffer.read()\n' + f'final.write_bytes({FINAL!r})\n' + 'time.sleep(30)\n'
    with session(layout) as recording:
        runner = instance(layout, body, recording)
        def action(_thread):
            await_condition(lambda: (layout[3].workspace / 'child.ready').exists())
            if mode == 'cancel':
                runner.cancel()
        started = time.monotonic()
        result = threaded(layout, runner, recording, action)
        assert result.error_code == ('runner_timeout' if mode == 'deadline' else 'interrupted')
        assert result.final_bytes is None and time.monotonic() - started < 7
        _path, terminal = artifacts(layout)
        assert terminal['timed_out'] == (mode == 'deadline')
        assert terminal['cancelled'] == (mode == 'cancel')
        assert terminal['returncode'] is not None and terminal['actual_spawned']
        assert runner._active_process is runner._active_recording_call is None


def test_exited_leader_stubborn_descendant_is_observed_then_group_killed(layout):
    child = ('import os,signal,time\nfrom pathlib import Path\n'
             'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
             'Path("descendant.pid").write_text(str(os.getpid()))\n'
             'Path("descendant.ready").touch()\ntime.sleep(30)\n')
    body = ('sys.stdin.buffer.read()\n' +
            f'subprocess.Popen([sys.executable,"-I","-B","-c",{child!r}])\n' +
            'limit=time.monotonic()+2\n'
            'while not Path("descendant.ready").exists():\n'
            ' if time.monotonic()>=limit: raise SystemExit(4)\n'
            ' time.sleep(.005)\nraise SystemExit(0)\n')
    with session(layout) as recording:
        runner = instance(layout, body, recording)
        def observe(thread):
            await_condition(lambda: runner._active_process is not None and runner._active_process.poll() == 0)
            assert thread.is_alive() and (layout[3].workspace / 'descendant.ready').exists()
        result = threaded(layout, runner, recording, observe)
        assert result.error_code == 'runner_timeout' and result.final_bytes is None
        _path, terminal = artifacts(layout)
        assert terminal['returncode'] == 0 and terminal['timed_out']
        pid = int((layout[3].workspace / 'descendant.pid').read_text())
        def stopped():
            status = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='],
                capture_output=True, text=True, timeout=1).stdout.strip()
            return not status or status.startswith('Z')
        await_condition(stopped, timeout=2)


def test_actual_recorder_fd_failure_cleans_spawned_process_and_keeps_observed_usage(layout):
    body = ('sys.stdin.buffer.read()\n' + f'os.write(1,{USAGE!r})\n' +
            'while not Path("release.damage").exists(): time.sleep(.005)\n'
            'os.write(1,b"{}\\n")\ntime.sleep(30)\n')
    with session(layout) as recording:
        runner = instance(layout, body, recording)
        def break_owned_fd(_thread):
            await_condition(lambda: recording._active is not None and recording._active.usage.get('input_tokens') == 17)
            os.close(recording._active._streams['out'])  # Actual owned FD invalidation, not an I/O mock.
            (layout[3].workspace / 'release.damage').touch(mode=0o600)
        result = threaded(layout, runner, recording, break_owned_fd)
        assert result.error_code == 'agent_failed' and result.final_bytes is None
        path, terminal = artifacts(layout)
        assert terminal['error_code'] == 'recording_failed'
        assert terminal['usage'] == {'input_tokens': 17, 'output_tokens': 3}
        assert (path / 'stdout.jsonl.raw').read_bytes() == USAGE
        assert terminal['observed_bytes']['out'] > terminal['retained_bytes']['out']
        assert terminal['returncode'] is not None and recording._fd is None
        assert runner._active_process is None
