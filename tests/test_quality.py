"""Independent gate counterexamples; uses only disposable fixtures."""
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('quality_cli',Path(__file__).parents[1]/'scripts/quality.py')
q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)


def test_registry_has_real_runners_and_visible_native_gaps():
    _,scenarios,_,_=q.registry()
    quality=next(s for s in scenarios if s['id']=='SC-QUALITY--synthetic')
    assert not q.scenario_gaps(quality)
    native=next(s for s in scenarios if s['id']=='SC-DESKTOP-REOPEN--macos-arm64--native')
    assert q.scenario_gaps(native)==['missing runner: '+native['id']]


def test_unmapped_and_severe_defects_block():
    with pytest.raises(q.Blocked,match='unmapped'):
        q.check_promotion({'unmapped_paths':['unknown.py']},'module')
    plan={'unmapped_paths':[],'new_defects':[{'id':'bad','severity':'S1','status':'open'}]}
    q.check_promotion(plan,'module')
    with pytest.raises(q.Blocked,match='S0/S1'):q.check_promotion(plan,'candidate')
    plan['new_defects'][0]['severity']='S2'
    q.check_promotion(plan,'candidate')


def test_data_roots_reject_unowned_and_protected_and_allow_exact_plan(tmp_path):
    with pytest.raises(q.Blocked):q.data_root(tmp_path,'plan')
    path=tmp_path/'new'
    assert q.data_root(path,'plan')==path
    assert q.data_root(path,'plan')==path
    with pytest.raises(q.Blocked):q.data_root(path,'other')
    for path in ['/Applications/知识蒸馏器.app',str(tmp_path/'Vault'),str(tmp_path/'工程治理')]:
        with pytest.raises(q.Blocked):q.protected(path)


def test_lock_excludes_concurrent_writer(tmp_path):
    with q.lock(tmp_path/'lock'):
        with pytest.raises(q.Gap):
            with q.lock(tmp_path/'lock'):pass


def test_skips_and_empty_results_never_pass(tmp_path):
    s={'runner':{'adapter':'pytest'}}
    (tmp_path/'junit.xml').write_text('<testsuites><testsuite><testcase><skipped /></testcase></testsuite></testsuites>')
    assert q.validate_result(s,tmp_path,0)==('not_run','skipped_assertions')
    (tmp_path/'junit.xml').write_text('<testsuites/>')
    assert q.validate_result(s,tmp_path,0)==('not_run','empty_collection')
    assert q.validate_result(s,tmp_path,2)==('failed','runner_failure')


def seal(passport):
    passport.pop('passport_sha256',None)
    passport['passport_sha256']=q.object_hash(passport)
    return passport


@pytest.fixture
def evidence(tmp_path):
    root=tmp_path/'source'; policy=root/'src/knowledge_distiller/v1/adapters/python-runtime.json'
    q.atomic(policy,{'version':q.platform.python_version()})
    fixture=root/'fixture.json';q.atomic(fixture,{'synthetic':True})
    scenario={'id':'sample','level':'synthetic','platform':'host'}
    plan={'source_root':str(root),'snapshots':{'sample':{'identity':'unchanged'}},'release_input':{},'change_id':'new'}
    log=tmp_path/'log.txt';log.write_text('one real assertion passed')
    passport=dict(scenario_id='sample',level='synthetic',platform=q.host_platform(),source_commit='original-source',
        started_at='2026-09-14T00:00:00Z',finished_at='2026-09-14T00:00:01Z',dirty=False,
        environment={'python':q.platform.python_version()},dependencies={'identity':'unchanged'},
        artifact_sha256=None,outputs=[{'path':'log.txt','sha256':q.digest(log)}],result='passed')
    return plan,scenario,seal(passport),tmp_path


def test_valid_unchanged_evidence_preserves_original_commit(evidence):
    plan,s,p,folder=evidence
    assert q.verify_passport(p,s,plan,folder)
    assert p['source_commit']=='original-source'


@pytest.mark.parametrize('field,value', [('level','native'),('platform','windows-x64-pretend'),
    ('dirty',True),('dependencies',{'identity':'stale'}),('outputs',[]),('source_commit',None)])
def test_tampered_passport_cannot_pass_even_with_recomputed_checksum(evidence,field,value):
    plan,s,p,folder=evidence;p[field]=value;seal(p)
    with pytest.raises(q.Blocked):q.verify_passport(p,s,plan,folder)


def test_hash_corruption_missing_log_and_runtime_rejected(evidence):
    plan,s,p,folder=evidence
    p['passport_sha256']='0'*64
    with pytest.raises(q.Blocked,match='passport hash'):q.verify_passport(p,s,plan,folder)
    seal(p);(folder/'log.txt').write_text('modified')
    with pytest.raises(q.Blocked,match='output hash'):q.verify_passport(p,s,plan,folder)
    p['environment']['python']='3.11.15';seal(p)
    with pytest.raises(q.Blocked,match='runtime'):q.verify_passport(p,s,plan,folder)


def test_evidence_path_cannot_escape_even_with_matching_hash(evidence):
    plan,s,p,folder=evidence
    outside=folder.parent/'outside-log';outside.write_text('outside')
    p['outputs']=[{'path':'../outside-log','sha256':q.digest(outside)}];seal(p)
    with pytest.raises(q.Blocked,match='escapes'):q.verify_passport(p,s,plan,folder)


@pytest.fixture
def repository(tmp_path):
    import subprocess
    root=tmp_path/'repo';root.mkdir()
    def git(*args):return subprocess.check_output(['git',*args],cwd=root,text=True).strip()
    git('init','-q');git('config','user.name','Quality Fixture');git('config','user.email','quality@example.invalid')
    requirements={'schema_version':1,'requirements':{'ENG':{}}}
    scenarios={'scenarios':[{'id':'unit','requirements':['ENG'],'components':['business'],'gate':'module',
        'level':'synthetic','platform':'host','runner':{'adapter':'pytest','paths':['tests/test_business.py']},
        'fixture':'quality/fixture.json','assertions':['counterexample'],'timeout':1,'not_proven':['native']}]}
    impact={'components':{'business':{'owner':'A','paths':['business.py','tests/test_business.py'],
        'depends_on':[],'artifact':False},'main':{'owner':'E','paths':['main.py'],'depends_on':['business'],'artifact':True},
        'quality':{'owner':'E','paths':['quality/**'],'depends_on':[],'artifact':False}}}
    q.atomic(root/'quality/requirements.yaml',requirements);q.atomic(root/'quality/scenarios.yaml',scenarios)
    q.atomic(root/'quality/change-impact.yaml',impact);q.atomic(root/'quality/fixture.json',{})
    (root/'tests').mkdir();(root/'tests/test_business.py').write_text('def test_thing(): assert True\n')
    (root/'business.py').write_text('value=1\n');(root/'main.py').write_text('import business\n')
    q.atomic(root/'src/knowledge_distiller/v1/adapters/python-runtime.json',{'version':q.platform.python_version()})
    git('add','.');git('commit','-qm','baseline');base=git('rev-parse','HEAD')
    return root,git,base


def test_runner_only_invalidates_evidence_not_product_and_unmapped_delete_blocks(repository,tmp_path):
    root,git,base=repository
    (root/'tests/test_business.py').write_text('def test_thing(): assert 1 == 1\n')
    git('add','.');git('commit','-qm','collector only')
    plan=q.make_plan(base,'HEAD',None,tmp_path/'plan',root)
    assert plan['invalidate_scenarios']==['unit']
    assert plan['rebuild_components']==[]
    (root/'new-unmapped.py').write_text('pass')
    git('add','.');git('commit','-qm','unmapped')
    plan=q.make_plan(base,'HEAD',None,tmp_path/'plan-bad',root)
    with pytest.raises(q.Blocked,match='unmapped'):q.check_promotion(plan,'module')
    newbase=git('rev-parse','HEAD');(root/'new-unmapped.py').unlink()
    git('add','.');git('commit','-qm','delete unmapped')
    plan=q.make_plan(newbase,'HEAD',None,tmp_path/'plan-delete',root)
    assert plan['unmapped_paths']==['new-unmapped.py']


def test_product_change_propagates_rebuild_and_missing_runner_remains_gap(repository,tmp_path):
    root,git,base=repository
    (root/'business.py').write_text('value=2\n');git('add','.');git('commit','-qm','product')
    plan=q.make_plan(base,'HEAD',None,tmp_path/'plan',root)
    assert plan['rebuild_components']==['main']
    (root/'tests/test_business.py').unlink();git('add','.');git('commit','-qm','runner removed')
    plan=q.make_plan(base,'HEAD',None,tmp_path/'plan-gap',root)
    assert plan['gaps']['unit']==['missing runner file: tests/test_business.py']


def test_run_records_real_failure_timeout_and_logs(repository,tmp_path):
    root,git,base=repository
    (root/'tests/test_business.py').write_text('def test_thing(): assert False\n')
    git('add','.');git('commit','-qm','real failure')
    output=tmp_path/'plan'
    plan=q.make_plan(base,'HEAD',None,output,root)
    result=q.run(plan,output/'plan.json','module',tmp_path/'data')
    assert result['exit_code']==1
    p=q.read(output/'evidence/unit.json')
    assert p['result']=='failed' and p['exit_code']==1
    assert {Path(r['path']).name for r in p['outputs']} >= {'stdout.log','stderr.log','junit.xml'}
    (root/'tests/test_business.py').write_text('import time\ndef test_thing(): time.sleep(5)\n')
    git('add','.');git('commit','-qm','real timeout')
    output=tmp_path/'timeout-plan';plan=q.make_plan(base,'HEAD',None,output,root)
    result=q.run(plan,output/'plan.json','module',tmp_path/'timeout-data')
    assert result['exit_code']==1
    assert q.read(output/'evidence/unit.json')['result']=='timeout'


def test_build_adapters_use_real_interfaces_and_no_input_shell(tmp_path):
    plan={'source_root':str(q.ROOT),'head_commit':'commit','release_input':{'version':'2026.09.14.2',
        'product_version':'1.3','build_job':str(tmp_path/'job'),'build_config':str(tmp_path/'config.json')}}
    for adapter in ['build_job','dual_build']:
        command=q.command_for({'runner':{'adapter':adapter}},plan,tmp_path)
        assert command[:3]==[q.sys.executable,str(q.ROOT/'scripts'/ (adapter+'.py')),'status']
    with pytest.raises(q.Gap,match='unimplemented'):
        q.command_for({'runner':{'adapter':'shell','command':'touch unexpected'}},plan,tmp_path)


def test_actual_evidence_reuse_keeps_original_source_and_rejects_changed_runner(repository,tmp_path):
    root,git,base=repository
    registry=q.read(root/'quality/scenarios.yaml');registry['scenarios'][0]['always']=True
    q.atomic(root/'quality/scenarios.yaml',registry);git('add','.');git('commit','-qm','always regression')
    old_head=git('rev-parse','HEAD');old_dir=tmp_path/'old'
    plan=q.make_plan(base,'HEAD',None,old_dir,root)
    assert q.run(plan,old_dir/'plan.json','module',tmp_path/'old-data')['exit_code']==0
    old=q.read(old_dir/'evidence/unit.json')
    (root/'main.py').write_text('import business\n# unrelated packaging entry\n')
    git('add','.');git('commit','-qm','unchanged business dependency')
    config=tmp_path/'reuse.json';q.atomic(config,{'reuse_evidence':[{'passport':str(old_dir/'evidence/unit.json'),
        'plan_dir':str(old_dir),'reason':'Business inputs, runner, fixture, exact platform and Python unchanged.'}]})
    new_dir=tmp_path/'new';new=q.make_plan(base,'HEAD',config,new_dir,root)
    assert new['reuse_evidence_ids']==[old['evidence_id']]
    assert q.read(new_dir/'evidence/unit.json')['source_commit']==old_head
    assert q.inspect(new,new_dir/'plan.json','module')['exit_code']==0
    (root/'tests/test_business.py').write_text('def test_thing(): assert 1\n')
    git('add','.');git('commit','-qm','changed runner')
    with pytest.raises(q.Blocked,match='stale'):
        q.make_plan(base,'HEAD',config,tmp_path/'stale',root)


def test_transitive_dependency_hash_is_bound(repository):
    root,git,base=repository
    _,_,impact,_=q.registry(root)
    scenario={'components':['main'],'runner':{'paths':['tests/test_business.py']},'fixture':'quality/fixture.json'}
    snap=q.snapshot(scenario,impact,root)
    assert set(snap['component_inputs'])=={'main','business'}


def test_candidate_archive_and_build_tree_both_bound(tmp_path):
    build=tmp_path/'build';build.mkdir();(build/'executable').write_text('candidate')
    archive=tmp_path/'candidate.zip';archive.write_text('archive')
    scenario={'level':'native','platform':'macos-arm64'}
    entry={'path':str(archive),'sha256':q.digest(archive),'build':str(build),'build_sha256':q.tree_digest(build)}
    plan={'release_input':{'artifacts':{'macos-arm64':entry}}}
    assert q.artifact_identity(plan,scenario)==entry['sha256']
    (build/'executable').write_text('different candidate')
    with pytest.raises(q.Blocked,match='tree changed'):q.artifact_identity(plan,scenario)


def test_existing_build_job_status_uses_real_artifact_validation(tmp_path):
    import subprocess,json
    job=tmp_path/'job';job.mkdir()
    artifact=job/'artifact.bin';artifact.write_text('built')
    q.atomic(job/'request.json',{'source_commit':'commit'})
    q.atomic(job/'status.json',{'status':'succeeded','artifacts':[{'path':'artifact.bin','sha256':q.digest(artifact)}]})
    scenario={'runner':{'adapter':'build_job'}}
    plan={'source_root':str(q.ROOT),'head_commit':'commit','release_input':{'build_job':str(job)}}
    logs=tmp_path/'logs';logs.mkdir()
    result=subprocess.run(q.command_for(scenario,plan,tmp_path),capture_output=True)
    (logs/'stdout.log').write_bytes(result.stdout)
    assert q.validate_result(scenario,tmp_path,result.returncode,plan,logs)==('passed',None)
    artifact.write_text('corrupted')
    assert q.validate_result(scenario,tmp_path,0,plan,logs)==('failed','build_artifact_mismatch')


def test_windows_only_assertion_is_registered_separately():
    _,scenarios,_,_=q.registry()
    normal=next(s for s in scenarios if s['id']=='SC-RELEASE-INTEGRITY--synthetic')
    windows=next(s for s in scenarios if s['id']=='SC-BUILD-CHILD--windows-x64')
    assert windows['runner']['nodeids']==normal['runner']['deselect']
    assert windows['platform']=='windows-x64' and windows['level']=='integration'
    command=q.command_for(windows,{'source_root':str(q.ROOT)},Path('/tmp/isolated'))
    assert windows['runner']['nodeids'][0] in command
    assert '--deselect='+windows['runner']['nodeids'][0] in q.command_for(normal,{'source_root':str(q.ROOT)},Path('/tmp/isolated'))


@pytest.fixture
def browser_report(tmp_path):
    root=tmp_path/'source'
    source=root/'src/knowledge_distiller/v1/static/home.js';source.parent.mkdir(parents=True);source.write_text('source')
    q.atomic(root/'src/knowledge_distiller/v1/adapters/python-runtime.json',{'version':q.platform.python_version()})
    plan={'source_root':str(root),'head_commit':'current'}
    folder=tmp_path/'evidence';folder.mkdir()
    result=dict(status='passed',level='integration',fixture='synthetic_queue',python=q.platform.python_version(),
        platform=q.platform.platform(),source_sha256=q.digest(source),browser='Chromium measured version',
        assertions=[{'id':'reconciliation_'+mode,'passed':True} for mode in ('playing','paused')])
    for mode in ('playing','paused'):
        result[mode]={'samples':[dict(sameAudio=True,sameInput=True,focus=True,draft='保留合成草稿',selection=[2,4],
            anchorDelta=0,ms=100,playing=mode=='playing',audioDelta=1 if mode=='playing' else 0) for _ in range(20)],'max_ms':100,'p95_ms':100}
    q.atomic(folder/'result.json',result)
    q.atomic(folder/'commands.json',[{'command':[c],'returncode':0} for c in ('open','eval','close')])
    return plan,folder,result


def test_browser_adapter_rechecks_samples_and_identity(browser_report):
    plan,folder,result=browser_report
    assert q.validate_manual_browser(folder,plan)==('passed',None)
    result['playing']['samples'][0]['ms']=3100
    q.atomic(folder/'result.json',result)
    assert q.validate_manual_browser(folder,plan)==('failed','browser_sample_assertion')


@pytest.mark.parametrize('field,value',[('platform','other-platform'),('source_sha256','false'),('level','native'),('status','failed')])
def test_browser_adapter_cannot_accept_arbitrary_pass_json(browser_report,field,value):
    plan,folder,result=browser_report;result[field]=value;q.atomic(folder/'result.json',result)
    assert q.validate_manual_browser(folder,plan)[0]=='failed'


def test_browser_adapter_requires_trace_and_all_samples(browser_report):
    plan,folder,result=browser_report
    result['paused']['samples'].pop();q.atomic(folder/'result.json',result)
    assert q.validate_manual_browser(folder,plan)==('not_run','browser_sample_count')
    (folder/'commands.json').unlink()
    with pytest.raises(OSError):q.validate_manual_browser(folder,plan)


def test_resealed_success_passport_cannot_override_failed_actual_junit(repository,tmp_path):
    root,git,base=repository
    (root/'tests/test_business.py').write_text('def test_thing(): assert False\n');git('add','.');git('commit','-qm','fail')
    output=tmp_path/'plan';plan=q.make_plan(base,'HEAD',None,output,root)
    q.run(plan,output/'plan.json','module',tmp_path/'data')
    passport=q.read(output/'evidence/unit.json');passport['result']='passed';passport['exit_code']=0;seal(passport)
    with pytest.raises(q.Blocked,match='outputs do not attest pass'):
        q.verify_passport(passport,plan['scenarios'][0],plan,output)
    passport['outputs']=[r for r in passport['outputs'] if not r['path'].endswith('junit.xml')];seal(passport)
    with pytest.raises(q.Blocked,match='required runner outputs'):
        q.verify_passport(passport,plan['scenarios'][0],plan,output)


def test_candidate_adapter_checks_actual_identity_and_required_files(tmp_path,monkeypatch):
    monkeypatch.setattr(q,"candidate_source",lambda *args:"current")
    plan={'source_root':str(q.ROOT),'head_commit':'current','release_input':{'version':'candidate'}}
    scenario={'runner':{'adapter':'verify_candidate'},'platform':'macos-arm64'}
    report={'ok':True,'source_commit':'current','version':'candidate','platform':'windows','disposable_data':True}
    q.atomic(tmp_path/'verification/result.json',report)
    assert q.validate_result(scenario,tmp_path,0,plan)==('failed','candidate_identity_mismatch')
    report['platform']='mac';q.atomic(tmp_path/'verification/result.json',report)
    with pytest.raises(OSError):q.validate_result(scenario,tmp_path,0,plan)


def test_browser_cannot_be_promoted_to_native_by_registry(repository):
    root,git,base=repository
    registry=q.read(root/'quality/scenarios.yaml');s=registry['scenarios'][0]
    s.update(level='native',platform='macos-arm64',runner={'adapter':'manual_browser','paths':[
        'tests/v1/browser/manual_browser.py','tests/v1/browser/manual_fixture.py','tests/v1/browser/manual_samples.js']})
    q.atomic(root/'quality/scenarios.yaml',registry)
    with pytest.raises(q.Gap,match='integration only'):q.registry(root)


def test_severe_incident_allows_collection_but_never_promotion(repository,tmp_path):
    root,git,base=repository
    (root/'business.py').write_text('value=2');git('add','.');git('commit','-qm','changed')
    output=tmp_path/'plan';plan=q.make_plan(base,'HEAD',None,output,root)
    plan['new_defects']=[{'id':'severe','severity':'S0','status':'module_fixed_candidate_pending'}]
    result=q.run(plan,output/'plan.json','native',tmp_path/'data')
    assert result['exit_code']==1 and result['result']=='blocked'
    assert 'S0/S1' in result['reason']
    assert result['scenarios'][0]['result']=='passed'


def test_candidate_keeps_original_commit_for_collector_only_changes(repository,tmp_path):
    root,git,base=repository
    build=tmp_path/'candidate';build.mkdir()
    q.atomic(build/'build-manifest.json',{'git_head':base,'git_dirty':False})
    archive=tmp_path/'candidate.zip';archive.write_bytes(b'candidate artifact')
    entry={'path':str(archive),'sha256':q.digest(archive),'build':str(build),'build_sha256':q.tree_digest(build)}
    (root/'docs').mkdir();(root/'docs/new.md').write_text('new collection contract')
    git('add','.');git('commit','-qm','documentation only')
    plan={'source_root':str(root),'head_commit':git('rev-parse','HEAD'),'release_input':{'artifacts':{'macos-arm64':entry}}}
    scenario={'platform':'macos-arm64','level':'native'}
    assert q.candidate_source(plan,scenario)==base
    (root/'business.py').write_text('value=2');git('add','.');git('commit','-qm','product changes')
    plan['head_commit']=git('rev-parse','HEAD')
    with pytest.raises(q.Blocked,match='product/build inputs changed'):q.candidate_source(plan,scenario)


def test_audio_adapter_requires_measured_permission_and_rejects_modified_fixture(tmp_path):
    fixture=tmp_path/'fixture';fixture.mkdir()
    for name in ('source.m4a','standard.wav','short.wav','script.txt'):(fixture/name).write_bytes(b'synthetic validator input')
    config={'fixtures':str(fixture),'permission':'self_created','fixture_hashes':{p.name:q.digest(p) for p in fixture.iterdir()}}
    scenario={'runner':{'adapter':'audio_pcm'}}
    assert q.audio_identity(config,scenario)['permission']=='self_created'
    config['permission']='unknown'
    with pytest.raises(q.Gap,match='permission'):q.audio_identity(config,scenario)
    config['permission']='self_created';(fixture/'standard.wav').write_bytes(b'modified')
    with pytest.raises(q.Blocked,match='fixture hash'):q.audio_identity(config,scenario)


def test_audio_model_runtime_cannot_escape_registered_component(tmp_path):
    fixture=tmp_path/'fixture';fixture.mkdir()
    for name in ('source.m4a','standard.wav','short.wav','script.txt'):(fixture/name).write_bytes(b'fixture')
    component=tmp_path/'component';component.mkdir();(component/'model').mkdir();(component/'model/weights').write_bytes(b'model')
    outside=tmp_path/'unrelated-python';outside.write_bytes(b'python')
    config={'fixtures':str(fixture),'permission':'self_created','fixture_hashes':{p.name:q.digest(p) for p in fixture.iterdir()},
        'components':{'macos-arm64':{'root':str(component),'tree_sha256':q.tree_digest(component),'python':str(outside),'model':str(component/'model')}}}
    with pytest.raises(q.Blocked,match='runtime/model'):
        q.audio_identity(config,{'runner':{'adapter':'audio_engine'},'platform':'macos-arm64'})
