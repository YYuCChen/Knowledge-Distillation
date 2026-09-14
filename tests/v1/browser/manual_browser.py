"""Real browser, synthetic queue: validate production home.js without formal data.

This proves reconciliation only, not native IME, real model or frozen-app flows.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import threading
import uuid

from manual_fixture import serve, source

parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
session='kd-manual-'+uuid.uuid4().hex[:12]
server=serve()
threading.Thread(target=server.serve_forever,daemon=True).start()
log=[]
def browser(*command,js=None):
    argv=['agent-browser','--session',session,*command]
    if platform.system()=='Windows':
        # The browser daemon inherits PIPE handles on Windows, preventing EOF
        # after the CLI exits. Files preserve output without waiting on the daemon.
        capture=args.output/'command-capture'
        capture.mkdir(exist_ok=True)
        prefix=capture/str(len(log))
        stdin_path=prefix.with_suffix('.stdin')
        stdout_path=prefix.with_suffix('.stdout')
        stderr_path=prefix.with_suffix('.stderr')
        stdin_path.write_text(js or '',encoding='utf-8')
        timeout_error=None
        with stdin_path.open('rb') as stdin,stdout_path.open('wb') as stdout,stderr_path.open('wb') as stderr:
            try:
                completed=subprocess.run(argv,stdin=stdin,stdout=stdout,stderr=stderr,timeout=45)
            except subprocess.TimeoutExpired as error:
                timeout_error=error
        stdout_text=stdout_path.read_text(encoding='utf-8',errors='replace')
        stderr_text=stderr_path.read_text(encoding='utf-8',errors='replace')
        if timeout_error is not None:
            log.append({'command':list(command),'returncode':None,'timeout_seconds':45,'stdout':stdout_text,'stderr':stderr_text})
            timeout_error.output=stdout_text
            timeout_error.stderr=stderr_text
            raise timeout_error
        result=subprocess.CompletedProcess(argv,completed.returncode,stdout_text,stderr_text)
    else:
        result=subprocess.run(argv,input=js,text=True,capture_output=True,timeout=45)
    log.append({'command':list(command),'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    if result.returncode:raise RuntimeError('browser command failed: '+str(command))
    return result.stdout

def evaluate(script):
    return json.loads(browser('eval','--stdin',js=script))

result={'status':'failed','level':'integration','fixture':'synthetic_queue',
        'python':platform.python_version(),'platform':platform.platform(),
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'not_proven':['native_IME','actual_group_mutation','Feishu_device','frozen_application'],
        'assertions':[]}
try:
    browser('open',f'http://127.0.0.1:{server.server_port}')
    browser('snapshot','-i')
    browser('fill','input[name=value]','保留合成草稿')
    evaluate('''(()=>{window.probe={audio:document.querySelector('audio'),input:document.querySelector('input[name=value]'),samples:[]};probe.audio.muted=true;probe.audio.play();probe.input.focus();probe.input.setSelectionRange(2,4);window.scrollTo(0,250);return true;})()''')
    setup=Path(__file__).with_name('manual_samples.js').read_text()
    evaluate(setup)
    result['browser']=evaluate('navigator.userAgent')
    result['browser_full_version']=evaluate("navigator.userAgentData ? navigator.userAgentData.getHighEntropyValues(['fullVersionList']) : Promise.resolve({userAgent:navigator.userAgent})")
    result['source_commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source.parents[3],text=True).strip()
    result['source_dirty']=bool(subprocess.check_output(['git','status','--porcelain'],cwd=source.parents[3],text=True).strip())
    for mode in ('playing','paused'):
        if mode=='paused':evaluate('probe.audio.pause(); true')
        for batch in range(2):evaluate('runSamples(10)')
    samples=evaluate('probe.samples')
    for mode,selected in [('playing',samples[:20]),('paused',samples[20:])]:
        result[mode]={'samples':selected,'max_ms':max(s['ms'] for s in selected),
                      'p95_ms':sorted(s['ms'] for s in selected)[18]}
        passed=all(s['sameAudio'] and s['sameInput'] and s['focus'] and s['draft']=='保留合成草稿'
            and s['selection']==[2,4] and abs(s['anchorDelta'])<=2 and s['ms']<=3000
            and (s['playing'] and s['audioDelta']>0 if mode=='playing' else not s['playing'] and abs(s['audioDelta'])<=.1)
            for s in selected)
        result['assertions'].append({'id':'reconciliation_'+mode,'passed':passed})
    result['status']='passed' if all(a['passed'] for a in result['assertions']) else 'failed'
except Exception as error:
    result['error']=str(error)
finally:
    try:browser('close')
    except Exception:pass
    server.shutdown()
    (args.output/'commands.json').write_text(json.dumps(log,ensure_ascii=False,indent=2))
    (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({'status':result['status'],'output':str(args.output)},ensure_ascii=False))
raise SystemExit(0 if result['status']=='passed' else 1)
