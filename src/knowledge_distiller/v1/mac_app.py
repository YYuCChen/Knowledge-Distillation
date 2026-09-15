"""Native macOS lifecycle around the existing local web application."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import secrets
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from werkzeug.serving import make_server

from .app import AppPaths, create_application


def configure_bundled_runtime():
    if getattr(sys, 'frozen', False):
        root = Path(sys._MEIPASS)
        os.environ['PATH'] = str(root / 'bin') + ':/usr/bin:/bin:/usr/sbin:/sbin'
    os.environ.setdefault('LANG', 'en_US.UTF-8')
    os.environ.setdefault('LC_ALL', 'en_US.UTF-8')


def serve(paths, port=57740, *, start_workers=True):
    """Bind before constructing the worker; never share an occupied socket."""
    try:
        server = make_server('127.0.0.1', port, lambda e,s: [], threaded=True)
    except SystemExit as error:
        from .local_address import LocalAddressBindError
        raise LocalAddressBindError(f'本地端口 {port} 无法绑定，可能已被占用。') from error
    try:
        app = create_application(paths, start_workers=start_workers)
        from .local_address import install_boundary, load
        install_boundary(app, load(paths.data_root), server.server_port)
        server.app = app
    except BaseException:
        server.server_close()
        raise
    thread = threading.Thread(target=server.serve_forever, name='knowledge-distiller-web', daemon=True)
    thread.start()
    return app, server, thread



def reveal_browser(url, workspace, native_url, running_applications, activation_options,
                   browser_bundle_id=None):
    """Activate the browser only after an app-owned page has been detected."""
    applications = [app for app in running_applications() if not app.isTerminated()]
    if browser_bundle_id:
        for application in applications:
            if application.bundleIdentifier() == browser_bundle_id:
                application.activateWithOptions_(activation_options)
                return browser_bundle_id
    application_url = workspace.URLForApplicationToOpenURL_(native_url(url))
    if application_url is not None:
        for application in applications:
            if application.bundleURL() == application_url:
                application.activateWithOptions_(activation_options)
                return application.bundleIdentifier()
    return None


class NativeReopener:
    """Keep Cocoa calls on the owner loop and all condition waits off that loop."""
    def __init__(self, pages, open_page, activate, completed):
        self.pages, self.open_page, self.activate, self.completed = pages, open_page, activate, completed
        self.calls = queue.Queue()
        self.results = queue.Queue()
        self.running = False
        self.closed = False

    def _native(self, callback, argument):
        done, result = threading.Event(), []
        self.calls.put((callback, argument, done, result))
        while not done.wait(0.1):
            if self.closed:
                raise RuntimeError('desktop closing')
        if isinstance(result[0], BaseException):
            raise result[0]
        return result[0]

    def request(self, explicit_request=None):
        # Called by the Cocoa main loop, including the queued second launcher.
        if self.running or self.closed:
            return False
        self.running = True
        def run():
            try:
                outcome = self.pages.reopen(
                    lambda nonce: self._native(self.open_page, nonce),
                    lambda page: self._native(self.activate, page),
                    explicit_request=explicit_request)
                self.results.put(outcome)
            except Exception:
                logging.exception('Desktop reopen failed')
                from .desktop_pages import ReopenOutcome
                self.results.put(ReopenOutcome('failed', secrets.token_urlsafe(18), reason='native_callback_failed'))
        threading.Thread(target=run, daemon=True, name='desktop-reopen').start()
        return True

    def poll(self):
        while not self.calls.empty():
            callback, argument, done, result = self.calls.get_nowait()
            try:
                result.append(callback(argument))
            except Exception as error:
                result.append(error)
            finally:
                done.set()
        while not self.results.empty():
            outcome = self.results.get_nowait()
            self.running = False
            self.completed(outcome)


def request_reopen(port, token):
    """A second launcher asks the owning process; it never guesses browser state."""
    import urllib.request
    request = urllib.request.Request(f'http://127.0.0.1:{port}/desktop/reopen',
        data=json.dumps({'token':token}).encode(), headers={'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=2) as response:
        if response.status != 202:
            raise OSError('应用未接受页面唤起请求')


def main(argv=None):
    configure_bundled_runtime()
    if (sys.argv[1:] if argv is None else argv) == ['--bilibili-worker']:
        from .bilibili import _worker_main
        _worker_main()
        return
    if (sys.argv[1:] if argv is None else argv) == ['--feishu-worker']:
        from .feishu_socket import worker_main
        worker_main()
        return
    parser = argparse.ArgumentParser(description='知识蒸馏器 Mac 应用')
    default_root = AppPaths.mac_default().data_root
    if getattr(sys, 'frozen', False):
        import plistlib
        info = plistlib.loads((Path(sys.executable).resolve().parents[1]/'Info.plist').read_bytes())
        if info.get('CFBundleIdentifier') == 'local.knowledge-distiller.updater-test':
            default_root = Path(info['KDUpdateTestDataRoot'])
    parser.add_argument('--data-dir', type=Path, default=default_root)
    parser.add_argument('--port', type=int)
    parser.add_argument('--no-open', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--update-handshake', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-runtime', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-audio', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-ocr-image', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-pdf', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--check-epub', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.check_runtime:
        from .runtime_probe import check
        raise SystemExit(check(args.check_runtime,args.check_audio,ocr_image=args.check_ocr_image,
                              pdf=args.check_pdf,epub=args.check_epub,component_root=args.data_dir.expanduser().resolve()/"components"/"qwen"))
    paths = AppPaths(args.data_dir.expanduser().resolve())
    paths.data_root.mkdir(parents=True, exist_ok=True)
    from .local_address import LocalAddress, LocalAddressError, load as load_local_address, save as save_local_address
    try:
        local_address = load_local_address(paths.data_root)
    except LocalAddressError:
        local_address = None
    log_path = paths.data_root / 'application.log'
    handler = RotatingFileHandler(log_path,maxBytes=1_000_000,backupCount=2,encoding='utf-8')
    logging.basicConfig(level=logging.INFO,handlers=[handler])
    # Windowed apps may have no stdout/stderr. Keep diagnostics in owned storage.
    output = log_path.open('a', encoding='utf-8')
    sys.stdout = output
    sys.stderr = output
    from AppKit import NSApplication, NSMenu, NSMenuItem, NSWorkspace, NSAlert, NSApplicationActivationPolicyRegular, NSApplicationActivateIgnoringOtherApps, NSApplicationActivateAllWindows
    from Foundation import NSObject, NSURL, NSTimer
    native = NSApplication.sharedApplication()
    native.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    if not args.update_handshake:
        from .component_install import require_recovered
        try:
            require_recovered(paths.data_root)
        except (ValueError, OSError) as error:
            alert = NSAlert.alloc().init()
            alert.setMessageText_('请先恢复中断的安装')
            alert.setInformativeText_('请重新打开知识蒸馏器安装器，选择当前程序和数据目录，点击恢复中断的安装。')
            alert.runModal()
            return
    if local_address is None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_('本地访问地址配置无法读取')
        alert.setInformativeText_('恢复默认地址后可重新启动，知识和设置会保留。')
        alert.addButtonWithTitle_('恢复默认地址'); alert.addButtonWithTitle_('退出')
        if alert.runModal() != 1000:
            return
        local_address = LocalAddress()
        save_local_address(paths.data_root, local_address)
    startup_lock = (paths.data_root / '.update.lock').open('a')
    if not args.update_handshake:
        try:
            fcntl.flock(startup_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            alert=NSAlert.alloc().init();alert.setMessageText_('知识蒸馏器正在更新')
            alert.setInformativeText_('更新完成后会重新打开，请稍候。');alert.runModal()
            startup_lock.close()
            return
    lock = (paths.data_root / '.instance.lock').open('a')
    state_path = paths.data_root / '.desktop-instance.json'

    workspace = NSWorkspace.sharedWorkspace()
    browser_bundle_id = None

    def open_url(url):
        nonlocal browser_bundle_id
        application_url = workspace.URLForApplicationToOpenURL_(NSURL.URLWithString_(url))
        if application_url is not None:
            from Foundation import NSBundle
            bundle = NSBundle.bundleWithURL_(application_url)
            browser_bundle_id = bundle.bundleIdentifier() if bundle else None
        if not workspace.openURL_(NSURL.URLWithString_(url)):
            raise RuntimeError('浏览器未能打开应用页面')

    def reveal(url, bundle_id=None):
        # The worker never owns Cocoa objects; tick_ marshals its callbacks.
        if recovery_alert is not None:
            native.activateIgnoringOtherApps_(True)
            return False
        return reopener.request()


    try:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            state = json.loads(state_path.read_text())
            port = state['port']
            if type(port) is not int or not 1<=port<=65535:raise ValueError
            if not args.no_open:
                request_reopen(port, state['desktop_token'])
        except (OSError,KeyError,TypeError,ValueError):
            alert=NSAlert.alloc().init();alert.setMessageText_('知识蒸馏器已经运行')
            alert.setInformativeText_('请使用已打开的页面。当前数据目录已有服务运行。');alert.runModal()
        return
    from .local_address import LocalAddressBindError, validate as validate_local_address
    while True:
        selected_port = args.port if args.port is not None else local_address.port
        try:
            app,server,thread = serve(paths,selected_port,start_workers=not args.update_handshake)
            break
        except LocalAddressBindError:
            from AppKit import NSTextField
            alert=NSAlert.alloc().init();alert.setMessageText_(f'端口 {selected_port} 无法使用')
            alert.setInformativeText_('端口可能正被其他程序占用。关闭占用程序后重试，或在下方填写新的固定端口（1024–65535）。')
            field=NSTextField.alloc().initWithFrame_(((0,0),(260,24)))
            field.setStringValue_(str(selected_port));alert.setAccessoryView_(field)
            alert.addButtonWithTitle_('重新启动');alert.addButtonWithTitle_('退出')
            if alert.runModal()!=1000:return
            try:
                address=validate_local_address(local_address.name,field.stringValue())
            except LocalAddressError:
                continue
            save_local_address(paths.data_root,address)
            local_address=address;args.port=None
        except (Exception,SystemExit) as error:
            logging.exception('Desktop startup failed (%s)',type(error).__name__)
            alert=NSAlert.alloc().init();alert.setMessageText_('知识蒸馏器暂未启动')
            alert.setInformativeText_(f'请重新打开应用；若仍无法启动，可查看诊断日志：{log_path}');alert.runModal()
            return
    state_path.write_text(json.dumps({'port':server.server_port,'pid':os.getpid(),
                                      'desktop_token':app.extensions['desktop_pages'].token}))
    url = (f'http://{local_address.host}:{server.server_port}/' if local_address and args.port is None
           else f'http://127.0.0.1:{server.server_port}/')
    startup_lock.close()
    updates = app.extensions['updates']
    worker = app.config['KNOWLEDGE_DISTILLER_WORKER']

    def begin_install():
        from .updates import UpdateError, validate_install_paths
        import subprocess
        import shutil
        validate_install_paths(paths.data_root, updates.info['bundle'])
        if app.extensions['qwen_component'].status()['busy']:
            raise UpdateError('Qwen 组件正在安装，请完成后再更新应用。')
        if not worker.reserve_for_update():
            raise UpdateError('蒸馏或整理仍在进行，请完成后再安装。')
        try:
            plan = updates.root/'install-plan.json'
            plan.write_text(json.dumps({'data_root':str(paths.data_root), 'info':updates.info,
                                       'version':updates.release['version'], 'parent_pid':os.getpid(), 'request_token':updates.token,
                                       'asset_name':updates.release['selected']['name'],
                                       'no_open':args.no_open}))
            helper = updates.root/'update-helper'
            shutil.copy2(Path(updates.info['bundle'])/'Contents/MacOS/update-helper', helper)
            process = subprocess.Popen([str(helper), *(['--component'] if updates.info.get('component_updates') else []), str(plan)],
                                       start_new_session=True, stdout=output, stderr=output)
        except Exception:
            worker.release_update()
            raise
        def watch():
            result = process.wait()
            if result:
                with updates.lock:
                    updates.phase = 'error'
                    updates.error = '安装未完成，当前版本已保留。请重新检查后重试。'
                worker.release_update()
        threading.Thread(target=watch, daemon=True, name='update-install-result').start()

    if updates.info['bundle'] and (Path(updates.info['bundle'])/'Contents/Helpers/Updater.app/Contents/MacOS/update-cli').is_file():
        updates.install = begin_install
        updates.block_reason = lambda: ('下载完成，蒸馏或整理任务结束后可安装。' if not worker.update_ready()
            else '下载完成，Qwen 组件安装结束后可更新。' if app.extensions['qwen_component'].status()['busy'] else '')
    if not args.update_handshake and updates.info['feed_url'] and updates.info['public_key']:
        updates.start('check', automatic=True)

    stopped = False
    restart_request = []

    def request_restart(address):
        from .local_address import LocalAddressError
        if app.extensions['qwen_component'].status()['busy'] or not worker.reserve_for_update():
            raise LocalAddressError('local_address_busy')
        try:
            save_local_address(paths.data_root, address)
        except Exception:
            worker.release_update()
            raise
        restart_request[:] = [(address.url, time.monotonic() + 0.75)]

    app.config['KNOWLEDGE_DISTILLER_RESTART'] = request_restart

    def relaunch_after_exit():
        if getattr(sys, 'frozen', False):
            bundle = str(Path(sys.executable).resolve().parents[2])
            command = ['/usr/bin/open', '-n', bundle, '--args', '--data-dir', str(paths.data_root)]
        else:
            command = [sys.executable, '-c',
                       'from knowledge_distiller.v1.mac_app import main; main()',
                       '--data-dir', str(paths.data_root)]
        if args.no_open:
            command.append('--no-open')
        script = 'while kill -0 "$1" 2>/dev/null; do sleep 0.1; done; shift; exec "$@"'
        subprocess.Popen(['/bin/sh', '-c', script, 'kd-restart', str(os.getpid()), *command],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
    def stop():
        nonlocal stopped
        if stopped:return
        stopped = True
        reopener.closed = True
        server.shutdown();server.server_close()
        app.config['KNOWLEDGE_DISTILLER_CLOSE_FEISHU']()
        app.config['KNOWLEDGE_DISTILLER_WORKER'].stop()
        app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()
        state_path.unlink(missing_ok=True)
        lock.close();output.flush()

    recovery_alert = None
    recovery_outcome = None

    def completed(outcome):
        nonlocal recovery_alert, recovery_outcome
        logging.info('desktop outcome request=%s status=%s target=%s epoch=%s generation=%s received=%s visible=%s focused=%s reason=%s',
            outcome.request_id, outcome.status, outcome.target, outcome.connection_epoch,
            outcome.generation, outcome.received, outcome.visible, outcome.focused, outcome.reason)
        needs_recovery = outcome.status in {'unknown', 'failed'} or (
            outcome.status == 'online' and outcome.reason != 'opened_handshake')
        if not needs_recovery or recovery_alert is not None:
            return
        native.activateIgnoringOtherApps_(True)
        recovery_alert = NSAlert.alloc().init()
        recovery_alert.setMessageText_('未能显示已有页面' if outcome.status != 'failed' else '暂未打开产品页面')
        recovery_alert.setInformativeText_('旧页面可能仍保留。你可以重试显示，或另开产品页面；另开可能保留两个页面，原有输入不会被替换。')
        recovery_alert.addButtonWithTitle_('重试显示')
        recovery_alert.addButtonWithTitle_('另开产品页面')
        recovery_alert.addButtonWithTitle_('取消')
        recovery_outcome = outcome
        for button, selector in zip(recovery_alert.buttons(),
                                    ('retryDisplay:', 'openAnotherPage:', 'cancelRecovery:')):
            button.setTarget_(delegate)
            button.setAction_(selector)
        recovery_alert.layout()
        recovery_alert.window().makeKeyAndOrderFront_(None)
        # This is a nonmodal native window. The application timer and normal
        # event loop keep running while it is visible; no runModal/network wait.

    def finish_recovery(action):
        nonlocal recovery_alert, recovery_outcome
        if recovery_alert is None:
            return
        outcome = recovery_outcome
        recovery_alert.window().orderOut_(None)
        recovery_alert = recovery_outcome = None
        if action == 'retry':
            reopener.request()
        elif action == 'new':
            reopener.request(explicit_request=outcome.request_id)

    def activate_page(page):
        # The family is only an app-owned UA hint, never PID/ownership evidence.
        family = {'chrome': 'com.google.Chrome', 'safari': 'com.apple.Safari',
                  'edge': 'com.microsoft.edgemac', 'firefox': 'org.mozilla.firefox'}
        return reveal_browser(url, workspace, NSURL.URLWithString_, workspace.runningApplications,
            NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows,
            family.get(page.browser_hint) or browser_bundle_id)

    from urllib.parse import urlencode
    launch_bundles, browser_instances = {}, {}

    def open_product(nonce):
        logging.info('desktop open request time=%s request=%s', time.monotonic(), nonce)
        open_url(url + '?' + urlencode({'_desktop_launch': nonce}))
        if browser_bundle_id:
            launch_bundles[nonce] = browser_bundle_id

    def observe_owned_browsers():
        pages = app.extensions['desktop_pages']
        # Associate only launches sent by this app, and retain the exact native
        # instance object; UA family and a changed default are not exit proof.
        for nonce, bundle_id in list(launch_bundles.items()):
            matches = [browser for browser in workspace.runningApplications()
                       if browser.bundleIdentifier() == bundle_id and not browser.isTerminated()]
            if len(matches) != 1:
                # Multiple instances/profiles cannot be resolved by bundle ID.
                continue
            browser = matches[0]
            identity = f'{bundle_id}:{browser.processIdentifier()}:{browser.launchDate()}'
            pages.associate_launch(nonce, identity)
            browser_instances[identity] = browser
            del launch_bundles[nonce]
        for identity, browser in list(browser_instances.items()):
            if browser.isTerminated():
                pages.browser_exited(identity)
                del browser_instances[identity]

    reopener = NativeReopener(app.extensions['desktop_pages'], open_product,
                             activate_page, completed)

    class Delegate(NSObject):
        def applicationDidFinishLaunching_(self, notification):
            if not args.no_open and not args.update_handshake:
                reveal(url)
                state_path.write_text(json.dumps({'port':server.server_port,'pid':os.getpid(),
                                                  'browser_bundle_id':browser_bundle_id,
                                                  'desktop_token':app.extensions['desktop_pages'].token}))
        def applicationSupportsSecureRestorableState_(self,application):return True
        def applicationWillTerminate_(self,notification):stop()
        def tick_(self,timer):
            reopener.poll()
            observe_owned_browsers()
            if restart_request and time.monotonic() >= restart_request[0][1]:
                restart_request.clear()
                relaunch_after_exit()
                native.terminate_(None)
                return
            if app.extensions['desktop_pages'].take_request() and not args.no_open:
                reveal(url)
            if args.update_handshake and args.update_handshake.is_file():
                decision = args.update_handshake.read_text()
                if decision == 'accepted':
                    args.update_handshake = None
                    updates.phase = 'latest' if updates.feed and not updates.release else 'available' if updates.release else 'idle'
                    worker.start()
                    app.extensions['feishu'].start()
                    if not args.no_open:
                        reveal(url)
                    if updates.info['feed_url'] and updates.info['public_key']:
                        updates.start('check', automatic=True)
        def retryDisplay_(self,sender):finish_recovery('retry')
        def openAnotherPage_(self,sender):finish_recovery('new')
        def cancelRecovery_(self,sender):finish_recovery('cancel')
        def openHome_(self,sender):reveal(url)
        def openSettings_(self,sender):open_url(url+'settings')
        def applicationShouldHandleReopen_hasVisibleWindows_(self,application,visible):
            logging.info('desktop Dock callback time=%s', time.monotonic())
            if not args.no_open:reveal(url)
            return False

    delegate=Delegate.alloc().init();native.setDelegate_(delegate)
    menu=NSMenu.alloc().init()
    app_item=NSMenuItem.alloc().init();menu.addItem_(app_item)
    actions=NSMenu.alloc().initWithTitle_('知识蒸馏器')
    for title,selector,key in [('打开知识蒸馏器','openHome:','o'),('设置','openSettings:',',')]:
        entry=NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title,selector,key)
        entry.setTarget_(delegate);actions.addItem_(entry)
    actions.addItem_(NSMenuItem.separatorItem())
    actions.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('退出知识蒸馏器','terminate:','q'))
    app_item.setSubmenu_(actions);native.setMainMenu_(menu)
    timer=NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.25,delegate,'tick:',None,True)
    signal.signal(signal.SIGTERM,lambda signum,frame:native.terminate_(None))
    try:native.run()
    finally:
        timer.invalidate();stop()
