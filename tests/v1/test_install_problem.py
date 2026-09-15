import hashlib
import json
from types import SimpleNamespace
import httpx
import pytest
from knowledge_distiller.v1.component_assembly import ComponentAssembly
from knowledge_distiller.v1.component_download import ComponentDownloader
from knowledge_distiller.v1.install_problem import InstallProblem, problem_from
from knowledge_distiller.v1.updates import UpdateError


@pytest.mark.parametrize('role',['base','docling','delta'])
@pytest.mark.parametrize('fault',[404,401,403,503,'timeout','hash','size','resume'])
def test_actual_asset_failure_preserves_role_and_other_cache(tmp_path,role,fault):
    data=b'expected';digest=hashlib.sha256(data).hexdigest()
    asset={'url':'https://example.com/asset.zip?private=secret','sha256':digest,'size':len(data),'unpacked_size':100}
    other={**asset,'sha256':'f'*64}
    release={'base':asset if role=='base' else other,'docling':asset if role=='docling' else other}
    cache=tmp_path/'cache';cache.mkdir();(cache/'keep').write_bytes(b'good-cache')
    if fault=='resume':
        (cache/(digest+'.part')).write_bytes(b'ex')
        (cache/(digest+'.json')).write_text(json.dumps({'asset':asset,'etag':'"one"'}))
    def respond(request):
        if fault=='timeout':raise httpx.ReadTimeout('timed out https://user:pass@example.com/asset?token=secret',request=request)
        if fault=='resume':return httpx.Response(206,headers={'ETag':'"different"','Content-Range':'bytes 2-7/8'},content=b'pected')
        if isinstance(fault,int):return httpx.Response(fault)
        return httpx.Response(200,content=b'x'*(len(data)+(1 if fault=='size' else 0)))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assembly=ComponentAssembly(tmp_path/'components',cache,platform='windows-x86_64',public_key='unused',
            downloader=ComponentDownloader(cache,client=client))
        with pytest.raises(InstallProblem) as caught:
            assembly.assemble(release,SimpleNamespace(assets=[asset]),tmp_path/'attempt')
    problem=caught.value
    assert problem.resource_role==role and problem.stage=='prepare'
    expected={404:'not_found',401:'auth',403:'auth',503:'server','timeout':'network','hash':'hash','size':'size','resume':'resume_identity'}
    assert problem.category==expected[fault]
    assert 'secret' not in json.dumps(problem.to_dict()) and 'pass@' not in problem.detail_ref
    assert (cache/'keep').read_bytes()==b'good-cache'
    assert not (cache/digest).exists()


@pytest.mark.parametrize('message,category',[('发行清单无效。','schema'),('发行协议不兼容。','protocol'),
    ('发行签名无效。','signature'),('最终程序内容校验失败。','tree'),('最终程序版本不符。','version')])
def test_verification_failures_keep_category_and_accepted_data_state(message,category):
    error=problem_from(UpdateError(message),stage='verify',role='candidate',accepted=True,data_state='accepted')
    assert error.category==category
    assert '安装已接受' in str(error) and '程序未被替换' not in str(error)
