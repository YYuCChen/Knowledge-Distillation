"""Windows desktop lifecycle for the existing local web composition root."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import subprocess
import shutil
import time
import threading
import webbrowser

from werkzeug.serving import make_server

from .app import AppPaths, create_application
from .file_lock import acquire
from .local_address import LocalAddressBindError, install_boundary, load, save


def default_data_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'Knowledge Distiller'


def configure_runtime():
    if getattr(sys, 'frozen', False):
        root = Path(sys._MEIPASS)
        os.environ['PATH'] = str(root / 'bin') + os.pathsep + os.environ.get('PATH', '')
    os.environ['PYTHONUTF8'] = '1'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'


def serve(paths, port=None, *, before_app=None, configure_app=None, start_workers=True):
    address = load(paths.data_root)
    port = address.port if port is None else port
    try:
        server = make_server('127.0.0.1', port, lambda e, s: [], threaded=True)
    except SystemExit as error:
        raise LocalAddressBindError(f'本地端口 {port} 无法绑定，可能已被占用。') from error
    try:
        if before_app is not None:
            before_app(server.server_port)
        app = create_application(paths, start_workers=start_workers)
        install_boundary(app, address, server.server_port)
        if configure_app is not None:
            configure_app(app)
        server.app = app
    except BaseException:
        server.server_close()
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True, name='local-web')
    thread.start()
    return app, server, thread


def restart_command(args, root):
    if getattr(sys, 'frozen', False):
        command = [sys.executable]
    else:
        command = [sys.executable, '-m', 'knowledge_distiller.v1.windows_app']
    command += ['--data-dir', str(root), '--restart-wait-pid', str(os.getpid())]
    if args.no_open:
        command.append('--no-open')
    if args.smoke_seconds is not None:
        command += ['--smoke-seconds', str(args.smoke_seconds)]
    # An explicit test/diagnostic port must not override newly saved settings.
    return command


def install_restart(app, args, root, log_path, restart):
    request_lock = threading.Lock()
    pending = False

    def request_restart(new_address):
        nonlocal pending
        from .local_address import LocalAddressError
        with request_lock:
            worker = app.config['KNOWLEDGE_DISTILLER_WORKER']
            if pending or app.extensions['qwen_component'].status()['busy'] or not worker.reserve_for_update():
                raise LocalAddressError('local_address_busy')
            try:
                previous = load(root)
                save(root, new_address)
            except Exception:
                worker.release_update()
                raise
            try:
                with log_path.open('ab') as output:
                    subprocess.Popen(restart_command(args, root),
                                     creationflags=(subprocess.CREATE_BREAKAWAY_FROM_JOB |
                                                    subprocess.CREATE_NO_WINDOW),
                                     close_fds=True, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=output)
            except Exception:
                try:
                    save(root, previous)
                finally:
                    worker.release_update()
                raise
            pending = True
            # Let the settings response reach the browser before shutdown.
            timer = threading.Timer(0.75, restart.set)
            timer.daemon = True
            timer.start()

    app.config['KNOWLEDGE_DISTILLER_RESTART'] = request_restart



def install_updates(app,args,root,log_path,restart):
    updates=app.extensions['updates']
    worker=app.config['KNOWLEDGE_DISTILLER_WORKER']
    def begin():
        from .updates import UpdateError,validate_install_paths
        validate_install_paths(root,updates.info['bundle'])
        if app.extensions['qwen_component'].status()['busy'] or not worker.reserve_for_update():
            raise UpdateError('蒸馏、整理或模型安装结束后才能更新。')
        try:
            updates.root.mkdir(parents=True,exist_ok=True)
            for name in ('install-result.json','install-error.txt','shutdown-request'):
                (updates.root/name).unlink(missing_ok=True)
            plan=updates.root/'install-plan.json'
            plan.write_text(json.dumps({'data_root':str(root),'info':updates.info,'parent_pid':os.getpid(),
                            'version':updates.release['version'],'asset_name':updates.release['selected']['name'],
                            'no_open':args.no_open}),encoding='utf-8')
            helper=updates.root/'update-helper.exe'
            shutil.copy2(Path(updates.info['bundle'])/'update-helper.exe',helper)
            with log_path.open('ab') as output:
                process=subprocess.Popen([str(helper),str(plan)],creationflags=(subprocess.CREATE_BREAKAWAY_FROM_JOB|subprocess.CREATE_NO_WINDOW),
                                         close_fds=True,stdin=subprocess.DEVNULL,stdout=output,stderr=output)
        except Exception:
            worker.release_update()
            raise
        def watch():
            process.wait()
            if not restart.is_set():
                with updates.lock:
                    updates.phase='error'
                    try: updates.error=(updates.root/'install-error.txt').read_text(encoding='utf-8')
                    except OSError: updates.error='安装未完成，当前版本已保留，请重试。'
                worker.release_update()
        threading.Thread(target=watch,daemon=True,name='update-install-result').start()
    if updates.info['bundle'] and (Path(updates.info['bundle'])/'update-helper.exe').is_file():
        updates.install=begin
        updates.block_reason=lambda: ('蒸馏或整理任务结束后可安装。' if not worker.update_ready() else '模型安装结束后可安装。' if app.extensions['qwen_component'].status()['busy'] else '')
    if not args.update_handshake:
        error_file=updates.root/'install-error.txt'
        if error_file.is_file():
            updates.error=error_file.read_text(encoding='utf-8')
            updates.phase='error'
        elif updates.info['feed_url'] and updates.info['public_key']:
            updates.start('check',automatic=True)


def main(argv=None):
    configure_runtime()
    parser = argparse.ArgumentParser(description='知识蒸馏器 Windows 应用')
    parser.add_argument('--data-dir', type=Path, default=default_data_root())
    parser.add_argument('--port', type=int)
    parser.add_argument('--restart-wait-pid', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--smoke-seconds', type=float, help=argparse.SUPPRESS)
    parser.add_argument('--update-handshake', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-runtime', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-ocr-image', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-pdf', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-epub', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-audio', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-offline', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.restart_wait_pid is not None:
        from .windows_job import wait_for_exit
        wait_for_exit(args.restart_wait_pid)
    root = args.data_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / 'application.log'
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=2, encoding='utf-8')])
    if sys.stdout is None:
        sys.stdout = log_path.open('a', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = sys.stdout
    if args.check_runtime:
        from .runtime_probe import check
        if args.check_offline:
            import socket
            os.environ['HF_HUB_OFFLINE'] = '1'
            def denied(*args, **kwargs):
                raise RuntimeError('network_disabled_for_explicit_runtime_check')
            original_connect = socket.socket.connect
            def offline_connect(sock, address):
                if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
                    return original_connect(sock, address)
                return denied()
            socket.socket.connect = offline_connect
            socket.create_connection = denied
        return check(args.check_runtime, args.check_audio, ocr_image=args.check_ocr_image,
                     pdf=args.check_pdf, epub=args.check_epub, component_root=root / 'components/qwen')
    if not args.update_handshake:
        from .component_install import require_recovered
        require_recovered(root)
        try:
            update_lock=acquire(root/'.update.lock')
            update_lock.close()
        except BlockingIOError:
            return 1
        journal=root/'updates/install-journal.json'
        if journal.is_file():
            helper=root/'updates/update-helper.exe'
            subprocess.Popen([str(helper),'--recover',str(root/'updates/install-plan.json'),str(os.getpid())],
                             creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
            return 0
    state = root / '.desktop-instance.json'
    try:
        lock = acquire(root / '.instance.lock')
    except BlockingIOError:
        try:
            port = json.loads(state.read_text(encoding='utf-8'))['port']
            if type(port) is int and 1 <= port <= 65535 and not args.no_open:
                webbrowser.open(f'http://127.0.0.1:{port}/')
        except (OSError, ValueError, KeyError):
            pass
        return 0
    app = server = None
    restart = threading.Event()
    try:
        address = load(root)
        def prepare_processes(port):
            # The bound socket queues the browser request until the server is
            # ready. Launch the user's browser before assigning our job, then
            # assign the job before the worker can resume queued tasks.
            if not args.no_open and not args.update_handshake:
                webbrowser.open(f'http://{address.host if args.port is None else "127.0.0.1"}:{port}/')
            from .windows_job import own_children
            own_children()
        def configure_app(app):
            install_restart(app, args, root, log_path, restart)
            install_updates(app,args,root,log_path,restart)

        while True:
            try:
                app, server, thread = serve(AppPaths(root), args.port,
                                            before_app=prepare_processes,
                                            configure_app=configure_app, start_workers=not args.update_handshake)
                break
            except LocalAddressBindError:
                if args.no_open:
                    raise
                from tkinter import Tk, simpledialog
                from .local_address import validate
                prompt = Tk()
                prompt.withdraw()
                try:
                    selected = args.port if args.port is not None else address.port
                    port = simpledialog.askinteger(
                        '本地端口无法使用',
                        f'端口 {selected} 可能正被其他程序占用。\n关闭占用程序后重试，或填写新的固定端口：',
                        initialvalue=selected, minvalue=1024, maxvalue=65535, parent=prompt)
                finally:
                    prompt.destroy()
                if port is None:
                    return 1
                address = validate(address.name, port)
                save(root, address)
                args.port = None
        url = f'http://{address.host if args.port is None else "127.0.0.1"}:{server.server_port}/'
        state.write_text(json.dumps({'port': server.server_port, 'pid': os.getpid()}), encoding='utf-8')
        def poll_update():
            updates=app.extensions['updates']
            try:
                if (updates.root/'shutdown-request').read_text()==str(os.getpid()): restart.set()
            except FileNotFoundError: pass
            accepted=args.update_handshake and args.update_handshake.is_file() and args.update_handshake.read_text()=='accepted'
            if args.update_handshake and not accepted:
                journal=updates.root/'install-journal.json'
                if journal.is_file():
                    accepted=json.loads(journal.read_text(encoding='utf-8'))['phase']=='accepted'
                    if not accepted:
                        try:
                            abandoned=acquire(root/'.update.lock')
                        except BlockingIOError:
                            pass
                        else:
                            abandoned.close()
                            subprocess.Popen([str(updates.root/'update-helper.exe'),'--recover',str(updates.root/'install-plan.json'),str(os.getpid())],
                                             creationflags=(subprocess.CREATE_BREAKAWAY_FROM_JOB|subprocess.CREATE_NO_WINDOW),close_fds=True)
                            restart.set()
            if accepted:
                args.update_handshake=None
                app.config['KNOWLEDGE_DISTILLER_WORKER'].start()
                app.extensions['feishu'].start()
                updates.phase='idle'
                if not args.no_open: webbrowser.open(url)
                updates.start('check',automatic=True)
        if args.smoke_seconds is not None or args.no_open:
            deadline=time.monotonic()+args.smoke_seconds if args.smoke_seconds is not None else float('inf')
            while not restart.wait(.1) and time.monotonic()<deadline:
                poll_update()
        else:
            import tkinter as tk
            from tkinter import ttk
            window = tk.Tk()
            icon = (Path(sys._MEIPASS) / 'assets/app-icon.ico' if getattr(sys, 'frozen', False)
                    else Path(__file__).resolve().parents[3] / 'packaging/assets/app-icon.ico')
            window.iconbitmap(default=str(icon))
            window.title('知识蒸馏器')
            window.geometry('440x200')
            window.resizable(False, False)
            ttk.Label(window, text='知识蒸馏器本地服务正在运行', font=('Microsoft YaHei UI', 13)).pack(pady=(24, 8))
            ttk.Label(window, text='关闭此窗口即可退出服务。').pack(pady=4)
            ttk.Button(window, text='打开浏览器', command=lambda: webbrowser.open(url)).pack(pady=6)
            ttk.Button(window, text='退出知识蒸馏器', command=window.destroy).pack(pady=6)
            def check_restart():
                poll_update()
                if restart.is_set():
                    window.destroy()
                else:
                    window.after(100, check_restart)
            window.after(100, check_restart)
            window.mainloop()
        return 0
    except Exception:
        logging.exception('Windows application failed')
        if not args.no_open:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'启动失败，请查看日志：\n{log_path}', '知识蒸馏器', 16)
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if app is not None:
            app.config['KNOWLEDGE_DISTILLER_CLOSE_FEISHU']()
            app.config['KNOWLEDGE_DISTILLER_WORKER'].stop()
            app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()
        state.unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
