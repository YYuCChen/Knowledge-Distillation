import base64
import json
import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from knowledge_distiller.v1.component_release import parse_release, plan_release
from knowledge_distiller.v1.docling_component import DoclingComponent
from knowledge_distiller.v1.updates import UpdateError


def release():
    def asset(digest):
        return {'sha256': digest * 64, 'size': 100, 'unpacked_size': 200,
                'url': 'https://example.com/' + digest}
    return {'format': 1, 'product': 'knowledge-distiller', 'platform': 'windows-x86_64',
        'minimum_installer': 1, 'version': '2026.09.13.10', 'source_commit': 'a' * 40,
        'target_identity': 'c' * 64, 'python_version': '3.11.16',
        'docling': {**asset('d'), 'identity': DoclingComponent('.').identity},
        'base': {**asset('b'), 'identity': 'b' * 64},
        'deltas': [{**asset('e'), 'from_identity': 'b' * 64, 'to_identity': 'c' * 64},
                   {**asset('f'), 'from_identity': 'a' * 64, 'to_identity': 'c' * 64}]}


def signed(payload):
    key = ECC.generate(curve='Ed25519')
    raw = json.dumps(payload).encode()
    wrapper = json.dumps({'payload': base64.b64encode(raw).decode(),
        'signature': base64.b64encode(eddsa.new(key, 'rfc8032').sign(raw)).decode()}).encode()
    return wrapper, base64.b64encode(key.public_key().export_key(format='raw')).decode()


def test_real_signature_and_direct_paths():
    wrapper, key = signed(release())
    data = parse_release(wrapper, key, platform='windows-x86_64')
    new = plan_release(data)
    assert new.source == 'base' and new.download_bytes == 300
    existing = plan_release(data, verified_current_identity='a'*64,
                            verified_model_identity=data['docling']['identity'])
    assert existing.source == 'current' and existing.download_bytes == 100
    assert existing.assets[0]['sha256'] == 'f'*64
    cached = plan_release(data, verified_cached_assets={'d'*64, 'b'*64})
    assert cached.download_bytes == 100


@pytest.mark.parametrize('change', ['platform', 'model', 'base_path', 'downgrade', 'signature'])
def test_invalid_or_incompatible_release_is_rejected(change):
    data = release()
    if change == 'platform': data['platform'] = 'macos-arm64'
    if change == 'model': data['docling']['identity'] = '0'*64
    if change == 'base_path': data['deltas'] = data['deltas'][1:]
    if change == 'downgrade': data['version'] = '1'
    wrapper, key = signed(data)
    if change == 'signature':
        wrapper = wrapper.replace(b'"signature": "', b'"signature": "X', 1)
    with pytest.raises(UpdateError):
        parse_release(wrapper, key, platform='windows-x86_64', current='2026.09.13.9')
