"""Fail-closed impact plans and evidence, using existing project runners.

Registry .yaml files use the JSON subset of YAML to keep bootstrap stdlib-only.
No command from a passport or local release input is ever executed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
GATES = ('module', 'contract', 'candidate', 'native', 'release')
LEVELS = ('source', 'synthetic', 'integration', 'native', 'model_real', 'visual', 'remote_readback')


class Gap(ValueError):
    """Configuration, unavailable evidence or runner gap (exit 2)."""


class Blocked(Gap):
    """Known failure or forbidden promotion (exit 1)."""


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise Gap(f'missing or linked evidence/input: {path}')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def git(*args, root=ROOT):
    return subprocess.check_output(['git', *args], cwd=root).decode('utf-8').strip()


def host_platform():
    machine = platform.machine().lower()
    machine = {'aarch64': 'arm64', 'amd64': 'x64', 'x86_64': 'x64'}.get(machine, machine)
    return {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}.get(platform.system(), platform.system().lower()) + '-' + machine


def contained(path, parent):
    return path == parent or parent in path.parents


def protected(path):
    """Reject product/data/recovery roots even when reached through a symlink."""
    path = Path(path).expanduser().resolve()
    names = {p.casefold() for p in path.parts}
    denied = {'applications', 'application support', 'appdata', '工程治理', '发行候选',
              '双平台构建', 'vault', 'vaults', '知识库'}
    if names & denied or any(p.casefold().endswith('.app') for p in path.parts):
        raise Blocked(f'protected product/data/recovery location: {path}')
    if path in (Path.home().resolve(), Path(path.anchor)):
        raise Blocked('home/filesystem root is not disposable')
    return path


@contextmanager
def lock(path):
    """OS lock releases on crash; no stale PID removal or concurrent overwrite."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
            try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc: raise Gap('quality plan/data root is already locked') from exc
        else:
            import fcntl
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc: raise Gap('quality plan/data root is already locked') from exc
        yield
    finally:
        stream.close()


def data_root(path, plan_id):
    path = protected(path)
    if contained(ROOT, path) or contained(path, ROOT):
        raise Blocked('data root must be outside the source tree')
    # Only newly created roots, or roots previously marked by this plan, qualify.
    if path.exists():
        marker = path / '.quality-disposable.json'
        if not marker.is_file() or read(marker) != {'plan_id': plan_id, 'disposable': True}:
            raise Blocked('existing data root is not owned by this plan')
    else:
        path.mkdir(parents=True, exist_ok=False)
        atomic(path / '.quality-disposable.json', {'plan_id': plan_id, 'disposable': True})
    return path


def registry(root=ROOT):
    folder = root / 'quality'
    requirements = read(folder / 'requirements.yaml')
    scenarios = read(folder / 'scenarios.yaml')
    impact = read(folder / 'change-impact.yaml')
    incidents = [read(p) for p in sorted((folder / 'incidents').glob('*.yaml'))]
    ids = set(requirements['requirements'])
    if not ids or len({s['id'] for s in scenarios['scenarios']}) != len(scenarios['scenarios']):
        raise Gap('empty requirements or duplicate scenario IDs')
    for s in scenarios['scenarios']:
        required = {'id','requirements','components','gate','level','platform','runner','fixture','assertions','timeout','not_proven'}
        if required - s.keys() or not s['assertions'] or not s['requirements'] or not set(s['requirements']) <= ids:
            raise Gap('incomplete scenario: ' + s.get('id', '?'))
        if s['gate'] not in GATES or s['level'] not in LEVELS:
            raise Gap('invalid gate/level: ' + s['id'])
        runner = s['runner']
        if runner and runner['adapter'] == 'pytest' and s['level'] not in ('source', 'synthetic', 'integration'):
            raise Gap('pytest cannot attest native/model/visual evidence: ' + s['id'])
        if s['level'] in ('native','visual','model_real','remote_readback') and s['platform'] == 'host':
            raise Gap('real evidence needs an exact platform: ' + s['id'])
    needed = {'id','requirement_ids','symptom','evidence_state','severity','decision','affected_components',
              'corruption_or_privacy','core_path','propagation','workaround','verification','owner','target','status'}
    for incident in incidents:
        if needed - incident.keys(): raise Gap('incomplete incident: ' + incident.get('id', '?'))
        if incident['severity'] not in ('S0','S1','S2','S3','unknown'): raise Gap('invalid severity')
        if incident['severity'] in ('S0','S1') and incident['decision'] != 'must_fix_now':
            raise Blocked('S0/S1 cannot be deferred')
        if incident['decision'] == 'defer_next_patch' and not all(incident.get(k) for k in ('workaround','verification','target')):
            raise Gap('deferral requires verified workaround and target')
    return requirements, scenarios['scenarios'], impact, incidents


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def hashes_for(patterns, root=ROOT):
    paths = git('ls-files', '-z', root=root).split('\0')
    return {p: digest(root / p) for p in paths if p and matches(p, patterns) and (root / p).is_file()}


def snapshot(scenario, impact, root=ROOT):
    inputs = {}
    for component in scenario['components']:
        definition = impact['components'][component]
        inputs[component] = hashes_for(definition['paths'], root)
    runner = scenario['runner']
    runner_paths = [] if runner is None else runner.get('paths', [])
    return {'component_inputs': {key: object_hash(value) for key,value in inputs.items()},
            'input_files': inputs,
            'runner_hashes': {p: digest(root / p) for p in runner_paths},
            'fixture': {'sha256': digest(root / scenario['fixture']), 'permission': 'synthetic'},
            'scenario_sha256': object_hash(scenario)}


def scenario_gaps(scenario, root=ROOT):
    runner = scenario['runner']
    if not runner: return ['missing runner: ' + scenario['id']]
    paths = runner.get('paths', [])
    if not paths: return ['missing runner paths: ' + scenario['id']]
    return ['missing runner file: ' + p for p in paths if not (root / p).is_file()]


def make_plan(base, head, release_input, output, root=ROOT):
    reqs, scenarios, impact, incidents = registry(root)
    base = git('rev-parse', base + '^{commit}', root=root)
    head = git('rev-parse', head + '^{commit}', root=root)
    if git('rev-parse', 'HEAD', root=root) != head: raise Gap('head must be the actual checkout')
    if git('status', '--porcelain', root=root): raise Gap('commit changes before making an immutable plan')
    if subprocess.run(['git','merge-base','--is-ancestor',base,head],cwd=root).returncode:
        raise Gap('baseline must be an ancestor of head')
    paths = [p for p in git('diff','--no-renames','--name-only','-z',base,head,root=root).split('\0') if p]
    affected, owners, unmapped = set(), set(), []
    for path in paths:
        found = False
        for component, spec in impact['components'].items():
            if matches(path, spec['paths']):
                found = True; affected.add(component); owners.add(spec['owner'])
        if not found: unmapped.append(path)
    changed = set(affected)
    while True:
        expanded = affected | {c for c,spec in impact['components'].items() if set(spec.get('depends_on',[])) & affected}
        if expanded == affected: break
        affected = expanded
    selected = [s for s in scenarios if set(s['components']) & affected or s.get('always')]
    config = read(release_input) if release_input else {}
    forbidden = set(config) - {'version','product_version','artifacts','reuse_evidence','build_config','build_job','parameter_lock'}
    if forbidden: raise Gap('unsupported release-input keys: ' + ', '.join(sorted(forbidden)))
    output = protected(output)
    if contained(output, root): raise Gap('plan output must be outside checkout')
    output.mkdir(parents=True, exist_ok=False)
    gaps, snapshots = {}, {}
    for s in selected:
        gaps[s['id']] = scenario_gaps(s, root)
        if not gaps[s['id']]: snapshots[s['id']] = snapshot(s, impact, root)
    plan = dict(schema_version=1, change_id=uuid.uuid4().hex, baseline_commit=base, head_commit=head,
                source_root=str(root), requirement_ids=sorted({r for s in selected for r in s['requirements']}),
                changed_paths=paths, owners=sorted(owners), contracts_changed=sorted(affected-changed),
                migrations=['schema migration' ] if 'storage' in changed else [],
                component_inputs={c: object_hash(hashes_for(impact['components'][c]['paths'],root)) for c in affected},
                invalidate_scenarios=[s['id'] for s in selected], reuse_evidence_ids=[], reuse_reasons={},
                prohibited_scope=['formal application','formal data','Vault','recovery sources','publishing'],
                new_defects=[i for i in incidents if i['status'] != 'closed'],
                rebuild_components=sorted(c for c in affected if impact['components'][c].get('artifact')),
                scenarios=selected, snapshots=snapshots, gaps=gaps, unmapped_paths=unmapped,
                release_input=config, release_input_sha256=object_hash(config),
                registry_hashes={p.relative_to(root).as_posix(): digest(p) for p in sorted((root/'quality').rglob('*.yaml'))},
                created_at=now())
    plan['plan_sha256'] = object_hash(plan)
    atomic(output / 'plan.json', plan)
    return plan


def load_plan(path):
    plan = read(path)
    check = dict(plan); expected = check.pop('plan_sha256', None)
    if not expected or object_hash(check) != expected: raise Blocked('plan hash mismatch')
    root = Path(plan['source_root'])
    if git('rev-parse','HEAD',root=root) != plan['head_commit'] or git('status','--porcelain',root=root):
        raise Blocked('source checkout changed; generate a fresh plan')
    for name, expected in plan['registry_hashes'].items():
        if digest(root/name) != expected: raise Blocked('registry changed: ' + name)
    return plan


def gate_scenarios(plan, gate):
    return [s for s in plan['scenarios'] if GATES.index(s['gate']) <= GATES.index(gate)]


def check_promotion(plan, gate):
    if plan['unmapped_paths']: raise Blocked('unmapped diff: ' + ', '.join(plan['unmapped_paths']))
    if GATES.index(gate) >= GATES.index('candidate'):
        severe = [i['id'] for i in plan['new_defects'] if i['severity'] in ('S0','S1') and i['status'] != 'closed']
        if severe: raise Blocked('open S0/S1: ' + ', '.join(severe))


def artifact_identity(plan, scenario):
    if scenario['level'] not in ('native','visual','model_real','remote_readback'): return None
    entry = plan['release_input'].get('artifacts',{}).get(scenario['platform'])
    if not entry or not all(k in entry for k in ('path','sha256','build')): raise Gap('missing candidate artifact identity')
    path = protected(entry['path'])
    if digest(path) != entry['sha256']: raise Blocked('artifact hash mismatch')
    protected(entry['build'])
    return entry['sha256']


def command_for(scenario, plan, attempt):
    """Only registered adapters form argument arrays; no user supplied shell."""
    root = Path(plan['source_root']); runner = scenario['runner']; adapter = runner['adapter']
    if adapter == 'pytest':
        return [sys.executable,'-m','pytest','-q',*runner['paths'],'--basetemp',str(attempt/'pytest-temp'),
                '--junitxml',str(attempt/'junit.xml'),'-p','no:cacheprovider']
    if adapter == 'verify_candidate':
        artifact = plan['release_input'].get('artifacts',{}).get(scenario['platform'])
        if not artifact: raise Gap('missing artifact/build input')
        return [sys.executable,str(root/'scripts/verify_candidate.py'),'--platform',
                'mac' if scenario['platform'].startswith('macos-') else 'windows',
                '--build',str(protected(artifact['build'])),'--output',str(attempt/'verification'),
                '--version',plan['release_input']['version'],'--commit',plan['head_commit']]
    raise Gap('unimplemented runner adapter: ' + adapter)


def validate_result(scenario, attempt, returncode):
    if returncode: return 'failed', 'product_failure' if returncode == 1 else 'runner_failure'
    if scenario['runner']['adapter'] == 'pytest':
        suites = ET.parse(attempt/'junit.xml').getroot()
        cases = list(suites.iter('testcase'))
        if not cases: return 'not_run', 'empty_collection'
        if any(list(c.iter('skipped')) for c in cases): return 'not_run', 'skipped_assertions'
        if any(list(c.iter('failure')) or list(c.iter('error')) for c in cases): return 'failed', 'assertion_failure'
    elif scenario['runner']['adapter'] == 'verify_candidate':
        result = read(attempt/'verification/result.json')
        if result.get('ok') is not True: return 'failed', 'candidate_verification'
    return 'passed', None


def verify_passport(passport, scenario, plan, plan_dir):
    check = dict(passport); signature = check.pop('passport_sha256',None)
    if not signature or object_hash(check) != signature: raise Blocked('passport hash mismatch')
    if passport.get('scenario_id') != scenario['id'] or passport.get('level') != scenario['level']:
        raise Blocked('scenario/evidence level mismatch')
    expected_platform = host_platform() if scenario['platform']=='host' else scenario['platform']
    if passport.get('platform') != expected_platform: raise Blocked('platform mismatch')
    if passport.get('dirty') is not False: raise Blocked('dirty evidence')
    if passport.get('environment',{}).get('python') != read(Path(plan['source_root'])/'src/knowledge_distiller/v1/adapters/python-runtime.json')['version']:
        raise Blocked('runtime identity mismatch')
    current = plan['snapshots'].get(scenario['id'])
    if not current or passport.get('dependencies') != current: raise Blocked('stale dependency/runner/fixture hashes')
    if passport.get('artifact_sha256') != artifact_identity(plan,scenario): raise Blocked('candidate artifact changed')
    if scenario['level'] == 'remote_readback' and passport.get('plan_id') != plan['change_id']:
        raise Gap('remote availability must be read back for this plan')
    if not passport.get('outputs'): raise Blocked('missing evidence logs')
    for record in passport['outputs']:
        path = (plan_dir / record['path']).resolve()
        if not contained(path, plan_dir.resolve()): raise Blocked('evidence path escapes plan directory')
        if digest(path) != record.get('sha256'): raise Blocked('output hash mismatch')
    if passport['result'] != 'passed': raise Gap('evidence is ' + passport['result'])
    return True


def inspect(plan, path, gate):
    rows = []
    try: check_promotion(plan,gate)
    except Gap as exc: return {'gate':gate,'result':'blocked','exit_code':1,'reason':str(exc),'scenarios':rows}
    for scenario in gate_scenarios(plan,gate):
        entry = {'scenario_id':scenario['id'],'level':scenario['level'],'platform':scenario['platform'],'result':'not_run'}
        passport_path = path.parent/'evidence'/ (scenario['id'] + '.json')
        try:
            if plan['gaps'].get(scenario['id']): raise Gap('; '.join(plan['gaps'][scenario['id']]))
            if not passport_path.exists(): raise Gap('missing evidence')
            passport = read(passport_path)
            if passport.get('result') in ('failed','cancelled','timeout','tool_error'):
                entry['result'] = passport['result']
            verify_passport(passport,scenario,plan,path.parent)
            entry.update(result='passed',evidence_id=passport['evidence_id'])
        except (Gap,OSError,ValueError,KeyError) as exc:
            entry['reason'] = str(exc)
            if isinstance(exc,Blocked): entry['result']='invalid'
        rows.append(entry)
    code = 1 if any(r['result'] in ('failed','invalid','cancelled','timeout','tool_error') for r in rows) else 2 if not rows or any(r['result'] != 'passed' for r in rows) else 0
    return {'gate':gate,'result':'passed' if code==0 else 'blocked' if code==1 else 'not_run','exit_code':code,'scenarios':rows}


def run(plan, path, gate, disposable):
    check_promotion(plan,gate)
    root = Path(plan['source_root'])
    expected_python = read(root/'src/knowledge_distiller/v1/adapters/python-runtime.json')['version']
    if platform.python_version() != expected_python: raise Gap('runner Python differs from project runtime policy')
    with lock(path.parent/'.run.lock'):
        disposable = data_root(disposable,plan['change_id'])
        with lock(disposable/'.data.lock'):
            for scenario in gate_scenarios(plan,gate):
                if plan['gaps'].get(scenario['id']): continue
                evidence_path = path.parent/'evidence'/(scenario['id']+'.json')
                if evidence_path.exists():
                    try:
                        verify_passport(read(evidence_path),scenario,plan,path.parent)
                        continue
                    except (Gap,OSError,ValueError,KeyError): pass
                attempt_id = uuid.uuid4().hex
                attempt = disposable/attempt_id; attempt.mkdir()
                logdir = path.parent/'logs'/attempt_id; logdir.mkdir(parents=True)
                passport = dict(schema_version=1,evidence_id=attempt_id,plan_id=plan['change_id'],scenario_id=scenario['id'],
                                requirements=scenario['requirements'],source_commit=plan['head_commit'],dirty=False,
                                dependencies=plan['snapshots'][scenario['id']],level=scenario['level'],
                                platform=host_platform(),environment={'os':platform.platform(),'python':platform.python_version(),
                                'executable':sys.executable,'browser':None,'model':None},fixture=plan['snapshots'][scenario['id']]['fixture'],
                                assertions=scenario['assertions'],not_proven=scenario['not_proven'],started_at=now(),
                                result='running',outputs=[],artifact_sha256=None,invocation=[])
                atomic(evidence_path,passport)
                process = None
                try:
                    if scenario['platform'] not in ('host',host_platform()): raise Gap('requires native host ' + scenario['platform'])
                    passport['artifact_sha256'] = artifact_identity(plan,scenario)
                    command = command_for(scenario,plan,attempt); passport['invocation']=command
                    env = {k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','CONDA','VIRTUAL_ENV','KNOWLEDGE_DISTILLER'))}
                    home = attempt/'home'; home.mkdir()
                    env.update(HOME=str(home),USERPROFILE=str(home),APPDATA=str(home/'AppData/Roaming'),
                               LOCALAPPDATA=str(home/'AppData/Local'),TMPDIR=str(attempt),TEMP=str(attempt),TMP=str(attempt),
                               PYTHONPATH=str(root/'src')+os.pathsep+str(root),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',
                               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',KNOWLEDGE_DISTILLER_DATA_DIR=str(attempt/'data'))
                    with (logdir/'stdout.log').open('wb') as stdout, (logdir/'stderr.log').open('wb') as stderr:
                        process = subprocess.Popen(command,cwd=root,env=env,stdout=stdout,stderr=stderr,start_new_session=os.name!='nt')
                        passport['exit_code']=process.wait(timeout=scenario['timeout'])
                    passport['result'],passport['failure_kind']=validate_result(scenario,attempt,passport['exit_code'])
                except subprocess.TimeoutExpired:
                    passport.update(result='timeout',failure_kind='runner_timeout')
                except KeyboardInterrupt:
                    passport.update(result='cancelled',failure_kind='cancelled')
                except Gap as exc:
                    passport.update(result='not_run',failure_kind='environment_gap',error=str(exc))
                except Exception as exc:
                    passport.update(result='tool_error',failure_kind='tool_error',error=f'{type(exc).__name__}: {exc}')
                finally:
                    if process is not None and process.poll() is None:
                        if os.name!='nt': os.killpg(process.pid,signal.SIGTERM)
                        else: process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            if os.name!='nt': os.killpg(process.pid,signal.SIGKILL)
                            else: process.kill()
                            process.wait()
                    # Copy machine-readable assertion result, retain raw attempt data locally.
                    for source in (attempt/'junit.xml',attempt/'verification/result.json'):
                        if source.is_file(): (logdir/source.name).write_bytes(source.read_bytes())
                    for log in sorted(logdir.iterdir()):
                        passport['outputs'].append({'path':log.relative_to(path.parent).as_posix(),'sha256':digest(log)})
                    passport['finished_at']=now()
                    passport['passport_sha256']=object_hash(passport)
                    atomic(logdir/'passport.json',passport)
                    atomic(evidence_path,passport)
                if passport['result']=='cancelled': break
    return inspect(plan,path,gate)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    p=commands.add_parser('plan'); p.add_argument('--base',required=True); p.add_argument('--head',required=True)
    p.add_argument('--release-input',type=Path); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--gate',choices=GATES,default='module')
    for action in ('run','status','report'):
        p=commands.add_parser(action); p.add_argument('--plan',type=Path,required=True)
        p.add_argument('--gate',choices=GATES,default='module')
        if action=='run':p.add_argument('--data-root',type=Path,required=True)
        if action=='report':p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='plan':
            plan=make_plan(args.base,args.head,args.release_input,args.output)
            check_promotion(plan,args.gate)
            gaps=[v for s,v in plan['gaps'].items() if s in {s['id'] for s in gate_scenarios(plan,args.gate)} and v]
            result={'plan':str(args.output/'plan.json'),'scenarios':len(plan['scenarios']),'gaps':gaps,'rebuild_components':plan['rebuild_components'],'exit_code':2 if gaps else 0}
        else:
            plan=load_plan(args.plan)
            result=run(plan,args.plan,args.gate,args.data_root) if args.action=='run' else inspect(plan,args.plan,args.gate)
            if args.action=='report':atomic(protected(args.output),result)
        print(json.dumps(result,ensure_ascii=False,indent=2));return result['exit_code']
    except (Gap,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        result={'result':'blocked' if isinstance(exc,Blocked) else 'not_run','reason':str(exc),'exit_code':1 if isinstance(exc,Blocked) else 2}
        if args.action=='report':atomic(protected(args.output),result)
        print(json.dumps(result,ensure_ascii=False,indent=2));return result['exit_code']


if __name__=='__main__':sys.exit(main())
