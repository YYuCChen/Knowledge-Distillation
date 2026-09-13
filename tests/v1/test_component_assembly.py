import base64
import hashlib
import json
from pathlib import Path
import runpy
import zipfile

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from knowledge_distiller.v1.component_assembly import ComponentAssembly
from knowledge_distiller.v1.docling_component import DoclingComponent
from knowledge_distiller.v1.program_tree import identity
from knowledge_distiller.v1.windows_delta_v2 import build_payload


def test_offline_first_install_and_current_direct_delta_share_plan(tmp_path, monkeypatch):
    manifest = {'files': {'model.bin': {'size': 5, 'sha256': hashlib.sha256(b'model').hexdigest()}}}
    monkeypatch.setattr('knowledge_distiller.v1.docling_component.trusted_manifest', lambda: manifest)
    cache = tmp_path / 'cache'
    cache.mkdir()
    def cache_asset(path, unpacked):
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        (cache / digest).write_bytes(data)
        return {'sha256': digest, 'size': len(data), 'unpacked_size': unpacked,
                'url': 'https://example.com/' + digest}
    programs = []
    for version in ('1', '2'):
        path = tmp_path / ('program-' + version)
        (path / '_internal').mkdir(parents=True)
        (path / 'KnowledgeDistiller.exe').write_bytes(('business-' + version).encode())
        (path / '_internal/windows-version.json').write_text(json.dumps({'version': version}))
        programs.append(path)
    old, target = programs
    base_zip, delta_zip = tmp_path / 'base.zip', tmp_path / 'delta.zip'
    build_payload(old, base_zip)
    build_payload(target, delta_zip, old)
    model_zip = tmp_path / 'model.zip'
    with zipfile.ZipFile(model_zip, 'w') as archive:
        archive.writestr('model.bin', b'model')
    model_id = DoclingComponent(tmp_path / 'components').identity
    release = {'format': 1, 'product': 'knowledge-distiller', 'platform': 'windows-x86_64',
        'minimum_installer': 1, 'source_commit': 'a'*40, 'version': '2', 'python_version': '3.11.16',
        'target_identity': identity(target, 'windows-x86_64'),
        'docling': {**cache_asset(model_zip, 5), 'identity': model_id},
        'base': {**cache_asset(base_zip, 100), 'identity': identity(old, 'windows-x86_64'), 'version': '1'},
        'deltas': [{**cache_asset(delta_zip, 100), 'from_identity': identity(old, 'windows-x86_64'),
                    'to_identity': identity(target, 'windows-x86_64')}]}
    key = ECC.generate(curve='Ed25519')
    raw = json.dumps(release).encode()
    envelope = json.dumps({'payload': base64.b64encode(raw).decode(),
        'signature': base64.b64encode(eddsa.new(key, 'rfc8032').sign(raw)).decode()}).encode()
    assembler = ComponentAssembly(tmp_path / 'components', cache, platform='windows-x86_64',
        public_key=base64.b64encode(key.public_key().export_key(format='raw')).decode())
    verified, plan = assembler.prepare(envelope)
    assert plan.source == 'base' and plan.download_bytes == 0
    candidate, _ = assembler.assemble(verified, plan, tmp_path / 'new')
    assert identity(candidate, 'windows-x86_64') == release['target_identity']
    verified, plan = assembler.prepare(envelope, installed=old, current='1')
    assert plan.source == 'current' and len(plan.assets) == 1
    candidate, _ = assembler.assemble(verified, plan, tmp_path / 'upgrade', installed=old)
    assert identity(candidate, 'windows-x86_64') == release['target_identity']
    assert (old / 'KnowledgeDistiller.exe').read_bytes() == b'business-1'
