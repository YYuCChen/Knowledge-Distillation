import json
from pathlib import Path
import zipfile
import pytest
from knowledge_distiller.v1.windows_delta import build_payload,stage_payload,inventory,safe_name
from knowledge_distiller.v1.updates import UpdateError


def app(root,version):
    (root/'_internal').mkdir(parents=True)
    (root/'_internal/windows-version.json').write_text(json.dumps({'version':version,'product_version':'1.1'}))
    (root/'KnowledgeDistiller.exe').write_bytes(b'compiled-'+version.encode())
    (root/'_internal/unchanged-model').write_bytes(b'model'*10000)
    return root


def test_changed_files_reconstruct_exact_release_and_remove_old(tmp_path):
    old=app(tmp_path/'old','1'); new=app(tmp_path/'new','2')
    (old/'removed').write_text('old'); (new/'added').write_text('new')
    payload=tmp_path/'delta.zip'; result=build_payload(new,payload,old)
    assert result['changed_files']==3
    with zipfile.ZipFile(payload) as archive:
        assert 'files/_internal/unchanged-model' not in archive.namelist()
    stage_payload(payload,old,tmp_path/'stage',version='2',current='1')
    assert inventory(tmp_path/'stage')==inventory(new)
    assert (old/'removed').read_text()=='old'


def test_mismatched_base_preserves_installed_app(tmp_path):
    old=app(tmp_path/'old','1');new=app(tmp_path/'new','2')
    payload=tmp_path/'delta.zip';build_payload(new,payload,old)
    (old/'KnowledgeDistiller.exe').write_text('modified')
    before=inventory(old)
    with pytest.raises(UpdateError,match='基线'):
        stage_payload(payload,old,tmp_path/'stage',version='2',current='1')
    assert inventory(old)==before and not (tmp_path/'stage').exists()


@pytest.mark.parametrize('name',['../x','/tmp/x','a/../../x','a\\b','C:/x','a:stream','a/NUL.txt','a/CON','a./x','a//b'])
def test_rejects_paths(name):
    with pytest.raises(UpdateError):safe_name(name)


def test_corrupted_payload_cannot_replace_or_leave_stage(tmp_path):
    old=app(tmp_path/'old','1');new=app(tmp_path/'new','2')
    payload=tmp_path/'delta.zip';build_payload(new,payload,old)
    bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(payload) as source,zipfile.ZipFile(bad,'w') as target:
        for name in source.namelist():
            target.writestr(name,b'corrupt' if name=='files/KnowledgeDistiller.exe' else source.read(name))
    with pytest.raises(UpdateError):stage_payload(bad,old,tmp_path/'stage',version='2',current='1')
    assert not (tmp_path/'stage').exists()
    assert (old/'KnowledgeDistiller.exe').read_bytes()==b'compiled-1'


def test_signed_full_release_format_is_stageable(tmp_path):
    target=app(tmp_path/'new','2');payload=tmp_path/'full.zip'
    with zipfile.ZipFile(payload,'w') as z:
        for f in target.rglob('*'):
            if f.is_file():z.write(f,'知识蒸馏器/'+f.relative_to(target).as_posix())
    stage_payload(payload,tmp_path/'absent',tmp_path/'stage',version='2',current='1')
    assert inventory(tmp_path/'stage')==inventory(target)


def test_new_windows_metadata_enables_signed_feed(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from knowledge_distiller.v1 import updates
    monkeypatch.setattr(updates,'sys',SimpleNamespace(platform='win32',frozen=True,_MEIPASS=str(tmp_path),executable=str(tmp_path/'KnowledgeDistiller.exe')))
    (tmp_path/'windows-version.json').write_text(json.dumps({'version':'2026.09.11.11','product_version':'1.1','feed_url':'https://example.com/appcast-windows.xml','public_key':'public'}))
    info=updates.bundle_info()
    assert not info['manual_update_only'] and info['bundle']==str(tmp_path)
    assert info['download_url']=='' and info['windows_update']


def test_mmap_package_signature_tamper(tmp_path):
    cryptography=pytest.importorskip('cryptography')
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from knowledge_distiller.v1.updates import Updates
    key=Ed25519PrivateKey.generate();data=b'large payload'*100000
    public=base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
    asset={'size':len(data),'signature':base64.b64encode(key.sign(data)).decode()}
    service=Updates(tmp_path,info={'version':'1','public_key':public,'bundle':None,'windows_update':True})
    file=tmp_path/'package';file.write_bytes(data)
    service.verify_file(file,asset)
    file.write_bytes(b'x'+data[1:])
    with pytest.raises(UpdateError,match='签名'):service.verify_file(file,asset)
