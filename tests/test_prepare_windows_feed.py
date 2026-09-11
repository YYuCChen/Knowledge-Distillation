import base64
import importlib.util
from pathlib import Path
import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

spec=importlib.util.spec_from_file_location('prepare_windows_feed',Path(__file__).resolve().parents[1]/'scripts/prepare_windows_feed.py')
feed=importlib.util.module_from_spec(spec);spec.loader.exec_module(feed)


def test_signed_windows_feed_uses_own_platform_and_exact_baseline(tmp_path):
    key=ECC.generate(curve='Ed25519')
    public=base64.b64encode(key.public_key().export_key(format='raw')).decode()
    full=tmp_path/'Windows-full.zip';full.write_bytes(b'f'*1000)
    delta=tmp_path/'Windows-1-2.delta.zip';delta.write_bytes(b'd'*10)
    def sign(path,*args):
        data=path.read_bytes();signature=base64.b64encode(eddsa.new(key,'rfc8032').sign(data)).decode()
        if path.suffix=='.xml':path.write_bytes(data+f'<!-- sparkle-signatures:\nedSignature: {signature}\nlength: {len(data)}\n-->\n'.encode())
        return signature
    output=tmp_path/'appcast-windows.xml'
    result=feed.prepare(full=full,delta=delta,version='2026.09.11.12',from_version='2026.09.11.11',notes='差量更新',output=output,sdk=tmp_path,account='test',public_key=public,signer=sign)
    assert result['selected']['name']==delta.name and result['selected']['size']==10
    assert b'minimumSystemVersion' not in output.read_bytes()
    assert result['published'] is False
    assert feed.parse_feed(output.read_bytes(),public,'2026.09.11.10')['selected']['name']==full.name
    with pytest.raises(ValueError,match='new appcast'):
        feed.prepare(full=full,delta=delta,version='2026.09.11.12',from_version='2026.09.11.11',notes='',output=output,sdk=tmp_path,account='test',public_key=public,signer=sign)
