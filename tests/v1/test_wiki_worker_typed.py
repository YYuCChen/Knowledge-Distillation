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
        checks=[dict(claim_id=c['claim_id'],status='supported',reason='Synthetic transport control.',issues=[])
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
        if not repair:
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
    assert worker(world).run_one().error_code is None


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
