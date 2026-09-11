from __future__ import annotations

import hashlib
import http.cookiejar
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5
import re

from knowledge_distiller.media import FFmpegMediaVerifier, MediaVerificationError
from .chrome import ChromePage, ChromeSessionError
from .domain import CapturedMaterial


class YouTubeSourceError(RuntimeError):
    pass


def youtube_identity(value: str) -> tuple[str, str]:
    try:
        url = urlsplit(value.strip())
        query = parse_qs(url.query, keep_blank_values=True)
        if url.scheme not in {'http', 'https'} or url.username or url.password or url.port or 'list' in query:
            raise ValueError
        host = (url.hostname or '').lower()
        if host == 'youtu.be':
            key = url.path.strip('/')
        elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
            if url.path.rstrip('/') == '/watch' and len(query.get('v', [])) == 1:
                key = query['v'][0]
            elif url.path.startswith(('/shorts/', '/live/')):
                key = url.path.split('/', 2)[2].rstrip('/')
            else:
                raise ValueError
        else:
            raise ValueError
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', key):
            raise ValueError
        return key, f'https://www.youtube.com/watch?v={key}'
    except ValueError as error:
        raise ValueError('请提交一条 YouTube 视频或 Shorts 链接，不含播放列表。') from error


@dataclass(frozen=True)
class YouTubeConnection:
    account_label: str | None = None


class YouTubeChromeSession:
    def __init__(self, page_factory=None):
        self._page_factory = page_factory or ChromePage

    def cookies(self):
        try:
            with self._page_factory('https://www.youtube.com/') as page:
                if not page.wait_for('Boolean(window.ytcfg?.get("LOGGED_IN"))'):
                    raise ChromeSessionError('youtube_login_required')
                return page.cookies(['https://www.youtube.com/'])
        except ChromeSessionError:
            raise
        except Exception as error:
            raise ChromeSessionError('chrome_connection_failed') from error

    def verify(self):
        self.cookies()
        return YouTubeConnection()


def connection_authority(row):
    if row is None or row['state'] == 'unconfigured':
        raise ChromeSessionError('youtube_not_configured')
    if row['state'] != 'connected':
        raise ChromeSessionError('youtube_login_required')
    instance = uuid5(NAMESPACE_URL, f"youtube:{row['connected_at']}:{row['generation']}").hex
    return {'class': 'EXISTING_BROWSER_OWNED', 'connection_instance_id': instance,
            'session_authority_ref': f'browser:{instance}', 'generation': row['generation'],
            'connected_at': row['connected_at']}


class _QuietDownloadLog:
    # Upstream errors can contain signed URLs. Only typed failures leave this boundary.
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


class YouTubeSource:
    def __init__(self, store, session=None, downloader=None, verifier=None):
        self.store = store
        self.session = session or YouTubeChromeSession()
        self.downloader = downloader or download_youtube
        self.verifier = verifier or FFmpegMediaVerifier()

    def capture(self, submitted_url: str, work_dir: Path, *, expected_authority=None) -> CapturedMaterial:
        key, canonical = youtube_identity(submitted_url)
        authority = connection_authority(self.store.connection('youtube'))
        if expected_authority is not None and authority != expected_authority:
            raise ChromeSessionError('youtube_connection_changed')
        try:
            cookies = self.session.cookies()
        except ChromeSessionError as error:
            if str(error) == 'youtube_login_required':
                self.store.require_relogin('youtube')
            raise
        info, media = self.downloader(key, canonical, cookies, work_dir / 'download')
        _qualify(info, key)
        def verify_authority():
            if connection_authority(self.store.connection('youtube')) != authority:
                raise ChromeSessionError('youtube_connection_changed')
        try:
            destination, duration = normalize_downloaded_audio(
                media, work_dir, float(info['duration']), self.verifier, before_commit=verify_authority)
        except (OSError, subprocess.TimeoutExpired, MediaVerificationError) as error:
            raise YouTubeSourceError('youtube_media_invalid') from error
        metadata = {
            'source_title': info.get('title'),
            'original_description': info.get('description'),
            'published_at': info.get('upload_date'),
            'author': {'display_name': info.get('uploader'), 'platform_account_id': info.get('channel_id')},
            'completed_status': info['live_status'], 'platform_duration_seconds': info['duration'],
            'captured_at': datetime.now(UTC).isoformat(), 'session_authority': authority,
            'audio_sha256': _digest(destination),
            'captions': _captions(info, work_dir / 'download'),
        }
        # The selected source is audio only; the download may contain video pixels.
        shutil.rmtree(work_dir / 'download')
        return CapturedMaterial('youtube', key, submitted_url, canonical, metadata, destination, duration)

    def reuse_retained(self, *, source_key, submitted_url, canonical_url, metadata, work_dir, expected_authority=None):
        media = work_dir / 'media' / 'source.mka'
        if not media.exists():
            return None
        current = connection_authority(self.store.connection('youtube'))
        if expected_authority is not None and expected_authority != current:
            raise ChromeSessionError('youtube_connection_changed')
        if metadata.get('session_authority') != current:
            return None
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(metadata['captured_at'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
        if age > 72 * 3600:
            return None
        try:
            if media.is_symlink() or _digest(media) != metadata.get('audio_sha256'):
                raise MediaVerificationError('audio identity changed')
            duration = self.verifier.verify(media, expected_duration_seconds=metadata.get('platform_duration_seconds'),
                                            complete_decode=True, require_video=False)
        except (OSError, MediaVerificationError) as error:
            raise YouTubeSourceError('youtube_media_invalid') from error
        return CapturedMaterial('youtube', source_key, submitted_url, canonical_url, metadata, media, duration)


def _qualify(info, key):
    if not isinstance(info, dict) or info.get('id') != key:
        raise YouTubeSourceError('youtube_identity_mismatch')
    duration = info.get('duration')
    if (info.get('live_status') not in {'not_live', 'was_live'} or
            isinstance(duration, bool) or not isinstance(duration, (int, float)) or
            not math.isfinite(duration) or duration <= 0):
        raise YouTubeSourceError('youtube_input_unsupported')


def download_youtube(key, canonical, cookies, work_dir):
    import yt_dlp
    node = shutil.which('node')
    if not node:
        raise YouTubeSourceError('youtube_runtime_unavailable')
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    def match(info, *, incomplete=False):
        if not incomplete:
            _qualify(info, key)
        return None
    options = {'cachedir': False, 'quiet': True, 'logger': _QuietDownloadLog(), 'noplaylist': True,
               'format': 'bestaudio/best', 'outtmpl': str(work_dir / '%(id)s.%(ext)s'),
               'js_runtimes': {'node': {'path': node}}, 'socket_timeout': 20,
               'retries': 0, 'extractor_retries': 0, 'match_filter': match,
               'writesubtitles': True, 'writeautomaticsub': True,
               'subtitleslangs': ['en', 'zh-Hans', 'zh-Hant'], 'subtitlesformat': 'vtt'}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            for row in cookies:
                expiry = int(row['expires']) if row.get('expires', -1) > 0 else None
                ydl.cookiejar.set_cookie(http.cookiejar.Cookie(
                    0, row['name'], row['value'], None, False, row['domain'], True,
                    row['domain'].startswith('.'), row.get('path', '/'), True,
                    row.get('secure', False), expiry, expiry is None, None, None, {}, False))
            info = ydl.extract_info(canonical, download=True)
            _qualify(info, key)
            media = Path(ydl.prepare_filename(info))
            if not media.is_file() or media.resolve().parent != work_dir.resolve():
                raise YouTubeSourceError('youtube_media_invalid')
            return info, media
    except YouTubeSourceError:
        shutil.rmtree(work_dir)
        raise
    except Exception as error:
        shutil.rmtree(work_dir)
        raise YouTubeSourceError('youtube_upstream_failed') from error


def _captions(info, root):
    rows = []
    for language, track in (info.get('requested_subtitles') or {}).items():
        path = Path(track.get('filepath') or '')
        if not path.is_file() or path.resolve().parent != root.resolve():
            raise YouTubeSourceError('youtube_caption_incomplete')
        text = path.read_text(encoding='utf-8')
        if not text.startswith('WEBVTT'):
            raise YouTubeSourceError('youtube_caption_incomplete')
        rows.append({'language': language, 'format': 'vtt', 'text': text,
                     'kind': 'manual' if language in (info.get('subtitles') or {}) else 'automatic'})
    return rows


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalize_downloaded_audio(media, work_dir, expected_duration, verifier, *, before_commit=None):
    """Mechanical yt-dlp audio conversion shared by video platforms."""
    destination = work_dir / 'media' / 'source.mka'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('source.pending.mka')
    try:
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(media),
                                 '-map', '0:a:0', '-vn', '-c:a', 'copy',
                                 '-fflags', '+bitexact', '-map_metadata', '-1', str(temporary)],
                                capture_output=True, timeout=max(60, expected_duration * 2))
        if result.returncode:
            raise MediaVerificationError('audio extraction failed')
        duration = verifier.verify(temporary, expected_duration_seconds=expected_duration,
                                   complete_decode=True, require_video=False)
        if before_commit is not None:
            before_commit()
        temporary.replace(destination)
        return destination, duration
    finally:
        temporary.unlink(missing_ok=True)
