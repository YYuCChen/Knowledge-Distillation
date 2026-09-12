"""Mac-owned dual-platform candidate dispatch, recovery and verified collection.

Local machine configuration stays outside source control. Explicit commands only;
this tool never publishes, installs, changes credentials or reboots a host.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def call(command, **kw):
    return subprocess.check_output(command, text=True, encoding='utf-8', **kw).strip()


def ssh_options(config):
    return ['-i', config['ssh_key'], '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + config['known_hosts'],
            '-o', 'ConnectTimeout=8']


def remote(config, ps):
    ps = "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + ps
    encoded = base64.b64encode(ps.encode('utf-16le')).decode()
    return call(['ssh', *ssh_options(config), config['windows_host'],
                 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + encoded])


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def copy(config, local, remote_path, *, download=False):
    endpoint = config['windows_host'] + ':' + remote_path
    command = ['scp', *ssh_options(config)]
    subprocess.run(command + ([endpoint, str(local)] if download else [str(local), endpoint]), check=True)


def remote_python(config, code):
    encoded = base64.b64encode(code.encode()).decode()
    return remote(config, f'& {literal(config["windows_python"])} -c "import base64;exec(base64.b64decode(\'{encoded}\'))"; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}')


def job_action(config, job, action, request=None, retry=False):
    command = [config['windows_python'], job['source'] + '/scripts/build_job.py', action, job['job']]
    if request: command += ['--request', request]
    if retry: command += ['--retry']
    return json.loads(remote(config, '& ' + ' '.join(literal(v) for v in command) + '; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }'))


def log_snapshot(job):
    patterns=['worker.log','attempt-*/*.log','attempt-*/build/build.log','attempt-*/verification/*.log']
    result={}
    for pattern in patterns:
        for path in sorted(job.glob(pattern)):
            if path.is_file():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size-65536))
                    result[path.relative_to(job).as_posix()]=stream.read(65536).decode('utf-8',errors='replace')
    return result


def collect_logs(config, state, destination):
    # Bounded snapshots are readable even while a build is running. Full logs
    # remain on the owning host; no artifact or personal data directories copied.
    logs={'mac':log_snapshot(Path(state['mac']['job']))}
    body = """import pathlib,json
job=pathlib.Path(JOB_PATH)
result={}
for pattern in ['worker.log','attempt-*/*.log','attempt-*/build/build.log','attempt-*/verification/*.log']:
 for path in sorted(job.glob(pattern)):
  if path.is_file():
   with path.open('rb') as stream:
    stream.seek(max(0,path.stat().st_size-65536))
    result[path.relative_to(job).as_posix()]=stream.read(65536).decode('utf-8',errors='replace')
print(json.dumps(result,ensure_ascii=True))
""".replace('JOB_PATH',repr(state['windows']['job']))
    logs['windows']=json.loads(remote_python(config,body))
    (destination/'logs.json').write_text(json.dumps(logs,ensure_ascii=False,indent=2),encoding='utf-8')


def make_requests(config, commit, version, product, mac_source, win_source):
    mac_py = config['mac_python']; win_py = config['windows_python']
    common = dict(source_commit=commit, version=version, product_version=product)
    mac = dict(common, source=str(mac_source), env={'LANG':'en_US.UTF-8','LC_ALL':'en_US.UTF-8','PYTHONUTF8':'1','PYTHONPATH':'{source}/src:{source}'}, steps=[
        {'name':'build','command':[mac_py,'scripts/build_mac.py','--output','{attempt}/build','--version',version,'--product-version',product,'--signing-config',config['signing_config'],'--sparkle-sdk',config['sparkle_sdk'],'--update-config','packaging/update_config.json','--docling-models',config['mac_models']]},
        {'name':'verify','command':[mac_py,'scripts/verify_candidate.py','--platform','mac','--build','{attempt}/build','--output','{attempt}/verification','--version',version,'--commit',commit]},
        {'name':'zip','command':['ditto','-c','-k','--sequesterRsrc','--keepParent','{attempt}/build/知识蒸馏器.app','{attempt}/KnowledgeDistiller-'+version+'-Mac-arm64.zip']}
    ],artifacts=['*.zip','verification/result.json','build/build-manifest.json'])
    windows = dict(common, source=win_source, env={'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','PYTHONPATH':'{source}/src;{source}'},steps=[
        {'name':'native','command':[win_py,'-m','pytest','-q','tests/v1/test_v12_source_integrity.py','tests/v1/test_vision_ocr.py','tests/v1/test_windows_lifecycle.py','tests/v1/test_windows_version_metadata.py','tests/v1/test_windows_12_updates.py','tests/test_build_job.py','--basetemp','{attempt}/test-data']},
        {'name':'build','command':[win_py,'scripts/build_windows.py','--output','{attempt}/build','--version',version,'--product-version',product,'--cache-root',config['windows_cache']]},
        {'name':'verify','command':[win_py,'scripts/verify_candidate.py','--platform','windows','--build','{attempt}/build','--output','{attempt}/verification','--version',version,'--commit',commit]},
        {'name':'zip','command':[win_py,'scripts/package_windows.py','--app','{attempt}/build/KnowledgeDistiller','--output','{attempt}/delivery','--version',version,'--cache-root',config['windows_cache'],'--test-report','{attempt}/verification/support.md']}
    ],artifacts=['delivery/*.zip','verification/result.json','build/build-manifest.json'])
    mac['host_lock']=str(Path(config['output_root'])/'host.lock')
    windows['host_lock']=config['windows_root'].rstrip('/')+'/host.lock'
    mac['steps'].append({'name':'archive-check','command':[mac_py,'scripts/verify_archive.py','{attempt}/KnowledgeDistiller-'+version+'-Mac-arm64.zip','--version',version,'--commit',commit]})
    windows['steps'].append({'name':'archive-check','command':[win_py,'scripts/verify_archive.py','{attempt}/delivery/KnowledgeDistiller-'+version+'-Windows-x64.zip','--version',version,'--commit',commit]})
    return mac,windows


def accept_download(partial, target, artifact):
    with partial.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if digest!=artifact['sha256'] or partial.stat().st_size!=artifact['size']:
        raise ValueError('Collected artifact mismatch: '+target.name)
    os.replace(partial,target)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','start','status','collect','cancel'])
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--product-version',default='1.2')
    parser.add_argument('--commit',default='HEAD')
    parser.add_argument('--retry',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+',args.version):parser.error('Invalid version')
    config=json.loads(args.config.read_text())
    root=Path(config['output_root']).resolve()/args.version
    state_path=root/'dispatch.json'
    if args.action=='prepare':
        if state_path.exists():
            previous=json.loads(state_path.read_text())
            commit=call(['git','rev-parse',args.commit])
            if previous['commit']!=commit or previous['product_version']!=args.product_version: raise ValueError('Version already assigned to different inputs')
            print(state_path);return
        probe = 'import platform,sys; assert platform.python_version()=="3.11.16", sys.version; print(platform.python_version())'
        call([config['mac_python'], '-I', '-c', probe])
        remote_python(config, probe)
        root.mkdir(parents=True,exist_ok=True)
        commit=call(['git','rev-parse',args.commit])
        if call(['git','status','--porcelain']):raise ValueError('Commit all candidate changes before preparing immutable sources')
        bundle=root/'source.bundle'
        subprocess.run(['git','bundle','create',str(bundle),'HEAD'],check=True)
        mac_source=root/'source'
        if not mac_source.exists():
            subprocess.run(['git','clone','--no-hardlinks',str(bundle),str(mac_source)],check=True)
        elif call(['git','status','--porcelain'],cwd=mac_source):
            raise ValueError('Partial snapshot has changed; inspect before resuming prepare')
        subprocess.run(['git','checkout','--detach',commit],cwd=mac_source,check=True)
        win_root=config['windows_root'].rstrip('/')+'/'+args.version
        win_source=win_root+'/source'
        remote(config,f'[IO.Directory]::CreateDirectory({literal(win_root)}) | Out-Null')
        copy(config,bundle,win_root+'/source.bundle')
        expected=hashlib.sha256(bundle.read_bytes()).hexdigest()
        actual=remote(config,f'(Get-FileHash -Algorithm SHA256 {literal(win_root+"/source.bundle")}).Hash').lower()
        if actual!=expected:raise ValueError('Source transfer hash mismatch')
        remote(config,f'if (-not (Test-Path {literal(win_source)})) {{ git clone {literal(win_root+"/source.bundle")} {literal(win_source)}; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }} }}; git -C {literal(win_source)} checkout --detach {literal(commit)}; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}')
        mac_req,win_req=make_requests(config,commit,args.version,args.product_version,mac_source,win_source)
        (root/'mac-request.json').write_text(json.dumps(mac_req,ensure_ascii=False,indent=2))
        (root/'windows-request.json').write_text(json.dumps(win_req,ensure_ascii=False,indent=2))
        copy(config,root/'windows-request.json',win_root+'/request.json')
        state={'commit':commit,'product_version':args.product_version,'version':args.version,'mac':{'source':str(mac_source),'job':str(root/'mac-job')},'windows':{'source':win_source,'job':win_root+'/job'},'windows_request':win_root+'/request.json'}
        state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2));print(state_path);return
    state=json.loads(state_path.read_text())
    local=[config['mac_python'],state['mac']['source']+'/scripts/build_job.py']
    action={'start':'submit','collect':'status'}.get(args.action,args.action)
    command=local+[action,state['mac']['job']]
    if action=='submit':command+=['--request',str(root/'mac-request.json')]+(['--retry'] if args.retry else [])
    mac=json.loads(call(command))
    windows=job_action(config,state['windows'],action,state['windows_request'] if action=='submit' else None,args.retry)
    result={'version':args.version,'commit':state['commit'],'mac':mac,'windows':windows,'published':False}
    if args.action=='collect':
        destination=root/'collected';destination.mkdir(exist_ok=True)
        collect_logs(config,state,destination)
        for platform,record in [('mac',mac),('windows',windows)]:
            if record['status']!='succeeded':continue
            for artifact in record['artifacts']:
                target=destination/(platform+'-'+Path(artifact['path']).name)
                partial=target.with_name(target.name+'.part')
                if platform=='windows':copy(config,partial,state['windows']['job']+'/'+artifact['path'],download=True)
                else:
                    import shutil
                    shutil.copyfile(Path(state['mac']['job'])/artifact['path'],partial)
                accept_download(partial,target,artifact)
        result['both_candidates_built']=mac['status']==windows['status']=='succeeded'
        (destination/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
