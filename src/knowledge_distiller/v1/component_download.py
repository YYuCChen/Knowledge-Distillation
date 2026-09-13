"""Content-addressed cache with conservative HTTP resume and full-byte checks."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import httpx
from .component_release import _asset
from .file_lock import acquire
from .local_records import write_record
from .updates import UpdateError


class ComponentDownloader:
    def __init__(self, root, *, client=None):
        self.root = Path(root)
        self.client = client

    @staticmethod
    def valid(path, asset):
        if not path.is_file() or path.is_symlink() or path.stat().st_size != asset['size']:
            return False
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest() == asset['sha256']

    def fetch(self, asset, *, cancelled=lambda: False, progress=lambda received, total: None):
        # Asset must already belong to an authenticated release; this method
        # validates bytes and transport, it is not publisher authentication.
        _asset(asset)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise UpdateError('组件缓存目录无效。')
        name = asset['sha256']
        with closing(acquire(self.root / (name + '.lock'))):
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
            record = json.loads(metadata.read_text())
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
