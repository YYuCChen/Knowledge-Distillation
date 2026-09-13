"""Application update UI backed by the same component planner as the installer."""
import json
from pathlib import Path

import httpx
from .component_assembly import ComponentAssembly
from .component_release import MAX_MANIFEST
from .local_records import write_record
from .updates import Updates, UpdateError


class ComponentUpdates(Updates):
    def __init__(self, data_root, *, info, clock=None):
        # Do not interpret the old XML cache as a component release.
        super().__init__(data_root, info={**info, 'feed_url': '', 'public_key': ''},
                         **({'clock': clock} if clock is not None else {}))
        self.info = info
        self.record = {}
        self.phase, self.error = 'idle', ''
        self.protocol_release = None
        self.plan = None
        self.platform = 'windows-x86_64' if info.get('windows_update') else 'macos-arm64'
        self.assembler = ComponentAssembly(Path(data_root) / 'components', self.root / 'component-cache',
            platform=self.platform, public_key=info['public_key'])
        try:
            self.record = json.loads((self.root / 'component-state.json').read_text())
        except (OSError, ValueError):
            pass

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        write_record(self.root / 'component-state.json', self.record)

    def full_update_required(self):
        return False

    def snapshot(self):
        result = super().snapshot()
        result['component_updates'] = True
        return result

    def _run(self, action):
        try:
            if action == 'check':
                url = self.info['feed_url'].rsplit('/', 1)[0] + '/release-' + self.platform + '.json'
                with httpx.stream('GET', url, follow_redirects=True, timeout=60) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes(65536):
                        content.extend(chunk)
                        if len(content) > MAX_MANIFEST:
                            raise UpdateError('发行清单过大。')
                release, plan = self.assembler.prepare(bytes(content), installed=self.info['bundle'],
                                                       current=self.info['version'])
                # Same version can still need repair if its bytes or models differ.
                same = (release['version'] == self.info['version'] and
                        plan.source == 'current' and not plan.assets)
                with self.lock:
                    self.root.mkdir(parents=True, exist_ok=True)
                    temporary = self.root / 'component-release.tmp'
                    temporary.write_bytes(content)
                    temporary.replace(self.root / 'component-release.json')
                    self.protocol_release, self.plan, self.feed = release, plan, bytes(content)
                    self.release = None if same else {'version': release['version'],
                        'display_version': release.get('product_version', release['version']),
                        'notes': release.get('notes', ''),
                        'selected': {'name': 'component-release.json', 'size': plan.download_bytes},
                        'full_reason': '复用已校验组件；必要时由固定基座重建'}
                    self.record['checked_at'] = self.clock()
                    self.save()
                    self.phase = 'latest' if same else 'available'
                    if not same and plan.download_bytes == 0:
                        self.phase = 'downloaded'
            elif action == 'download':
                if self.plan is None:
                    raise UpdateError('请重新检查更新。')
                completed = 0
                for asset in self.plan.assets:
                    if self.assembler.downloader.valid(self.assembler.downloader.root / asset['sha256'], asset):
                        continue
                    before = completed
                    self.assembler.downloader.fetch(asset,
                        progress=lambda received, total: setattr(self, 'received', before + received))
                    completed += asset['size']
                with self.lock:
                    self.phase = 'downloaded'
            else:
                raise UpdateError('组件发行无需改用完整包，请重新检查。')
        except httpx.HTTPStatusError as error:
            with self.lock:
                self.phase = 'error'
                self.error = ('此平台的组件发行清单暂不可用，请稍后重试。' if error.response.status_code == 404
                              else '下载服务返回 HTTP ' + str(error.response.status_code) + '，请稍后重试。')
        except Exception as error:
            with self.lock:
                self.phase = 'error'
                self.error = str(error) if isinstance(error, UpdateError) else '更新检查或下载未完成，请检查网络及空间后重试。'
        finally:
            with self.lock:
                self.busy = False

    def request_install(self):
        with self.lock:
            if self.busy or self.phase != 'downloaded' or not self.install or not self.plan:
                raise UpdateError('请先完成组件下载。')
            for asset in self.plan.assets:
                if not self.assembler.downloader.valid(self.assembler.downloader.root / asset['sha256'], asset):
                    raise UpdateError('组件缓存已变化，请重新检查并下载。')
            self.last_action = 'install'
            self.install()
            self.phase = 'installing'
