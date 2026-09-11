from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from .chrome import ChromeSessionError


class XiaohongshuSourceError(RuntimeError):
    pass


def xiaohongshu_identity(value: str) -> tuple[str, str]:
    try:
        url = urlsplit(value.strip())
        match = re.fullmatch(r'/(?:explore|discovery/item|search_result)/([a-fA-F0-9]{24})/?', url.path)
        if (url.scheme not in {'http', 'https'} or url.hostname not in
                {'xiaohongshu.com', 'www.xiaohongshu.com'} or url.port or
                url.username or url.password or not match):
            raise ValueError
        key = match[1].lower()
        return key, f'https://www.xiaohongshu.com/explore/{key}'
    except ValueError as error:
        raise ValueError('请提交一条小红书普通图文或视频笔记链接。') from error


def xiaohongshu_input(value):
    """Validate a locator without network work; shortlinks resolve in the worker."""
    url = urlsplit(value.strip())
    if (url.scheme in {'http', 'https'} and url.hostname in {'xhslink.com', 'www.xhslink.com'}
            and not url.username and not url.password and not url.port
            and re.fullmatch(r'/(?:a/|o/)?[A-Za-z0-9]+/?', url.path)):
        return None, None
    return xiaohongshu_identity(value)


def connection_authority(row):
    if row is None or row['state'] == 'unconfigured':
        raise ChromeSessionError('xiaohongshu_not_configured')
    if row['state'] != 'connected':
        raise ChromeSessionError('xiaohongshu_login_required')
    context = row['browser_context']
    if not context:
        raise ChromeSessionError('xiaohongshu_not_configured')
    instance = uuid5(NAMESPACE_URL, f"xiaohongshu:{row['connected_at']}:{row['generation']}:{context}").hex
    return {'class': 'EXISTING_BROWSER_OWNED', 'connection_instance_id': instance,
            'session_authority_ref': f'browser:{instance}', 'generation': row['generation'],
            'connected_at': row['connected_at'], 'browser_context': context}


class XiaohongshuSession:
    def read(self, url, context=None):
        from .opencli_session import read_opencli
        return read_opencli('xiaohongshu', 'xiaohongshu', url, context)

    def verify(self):
        return self.read('https://www.xiaohongshu.com/explore')['contextId']


def preview_title(store, session, url):
    """Read native identity/title before queueing, without downloading media."""
    key, _ = xiaohongshu_input(url)
    authority = connection_authority(store.connection('xiaohongshu'))
    state = session.read(url, authority['browser_context'])
    if (state.get('contextId') != authority['browser_context']
            or connection_authority(store.connection('xiaohongshu')) != authority):
        raise ChromeSessionError('xiaohongshu_connection_changed')
    if key is None:
        key, _ = xiaohongshu_identity(state.get('pageUrl', ''))
    note, _ = qualify_note(state, key)
    return (note['title'].strip() or note['desc'].strip().split('\n')[0] or '笔记 ' + key), authority


@dataclass(frozen=True)
class NoteMedia:
    member_id: str
    kind: str
    path: Path
    sha256: str
    mime_type: str
    width: int
    height: int
    duration_seconds: float | None = None
    audio_present: bool = False

    def manifest(self):
        return {k: v for k, v in vars(self).items() if k != 'path'}


@dataclass(frozen=True)
class CapturedNote:
    source_key: str
    submitted_url: str
    canonical_url: str
    metadata: dict
    members: tuple[NoteMedia, ...]
    source_kind: str = 'xiaohongshu'

    @property
    def media_path(self):
        return self.members[0].path

    @property
    def duration_seconds(self):
        return self.members[0].duration_seconds


def qualify_note(state, key):
    if not isinstance(state, dict) or state.get('loggedIn') is not True or state.get('loginPrompt'):
        raise ChromeSessionError('xiaohongshu_login_required')
    note = state.get('note')
    try:
        resolved, _ = xiaohongshu_identity(state['pageUrl'])
    except (KeyError, ValueError):
        raise XiaohongshuSourceError('xiaohongshu_identity_mismatch')
    if not isinstance(note, dict) or resolved != key or note.get('noteId') != key:
        raise XiaohongshuSourceError('xiaohongshu_identity_mismatch')
    if note.get('type') not in {'normal', 'video'} or note.get('isLongNote') or note.get('longNote'):
        raise XiaohongshuSourceError('xiaohongshu_input_unsupported')
    if not isinstance(note.get('title'), str) or not isinstance(note.get('desc'), str):
        raise XiaohongshuSourceError('xiaohongshu_text_incomplete')
    media = []
    if note['type'] == 'normal':
        images = note.get('imageList')
        if not isinstance(images, list):
            raise XiaohongshuSourceError('xiaohongshu_media_incomplete')
        for index, entry in enumerate(images, 1):
            if not isinstance(entry, dict) or not entry.get('urlDefault'):
                raise XiaohongshuSourceError('xiaohongshu_media_incomplete')
            media.append((f'image-{index}', 'image', entry['urlDefault'], None))
        if not media and not (note['title'] + note['desc']).strip():
            raise XiaohongshuSourceError('xiaohongshu_text_incomplete')
    else:
        video = note.get('video') or {}
        stream = _video_stream(video)
        milliseconds = stream.get('duration') if stream else None
        duration = milliseconds / 1000 if isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool) else None
        if not stream or isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise XiaohongshuSourceError('xiaohongshu_media_incomplete')
        media.append(('video-1', 'video', stream['masterUrl'], duration))
    for _, _, url, _ in media:
        _media_url(url)
    return note, media


def _video_stream(video):
    if video.get('media', {}).get('video', {}).get('drmType', 0) != 0:
        raise XiaohongshuSourceError('xiaohongshu_input_unsupported')
    streams = video.get('media', {}).get('stream', {})
    candidates = [s for group in streams.values() if isinstance(group, list) for s in group
                  if isinstance(s, dict) and s.get('masterUrl') and s.get('format') == 'mp4']
    # Select one complete native stream. Alternate encodings are not extra members.
    return min(candidates, key=lambda s: s.get('size') or float('inf')) if candidates else None


def native_video_fact(metadata, fact):
    from .domain import SourceFact
    prefix = metadata['source_title'] + '\n\n' + metadata['original_description'] + '\n\n'
    return SourceFact(prefix + fact.snapshot, tuple(
        {**u, **({'start': u['start'] + len(prefix), 'end': u['end'] + len(prefix)} if 'start' in u and 'end' in u else {})}
        for u in fact.uncertainties))


def video_segments(metadata, transcript):
    from difflib import SequenceMatcher
    chunks = metadata.get('transcript_chunks', [])
    original = ''.join(c['text'] for c in chunks)
    offset = len(metadata['source_title']) + len(metadata['original_description']) + 4
    result = []
    for tag, a, b, c, d in SequenceMatcher(None, original, transcript, autojunk=False).get_opcodes():
        if c == d:
            continue
        position = 0
        selected = []
        for chunk in chunks:
            end = position + len(chunk['text'])
            if position < b and end > a or a == b and position <= a <= end:
                selected.append(chunk)
            position = end
        if selected:
            result.append({'start': offset + c, 'end': offset + d, 'member_id': 'video-1',
                           'start_seconds': selected[0]['start_seconds'], 'end_seconds': selected[-1]['end_seconds'],
                           'granularity': 'asr_chunk'})
    return result


def _media_url(value):
    url = urlsplit(value)
    if (url.scheme not in {'http', 'https'} or url.username or url.password or url.port or
            not (url.hostname or '').endswith('.xhscdn.com')):
        raise XiaohongshuSourceError('xiaohongshu_media_incomplete')


def download_media(url, destination):
    import httpx
    _media_url(url)
    try:
        with httpx.stream('GET', url, headers={'Referer': 'https://www.xiaohongshu.com/'}, timeout=60) as response:
            response.raise_for_status()
            with destination.open('xb') as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
    except (httpx.HTTPError, OSError) as error:
        raise XiaohongshuSourceError('xiaohongshu_media_incomplete') from error


def verify_media(path, member_id, kind, expected_duration=None):
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                               capture_output=True, timeout=30, check=True)
        info = json.loads(probe.stdout)
        visual = next(s for s in info['streams'] if s['codec_type'] == 'video')
        duration = None
        if kind == 'image':
            mime = {'png': 'image/png', 'mjpeg': 'image/jpeg', 'webp': 'image/webp'}[visual['codec_name']]
        else:
            mime = 'video/mp4'
            duration = float(info['format']['duration'])
            if not math.isfinite(duration) or duration <= 0 or abs(duration - expected_duration) > max(1, expected_duration * .03):
                raise ValueError
        decode = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'],
                                capture_output=True, timeout=max(60, (duration or 1) * 2), check=True)
        if decode.stderr:
            raise ValueError
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        audio_present = any(s.get('codec_type') == 'audio' for s in info['streams'])
        return NoteMedia(member_id, kind, path, digest, mime, int(visual['width']), int(visual['height']), duration, audio_present)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as error:
        raise XiaohongshuSourceError('xiaohongshu_media_invalid') from error


class XiaohongshuSource:
    def __init__(self, store, session=None, downloader=download_media, verifier=verify_media):
        self.store = store
        self.session = session or XiaohongshuSession()
        self.downloader = downloader
        self.verifier = verifier

    def capture(self, submitted_url, work_dir, *, expected_authority):
        key, canonical = xiaohongshu_input(submitted_url)
        authority = connection_authority(self.store.connection('xiaohongshu'))
        if authority != expected_authority:
            raise ChromeSessionError('xiaohongshu_connection_changed')
        try:
            state = self.session.read(submitted_url, authority['browser_context'])
            if state.get('contextId') != authority['browser_context']:
                raise ChromeSessionError('xiaohongshu_connection_changed')
            if key is None:
                try:
                    key, canonical = xiaohongshu_identity(state.get('pageUrl', ''))
                except ValueError as error:
                    raise XiaohongshuSourceError('xiaohongshu_identity_mismatch') from error
            note, selected = qualify_note(state, key)
        except ChromeSessionError as error:
            if str(error) == 'xiaohongshu_login_required':
                self.store.require_relogin('xiaohongshu')
            raise
        root = work_dir / 'xhs-media'
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        members = []
        try:
            for member_id, kind, url, duration in selected:
                path = root / member_id
                self.downloader(url, path)
                member = self.verifier(path, member_id, kind, duration)
                if kind == 'video':
                    stream = _video_stream(note['video'])
                    if stream.get('audioDuration', 0) > 0 and not member.audio_present:
                        raise XiaohongshuSourceError('xiaohongshu_media_incomplete')
                    if stream.get('size') and path.stat().st_size != stream['size']:
                        raise XiaohongshuSourceError('xiaohongshu_media_incomplete')
                members.append(member)
            if connection_authority(self.store.connection('xiaohongshu')) != authority:
                raise ChromeSessionError('xiaohongshu_connection_changed')
        except Exception:
            shutil.rmtree(root)
            raise
        metadata = {'note_kind': note['type'], 'source_title': note['title'], 'original_description': note['desc'],
                    'captured_at': datetime.now(UTC).isoformat(), 'session_authority': authority,
                    'author': {'display_name': (note.get('user') or {}).get('nickname'),
                               'platform_account_id': (note.get('user') or {}).get('userId')},
                    'published_at': note.get('time'), 'updated_at': note.get('lastUpdateTime'),
                    'media_members': [m.manifest() for m in members]}
        return CapturedNote(key, submitted_url, canonical, metadata, tuple(members))

    def reuse_retained(self, *, source_key, submitted_url, canonical_url, metadata, work_dir, expected_authority):
        current = connection_authority(self.store.connection('xiaohongshu'))
        if current != expected_authority:
            raise ChromeSessionError('xiaohongshu_connection_changed')
        if metadata.get('session_authority') != current:
            return None
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(metadata['captured_at'])).total_seconds()
            if not 0 <= age <= 72 * 3600:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        members = []
        for entry in metadata.get('media_members', []):
            member_id = entry['member_id']
            if not re.fullmatch(r'(?:image-[1-9][0-9]*|video-1)', member_id):
                raise XiaohongshuSourceError('xiaohongshu_media_invalid')
            path = work_dir / 'xhs-media' / member_id
            if not path.exists():
                return None
            if path.is_symlink():
                raise XiaohongshuSourceError('xiaohongshu_media_invalid')
            member = self.verifier(path, member_id, entry['kind'], entry.get('duration_seconds'))
            if member.manifest() != entry:
                raise XiaohongshuSourceError('xiaohongshu_media_invalid')
            members.append(member)
        return CapturedNote(source_key, submitted_url, canonical_url, metadata, tuple(members))
