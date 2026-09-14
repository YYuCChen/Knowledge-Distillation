"""K09 ownership is an in-memory capability, never a UUID/path convention."""
import os
from pathlib import Path
import pytest

from knowledge_distiller.v1.component_attempt import create_attempt, bind_candidate, cleanup_attempt


def owned(tmp_path):
    root=tmp_path/'attempts'/'one'
    cap=create_attempt(root, excluded=[tmp_path/'cache', tmp_path/'data'])
    candidate=root/'candidate'; candidate.mkdir(); (candidate/'payload').write_bytes(b'owned')
    bind_candidate(cap,candidate,'verified-tree')
    outcome={'accepted':True,'target_identity':'verified-tree','activation':{'status':'ready'}}
    return cap,root,outcome


def test_exclusive_creation_and_ordinary_cleanup_preserve_other_material(tmp_path):
    cap,root,outcome=owned(tmp_path)
    (tmp_path/'cache').mkdir(); (tmp_path/'cache'/'keep').write_bytes(b'cache')
    other=root.parent/'other'; other.mkdir(); (other/'keep').write_bytes(b'other')
    with pytest.raises(FileExistsError): create_attempt(root,excluded=[])
    result=cleanup_attempt(cap,outcome)
    assert result['status']=='clean' and result['deleted_logical_bytes']==5
    assert not root.exists()
    assert cleanup_attempt(cap,outcome)['status']=='already_clean'
    assert (tmp_path/'cache'/'keep').read_bytes()==b'cache'
    assert (other/'keep').read_bytes()==b'other'


@pytest.mark.parametrize('change',['unaccepted','identity','activation','no_capability'])
def test_missing_authority_preserves_attempt(tmp_path,change):
    cap,root,outcome=owned(tmp_path)
    if change=='unaccepted': outcome['accepted']=False
    if change=='identity': outcome['target_identity']='different'
    if change=='activation': outcome['activation']['status']='pending'
    if change=='no_capability': cap=None
    assert cleanup_attempt(cap,outcome)['status']=='pending'
    assert (root/'candidate'/'payload').read_bytes()==b'owned'


@pytest.mark.skipif(os.name=='nt',reason='native Windows handles prevent rename; covered separately')
@pytest.mark.parametrize('which',['root','parent'])
def test_path_replacement_never_deletes_replacement_or_moved_material(tmp_path,which):
    cap,root,outcome=owned(tmp_path)
    source=root if which=='root' else root.parent
    moved=source.with_name(source.name+'-moved'); source.rename(moved)
    root.mkdir(parents=True); (root/'outside').write_bytes(b'outside')
    assert cleanup_attempt(cap,outcome)['status']=='pending'
    assert (root/'outside').read_bytes()==b'outside'
    assert (moved/('candidate/payload' if which=='root' else 'one/candidate/payload')).read_bytes()==b'owned'


@pytest.mark.skipif(os.name=='nt',reason='native junction test separate')
def test_legitimate_bundle_symlinks_remove_only_entries(tmp_path):
    cap,root,outcome=owned(tmp_path)
    external=tmp_path/'external';external.mkdir();(external/'keep').write_bytes(b'keep')
    (root/'candidate'/'link').symlink_to(external,target_is_directory=True)
    assert cleanup_attempt(cap,outcome)['status']=='clean'
    assert (external/'keep').read_bytes()==b'keep'


def test_overlap_refused_before_creation(tmp_path):
    with pytest.raises(ValueError):create_attempt(tmp_path/'data'/'attempt',excluded=[tmp_path/'data'])
    assert not (tmp_path/'data'/'attempt').exists()
