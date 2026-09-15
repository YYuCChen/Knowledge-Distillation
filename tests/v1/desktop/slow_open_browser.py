"""Real browser loading beyond the open budget; no native/Dock claims."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--session', required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
base = f'http://127.0.0.1:{args.port}'
records = []
def state():
    return json.load(urllib.request.urlopen(base+'/_fixture/state'))
def reopen():
    urllib.request.urlopen(urllib.request.Request(base+'/_fixture/reopen', data=b'', method='POST')).close()
def wait(predicate):
    deadline = time.monotonic()+18
    while time.monotonic() < deadline:
        value = state()
        if predicate(value): return value
        time.sleep(.05)
    raise AssertionError(state())
def browser(*argv):
    return subprocess.check_output(['agent-browser', '--session', args.session, *argv], text=True)

assert state()['pages'] == [] and state()['opened'] == [], 'requires a fresh isolated fixture server'
lock = {'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip()),
        'os': subprocess.check_output(['sw_vers'], text=True), 'python': sys.version,
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'fixture_sha256': hashlib.sha256(Path(__file__).with_name('browser_fixture.py').read_bytes()).hexdigest(),
        'open_budget_seconds': 10, 'injected_response_delay_seconds': 12,
        'evidence_level': 'browser_protocol', 'native_dock': 'not_run'}
(args.output/'parameter-lock.json').write_text(json.dumps(lock, ensure_ascii=False, indent=2))
try:
    reopen()
    value = wait(lambda s: len(s['opened']) == 1)
    nonce = value['opened'][0]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(browser, 'open', base+'/_fixture/slow?seconds=12&_desktop_launch='+nonce)
        value = wait(lambda s: s['results'])
        assert value['results'][0]['reason'] == 'open_handshake_timeout'
        records.append({'case': 'budget_timeout', 'state': value})
        for _ in range(5): reopen()
        value = wait(lambda s: len(s['results']) >= 6)
        assert len(value['opened']) == 1
        records.append({'case': 'five_requests_do_not_open_again', 'state': value})
        pending.result(timeout=18)
    value = wait(lambda s: any(p['launch_id'] == nonce for p in s['pages']))
    records.append({'case': 'late_matching_handshake', 'state': value})
    before = len(value['results']); reopen()
    value = wait(lambda s: len(s['results']) > before)
    assert value['results'][-1]['status'] in {'online', 'visible_reported'}
    assert len(value['opened']) == 1 and not value['results'][-1]['foreground_verified']
    records.append({'case': 'late_page_is_reusable', 'state': value})
    lock['browser'] = json.loads(json.loads(browser('eval',
        '(async () => JSON.stringify({version:await navigator.userAgentData.getHighEntropyValues(["fullVersionList"]),width:innerWidth,height:innerHeight,dpr:devicePixelRatio}))()')))
    (args.output/'parameter-lock.json').write_text(json.dumps(lock, ensure_ascii=False, indent=2))
    (args.output/'result.json').write_text(json.dumps({'result': 'passed', 'records': records,
        'not_proven': ['native_openURL', 'Dock', 'Safari', 'actual_foreground']}, ensure_ascii=False, indent=2))
except Exception as error:
    (args.output/'result.json').write_text(json.dumps({'result': 'failed', 'error': str(error), 'records': records}, ensure_ascii=False, indent=2))
    raise
finally:
    browser('close')
