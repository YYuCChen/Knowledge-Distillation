"""One signed release contract and download plan for app and bootstrap installer."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from urllib.parse import urlsplit
import re

from .updates import UpdateError, verify_bytes, version_key


MAX_MANIFEST = 4 * 1024 * 1024
INSTALLER_PROTOCOL = 1


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError('发行清单含重复字段。')
        result[key] = value
    return result


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)


def _asset(value):
    if (not isinstance(value, dict) or not _digest(value.get('sha256'))
            or type(value.get('size')) is not int or not 0 < value['size'] <= 8 * 1024**3
            or type(value.get('unpacked_size')) is not int
            or not 0 < value['unpacked_size'] <= 20 * 1024**3):
        raise UpdateError('发行组件身份或大小无效。')
    url = urlsplit(value.get('url', ''))
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.fragment):
        raise UpdateError('发行组件下载地址无效。')


def parse_release(envelope, public_key, *, platform, current='0'):
    """Authenticate exact bytes before inspecting version, platform or addresses."""
    if len(envelope) > MAX_MANIFEST:
        raise UpdateError('发行清单过大。')
    try:
        wrapper = json.loads(envelope, object_pairs_hook=_object)
        raw = base64.b64decode(wrapper['payload'], validate=True)
        verify_bytes(raw, wrapper['signature'], public_key)
        release = json.loads(raw, object_pairs_hook=_object)
        if (release['format'] != 1 or release['product'] != 'knowledge-distiller'
                or release['platform'] != platform
                or type(release['minimum_installer']) is not int
                or not 1 <= release['minimum_installer'] <= INSTALLER_PROTOCOL
                or not re.fullmatch('[0-9a-f]{40}', release['source_commit'])
                or not _digest(release['target_identity'])):
            raise UpdateError('发行清单与当前平台或安装器不兼容。')
        if version_key(release['version']) < version_key(current):
            raise UpdateError('已拒绝安装旧版本。')
        from .adapters.python_policy import PYTHON_VERSION
        if release['python_version'] != PYTHON_VERSION:
            raise UpdateError('发行 Python 运行契约不匹配。')
        model = release['docling']
        from .docling_component import DoclingComponent
        if model['identity'] != DoclingComponent('.').identity:
            raise UpdateError('发行模型与程序固定清单不匹配。')
        _asset(model)
        base = release['base']
        _asset(base)
        if not _digest(base['identity']):
            raise UpdateError('程序基座身份无效。')
        deltas = release['deltas']
        if not isinstance(deltas, list) or len(deltas) > 16:
            raise UpdateError('程序差量清单无效。')
        seen = set()
        for delta in deltas:
            _asset(delta)
            if (not _digest(delta['from_identity']) or delta['from_identity'] in seen
                    or delta['to_identity'] != release['target_identity']):
                raise UpdateError('程序差量路径无效。')
            seen.add(delta['from_identity'])
        if base['identity'] != release['target_identity'] and base['identity'] not in seen:
            raise UpdateError('固定基座缺少直达目标的差量。')
        return release
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError('发行清单无效。') from error


@dataclass(frozen=True)
class ComponentPlan:
    source: str
    assets: tuple[dict, ...]
    download_bytes: int
    target_identity: str


def plan_release(release, *, verified_current_identity=None, verified_model_identity=None,
                 verified_cached_assets=()):
    """Inputs with 'verified' names must come from byte checks, never receipt claims."""
    assets = []
    if verified_model_identity != release['docling']['identity']:
        assets.append(release['docling'])
    target = release['target_identity']
    if verified_current_identity == target:
        source = 'current'
    else:
        direct = next((d for d in release['deltas']
                       if d['from_identity'] == verified_current_identity), None)
        if direct is not None:
            source = 'current'
            assets.append(direct)
        else:
            source = 'base'
            assets.append(release['base'])
            if release['base']['identity'] != target:
                assets.append(next(d for d in release['deltas']
                                   if d['from_identity'] == release['base']['identity']))
    return ComponentPlan(source, tuple(assets), sum(asset['size'] for asset in assets
        if asset['sha256'] not in verified_cached_assets), target)
