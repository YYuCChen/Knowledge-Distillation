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
    assert set(windows['runner']['nodeids']) < set(normal['runner']['deselect'])
    mac=next(s for s in scenarios if s['id']=='SC-BUILD-CHILD--macos-arm64')
    assert set(normal['runner']['deselect']) == set(windows['runner']['nodeids'] + mac['runner']['nodeids'])
    assert mac['platform']=='macos-arm64'
    desktop=next(s for s in scenarios if s['id']=='SC-DESKTOP-REOPEN--synthetic')
    mac_desktop=next(s for s in scenarios if s['id']=='SC-DESKTOP-REOPEN--macos-arm64--synthetic')
    assert 'tests/v1/test_mac_app.py' not in desktop['runner']['paths']
    assert mac_desktop['platform']=='macos-arm64' and mac_desktop['runner']['paths']==['tests/v1/test_mac_app.py']
    launcher_node='tests/v1/test_desktop_pages.py::test_second_launcher_queues_on_owning_server'
    launcher=next(s for s in scenarios if s['id']=='SC-DESKTOP-LAUNCHER--macos-arm64')
    assert desktop['runner']['deselect'].count(launcher_node)==1
    assert launcher['platform']=='macos-arm64' and launcher['runner']['nodeids']==[launcher_node]
    launcher_command=q.command_for(launcher,{'source_root':str(q.ROOT)},Path('/tmp/isolated'))
    assert launcher_node in launcher_command
    assert 'tests/v1/test_mac_app.py' not in launcher_command
    assert '--deselect='+launcher_node in q.command_for(desktop,{'source_root':str(q.ROOT)},Path('/tmp/isolated'))
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
        platform=q.platform.platform(),source_sha256=q.digest(source),browser='Chromium measured version',browser_full_version={'fullVersionList':['fixture']},source_commit='current',source_dirty=False,
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


def test_cross_host_report_checks_measured_execution_not_collector(repository,tmp_path,monkeypatch):
    root,git,base=repository
    scenarios=q.read(root/'quality/scenarios.yaml');scenarios['scenarios'][0]['always']=True
    q.atomic(root/'quality/scenarios.yaml',scenarios);git('add','.');git('commit','-qm','collect')
    directory=tmp_path/'native-host';plan=q.make_plan(base,'HEAD',None,directory,root)
    assert q.run(plan,directory/'plan.json','module',tmp_path/'data')['exit_code']==0
    passport=q.read(directory/'evidence/unit.json');scenario=plan['scenarios'][0]
    # The evidence was executed for real above. Simulate the receiving collector OS only.
    foreign_platform='macos-arm64' if passport['platform']=='windows-x64' else 'windows-x64'
    assert foreign_platform != passport['platform']
    monkeypatch.setattr(q,'host_platform',lambda:foreign_platform)
    monkeypatch.setattr(q.platform,'platform',lambda:'foreign-collector-os')
    assert q.verify_passport(passport,scenario,plan,directory)
    other=dict(scenario,platform=foreign_platform)
    with pytest.raises(q.Blocked,match='platform mismatch'):q.verify_passport(passport,other,plan,directory)
    environment_record=next(r for r in passport['outputs'] if r['path'].endswith('execution-environment.json'))
    environment_path=directory/environment_record['path'];measurement=q.read(environment_path)
    measurement['python']='3.11.15';q.atomic(environment_path,measurement)
    environment_record['sha256']=q.digest(environment_path);seal(passport)
    with pytest.raises(q.Blocked,match='environment mismatch'):q.verify_passport(passport,scenario,plan,directory)


def test_portable_export_preserves_passport_and_rejects_missing_or_changed_output(repository,tmp_path):
    root,git,base=repository
    scenarios=q.read(root/'quality/scenarios.yaml');scenarios['scenarios'][0]['always']=True
    q.atomic(root/'quality/scenarios.yaml',scenarios);git('add','.');git('commit','-qm','collect')
    directory=tmp_path/'plan';plan=q.make_plan(base,'HEAD',None,directory,root)
    assert q.run(plan,directory/'plan.json','module',tmp_path/'data')['exit_code']==0
    original=(directory/'evidence/unit.json').read_bytes()
    bundle=tmp_path/'portable';assert q.export_evidence(directory/'plan.json',bundle)['exit_code']==0
    assert (bundle/'evidence/unit.json').read_bytes()==original
    portable=q.load_portable_plan(bundle/'plan.json',root)
    assert q.inspect(portable,bundle/'plan.json','module')['exit_code']==0
    (bundle/'evidence/unit.json').write_text('{}')
    with pytest.raises(q.Blocked,match='portable file changed'):q.load_portable_plan(bundle/'plan.json',root)


def test_artifact_closure_reuses_only_independent_bootstrap_and_binds_shared_inputs(repository,tmp_path):
    root,git,_=repository
    impact=q.read(root/'quality/change-impact.yaml')
    impact['components']['shared']={'owner':'D','paths':['platform_bridge.py'],'depends_on':[],'artifact':False}
    impact['components']['installer']={'owner':'D','paths':['bootstrap.py'],'depends_on':['shared'],'artifact':True}
    impact['components']['main']['depends_on'].append('shared')
    impact['artifact_inputs']={'main':{'components':['main'],'paths':[]}}
    q.atomic(root/'quality/change-impact.yaml',impact)
    (root/'bootstrap.py').write_text('picker_timeout=1\n');(root/'platform_bridge.py').write_text('shortcut=True\n')
    git('add','.');git('commit','-qm','artifact graph');base=git('rev-parse','HEAD')
    build=tmp_path/'build';build.mkdir();q.atomic(build/'build-manifest.json',{'git_head':base,'git_dirty':False})
    artifact=tmp_path/'app.zip';artifact.write_bytes(b'old immutable main')
    entry={'path':str(artifact),'sha256':q.digest(artifact),'build':str(build),'build_sha256':q.tree_digest(build)}
    (root/'bootstrap.py').write_text('picker_timeout=2\n');git('add','.');git('commit','-qm','bootstrap only')
    plan=q.make_plan(base,'HEAD',None,tmp_path/'plan',root)
    assert plan['rebuild_components']==['installer']
    plan['release_input']={'artifacts':{'macos-arm64':entry}}
    scenario={'platform':'macos-arm64','level':'native'}
    assert q.candidate_source(plan,scenario)==base
    proof=q.candidate_source(plan,scenario,proof=True)
    assert proof['method']=='unchanged_artifact_input_closure' and proof['independent_changed_paths']==['bootstrap.py']
    assert 'platform_bridge.py' in proof['input_files'] and 'bootstrap.py' not in proof['input_files']
    (root/'platform_bridge.py').write_text('shortcut=False\n');git('add','.');git('commit','-qm','shared changes')
    shared=q.make_plan(base,'HEAD',None,tmp_path/'shared-plan',root)
    assert shared['rebuild_components']==['installer','main']
    plan['head_commit']=git('rev-parse','HEAD')
    with pytest.raises(q.Blocked,match='product/build inputs changed'):q.candidate_source(plan,scenario)
    # Removing the shared dependency from today's graph must not erase historical inputs.
    impact['components']['main']['depends_on'].remove('shared')
    q.atomic(root/'quality/change-impact.yaml',impact);git('add','.');git('commit','-qm','remove dependency')
    plan['head_commit']=git('rev-parse','HEAD')
    with pytest.raises(q.Blocked,match='product/build inputs changed'):q.candidate_source(plan,scenario)


def test_real_main_mapping_includes_shared_module_and_excludes_bootstrap():
    _,_,impact,_=q.registry()
    assert 'installer_shared' in impact['components']['main']['depends_on']
    assert 'src/knowledge_distiller/v1/installer_platform.py' in impact['components']['installer_shared']['paths']
    assert 'src/knowledge_distiller/v1/component_bootstrap.py' in impact['components']['installer']['paths']
    # installer HTML/SVG are also copied by application_datas and cannot be installer-only.
    assert 'src/knowledge_distiller/v1/installer_assets/page.html' in impact['components']['installer_shared']['paths']


def test_candidate_rejects_nonancestor_source_even_with_matching_artifact(repository,tmp_path):
    root,git,base=repository
    git('checkout','-qb','other');(root/'main.py').write_text('other branch\n')
    git('add','.');git('commit','-qm','other source');other=git('rev-parse','HEAD');git('checkout','-q','-')
    build=tmp_path/'build';build.mkdir();q.atomic(build/'build-manifest.json',{'git_head':other,'git_dirty':False})
    archive=tmp_path/'app.zip';archive.write_bytes(b'immutable')
    plan={'source_root':str(root),'head_commit':base,'release_input':{'artifacts':{'macos-arm64':{
        'path':str(archive),'sha256':q.digest(archive),'build':str(build),'build_sha256':q.tree_digest(build)}}}}
    with pytest.raises(q.Blocked,match='not an ancestor'):
        q.candidate_source(plan,{'platform':'macos-arm64','level':'native'})


def test_windows_audio_paths_are_replayed_without_accessing_original_drive(tmp_path):
    import wave
    root=q.ROOT;fixture=tmp_path/'fixtures';fixture.mkdir();folder=tmp_path/'verification';folder.mkdir()
    standard=b'\0\0'*(312*16000);short=standard[:24*32000]
    def wav(path,pcm):
        path.parent.mkdir(parents=True,exist_ok=True)
        with wave.open(str(path),'wb') as out:out.setparams((1,2,16000,0,'NONE','not compressed'));out.writeframes(pcm)
    wav(fixture/'standard.wav',standard);wav(fixture/'short.wav',short)
    wav(folder/'long/asr-segments/000/audio.wav',standard)
    wav(folder/'locations/location-recovery/000/audio.wav',short)
    wav(folder/'recovery/asr-recovery/000/audio.wav',short)
    wav(folder/'calls/001/input.wav',short)
    q.atomic(folder/'summary.json',dict(long_success=True,location_text_unchanged=True,location_pcm_equal=True,
        recovery_pcm_equal=True,recovery_success=True,actual_worker_calls=1))
    version=q.read(root/'src/knowledge_distiller/v1/adapters/python-runtime.json')['version']
    q.atomic(folder/'runtime.json',dict(driver_python=version+' actual fixture',component_python=version+' actual fixture',
        worker_sha256=q.digest(root/'src/knowledge_distiller/v1/adapters/qwen_windows_worker.py')))
    q.atomic(folder/'probe-binding.json',{'output_root':r'C:\isolated\attempt\verification'})
    measured=dict(source=r'C:\isolated\attempt\verification\calls\001\input.wav',frames=len(short)//2,
        pcm_sha256=q.hashlib.sha256(short).hexdigest(),returncode=0)
    q.atomic(folder/'calls/001/input.json',measured);q.atomic(folder/'calls/001/result.json',{'text':'validator fixture only'})
    (folder/'calls/001/stderr.txt').write_text('')
    scenario={'id':'engine','platform':'windows-x64','runner':{'adapter':'audio_engine'}}
    identity={'fixture':'measured'}
    plan={'source_root':str(root),'release_input':{'audio_input':{'fixtures':str(fixture)}},
        'snapshots':{'engine':{'audio_inputs':identity}},'_portable_audio':{'engine':identity},
        '_original_audio_fixture':r'C:\isolated\fixtures'}
    assert q.validate_audio(folder,plan,scenario)==('passed',None)
    measured['source']=r'C:\outside\input.wav';q.atomic(folder/'calls/001/input.json',measured)
    assert q.validate_audio(folder,plan,scenario)==('failed','audio_call_source_outside_probe')


def test_peer_bundle_fills_exact_foreign_rows_without_bypassing_s1(repository,tmp_path):
    root,git,base=repository
    scenarios=q.read(root/'quality/scenarios.yaml');scenario=scenarios['scenarios'][0]
    scenario.update(always=True,platform=q.host_platform(),level='integration',gate='contract')
    q.atomic(root/'quality/scenarios.yaml',scenarios);git('add','.');git('commit','-qm','native host contract fixture')
    directory=tmp_path/'peer-plan';peer=q.make_plan(base,'HEAD',None,directory,root)
    assert q.run(peer,directory/'plan.json','contract',tmp_path/'data')['exit_code']==0
    bundle=tmp_path/'bundle';q.export_evidence(directory/'plan.json',bundle)
    central=dict(peer,execution_platform='different-collector',gaps={'unit':['not executable on collector']})
    result=q.inspect_with_peers(central,tmp_path/'central/plan.json','contract',[bundle])
    assert result['exit_code']==0 and result['scenarios'][0]['peer_plan_id']==peer['change_id']
    central['new_defects']=[{'id':'live-failure','severity':'S1','status':'open'}]
    result=q.inspect_with_peers(central,tmp_path/'central/plan.json','candidate',[bundle])
    assert result['exit_code']==1 and 'S0/S1' in result['reason']


@pytest.fixture
def docling_inputs(tmp_path):
    root=tmp_path.resolve()/'source';models=tmp_path.resolve()/'models';models.mkdir()
    (models/'weight.bin').write_bytes(b'test-only weights')
    manifest={'docling_version':'2.126.0','files':{'weight.bin':{'size':17,'sha256':q.digest(models/'weight.bin')}}}
    q.atomic(root/'src/knowledge_distiller/v1/adapters/docling-models-manifest.json',manifest)
    q.atomic(models/'manifest.json',manifest)
    source=root/'src/knowledge_distiller/v1/docling_source.py';source.write_text('DOCLING_VERSION = "2.126.0"\n')
    identity=q.hashlib.sha256(q.json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    config={'windows-x64':{'models_root':str(models),'component_identity':identity,'tree_sha256':q.tree_digest(models)}}
    scenario={'id':'pdf','platform':'windows-x64','runner':{'adapter':'pytest','docling':'pdf'}}
    return root,models,config,scenario


def test_docling_requires_explicit_trusted_component_and_rejects_changed_bytes(docling_inputs):
    root,models,config,scenario=docling_inputs
    expected=q.docling_identity(config,scenario,root)
    assert expected['model_required'] and 'models_root' not in expected
    with pytest.raises(q.Gap,match='missing explicit'):q.docling_identity({},scenario,root)
    config['windows-x64']['component_identity']='0'*64
    with pytest.raises(q.Blocked,match='trusted identity'):q.docling_identity(config,scenario,root)
    config['windows-x64']['component_identity']=expected['component_identity']
    (models/'weight.bin').write_bytes(b'tampered weights!')
    config['windows-x64']['tree_sha256']=q.tree_digest(models)
    with pytest.raises(q.Blocked,match='model bytes differ'):q.docling_identity(config,scenario,root)


def test_docling_missing_file_is_gap_and_epub_does_not_need_weights(docling_inputs):
    root,models,config,scenario=docling_inputs
    (models/'weight.bin').unlink()
    with pytest.raises(q.Gap,match='missing Docling'):q.docling_identity(config,scenario,root)
    scenario['runner']['docling']='epub'
    assert q.docling_identity({},scenario,root)=={'kind':'epub','model_required':False}


def test_docling_environment_uses_only_explicit_models_and_isolated_cache(docling_inputs,tmp_path):
    root,models,config,scenario=docling_inputs
    env={'HF_HOME':'untrusted-cache','KNOWLEDGE_DISTILLER_DOCLING_MODELS':'untrusted-models','HF_HUB_OFFLINE':'0'}
    attempt=tmp_path/'attempt'
    q.docling_environment(env,scenario,{'release_input':{'docling_input':config}},attempt)
    assert env['KNOWLEDGE_DISTILLER_DOCLING_MODELS']==str(models)
    assert env['HF_HUB_OFFLINE']==env['TRANSFORMERS_OFFLINE']=='1'
    assert Path(env['HF_HOME']).is_relative_to(attempt)
    scenario['runner']['docling']='epub'
    q.docling_environment(env,scenario,{'release_input':{}},attempt)
    assert 'KNOWLEDGE_DISTILLER_DOCLING_MODELS' not in env


def test_docling_portable_validation_does_not_open_foreign_model_path(docling_inputs,tmp_path):
    root,models,config,scenario=docling_inputs
    identity=q.docling_identity(config,scenario,root)
    config['windows-x64']['models_root']='C:/absent-on-receiver/models'
    plan={'source_root':str(root),'release_input':{'docling_input':config},'snapshots':{'pdf':{'docling_inputs':identity}},
          '_portable_docling':{'pdf':identity}}
    folder=tmp_path/'logs';folder.mkdir()
    packages={k:'recorded-version' for k in ('docling-core','docling-ibm-models','torch','onnxruntime','rapidocr')}
    packages['docling']='2.126.0'
    runtime={'inputs':identity,'packages':packages,'models_root':r'C:\absent-on-receiver\models',
             'offline':{'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}}
    q.atomic(folder/'execution-docling.json',runtime)
    assert q.validate_docling(folder,plan,scenario)==('passed',None)
    runtime['models_root']=r'C:\different\models';q.atomic(folder/'execution-docling.json',runtime)
    assert q.validate_docling(folder,plan,scenario)==('failed','docling_model_environment_mismatch')
    runtime['models_root']=r'C:\absent-on-receiver\models';runtime['offline']['HF_HUB_OFFLINE']='0'
    q.atomic(folder/'execution-docling.json',runtime)
    assert q.validate_docling(folder,plan,scenario)==('failed','docling_offline_environment_missing')


def test_docling_true_nodes_are_removed_from_host_and_covered_on_each_platform():
    _,scenarios,_,_=q.registry()
    host=next(s for s in scenarios if s['id']=='SC-MANUAL-FIFO--synthetic')
    for kind in ('pdf','epub'):
        node='tests/v1/test_submitted_sources.py::test_document_uses_same_durable_worker_and_locator_without_audio['+kind+']'
        assert host['runner']['deselect'].count(node)==1
        for plat in ('macos-arm64','windows-x64'):
            scenario=next(s for s in scenarios if s['id']==f'SC-DOCUMENT-WORKER--{kind}--{plat}')
            assert scenario['runner']['nodeids']==[node] and scenario['level']=='integration'
            assert scenario['runner']['docling']==kind and not scenario.get('enabled_by')
            command=q.command_for(scenario,{'source_root':str(q.ROOT)},Path('/tmp/fixture'))
            assert node in command and 'tests/v1/test_document_sources.py' not in command


def test_docling_export_rechecks_identity_and_portable_report_needs_no_model_tree(repository,docling_inputs,tmp_path,monkeypatch):
    """Adapter-only fixture: package probe is mocked; this does not prove model conversion."""
    root,git,base=repository
    source,models,config,scenario=docling_inputs
    platform_id=q.host_platform()
    config={platform_id:config['windows-x64']}
    paths=['tests/v1/test_submitted_sources.py','tests/v1/test_document_sources.py']
    q.atomic(root/'src/knowledge_distiller/v1/adapters/docling-models-manifest.json',q.read(source/'src/knowledge_distiller/v1/adapters/docling-models-manifest.json'))
    (root/'src/knowledge_distiller/v1/docling_source.py').write_text('DOCLING_VERSION = "2.126.0"\n')
    (root/'tests/v1').mkdir()
    (root/paths[0]).write_text('import pytest\n@pytest.mark.parametrize("kind",["pdf"])\ndef test_document_uses_same_durable_worker_and_locator_without_audio(kind): assert kind == "pdf"\n')
    (root/paths[1]).write_text('# helper fixture only\n')
    registry=q.read(root/'quality/scenarios.yaml');s=registry['scenarios'][0]
    s.update(always=True,level='integration',gate='contract',platform=platform_id)
    s['runner']={'adapter':'pytest','paths':paths,'nodeids':[paths[0]+'::test_document_uses_same_durable_worker_and_locator_without_audio[pdf]'],'docling':'pdf'}
    q.atomic(root/'quality/scenarios.yaml',registry)
    impact=q.read(root/'quality/change-impact.yaml');impact['components']['business']['paths']+=paths+['src/**']
    q.atomic(root/'quality/change-impact.yaml',impact);git('add','.');git('commit','-qm','synthetic adapter contract')
    release=tmp_path/'release.json';q.atomic(release,{'docling_input':config})
    directory=tmp_path/'plan';plan=q.make_plan(base,'HEAD',release,directory,root)
    original=q.subprocess.check_output
    def probe(command,**kwargs):
        if command[:2]==[q.sys.executable,'-c'] and 'importlib.metadata' in command[-1]:
            packages={key:'fixture-version' for key in ('docling-core','docling-ibm-models','torch','onnxruntime','rapidocr')};packages['docling']='2.126.0'
            return q.json.dumps({'packages':packages,'models_root':str(models),'offline':{'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}})
        return original(command,**kwargs)
    monkeypatch.setattr(q.subprocess,'check_output',probe)
    assert q.run(plan,directory/'plan.json','contract',tmp_path/'data')['exit_code']==0
    bundle=tmp_path/'bundle';q.export_evidence(directory/'plan.json',bundle)
    assert not (bundle/'weight.bin').exists()
    q.shutil.rmtree(models)
    portable=q.load_portable_plan(bundle/'plan.json',root)
    assert q.inspect(portable,bundle/'plan.json','contract')['exit_code']==0
    # Re-exporting from the execution host must not borrow the portable receipt when inputs vanished.
    with pytest.raises(q.Gap,match='missing explicit Docling'):q.export_evidence(directory/'plan.json',tmp_path/'bad-export')
    output=next(bundle.glob('logs/*/execution-docling.json'));output.write_text('{}')
    with pytest.raises(q.Blocked,match='portable file changed'):q.load_portable_plan(bundle/'plan.json',root)
