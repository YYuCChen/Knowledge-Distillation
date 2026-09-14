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
