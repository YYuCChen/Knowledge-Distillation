"""Actual Worker/recorder/kit/schema27 APIs with synthetic local CLI responses.

This tests orchestration and refusal, not real model semantics or subscription.
No real database, Vault, profile, model or network is accessed.
"""
import json
import os
from pathlib import Path
import sys

import pytest

from knowledge_distiller.v1 import wiki_typed as t
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.ingestion import Ingestion
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore
from knowledge_distiller.v1.wiki_worker import WikiWorker
from knowledge_distiller.v1.wiki_publish import publish_wiki, WikiPublishError
from .test_wiki_staging import KIT, _install

CHILD = r'''
import hashlib,json,pathlib,subprocess,sys
def sha(b): return hashlib.sha256(b).hexdigest()
def enc(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
prompt=sys.stdin.buffer.read().decode()
data=json.loads(prompt[prompt.index('{"binding"'):])
root=pathlib.Path.cwd()
final=pathlib.Path(sys.argv[sys.argv.index('-o')+1])
mode=pathlib.Path(__file__).with_name('mode').read_text()
knowledge=mode in ('knowledge','repair')
repair='"status":"repair_reserved"' in prompt
structure_feedback='processed_documents_missing' in prompt
counter=pathlib.Path(__file__).with_name('calls')
with counter.open('a') as f: f.write('call\n')
if mode == 'crash': sys.exit(1)
binding=data['binding']
if 'inputs' in data:
    pathlib.Path(__file__).with_name('registry.json').write_text(json.dumps(data['inputs']['registry']))
    failure=mode=='repair' and not pathlib.Path(__file__).with_name('support-failed').exists()
    if failure: pathlib.Path(__file__).with_name('support-failed').write_text('1')
    result=dict(contract='r14-typed-support-v1',schema_revision=1,binding=binding,
        registry_sha256=data['registry_sha256'],candidate_sha256=data['candidate_sha256'],
        checks=[dict(claim_id=c['claim_id'],status='supported',basis='program' if c['management_eligible'] else 'raw',
                     reason='Synthetic transport control.',issues=[])
                for c in data['inputs']['registry']['claims']])
    for c,check in zip(data['inputs']['registry']['claims'],result['checks']):
        if failure and c['path']=='wiki/概念/条件测试.md' and '低温' in c['text']:
            check.update(status='unsupported',reason='原文低温值为10，候选误写12。',
                issues=[dict(field='text',category='number',reason='保留低温条件，将12改为10。')])
elif 'proposal' in data:
    reviews=[]
    for row in data['input']['raw']:
        r=row['frozen']
        reviews.append(dict(raw_id=r['raw_id'],content_sha256=r['content_sha256'],status='verified',
            reason='完整合成文本只有感谢，四个维度均没有实质内容。',
            source_check=dict(status='complete',reason='仅核当前留存合成文字。',evidence_sha256=data['input']['source_proof_sha256']),
            dimensions=[dict(dimension=n,status='absent',reason='完整合成感谢文本没有该维内容。',evidence=[],related_raw_ids=[])
                        for n in ('definition','method','reference_lead','relations')]))
    if mode == 'unknown': reviews[0]['source_check']['status']='unknown'; reviews[0]['status']='unknown'
    if knowledge:
        for row,review in zip(data['input']['raw'],reviews):
            body=row['full_raw']; r=row['frozen']
            review['status']='unsupported'
            review['dimensions'][0].update(status='present',reason='来源含明确低温数值条件。',
                evidence=[dict(raw_id=r['raw_id'],content_sha256=r['content_sha256'],start=0,end=len(body),text=body)])
    result=dict(contract='r08-no-knowledge-check-v1',schema_revision=1,binding=binding,
        proposal_sha256=data['proposal_sha256'],changes_sha256=data['changes_sha256'],reviews=reviews)
else:
    refs=' '.join('[['+row['frozen']['relative_path']+'#^source-1]]' for row in data['input']['raw'])
    log=root/'wiki/log.md'
    if '--output-schema' not in sys.argv:
        with log.open('a') as f: f.write('\n## [2026-10-08] lint | '+refs+'\n- '+refs+'\n')
        (root/'wiki/体检报告.md').write_text('# 体检报告\n\n'+refs+'\n')
        result=None
    else:
        if not repair and not structure_feedback:
            with log.open('a') as f: f.write('\n## [2026-10-08] ingest | '+refs+'\n- '+refs+'\n')
        if knowledge:
            page=root/'wiki/概念/条件测试.md'; page.parent.mkdir(exist_ok=True)
            number='12' if mode=='repair' and not repair else '10'
            page.write_text('---\n类型: 概念\n子类: 术语\n创建: 2026-10-08\n更新: 2026-10-08\n主题: [社科]\n---\n\n# 条件测试\n\n## 定义\n仅低温数值为'+number+'。'+refs+'\n\n## 各家观点\n\n## 相关概念\n')
            if repair:
                assert '保留低温条件，将12改为10。' in prompt
                pathlib.Path(__file__).with_name('repair-feedback').write_text(prompt)
        result=True
    subprocess.run([sys.executable,str(root/'tools/kb.py'),'--root',str(root)],check=True,stdout=subprocess.DEVNULL)
    if result:
        result=dict(contract='r08-wiki-outcomes-v1',schema_revision=1,binding=binding,
            outcomes=[dict(raw_id=row['frozen']['raw_id'],content_sha256=row['frozen']['content_sha256'],
                ordinal=row['frozen']['ordinal'],status='processed_no_knowledge',reason_code='non_substantive',
                reason='完整合成投递只表示感谢，没有定义、方法、参考线索或关系。',
                documents=[dict(path='wiki/log.md',sha256=sha(log.read_bytes()))]) for row in data['input']['raw']])
        if knowledge:
            for outcome in result['outcomes']:
                outcome.update(status='processed_with_knowledge',reason_code='knowledge_proposed',reason='保留来源低温数值条件。')
                outcome['documents'].append(dict(path='wiki/概念/条件测试.md',sha256=sha(page.read_bytes())))
        if mode in ('documents_repair','documents_repeat','documents_budget'):
            assert 'processed_no_knowledge列wiki/log.md' in prompt
            if structure_feedback:
                assert 'processed结果必须列实际写入文件及最终字节SHA256' in prompt
                pathlib.Path(__file__).with_name('documents-feedback').write_text(prompt)
            if not structure_feedback or mode in ('documents_repeat','documents_budget'):
                result['outcomes'][0]['documents']=[]
                if mode=='documents_budget':
                    result['outcomes'][0]['reason']+='尝试'+str(len(counter.read_text().splitlines()))
final.write_bytes(b'health complete' if result is None else enc(result))
'''


@pytest.fixture
def world(tmp_path, request):
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite3'); store.initialize()
    vault, runtime = root / 'vault', root / 'runtime'
    vault.mkdir(); runtime.mkdir(mode=0o700); _install(vault)
    store.set_setting('vault_path', str(vault))
    ingestion = Ingestion(store)
    inbox = FeishuInbox(store, 'synthetic')
    inbox.bind(bot_open_id='synthetic-bot', user_open_id='synthetic-user', chat_id='synthetic-chat', start_ms=0)
    inbox.receive(Message('1', 'synthetic-chat', 'synthetic-user', 'user', 'p2p', 1790000000000,
        'text', json.dumps({'text': '仅低温数值为10，不是12。' if getattr(request,'param',None) else '谢谢你，完整合成测试文字。'}), (), {}))
    capture = ingestion.captures.for_message('synthetic', '1')
    ingestion.captures.decide(capture['capture_id'], 'my_thought')
    from knowledge_distiller.v1.feishu_intake import FeishuIntake
    FeishuIntake(inbox, links=None, wake=None, api=None, jev=None).process('1')
    ingestion.capture(capture['capture_id'], vault)
    # A synthetic log baseline uses its exact file heading, no business prose.
    (vault / 'wiki/log.md').write_text('# log\n', encoding='utf-8')
    tasks = WikiTaskStore(store.path, kit_root=KIT, python_executable=sys.executable)
    task = tasks.create_or_reuse(vault, request_kind='all', trigger_source='local_web',
        backend='codex_cli', model='fake', effort='medium', outcome_contract=t.CONTRACT)
    cli = root / 'controlled-cli'
    cli.write_text('#!' + sys.executable + '\n' + CHILD); cli.chmod(0o700)
    (root / 'mode').write_text(getattr(request,'param','success'))
    runner = CodexWikiRunner(executable_resolver=lambda: str(cli), kit_runtime=tasks.runtime)
    # No installed CLI discovery, account or network model probe.
    runner.preflight = lambda *_: str(cli)
    return root, store, vault, runtime, tasks, task, runner


def worker(world, **options):
    root, store, vault, runtime, tasks, task, runner = world
    return WikiWorker(tasks, runtime, runner, source_store=store, **options)


def test_real_typed_worker_commits_no_knowledge_and_formal_receipt(world):
    root, store, vault, runtime, tasks, task, runner = world
    before = {r.relative_path: (vault / r.relative_path).read_bytes() for r in task.raw}
    result = worker(world).run_one()
    assert result.error_code is None
    assert tasks.get(task.task_id).state == 'succeeded'
    with connect(store.path) as db:
        row = db.execute("SELECT * FROM wiki_outcome_receipts WHERE phase='accepted'").fetchone()
        assert row['plan_sha256'] == task.plan_sha256
        payload = json.loads(row['payload_json'])
        assert payload['outcomes'][0]['status'] == 'processed_no_knowledge'
        assert payload['journal_sha256'] and payload['check_sha256']
    assert before == {r.relative_path: (vault / r.relative_path).read_bytes() for r in task.raw}


def test_missing_documents_gets_fixed_feedback_then_real_worker_accepts(world):
    root, store, vault, runtime, tasks, task, runner = world
    (root / 'mode').write_text('documents_repair')
    before = {r.relative_path: (vault / r.relative_path).read_bytes() for r in task.raw}
    assert worker(world).run_one().error_code is None
    assert tasks.get(task.task_id).state == 'succeeded'
    records = list(runtime.rglob('generation-feedback-1.json'))
    assert len(records) == 1
    saved = json.loads(records[0].read_bytes())
    assert saved['issues'][0]['field'] == 'documents'
    assert saved['issues'][0]['code'] == 'processed_documents_missing'
    assert json.loads(saved['candidate'])['outcomes'][0]['documents'] == []
    reservation = json.loads((records[0].parent / 'reservation.json').read_bytes())
    original = Path(reservation['argv'][reservation['argv'].index('-o') + 1]).read_bytes()
    assert saved['candidate'].encode() == original and t.digest(original) == saved['candidate_sha256']
    assert (root / 'documents-feedback').is_file()
    assert len(list(runtime.rglob('reservation*.json'))) == 2
    assert (vault / 'wiki/log.md').read_text().count('ingest |') == 1
    assert before == {r.relative_path: (vault / r.relative_path).read_bytes() for r in task.raw}
    with connect(store.path) as db:
        accepted = db.execute("SELECT payload_json FROM wiki_outcome_receipts WHERE phase='accepted'").fetchall()
        assert len(accepted) == 1
        assert json.loads(accepted[0][0])['outcomes'][0]['status'] == 'processed_no_knowledge'


def test_documents_feedback_survives_crash_without_resetting_generation_budget(world, monkeypatch):
    root, store, vault, runtime, tasks, task, runner = world
    (root / 'mode').write_text('documents_repair')
    first = worker(world)
    original = first._proposal_feedback
    def crash(*args):
        result = original(*args)
        if result is not None:
            raise SystemExit('controlled crash after durable structure feedback')
        return result
    monkeypatch.setattr(first, '_proposal_feedback', crash)
    with pytest.raises(SystemExit):
        first.run_one()
    assert len((root / 'calls').read_text().splitlines()) == 1
    saved = next(runtime.rglob('generation-feedback-1.json')).read_bytes()
    assert worker(world).run_one().error_code is None
    assert next(runtime.rglob('generation-feedback-1.json')).read_bytes() == saved
    assert len(list(runtime.rglob('reservation*.json'))) == 2
    assert tasks.get(task.task_id).state == 'succeeded'


def test_existing_documents_feedback_cannot_loop_when_next_preflight_fails(world, monkeypatch):
    from knowledge_distiller.v1.wiki_runner import WikiRunnerError
    root, store, vault, runtime, tasks, task, runner = world
    (root / 'mode').write_text('documents_repair')
    preflight = runner.preflight
    calls = []
    def fail_second(*args):
        calls.append(args)
        if len(calls) > 1:
            raise WikiRunnerError('runner_unavailable')
        return preflight(*args)
    monkeypatch.setattr(runner, 'preflight', fail_second)
    assert worker(world).run_one().error_code == 'runner_unavailable'
    assert len(calls) == 2
    assert len((root / 'calls').read_text().splitlines()) == 1
    assert len(list(runtime.rglob('reservation*.json'))) == 1
    assert len(list(runtime.rglob('generation-feedback-*.json'))) == 1
    assert tasks.get(task.task_id).state == 'failed'


@pytest.mark.parametrize('mode,attempts', [('documents_repeat', 2), ('documents_budget', 3)])
def test_documents_same_candidate_stops_and_total_budget_remains_durable(world, mode, attempts):
    root, store, vault, runtime, tasks, task, runner = world
    (root / 'mode').write_text(mode)
    before = (vault / 'wiki/log.md').read_bytes()
    assert worker(world).run_one().error_code is not None
    assert len(list(runtime.rglob('reservation*.json'))) == attempts
    assert len(list(runtime.rglob('generation-feedback-*.json'))) == attempts
    calls = (root / 'calls').read_bytes()
    tasks.retry_failed(task.task_id)
    assert worker(world).run_one().error_code is not None
    assert (root / 'calls').read_bytes() == calls
    assert (vault / 'wiki/log.md').read_bytes() == before
    with connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM wiki_outcome_receipts WHERE phase='accepted'").fetchone()[0] == 0


@pytest.mark.parametrize('mode', ['unknown', 'crash'])
def test_typed_rejection_keeps_generation_budget_and_never_accepts(world, mode):
    root, store, vault, runtime, tasks, task, runner = world
    (root / 'mode').write_text(mode)
    before = (vault / 'wiki/log.md').read_bytes()
    assert worker(world).run_one().error_code is not None
    for _ in range(2):
        tasks.retry_failed(task.task_id)
        assert worker(world).run_one().error_code is not None
    calls = (root / 'calls').read_text()
    tasks.retry_failed(task.task_id)
    assert worker(world).run_one().error_code is not None
    assert (root / 'calls').read_text() == calls
    assert len(calls.splitlines()) == (3 if mode == 'crash' else 4)
    assert (vault / 'wiki/log.md').read_bytes() == before
    with connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM wiki_outcome_receipts WHERE phase='accepted'").fetchone()[0] == 0


def test_commit_then_receipt_failure_recovers_without_model_or_republish(world):
    root, store, vault, runtime, tasks, task, runner = world
    def interrupted(*args, **kwargs):
        publish_wiki(*args, **kwargs)
        raise WikiPublishError('publish_interrupted')
    assert worker(world, publish=interrupted).run_one().error_code == 'publish_interrupted'
    calls = (root / 'calls').read_text()
    result = worker(world, publish=lambda *_a, **_k: pytest.fail('must not republish')).recover_task(task.task_id)
    assert result.error_code == 'publish_interrupted'
    assert tasks.get(task.task_id).batches[0].state == 'succeeded'
    assert (root / 'calls').read_text() == calls
    tasks.retry_failed(task.task_id)
    resumed = worker(world)
    resumed._trusted_kb = lambda *_: pytest.fail('committed recovery must not regenerate staging')
    assert resumed.run_one().error_code is None


@pytest.mark.parametrize('tamper', [None, 'wiki/index.md', 'wiki/待确认.md', 'raw', 'kit_missing', 'kit_drift'])
def test_final_stage_resume_preserves_dated_candidate_and_rejects_tampering(world, monkeypatch, tamper):
    root, store, vault, runtime, tasks, task, runner = world
    first = worker(world)
    def crash(*_args, **_kwargs):
        raise SystemExit('controlled crash after final stage, before support')
    monkeypatch.setattr(first, '_typed_candidate', crash)
    with pytest.raises(SystemExit):
        first.run_one()
    stage = next(runtime.rglob('final-stage-0.json'))
    batch = stage.parent
    workspace = next(runtime.rglob('workspace'))
    cached = {p.name: p.read_bytes() for p in batch.glob('*check*json')}
    if tamper in {'kit_missing', 'kit_drift'}:
        from knowledge_distiller.v1 import wiki_worker as worker_module
        from knowledge_distiller.v1.wiki_kit import WikiKitError
        def rejected_kit(*_args):
            raise WikiKitError(tamper)
        monkeypatch.setattr(worker_module, 'verify_source_kit', rejected_kit)
    elif tamper:
        path = workspace / (task.raw[0].relative_path if tamper == 'raw' else tamper)
        path.write_bytes(path.read_bytes() + '\n未授权正文变更。\n'.encode())
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    calls = (root / 'calls').read_bytes()
    resumed = worker(world)
    # This is the next-day writer that previously ran before cache validation.
    # Refuse any invocation: even an unchanged page must retain its original
    # complete bytes, including its dated display and system content.
    def next_day_writer(*_args):
        pytest.fail('cached final-stage recovery must not run the dated kit writer')
    monkeypatch.setattr(resumed, '_trusted_kb', next_day_writer)
    result = resumed.run_one()
    assert {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()} == before
    assert {p.name: p.read_bytes() for p in batch.glob('*check*json')} == cached
    with connect(store.path) as db:
        accepted = db.execute("SELECT count(*) FROM wiki_outcome_receipts WHERE phase='accepted'").fetchone()[0]
    if tamper:
        assert result.error_code == 'validation_failed' and accepted == 0
        assert (root / 'calls').read_bytes() == calls
    else:
        assert result.error_code is None and accepted == 1


@pytest.mark.parametrize('tamper', [None, 'generated', 'record_hash', 'wiki/index.md', 'wiki/待确认.md'])
def test_crossday_resume_reuses_only_bound_generated_candidate(world, monkeypatch, tamper):
    import contextlib
    import datetime
    import io
    from knowledge_distiller.v1 import wiki_kit_runtime as kit_module
    from knowledge_distiller.v1 import wiki_support as support
    root, store, vault, runtime, tasks, task, runner = world
    original = tasks.set_batch_state
    def crash(task_id, batch_no, state, **kwargs):
        if state == 'validating':
            raise SystemExit('controlled crash after app-owned candidate and cached support')
        return original(task_id, batch_no, state, **kwargs)
    monkeypatch.setattr(tasks, 'set_batch_state', crash)
    with pytest.raises(SystemExit):
        worker(world).run_one()
    monkeypatch.setattr(tasks, 'set_batch_state', original)
    workspace = next(runtime.rglob('workspace'))
    directory = next(runtime.rglob('wiki-support-*'))
    state = json.loads((directory / 'state.json').read_bytes())['payload']
    candidate_path = directory / ('candidate-' + state['attempts'][-1]['request_id'] + '.json')
    candidate_record = json.loads(candidate_path.read_bytes())
    candidate = candidate_record['payload']
    saved_hash = candidate['candidate_hash']
    # Exercise the actual trusted renderer on two calendar days, with no kit
    # changes and no writes. Only its loaded module's clock label is advanced.
    real_load = kit_module.runpy.run_path
    def render(day):
        def load(*args, **kwargs):
            module = real_load(*args, **kwargs)
            module['Vault'].__init__.__globals__['TODAY'] = day.isoformat()
            return module
        with monkeypatch.context() as patch:
            patch.syspath_prepend(str(KIT / 'tools'))
            patch.setattr(kit_module.runpy, 'run_path', load)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                kit_module._describe_generated(KIT, workspace)
        return {row[0]: row[2] for row in json.loads(output.getvalue()) if row[1] == '@system'}
    day_a = datetime.date.today()
    rendered_a = render(day_a)
    rendered_b = render(day_a + datetime.timedelta(days=1))
    for path in ('wiki/index.md', 'wiki/待确认.md'):
        assert rendered_a[path] == (workspace / path).read_text()
        assert rendered_b[path] != rendered_a[path]
    if tamper == 'generated':
        candidate['generated'][0]['content'] += '\n伪造受管区。\n'
        # Rehashing an outer record cannot change its bound candidate identity.
        candidate_record['sha256'] = support._hash(candidate)
        candidate_path.write_text(json.dumps(candidate_record, ensure_ascii=False))
    elif tamper == 'record_hash':
        candidate_record['sha256'] = '0' * 64
        candidate_path.write_text(json.dumps(candidate_record, ensure_ascii=False))
    elif tamper:
        path = workspace / tamper
        path.write_bytes(path.read_bytes() + '\n未授权正文变更。\n'.encode())
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    evidence = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    calls = (root / 'calls').read_bytes()
    resumed = worker(world)
    monkeypatch.setattr(resumed, '_trusted_kb', lambda *_: pytest.fail('must not rewrite cached candidate'))
    monkeypatch.setattr(resumed, '_managed_sections', lambda *_: pytest.fail('must not certify using day B renderer'))
    result = resumed.run_one()
    assert (root / 'calls').read_bytes() == calls
    assert {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()} == before
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == evidence
    with connect(store.path) as db:
        accepted = db.execute("SELECT count(*) FROM wiki_outcome_receipts WHERE phase='accepted'").fetchone()[0]
    assert result.error_code == ('validation_failed' if tamper else None)
    assert accepted == (0 if tamper else 1)
    if not tamper:
        assert json.loads(candidate_path.read_bytes())['payload']['candidate_hash'] == saved_hash


@pytest.mark.parametrize('world', ['repair'], indirect=True)
def test_checked_pending_repair_before_gate_candidate_resumes_original_reservation(world, monkeypatch):
    import datetime
    root, store, vault, runtime, tasks, task, runner = world
    # This window is same-day recovery. Keep this synthetic writer's page
    # dates on the test day so kb cannot introduce an unrelated metadata edit.
    cli = root / 'controlled-cli'
    today = datetime.date.today().isoformat()
    cli.write_text(cli.read_text().replace('创建: 2026-10-08\\n更新: 2026-10-08',
                                          f'创建: {today}\\n更新: {today}'))
    first = worker(world)
    original = first._typed_candidate
    def crash(*args, **kwargs):
        if kwargs.get('reservation') is not None:
            raise SystemExit('checked repair final-stage persisted, child gate candidate not yet written')
        return original(*args, **kwargs)
    monkeypatch.setattr(first, '_typed_candidate', crash)
    with pytest.raises(SystemExit):
        first.run_one()
    directory = next(runtime.rglob('wiki-support-*'))
    state = json.loads((directory / 'state.json').read_bytes())['payload']
    assert state['used'] == 1 and len(state['attempts']) == 1
    assert state['repair']['consumed'] is False
    assert state['repair']['parent_hash'] == state['attempts'][-1]['candidate_hash']
    token = state['repair']['token']
    batch = next(runtime.rglob('final-stage-1.json')).parent
    assert json.loads((batch / 'final-stage-1.json').read_bytes())['check_phase'] == 'repair-1-check'
    saved = {p.name: p.read_bytes() for p in batch.iterdir() if p.is_file()}
    workspace = next(runtime.rglob('workspace'))
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    calls = (root / 'calls').read_text().splitlines()
    resumed = worker(world)
    monkeypatch.setattr(resumed, '_trusted_kb', lambda *_: pytest.fail('checked repair must not regenerate'))
    assert resumed.run_one().error_code is None
    after = json.loads((directory / 'state.json').read_bytes())['payload']
    assert after['used'] == 1 and after['repair']['token'] == token and after['repair']['consumed'] is True
    assert len(after['attempts']) == 2
    assert len((root / 'calls').read_text().splitlines()) == len(calls) + 1  # only the repaired support review
    assert not (batch / 'reservation-3.json').exists()
    assert not list(batch.glob('repair-2-*'))
    assert all((batch / name).read_bytes() == content for name, content in saved.items())
    assert {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()} == before
    with connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM wiki_outcome_receipts WHERE phase='accepted'").fetchone()[0] == 1


@pytest.mark.parametrize('corrupt,world', [
    pytest.param(False, 'success', id='False'),
    pytest.param(True, 'success', id='True'),
    pytest.param('wiki', 'knowledge', id='wiki'),
], indirect=['world'])
def test_validated_checkpoint_crash_before_publishing_reuses_only_exact_receipt(world, monkeypatch, corrupt):
    root, store, vault, runtime, tasks, task, runner = world
    original = tasks.set_batch_state
    def crash(task_id, batch_no, state, **kwargs):
        if state == 'validating':
            raise SystemExit('controlled crash after durable validated checkpoint')
        return original(task_id, batch_no, state, **kwargs)
    monkeypatch.setattr(tasks, 'set_batch_state', crash)
    with pytest.raises(SystemExit):
        worker(world).run_one()
    checkpoints = list(runtime.rglob('validated.json'))
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    calls = (root / 'calls').read_bytes()
    assert not list(runtime.rglob('journal.json'))
    if corrupt is True:
        # Valid JSON, incorrect receipt and an extra field: never overwrite it.
        checkpoint.write_bytes(t.encoded({'receipt_id': 'f' * 64, 'extra': True}))
    elif corrupt == 'wiki':
        pages = list(runtime.rglob('workspace/wiki/概念/条件测试.md'))
        assert len(pages) == 1
        pages[0].write_bytes(pages[0].read_bytes() + '\n用户修改实际 Wiki 正文。\n'.encode())
    before = checkpoint.read_bytes()
    monkeypatch.setattr(tasks, 'set_batch_state', original)
    result = worker(world, **({'publish': lambda *_a, **_k: pytest.fail('corrupt receipt must not publish')}
                             if corrupt else {})).run_one()
    assert (root / 'calls').read_bytes() == calls
    assert checkpoint.read_bytes() == before
    assert result.error_code == ('validation_failed' if corrupt else None)
    with connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM wiki_outcome_receipts WHERE phase='accepted'").fetchone()[0] == (0 if corrupt else 1)
    assert tasks.get(task.task_id).state == ('failed' if corrupt else 'succeeded')


@pytest.mark.parametrize('world', ['knowledge','repair'], indirect=True)
def test_actual_knowledge_claim_ranges_publish_and_feedback_repair(world):
    root, store, vault, runtime, tasks, task, runner = world
    before={r.relative_path:(vault/r.relative_path).read_bytes() for r in task.raw}
    result=worker(world).run_one()
    assert result.error_code is None
    assert tasks.get(task.task_id).state=='succeeded'
    page=(vault/'wiki/概念/条件测试.md').read_text()
    assert '仅低温数值为10。' in page and '数值为12。' not in page
    registry=json.loads((root/'registry.json').read_text())
    facts=registry['program_facts']
    assert facts['candidate_hash']==registry['candidate_hash']
    page_fact=next(p for p in facts['facts']['pages'] if p['path']=='wiki/概念/条件测试.md')
    assert page_fact['declared_topics']==['社科'] and page_fact['confirmed'] is False
    phases={(p['phase'],p['attempt']) for p in facts['facts']['completed_phases']}
    assert ('generation',1) in phases and ('check',1) in phases
    if (root/'mode').read_text()=='repair': assert ('repair-check',1) in phases
    claims=[c for c in registry['claims'] if c['path']=='wiki/概念/条件测试.md']
    assert claims and all(c['evidence'] for c in claims)
    assert any(e.get('start_line') and e.get('end_line') and '低温' in e['excerpt'] for c in claims for e in c['evidence'])
    with connect(store.path) as db:
        rows=db.execute("SELECT payload_json FROM wiki_outcome_receipts WHERE phase='accepted'").fetchall()
        assert len(rows)==1
        payload=json.loads(rows[0][0])
        assert payload['outcomes'][0]['status']=='processed_with_knowledge'
        assert payload['support']['status']=='supported_candidate_not_published'
        assert payload['journal_sha256'] and payload['published_after']['wiki/概念/条件测试.md']
    assert before=={r.relative_path:(vault/r.relative_path).read_bytes() for r in task.raw}
    states=[json.loads(p.read_text())['payload'] for p in runtime.rglob('state.json') if 'wiki-support-' in str(p)]
    assert len(states)==1 and states[0]['used']==(1 if (root/'mode').read_text()=='repair' else 0)
    if (root/'mode').read_text()=='repair': assert (root/'repair-feedback').is_file()


def test_real_application_assembles_existing_store_without_starting_workers(tmp_path):
    from knowledge_distiller.v1.app import AppPaths, create_application
    from .test_app import Chrome
    app = create_application(AppPaths(tmp_path / 'synthetic-app'), chrome=Chrome(), start_workers=False)
    try:
        wiki = app.config['KNOWLEDGE_DISTILLER_WIKI_WORKER']
        assert wiki.source_store is app.config['KNOWLEDGE_DISTILLER_STORE']
        assert wiki.source_store.path == wiki.store.database_path
    finally:
        app.config['KNOWLEDGE_DISTILLER_WORKERS'].stop()
        app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()
