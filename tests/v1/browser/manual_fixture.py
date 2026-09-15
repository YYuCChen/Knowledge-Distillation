from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import json,time,io,wave
source=Path(__file__).resolve().parents[3]/'src/knowledge_distiller/v1/static/home.js'
state={'revision':0,'committed':0}
audio=io.BytesIO()
with wave.open(audio,'wb') as w:
 w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0\0'*16000*120)
def page():
 n=state['revision']
 return f'''<!doctype html><meta charset="utf-8"><title>Isolated reconciliation fixture</title><span class="topbar-status">fixture {n}</span><div style="height:300px"></div><div id="home-results"><section data-sync-key="todo"><h2>待处理</h2><div>{'<p data-sync-key="inserted">new preceding card</p>' if n%2 else ''}<article data-sync-key="member-stable"><b id="server-version" data-committed="{state['committed']}">{n}</b><form class="manual-confirmation" id="manual-stable"><input name="value" aria-label="合成草稿"><input type="hidden" name="token" value="{n}"><button type="button">保持焦点</button></form><audio id="source-audio" controls preload="auto" src="/audio.wav"></audio><p style="height:1000px">synthetic fixture</p></article></div></section></div><script>window.kdDialog=async()=>true;</script><script src="/home.js"></script>'''
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path.startswith('/advance'):
   state.update(revision=state['revision']+1,committed=time.time()*1000);data=json.dumps(state).encode();kind='application/json'
  elif self.path=='/home.js':data=source.read_bytes();kind='text/javascript'
  elif self.path=='/audio.wav':data=audio.getvalue();kind='audio/wav'
  else:data=page().encode();kind='text/html'
  self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*a):pass
def serve():
 return ThreadingHTTPServer(('127.0.0.1',0),Handler)
