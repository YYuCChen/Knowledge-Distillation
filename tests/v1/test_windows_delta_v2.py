"""Format/security tests plus real native codec round-trip on synthetic files.

Native EXE business-build acceptance is a separate release gate (W05), not
claimed by these byte fixtures.
"""
import json
import os
from pathlib import Path
import random
import zipfile

import pytest

from knowledge_distiller.v1.windows_delta import build_payload,stage_payload,inventory,MANIFEST
from knowledge_distiller.v1.updates import UpdateError


def tree(root, version, data=b'fake-exe'):
    (root/'_internal').mkdir(parents=True)
    (root/'KnowledgeDistiller.exe').write_bytes(data)
    (root/'_internal/windows-version.json').write_text(json.dumps({'version':version}))
    (root/'_internal/reuse.dll').write_bytes(b'stable dependency')
    return root


def rewrite(source,target,change):
    with zipfile.ZipFile(source) as archive:
        data={n:archive.read(n) for n in archive.namelist()}
    manifest=json.loads(data[MANIFEST]);change(manifest,data)
    data[MANIFEST]=json.dumps(manifest).encode()
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,value in data.items():archive.writestr(name,value)
    return target


def test_full_reuse_add_delete_and_legacy_read(tmp_path):
    old=tree(tmp_path/'old','1');new=tree(tmp_path/'new','2',b'new-exe')
    (old/'removed').write_bytes(b'retired')
    (new/'added').write_bytes(b'new')
    for protocol in (1,2):
        archive=tmp_path/f'{protocol}.zip'
        build_payload(new,archive,old,format_version=protocol)
        stage=tmp_path/f'stage-{protocol}'
        stage_payload(archive,old,stage,version='2',current='1')
        assert inventory(stage)==inventory(new)
        assert (old/'removed').read_bytes()==b'retired'


@pytest.mark.parametrize('mutation', ['platform','baseline','escape','case','size','updater','operation'])
def test_invalid_manifest_never_changes_old_or_leaves_stage(tmp_path,mutation):
    old=tree(tmp_path/'old','1');new=tree(tmp_path/'new','2')
    original=inventory(old)
    archive=tmp_path/'valid.zip';build_payload(new,archive,old,format_version=2)
    def corrupt(m,d):
        if mutation=='platform':m['platform']='macos-arm64'
        elif mutation=='baseline':m['base_manifest_sha256']='0'*64
        elif mutation=='escape':m['files']['../escape']=m['files']['KnowledgeDistiller.exe']
        elif mutation=='case':m['files']['KNOWLEDGEDISTILLER.exe']=m['files']['KnowledgeDistiller.exe']
        elif mutation=='size':m['files']['KnowledgeDistiller.exe']['size']=2**50
        elif mutation=='updater':m['minimum_updater']=999
        elif mutation=='operation':m['operations']['KnowledgeDistiller.exe']['kind']='unknown'
    bad=rewrite(archive,tmp_path/'bad.zip',corrupt)
    with pytest.raises(UpdateError):stage_payload(bad,old,tmp_path/'stage',version='2',current='1')
    assert inventory(old)==original and not (tmp_path/'stage').exists()


def test_native_patch_and_corruption(tmp_path):
    tools=os.environ.get('KD_TEST_HDIFFPATCH')
    if not tools:pytest.skip('Explicit native HDiffPatch test tool directory required')
    data=random.Random(741).randbytes(2*1024**2)
    old=tree(tmp_path/'old','1',data)
    new=tree(tmp_path/'new','2',data[:900000]+b'changed business code'+data[900000:])
    original=inventory(old)
    archive=tmp_path/'patch.zip'
    report=build_payload(new,archive,old,format_version=2,tools_dir=Path(tools))
    operation=report['operations']['KnowledgeDistiller.exe']
    assert operation['kind']=='patch'
    assert operation['patch_encoded_size'] < operation['full_encoded_size']/10
    stage_payload(archive,old,tmp_path/'stage',version='2',current='1',tools_dir=tools)
    assert inventory(tmp_path/'stage')==inventory(new)
    for mode in ('algorithm','patch','old'):
        def corrupt(m,d):
            op=m['operations']['KnowledgeDistiller.exe']
            if mode=='algorithm':op['algorithm']='unknown'
            elif mode=='old':op['old']={**op['old'],'sha256':'0'*64}
            else:
                payload=bytearray(d[op['asset']]);payload[-1]^=1;d[op['asset']]=bytes(payload)
        bad=rewrite(archive,tmp_path/f'bad-{mode}.zip',corrupt)
        stage=tmp_path/f'stage-{mode}'
        with pytest.raises(UpdateError):stage_payload(bad,old,stage,version='2',current='1',tools_dir=tools)
        assert not stage.exists() and inventory(old)==original
