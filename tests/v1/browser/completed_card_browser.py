"""Focused production reconciler regression; synthetic server, real browser/audio."""
import argparse
import json
from pathlib import Path
import subprocess
import threading
import uuid

import manual_fixture

p = argparse.ArgumentParser()
p.add_argument('--output', type=Path, required=True)
p.add_argument('--baseline', action='store_true')
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
if a.baseline:
    previous = subprocess.check_output(['git', 'show', 'HEAD:src/knowledge_distiller/v1/static/home.js'])
    original = a.output / 'baseline-home.js'
    original.write_bytes(previous)
    manual_fixture.source = original
server = manual_fixture.serve()
threading.Thread(target=server.serve_forever, daemon=True).start()
session = 'kd-stale-' + uuid.uuid4().hex[:10]
commands = []
def browser(*args, js=None):
    r = subprocess.run(['agent-browser', '--session', session, *args], input=js,
                       text=True, capture_output=True, timeout=45)
    commands.append({'args': args, 'returncode': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr})
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout
def evaluate(js):
    return json.loads(browser('eval', '--stdin', js=js))
result = {'baseline': a.baseline, 'passed': False, 'checks': {}}
try:
    browser('open', f'http://127.0.0.1:{server.server_port}')
    browser('snapshot', '-i')
    browser('fill', 'input[name=value]', '保留我的草稿')
    result['checks'].update(evaluate('''(async()=>{
      clearTimeout(pollTimer);
      const card=document.querySelector('[data-sync-key="member-stable"]');
      card.classList.add('todo-card-shell');
      const input=card.querySelector('input[name=value]'), audio=card.querySelector('audio');
      window.old={card,input,audio}; audio.muted=true; audio.loop=true; await audio.play();
      input.focus(); input.setSelectionRange(1,4);
      window.originalPage=document.documentElement.outerHTML;
      const incoming=new DOMParser().parseFromString(originalPage,'text/html');
      const replacement=incoming.querySelector('[data-sync-key="member-stable"]');
      replacement.dataset.syncKey='member-unrelated';
      replacement.querySelector('form').id='manual-unrelated';
      replacement.querySelector('audio').id='audio-unrelated';
      replacement.querySelector('input[name=value]').value='';
      applyPage(incoming.documentElement.outerHTML);
      const first={draftRetained:input.isConnected && input.value==='保留我的草稿',
        sameAudio:audio.isConnected && old.card.querySelector('audio')===audio,
        playing:!audio.paused, readonly:input.readOnly,
        disabled:Array.from(card.querySelectorAll('button')).every(b=>b.disabled),
        unrelatedPresent:!!document.querySelector('[data-sync-key="member-unrelated"]'),
        selection:input.selectionStart===1 && input.selectionEnd===4};
      let posts=0; window.fetch=async()=>{posts++; throw new Error('stale submission escaped')};
      card.querySelector('form').requestSubmit(); await Promise.resolve();
      first.staleSubmitBlocked=posts===0;
      const empty=new DOMParser().parseFromString(originalPage,'text/html');
      empty.querySelector('#home-results').innerHTML='<section data-sync-key="processing">处理中</section>';
      applyPage(empty.documentElement.outerHTML);
      first.lastCardRetained=input.isConnected && audio.isConnected && !audio.paused;
      first.unrelatedRemoved=!document.querySelector('[data-sync-key="member-unrelated"]');
      applyPage(empty.documentElement.outerHTML);
      first.noDuplicate=document.querySelectorAll('[data-stale-confirmation]').length===1;
      const before=audio.currentTime; await new Promise(resolve=>setTimeout(resolve,200));
      first.audioAdvances=audio.currentTime>before;
      return first;
    })()'''))
    # A successful local submission must still remove its own card.
    result['checks'].update(evaluate('''(()=>{
      document.querySelector('#home-results').outerHTML=new DOMParser().parseFromString(originalPage,'text/html').querySelector('#home-results').outerHTML;
      const card=document.querySelector('[data-sync-key="member-stable"]');
      card.querySelector('input[name=value]').value='本页已提交';
      lastServerHTML='force refresh';
      applyPage('<div id="home-results"></div>','manual-stable','member-stable');
      const ownRemoved=!card.isConnected;
      document.querySelector('#home-results').innerHTML='<div class="todo-card-shell" data-sync-key="member-clean"><form><input name="value"></form></div>';
      lastServerHTML='force clean refresh';
      applyPage('<div id="home-results"></div>');
      return {ownRemoved,cleanPeerRemoved:!document.querySelector('[data-sync-key="member-clean"]')};
    })()'''))
    result['passed'] = all(result['checks'].values())
finally:
    browser('close')
    server.shutdown()
    (a.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (a.output / 'commands.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result['passed'] else 1)
