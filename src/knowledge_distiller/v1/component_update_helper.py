"""Prepare a component candidate before requesting an idle application's exit."""
import json
import os
from pathlib import Path
import signal
import sys
import time
import webbrowser
from uuid import uuid4

import httpx
from .component_assembly import ComponentAssembly
from .component_install import install, finalize_install
from .component_release import MAX_MANIFEST
from .updates import UpdateError, validate_install_paths


def request_exit(root, plan, platform):
    state = json.loads((root / '.desktop-instance.json').read_text(encoding='utf-8'))
    if state['pid'] != plan['parent_pid']:
        raise UpdateError('申请更新的应用进程已变化。')
    url = 'http://127.0.0.1:' + str(state['port']) + '/settings/updates/status'
    deadline = time.monotonic() + 10
    while True:
        status = httpx.get(url, timeout=3).json()
        if status.get('token') != plan['request_token']:
            raise UpdateError('更新请求身份已变化。')
        if status['phase'] == 'installing':
            break
        if time.monotonic() > deadline:
            raise UpdateError('应用尚未进入更新状态。')
        time.sleep(.1)
    # The application has already reserved its worker and blocked new writes.
    if platform == 'windows-x86_64':
        from .windows_job import wait_for_exit
        (root / 'updates/shutdown-request').write_text(str(state['pid']))
        wait_for_exit(state['pid'], 120000)
    else:
        os.kill(state['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                os.kill(state['pid'], 0)
            except ProcessLookupError:
                return
            time.sleep(.1)
        raise UpdateError('应用尚未退出，程序未被替换。')


def run(plan_path):
    plan = json.loads(Path(plan_path).read_text(encoding='utf-8'))
    root = Path(plan['data_root'])
    target = Path(plan['info']['bundle'])
    platform = 'windows-x86_64' if sys.platform == 'win32' else 'macos-arm64'
    try:
        validate_install_paths(root, target)
        config = json.loads((Path(__file__).parent / 'adapters/update_config.json').read_text(encoding='utf-8'))
        tools = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'tools'
        with (root / 'updates/component-release.json').open('rb') as stream:
            envelope = stream.read(MAX_MANIFEST + 1)
        if platform == 'macos-arm64':
            import plistlib
            current = plistlib.loads((target / 'Contents/Info.plist').read_bytes())['CFBundleVersion']
        else:
            current = json.loads((target / '_internal/windows-version.json').read_text(encoding='utf-8'))['version']
        assembler = ComponentAssembly(root / 'components', root / 'updates/component-cache',
            platform=platform, public_key=config['public_key'], binary_delta=tools / 'BinaryDelta', windows_tools=tools)
        release, selected = assembler.prepare(envelope, installed=target, current=current)
        if release['version'] != plan['version']:
            raise UpdateError('安装目标已变化，请重新检查更新。')
        candidate, _ = assembler.assemble(release, selected,
            root / 'updates/component-attempts' / uuid4().hex, installed=target)
        request_exit(root, plan, platform)
        outcome = install(candidate, target, root, platform=platform, version=release['version'],
                target_identity=release['target_identity'])
        from .installer_platform import create_shortcut
        outcome = finalize_install(outcome, capability=assembler.capability_for(candidate),
            shortcut=lambda:create_shortcut(target,root))
        try:
            from .local_records import write_record
            write_record(root / 'updates/component-install-outcome.json', outcome)
        except Exception as error:
            print('安装已接受，收尾记录写入失败：' + str(error), file=sys.stderr)
        if outcome['activation']['status'] != 'ready':
            return 2
        if not plan.get('no_open'):
            state = json.loads((root / '.desktop-instance.json').read_text(encoding='utf-8'))
            webbrowser.open('http://127.0.0.1:' + str(state['port']) + '/')
        return 0
    except Exception as error:
        (root / 'updates').mkdir(parents=True, exist_ok=True)
        (root / 'updates/install-error.txt').write_text(str(error), encoding='utf-8')
        return 1
    finally:
        (root / 'updates/shutdown-request').unlink(missing_ok=True)
