"""Run against browser_fixture.py with an explicitly isolated agent-browser session.

This collects browser protocol evidence. It cannot certify native Dock behavior.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import platform
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--session', required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
base = f'http://127.0.0.1:{args.port}'
records = []
def command(*argv):
    result = subprocess.run(['agent-browser', '--session', args.session, *argv], text=True, capture_output=True, check=True)
    return result.stdout

def state():
    return json.load(urllib.request.urlopen(base+'/_fixture/state'))

def wait(predicate):
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        current = state()
        if predicate(current): return current
        time.sleep(.03)
    raise AssertionError(state())

def capture(name):
    value = state(); records.append({'name': name, 'state': value}); return value

try:
    command('open', base+'/settings')
    metadata = command('eval', 'JSON.stringify({ua:navigator.userAgent,width:innerWidth,height:innerHeight,dpr:devicePixelRatio,zoom:visualViewport.scale})')
    lock = {'os': subprocess.check_output(['sw_vers'], text=True),
            'machine': platform.machine(), 'python': sys.version,
            'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip()),
            'fixture_sha256': hashlib.sha256(Path(__file__).with_name('browser_fixture.py').read_bytes()).hexdigest(),
            'browser': json.loads(json.loads(metadata)),
            'runner': {'path': __file__, 'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            'agent_browser': subprocess.check_output(['agent-browser', '--version'], text=True).strip(),
            'parameters': {'navigation_grace': 2, 'probe_timeout': 1, 'recovery_timeout': 3, 'opening_timeout': 10},
            'status': 'initial_parameters_browser_protocol_only', 'native_dock': 'not_run'}
    lock['browser']['full_version'] = json.loads(json.loads(command('eval',
        '(async () => JSON.stringify(await navigator.userAgentData.getHighEntropyValues(["fullVersionList"])))()')))
    lock['parameters']['collision_probe'] = .25
    (args.output/'parameter-lock.json').write_text(json.dumps(lock, ensure_ascii=False, indent=2))
    before = wait(lambda s: any(p['route'] == '/settings' and p['transport_state'] == 'connected' for p in s['pages']))
    page = next(p for p in before['pages'] if p['transport_state'] == 'connected')
    command('fill', '#draft', '保留合成草稿')
    capture('initial_handshake')
    # A true click establishes app-owned interaction; it is setup, never a
    # tool-assisted bringToFront counted as the result of a product reopen.
    command('click', 'a[href="/topics"]')
    after = wait(lambda s: any(p['route'] == '/topics' and p['transport_state'] == 'connected' for p in s['pages']))
    current = next(p for p in after['pages'] if p['route'] == '/topics' and p['transport_state'] == 'connected')
    assert current['page_id'] == page['page_id']
    assert current['connection_epoch'] != page['connection_epoch']
    capture('navigation_preserves_page_new_epoch')
    count = len(after['results'])
    urllib.request.urlopen(urllib.request.Request(base+'/_fixture/reopen', data=b'', method='POST')).close()
    result = wait(lambda s: len(s['results']) > count)['results'][-1]
    assert result['status'] in ['online', 'visible_reported'] and not result['foreground_verified']
    assert not state()['opened']
    capture('reopen_ack_not_foreground_claim')
    # Delay the target response, then challenge Dock protocol while navigation
    # or a browser reload is in progress. Do not forcibly focus the target.
    for seconds in [.4, 2, 5]:
        command('open', base+'/topics')
        wait(lambda s: any(p['route'] == '/topics' and p['transport_state'] == 'connected' for p in s['pages']))
        command('eval', f'document.getElementById("slow").href="/_fixture/slow?seconds={seconds}"')
        for action in ['navigate', 'reload']:
            before_state = state()
            before_count = len(before_state['results'])
            previous_documents = {p['document_id'] for p in before_state['pages']}
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(command, *(['click', '#slow'] if action == 'navigate' else ['reload']))
                time.sleep(.15)
                urllib.request.urlopen(urllib.request.Request(base+'/_fixture/reopen', data=b'', method='POST')).close()
                future.result(timeout=15)
            value = wait(lambda s: len(s['results']) > before_count and
                any(p['route'] == '/_fixture/slow' and p['document_id'] not in previous_documents
                    and p['transport_state'] == 'connected' for p in s['pages']))
            command('wait', '--fn', 'document.readyState !== "loading"')
            assert not value['opened'], (seconds, action, value)
            capture(f'slow_{action}_{seconds}s_no_duplicate')
    command('open', base+'/_fixture/icons')
    command('set', 'viewport', '1280', '900', '2')
    lock['gallery_environment'] = json.loads(json.loads(command('eval',
        'JSON.stringify({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,zoom:visualViewport.scale})')))
    (args.output/'parameter-lock.json').write_text(json.dumps(lock, ensure_ascii=False, indent=2))
    command('screenshot', str(args.output/'icons-light-dark-2x.png'))
    capture('brand_sizes_light_dark_retina_resource_gallery')
    command('open', base+'/topics')
    command('eval', 'window.open("/settings", "_blank"); "opened test duplicate"')
    after = wait(lambda s: len([p for p in s['pages'] if p['transport_state'] == 'connected']) == 2)
    active = [p for p in after['pages'] if p['transport_state'] == 'connected']
    assert len({p['page_id'] for p in active}) == 2
    capture('copied_session_storage_allocates_distinct_page')
    command('close')
    after = wait(lambda s: all(p['transport_state'] == 'disconnected' for p in s['pages']))
    capture('browser_close_transport_observed')
    # This is a protocol close observation only. No native openURL is executed.
    close_signals = all(p['leaving_at'] is not None for p in after['pages'])
    records.append({'name': 'browser_shutdown_signal_observation', 'all_pagehide_received': close_signals,
                    'note': 'headless runner browser shutdown may differ from normal native Quit'})
    (args.output/'result.json').write_text(json.dumps({'result': 'passed', 'records': records,
        'not_proven': ['native_dock', 'safari', 'actual_foreground', 'native_normal_close_matrix']}, ensure_ascii=False, indent=2))
except Exception as error:
    (args.output/'result.json').write_text(json.dumps({'result': 'failed', 'error': str(error), 'records': records}, ensure_ascii=False, indent=2))
    raise
