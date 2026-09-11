from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

from knowledge_distiller.media import FFmpegMediaVerifier, MediaVerificationError

from .chrome import DouyinChromeSession, ChromeSessionError
from .domain import CapturedMaterial
from .douyin_text import DouyinText, DouyinTextError, parse_douyin_text
from .xiaohongshu import CapturedNote, verify_media, XiaohongshuSourceError


logger = logging.getLogger(__name__)


class DouyinSourceError(RuntimeError):
    pass


class _LoginRequired(Exception):
    pass


class _Unsupported(Exception):
    pass


class _SourceUnavailable(Exception):
    pass


class _UpstreamFailed(Exception):
    pass


@dataclass(frozen=True)
class DouyinDownload:
    item_id: str
    canonical_url: str
    media_path: Path
    expected_duration_seconds: float | None
    author_name: str | None
    author_id: str | None
    description: str | None
    published_at: str | None
    native_content_version: str | None = None


@dataclass(frozen=True)
class DouyinTextDownload:
    native: DouyinText
    paths: tuple[Path, ...]
    metadata: dict


class DouyinSource:
    def __init__(
        self,
        session: DouyinChromeSession,
        binding_factory: Callable[[Mapping[str, str]], InstalledDouyinBinding] | None = None,
        verifier: FFmpegMediaVerifier | None = None,
    ):
        self._session = session
        self._binding_factory = binding_factory
        self._verifier = verifier or FFmpegMediaVerifier()

    def capture(self, submitted_url: str, work_dir: Path) -> CapturedMaterial:
        authority = None
        if hasattr(self._session, 'store'):
            from .douyin_collections import connection_authority
            authority = connection_authority(self._session.store)
        binding = (self._binding_factory(self._session.cookies()) if self._binding_factory
                   else InstalledDouyinBinding(self._session.cookies(), session=self._session))
        try:
            downloaded = binding.download(submitted_url, work_dir / "download")
        except _LoginRequired as error:
            if authority is not None and connection_authority(self._session.store) == authority:
                self._session.store.require_relogin('douyin')
            raise DouyinSourceError("douyin_login_required") from error
        except _Unsupported as error:
            raise DouyinSourceError("douyin_input_unsupported") from error
        except _SourceUnavailable as error:
            raise DouyinSourceError("douyin_source_unavailable") from error
        except _UpstreamFailed as error:
            raise DouyinSourceError("douyin_upstream_failed") from error

        if isinstance(downloaded, DouyinTextDownload):
            if authority is not None and connection_authority(self._session.store) != authority:
                raise DouyinSourceError('douyin_connection_changed')
            members = []
            try:
                for image, path in zip(downloaded.native.images, downloaded.paths, strict=True):
                    members.append(verify_media(path, image.member_id, 'image'))
            except XiaohongshuSourceError as error:
                raise DouyinSourceError('douyin_media_invalid') from error
            native = downloaded.native
            metadata = {**downloaded.metadata, 'captured_at': datetime.now(UTC).isoformat(),
                'note_kind': 'normal', 'native_kind': native.kind, 'source_title': native.title,
                'original_description': native.body, 'original_markdown': native.original_markdown,
                'native_content_version': native.native_content_version,
                'media_members': [m.manifest() for m in members]}
            if native.motion_omitted:
                metadata['motion_omitted'] = list(native.motion_omitted)
                metadata['source_scope'] = '仅处理静态原图和配文；动态部分及其声音未处理。'
            if authority is not None:
                metadata['session_authority'] = authority
            return CapturedNote(native.item_id, submitted_url, native.canonical_url,
                                metadata, tuple(members), source_kind='douyin')

        destination = work_dir / "media" / "source.mp4"
        try:
            _copy_complete(downloaded.media_path, destination)
            duration = self._verifier.verify(
                destination,
                expected_duration_seconds=downloaded.expected_duration_seconds,
                complete_decode=True,
            )
        except (OSError, MediaVerificationError) as error:
            destination.unlink(missing_ok=True)
            raise DouyinSourceError("douyin_media_invalid") from error

        if authority is not None and connection_authority(self._session.store) != authority:
            raise DouyinSourceError('douyin_connection_changed')
        metadata: dict[str, object] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "original_description": downloaded.description,
            "published_at": downloaded.published_at,
        }
        if downloaded.native_content_version:
            metadata["native_content_version"] = downloaded.native_content_version
        if downloaded.author_name or downloaded.author_id:
            metadata["author"] = {
                "display_name": downloaded.author_name,
                "platform_account_id": downloaded.author_id,
            }
        return CapturedMaterial(
            source_kind="douyin",
            source_key=downloaded.item_id,
            submitted_url=submitted_url,
            canonical_url=downloaded.canonical_url,
            metadata=metadata,
            media_path=destination,
            duration_seconds=duration,
        )

    def reuse_retained(
        self,
        *,
        source_key: str,
        submitted_url: str,
        canonical_url: str,
        metadata: Mapping[str, object],
        work_dir: Path,
    ) -> CapturedMaterial | None:
        if metadata.get('note_kind') == 'normal':
            # Reacquire and compare the complete authored snapshot on retry.
            return None
        media = work_dir / "media" / "source.mp4"
        if not media.exists():
            return None
        try:
            if media.is_symlink():
                raise MediaVerificationError("Retained media cannot be a symlink")
            duration = self._verifier.verify(
                media,
                expected_duration_seconds=None,
                complete_decode=True,
            )
        except (OSError, MediaVerificationError) as error:
            media.unlink(missing_ok=True)
            raise DouyinSourceError("douyin_media_invalid") from error
        return CapturedMaterial(
            source_kind="douyin",
            source_key=source_key,
            submitted_url=submitted_url,
            canonical_url=canonical_url,
            metadata=metadata,
            media_path=media,
            duration_seconds=duration,
        )


class InstalledDouyinBinding:
    def __init__(self, cookies: Mapping[str, str], *, session=None):
        self._cookies = dict(cookies)
        self._session = session

    def download(self, submitted_url: str, work_dir: Path) -> DouyinDownload:
        return asyncio.run(self._download(submitted_url, work_dir))

    async def _download(self, submitted_url: str, work_dir: Path) -> DouyinDownload:
        from .douyin_collections import CollectionError
        if not self._cookies:
            raise _LoginRequired
        try:
            from config import ConfigLoader
            from core.api_client import DouyinAPIClient, LoginRequiredError
            from core.url_parser import URLParser
            from core.video_downloader import VideoDownloader
            from storage import FileManager
            from utils.validators import is_short_url, normalize_short_url
        except ImportError as error:
            raise _UpstreamFailed from error

        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            from contextlib import AsyncExitStack
            from .douyin_collection_browser import BrowserCollectionClient
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(DouyinAPIClient(self._cookies))
                detail_client = (await stack.enter_async_context(BrowserCollectionClient(self._session))
                                 if self._session is not None else client)
                resolved = submitted_url
                if is_short_url(resolved):
                    resolved = await detail_client.resolve_short_url(
                        normalize_short_url(resolved)
                    )
                parsed = parse_work_url(resolved) if resolved else None
                if not parsed or parsed.get("type") not in {"video", "gallery", "article"}:
                    raise _Unsupported
                item_id = str(parsed.get("aweme_id") or "")
                if not item_id:
                    raise _SourceUnavailable

                detail = await detail_client.get_video_detail(item_id)
                if not isinstance(detail, dict) or str(detail.get("aweme_id") or "") != item_id:
                    raise _SourceUnavailable
                try:
                    native = parse_douyin_text(detail)
                except DouyinTextError as error:
                    raise DouyinSourceError(str(error)) from error
                if native is not None:
                    paths = []
                    for image in native.images:
                        destination = work_dir / image.member_id
                        destination.unlink(missing_ok=True)
                        await asyncio.to_thread(_download_image, image.url, destination)
                        paths.append(destination)
                    current = await detail_client.get_video_detail(item_id)
                    if not isinstance(current, dict):
                        raise DouyinSourceError('douyin_upstream_failed')
                    if content_version(current) != native.native_content_version:
                        raise DouyinSourceError('source_snapshot_changed')
                    author_name, author_id, _, published_at = _metadata(detail)
                    return DouyinTextDownload(native, tuple(paths), {
                        'author': {'display_name': author_name, 'platform_account_id': author_id},
                        'published_at': published_at})
                downloader = VideoDownloader(
                    _download_config(work_dir),
                    client,
                    FileManager(str(work_dir)),
                    _CookieManager(self._cookies),
                    database=None,
                )
                result = await downloader.download(parsed)
                if not _one_success(result):
                    raise _SourceUnavailable
                media_path = _find_media(work_dir, item_id)
                if media_path is None:
                    raise _SourceUnavailable
                current = await detail_client.get_video_detail(item_id)
                if not isinstance(current, dict) or content_version(current) != content_version(detail):
                    raise DouyinSourceError('source_snapshot_changed')
                author_name, author_id, description, published_at = _metadata(detail)
                return DouyinDownload(
                    item_id=item_id,
                    canonical_url=f"https://www.douyin.com/video/{item_id}",
                    media_path=media_path,
                    expected_duration_seconds=_duration(detail.get("duration")),
                    author_name=author_name,
                    author_id=author_id,
                    description=description,
                    published_at=published_at,
                    native_content_version=content_version(detail),
                )
        except ChromeSessionError as error:
            if str(error) == 'douyin_login_required':
                raise _LoginRequired from error
            raise
        except LoginRequiredError as error:
            raise _LoginRequired from error
        except CollectionError as error:
            raise DouyinSourceError(str(error)) from error
        except (_LoginRequired, _Unsupported, _SourceUnavailable, DouyinSourceError):
            raise
        except Exception as error:
            logger.warning("Douyin download failed: %s", type(error).__name__)
            raise _UpstreamFailed from error


class _CookieManager:
    def __init__(self, cookies: Mapping[str, str]):
        self._cookies = dict(cookies)

    def get_cookies(self) -> Mapping[str, str]:
        return self._cookies


def _download_config(work_dir: Path):
    from config import ConfigLoader

    config = ConfigLoader()
    config.update(
        path=str(work_dir),
        video=True,
        cover=False,
        music=False,
        avatar=False,
        json=False,
        database=False,
        folderstyle=False,
        group_by_mode=False,
        filename_template="{id}",
        folder_template="{id}",
        comments={"enabled": False},
        transcript={"enabled": False},
    )
    return config


def _one_success(result: object) -> bool:
    counts = tuple(getattr(result, name, None) for name in ("total", "success", "failed", "skipped"))
    if any(not isinstance(value, int) or isinstance(value, bool) for value in counts):
        return False
    total, success, failed, skipped = counts
    return total == 1 and failed == 0 and success + skipped == 1


def _find_media(work_dir: Path, item_id: str) -> Path | None:
    matches = [
        path
        for path in work_dir.rglob(f"{item_id}.mp4")
        if path.is_file() and not path.is_symlink()
    ]
    return matches[0] if len(matches) == 1 else None


def _copy_complete(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _metadata(
    detail: Mapping[str, object],
) -> tuple[str | None, str | None, str | None, str | None]:
    author = detail.get("author")
    if isinstance(author, Mapping):
        author_name = _text(author.get("nickname"))
        author_id = _text(author.get("sec_uid")) or _text(author.get("uid"))
    else:
        author_name, author_id = None, None
    return (
        author_name,
        author_id,
        _text(detail.get("desc")),
        _published_at(detail.get("create_time")),
    )


def _duration(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(milliseconds) or milliseconds <= 0:
        return None
    return milliseconds / 1000


def _published_at(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
        if not math.isfinite(timestamp) or timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp, UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def content_version(detail):
    try:
        native = parse_douyin_text(detail)
    except DouyinTextError:
        native = None
    if native is not None:
        return native.native_content_version
    import hashlib
    import json
    video = detail.get('video') or {}
    play = video.get('play_addr') or {}
    selected = {'id': str(detail.get('aweme_id') or ''), 'kind': detail.get('aweme_type'),
                'description': detail.get('desc'), 'duration': detail.get('duration') or video.get('duration'),
                'media_id': video.get('vid') or play.get('uri'),
                'images': [i.get('uri') for i in detail.get('images') or []]}
    return hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def parse_work_url(url):
    import re
    from urllib.parse import urlsplit
    from core.url_parser import URLParser
    parsed = urlsplit(url)
    if parsed.hostname in {'douyin.com', 'www.douyin.com'}:
        match = re.fullmatch(r'/article/([0-9]+)/?', parsed.path)
        if match:
            return {'type': 'article', 'aweme_id': match[1], 'original_url': url}
    return URLParser.parse(url)


def _download_image(url, destination):
    import httpx
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ''
    if not any(host.endswith('.'+domain) or host == domain for domain in
               ('douyinpic.com','douyincdn.com','byteimg.com','ibytedtos.com','pstatp.com')):
        raise DouyinSourceError('douyin_media_invalid')
    try:
        with httpx.stream('GET', url, headers={'Referer': 'https://www.douyin.com/'}, timeout=60) as response:
            response.raise_for_status()
            with destination.open('xb') as stream:
                for chunk in response.iter_bytes():
                    stream.write(chunk)
    except (httpx.HTTPError, OSError) as error:
        raise DouyinSourceError('douyin_media_invalid') from error
