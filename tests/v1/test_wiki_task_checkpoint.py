"""Current-schema real task/capture files; controlled subprocess, no real model.

Requires the actual schema27 migration/inventory and source-proof compatibility.
No PRAGMA version spoofing, catalog monkeypatch, SQL green receipt or fake proof.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from knowledge_distiller.v1 import wiki_typed as t
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.ingestion import Ingestion
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.wiki_exec_recording import ExecRecordingV1, RecordingError
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner
from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback
from knowledge_distiller.v1.wiki_staging import (prepare_staging, preserve_batch_baseline,
    load_generation_checkpoint, load_bound_staging, WikiStagingError, _checkpoint_read,
    _checkpoint_write, _checkpoint_tree)
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore, WikiTaskError, execution_policy_sha256
from .test_wiki_staging import KIT, _install

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='typed locking requires POSIX')


@pytest.fixture
def h(tmp_path, request, monkeypatch):
    from knowledge_distiller.v1.feishu_intake import FeishuIntake
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite3'); store.initialize()
    with connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 27
    vault, runtime = root / 'vault', root / 'runtime'
    vault.mkdir(); runtime.mkdir(mode=0o700); _install(vault)
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    monkeypatch.setattr('knowledge_distiller.v1.captures.now_ms', lambda: 1790000000000)
    for n in range(6):
        app, message = f'checkpoint-self-{n}', str(n)
        inbox = FeishuInbox(store, app)
        inbox.bind(bot_open_id='synthetic-bot', user_open_id='synthetic-user',
            chat_id='synthetic-chat', start_ms=0)
        inbox.receive(Message(message, 'synthetic-chat', 'synthetic-user', 'user', 'p2p',
            1790000000000, 'text', json.dumps({'text': f'完整合成自述{n}，仅向接收者表示感谢。'}),
            (), {'fixture_contract': 'controlled-authenticated-synthetic-message-v1'}))
        cid = ingestion.captures.for_message(app, message)['capture_id']
        ingestion.captures.decide(cid, 'my_thought')
        FeishuIntake(inbox, links=None, wake=None, api=None, jev=None).process(message)
        ingestion.capture(cid, vault)
    tasks = WikiTaskStore(store.path, kit_root=KIT, python_executable=sys.executable)
    task = tasks.create_or_reuse(vault, request_kind='all', trigger_source='local_web',
        backend='codex_cli', model='fake', effort='medium',
        outcome_contract=getattr(request, 'param', t.CONTRACT))
    assert len(task.raw) == 6 and task.batch_count == 2
    tasks.set_task_state(task.task_id, 'preparing'); task = tasks.set_task_state(task.task_id, 'running')
    tasks.set_batch_state(task.task_id, 1, 'preparing'); task = tasks.set_batch_state(task.task_id, 1, 'running')
    with VaultWriteLock.acquire(vault) as lock:
        snapshot = prepare_staging(vault, runtime, task.task_id, task.raw,
            python_executable=sys.executable, source_kit_root=KIT, lock=lock)
        proof = trusted_source_callback(store, lock)
        binding, rows, payload = t.freeze_input(task, snapshot, 1, proof, runtime_root=runtime)
        assert len(rows) == 5 and len(payload['context_raw']) == 6
        schema = t.encoded(t.PROPOSAL_SCHEMA)
        stdin = b'Controlled generation fixture.\n' + t.encoded(dict(binding=binding, input=payload))
        admission = t.admit_input(stdin, t.PROPOSAL_SCHEMA, None, input_policy=t.APPLICATION_UTF8_POLICY)
        page = next(f for f in snapshot.files if f.relative_path.startswith('wiki/') and f.relative_path.endswith('.md'))
        proposal = t.encoded(dict(contract=t.CONTRACT, schema_revision=1, binding=binding,
            outcomes=[dict(raw_id=r.raw_id, content_sha256=r.content_sha256, ordinal=r.ordinal,
                status='processed_no_knowledge', reason_code='non_substantive',
                reason='完整投递仅表达感谢，不含独立定义、方法或检索线索。',
                documents=[dict(path=page.relative_path, sha256=page.sha256)]) for r, _ in rows]))
        artifact = snapshot.control / 'generation-fixture'; artifact.mkdir(mode=0o700)
        final, schema_path = artifact / 'final.json', artifact / 'final.schema.json'
        schema_path.write_bytes(schema); schema_path.chmod(0o600)
        response = root / 'response.json'; response.write_bytes(proposal)
        script = root / 'fake.py'
        script.write_text("import pathlib,sys\nsys.stdin.buffer.read()\n"
            "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(pathlib.Path(sys.argv[-1]).read_bytes())\n")
        argv = (sys.executable, str(script), '-o', str(final), '--output-schema', str(schema_path), str(response))
        recording = root / 'recording'; recording.mkdir(mode=0o700)
        yield dict(root=root, store=store, tasks=tasks, task=task, snapshot=snapshot, runtime=runtime,
            lock=lock, proof=proof, stdin=stdin, schema=schema, admission=admission,
            proposal=proposal, argv=argv, final=final, recording=recording)


def recorder(h, *, after_finish=None, claim_spawn=False):
    def before(**kwargs):
        h['permit'] = h['tasks'].reserve_generation(h['task'].task_id, h['snapshot'], h['runtime'],
            expected_plan_sha256=h['task'].plan_sha256, batch_no=1, lock=h['lock'], source_proof=h['proof'],
            argv=kwargs['argv'], stdin_bytes=kwargs['stdin_bytes'], schema_bytes=kwargs['schema_bytes'],
            input_binding=h['admission'], timeout_seconds=kwargs['timeout_seconds'],
            recording_call=h['recording'] / f"exec-{kwargs['call_id']:04d}")
        if claim_spawn:
            h['permit'].claim_spawn()
    return ExecRecordingV1(h['recording'], workspace_root=h['snapshot'].workspace,
        runtime_root=h['runtime'], before_spawn=before, after_finish=after_finish)


def execute(h, *, after_finish=None):
    with recorder(h, after_finish=after_finish) as rec:
        call = rec.begin(argv=h['argv'], stdin_bytes=h['stdin'], schema_bytes=h['schema'], timeout_seconds=10)
        h['permit'].claim_spawn()
        process = subprocess.Popen(h['argv'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   umask=0o077)
        call.mark_spawned(process.pid)
        out, err = process.communicate(h['stdin'], timeout=5)
        call.write('out', out); call.write('err', err)
        call.progress(stdin_written=len(h['stdin']), stdout_eof=True, stderr_eof=True, usage={})
        call.finish(returncode=process.returncode, usage={})
        assert rec.counts['actual_spawned'] == 1
    content = h['final'].read_bytes()
    return t.TypedRunnerResult(None, (), content, t.digest(content), h['admission'])


def reserve_again(h):
    return h['tasks'].reserve_generation(h['task'].task_id, h['snapshot'], h['runtime'],
        expected_plan_sha256=h['task'].plan_sha256, batch_no=1, lock=h['lock'], source_proof=h['proof'],
        argv=h['argv'], stdin_bytes=h['stdin'], schema_bytes=h['schema'], input_binding=h['admission'],
        timeout_seconds=10, recording_call=h['recording'] / 'exec-0001')


def test_program_plan_dedup_and_policy_do_not_depend_on_attempt_or_entry(h):
    task = h['task']; plan = json.loads(task.plan_json)
    assert task.plan_sha256 == hashlib.sha256(task.plan_json.encode()).hexdigest()
    assert plan['raw'] == [r.__dict__ for r in task.raw]
    assert [b['raw_ids'] for b in plan['batches']] == [[r.raw_id for r in task.raw if r.batch_no == n] for n in (1, 2)]
    assert plan['source_requirement'] == {'contract':'wiki-source-proof-v1','scope':'finite_retained_sources'}
    assert plan['schemas']['proposal']['sha256'] == t.digest(t.encoded(t.PROPOSAL_SCHEMA))
    assert plan['configuration']['max_extra_repairs'] == 2
    assert plan['configuration']['r14_max_tokens'] == 8192
    assert execution_policy_sha256(task) == t.digest(t.encoded({k:plan[k] for k in ('configuration','schemas','r14_versions')}))
    # The fixture holds the Vault lock; release it before the normal creation API reacquires it.
    h['lock'].close()
    reused = h['tasks'].create_or_reuse(task.vault_path, request_kind='all', trigger_source='cli',
        backend='codex_cli', model='fake', effort='medium', outcome_contract=t.CONTRACT)
    assert reused.task_id == task.task_id and reused.plan_json == task.plan_json


def test_actual_terminal_and_runner_result_can_be_reloaded_without_another_call(h):
    result = execute(h)
    receipt = h['tasks'].save_generation_result(h['permit'], result, lock=h['lock'], source_proof=h['proof'])
    fresh = h['tasks'].get(h['task'].task_id)
    snapshot, content, loaded = load_generation_checkpoint(h['snapshot'].task_root.parent.parent,
        h['runtime'], task=fresh, batch_no=1, lock=h['lock'], source_proof=h['proof'])
    assert content == h['proposal'] and loaded == receipt
    assert snapshot.files == h['snapshot'].files
    assert len(list(h['recording'].glob('exec-*'))) == 1
    with pytest.raises(WikiStagingError, match='generation_already_reserved'):
        h['permit'].claim_spawn()


def test_recorded_typed_runner_full_return_precedes_success_checkpoint(h):
    executable = h['root'] / 'fake-cli'
    executable.write_text(f'#!{sys.executable}\nimport pathlib,sys\nsys.stdin.buffer.read()\n'
        f"pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(pathlib.Path({str(h['root'] / 'response.json')!r}).read_bytes())\n")
    executable.chmod(0o700)
    with recorder(h, claim_spawn=True) as rec:
        runner = CodexWikiRunner(recording=rec, executable_resolver=lambda: str(executable),
                                model_probe=lambda: pytest.fail('no account/model probe'))
        value = json.loads(h['proposal'])
        rows = tuple((r, b'') for r in h['task'].raw if r.batch_no == 1)
        result = runner._run_typed(h['snapshot'], h['runtime'], model='fake', effort='medium',
            binding=value['binding'], schema=t.PROPOSAL_SCHEMA, prompt=h['stdin'],
            parse=lambda data: t.parse_proposal(data, value['binding'], rows), measure=None,
            check_only=True, timeout=10, skip_preflight=True, input_policy=t.APPLICATION_UTF8_POLICY)
        assert result.succeeded and rec.counts['actual_spawned'] == 1
        assert (h['recording'] / 'exec-0001/terminal.json').is_file()
        receipt = h['tasks'].save_generation_result(h['permit'], result, lock=h['lock'], source_proof=h['proof'])
        assert receipt['proposal_sha256'] == result.final_sha256


def test_after_finish_callback_cannot_save_before_terminal_is_durable(h):
    attempts = []
    def after(**_kwargs):
        content = h['final'].read_bytes()
        result = t.TypedRunnerResult(None, (), content, t.digest(content), h['admission'])
        with pytest.raises((OSError, t.TypedError, WikiStagingError)):
            h['tasks'].save_generation_result(h['permit'], result, lock=h['lock'], source_proof=h['proof'])
        attempts.append(True)
    result = execute(h, after_finish=after)
    assert attempts == [True] and not (h['permit'].root / 'result.json').exists()
    h['tasks'].save_generation_result(h['permit'], result, lock=h['lock'], source_proof=h['proof'])


@pytest.mark.parametrize('damage', ['cancel', 'spawn_failure', 'missing_terminal', 'short_stdin', 'missing_eof',
                                   'overflow', 'timeout', 'wrong_argv', 'wrong_schema', 'wrong_input', 'wrong_p0'])
def test_incomplete_or_changed_transport_never_creates_success_or_new_permission(h, damage):
    result = execute(h)
    root = h['recording'] / 'exec-0001'
    terminal = _checkpoint_read(root / 'terminal.json')
    if damage == 'missing_terminal':
        (root / 'terminal.json').unlink()
    elif damage in {'wrong_argv', 'wrong_schema', 'wrong_input'}:
        name = {'wrong_argv':'argv.json','wrong_schema':'schema.json','wrong_input':'stdin.utf8'}[damage]
        path = root / name; path.write_bytes(path.read_bytes() + b'changed')
    elif damage == 'wrong_p0':
        value = json.loads(h['proposal']); value['outcomes'].pop()
        data = t.encoded(value); h['final'].write_bytes(data)
        result = replace(result, final_bytes=data, final_sha256=t.digest(data))
    else:
        changes = {'cancel':{'cancelled':True}, 'spawn_failure':{'actual_spawned':False},
            'short_stdin':{'stdin_written':1}, 'missing_eof':{'complete_stderr_eof':False},
            'overflow':{'truncated_due_to_overflow':True}, 'timeout':{'timed_out':True}}
        terminal.update(changes[damage]); (root / 'terminal.json').write_bytes(t.encoded(terminal))
    with pytest.raises((OSError, WikiStagingError, t.TypedError)):
        h['tasks'].save_generation_result(h['permit'], result, lock=h['lock'], source_proof=h['proof'])
    assert not (h['permit'].root / 'result.json').exists()
    with pytest.raises(WikiStagingError, match='generation_already_reserved'):
        reserve_again(h)
    assert len(list(h['recording'].glob('exec-*'))) == 1


def test_reservation_remains_after_cancel_before_any_spawn(h):
    with recorder(h) as rec:
        call = rec.begin(argv=h['argv'], stdin_bytes=h['stdin'], schema_bytes=h['schema'], timeout_seconds=10)
        assert (h['permit'].root / 'reservation.json').is_file()
        with pytest.raises(RecordingError):
            call.finish(returncode=None, usage={}, cancelled=True)
        assert rec.counts['actual_spawned'] == 0
    with pytest.raises(WikiStagingError, match='generation_interrupted'):
        load_generation_checkpoint(h['snapshot'].task_root.parent.parent, h['runtime'],
            task=h['task'], batch_no=1, lock=h['lock'], source_proof=h['proof'])
    with pytest.raises(WikiStagingError, match='generation_already_reserved'):
        reserve_again(h)


def test_process_crash_after_reservation_before_spawn_never_grants_second_call(h):
    pid = os.fork()
    if pid == 0:
        try:
            with recorder(h) as rec:
                rec.begin(argv=h['argv'], stdin_bytes=h['stdin'], schema_bytes=h['schema'], timeout_seconds=10)
                os._exit(17)
        except BaseException:
            os._exit(99)
    deadline = time.monotonic() + 5
    status = None
    while time.monotonic() < deadline:
        waited, value = os.waitpid(pid, os.WNOHANG)
        if waited:
            status = value; break
        time.sleep(0.01)
    if status is None:
        os.kill(pid, 9); os.waitpid(pid, 0)
        pytest.fail('bounded fixture child did not exit')
    assert os.waitstatus_to_exitcode(status) == 17
    root = h['snapshot'].task_root.parent.parent / 'execution/batch-1'
    assert (root / 'reservation.json').is_file() and not (root / 'result.json').exists()
    assert not h['final'].exists() and not (h['recording'] / 'exec-0001/terminal.json').exists()
    with pytest.raises(WikiStagingError, match='generation_already_reserved'):
        reserve_again(h)


@pytest.mark.parametrize('damage', ['plan', 'policy', 'raw_tail', 'pointer', 'baseline', 'hardlink', 'symlink'])
def test_binding_baseline_and_file_identity_drift_reject(h, damage):
    result = execute(h)
    root = h['permit'].root
    if damage in {'plan', 'policy'}:
        binding = root.parent / 'binding.json'; value = _checkpoint_read(binding)
        value['plan_sha256' if damage == 'plan' else 'execution_policy_sha256'] = '0' * 64
        binding.write_bytes(t.encoded(value))
    elif damage == 'raw_tail':
        p = h['snapshot'].workspace / h['task'].raw[-1].relative_path
        p.write_bytes(p.read_bytes() + b'changed')
    elif damage == 'pointer':
        (root.parent.parent / 'current-attempt').write_bytes(b'0' * 32 + b'\n')
    elif damage == 'baseline':
        p = root / 'before' / h['snapshot'].files[0].relative_path
        p.write_bytes(p.read_bytes() + b'changed')
    else:
        p = root / 'reservation.json'
        other = h['root'] / 'replacement'; other.write_bytes(p.read_bytes()); other.chmod(0o600)
        p.unlink()
        if damage == 'hardlink': os.link(other, p)
        else: p.symlink_to(other)
    with pytest.raises((WikiStagingError, t.TypedError, OSError)):
        # Fresh loading must not issue a new permit; save also rechecks reservation/source.
        load_generation_checkpoint(h['snapshot'].task_root.parent.parent, h['runtime'],
            task=h['task'], batch_no=1, lock=h['lock'], source_proof=h['proof'])
    assert not (root / 'result.json').exists()


def test_full_prebatch_beforebytes_are_retained_and_sibling_creation_is_harmless(h):
    root = preserve_batch_baseline(h['snapshot'], h['runtime'], task=h['task'], batch_no=1, lock=h['lock'])
    baseline = (root / 'baseline.json').read_bytes()
    for f in h['snapshot'].files:
        assert (root / 'before' / f.relative_path).read_bytes() == (h['snapshot'].workspace / f.relative_path).read_bytes()
    (root.parent / 'unrelated').write_bytes(b'sibling')
    assert preserve_batch_baseline(h['snapshot'], h['runtime'], task=h['task'], batch_no=1, lock=h['lock']) == root
    assert (root / 'baseline.json').read_bytes() == baseline
    changed = replace(h['snapshot'], pending_before=())
    with pytest.raises(WikiStagingError, match='checkpoint_baseline_changed'):
        preserve_batch_baseline(changed, h['runtime'], task=h['task'], batch_no=1, lock=h['lock'])


def test_wrong_expected_plan_and_external_plan_arguments_are_not_a_capability(h):
    with pytest.raises(WikiTaskError, match='plan_binding_invalid'):
        h['tasks'].read_execution_task(h['task'].task_id, expected_plan_sha256='0' * 64)
    with pytest.raises(TypeError):
        h['tasks'].create_or_reuse(h['task'].vault_path, request_kind='all', trigger_source='cli',
            backend='codex_cli', model='fake', effort='medium', outcome_contract=t.CONTRACT,
            plan_json={'green':True})


@pytest.mark.parametrize('h', ['legacy'], indirect=True)
def test_default_legacy_creation_dto_and_exact_reuse_remain_legacy(h):
    task = h['task']
    assert (task.outcome_contract, task.plan_json, task.plan_sha256) == ('legacy', '{}', None)
    h['lock'].close()
    reused = h['tasks'].create_or_reuse(task.vault_path, request_kind='all', trigger_source='cli',
        backend='codex_cli', model='fake', effort='medium')
    assert reused.task_id == task.task_id and reused.raw == task.raw
    with pytest.raises(WikiTaskError, match='plan_binding_invalid'):
        h['tasks'].read_execution_task(task.task_id, expected_plan_sha256='0' * 64)


@pytest.mark.parametrize('damage', ['external_final', 'schema', 'input_binding', 'source_tail'])
def test_before_spawn_invalid_inputs_never_leave_a_usable_reservation(h, damage):
    if damage == 'external_final':
        argv = list(h['argv']); argv[argv.index('-o') + 1] = str(h['root'] / 'outside.json')
        h['argv'] = tuple(argv)
    elif damage == 'schema':
        h['schema'] = b'{}'
    elif damage == 'input_binding':
        h['admission'] = replace(h['admission'], prompt_sha256='0' * 64)
    else:
        path = h['snapshot'].workspace / h['task'].raw[-1].relative_path
        path.write_bytes(path.read_bytes() + b'changed')
    with recorder(h) as rec:
        with pytest.raises(RecordingError):
            rec.begin(argv=h['argv'], stdin_bytes=h['stdin'], schema_bytes=h['schema'], timeout_seconds=10)
        assert rec.counts['actual_spawned'] == 0
    root = h['snapshot'].task_root.parent.parent / 'execution/batch-1'
    assert not (root / 'reservation.json').exists() and not h['final'].exists()


def test_partial_reservation_is_not_deleted_or_treated_as_unused(h):
    root = preserve_batch_baseline(h['snapshot'], h['runtime'], task=h['task'], batch_no=1, lock=h['lock'])
    _checkpoint_write(root / 'reservation.json', b'{')
    before = (root / 'reservation.json').read_bytes()
    with pytest.raises(WikiStagingError, match='generation_already_reserved'):
        reserve_again(h)
    assert (root / 'reservation.json').read_bytes() == before and not h['final'].exists()


def test_generation_permission_is_not_reissued_to_another_process(h):
    with recorder(h) as rec:
        call = rec.begin(argv=h['argv'], stdin_bytes=h['stdin'], schema_bytes=h['schema'], timeout_seconds=10)
        pid = os.fork()
        if pid == 0:
            try:
                h['permit'].claim_spawn()
            except WikiStagingError:
                os._exit(0)
            os._exit(99)
        deadline = time.monotonic() + 3
        status = None
        while time.monotonic() < deadline:
            waited, value = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = value; break
            time.sleep(0.01)
        if status is None:
            os.kill(pid, 9); os.waitpid(pid, 0)
            pytest.fail('bounded fixture child did not exit')
        assert os.waitstatus_to_exitcode(status) == 0 and rec.counts['actual_spawned'] == 0
        with pytest.raises(RecordingError):
            call.finish(returncode=None, usage={}, cancelled=True)
