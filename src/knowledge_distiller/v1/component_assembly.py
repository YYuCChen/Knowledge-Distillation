"""Offline reconstruction after one shared authenticated release/download plan."""
from pathlib import Path
import json
import plistlib
import shutil
import subprocess
import time

from .component_attempt import create_attempt, bind_candidate
from .component_release import parse_release, plan_release
from .component_download import ComponentDownloader
from .docling_component import DoclingComponent, DoclingComponentError
from .program_tree import identity, extract_mac_base
from .updates import UpdateError
from .windows_platform import filesystem_path


class ComponentAssembly:
    def __init__(self, components_root, cache_root, *, platform, public_key, binary_delta=None,
                 windows_tools=None, downloader=None):
        self.attempts = {}
        self.components_root = Path(components_root)
        self.platform = platform
        self.public_key = public_key
        self.binary_delta = binary_delta
        self.windows_tools = windows_tools
        self.downloader = downloader or ComponentDownloader(cache_root)

    def prepare(self, envelope, *, installed=None, current='0'):
        release = parse_release(envelope, self.public_key, platform=self.platform, current=current)
        model = DoclingComponent(self.components_root)
        model_id = None
        try:
            model.verify()
            model_id = model.identity
        except DoclingComponentError:
            if installed is not None:
                old = Path(installed) / ('Contents/Resources/docling-models'
                    if self.platform == 'macos-arm64' else '_internal/docling-models')
                try:
                    model.import_existing(old)
                    model_id = model.identity
                except (DoclingComponentError, OSError):
                    pass  # The signed model asset is the explicit repair path.
        current_id = None
        if installed is not None and Path(installed).is_dir():
            current_id = identity(installed, self.platform)
        cached = {asset['sha256'] for asset in [release['docling'], release['base'], *release['deltas']]
            if self.downloader.valid(self.downloader.root / asset['sha256'], asset)
            or self.downloader.offline_asset(asset) is not None}
        return release, plan_release(release, verified_current_identity=current_id,
            verified_model_identity=model_id, verified_cached_assets=cached)

    def assemble(self, release, plan, work_root, *, installed=None, cancelled=lambda: False,
                 progress=lambda received, total: None, event=lambda *a, **k:None):
        """Never stops or replaces an installed application; produces a checked candidate."""
        started = time.monotonic()
        root = Path(work_root)
        excluded = [self.components_root, self.downloader.root]
        if installed is not None: excluded.append(installed)
        capability = create_attempt(root, excluded=excluded)
        self.attempts[str(root)] = capability
        candidate = root / ('candidate.app' if self.platform == 'macos-arm64' else 'candidate')
        if candidate.exists():
            raise UpdateError('安装暂存目录已存在，请先恢复上次安装。')
        assets = {}
        for asset in plan.assets:
            if cancelled():
                raise InterruptedError('component_assembly_cancelled')
            from urllib.parse import urlsplit
            event('prepare', asset=urlsplit(asset['url']).path.rsplit('/',1)[-1], bytes_done=0, bytes_total=asset['size'])
            assets[asset['sha256']] = self.downloader.fetch(asset, cancelled=cancelled, progress=progress)
        model = DoclingComponent(self.components_root)
        if release['docling']['sha256'] in assets:
            model.import_archive(assets[release['docling']['sha256']])
        model.verify()
        baseline = Path(installed) if plan.source == 'current' and installed is not None else None
        if plan.source == 'base':
            baseline = root / ('base.app' if self.platform == 'macos-arm64' else 'base')
            base = release['base']
            if self.platform == 'macos-arm64':
                extract_mac_base(assets[base['sha256']], baseline, expected_identity=base['identity'],
                                 maximum_bytes=base['unpacked_size'])
            else:
                from .windows_delta import stage_payload
                stage_payload(assets[base['sha256']], root / 'absent', baseline,
                              version=base['version'], current='0', tools_dir=self.windows_tools)
        if baseline is None:
            raise UpdateError('缺少已验证程序基线。')
        baseline_id = identity(baseline, self.platform)
        if baseline_id == release['target_identity']:
            shutil.copytree(filesystem_path(baseline), filesystem_path(candidate), symlinks=True)
        else:
            delta = next((asset for asset in release['deltas'] if asset['from_identity'] == baseline_id), None)
            if delta is None or delta['sha256'] not in assets:
                raise UpdateError('程序基线已变化，请重新规划安装。')
            if self.platform == 'windows-x86_64':
                from .windows_delta import stage_payload
                current = json.loads((baseline / '_internal/windows-version.json').read_text(encoding='utf-8'))['version']
                stage_payload(assets[delta['sha256']], baseline, candidate,
                              version=release['version'], current=current, tools_dir=self.windows_tools)
            else:
                if self.binary_delta is None:
                    raise UpdateError('安装器缺少 Mac 差量解码器。')
                subprocess.run([str(self.binary_delta), 'apply', str(baseline), str(candidate),
                                str(assets[delta['sha256']])], check=True, timeout=900,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        event('verify', bytes_done=0, bytes_total=0, asset='')
        if identity(candidate, self.platform) != release['target_identity']:
            raise UpdateError('最终程序内容校验失败，当前应用未改变。')
        if self.platform == 'macos-arm64':
            info = plistlib.loads((candidate / 'Contents/Info.plist').read_bytes())
            version = info['CFBundleVersion']
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(candidate)], check=True)
        else:
            version = json.loads((candidate / '_internal/windows-version.json').read_text(encoding='utf-8'))['version']
        if version != release['version']:
            raise UpdateError('最终程序版本不符。')
        bind_candidate(capability, candidate, release['target_identity'])
        return candidate, {'seconds': time.monotonic() - started, 'download_bytes': plan.download_bytes,
                           'target_identity': release['target_identity']}

    def capability_for(self, candidate):
        return self.attempts.get(str(Path(candidate).parent))
