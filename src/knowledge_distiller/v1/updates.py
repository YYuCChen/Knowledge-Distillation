"""Signed update discovery and download; installation requires a separate action."""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import httpx
from Crypto.Signature import eddsa

SPARKLE = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'
FEED_LIMIT = 1024 * 1024


class UpdateError(ValueError):
    pass


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+){0,4}', value):
        raise UpdateError('更新版本格式无效。')
    return tuple(int(v) for v in value.split('.'))


def verify_bytes(data, signature, public_key):
    try:
        key = eddsa.import_public_key(base64.b64decode(public_key, validate=True))
        eddsa.new(key, 'rfc8032').verify(data, base64.b64decode(signature, validate=True))
    except (ValueError, TypeError) as error:
        raise UpdateError('更新签名验证失败，已保留当前版本。') from error


def parse_feed(data, public_key, current):
    """Verify the exact Sparkle 2.9 signed bytes before parsing any metadata."""
    if len(data) > FEED_LIMIT:
        raise UpdateError('更新说明超过大小限制。')
    content, marker, block = data.rpartition(b'<!-- sparkle-signatures:\n')
    if not marker or not block.rstrip().endswith(b'-->'):
        raise UpdateError('更新源缺少有效签名。')
    try:
        fields = dict(line.split(':', 1) for line in block.rstrip()[:-3].decode('ascii').strip().splitlines())
        if int(fields['length'].strip()) != len(content):
            raise ValueError()
        verify_bytes(content, fields['edSignature'].strip(), public_key)
        root = ET.fromstring(content)
        releases = []
        for item in root.findall('./channel/item'):
            version = item.findtext(SPARKLE + 'version', '')
            if version_key(version) <= version_key(current):
                continue
            if item.find(SPARKLE + 'channel') is not None:
                continue
            minimum = item.findtext(SPARKLE + 'minimumSystemVersion')
            if minimum and version_key(minimum) > version_key(platform.mac_ver()[0] or '0'):
                continue
            def enclosure(node):
                name = node.attrib['url']
                # Relative, single filenames permit replay of the original signed feed
                # over loopback without modifying it or exposing arbitrary local paths.
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', name):
                    raise UpdateError('更新文件地址不受支持。')
                length = int(node.attrib['length'])
                if not 0 < length <= 20 * 1024**3:
                    raise UpdateError('更新文件大小无效。')
                return {'name': name, 'size': length, 'signature': node.attrib[SPARKLE+'edSignature']}
            full = enclosure(item.find('enclosure'))
            delta = next((enclosure(n) for n in item.findall('./'+SPARKLE+'deltas/enclosure')
                          if n.attrib.get(SPARKLE+'deltaFrom') == current), None)
            description = item.findtext('description', '')
            # Release descriptions are plain text by publishing contract; never HTML.
            releases.append({'version': version, 'display_version': item.findtext(SPARKLE+'shortVersionString', version),
                             'notes': description[:20000], 'full': full,
                             'full_reason': ('' if delta and delta['size'] < full['size'] else
                                             '本次没有适用于当前版本的差量包' if not delta else '本次差量包不小于完整包'),
                             'selected': delta if delta and delta['size'] < full['size'] else full})
        return max(releases, key=lambda r: version_key(r['version'])) if releases else None
    except (KeyError, TypeError, AttributeError, ValueError, ET.ParseError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError('更新源格式无效，当前版本仍可使用。') from error


def bundle_info():
    if not getattr(sys, 'frozen', False):
        return {'version': '0', 'display_version': '开发版本', 'bundle': None, 'feed_url': '', 'public_key': ''}
    bundle = Path(sys.executable).resolve().parents[2]
    with (bundle/'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    return {'version': info['CFBundleVersion'], 'display_version': info['CFBundleShortVersionString'],
            'bundle': str(bundle), 'feed_url': info.get('SUFeedURL', ''), 'public_key': info.get('SUPublicEDKey', ''),
            'manual_update_only':info.get('KDManualUpdateOnly', True),
            'testing': info.get('CFBundleIdentifier') == 'local.knowledge-distiller.updater-test'}


class Updates:
    def __init__(self, data_root, *, info=None, clock=time.time):
        self.root = Path(data_root)/'updates'
        self.info = info or bundle_info()
        self.clock = clock
        self.lock = threading.RLock()
        self.busy = False
        self.error = ''
        self.phase = 'idle'
        self.received = 0
        self.release = None
        self.feed = None
        self.thread = None
        self.install = None
        self.block_reason = None
        self.last_action = 'check'
        self.token = __import__('secrets').token_urlsafe(32)
        self.record = {}
        try:
            self.record = json.loads((self.root/'state.json').read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = '上次更新状态无法读取，请重新检查。'
        if (self.root/'appcast.xml').is_file() and self.info['public_key']:
            try:
                self.feed = (self.root/'appcast.xml').read_bytes()
                self.release = self._parse_release(self.feed)
                if self.release:
                    self.phase = 'available'
                elif self.record.get('checked_at'):
                    self.phase = 'latest'
            except (OSError, UpdateError):
                self.feed = None
        if self.release and self.cached(self.release['selected']):
            self.phase = 'downloaded'

    def _parse_release(self, data):
        release=parse_feed(data,self.info['public_key'],self.info['version'])
        if release and self.info.get('manual_update_only'):
            release['selected']=release['full']
            release['full_reason']='当前安装模式仅支持完整包手动替换'
        if release and self.record.get('full_version') == release['version']:
            release['selected']=release['full']
            release['full_reason']='已选择改用完整包'
        return release

    def full_update_required(self):
        try:
            notice=json.loads((self.root/'full-update-required.json').read_text())
        except (OSError,ValueError):
            return False
        return bool(self.release and notice.get('version')==self.release['version']
                    and self.release['selected']!=self.release['full'])

    def manual_archive(self):
        with self.lock:
            if not self.info.get('manual_update_only') or self.phase!='downloaded' or not self.release:
                raise UpdateError('请先下载完整更新包。')
            asset=self.release['full']
            path=self.root/asset['name']
            self.verify_file(path,asset)
            return path

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root/'state.tmp'
        temp.write_text(json.dumps(self.record))
        temp.replace(self.root/'state.json')

    def cached(self, asset):
        path = self.root/asset['name']
        return path.is_file() and path.stat().st_size == asset['size']

    def snapshot(self):
        with self.lock:
            return {**{k: self.info[k] for k in ('version', 'display_version')},
                    'system': f'macOS {platform.mac_ver()[0]} · {platform.machine()}',
                    'release_date': self.info.get('release_date') or ('-'.join(self.info['version'].split('.')[:3]) if re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+', self.info['version']) else '待发行'),
                    'configured': bool(self.info['feed_url'] and self.info['public_key']),
                    'phase': self.phase, 'error': self.error, 'received': self.received,
                    'release': self.release, 'checked_at': self.record.get('checked_at'),
                    'attention': bool(self.release and self.record.get('seen_version') != self.release['version']),
                    'can_install': self.install is not None and not self.info.get('manual_update_only'),
                    'manual_update_only':bool(self.info.get('manual_update_only')), 'token': self.token,
                    'full_update_required':self.full_update_required(),
                    'retry_action': self.last_action,
                    'block_reason': self.block_reason() if self.phase == 'downloaded' and self.block_reason else ''}

    def start(self, action, *, automatic=False):
        with self.lock:
            if self.phase == 'installing':
                raise UpdateError('正在安装更新，请等待应用重新打开。')
            if self.busy:
                return False
            if not self.info['feed_url'] or not self.info['public_key']:
                raise UpdateError('此版本尚未配置更新源。')
            if automatic:
                if self.clock() - self.record.get('automatic_at', 0) < 86400:
                    return False
                self.record['automatic_at'] = self.clock()
                self.save()
            if action == 'download' and not self.release:
                raise UpdateError('请先检查更新。')
            if action == 'download-full':
                if not self.full_update_required():
                    raise UpdateError('完整包选项已变化，请重新检查更新。')
                self.record['full_version']=self.release['version']
                self.save()
                self.release['selected']=self.release['full']
                self.release['full_reason']='已选择改用完整包'
                action='download'
            self.busy = True
            self.last_action = action
            self.phase = 'checking' if action == 'check' else 'downloading'
            self.error = ''
            self.thread = threading.Thread(target=self._run, args=(action,), daemon=True, name='update-'+action)
            self.thread.start()
            return True

    def _run(self, action):
        try:
            if action == 'check':
                data = self.fetch_feed()
                release = self._parse_release(data)
                with self.lock:
                    self.root.mkdir(parents=True, exist_ok=True)
                    (self.root/'appcast.tmp').write_bytes(data)
                    (self.root/'appcast.tmp').replace(self.root/'appcast.xml')
                    self.feed, self.release = data, release
                    self.record['checked_at'] = self.clock()
                    self.save()
                    self.phase = 'available' if release else 'latest'
                    if release and self.cached(release['selected']):
                        self.phase = 'downloaded'
            else:
                self.download(self.release['selected'])
                with self.lock:
                    self.phase = 'downloaded'
        except Exception as error:
            logging.exception('Update %s failed', action)
            with self.lock:
                self.error = str(error) if isinstance(error, UpdateError) else '更新未完成，请检查网络或可用空间后重试。'
                self.phase = 'error'
        finally:
            with self.lock:
                self.busy = False

    def _url(self, name=None):
        url = self.info['feed_url']
        parsed = urlparse(url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and self.info.get('testing')):
            raise UpdateError('更新源必须使用 HTTPS。')
        return urljoin(url, name) if name else url

    def fetch_feed(self):
        with httpx.stream('GET', self._url(), follow_redirects=True, timeout=30) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > FEED_LIMIT:
                    raise UpdateError('更新说明超过大小限制。')
            return bytes(data)

    def verify_file(self, path, asset):
        if path.stat().st_size != asset['size']:
            raise UpdateError('更新文件不完整，请重新下载。')
        verifier = self.info.get('verifier') or (str(Path(self.info['bundle'])/'Contents/MacOS/update-verify') if self.info['bundle'] else None)
        if verifier:
            result = subprocess.run([verifier, self.info['public_key'], asset['signature'], str(path)], capture_output=True)
            if result.returncode:
                raise UpdateError('更新签名验证失败，已保留当前版本。')
        else:
            # Source-mode test fixtures only; frozen builds use mmap CryptoKit verifier.
            verify_bytes(path.read_bytes(), asset['signature'], self.info['public_key'])

    def download(self, asset):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root/asset['name']
        if self.cached(asset):
            try:
                self.verify_file(target, asset)
                return target
            except UpdateError:
                target.unlink()
        part = self.root/(asset['name']+'.part')
        self.received = 0
        try:
            with httpx.stream('GET', self._url(asset['name']), follow_redirects=True, timeout=60) as response, part.open('wb') as out:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024*1024):
                    self.received += len(chunk)
                    if self.received > asset['size']:
                        raise UpdateError('更新文件大小与签名说明不符。')
                    out.write(chunk)
            self.verify_file(part, asset)
            part.replace(target)
            return target
        finally:
            part.unlink(missing_ok=True)

    def mark_seen(self, version):
        with self.lock:
            if not self.release or version != self.release['version']:
                raise UpdateError('更新说明已变化，请重新打开。')
            self.record['seen_version'] = version
            self.save()

    def request_install(self):
        with self.lock:
            if self.info.get('manual_update_only'):
                raise UpdateError('此版本使用完整安装包手动替换，不申请应用管理权限。')
            if self.full_update_required():
                raise UpdateError('差量更新未能应用，请先确认改用完整包。')
            if self.busy or self.phase != 'downloaded' or not self.install:
                raise UpdateError('请先完成下载，再安装更新。')
            self.verify_file(self.root/self.release['selected']['name'], self.release['selected'])
            self.last_action = 'install'
            self.install()
            self.phase = 'installing'
