"""Self-contained installer UI; all program/model logic lives in shared modules."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from uuid import uuid4
import webbrowser

import httpx
from flask import Flask, request, render_template_string, redirect, make_response
from werkzeug.serving import make_server

from .adapters.python_policy import check_current
from .component_assembly import ComponentAssembly
from .component_install import install, recover, finalize_install
from .component_release import MAX_MANIFEST
from .component_download import ComponentDownloader
from .updates import UpdateError, validate_install_paths


PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>安装知识蒸馏器</title><style>body{font:16px system-ui;margin:64px auto;padding:0 24px;max-width:640px;background:#f7f7f4;color:#202724}h1{font-size:28px}p{line-height:1.7}label{display:block;margin:18px 0}input{display:block;width:100%;box-sizing:border-box;padding:12px;margin-top:8px}button{padding:12px 20px;border:0;border-radius:8px;background:#264e42;color:white;cursor:pointer}form{margin:16px 0}button:disabled{opacity:.5}.note{color:#56635d}progress{width:100%}</style>
<h1>安装知识蒸馏器</h1><p>{{ state.message }}</p>
{% if state.busy %}<progress></progress><script>setTimeout(()=>location.reload(),2000)</script>{% endif %}
{% if state.error %}<p role="alert">{{ state.error }}</p>{% endif %}
<p class="note">知识和设置保留在现有数据目录。切换程序前，请先正常退出知识蒸馏器；已有下载会保留。</p>
{% if not state.busy and not state.complete %}<form method="post"><input type="hidden" name="token" value="{{ token }}">
{% if not state.ready %}<label>程序安装位置<input name="target" value="{{ target }}" required></label>
<label>现有数据目录<input name="data_root" value="{{ data_root }}" required></label>
<label>离线发行清单（可选）<input name="manifest_path" value="{{ manifest_path }}" placeholder="已下载清单的完整路径；组件放在同一目录"></label>
<button name="action" value="prepare">检查安装内容</button>
{% else %}<p>目标版本：{{ state.version }}<br>需要下载：{{ state.download }}<br>安装位置：{{ target }}</p>
<button name="action" value="install">安装此版本</button>{% endif %}
{% if state.recovery %}<button name="action" value="recover">恢复中断的安装</button>{% endif %}
</form>{% endif %}
{% if state.complete %}<p>安装已验收，程序已启动。可以关闭此安装页面。</p>{% endif %}
<form method="post"><input type="hidden" name="token" value="{{ token }}">
{% if state.busy and state.cancellable %}<button name="action" value="cancel">取消准备</button>
{% elif not state.busy %}<button name="action" value="close">关闭安装器</button>{% endif %}</form></html>'''


def create_installer(*, target, data_root, platform, public_key, manifest_url,
                     binary_delta=None, windows_tools=None, manifest_path=None):
    app = Flask(__name__)
    token = secrets.token_urlsafe(32)
    state = {'busy': False, 'ready': False, 'complete': False, 'error': '',
             'message': '检查当前版本和可复用组件后，显示本次需要下载的内容。'}
    context = {'target': Path(target), 'root': Path(data_root), 'candidate': None, 'manifest_path': Path(manifest_path) if manifest_path else None}
    operation = threading.Lock()
    cancelled = threading.Event()

    def task(action):
        try:
            root, target = context['root'], context['target']
            if action == 'recover':
                outcome = recover(target, root)
                context['outcome'] = outcome
                if outcome.get('accepted'):
                    state.update(complete=outcome['activation']['status'] == 'ready', ready=False,
                        message='安装已接受。' + ' '.join(outcome.get('warnings', [])))
                else:
                    state.update(message='中断的安装已恢复，可以重新检查安装内容。', ready=False)
                return
            if action == 'prepare':
                validate_install_paths(root, target)
                current = '0'
                if target.exists():
                    if platform == 'macos-arm64':
                        import plistlib
                        current = plistlib.loads((target / 'Contents/Info.plist').read_bytes())['CFBundleVersion']
                    else:
                        current = json.loads((target / '_internal/windows-version.json').read_text(encoding='utf-8'))['version']
                offline = context['manifest_path']
                if offline is not None:
                    with offline.open('rb') as stream:
                        envelope = stream.read(MAX_MANIFEST + 1)
                    if len(envelope) > MAX_MANIFEST:
                        raise UpdateError('发行清单过大。')
                else:
                    chunks, count = [], 0
                    with httpx.stream('GET', manifest_url, follow_redirects=True, timeout=60) as response:
                        response.raise_for_status()
                        for chunk in response.iter_bytes(65536):
                            if cancelled.is_set():
                                raise InterruptedError('安装准备已取消。')
                            count += len(chunk)
                            if count > MAX_MANIFEST:
                                raise UpdateError('发行清单过大。')
                            chunks.append(chunk)
                    envelope = b''.join(chunks)
                cache = root / 'updates/component-cache'
                assembler = ComponentAssembly(root / 'components', cache,
                    platform=platform, public_key=public_key, binary_delta=binary_delta, windows_tools=windows_tools,
                    downloader=ComponentDownloader(cache, offline_root=offline.parent if offline else None))
                release, plan = assembler.prepare(envelope,
                    installed=target if target.exists() else None, current=current)
                if cancelled.is_set():
                    raise InterruptedError('安装准备已取消，已有下载会保留。')
                if offline and plan.download_bytes:
                    raise UpdateError('离线目录缺少本次所需组件，尚未开始安装。请补齐发行文件后重试。')
                context.update(assembler=assembler, release=release, plan=plan, candidate=None)
                state.update(ready=True, version=release['version'],
                    download=f'{plan.download_bytes / 1024**2:.1f} MB',
                    message='发行签名与可复用组件已检查。确认后将准备组件并切换程序。')
            elif action == 'install':
                if not state['ready']:
                    raise UpdateError('请先检查安装内容。')
                release = context['release']
                if context['candidate'] is None:
                    state['message'] = '正在下载并校验组件，已有可用内容会复用。'
                    candidate, metrics = context['assembler'].assemble(release, context['plan'],
                        root / 'updates/component-attempts' / uuid4().hex,
                        installed=target if target.exists() else None,
                        cancelled=cancelled.is_set,
                        progress=lambda received, total: state.update(
                            message=f'正在准备组件：{received / 1024**2:.1f} / {total / 1024**2:.1f} MB'))
                    context['candidate'] = candidate
                if cancelled.is_set():
                    raise InterruptedError('安装准备已取消，已有下载会保留。')
                state['message'] = '候选程序已校验，正在安装并检查启动状态。'
                state['cancellable'] = False
                outcome = install(context['candidate'], target, root, platform=platform,
                        version=release['version'], target_identity=release['target_identity'])
                outcome = finalize_install(outcome, capability=context['assembler'].capability_for(context['candidate']))
                context['outcome'] = outcome
                state.update(complete=outcome['activation']['status'] == 'ready', ready=False,
                    message=('知识蒸馏器已安装并通过启动检查。' if outcome['activation']['status'] == 'ready'
                             else '安装已接受，启动放行待恢复。') + ' '.join(outcome['warnings']))
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            state['error'] = (
                '当前发布尚未提供此平台的组件安装清单，请稍后重试。程序未被替换。' if status == 404 else
                '下载服务拒绝访问（HTTP ' + str(status) + '），请检查网络访问权限后重试。' if status in {401, 403} else
                '下载服务暂时不可用（HTTP ' + str(status) + '），请稍后重试。已有下载会保留。')
        except httpx.RequestError:
            state['error'] = '无法连接下载服务，请检查网络后重试。已有下载会保留。'
        except Exception as error:
            state['error'] = str(error)
        finally:
            state['busy'] = False
            operation.release()

    @app.before_request
    def local_request_only():
        if request.host.split(':', 1)[0] not in {'127.0.0.1', 'localhost'}:
            return '安装页面仅限本机访问。', 403
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            return '安装请求来源无效。', 403

    @app.route('/', methods=['GET', 'POST'])
    def home():
        if request.method == 'POST':
            if not secrets.compare_digest(request.form.get('token', ''), token):
                return '安装请求已失效。', 403
            action = request.form.get('action')
            if action == 'cancel':
                if not state.get('cancellable'):
                    return '程序正在切换或验证，完成后即可关闭安装器。', 409
                cancelled.set()
                state['message'] = '正在停止准备，已校验的下载会保留。'
                return redirect('/')
            if action == 'close' and not state['busy']:
                response = make_response('安装器已关闭，可以关闭此页面。')
                shutdown = app.config.get('SHUTDOWN')
                if shutdown:
                    # WSGI closes the response after writing/flushing its body.
                    # Stopping the server before then can terminate the daemon
                    # request thread and truncate the browser acknowledgement.
                    response.call_on_close(lambda: threading.Thread(
                        target=shutdown, daemon=True).start())
                return response
            if action not in {'prepare', 'install', 'recover'}:
                return '未知安装操作。', 400
            if operation.acquire(blocking=False):
                cancelled.clear()
                if action == 'prepare':
                    target = Path(request.form.get('target', '')).expanduser()
                    root = Path(request.form.get('data_root', '')).expanduser()
                    if not target.is_absolute() or not root.is_absolute():
                        operation.release()
                        return '请填写完整的本地路径。', 400
                    offline = request.form.get('manifest_path', '').strip()
                    if offline and not Path(offline).expanduser().is_absolute():
                        operation.release()
                        return '请填写离线清单的完整路径。', 400
                    context.update(target=target, root=root, manifest_path=Path(offline).expanduser() if offline else None)
                state.update(busy=True, error='', cancellable=True)
                threading.Thread(target=task, args=(action,), daemon=True).start()
            return redirect('/')
        state['recovery'] = (context['root'] / 'updates/component-install-journal.json').exists()
        return render_template_string(PAGE, state=state, token=token,
                                      target=context['target'], data_root=context['root'],
                                      manifest_path=context['manifest_path'] or '')
    return app


def main(argv=None):
    check_current()
    from .paths import AppPaths
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=AppPaths.system_default().data_root)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--release-manifest', type=Path, help='离线签名清单；组件文件须放在同一目录')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--check-runtime', type=Path)
    args = parser.parse_args(argv)
    platform = 'windows-x86_64' if sys.platform == 'win32' else 'macos-arm64'
    config_path = Path(__file__).parent / 'adapters/update_config.json'
    if not config_path.is_file() and not getattr(sys, 'frozen', False):
        config_path = Path(__file__).resolve().parents[3] / 'packaging/update_config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    target = args.target or (Path(os.environ['LOCALAPPDATA']) / 'Programs/KnowledgeDistiller'
        if platform == 'windows-x86_64' else Path('/Applications/知识蒸馏器.app'))
    tools = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'tools'
    if args.check_runtime:
        import hashlib
        decoder = tools / ('hpatchz.exe' if platform == 'windows-x86_64' else 'BinaryDelta')
        if not decoder.is_file():
            raise UpdateError('安装器缺少差量解码器。')
        args.check_runtime.write_text(json.dumps({'python': check_current(), 'platform': platform,
            'decoder_sha256': hashlib.sha256(decoder.read_bytes()).hexdigest(),
            'public_key_present': bool(config['public_key'])}, indent=2))
        return
    app = create_installer(target=target, data_root=args.data_dir, platform=platform,
        public_key=config['public_key'], manifest_path=args.release_manifest,
        manifest_url=config['feed_url'].rsplit('/', 1)[0] + '/release-' + platform + '.json',
        binary_delta=tools / 'BinaryDelta', windows_tools=tools)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    app.config['SHUTDOWN'] = server.shutdown
    url = 'http://127.0.0.1:' + str(server.server_port)
    print(url, flush=True)
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == '__main__':
    main()
