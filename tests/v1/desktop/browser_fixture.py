"""Explicitly isolated, synthetic browser protocol fixture (not a Dock test)."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import threading

from flask import Flask, jsonify, render_template_string, request
from werkzeug.serving import make_server
from knowledge_distiller.v1.desktop_pages import install

parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', type=Path, required=True)
args = parser.parse_args()
args.data_dir.mkdir(parents=True, exist_ok=False)
root = Path(__file__).resolve().parents[3] / 'src/knowledge_distiller/v1'
app = Flask(__name__, template_folder=str(root/'templates'), static_folder=str(root/'static'))
pages = install(app)
opened, results = [], []

@app.get('/')
@app.get('/settings')
@app.get('/topics')
@app.get('/new-knowledge')
@app.get('/collections')
def page():
    return render_template_string('''{% extends "base.html" %}{% block content %}
    <main><h1>合成桌面协议测试</h1><a href="/settings">设置</a><a href="/topics">主题</a>
    <a id="slow" href="/_fixture/slow?seconds=5">慢导航</a>
    <input id="draft" aria-label="合成草稿"><textarea id="notes"></textarea></main>
    {% endblock %}''')

@app.get('/_fixture/icons')
def icons():
    names = [p.name for p in sorted((root/'static/icons').glob('favicon-*.png'))]
    return render_template_string('''{% extends "base.html" %}{% block content %}
    {% for background in ['#ffffff', '#171717'] %}<section style="background:{{ background }};padding:32px">
    {% for name in names %}<img src="/static/icons/{{ name }}" style="margin:16px" alt="{{ name }}">{% endfor %}
    </section>{% endfor %}{% endblock %}''', names=names)

@app.get('/_fixture/state')
def state():
    with pages.condition:
        return jsonify(pages=[asdict(p) for p in pages.pages.values()], selected=pages.selected,
                       opened=opened, results=results)

@app.post('/_fixture/reopen')
def reopen():
    def run():
        result = pages.reopen(opened.append)
        results.append(asdict(result))
    threading.Thread(target=run, daemon=True).start()
    return '', 202

@app.get('/_fixture/slow')
def slow():
    import time
    time.sleep(float(request.args.get('seconds', 5)))
    return page()

server = make_server('127.0.0.1', 0, app, threaded=True)
(args.data_dir/'server.json').write_text(json.dumps({'port': server.server_port}))
print(json.dumps({'port': server.server_port}), flush=True)
server.serve_forever()
