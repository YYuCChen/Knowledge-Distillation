from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Protocol

from ..identity import (
    ConfirmedMaterialIdentity,
    IdentityFailure,
    IdentityResolution,
)
from ..media import (
    FFmpegMediaVerifier,
    MediaAcquisition,
    MediaAcquisitionKind,
    MediaFailure,
    MediaVerificationError,
    VerifiedTemporaryMedia,
)


logger = logging.getLogger(__name__)


class LoginContext(Protocol):
    def get_cookies(self) -> Mapping[str, str]: ...


class EmptyLoginContext:
    def get_cookies(self) -> Mapping[str, str]:
        return {}


class JsonFileLoginContext:
    def __init__(self, cookie_file: Path):
        self.cookie_file = cookie_file

    def get_cookies(self) -> Mapping[str, str]:
        try:
            value = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(key): str(item)
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str) and item
        }


class _CandidateALoginRequired(Exception):
    pass


class _CandidateAInputUnsupported(Exception):
    pass


class _CandidateAIdentityUnconfirmed(Exception):
    pass


class _CandidateAUpstreamFailure(Exception):
    pass


@dataclass(frozen=True)
class _CandidateAIdentity:
    platform_item_id: str
    canonical_url: str


@dataclass(frozen=True)
class _CandidateADownloadReport:
    platform_item_id: str
    platform_duration_seconds: float | None
    total: int
    success: int
    failed: int
    skipped: int
    media_path: Path | None
    author_name: str | None = None
    author_platform_id: str | None = None
    original_description: str | None = None
    published_at: str | None = None


class CandidateABinding(Protocol):
    def confirm_identity(self, target_url: str) -> _CandidateAIdentity: ...

    def download_media(
        self,
        target_url: str,
        candidate_work_dir: Path,
    ) -> _CandidateADownloadReport: ...


class _MemoryCookieManager:
    def __init__(self, cookies: Mapping[str, str]):
        self.cookies = dict(cookies)

    def get_cookies(self) -> Mapping[str, str]:
        return self.cookies


class InstalledCandidateABinding:
    def __init__(self, login_context: LoginContext):
        self.login_context = login_context

    def confirm_identity(self, target_url: str) -> _CandidateAIdentity:
        return asyncio.run(self._confirm_identity(target_url))

    def download_media(
        self,
        target_url: str,
        candidate_work_dir: Path,
    ) -> _CandidateADownloadReport:
        return asyncio.run(self._download_media(target_url, candidate_work_dir))

    async def _confirm_identity(self, target_url: str) -> _CandidateAIdentity:
        cookies = dict(self.login_context.get_cookies())
        if not cookies:
            raise _CandidateALoginRequired

        try:
            from core.api_client import DouyinAPIClient, LoginRequiredError
            from core.url_parser import URLParser
            from utils.validators import is_short_url, normalize_short_url
        except ImportError as error:
            raise _CandidateAUpstreamFailure from error

        try:
            async with DouyinAPIClient(cookies) as api_client:
                resolved_url = target_url
                if is_short_url(resolved_url):
                    resolved_url = await api_client.resolve_short_url(
                        normalize_short_url(resolved_url)
                    )
                if not resolved_url:
                    raise _CandidateAIdentityUnconfirmed

                parsed = URLParser.parse(resolved_url)
                if not parsed or parsed.get("type") != "video":
                    raise _CandidateAInputUnsupported
                parsed_id = str(parsed.get("aweme_id") or "")
                if not parsed_id:
                    raise _CandidateAIdentityUnconfirmed

                detail = await api_client.get_video_detail(parsed_id)
                if not isinstance(detail, dict):
                    raise _CandidateAIdentityUnconfirmed
                detail_id = str(detail.get("aweme_id") or "")
                if not detail_id or detail_id != parsed_id:
                    raise _CandidateAIdentityUnconfirmed

                return _CandidateAIdentity(
                    platform_item_id=detail_id,
                    canonical_url=f"https://www.douyin.com/video/{detail_id}",
                )
        except LoginRequiredError as error:
            raise _CandidateALoginRequired from error
        except (
            _CandidateAInputUnsupported,
            _CandidateAIdentityUnconfirmed,
        ):
            raise
        except Exception as error:
            logger.warning(
                "Douyin identity lookup failed in upstream binding: %s",
                type(error).__name__,
            )
            raise _CandidateAUpstreamFailure from error

    async def _download_media(
        self,
        target_url: str,
        candidate_work_dir: Path,
    ) -> _CandidateADownloadReport:
        cookies = dict(self.login_context.get_cookies())
        if not cookies:
            raise _CandidateALoginRequired

        try:
            from config import ConfigLoader
            from core.api_client import DouyinAPIClient, LoginRequiredError
            from core.url_parser import URLParser
            from core.video_downloader import VideoDownloader
            from storage import FileManager
            from utils.validators import is_short_url, normalize_short_url
        except ImportError as error:
            raise _CandidateAUpstreamFailure from error

        candidate_work_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with DouyinAPIClient(cookies) as api_client:
                resolved_url = target_url
                if is_short_url(resolved_url):
                    resolved_url = await api_client.resolve_short_url(
                        normalize_short_url(resolved_url)
                    )
                if not resolved_url:
                    raise _CandidateAIdentityUnconfirmed

                parsed = URLParser.parse(resolved_url)
                if not parsed or parsed.get("type") != "video":
                    raise _CandidateAInputUnsupported
                parsed_id = str(parsed.get("aweme_id") or "")
                if not parsed_id:
                    raise _CandidateAIdentityUnconfirmed

                detail = await api_client.get_video_detail(parsed_id)
                if not isinstance(detail, dict):
                    raise _CandidateAIdentityUnconfirmed
                detail_id = str(detail.get("aweme_id") or "")
                if not detail_id or detail_id != parsed_id:
                    raise _CandidateAIdentityUnconfirmed

                config = ConfigLoader()
                config.update(
                    path=str(candidate_work_dir),
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
                downloader = VideoDownloader(
                    config,
                    api_client,
                    FileManager(str(candidate_work_dir)),
                    _MemoryCookieManager(cookies),
                    database=None,
                )
                result = await downloader.download(parsed)
                media_path = _find_candidate_media(candidate_work_dir, detail_id)
                (
                    author_name,
                    author_platform_id,
                    original_description,
                    published_at,
                ) = _source_metadata_from_detail(detail)
                return _CandidateADownloadReport(
                    platform_item_id=detail_id,
                    platform_duration_seconds=_platform_duration_seconds(detail),
                    total=result.total,
                    success=result.success,
                    failed=result.failed,
                    skipped=result.skipped,
                    media_path=media_path,
                    author_name=author_name,
                    author_platform_id=author_platform_id,
                    original_description=original_description,
                    published_at=published_at,
                )
        except LoginRequiredError as error:
            raise _CandidateALoginRequired from error
        except (
            _CandidateAInputUnsupported,
            _CandidateAIdentityUnconfirmed,
        ):
            raise
        except Exception as error:
            logger.warning(
                "Douyin media acquisition failed in upstream binding: %s",
                type(error).__name__,
            )
            raise _CandidateAUpstreamFailure from error


class CandidateAIdentityAdapter:
    def __init__(self, binding: CandidateABinding):
        self.binding = binding

    def identify(self, original_url: str, target_url: str) -> IdentityResolution:
        try:
            candidate_identity = self.binding.confirm_identity(target_url)
        except _CandidateALoginRequired:
            return IdentityResolution.failed(IdentityFailure.LOGIN_REQUIRED)
        except _CandidateAInputUnsupported:
            return IdentityResolution.failed(IdentityFailure.INPUT_UNSUPPORTED)
        except _CandidateAIdentityUnconfirmed:
            return IdentityResolution.failed(IdentityFailure.IDENTITY_UNCONFIRMED)
        except _CandidateAUpstreamFailure:
            return IdentityResolution.failed(IdentityFailure.UPSTREAM_FAILURE)

        return IdentityResolution.confirmed(
            ConfirmedMaterialIdentity(
                platform="douyin",
                platform_item_id=candidate_identity.platform_item_id,
                original_url=original_url,
                canonical_url=candidate_identity.canonical_url,
            )
        )


class CandidateAMediaAdapter:
    def __init__(
        self,
        binding: CandidateABinding,
        verifier: FFmpegMediaVerifier | None = None,
    ):
        self.binding = binding
        self.verifier = verifier or FFmpegMediaVerifier()

    def acquire(
        self,
        identity: ConfirmedMaterialIdentity,
        target_url: str,
        work_dir: Path,
    ) -> MediaAcquisition:
        project_media_path = work_dir / "media" / "source.mp4"
        try:
            report = self.binding.download_media(
                target_url,
                work_dir / "candidate-a",
            )
        except _CandidateALoginRequired:
            return MediaAcquisition.failed(MediaFailure.LOGIN_REQUIRED)
        except (
            _CandidateAInputUnsupported,
            _CandidateAIdentityUnconfirmed,
        ):
            return MediaAcquisition.failed(MediaFailure.SOURCE_UNAVAILABLE)
        except _CandidateAUpstreamFailure:
            return MediaAcquisition.failed(MediaFailure.UPSTREAM_FAILURE)

        if report.platform_item_id != identity.platform_item_id:
            return MediaAcquisition.failed(MediaFailure.UPSTREAM_RESULT_INVALID)
        if not _has_valid_single_item_counts(report):
            return MediaAcquisition.failed(MediaFailure.UPSTREAM_RESULT_INVALID)
        if report.failed == 1:
            return MediaAcquisition.failed(MediaFailure.SOURCE_UNAVAILABLE)
        kind = (
            MediaAcquisitionKind.DOWNLOADED
            if report.success == 1
            else MediaAcquisitionKind.REUSED
        )
        if report.media_path is None:
            return MediaAcquisition.failed(MediaFailure.SOURCE_UNAVAILABLE)

        try:
            _copy_to_project_media(report.media_path, project_media_path)
        except OSError:
            return MediaAcquisition.failed(MediaFailure.UPSTREAM_FAILURE)
        verified = self._verify_and_build(
            identity,
            project_media_path,
            report=report,
            expected_duration_seconds=report.platform_duration_seconds,
            kind=kind,
            complete_decode=kind is MediaAcquisitionKind.DOWNLOADED,
        )
        if verified.failure is not None:
            project_media_path.unlink(missing_ok=True)
        return verified

    def _verify_and_build(
        self,
        identity: ConfirmedMaterialIdentity,
        media_path: Path,
        *,
        report: _CandidateADownloadReport,
        expected_duration_seconds: float | None,
        kind: MediaAcquisitionKind,
        complete_decode: bool,
    ) -> MediaAcquisition:
        try:
            duration_seconds = self.verifier.verify(
                media_path,
                expected_duration_seconds=expected_duration_seconds,
                complete_decode=complete_decode,
            )
        except MediaVerificationError as error:
            logger.warning("Media integrity verification failed: %s", error)
            return MediaAcquisition.failed(MediaFailure.INTEGRITY_FAILED)
        return MediaAcquisition.succeeded(
            kind,
            VerifiedTemporaryMedia(
                platform=identity.platform,
                platform_item_id=identity.platform_item_id,
                path=media_path,
                duration_seconds=duration_seconds,
                author_name=report.author_name,
                author_platform_id=report.author_platform_id,
                original_description=report.original_description,
                published_at=report.published_at,
            ),
        )


def build_candidate_a_resolver(cookie_file: Path | None) -> CandidateAIdentityAdapter:
    login_context: LoginContext
    if cookie_file is None:
        login_context = EmptyLoginContext()
    else:
        login_context = JsonFileLoginContext(cookie_file)
    return CandidateAIdentityAdapter(InstalledCandidateABinding(login_context))


def build_candidate_a_adapters(
    cookie_file: Path | None,
) -> tuple[CandidateAIdentityAdapter, CandidateAMediaAdapter]:
    login_context: LoginContext
    if cookie_file is None:
        login_context = EmptyLoginContext()
    else:
        login_context = JsonFileLoginContext(cookie_file)
    binding = InstalledCandidateABinding(login_context)
    return CandidateAIdentityAdapter(binding), CandidateAMediaAdapter(binding)


def _find_candidate_media(work_dir: Path, platform_item_id: str) -> Path | None:
    matches = [
        path
        for path in work_dir.rglob(f"{platform_item_id}.mp4")
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _platform_duration_seconds(detail: Mapping[str, object]) -> float | None:
    value = detail.get("duration")
    if isinstance(value, bool):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(milliseconds) or milliseconds <= 0:
        return None
    return milliseconds / 1000


def _source_metadata_from_detail(
    detail: Mapping[str, object],
) -> tuple[str | None, str | None, str | None, str | None]:
    author = detail.get("author")
    if isinstance(author, Mapping):
        author_name = _optional_source_text(author.get("nickname"))
        author_platform_id = _optional_source_text(author.get("sec_uid"))
        if author_platform_id is None:
            author_platform_id = _optional_source_text(author.get("uid"))
    else:
        author_name = None
        author_platform_id = None
    return (
        author_name,
        author_platform_id,
        _optional_source_text(detail.get("desc")),
        _published_at(detail.get("create_time")),
    )


def _optional_source_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _published_at(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _has_valid_single_item_counts(report: _CandidateADownloadReport) -> bool:
    counts = (report.total, report.success, report.failed, report.skipped)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in counts):
        return False
    if report.total != 1 or min(counts) < 0:
        return False
    if report.success + report.failed + report.skipped != report.total:
        return False
    return sum(value == 1 for value in (report.success, report.failed, report.skipped)) == 1


def _copy_to_project_media(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
