"""Content-addressed cache with conservative HTTP resume and full-byte checks."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit, unquote

import httpx
from .component_release import _asset
from .file_lock import acquire
from .local_records import write_record
from .updates import UpdateError


class ComponentDownloader:
    def __init__(self, root, *, client=None, offline_root=None):
        self.root = Path(root)
        self.client = client
        self.offline_root = Path(offline_root) if offline_root is not None else None

    @staticmethod
    def valid(path, asset):
        if not path.is_file() or path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400 or path.stat().st_size != asset['size']:
            return False
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest() == asset['sha256']

    def offline_asset(self, asset):
        if self.offline_root is None:
            return None
        root = self.offline_root
        if root.is_symlink() or (root.exists() and getattr(root.lstat(), 'st_file_attributes', 0) & 0x400):
            raise UpdateError('离线组件目录无效。')
        filename = unquote(urlsplit(asset['url']).path.rsplit('/', 1)[-1])
        names = [asset['sha256']]
        if filename and filename not in {'.', '..'} and not any(c in filename for c in '/\\:'):
            names.append(filename)
        return next((root / name for name in names if self.valid(root / name, asset)), None)

    def _import_offline(self, asset, cancelled, progress):
        target = self.root / asset['sha256']
        if self.valid(target, asset):
            return target
        source = self.offline_asset(asset)
        if source is None:
            raise UpdateError('离线组件缺失或校验失败，请将发行清单及其列出的组件放在同一目录后重试。')
        if shutil.disk_usage(self.root).free < asset['size'] + 16 * 1024**2:
            raise UpdateError('组件缓存空间不足。')
        handle, name = tempfile.mkstemp(prefix='.offline-', dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(handle, 'wb') as dst, source.open('rb') as src:
                count = 0
                while block := src.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('component_download_cancelled')
                    count += len(block)
                    if count > asset['size']:
                        raise UpdateError('离线组件已变化。')
                    dst.write(block)
                    progress(count, asset['size'])
                dst.flush()
                os.fsync(dst.fileno())
            if not self.valid(temporary, asset):
                raise UpdateError('离线组件已变化。')
            temporary.replace(target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def fetch(self, asset, *, cancelled=lambda: False, progress=lambda received, total: None):
        # Asset must already belong to an authenticated release; this method
        # validates bytes and transport, it is not publisher authentication.
        _asset(asset)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or getattr(self.root.lstat(), 'st_file_attributes', 0) & 0x400:
            raise UpdateError('组件缓存目录无效。')
        name = asset['sha256']
        lock_path = self.root / (name + '.lock')
        if lock_path.is_symlink() or (lock_path.exists() and getattr(lock_path.lstat(), 'st_file_attributes', 0) & 0x400):
            raise UpdateError('组件缓存锁路径无效。')
        with closing(acquire(lock_path)):
            if self.offline_root is not None:
                return self._import_offline(asset, cancelled, progress)
            if self.client is not None:
                return self._fetch(asset, self.client, cancelled, progress)
            with httpx.Client(follow_redirects=True, timeout=60) as client:
                return self._fetch(asset, client, cancelled, progress)

    def _fetch(self, asset, client, cancelled, progress):
        target = self.root / asset['sha256']
        part, metadata = target.with_suffix('.part'), target.with_suffix('.json')
        if any(path.is_symlink() for path in (target, part, metadata)):
            raise UpdateError('组件缓存路径无效。')
        if self.valid(target, asset):
            progress(asset['size'], asset['size'])
            return target
        target.unlink(missing_ok=True)
        offset, etag = 0, None
        try:
            record = json.loads(metadata.read_text(encoding='utf-8'))
            if record['asset'] == asset and part.is_file():
                offset = part.stat().st_size
                etag = record['etag']
                if not isinstance(etag, str) or not etag.startswith('"') or not etag.endswith('"'):
                    offset, etag = 0, None
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if part.is_file() and self.valid(part, asset):
            part.replace(target)
            metadata.unlink(missing_ok=True)
            return target
        if offset >= asset['size']:
            offset, etag = 0, None
        if shutil.disk_usage(self.root).free < asset['size'] - offset + 16 * 1024**2:
            raise UpdateError('组件下载空间不足。')
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers.update(Range=f'bytes={offset}-', **{'If-Range': etag})
        if cancelled():
            raise InterruptedError('component_download_cancelled')
        with client.stream('GET', asset['url'], headers=headers) as response:
            response.raise_for_status()
            if response.headers.get('content-encoding', 'identity') != 'identity':
                raise UpdateError('组件传输编码无效。')
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                if (not offset or not match or tuple(map(int, match.groups())) !=
                        (offset, asset['size'] - 1, asset['size'])
                        or response.headers.get('etag') != etag):
                    raise UpdateError('组件续传身份不匹配。')
            elif response.status_code == 200:
                offset = 0
            else:
                raise UpdateError('组件下载响应无效。')
            received = offset
            write_record(metadata, {'asset': asset, 'etag': response.headers.get('etag')})
            with part.open('ab' if offset else 'wb') as output:
                for block in response.iter_bytes(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('component_download_cancelled')
                    received += len(block)
                    if received > asset['size']:
                        raise UpdateError('组件下载超过签名大小。')
                    output.write(block)
                    progress(received, asset['size'])
                output.flush()
                os.fsync(output.fileno())
        if not self.valid(part, asset):
            part.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            raise UpdateError('组件下载内容校验失败。')
        part.replace(target)
        metadata.unlink(missing_ok=True)
        return target
