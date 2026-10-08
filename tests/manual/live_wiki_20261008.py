"""One explicitly released real-CLI scenario, only in a freshly created /tmp root.

prepare performs no generation. run requires core's recorded synthetic readiness;
never substitutes a fake CLI, skips preflight, or retries a failed task.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

from knowledge_distiller.v1.database import connect, INGESTION_CONTRACT
from knowledge_distiller.v1.domain import SourceFact
from knowledge_distiller.v1.ingestion import Ingestion
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.wiki_kit_install import WikiKitInstaller
from knowledge_distiller.v1.wiki_kit_runtime import WikiKitRuntime
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner
from knowledge_distiller.v1.wiki_session_broker import WikiSessionBroker
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore
from knowledge_distiller.v1.wiki_typed import CONTRACT
from knowledge_distiller.v1.wiki_worker import WikiWorker
from knowledge_distiller.v1.worker import SingleWorker
from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate, WorkerCoordinator

MODEL, EFFORT = 'gpt-6.1-sol', 'medium'
SOURCES = (
    ('人工合成：种子发芽记录',
     '这是一份完全人工编写的合成实验记录，不是真实实验，也不证明一般规律。'
     '实验员在同一批种子中随机分出甲乙两组，各20粒。甲组在22摄氏度下每天加水10毫升，'
     '乙组在同温度下每天加水2毫升；两组都保持每天12小时照明，连续观察7天。'
     '第7天甲组有16粒发芽，乙组有6粒发芽，分别为80%和30%。'
     '这里只记录本次条件下的结果，没有重复实验，也没有控制种子批次以外的全部因素。'
     '不能据此断言更多浇水一定提高发芽率，也不能推广到别的温度或种类。'),
    ('人工合成：纯感谢', '谢谢你的帮助，辛苦了。非常感谢！'),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    path.chmod(0o600)


def private_root(path):
    root = Path(path).resolve(strict=True)
    if (root.parent != Path('/tmp').resolve()
            or not root.name.startswith('kd-live-wiki-20261008.')
            or root.stat().st_mode & 0o077):
        raise ValueError('explicit_private_disposable_root_required')
    return root


def assemble(root):
    # Same production classes/defaults as app.create_application; no Chrome,
    # application startup, dummy transport or preflight override.
    store = Store(root / 'synthetic.sqlite3', runtime_root=root / 'runtime')
    store.initialize()
    kit = WikiKitRuntime()
    tasks = WikiTaskStore(store.path, kit_root=kit.kit_root,
        python_executable=kit.python_executable, runtime=kit)
    runner = CodexWikiRunner(kit_runtime=kit)
    wiki = WikiWorker(tasks, root / 'runtime', runner, source_store=store)
    return store, kit, tasks, runner, wiki


@dataclass(frozen=True)
class SyntheticMaterial:
    source_kind: str
    source_key: str
    submitted_url: str
    canonical_url: str
    metadata: dict
    members: tuple = ()


def prepare():
    root = Path(tempfile.mkdtemp(prefix='kd-live-wiki-20261008.', dir='/tmp')).resolve()
    root.chmod(0o700)
    (root / 'vault').mkdir(mode=0o700)
    (root / 'runtime').mkdir(mode=0o700)
    store, kit, tasks, runner, wiki = assemble(root)
    manifest = kit.verify()
    source_worker = SingleWorker(store, lambda: None)  # never started
    gate = WorkAdmissionGate()
    coordinator = WorkerCoordinator(source_worker, wiki, gate)
    installed = WikiKitInstaller(kit.kit_root, root / 'runtime', gate, coordinator).install(root / 'vault')
    with WikiSessionBroker(root / 'vault', root / 'runtime') as broker:
        initialized = kit.run('kb', root / 'vault', ('init',), session_environment=broker.environment())
    (root / 'kit-init.log').write_text(initialized.stdout)
    if initialized.returncode:
        raise RuntimeError('synthetic_kit_init_failed')
    store.set_setting('vault_path', str(root / 'vault'))
    ingestion = Ingestion(store)
    raw = []
    for index, (title, text) in enumerate(SOURCES, 1):
        url = f'synthetic://handwritten/{index}'
        identity = digest(text.encode())
        item = store.create_item(url, ingestion_contract=INGESTION_CONTRACT,
            source_binding_sha256=digest(f'handwritten:{index}:{identity}'.encode()),
            relation_binding_sha256=digest(b'explicit independent synthetic input'))
        material = SyntheticMaterial('direct_text', identity, url, url,
            {'source_title': title, 'original_description': text,
             'native_content_version': identity, 'synthetic': True})
        mid = store.attach_material(item, material)
        store.establish_source_fact(mid, SourceFact(text),
            lineage={'kind': 'handwritten-synthetic', 'snapshot_sha256': identity})
        receipt = ingestion.material(mid, root / 'vault', item_id=item)
        raw.append({'raw_id': receipt.raw_id, 'relative_path': receipt.relative_path,
                    'sha256': digest((root / 'vault' / receipt.relative_path).read_bytes())})
    task = tasks.create_or_reuse(root / 'vault', request_kind='all', trigger_source='local_web',
        backend='codex_cli', model=MODEL, effort=EFFORT, outcome_contract=CONTRACT)
    if len(task.raw) != 2:
        raise RuntimeError('two_synthetic_sources_required')
    # Normal application model-list preflight uses account/read with
    # refreshToken=False; no auth file or token is read by this harness.
    cli = runner.preflight(MODEL, EFFORT)
    account = subprocess.run([cli, 'login', 'status'], capture_output=True, text=True, timeout=20)
    version = subprocess.run([cli, '--version'], capture_output=True, text=True, timeout=20)
    if account.returncode or 'Logged in using ChatGPT' not in account.stdout + account.stderr:
        raise RuntimeError('existing_chatgpt_account_required')
    source = Path(__file__).resolve().parents[2]
    rev = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=source,
        capture_output=True, text=True, check=True).stdout.strip()
    prepared = {'status': 'prepared_waiting_core', 'root': str(root), 'task_id': task.task_id,
        'model': MODEL, 'effort': EFFORT, 'raw': raw, 'source_head': rev,
        'kit_version': manifest.kit_version, 'kit_manifest_sha256': manifest.manifest_sha256,
        'kit_install_state': installed.state, 'cli': cli, 'cli_version': version.stdout.strip(),
        'account_status': 'Logged in using ChatGPT', 'live_worker_calls': 0}
    save(root / 'prepared.json', prepared)
    print(json.dumps(prepared, ensure_ascii=False))


def resume_task(root, prepared, store, tasks, wiki, *, final_check=False):
    """Use production retry gates; reject absent/invalid saved generation.

    No model call here. The checkpoint precheck prevents this manual recovery
    from silently falling back to a fresh proposal on generation_interrupted.
    """
    from knowledge_distiller.v1.wiki_lock import VaultWriteLock
    from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback
    task = tasks.get(prepared['task_id'])
    old = json.loads((root / 'result.json').read_text())
    if (not (root / 'run-once.json').is_file() or old.get('task_id') != task.task_id
            or old.get('status') != 'failed' or task.state != 'failed'
            or task.model != MODEL or task.effort != EFFORT
            or task.vault_path != str(root / 'vault')):
        raise RuntimeError('failed_original_task_required')
    if task.recovery_state in {'required', 'failed'}:
        recovered = wiki.recover_task(task.task_id)
        if recovered.error_code != 'publish_interrupted':
            raise RuntimeError(recovered.error_code or 'recovery_failed')
        task = tasks.get(task.task_id)
    if task.recovery_state not in {'not_needed', 'succeeded'}:
        raise RuntimeError('production_recovery_gate_unresolved')
    if final_check:
        previous = json.loads((root / 'resume-result.json').read_text())
        marker = json.loads((root / 'resume-once.json').read_text())
        if (task.task_id != '045b88da28254f258cb63edfe181e603'
                or root != Path('/private/tmp/kd-live-wiki-20261008.5yb2seef')
                or task.batch_count != 1 or previous.get('task_id') != task.task_id
                or previous.get('status') != 'failed' or marker.get('task_id') != task.task_id):
            raise RuntimeError('final_check_original_failed_task_required')
    with VaultWriteLock.acquire(root / 'vault') as lock:
        source = trusted_source_callback(store, lock)
        for batch in task.batches:
            if batch.state == 'succeeded':
                continue
            tasks.load_generation_checkpoint(task.task_id, root / 'runtime',
                batch_no=batch.batch_no, lock=lock, expected_plan_sha256=task.plan_sha256,
                source_proof=source, allow_regenerated_graph=True)
            phase_root = root / 'runtime' / 'wiki-tasks' / task.task_id / 'execution' / f'batch-{batch.batch_no}'
            if final_check:
                reservations = sorted(p.name for p in phase_root.glob('check-reservation-*.json'))
                if (reservations != ['check-reservation-1.json', 'check-reservation-2.json']
                        or (phase_root / 'check-result.json').exists()):
                    raise RuntimeError('final_check_requires_exactly_two_consumed_reservations')
                for name in reservations:
                    record = json.loads((phase_root / name).read_text())
                    if (record.get('task_id') != task.task_id or record.get('batch_no') != batch.batch_no
                            or record.get('phase') != 'check' or record.get('plan_sha256') != task.plan_sha256):
                        raise RuntimeError('final_check_reservation_binding_changed')
            if len(tuple(phase_root.glob('check-reservation-*.json'))) >= 3 and not (phase_root / 'check-result.json').exists():
                raise RuntimeError('production_checker_attempts_exhausted')
    tasks.retry_failed(task.task_id)


def run(path, core_ready, *, resume=False, final_check=False):
    root = private_root(path)
    prepared = json.loads((root / 'prepared.json').read_text())
    readiness = json.loads(Path(core_ready).read_text())
    if resume:
        ready_field = 'status_prompt' if final_check else 'protocol'
        action = 'resume-final-check' if final_check else 'resume-once'
        if (readiness.get(ready_field) != 'passed' or readiness.get('action') != action
                or readiness.get('task_id') != prepared['task_id']
                or readiness.get('released') is not True or not readiness.get('evidence')):
            raise ValueError('explicit_protocol_resume_release_required')
    elif (readiness.get('generated_knowledge') != 'passed' or readiness.get('repair') != 'passed'
              or readiness.get('released') is not True or not readiness.get('evidence')):
        raise ValueError('core_generated_and_repair_release_required')
    # One outer scenario; a crash also consumes this reservation. No automatic
    # retry_failed/recover_task loops. The actual worker owns its bounded repair.
    prefix = 'resume-final-check' if final_check else ('resume' if resume else '')
    marker_name = 'resume-final-check-once.json' if final_check else ('resume-once.json' if resume else 'run-once.json')
    fd = os.open(root / marker_name,
                 os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'task_id': prepared['task_id'], 'core_ready': readiness}, stream, ensure_ascii=False)
    summary = {'status': 'failed', 'root': str(root), 'task_id': prepared['task_id'],
               'model': MODEL, 'effort': EFFORT, 'scope': 'two_handwritten_synthetic_sources_only'}
    runner = None
    try:
        store, kit, tasks, runner, wiki = assemble(root)
        if kit.verify().manifest_sha256 != prepared['kit_manifest_sha256']:
            raise RuntimeError('prepared_kit_changed_reprepare_required')
        task = tasks.get(prepared['task_id'])
        if task.state != ('failed' if resume else 'queued') or task.model != MODEL or task.effort != EFFORT:
            raise RuntimeError('prepared_task_changed')
        for source in prepared['raw']:
            if digest((root / 'vault' / source['relative_path']).read_bytes()) != source['sha256']:
                raise RuntimeError('prepared_raw_changed')
        if resume:
            prior_hashes = {name: digest((root / name).read_bytes()) for name in
                            (('result.json', 'run-once.json', 'resume-result.json', 'resume-once.json')
                             if final_check else ('result.json', 'run-once.json'))}
            summary['prior_artifact_sha256'] = prior_hashes
            resume_task(root, prepared, store, tasks, wiki, final_check=final_check)
        result = wiki.run_one()  # actual runner, recording, validators and publisher
        after = tasks.get(task.task_id)
        with connect(store.path) as db:
            rows = db.execute("SELECT payload_json FROM wiki_outcome_receipts WHERE task_id=? AND phase='accepted'",
                              (task.task_id,)).fetchall()
        payloads = [json.loads(row['payload_json']) for row in rows]
        save(root / (prefix + '-accepted-private.json' if resume else 'accepted-private.json'), payloads)
        unchanged = all(digest((root / 'vault' / r['relative_path']).read_bytes()) == r['sha256']
                        for r in prepared['raw'])
        outcomes = [o for p in payloads for o in p.get('outcomes', [])]
        by_raw = {o['raw_id']: o['status'] for o in outcomes}
        expected_outcomes = {prepared['raw'][0]['raw_id']: 'processed_with_knowledge',
                             prepared['raw'][1]['raw_id']: 'processed_no_knowledge'}
        summary.update(error_code=result.error_code if result else 'no_work', task_state=after.state,
            raw_unchanged=unchanged, accepted_receipts=len(rows),
            outcome_statuses=[o['status'] for o in outcomes], expected_outcomes_match=by_raw == expected_outcomes)
        if resume:
            summary['prior_artifacts_unchanged'] = all(
                digest((root / name).read_bytes()) == sha for name, sha in prior_hashes.items())
        if (result is not None and result.error_code is None and after.state == 'succeeded'
                and unchanged and by_raw == expected_outcomes
                and (not resume or summary['prior_artifacts_unchanged'])):
            summary['status'] = 'passed'
    except Exception as error:
        (root / (prefix + '-failure-private.log' if resume else 'failure-private.log')).write_text(traceback.format_exc())
        summary.update(exception_type=type(error).__name__)
    finally:
        if runner is not None:
            runner.cancel()
        save(root / (prefix + '-result.json' if resume else 'result.json'), summary)
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'run', 'resume-once', 'resume-final-check'))
    parser.add_argument('--root')
    parser.add_argument('--core-ready')
    parser.add_argument('--resume-ready')
    options = parser.parse_args()
    if options.action == 'prepare':
        prepare()
    elif options.action in ('resume-once', 'resume-final-check'):
        if not options.root or not options.resume_ready:
            parser.error('resume requires --root and --resume-ready (fresh explicit release)')
        sys.exit(run(options.root, options.resume_ready, resume=True,
                     final_check=options.action == 'resume-final-check'))
    elif not options.root or not options.core_ready:
        parser.error('run requires --root and --core-ready')
    else:
        sys.exit(run(options.root, options.core_ready))
