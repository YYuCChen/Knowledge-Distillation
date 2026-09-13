"""Verify a newly built candidate with disposable data, never the installed app."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time
import urllib.request
import runpy
PYTHON_VERSION=runpy.run_path(str(Path(__file__).resolve().parents[1]/"src/knowledge_distiller/v1/adapters/python_policy.py"))["PYTHON_VERSION"]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--platform',choices=['mac','windows'],required=True)
    p.add_argument('--build',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--version',required=True)
    p.add_argument('--commit',required=True)
    args=p.parse_args()
    build=args.build.resolve();output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    report={'ok':False,'platform':args.platform,'version':args.version,'source_commit':args.commit,'disposable_data':True}
    try:
        manifest=json.loads((build/'build-manifest.json').read_text(encoding='utf-8'))
        assert manifest['python'] == PYTHON_VERSION and manifest['python_inventory']
        assert manifest['git_head']==args.commit and manifest['version']==args.version
        assert manifest['status'] in {'built','built-not-yet-accepted'}
        assert not manifest.get('changed_during_build') and not manifest.get('git_dirty')
        if args.platform=='mac':
            app=build/'知识蒸馏器.app';exe=app/'Contents/MacOS/KnowledgeDistiller'
            info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            assert info['CFBundleVersion']==args.version
            assert info.get('KDManualUpdateOnly') is False, '正式候选必须支持差量安装'
            assert info.get('SURequireSignedFeed') and info.get('SUPublicEDKey') and info.get('SUFeedURL')
            assert info.get('KDCodeSigningMode')=='local-certificate'
            assert not info.get('KDUpdateTestDataRoot')
            subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
            report['signature']='deep strict; local-certificate'
        else:
            app=build/'KnowledgeDistiller';exe=app/'KnowledgeDistiller.exe'
            info=json.loads((app/'_internal/windows-version.json').read_text(encoding='utf-8'))
            assert info['version']==args.version and info['source_commit']==args.commit
            assert info.get('feed_url') and info.get('public_key'), 'Windows正式候选必须配置签名更新源'
            assert (app/'update-helper.exe').is_file(), 'Windows正式候选缺少更新安装器'
        env={k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','CONDA','VIRTUAL_ENV','KNOWLEDGE_DISTILLER'))}
        env.update(HF_HOME=str(output/'empty-hf'),HF_HUB_OFFLINE='1',PADDLE_PDX_CACHE_HOME=str(output/'empty-paddle'))
        if args.platform=='windows':env.update(PATH=str(Path(os.environ['SystemRoot'])/'System32'),LOCALAPPDATA=str(output/'LocalAppData'))
        else:env['PATH']='/usr/bin:/bin:/usr/sbin:/sbin'
        helper = app/('Contents/MacOS/update-helper' if args.platform == 'mac' else 'update-helper.exe')
        subprocess.run([str(helper), '--runtime-report', str(output/'helper-runtime.json')], env=env, cwd=output, check=True, timeout=60)
        helper_runtime=json.loads((output/'helper-runtime.json').read_text(encoding='utf-8'))
        assert helper_runtime['frozen'] and helper_runtime['python']['version'] == PYTHON_VERSION
        report['update_helper_runtime']=helper_runtime
        base=[str(exe),'--data-dir',str(output/'data'),'--no-open']
        with (output/'runtime.log').open('wb') as log:
            command=base+['--check-runtime',str(output/'runtime.json')]
            if args.platform=='windows':command+=['--check-offline']
            subprocess.run(command,env=env,cwd=output,stdout=log,stderr=log,check=True,timeout=180)
        runtime=json.loads((output/'runtime.json').read_text(encoding='utf-8'))
        assert runtime['python']['version'] == PYTHON_VERSION and runtime['python_inventory']
        assert runtime['ok'] and runtime['frozen'];report['runtime']=runtime
        ports=[]
        for iteration in range(2):
            with (output/'launch.log').open('ab') as log:
                command=base+(['--smoke-seconds','12'] if args.platform=='windows' else [])
                process=subprocess.Popen(command,env=env,cwd=output,stdout=log,stderr=log)
                try:
                    state=output/'data/.desktop-instance.json';deadline=time.monotonic()+40
                    while not state.exists():
                        assert process.poll() is None,'Application exited before ready'
                        if time.monotonic()>deadline:raise TimeoutError('Candidate startup timed out')
                        time.sleep(.1)
                    record=json.loads(state.read_text(encoding='utf-8'));ports.append(record['port'])
                    for route in ('/','/topics','/insights','/settings'):
                        with urllib.request.urlopen(f'http://127.0.0.1:{record["port"]}'+route,timeout=10) as response:
                            assert response.status==200
                    if args.platform=='mac':process.terminate()
                    assert process.wait(timeout=35)==0
                    assert not state.exists()
                finally:
                    if process.poll() is None:process.terminate();process.wait(timeout=15)
        assert ports[0]==ports[1]==57740
        report.update(ok=True,fixed_port=ports[0],launches=2,pages=4)
        (output/'support.md').write_text('候选构建验收：版本、源码提交、冻结运行依赖、四个页面、固定端口及两次启动退出已通过。\n本报告不表示公开发布或正式数据升级已批准。Windows桌面凭据另以合成数据验收；不包含Windows 11或ARM64支持承诺。\n',encoding='utf-8')
    except BaseException as error:
        report['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
