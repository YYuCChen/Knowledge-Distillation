"""Execute the production JS preflight; fixtures never touch daily Chrome/daemon."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ADAPTER = Path(__file__).resolve().parents[2] / 'src/knowledge_distiller/v1/adapters/reader-page.mjs'


@pytest.mark.parametrize('states,expected,starts', [
    ([None, {'extensionConnected': True, 'contextId': 'daily'}], 'daily', 1),
    ([{'extensionConnected': True, 'contextId': 'daily'}], 'daily', 0),
    ([{'profileRequired': True}], 'xiaohongshu_bridge_profile_required', 0),
    ([{'profileDisconnected': True}], 'xiaohongshu_bridge_profile_disconnected', 0),
    ([{'extensionConnected': False}], 'xiaohongshu_bridge_extension_required', 0),
    ([None], 'xiaohongshu_bridge_start_failed', 1),
    ([{'extensionConnected': True, 'contextId': 'other'}], 'xiaohongshu_connection_changed', 0),
])
def test_bridge_preflight(tmp_path, states, expected, starts):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    browser = tmp_path / 'dist/src/browser'
    browser.mkdir(parents=True)
    (tmp_path/'package.json').write_text('{"type":"module"}')
    (browser/'page.js').write_text('export class Page {}')
    (browser/'daemon-transport.js').write_text(
        'const states='+json.dumps(states)+'; export async function fetchDaemonStatus(){return states.length>1?states.shift():states[0];}')
    (browser/'daemon-lifecycle.js').write_text(
        "export function spawnDaemonProcess(){globalThis.starts++;return {once(){}};}")
    runner = tmp_path/'check.mjs'
    runner.write_text('''globalThis.starts=0;
// Advance only this fixture clock to keep failed-start regression bounded.
let now=0;Date.now=()=>now;globalThis.setTimeout=(fn,ms)=>{now+=ms;queueMicrotask(fn)};
const {readerPage}=await import('''+json.dumps(ADAPTER.as_uri())+''');
let result;try{result=(await readerPage('''+json.dumps(str(tmp_path))+''','xiaohongshu','daily')).contextId}catch(e){result=e.message}
console.log(JSON.stringify({result,starts:globalThis.starts}));''')
    run = subprocess.run([node,str(runner)],capture_output=True,text=True,timeout=15,check=True)
    assert json.loads(run.stdout) == {'result': expected, 'starts': starts}


def test_bridge_errors_are_actionable_and_frontend_explains_dependency():
    from knowledge_distiller.v1.settings_web import MESSAGES
    for platform in ('xiaohongshu','zhihu'):
        assert 'Reconnect' in MESSAGES[platform+'_bridge_extension_required']
        assert '专用浏览器' not in MESSAGES[platform+'_browser_unavailable']
        assert '无需另装' in MESSAGES[platform+'_bridge_start_failed']
