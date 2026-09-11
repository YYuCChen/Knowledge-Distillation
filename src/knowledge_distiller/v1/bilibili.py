"""Bounded public Bilibili discovery and exact-member audio capture."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from knowledge_distiller.media import FFmpegMediaVerifier, MediaVerificationError
from .domain import CapturedMaterial
from .douyin_collections import Scope
from .youtube import _QuietDownloadLog, _digest, normalize_downloaded_audio

MAX_MEMBERS = 200
METADATA_TIMEOUT = 90
DOWNLOAD_TIMEOUT = 240
_BV = r'BV[0-9A-Za-z]{10}'
_NATIVE = rf'(?:{_BV}(?:_p[1-9][0-9]*|_[1-9][0-9]*)?|bangumi_ep[1-9][0-9]*|cheese_ep[1-9][0-9]*)'
ERROR_CODES = ('bilibili_incomplete', 'bilibili_scope_required', 'bilibili_identity_mismatch',
               'bilibili_scope_changed', 'bilibili_range_too_large', 'bilibili_empty',
               'bilibili_timeout', 'bilibili_login_required', 'bilibili_payment_required',
               'bilibili_unavailable', 'bilibili_rate_limited', 'bilibili_upstream_failed',
               'bilibili_media_invalid', 'bilibili_legacy_fragments_unsupported')


class BilibiliSourceError(RuntimeError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def bilibili_identity(value):
    """Validate a native leaf/range URL; tracking parameters never define identity."""
    try:
        value = value.strip()
        if re.fullmatch(rf'{_BV}|av[0-9]+', value):
            value = 'https://www.bilibili.com/video/' + value
        url = urlsplit(value)
        if url.scheme not in {'http', 'https'} or url.username or url.password or url.port:
            raise ValueError
        if url.hostname not in {'www.bilibili.com', 'bilibili.com', 'm.bilibili.com', 'space.bilibili.com', 'b23.tv'}:
            raise ValueError
        query = parse_qs(url.query, keep_blank_values=True)
        keep = {}
        def one(name, pattern):
            values = query.get(name, [])
            if len(values) > 1 or values and not re.fullmatch(pattern, values[0]):
                raise ValueError
            if values:
                keep[name] = values[0]
            return values[0] if values else None
        path = url.path.rstrip('/')
        host = 'www.bilibili.com'
        if url.hostname == 'b23.tv':
            if not re.fullmatch(r'/[A-Za-z0-9]+', path):
                raise ValueError
            host, key = 'b23.tv', 'short:' + path[1:]
        elif url.hostname == 'space.bilibili.com':
            host = 'space.bilibili.com'
            if re.fullmatch(r'/[0-9]+(?:/(?:upload/)?video)?', path):
                path = '/' + path.split('/')[1] + '/video'
            elif re.fullmatch(r'/[0-9]+/lists/[0-9]+', path):
                one('type', 'season|series')
            elif re.fullmatch(r'/[0-9]+/channel/(?:collectiondetail|seriesdetail)', path):
                if not one('sid', '[1-9][0-9]*'):
                    raise ValueError
            elif re.fullmatch(r'/[0-9]+/favlist', path):
                if not one('fid', '[1-9][0-9]*'):
                    raise ValueError
            else:
                raise ValueError
            key = 'range:' + path[1:] + ('?' + urlencode(keep) if keep else '')
        elif match := re.fullmatch(rf'/video/({_BV}|av[0-9]+)', path):
            part = one('p', '[1-9][0-9]*')
            key = match[1] + ('_p' + part if part else '')
            path += '/'
        elif match := re.fullmatch(r'/(bangumi|cheese)/play/(ep|ss)([1-9][0-9]*)', path):
            key = match[1] + '_' + match[2] + match[3]
        elif re.fullmatch(r'/bangumi/media/md[1-9][0-9]*', path):
            key = 'range:' + path[1:]
        elif re.fullmatch(r'/list/(?:[1-9][0-9]*|ml[1-9][0-9]*)', path):
            one('sid', '[1-9][0-9]*')
            key = 'range:' + path[1:] + ('?' + urlencode(keep) if keep else '')
        elif re.fullmatch(r'/medialist/(?:detail|play)/(?:ml)?[1-9][0-9]*', path):
            one('business', 'space_series|space_collection')
            one('business_id', '[1-9][0-9]*')
            key = 'range:' + path[1:] + ('?' + urlencode(keep) if keep else '')
        else:
            raise ValueError
        canonical = urlunsplit(('https', host, path, urlencode(keep), ''))
        if url.fragment.startswith('kd-source='):
            selected = url.fragment[len('kd-source='):]
            if not re.fullmatch(_NATIVE, selected):
                raise ValueError
            key = selected
        return key, canonical
    except (ValueError, AttributeError) as error:
        raise ValueError('请核对 B 站视频、分 P、番剧、课程或范围链接。') from error


def _native_key(info):
    identity = str(info.get('id') or '')
    extractor = str(info.get('extractor_key') or info.get('extractor') or '')
    if re.fullmatch(rf'{_BV}(?:_p[1-9][0-9]*|_[1-9][0-9]*)?', identity):
        return identity
    if identity.isdigit():
        path = urlsplit(info.get('webpage_url') or '').path
        if extractor == 'BiliBiliBangumi' or path.startswith('/bangumi/play/ep'):
            return 'bangumi_ep' + identity
        if extractor == 'BilibiliCheese' or path.startswith('/cheese/play/ep'):
            return 'cheese_ep' + identity
    raise BilibiliSourceError('bilibili_identity_mismatch')


def qualify(info, key=None):
    if not isinstance(info, dict):
        raise BilibiliSourceError('bilibili_incomplete')
    if info.get('_type') == 'multi_video':
        raise BilibiliSourceError('bilibili_legacy_fragments_unsupported')
    if info.get('_type') == 'playlist' or info.get('entries') is not None:
        raise BilibiliSourceError('bilibili_scope_required')
    identity = _native_key(info)
    if key is not None and key != identity:
        raise BilibiliSourceError('bilibili_identity_mismatch')
    duration = info.get('duration')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise BilibiliSourceError('bilibili_incomplete')
    if info.get('availability') in {'needs_auth', 'subscriber_only', 'premium_only'}:
        raise BilibiliSourceError('bilibili_login_required')
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'} or info.get('is_preview'):
        raise BilibiliSourceError('bilibili_incomplete')
    return identity


def _edges(info):
    if not re.fullmatch(rf'{_BV}_[1-9][0-9]*', str(info.get('id') or '')):
        return None
    try:
        edges = json.loads(info['description'].split('\n', 1)[0])
        cid = int(info['id'].rsplit('_', 1)[1])
        if not isinstance(edges, dict) or not edges:
            raise ValueError
        for edge_id, edge in edges.items():
            if not str(edge_id).isdigit() or not isinstance(edge, dict) or edge.get('cid') != cid:
                raise ValueError
            for choice in edge.get('choices', []):
                if type(choice.get('edge_id')) is not int or type(choice.get('cid')) is not int or not isinstance(choice.get('text'), str):
                    raise ValueError
        return edges
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise BilibiliSourceError('bilibili_incomplete') from error


def content_version(info):
    """Same authored identity fingerprint in discovery and captured material."""
    key = qualify(info)
    return _hash({'id': key, 'cid': info.get('cid'), 'duration': info['duration'],
                  'title': info.get('title'), 'description': info.get('description'),
                  'timestamp': info.get('timestamp'), 'edges': _edges(info),
                  'graph_version': info.get('graph_version')})


def member_url(key, source_url=None):
    if match := re.fullmatch(rf'({_BV})(?:_p([1-9][0-9]*))?', key):
        return f'https://www.bilibili.com/video/{match[1]}/' + ('?p=' + match[2] if match[2] else '')
    if match := re.fullmatch(rf'({_BV})_([1-9][0-9]*)', key):
        # An internal selector, never advertised as a platform-native jump link.
        canonical = f'https://www.bilibili.com/video/{match[1]}/'
        if source_url:
            _, supplied = bilibili_identity(source_url)
            if supplied != canonical:
                raise BilibiliSourceError('bilibili_identity_mismatch')
        return canonical + '#kd-source=' + key
    if match := re.fullmatch(r'(bangumi|cheese)_ep([1-9][0-9]*)', key):
        return f'https://www.bilibili.com/{match[1]}/play/ep{match[2]}'
    raise BilibiliSourceError('bilibili_identity_mismatch')


def _leaves(info, *, require_complete=True):
    leaves = []
    def visit(node):
        if not isinstance(node, dict):
            raise BilibiliSourceError('bilibili_incomplete')
        if node.get('_type') == 'multi_video':
            raise BilibiliSourceError('bilibili_legacy_fragments_unsupported')
        if node.get('_type') == 'playlist' or 'entries' in node:
            entries = node.get('entries')
            if not isinstance(entries, list) or require_complete and not node.get('_kd_complete'):
                raise BilibiliSourceError('bilibili_incomplete')
            expected = node.get('playlist_count')
            if expected is not None and (type(expected) is not int or expected != len(entries)):
                raise BilibiliSourceError('bilibili_incomplete')
            if len(entries) > MAX_MEMBERS:
                raise BilibiliSourceError('bilibili_range_too_large')
            if not entries:
                raise BilibiliSourceError('bilibili_empty')
            for entry in entries:
                visit(entry)
        else:
            qualify(node)
            _edges(node)
            leaves.append(node)
            if len(leaves) > MAX_MEMBERS:
                raise BilibiliSourceError('bilibili_range_too_large')
    visit(info)
    return leaves


@dataclass(frozen=True)
class BilibiliMember:
    item_id: str
    title: str
    supported: bool
    native_kind: int
    version: str
    url: str


class BilibiliDiscovery:
    def __init__(self, downloader=None):
        self.downloader = downloader

    def discover(self, urls, selected=None):
        if not urls or len(urls) > MAX_MEMBERS:
            raise BilibiliSourceError('bilibili_range_too_large')
        scopes = []
        deadline = time.monotonic() + METADATA_TIMEOUT
        for url in dict.fromkeys(urls):
            if time.monotonic() >= deadline:
                raise BilibiliSourceError('bilibili_timeout')
            _, canonical = bilibili_identity(url)
            info, _ = (self.downloader or extract_bilibili)(url)
            if time.monotonic() >= deadline:
                raise BilibiliSourceError('bilibili_timeout')
            leaves = _leaves(info)
            selector = urlsplit(url).fragment.removeprefix('kd-source=') if urlsplit(url).fragment.startswith('kd-source=') else None
            if selector:
                leaves = [leaf for leaf in leaves if _native_key(leaf) == selector]
                if len(leaves) != 1:
                    raise BilibiliSourceError('bilibili_identity_mismatch')
            members = {}
            for leaf in leaves:
                key = qualify(leaf)
                member = BilibiliMember(key, str(leaf.get('title') or key), True, 0,
                                       content_version(leaf), member_url(key))
                if key in members and members[key] != member:
                    raise BilibiliSourceError('bilibili_scope_changed')
                members.setdefault(key, member)
            if selected is not None:
                if not selected or not set(selected) <= set(members):
                    raise BilibiliSourceError('bilibili_scope_changed')
                members = {key: member for key, member in members.items() if key in selected}
            scope_key = str(info.get('extractor_key') or 'BiliBili') + ':' + str(info.get('id'))
            scopes.append(Scope('bilibili_range', scope_key, str(info.get('title') or scope_key),
                                str(info['uploader_id']) if info.get('uploader_id') else None,
                                tuple(members.values()), datetime.now(UTC).isoformat(),
                                {'platform': 'bilibili', 'class': 'PUBLIC'}))
        single = scopes[0].members[0].url if len(scopes) == 1 and len(scopes[0].members) == 1 else None
        return {'scopes': scopes, 'choices': [], 'single_url': single}


def _error_code(error):
    if isinstance(error, BilibiliSourceError):
        return str(error)
    message = str(error).lower()
    if 'purchase' in message or 'pay' in message or 'supporter-only' in message or 'preview' in message:
        return 'bilibili_payment_required'
    if 'login' in message or 'log in' in message or 'sign in' in message or 'http error 401' in message:
        return 'bilibili_login_required'
    if '412' in message or '429' in message or '-352' in message:
        return 'bilibili_rate_limited'
    if 'deleted' in message or 'geo-restrict' in message or '404' in message or 'not yet available' in message:
        return 'bilibili_unavailable'
    if 'timed out' in message or 'timeout' in message:
        return 'bilibili_timeout'
    return 'bilibili_upstream_failed'


def _run_worker(request, timeout):
    # A process deadline also bounds slow responses and interactive graph recursion.
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    if sys.platform == 'darwin':
        env['LC_ALL'] = 'en_US.UTF-8'
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    try:
        command = ([sys.executable, '--bilibili-worker'] if getattr(sys, 'frozen', False) else
                   [sys.executable, '-c', 'from knowledge_distiller.v1.bilibili import _worker_main; _worker_main()'])
        process = subprocess.Popen(command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', env=env, start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        try:
            output, _ = process.communicate(json.dumps(request), timeout=timeout)
        except subprocess.TimeoutExpired:
            # The worker's own session also owns any ffmpeg download subprocess.
            try:
                if sys.platform == 'win32':
                    subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True, timeout=10,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                    if process.poll() is None:
                        process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        if process.returncode:
            raise BilibiliSourceError('bilibili_upstream_failed')
        response = json.loads(output)
        if response.get('error'):
            raise BilibiliSourceError(response['error'] if response['error'] in ERROR_CODES else 'bilibili_upstream_failed')
        return response
    except subprocess.TimeoutExpired as error:
        raise BilibiliSourceError('bilibili_timeout') from error
    except (OSError, ValueError) as error:
        raise BilibiliSourceError('bilibili_upstream_failed') from error


def extract_bilibili(url, *, work_dir=None):
    requested, canonical = bilibili_identity(url)
    info = _run_worker({'url': canonical}, METADATA_TIMEOUT)['info']
    leaves = _leaves(info)
    selector = urlsplit(url).fragment.startswith('kd-source=')
    if selector:
        selected = [leaf for leaf in leaves if _native_key(leaf) == requested]
        if len(selected) != 1:
            raise BilibiliSourceError('bilibili_identity_mismatch')
        leaf = selected[0]
    elif len(leaves) == 1 and info.get('entries') is None:
        leaf = leaves[0]
        # av and short links resolve to canonical native identities upstream.
        if re.fullmatch(_NATIVE, requested) and _native_key(leaf) != requested:
            raise BilibiliSourceError('bilibili_identity_mismatch')
    else:
        if work_dir is not None:
            raise BilibiliSourceError('bilibili_scope_required')
        return info, None
    if work_dir is None:
        return leaf, None
    work_dir.mkdir(parents=True, exist_ok=True)
    response = _run_worker({'leaf': leaf, 'work_dir': str(work_dir.resolve())}, DOWNLOAD_TIMEOUT)
    downloaded = response['info']
    qualify(downloaded, _native_key(leaf))
    if content_version(downloaded) != content_version(leaf):
        raise BilibiliSourceError('bilibili_scope_changed')
    media = Path(response['media'])
    if not media.is_file() or media.is_symlink() or media.resolve().parent != work_dir.resolve():
        raise BilibiliSourceError('bilibili_media_invalid')
    return downloaded, media


def _worker_main():
    try:
        request = json.load(sys.stdin)
        result = _extract_worker(request)
    except Exception as error:
        result = {'error': _error_code(error)}
    print(json.dumps(result, ensure_ascii=False))


def _extract_worker(request):
    import yt_dlp
    from yt_dlp.extractor import bilibili as upstream
    from unittest.mock import patch
    pages, graphs, episode_media = {}, {}, {}
    original_json = upstream.BilibiliBaseIE._download_json
    original_playinfo = upstream.BilibiliBaseIE._download_playinfo
    original_course = upstream.BilibiliCheeseSeasonIE._get_cheese_entries
    original_divisions = upstream.BilibiliBaseIE._get_divisions
    edge_count = 0

    class Log(_QuietDownloadLog):
        def warning(self, message):
            # Missing premium resolutions are fine; a playable preview is not a full source.
            if 'preview' in message.lower() and ('only' in message.lower() or 'will be extracted' in message.lower()):
                raise BilibiliSourceError('bilibili_payment_required')

    def checked_json(extractor, url, *args, **kwargs):
        result = original_json(extractor, url, *args, **kwargs)
        anonymous_nav = '/x/web-interface/nav' in str(url) and isinstance(result, dict) and result.get('code') == -101
        optional_request = kwargs.get('fatal') is False and '/x/player/pagelist' not in str(url)
        if (not anonymous_nav and not optional_request and 'api.bilibili.com/' in str(url)
                and isinstance(result, dict) and result.get('code') not in (None, 0)):
            code = result['code']
            if code in (-101, -403):
                raise BilibiliSourceError('bilibili_login_required')
            if code in (-352, -412):
                raise BilibiliSourceError('bilibili_rate_limited')
            if code in (-404, 62002):
                raise BilibiliSourceError('bilibili_unavailable')
            raise BilibiliSourceError('bilibili_upstream_failed')
        if '/pgc/view/web/season' in str(url) or '/pugv/view/web/season' in str(url):
            body = (result.get('result') or result.get('data') or {}) if isinstance(result, dict) else {}
            episodes = list(body.get('episodes') or [])
            for section in body.get('section') or []:
                episodes.extend(section.get('episodes') or [])
            namespace = 'bangumi_ep' if '/pgc/' in str(url) else 'cheese_ep'
            for episode in episodes:
                if type(episode.get('id')) is int and type(episode.get('cid')) is int:
                    episode_media[namespace + str(episode['id'])] = episode['cid']
        if '/x/player/pagelist' in str(url):
            rows = result.get('data') if isinstance(result, dict) else None
            if not isinstance(rows, list) or not rows or any(type(x.get('page')) is not int or type(x.get('cid')) is not int for x in rows):
                raise BilibiliSourceError('bilibili_incomplete')
            if len(rows) > MAX_MEMBERS:
                raise BilibiliSourceError('bilibili_range_too_large')
            pages[(kwargs.get('query') or {}).get('bvid')] = rows
        if '/x/player/wbi/v2' in str(url):
            graph = ((result or {}).get('data') or {}).get('interaction') or {}
            if graph.get('graph_version'):
                graphs[(kwargs.get('query') or {}).get('bvid')] = graph['graph_version']
        return result

    def checked_playinfo(extractor, bvid, cid, *args, **kwargs):
        result = original_playinfo(extractor, bvid, cid, *args, **kwargs)
        if isinstance(result, dict) and (result.get('is_preview') or result.get('is_preview') == 1):
            raise BilibiliSourceError('bilibili_payment_required')
        return result

    def course_entries(extractor, season):
        episodes = season.get('episodes')
        if not isinstance(episodes, list) or not episodes:
            raise BilibiliSourceError('bilibili_incomplete')
        if len(episodes) > MAX_MEMBERS:
            raise BilibiliSourceError('bilibili_range_too_large')
        if any(not ep.get('episode_can_view') or not ep.get('playable') for ep in episodes):
            raise BilibiliSourceError('bilibili_payment_required')
        yield from original_course(extractor, season)

    def strict_entries(extractor, page_data, bvid_keys, ending_key='bvid'):
        rows = page_data
        for key in ((bvid_keys,) if isinstance(bvid_keys, str) else bvid_keys):
            rows = rows.get(key) if isinstance(rows, dict) else None
        if not isinstance(rows, list):
            raise BilibiliSourceError('bilibili_incomplete')
        for row in rows:
            identity = row.get(ending_key) if isinstance(row, dict) else None
            if not isinstance(identity, str) or not re.fullmatch(_BV, identity):
                raise BilibiliSourceError('bilibili_incomplete')
            yield extractor.url_result(f'https://www.bilibili.com/video/{identity}',
                                       upstream.BiliBiliIE, identity)

    def paged_playlist(extractor, fetch_page, get_metadata, get_entries):
        first = fetch_page(0)
        metadata = get_metadata(first)
        page_size, page_count = metadata.get('page_size'), metadata.get('page_count')
        page = first.get('page') or {}
        total = page.get('count', page.get('total'))
        if (type(page_size) is not int or page_size <= 0 or type(page_count) is not int
                or page_count < 0 or type(total) is not int or total < 0):
            raise BilibiliSourceError('bilibili_incomplete')
        if total > MAX_MEMBERS:
            raise BilibiliSourceError('bilibili_range_too_large')
        def entries():
            count = 0
            for index in range(page_count):
                current = first if index == 0 else fetch_page(index)
                current_page = current.get('page') or {}
                if current_page.get('count', current_page.get('total')) != total:
                    raise BilibiliSourceError('bilibili_scope_changed')
                rows = list(get_entries(current))
                if len(rows) != min(page_size, total - count):
                    raise BilibiliSourceError('bilibili_incomplete')
                count += len(rows)
                yield from rows
            if count != total:
                raise BilibiliSourceError('bilibili_incomplete')
        return metadata, entries()

    def season_entries(extractor, season_id, url):
        response = extractor._download_json(
            'https://api.bilibili.com/pgc/web/season/section', season_id,
            query={'season_id': season_id}, headers={'Referer': url})
        result = response.get('result') if isinstance(response, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get('main_section'), dict):
            raise BilibiliSourceError('bilibili_incomplete')
        sections = [result['main_section'], *result.get('section', [])]
        seen = set()
        for section in sections:
            episodes = section.get('episodes')
            if not isinstance(episodes, list):
                raise BilibiliSourceError('bilibili_incomplete')
            for episode in episodes:
                identity = episode.get('id')
                if type(identity) is not int or identity <= 0:
                    raise BilibiliSourceError('bilibili_incomplete')
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > MAX_MEMBERS:
                    raise BilibiliSourceError('bilibili_range_too_large')
                yield extractor.url_result(f'https://www.bilibili.com/bangumi/play/ep{identity}',
                                           upstream.BiliBiliBangumiIE, str(identity))

    def divisions(extractor, *args, **kwargs):
        nonlocal edge_count
        edge_count += 1
        if edge_count > MAX_MEMBERS * 5:
            raise BilibiliSourceError('bilibili_range_too_large')
        return original_divisions(extractor, *args, **kwargs)

    completed_files = []
    options = {'quiet': True, 'logger': Log(), 'cachedir': False, 'socket_timeout': 10,
               'format': 'bestaudio/best',
               'progress_hooks': [lambda event: completed_files.append(event['filename']) if event.get('status') == 'finished' else None],
               'retries': 0, 'extractor_retries': 0, 'fragment_retries': 0,
               'noplaylist': False, 'ignoreerrors': False, 'playlistend': MAX_MEMBERS + 1,
               'getcomments': False, 'check_formats': False, 'skip_download': False}
    if request.get('work_dir'):
        options.update(format='bestaudio/best', outtmpl=str(Path(request['work_dir']) / '%(id)s.%(ext)s'))
    with patch.object(upstream.BilibiliBaseIE, '_download_json', checked_json), \
         patch.object(upstream.BilibiliBaseIE, '_download_playinfo', checked_playinfo), \
         patch.object(upstream.BilibiliCheeseSeasonIE, '_get_cheese_entries', course_entries), \
         patch.object(upstream.BilibiliBaseIE, '_get_divisions', divisions), \
         patch.object(upstream.BilibiliBaseIE, '_get_episodes_from_season', season_entries), \
         patch.object(upstream.BilibiliSpaceBaseIE, '_extract_playlist', paged_playlist), \
         patch.object(upstream.BilibiliSpaceListBaseIE, '_get_entries', strict_entries), \
         yt_dlp.YoutubeDL(options) as ydl:
        if 'leaf' in request:
            qualify(request['leaf'])
            info = ydl.process_ie_result(request['leaf'], download=True)
            paths = [row.get('filepath') for row in info.get('requested_downloads', []) if row.get('filepath')]
            paths = list(dict.fromkeys(completed_files or paths or [ydl.prepare_filename(info)]))
            if len(paths) != 1:
                raise BilibiliSourceError('bilibili_media_invalid')
            return {'info': ydl.sanitize_info(info), 'media': paths[0]}
        info = ydl.extract_info(request['url'], download=False)
        def finish(node):
            if not isinstance(node, dict):
                raise BilibiliSourceError('bilibili_incomplete')
            if node.get('entries') is not None:
                node['entries'] = list(node['entries'])
                for entry in node['entries']:
                    finish(entry)
                node['_kd_complete'] = True
            else:
                key = qualify(node)
                video = key.split('_', 1)[0]
                if key in episode_media:
                    node['cid'] = episode_media[key]
                if video in pages:
                    part = int(key.rsplit('_p', 1)[1]) if '_p' in key else 1
                    matching = [row for row in pages[video] if row['page'] == part]
                    if not matching:
                        raise BilibiliSourceError('bilibili_identity_mismatch')
                    node['cid'] = int(key.rsplit('_', 1)[1]) if re.fullmatch(rf'{_BV}_[1-9][0-9]*', key) else matching[0]['cid']
                if video in graphs:
                    node['graph_version'] = graphs[video]
        finish(info)
        _leaves(info)
        return {'info': ydl.sanitize_info(info)}


class BilibiliSource:
    def __init__(self, downloader=None, verifier=None):
        self.downloader = downloader or extract_bilibili
        self.verifier = verifier or FFmpegMediaVerifier()

    def capture(self, submitted_url, work_dir, **options):
        requested, _ = bilibili_identity(submitted_url)
        download = work_dir / 'download'
        try:
            info, media = self.downloader(submitted_url, work_dir=download)
            key = qualify(info)
            if re.fullmatch(_NATIVE, requested) and key != requested:
                raise BilibiliSourceError('bilibili_identity_mismatch')
            canonical = bilibili_identity(member_url(key))[1]
            destination, duration = normalize_downloaded_audio(media, work_dir, float(info['duration']), self.verifier)
            branch = _edges(info)
            description = info.get('description')
            if branch:
                description = description.split('\n', 1)[1] if '\n' in description else ''
            metadata = {'source_title': info.get('title'), 'original_description': description,
                        'published_at': datetime.fromtimestamp(info['timestamp'], UTC).isoformat() if info.get('timestamp') else None,
                        'author': {'display_name': info.get('uploader'), 'platform_account_id': info.get('uploader_id')},
                        'captured_at': datetime.now(UTC).isoformat(), 'audio_sha256': _digest(destination),
                        'platform_duration_seconds': info['duration'], 'native_content_version': content_version(info),
                        'native_source_id': key, 'native_media_id': str(info.get('cid') or info['id']),
                        'source_scope': '视频音频转写；弹幕和评论未作为作者正文'}
            if branch:
                metadata['interactive_branch'] = {'cid': info.get('cid') or int(key.rsplit('_', 1)[1]),
                    'graph_version': info.get('graph_version'), 'edges': branch,
                    'locator_kind': 'internal_native_identity', 'platform_deep_link_available': False}
            return CapturedMaterial('bilibili', key, submitted_url, canonical, metadata, destination, duration)
        except (OSError, subprocess.TimeoutExpired, MediaVerificationError) as error:
            raise BilibiliSourceError('bilibili_media_invalid') from error
        finally:
            if download.exists():
                shutil.rmtree(download)

    def reuse_retained(self, *, source_key, submitted_url, canonical_url, metadata, work_dir, **options):
        media = work_dir / 'media' / 'source.mka'
        if any(path.is_symlink() for path in (work_dir, media.parent, media)):
            raise BilibiliSourceError('bilibili_media_invalid')
        if not media.exists():
            return None
        # Older captures did not persist sufficient native version identity.
        if (not metadata.get('native_source_id') or not isinstance(metadata.get('native_content_version'), str)
                or not re.fullmatch('[0-9a-f]{64}', metadata['native_content_version'])):
            return None
        try:
            expected_canonical = bilibili_identity(member_url(source_key))[1]
            submitted_key, _ = bilibili_identity(submitted_url)
            if (metadata['native_source_id'] != source_key or canonical_url != expected_canonical
                    or re.fullmatch(_NATIVE, submitted_key) and submitted_key != source_key):
                raise BilibiliSourceError('bilibili_identity_mismatch')
        except ValueError as error:
            raise BilibiliSourceError('bilibili_identity_mismatch') from error
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(metadata['captured_at'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 <= age <= 72 * 3600:
            return None
        expected = metadata.get('platform_duration_seconds')
        if isinstance(expected, bool) or not isinstance(expected, (int, float)) or not math.isfinite(expected) or expected <= 0:
            raise BilibiliSourceError('bilibili_media_invalid')
        try:
            if _digest(media) != metadata.get('audio_sha256'):
                raise MediaVerificationError('retained Bilibili audio digest changed')
            duration = self.verifier.verify(media, expected_duration_seconds=expected,
                                            complete_decode=True, require_video=False)
        except (OSError, MediaVerificationError) as error:
            raise BilibiliSourceError('bilibili_media_invalid') from error
        return CapturedMaterial('bilibili', source_key, submitted_url, canonical_url, metadata, media, duration)
