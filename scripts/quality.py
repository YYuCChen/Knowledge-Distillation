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
ADAPTERS = {'pytest', 'verify_candidate', 'build_job', 'dual_build', 'manual_browser', 'desktop_browser', 'audio_pcm', 'audio_engine'}
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
        if runner and runner.get('adapter') not in ADAPTERS:
            raise Gap('unregistered runner adapter: ' + s['id'])
        if runner:
            fixed={'manual_browser':{'tests/v1/browser/manual_browser.py','tests/v1/browser/manual_fixture.py','tests/v1/browser/manual_samples.js'},
                   'verify_candidate':{'scripts/verify_candidate.py'},'build_job':{'scripts/build_job.py'},'dual_build':{'scripts/dual_build.py'}}
            if runner['adapter'] in ('audio_pcm','audio_engine'):
                fixed[runner['adapter']]={'scripts/probe_audio_pcm.py'} if runner['adapter']=='audio_pcm' else {'scripts/probe_audio_boundaries.py', 'src/knowledge_distiller/v1/adapters/'+('qwen_worker.py' if s['platform']=='macos-arm64' else 'qwen_windows_worker.py')}
            if runner['adapter']=='desktop_browser':
                if runner.get('case') not in ('protocol','slow-open'):raise Gap('invalid desktop browser case')
                fixed['desktop_browser']={'quality/run_desktop_browser.py','tests/v1/desktop/browser_fixture.py',
                    'tests/v1/desktop/'+('run_browser_protocol.py' if runner['case']=='protocol' else 'slow_open_browser.py')}
            if not fixed.get(runner['adapter'],set())<=set(runner.get('paths',[])):
                raise Gap('adapter execution paths must be fingerprinted: '+s['id'])
        if runner and runner['adapter'] in ('manual_browser','desktop_browser','audio_pcm','audio_engine') and s['level'] != 'integration':
            raise Gap('source browser runner proves integration only: ' + s['id'])
        if runner and runner['adapter'] == 'pytest' and s['level'] not in ('source', 'synthetic', 'integration'):
            raise Gap('pytest cannot attest native/model/visual evidence: ' + s['id'])
        if runner and runner['adapter'] == 'pytest':
            for node in runner.get('nodeids', []) + runner.get('deselect', []):
                if '::' not in node or node.split('::',1)[0] not in runner['paths']:
                    raise Gap('pytest node must belong to registered path: ' + s['id'])
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
    components=set(scenario['components'])
    while True:
        expanded=components | {dependency for component in components for dependency in impact['components'][component].get('depends_on',[])}
        if expanded==components:break
        components=expanded
    for component in sorted(components):
        definition = impact['components'][component]
        inputs[component] = hashes_for(definition['paths'], root)
    runner = scenario['runner']
    runner_paths = [] if runner is None else runner.get('paths', [])
    return {'collector_hash': digest(root/'scripts/quality.py') if (root/'scripts/quality.py').is_file() else None,
            'component_inputs': {key: object_hash(value) for key,value in inputs.items()},
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
    product_affected = {c for c,spec in impact['components'].items()
                        if any(matches(p,spec['paths']) for p in paths
                               if not p.startswith(('tests/','docs/','quality/','.github/'))
                               and not matches(p,impact.get('collector_paths',['scripts/quality.py'])))}
    while True:
        expanded = affected | {c for c,spec in impact['components'].items() if set(spec.get('depends_on',[])) & affected}
        if expanded == affected: break
        affected = expanded
    while True:
        expanded = product_affected | {c for c,spec in impact['components'].items() if set(spec.get('depends_on',[])) & product_affected}
        if expanded == product_affected: break
        product_affected = expanded
    config = read(release_input) if release_input else {}
    selected = [s for s in scenarios if (set(s['components']) & affected or s.get('always'))
                and (not s.get('enabled_by') or config.get(s['enabled_by']))]
    forbidden = set(config) - {'version','product_version','artifacts','reuse_evidence','build_config','build_job','parameter_lock','audio_input'}
    if forbidden: raise Gap('unsupported release-input keys: ' + ', '.join(sorted(forbidden)))
    output = protected(output)
    if contained(output, root): raise Gap('plan output must be outside checkout')
    output.mkdir(parents=True, exist_ok=False)
    gaps, snapshots = {}, {}
    execution_inputs={}
    for key in ('build_config','parameter_lock'):
        if config.get(key): execution_inputs[key]=digest(Path(config[key]).resolve())
    for s in selected:
        gaps[s['id']] = scenario_gaps(s, root)
        if s.get('runner') and s['runner']['adapter'] in ('audio_pcm','audio_engine') and not config.get('audio_input'):
            gaps[s['id']].append('missing licensed audio fixture/component inputs')
        if not gaps[s['id']]:
            snapshots[s['id']] = snapshot(s, impact, root)
            if s['level'] not in ('source','synthetic'):
                snapshots[s['id']]['execution_inputs']=dict(execution_inputs)
                if s['runner']['adapter'] in ('audio_pcm','audio_engine') and config.get('audio_input'):
                    try: snapshots[s['id']]['audio_inputs']=audio_identity(config['audio_input'],s)
                    except Blocked: raise
                    except Gap as exc: gaps[s['id']].append(str(exc))

    plan = dict(schema_version=1, change_id=uuid.uuid4().hex, baseline_commit=base, head_commit=head,
                source_root=str(root), requirement_ids=sorted({r for s in selected for r in s['requirements']}),
                changed_paths=paths, owners=sorted(owners), contracts_changed=sorted(affected-changed),
                migrations=['schema migration' ] if 'storage' in changed else [],
                component_inputs={c: object_hash(hashes_for(impact['components'][c]['paths'],root)) for c in affected},
                invalidate_scenarios=[s['id'] for s in selected], reuse_evidence_ids=[], reuse_reasons={},
                prohibited_scope=['formal application','formal data','Vault','recovery sources','publishing'],
                new_defects=[i for i in incidents if i['status'] != 'closed'],
                rebuild_components=sorted(c for c in product_affected if impact['components'][c].get('artifact')),
                scenarios=selected, snapshots=snapshots, gaps=gaps, unmapped_paths=unmapped,
                release_input=config, release_input_sha256=object_hash(config),
                registry_hashes={p.relative_to(root).as_posix(): digest(p) for p in sorted((root/'quality').rglob('*.yaml'))},
                created_at=now())
    for reuse in config.get('reuse_evidence',[]):
        old_path=Path(reuse['passport']); old_dir=Path(reuse['plan_dir']).resolve()
        if not reuse.get('reason'): raise Gap('evidence reuse needs a compatibility reason')
        old=read(old_path)
        scenario=next((s for s in selected if s['id']==old.get('scenario_id')),None)
        if not scenario: raise Gap('reused evidence not in current impact plan')
        verify_passport(old,scenario,plan,old_dir)
        for record in old['outputs']:
            target=output/record['path']; target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists() and digest(target)!=record['sha256']: raise Blocked('reuse output collision')
            if not target.exists(): target.write_bytes((old_dir/record['path']).read_bytes())
        atomic(output/'evidence'/(scenario['id']+'.json'),old)
        plan['reuse_evidence_ids'].append(old['evidence_id'])
        plan['reuse_reasons'][old['evidence_id']]={'reason':reuse['reason'],'source_commit':old['source_commit'],
            'validated':['component_inputs','runner','fixture','scenario','platform','python','artifact','outputs']}
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
    for key in ('build_config','parameter_lock'):
        if plan['release_input'].get(key):
            actual=digest(Path(plan['release_input'][key]).resolve())
            for snap in plan['snapshots'].values():
                if key in snap.get('execution_inputs',{}) and snap['execution_inputs'][key]!=actual:
                    raise Blocked('execution configuration changed: '+key)
    for scenario in plan['scenarios']:
        snap=plan['snapshots'].get(scenario['id'],{})
        if 'audio_inputs' in snap and audio_identity(plan['release_input']['audio_input'],scenario)!=snap['audio_inputs']:
            raise Blocked('audio inputs changed')
    return plan


def gate_scenarios(plan, gate):
    return [s for s in plan['scenarios'] if GATES.index(s['gate']) <= GATES.index(gate)]


def check_promotion(plan, gate):
    if plan['unmapped_paths']: raise Blocked('unmapped diff: ' + ', '.join(plan['unmapped_paths']))
    if GATES.index(gate) >= GATES.index('candidate'):
        severe = [i['id'] for i in plan['new_defects'] if i['severity'] in ('S0','S1') and i['status'] != 'closed']
        if severe: raise Blocked('open S0/S1: ' + ', '.join(severe))


def tree_digest(folder):
    folder=Path(folder)
    if not folder.is_dir() or folder.is_symlink():raise Gap('missing or linked candidate build')
    records={}
    for path in sorted(folder.rglob('*')):
        name=path.relative_to(folder).as_posix()
        if path.is_symlink():records[name]={'link':os.readlink(path)}
        elif path.is_file():records[name]={'sha256':digest(path),'mode':path.stat().st_mode & 0o777}
    if not records:raise Gap('empty candidate build')
    return object_hash(records)


def artifact_identity(plan, scenario):
    if scenario['level'] not in ('native','visual','model_real','remote_readback'): return None
    entry = plan['release_input'].get('artifacts',{}).get(scenario['platform'])
    if not entry or not all(k in entry for k in ('path','sha256','build','build_sha256')): raise Gap('missing candidate artifact identity')
    path = protected(entry['path'])
    if digest(path) != entry['sha256']: raise Blocked('artifact hash mismatch')
    build=protected(entry['build'])
    if tree_digest(build)!=entry['build_sha256']:raise Blocked('candidate build tree changed')
    return entry['sha256']


def candidate_source(plan,scenario):
    entry=plan['release_input'].get('artifacts',{}).get(scenario['platform'])
    if not entry:raise Gap('missing candidate build source identity')
    artifact_identity(plan,scenario)
    manifest=read(protected(entry['build'])/'build-manifest.json')
    commit=manifest.get('git_head')
    root=Path(plan['source_root'])
    if not commit or manifest.get('git_dirty') or manifest.get('changed_during_build'):
        raise Blocked('candidate build source is not clean')
    if subprocess.run(['git','merge-base','--is-ancestor',commit,plan['head_commit']],cwd=root,
                      stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        raise Blocked('candidate source is not an ancestor of plan')
    impact=read(root/'quality/change-impact.yaml')
    allowed=['docs/**','quality/**','tests/**','.github/**','AGENTS.md','README.md','CONTRIBUTING.md','SECURITY.md',
             *impact.get('collector_paths',['scripts/quality.py'])]
    changed=git('diff','--no-renames','--name-only',commit,plan['head_commit'],root=root).splitlines()
    if any(not matches(path,allowed) for path in changed):raise Blocked('candidate product/build inputs changed')
    return commit


def command_for(scenario, plan, attempt):
    """Only registered adapters form argument arrays; no user supplied shell."""
    root = Path(plan['source_root']); runner = scenario['runner']; adapter = runner['adapter']
    if adapter == 'pytest':
        return [sys.executable,'-m','pytest','-q',*runner.get('nodeids',runner['paths']),*[f'--deselect={n}' for n in runner.get('deselect',[])],'--basetemp',str(attempt/'pytest-temp'),
                '--junitxml',str(attempt/'junit.xml'),'-p','no:cacheprovider']
    if adapter in ('audio_pcm','audio_engine'):
        config=plan['release_input'].get('audio_input')
        if not config:raise Gap('missing licensed audio fixture/component inputs')
        identity=audio_identity(config,scenario)
        if identity!=plan['snapshots'][scenario['id']].get('audio_inputs'):raise Blocked('audio inputs changed')
        if adapter=='audio_pcm':
            return [sys.executable,str(root/'scripts/probe_audio_pcm.py'),str(protected(config['fixtures'])),str(attempt/'verification')]
        entry=config['components'][scenario['platform']]
        engine='mlx' if scenario['platform']=='macos-arm64' else 'transformers'
        worker='qwen_worker.py' if engine=='mlx' else 'qwen_windows_worker.py'
        return [sys.executable,str(root/'scripts/probe_audio_boundaries.py'),'--engine',engine,
                '--python',str(Path(entry['python']).resolve()),'--model',str(Path(entry['model']).resolve()),
                '--worker',str(root/'src/knowledge_distiller/v1/adapters'/worker),
                '--fixtures',str(protected(config['fixtures'])),'--output',str(attempt/'verification')]
    if adapter == 'manual_browser':
        return [sys.executable,str(root/'tests/v1/browser/manual_browser.py'),'--output',str(attempt/'verification')]
    if adapter == 'desktop_browser':
        return [sys.executable,str(root/'quality/run_desktop_browser.py'),'--case',runner['case'],
                '--output',str(attempt/'verification'),'--data-dir',str(attempt/'browser-data')]
    if adapter == 'verify_candidate':
        artifact = plan['release_input'].get('artifacts',{}).get(scenario['platform'])
        if not artifact: raise Gap('missing artifact/build input')
        return [sys.executable,str(root/'scripts/verify_candidate.py'),'--platform',
                'mac' if scenario['platform'].startswith('macos-') else 'windows',
                '--build',str(protected(artifact['build'])),'--output',str(attempt/'verification'),
                '--version',plan['release_input']['version'],'--commit',candidate_source(plan,scenario)]
    if adapter == 'build_job':
        return [sys.executable,str(root/'scripts/build_job.py'),'status',str(protected(plan['release_input']['build_job']))]
    if adapter == 'dual_build':
        return [sys.executable,str(root/'scripts/dual_build.py'),'status','--config',str(Path(plan['release_input']['build_config']).resolve()),
                '--version',plan['release_input']['version'],'--product-version',plan['release_input']['product_version'],
                '--commit',plan['head_commit']]
    raise Gap('unimplemented runner adapter: ' + adapter)


def audio_identity(config,scenario):
    if set(config)-{'fixtures','fixture_hashes','permission','components'}:raise Gap('unknown audio input fields')
    if config.get('permission') not in ('self_created','redistributable'):raise Gap('audio fixture permission missing')
    folder=protected(config['fixtures'])
    names=('source.m4a','standard.wav','short.wav','script.txt')
    hashes={n:digest(folder/n) for n in names}
    if hashes!=config.get('fixture_hashes'):raise Blocked('audio fixture hash mismatch')
    identity={'fixture_hashes':hashes,'permission':config['permission']}
    if scenario['runner']['adapter']=='audio_engine':
        entry=config.get('components',{}).get(scenario['platform'])
        if not entry:raise Gap('missing actual audio component for '+scenario['platform'])
        if set(entry)!={'root','tree_sha256','python','model'}:raise Gap('invalid audio component fields')
        component=protected(entry['root']);python=Path(entry['python']).resolve();model=Path(entry['model']).resolve()
        if not contained(python,component) or not contained(model,component) or not model.is_dir():
            raise Blocked('audio runtime/model must belong to measured component tree')
        measured=tree_digest(component)
        if measured!=entry['tree_sha256']:raise Blocked('audio component tree changed')
        identity['component']={'tree_sha256':measured,'python_sha256':digest(python),
                               'python_relative':str(python.relative_to(component)),'model_relative':str(model.relative_to(component))}
    return identity


def read_pcm(path):
    import wave
    if Path(path).is_symlink():raise Blocked('linked PCM evidence')
    with wave.open(str(path),'rb') as stream:
        if (stream.getnchannels(),stream.getsampwidth(),stream.getframerate())!=(1,2,16000):raise Gap('PCM format mismatch')
        return stream.readframes(stream.getnframes())


def validate_audio(folder,plan,scenario):
    config=plan['release_input']['audio_input']
    if audio_identity(config,scenario)!=plan['snapshots'][scenario['id']]['audio_inputs']:
        return 'failed','audio_input_identity_changed'
    fixture=protected(config['fixtures']);standard=read_pcm(fixture/'standard.wav');short=read_pcm(fixture/'short.wav')
    if len(standard)<312*32000 or len(short)!=24*32000 or standard[:len(short)]!=short:
        return 'failed','audio_fixture_duration_or_origin'
    if scenario['runner']['adapter']=='audio_pcm':
        report=read(folder/'result.json')
        if (report.get('source_sha256')!=digest(fixture/'source.m4a')
                or report.get('standard_pcm_sha256')!=hashlib.sha256(standard).hexdigest()
                or report.get('standard_frames')!=len(standard)//2 or report.get('normalization_pcm_equal') is not True
                or read_pcm(folder/'normalization/standard.wav')!=standard):return 'failed','audio_normalization_mismatch'
        cases=report.get('cases',[])
        if {r.get('label') for r in cases}!={'head','window8','worker20','segment300','tail'} or len(cases)!=5:
            return 'not_run','audio_cases_missing'
        for row in cases:
            start,count=row['start_frame'],row['frames']
            if type(start)!=int or type(count)!=int or start<0 or count<=0 or (start+count)*2>len(standard):
                return 'failed','audio_range_invalid'
            if row.get('pcm_equal') is not True or read_pcm(folder/(row['label']+'.wav'))!=standard[start*2:(start+count)*2]:
                return 'failed','audio_preview_discontinuity'
    else:
        summary=read(folder/'summary.json');runtime=read(folder/'runtime.json')
        version=read(Path(plan['source_root'])/'src/knowledge_distiller/v1/adapters/python-runtime.json')['version']
        worker='qwen_worker.py' if scenario['platform']=='macos-arm64' else 'qwen_windows_worker.py'
        if (not runtime.get('driver_python','').startswith(version+' ') or not runtime.get('component_python','').startswith(version+' ')
                or runtime.get('worker_sha256')!=digest(Path(plan['source_root'])/'src/knowledge_distiller/v1/adapters'/worker)):
            return 'failed','audio_actual_runtime_mismatch'
        if any(summary.get(k) is not True for k in ('long_success','location_text_unchanged','location_pcm_equal','recovery_pcm_equal','recovery_success')):
            return 'failed','audio_engine_journey_failure'
        calls=sorted((folder/'calls').glob('*/input.json'))
        if not calls or len(calls)!=summary.get('actual_worker_calls'):return 'not_run','audio_calls_missing'
        original=Path(read(folder/'probe-binding.json')['output_root'])
        for path in calls:
            measured=read(path);source=Path(measured['source'])
            if contained(source,original): pcm_source=folder/source.relative_to(original)
            elif source in (fixture/'standard.wav',fixture/'short.wav'):pcm_source=source
            else:return 'failed','audio_call_source_outside_probe'
            pcm=read_pcm(pcm_source)
            if measured.get('returncode')!=0 or measured.get('frames')!=len(pcm)//2 or measured.get('pcm_sha256')!=hashlib.sha256(pcm).hexdigest():
                return 'failed','audio_call_input_mismatch'
            response=read(path.with_name('result.json'));digest(path.with_name('stderr.txt'))
            if not response.get('text') or response.get('truncated') is True:return 'failed','audio_call_incomplete'
        for pattern,expected in [('long/asr-segments/*/audio.wav',standard),('locations/location-recovery/*/audio.wav',short),('recovery/asr-recovery/*/audio.wav',short)]:
            if b''.join(read_pcm(p) for p in sorted(folder.glob(pattern)))!=expected:return 'failed','audio_segments_discontinuous'
    return 'passed',None


def output_paths(scenario, attempt=None):
    adapter=scenario['runner']['adapter']
    if adapter=='pytest': return ['junit.xml']
    if adapter=='verify_candidate':
        return ['verification/'+n for n in ('result.json','runtime.json','helper-runtime.json','runtime.log','launch.log','support.md')]
    if adapter=='audio_pcm':return ['verification/result.json','verification/normalization/standard.wav',*[f'verification/{n}.wav' for n in ('head','window8','worker20','segment300','tail')]]
    if adapter=='audio_engine':
        required=['verification/'+n for n in ('runtime.json','summary.json','long-result.json','long-pcm-cache.json','locations-result.json','recovery-result.json','probe-binding.json')]
        if attempt:
            required+= [str(p.relative_to(attempt)) for p in (attempt/'verification').rglob('*') if p.is_file() and p.suffix in ('.json','.txt','.wav') and not {'cache','hf-cache','tmp'}.intersection(p.relative_to(attempt/'verification').parts)]
        return sorted(set(required))
    if adapter=='manual_browser': return ['verification/result.json','verification/commands.json']
    if adapter=='desktop_browser':
        files=['result.json','parameter-lock.json','fixture.stdout.log','fixture.stderr.log','runner.stdout.log','runner.stderr.log']
        if scenario['runner']['case']=='protocol':files.append('icons-light-dark-2x.png')
        return ['verification/'+n for n in files]
    return []


def validate_manual_browser(folder,plan):
    result=read(folder/'result.json');commands=read(folder/'commands.json')
    root=Path(plan['source_root'])
    version=read(root/'src/knowledge_distiller/v1/adapters/python-runtime.json')['version']
    if (result.get('status')!='passed' or result.get('level')!='integration'
            or result.get('fixture')!='synthetic_queue' or result.get('python')!=version
            or result.get('platform')!=platform.platform() or not result.get('browser') or not result.get('browser_full_version')
            or result.get('source_commit')!=plan['head_commit'] or result.get('source_dirty') is not False
            or result.get('source_sha256')!=digest(root/'src/knowledge_distiller/v1/static/home.js')):
        return 'failed','browser_identity_or_result_mismatch'
    if not isinstance(commands,list) or not commands or any(c.get('returncode')!=0 for c in commands):
        return 'failed','browser_command_failure'
    if not {'open','eval','close'} <= {c.get('command',[None])[0] for c in commands}:
        return 'failed','browser_command_trace_missing'
    if result.get('assertions') != [{'id':'reconciliation_'+mode,'passed':True} for mode in ('playing','paused')]:
        return 'failed','browser_assertions_missing'
    for mode in ('playing','paused'):
        row=result.get(mode,{});samples=row.get('samples',[])
        if len(samples)!=20:return 'not_run','browser_sample_count'
        for sample in samples:
            if not (sample['sameAudio'] and sample['sameInput'] and sample['focus']
                    and sample['draft']=='保留合成草稿' and sample['selection']==[2,4]
                    and abs(sample['anchorDelta'])<=2 and 0<=sample['ms']<=3000
                    and (sample['playing'] and sample['audioDelta']>0 if mode=='playing'
                         else not sample['playing'] and abs(sample['audioDelta'])<=.1)):
                return 'failed','browser_sample_assertion'
        times=sorted(s['ms'] for s in samples)
        if row.get('max_ms')!=times[-1] or row.get('p95_ms')!=times[18]:
            return 'failed','browser_summary_mismatch'
    return 'passed',None


def validate_desktop_browser(folder,plan,case):
    result=read(folder/'result.json');parameters=read(folder/'parameter-lock.json')
    runner='run_browser_protocol.py' if case=='protocol' else 'slow_open_browser.py'
    root=Path(plan['source_root']);runner_hash=digest(root/'tests/v1/desktop'/runner)
    measured_hash=parameters.get('runner',{}).get('sha256') if case=='protocol' else parameters.get('runner_sha256')
    if (result.get('result')!='passed' or parameters.get('source_commit')!=plan['head_commit']
            or parameters.get('dirty') is not False or measured_hash!=runner_hash
            or parameters.get('fixture_sha256')!=digest(root/'tests/v1/desktop/browser_fixture.py')
            or not parameters.get('browser') or parameters.get('native_dock')!='not_run'):
        return 'failed','desktop_browser_identity_or_result_mismatch'
    records=result.get('records',[])
    if case=='protocol':
        expected={'initial_handshake','navigation_preserves_page_new_epoch','reopen_ack_not_foreground_claim',
                  'brand_sizes_light_dark_retina_resource_gallery','copied_session_storage_allocates_distinct_page',
                  'browser_close_transport_observed','browser_shutdown_signal_observation'}
        expected|={f'slow_{action}_{seconds}s_no_duplicate' for action in ('navigate','reload') for seconds in (.4,2,5)}
        lookup={r.get('name'):r for r in records}
        if not expected <= lookup.keys():return 'not_run','desktop_browser_cases_missing'
        for name in expected:
            if name.startswith('slow_') and lookup[name].get('state',{}).get('opened')!=[]:
                return 'failed','desktop_duplicate_open'
        state=lookup['reopen_ack_not_foreground_claim']['state']
        if state['opened'] or not state['results'] or state['results'][-1]['foreground_verified']:
            return 'failed','desktop_false_foreground'
        digest(folder/'icons-light-dark-2x.png')
    else:
        lookup={r.get('case'):r for r in records}
        expected={'budget_timeout','five_requests_do_not_open_again','late_matching_handshake','late_page_is_reusable'}
        if not expected<=lookup.keys():return 'not_run','desktop_browser_cases_missing'
        if lookup['budget_timeout']['state']['results'][0]['reason']!='open_handshake_timeout':
            return 'failed','desktop_timeout_missing'
        for row in lookup.values():
            if len(row['state']['opened'])!=1:return 'failed','desktop_duplicate_open'
        if lookup['late_page_is_reusable']['state']['results'][-1]['foreground_verified']:
            return 'failed','desktop_false_foreground'
    return 'passed',None


def validate_result(scenario, attempt, returncode, plan=None, logdir=None):
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
        expected_platform='mac' if scenario['platform'].startswith('macos-') else 'windows'
        if (result.get('platform') != expected_platform or result.get('source_commit') != candidate_source(plan,scenario) or result.get('version') != plan['release_input']['version'] or result.get('disposable_data') is not True):
            return 'failed','candidate_identity_mismatch'
        version=read(Path(plan['source_root'])/'src/knowledge_distiller/v1/adapters/python-runtime.json')['version']
        runtime=read(attempt/'verification/runtime.json');helper=read(attempt/'verification/helper-runtime.json')
        if (not runtime.get('ok') or not runtime.get('frozen') or not runtime.get('python_inventory')
                or runtime.get('python',{}).get('version') != version or not helper.get('frozen')
                or helper.get('python',{}).get('version') != version or result.get('runtime') != runtime
                or result.get('update_helper_runtime') != helper or result.get('launches') != 2
                or result.get('pages') != 4 or result.get('fixed_port') != 57740):
            return 'failed','candidate_runtime_or_journey_mismatch'
        for name in ('runtime.log','launch.log','support.md'):
            digest(attempt/'verification'/name)
    elif scenario['runner']['adapter'] in ('audio_pcm','audio_engine'):
        return validate_audio(attempt/'verification',plan,scenario)
    elif scenario['runner']['adapter'] == 'manual_browser':
        return validate_manual_browser(attempt/'verification',plan)
    elif scenario['runner']['adapter'] == 'desktop_browser':
        return validate_desktop_browser(attempt/'verification',plan,scenario['runner']['case'])
    elif scenario['runner']['adapter'] in ('build_job','dual_build'):
        result=read(logdir/'stdout.log')
        records=[result] if scenario['runner']['adapter']=='build_job' else [result['mac'],result['windows']]
        if any(r.get('status')!='succeeded' for r in records): return 'not_run','build_not_complete'
        if scenario['runner']['adapter']=='build_job':
            job=protected(plan['release_input']['build_job']); request=read(job/'request.json')
            if request.get('source_commit')!=plan['head_commit']: return 'failed','build_source_mismatch'
            if not result.get('artifacts'): return 'not_run','missing_build_artifacts'
            for item in result['artifacts']:
                target=(job/item['path']).resolve()
                if not contained(target,job) or digest(target)!=item['sha256']: return 'failed','build_artifact_mismatch'
        elif result.get('commit')!=plan['head_commit']: return 'failed','build_source_mismatch'
    return 'passed', None


def verify_passport(passport, scenario, plan, plan_dir):
    check = dict(passport); signature = check.pop('passport_sha256',None)
    if not signature or object_hash(check) != signature: raise Blocked('passport hash mismatch')
    if passport.get('scenario_id') != scenario['id'] or passport.get('level') != scenario['level']:
        raise Blocked('scenario/evidence level mismatch')
    expected_platform = host_platform() if scenario['platform']=='host' else scenario['platform']
    if passport.get('platform') != expected_platform: raise Blocked('platform mismatch')
    if not passport.get('source_commit') or not passport.get('started_at') or not passport.get('finished_at'):
        raise Blocked('missing evidence source/time identity')
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
    if scenario.get('runner'):
        if passport.get('environment',{}).get('os')!=platform.platform():raise Blocked('OS environment changed')
        if scenario['runner']['adapter']=='verify_candidate' and passport.get('artifact_source_commit')!=candidate_source(plan,scenario):
            raise Blocked('candidate source identity mismatch')
        directories={Path(r['path']).parent for r in passport['outputs'] if Path(r['path']).name=='stdout.log'}
        if len(directories)!=1: raise Blocked('missing unique execution logs')
        logdir=plan_dir/next(iter(directories))
        required={'stdout.log','stderr.log',*output_paths(scenario,logdir)}
        recorded={str(Path(r['path']).relative_to(logdir.relative_to(plan_dir))) for r in passport['outputs']
                  if contained(Path(r['path']),logdir.relative_to(plan_dir))}
        if not required <= recorded: raise Blocked('missing required runner outputs')
        result,_=validate_result(scenario,logdir,passport.get('exit_code',-1),plan,logdir)
        if result != 'passed': raise Blocked('runner outputs do not attest pass')
    return True


def inspect(plan, path, gate):
    rows = []
    promotion_error=None
    try: check_promotion(plan,gate)
    except Gap as exc: promotion_error=str(exc)
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
    code = 1 if promotion_error or any(r['result'] in ('failed','invalid','cancelled','timeout','tool_error') for r in rows) else 2 if not rows or any(r['result'] != 'passed' for r in rows) else 0
    return {'gate':gate,'result':'passed' if code==0 else 'blocked' if code==1 else 'not_run','exit_code':code,'scenarios':rows,**({'reason':promotion_error} if promotion_error else {})}


def run(plan, path, gate, disposable):
    # Registered collection can repair missing evidence; promotion still fails.
    check_promotion(plan,'module')
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
                browser_sockets = None
                try:
                    if scenario['platform'] not in ('host',host_platform()): raise Gap('requires native host ' + scenario['platform'])
                    passport['artifact_sha256'] = artifact_identity(plan,scenario)
                    if scenario['runner']['adapter']=='verify_candidate':
                        passport['artifact_source_commit']=candidate_source(plan,scenario)
                    command = command_for(scenario,plan,attempt); passport['invocation']=command
                    env = {k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','CONDA','VIRTUAL_ENV','KNOWLEDGE_DISTILLER'))}
                    home = attempt/'home'; home.mkdir()
                    env.update(HOME=str(home),USERPROFILE=str(home),APPDATA=str(home/'AppData/Roaming'),
                               LOCALAPPDATA=str(home/'AppData/Local'),TMPDIR=str(attempt),TEMP=str(attempt),TMP=str(attempt),
                               PYTHONPATH=str(root/'src')+os.pathsep+str(root),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',
                               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',KNOWLEDGE_DISTILLER_DATA_DIR=str(attempt/'data'))
                    if os.name != 'nt' and scenario['runner']['adapter'] in ('desktop_browser', 'manual_browser'):
                        # Unix socket paths have a 103-byte macOS limit; the isolated HOME is longer.
                        browser_sockets = tempfile.TemporaryDirectory(prefix='kdbr-', dir='/tmp')
                        env['AGENT_BROWSER_SOCKET_DIR'] = browser_sockets.name
                    with (logdir/'stdout.log').open('wb') as stdout, (logdir/'stderr.log').open('wb') as stderr:
                        process = subprocess.Popen(command,cwd=root,env=env,stdout=stdout,stderr=stderr,start_new_session=os.name!='nt')
                        passport['exit_code']=process.wait(timeout=scenario['timeout'])
                    if scenario['runner']['adapter']=='audio_engine' and (attempt/'verification').is_dir():
                        atomic(attempt/'verification/probe-binding.json',{'output_root':str(attempt/'verification')})
                    passport['result'],passport['failure_kind']=validate_result(scenario,attempt,passport['exit_code'],plan,logdir)
                    if scenario['runner']['adapter']=='manual_browser':
                        passport['environment']['browser']={k:read(attempt/'verification/result.json').get(k) for k in ('browser','browser_full_version')}
                    elif scenario['runner']['adapter']=='desktop_browser':
                        passport['environment']['browser']=read(attempt/'verification/parameter-lock.json').get('browser')
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
                    if browser_sockets is not None:
                        browser_sockets.cleanup()
                    # Copy machine-readable assertion result, retain raw attempt data locally.
                    for relative in output_paths(scenario,attempt):
                        source=attempt/relative
                        if source.is_file():
                            target=logdir/relative;target.parent.mkdir(parents=True,exist_ok=True)
                            target.write_bytes(source.read_bytes())
                    for log in sorted(p for p in logdir.rglob('*') if p.is_file()):
                        passport['outputs'].append({'path':log.relative_to(path.parent).as_posix(),'sha256':digest(log)})
                    try:
                        if git('rev-parse','HEAD',root=root)!=plan['head_commit'] or git('status','--porcelain',root=root):
                            passport.update(result='tool_error',failure_kind='source_changed_during_run',dirty=True)
                    except subprocess.SubprocessError:
                        passport.update(result='tool_error',failure_kind='source_identity_unavailable',dirty=True)
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
