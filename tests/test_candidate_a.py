from dataclasses import asdict
from pathlib import Path

import pytest

from knowledge_distiller.legacy.candidate_a import (
    CandidateAIdentityAdapter,
    CandidateAMediaAdapter,
    EmptyLoginContext,
    InstalledCandidateABinding,
    _CandidateAIdentity,
    _CandidateADownloadReport,
    _CandidateAIdentityUnconfirmed,
    _CandidateAInputUnsupported,
    _CandidateALoginRequired,
    _CandidateAUpstreamFailure,
    _source_metadata_from_detail,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity, IdentityFailure
from knowledge_distiller.media import (
    MediaAcquisitionKind,
    MediaFailure,
    MediaVerificationError,
)


class SuccessfulBinding:
    def confirm_identity(self, target_url):
        return _CandidateAIdentity(
            platform_item_id="1234567890123456789",
            canonical_url="https://www.douyin.com/video/1234567890123456789",
        )


class FailingBinding:
    def __init__(self, error):
        self.error = error

    def confirm_identity(self, target_url):
        raise self.error


class DownloadBinding:
    def __init__(self, report=None, error=None):
        self.report = report
        self.error = error

    def download_media(self, target_url, candidate_work_dir):
        if self.error is not None:
            raise self.error
        return self.report


class RecordingVerifier:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def verify(self, path, *, expected_duration_seconds, complete_decode):
        self.calls.append((path, expected_duration_seconds, complete_decode))
        if self.error is not None:
            raise self.error
        return 62.02


def identity():
    return ConfirmedMaterialIdentity(
        platform="douyin",
        platform_item_id="1234567890123456789",
        original_url="https://v.douyin.com/example/",
        canonical_url="https://www.douyin.com/video/1234567890123456789",
    )


def report(
    media_path,
    *,
    total=1,
    success=1,
    failed=0,
    skipped=0,
    author_name=None,
    author_platform_id=None,
    original_description=None,
    published_at=None,
):
    return _CandidateADownloadReport(
        platform_item_id="1234567890123456789",
        platform_duration_seconds=62.021,
        total=total,
        success=success,
        failed=failed,
        skipped=skipped,
        media_path=media_path,
        author_name=author_name,
        author_platform_id=author_platform_id,
        original_description=original_description,
        published_at=published_at,
    )


def test_candidate_a_success_is_translated_to_project_identity_only():
    adapter = CandidateAIdentityAdapter(SuccessfulBinding())

    result = adapter.identify(
        "https://v.douyin.com/short-form/",
        "https://v.douyin.com/short-form/",
    )

    assert result.failure is None
    assert asdict(result.identity) == {
        "platform": "douyin",
        "platform_item_id": "1234567890123456789",
        "original_url": "https://v.douyin.com/short-form/",
        "canonical_url": "https://www.douyin.com/video/1234567890123456789",
        "identity_confirmed": True,
    }
    assert "aweme_detail" not in asdict(result.identity)
    assert "DownloadResult" not in repr(result.identity)


def test_installed_binding_without_login_stops_before_upstream_access():
    adapter = CandidateAIdentityAdapter(
        InstalledCandidateABinding(EmptyLoginContext())
    )

    result = adapter.identify(
        "https://v.douyin.com/example/",
        "https://v.douyin.com/example/",
    )

    assert result.failure is IdentityFailure.LOGIN_REQUIRED
    assert result.identity is None


@pytest.mark.parametrize(
    "error,expected",
    [
        (_CandidateALoginRequired(), IdentityFailure.LOGIN_REQUIRED),
        (_CandidateAInputUnsupported(), IdentityFailure.INPUT_UNSUPPORTED),
        (_CandidateAIdentityUnconfirmed(), IdentityFailure.IDENTITY_UNCONFIRMED),
        (_CandidateAUpstreamFailure(), IdentityFailure.UPSTREAM_FAILURE),
    ],
)
def test_candidate_a_failures_are_translated(error, expected):
    result = CandidateAIdentityAdapter(FailingBinding(error)).identify(
        "https://v.douyin.com/example/",
        "https://v.douyin.com/example/",
    )

    assert result.failure is expected
    assert result.identity is None


def test_candidate_a_new_download_is_verified_and_translated(tmp_path):
    candidate_path = tmp_path / "candidate" / "1234567890123456789.mp4"
    candidate_path.parent.mkdir()
    candidate_path.write_bytes(b"media")
    verifier = RecordingVerifier()

    result = CandidateAMediaAdapter(
        DownloadBinding(report(candidate_path)), verifier
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.failure is None
    assert result.kind is MediaAcquisitionKind.DOWNLOADED
    assert result.media.platform_item_id == identity().platform_item_id
    assert result.media.path == tmp_path / "work" / "media" / "source.mp4"
    assert result.media.path.read_bytes() == b"media"
    assert verifier.calls == [(result.media.path, 62.021, True)]
    assert "_CandidateA" not in repr(result)
    assert result.media.path.relative_to(tmp_path / "work") == Path("media/source.mp4")


def test_candidate_a_source_metadata_is_translated_without_private_fields(tmp_path):
    candidate_path = tmp_path / "1234567890123456789.mp4"
    candidate_path.write_bytes(b"media")
    result = CandidateAMediaAdapter(
        DownloadBinding(
            report(
                candidate_path,
                author_name="原作者",
                author_platform_id="MS4wLjABAAAA-stable-account",
                original_description="原平台描述 #药品集采",
                published_at="2026-01-02T03:04:05+00:00",
            )
        ),
        RecordingVerifier(),
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.media.author_name == "原作者"
    assert result.media.author_platform_id == "MS4wLjABAAAA-stable-account"
    assert result.media.original_description == "原平台描述 #药品集采"
    assert result.media.published_at == "2026-01-02T03:04:05+00:00"
    assert "aweme_detail" not in repr(result.media)


def test_candidate_a_detail_metadata_mapping_preserves_values_and_missing_fields():
    provided = _source_metadata_from_detail(
        {
            "author": {
                "nickname": "原作者",
                "sec_uid": "MS4wLjABAAAA-stable-account",
            },
            "desc": "原平台描述",
            "create_time": 1_767_326_645,
        }
    )
    missing = _source_metadata_from_detail({"author": {}, "desc": ""})

    assert provided == (
        "原作者",
        "MS4wLjABAAAA-stable-account",
        "原平台描述",
        "2026-01-02T04:04:05+00:00",
    )
    assert missing == (None, None, None, None)


def test_candidate_a_success_cannot_bypass_media_verification(tmp_path):
    candidate_path = tmp_path / "1234567890123456789.mp4"
    candidate_path.write_bytes(b"media")

    result = CandidateAMediaAdapter(
        DownloadBinding(report(candidate_path)),
        RecordingVerifier(MediaVerificationError()),
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.failure is MediaFailure.INTEGRITY_FAILED
    assert result.media is None


def test_candidate_a_skip_requires_existing_verified_target_media(tmp_path):
    missing = CandidateAMediaAdapter(
        DownloadBinding(report(None, success=0, skipped=1)),
        RecordingVerifier(),
    ).acquire(identity(), identity().canonical_url, tmp_path / "missing")

    candidate_path = tmp_path / "1234567890123456789.mp4"
    candidate_path.write_bytes(b"media")
    verifier = RecordingVerifier()
    reused = CandidateAMediaAdapter(
        DownloadBinding(report(candidate_path, success=0, skipped=1)), verifier
    ).acquire(identity(), identity().canonical_url, tmp_path / "reused")

    assert missing.failure is MediaFailure.SOURCE_UNAVAILABLE
    assert reused.kind is MediaAcquisitionKind.REUSED
    assert verifier.calls == [(reused.media.path, 62.021, False)]


def test_candidate_a_failed_result_never_becomes_success(tmp_path):
    candidate_path = tmp_path / "1234567890123456789.mp4"
    candidate_path.write_bytes(b"media")

    result = CandidateAMediaAdapter(
        DownloadBinding(report(candidate_path, success=0, failed=1)),
        RecordingVerifier(),
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.failure is MediaFailure.SOURCE_UNAVAILABLE
    assert result.media is None


@pytest.mark.parametrize(
    "counts",
    [
        {"total": 0, "success": 0, "failed": 0, "skipped": 0},
        {"total": 2, "success": 1, "failed": 0, "skipped": 0},
        {"total": 1, "success": 1, "failed": 1, "skipped": 0},
    ],
)
def test_candidate_a_invalid_single_item_counts_never_succeed(
    tmp_path,
    counts,
):
    candidate_path = tmp_path / "1234567890123456789.mp4"
    candidate_path.write_bytes(b"media")

    result = CandidateAMediaAdapter(
        DownloadBinding(report(candidate_path, **counts)), RecordingVerifier()
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.failure is MediaFailure.UPSTREAM_RESULT_INVALID
    assert result.media is None


def test_candidate_a_media_login_failure_is_stable_and_recoverable(tmp_path):
    result = CandidateAMediaAdapter(
        DownloadBinding(error=_CandidateALoginRequired()), RecordingVerifier()
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.failure is MediaFailure.LOGIN_REQUIRED


def test_installed_media_binding_without_login_stops_before_download(tmp_path):
    result = CandidateAMediaAdapter(
        InstalledCandidateABinding(EmptyLoginContext()), RecordingVerifier()
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.failure is MediaFailure.LOGIN_REQUIRED
    assert not (tmp_path / "work").exists()


def test_candidate_a_media_identity_must_match_target_material(tmp_path):
    candidate_path = tmp_path / "1234567890123456789.mp4"
    candidate_path.write_bytes(b"media")
    mismatched = _CandidateADownloadReport(
        platform_item_id="different-work",
        platform_duration_seconds=62.021,
        total=1,
        success=1,
        failed=0,
        skipped=0,
        media_path=candidate_path,
    )

    result = CandidateAMediaAdapter(
        DownloadBinding(mismatched), RecordingVerifier()
    ).acquire(identity(), identity().canonical_url, tmp_path / "work")

    assert result.failure is MediaFailure.UPSTREAM_RESULT_INVALID
