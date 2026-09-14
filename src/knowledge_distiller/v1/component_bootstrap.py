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
from flask import Flask, request, render_template_string, redirect, make_response, jsonify, send_file
from werkzeug.serving import make_server

from .adapters.python_policy import check_current
from .component_assembly import ComponentAssembly
from .component_install import install, recover, finalize_install
from .component_release import MAX_MANIFEST
from .component_download import ComponentDownloader
from .install_problem import problem_from
from .installer_platform import pick_path, create_shortcut, manual_command, KINDS
from .updates import UpdateError, validate_install_paths


PAGE = (Path(__file__).parent / 'installer_assets/page.html').read_text(encoding='utf-8')


def create_installer(*, target, data_root, platform, public_key, manifest_url,
                     binary_delta=None, windows_tools=None, manifest_path=None):
    app = Flask(__name__)
    token = secrets.token_urlsafe(32)
    state = {'steps': ['pending'] * 6, 'seq': 0, 'accepted': False, 'busy': False, 'ready': False, 'complete': False, 'error': '',
             'message': '检查当前版本和可复用组件后，显示本次需要下载的内容。'}
    context = {'target': Path(target), 'root': Path(data_root), 'candidate': None, 'manifest_path': Path(manifest_path) if manifest_path else None}
    operation = threading.Lock()
    cancelled = threading.Event()

    def event(stage, **details):
        index = {'paths':0,'prepare':1,'verify':2,'install':3,'startup':4,'complete':5}[stage]
        state['seq'] += 1
        for previous in range(index):
            if state['steps'][previous] == 'working': state['steps'][previous] = 'complete'
        state['steps'][index] = 'complete' if stage == 'complete' else 'working'
        state.update(stage=stage, **details)

    def publish_outcome(outcome):
        context['outcome'] = outcome
        state.update(accepted=bool(outcome.get('accepted')), complete=outcome.get('activation',{}).get('status') == 'ready',
            ready=False, warnings=outcome.get('warnings', []),
            manual_command=manual_command(context['target'],context['root']) if platform == 'windows-x86_64' else '')
        if state['complete']: event('complete')
        else: state['steps'][4] = 'attention'

    def open_product():
        from .component_install import confirm_activation
        outcome = context.get('outcome', {})
        if not outcome.get('accepted') or outcome.get('activation',{}).get('status') != 'ready':
            raise UpdateError('请先恢复安装并完成启动检查。')
        confirm_activation(context['root'], outcome)
        instance = json.loads((context['root']/'.desktop-instance.json').read_text(encoding='utf-8'))
        webbrowser.open('http://127.0.0.1:' + str(instance['port']) + '/')


    def task(action):
        try:
            root, target = context['root'], context['target']
            if action == 'recover':
                state['cancellable'] = False
                event('startup')
                outcome = recover(target, root)
                if outcome.get('accepted'):
                    capability = context['assembler'].capability_for(context['candidate']) if context.get('candidate') is not None else None
                    outcome = finalize_install(outcome, capability=capability, shortcut=lambda:create_shortcut(target,root))
                    publish_outcome(outcome)
                    state.update(complete=outcome['activation']['status'] == 'ready', ready=False,
                        message='安装已接受。' + ' '.join(outcome.get('warnings', [])))
                else:
                    state.update(message='中断的安装已恢复，可以重新检查安装内容。', ready=False)
                return
            if action == 'prepare':
                state['steps'] = ['pending'] * 6
                event('paths')
                validate_install_paths(root, target)
                current = '0'
                if target.exists():
                    if platform == 'macos-arm64':
                        import plistlib
                        current = plistlib.loads((target / 'Contents/Info.plist').read_bytes())['CFBundleVersion']
                    else:
                        current = json.loads((target / '_internal/windows-version.json').read_text(encoding='utf-8'))['version']
                state['steps'][0] = 'complete'
                event('prepare')
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
                state['steps'][1] = 'pending'
                state.update(ready=True, version=release['version'],
                    download=f'{plan.download_bytes / 1024**2:.1f} MB',
                    message='发行签名与可复用组件已检查。确认后将准备组件并切换程序。')
            elif action == 'install':
                if not state['ready']:
                    raise UpdateError('请先检查安装内容。')
                release = context['release']
                if context['candidate'] is None:
                    event('prepare', bytes_done=0, bytes_total=0, asset='')
                    state['message'] = '正在下载并校验组件，已有可用内容会复用。'
                    candidate, metrics = context['assembler'].assemble(release, context['plan'],
                        root / 'updates/component-attempts' / uuid4().hex,
                        installed=target if target.exists() else None,
                        cancelled=cancelled.is_set, event=event,
                        progress=lambda received, total: state.update(
                            bytes_done=received, bytes_total=total))
                    context['candidate'] = candidate
                if cancelled.is_set():
                    raise InterruptedError('安装准备已取消，已有下载会保留。')
                state['message'] = '候选程序已校验，正在安装并检查启动状态。'
                state['cancellable'] = False
                outcome = install(context['candidate'], target, root, platform=platform,
                        version=release['version'], target_identity=release['target_identity'], event=event)
                outcome = finalize_install(outcome, capability=context['assembler'].capability_for(context['candidate']),
                    shortcut=lambda:create_shortcut(target,root))
                publish_outcome(outcome)
                state.update(complete=outcome['activation']['status'] == 'ready', ready=False,
                    message=('知识蒸馏器已安装并通过启动检查。' if outcome['activation']['status'] == 'ready'
                             else '安装已接受，启动放行待恢复。') + ' '.join(outcome['warnings']))
        except Exception as error:
            journal = context['root'] / 'updates/component-install-journal.json'
            accepted = bool(context.get('outcome',{}).get('accepted'))
            data_state = 'unknown' if journal.exists() else 'restored' if state.get('stage') in {'install','startup'} else 'unchanged'
            problem = problem_from(error,stage=state.get('stage','prepare'),
                role='manifest' if action == 'prepare' and state.get('steps',[None])[0]=='complete' else
                     'data' if state.get('stage')=='paths' else 'candidate',
                accepted=accepted,data_state=data_state)
            state.update(error=str(problem), problem=problem.to_dict())
            for index, status in enumerate(state['steps']):
                if status == 'working': state['steps'][index] = 'cancelled' if problem.category=='cancelled' else 'attention'
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


    @app.get('/status')
    def status():
        state['recovery'] = (context['root'] / 'updates/component-install-journal.json').exists()
        return jsonify(state)

    @app.get('/installer-logo.svg')
    def installer_logo():
        return send_file(Path(__file__).parent / 'installer_assets/installer-logo.svg', mimetype='image/svg+xml')

    @app.post('/picker')
    def picker():
        body = request.get_json(silent=True) or {}
        if not secrets.compare_digest(str(body.get('token','')),token): return jsonify(status='forbidden'),403
        if body.get('kind') not in KINDS: return jsonify(status='invalid'),400
        if state['busy']: return jsonify(status='unavailable',message='正在安装，请完成后再选择位置。'),409
        result = pick_path(body['kind'])
        # An application picker selects a folder. macOS installation targets
        # retain the bundle name when the selected folder is not itself an app.
        if result.get('status') == 'selected' and body['kind'] == 'folder_app' and platform == 'macos-arm64':
            path = Path(result['path'])
            if path.suffix != '.app': result['path'] = str(path/'知识蒸馏器.app')
        return jsonify(result)

    @app.route('/', methods=['GET', 'POST'])
    def home():
        if request.method == 'POST':
            if not secrets.compare_digest(request.form.get('token', ''), token):
                return '安装请求已失效。', 403
            action = request.form.get('action')
            if action == 'open' and not state['busy']:
                try: open_product()
                except Exception as error: state['error'] = str(error)
                return redirect('/')
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
                state.update(busy=True, error='', cancellable=action != 'recover')
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
    if sys.platform == 'darwin':
        from AppKit import NSApplication
        from Foundation import NSOperationQueue
        native = NSApplication.sharedApplication()
        native.setActivationPolicy_(0)
        def shutdown():
            server.shutdown()
            NSOperationQueue.mainQueue().addOperationWithBlock_(lambda:native.terminate_(None))
        app.config['SHUTDOWN'] = shutdown
        threading.Thread(target=server.serve_forever, daemon=True).start()
        native.run()
    else:
        server.serve_forever()


if __name__ == '__main__':
    main()
