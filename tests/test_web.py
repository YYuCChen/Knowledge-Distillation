import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import knowledge_distiller.legacy.web as web_module
import pytest
from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_knowledge_result,
    establish_source_fact,
    initialize_database,
    record_knowledge_result_published,
)
from knowledge_distiller.faithful_review import (
    FaithfulReview,
    FaithfulReviewCandidate,
    ReviewConcern,
    ReviewFailure,
)
from knowledge_distiller.identity import (
    ConfirmedMaterialIdentity,
    IdentityFailure,
    IdentityResolution,
)
from knowledge_distiller.knowledge_derivation import (
    DerivationFailure,
    KnowledgeCandidate,
    KnowledgeDerivation,
    KnowledgeEvidence,
    KnowledgePoint,
)
from knowledge_distiller.legacy.knowledge_qualification import (
    KnowledgeQualification,
    QualificationFailure,
    QualificationIssue,
)
from knowledge_distiller.knowledge_library import RecentKnowledgeRecord
from knowledge_distiller.media import (
    MediaAcquisition,
    MediaAcquisitionKind,
    MediaFailure,
    VerifiedTemporaryMedia,
)
from knowledge_distiller.legacy.obsidian_publisher import publication_relative_path
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary
from knowledge_distiller.primary import (
    AudioNormalization,
    PrimaryChunk,
    PrimaryFailure,
    PrimaryRecognition,
    PrimaryRecovery,
    StandardAudio,
)
from knowledge_distiller.secondary import SecondaryAudio, SecondaryResolution
from knowledge_distiller.legacy.web import (
    create_app,
    format_recent_time,
    recent_knowledge_item,
    validate_douyin_url,
)


class StaticResolver:
    def __init__(self, resolution):
        self.resolution = resolution

    def identify(self, original_url, target_url):
        if self.resolution.identity is None:
            return self.resolution
        identity = self.resolution.identity
        return IdentityResolution.confirmed(
            ConfirmedMaterialIdentity(
                platform=identity.platform,
                platform_item_id=identity.platform_item_id,
                original_url=original_url,
                canonical_url=identity.canonical_url,
            )
        )


class SequenceResolver:
    def __init__(self, resolutions):
        self.resolutions = iter(resolutions)

    def identify(self, original_url, target_url):
        resolution = next(self.resolutions)
        if resolution.identity is None:
            return resolution
        identity = resolution.identity
        return IdentityResolution.confirmed(
            ConfirmedMaterialIdentity(
                platform=identity.platform,
                platform_item_id=identity.platform_item_id,
                original_url=original_url,
                canonical_url=identity.canonical_url,
            )
        )


class StaticMediaAcquirer:
    def __init__(self, acquisition=None):
        self.acquisition = acquisition

    def acquire(self, identity, target_url, work_dir):
        if self.acquisition is not None:
            return self.acquisition
        return MediaAcquisition.succeeded(
            MediaAcquisitionKind.DOWNLOADED,
            VerifiedTemporaryMedia(
                platform=identity.platform,
                platform_item_id=identity.platform_item_id,
                path=work_dir / "media" / "source.mp4",
                duration_seconds=62.02,
            ),
        )


class StaticAudioNormalizer:
    def normalize(self, media, work_dir):
        return AudioNormalization.succeeded(
            StandardAudio(work_dir / "standard.wav", media.duration_seconds)
        )


class StaticPrimaryRecognizer:
    def __init__(self, recognition=None):
        self.recognition = recognition or PrimaryRecognition.succeeded(
            PrimaryRecovery(
                text="完整 Primary 文本",
                language="Chinese",
                chunks=(
                    PrimaryChunk("完整 Primary 文本", 0.0, 62.02, "Chinese"),
                ),
            )
        )

    def recognize(self, audio):
        return self.recognition


class StaticFaithfulReviewer:
    def __init__(self, review=None):
        self.review_result = review or FaithfulReview.succeeded(
            FaithfulReviewCandidate("完整 Primary 文本", ())
        )

    def review(self, recovery):
        return self.review_result


class StaticSecondaryResolver:
    def __init__(self, transcript):
        self.transcript = transcript
        self.calls = []

    @property
    def available(self):
        return True

    def resolve(self, audio):
        self.calls.append(audio)
        return SecondaryResolution.succeeded(self.transcript)


class StaticSecondaryClipper:
    def __init__(self):
        self.calls = []

    def clip(self, audio, start_seconds, end_seconds, output_path):
        self.calls.append((audio, start_seconds, end_seconds, output_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFF-local-audio")
        return SecondaryAudio(
            output_path,
            start_seconds,
            end_seconds,
            end_seconds - start_seconds,
        )


class StaticKnowledgeDeriver:
    def __init__(self, derivation=None):
        self.derivation = derivation

    def derive(self, source_fact_id, snapshot, uncertainties):
        if self.derivation is not None:
            return self.derivation
        evidence_text = snapshot
        return KnowledgeDerivation.succeeded(
            KnowledgeCandidate(
                "结构化知识标题",
                "一句话知识总括。",
                (
                    KnowledgePoint(
                        "p1",
                        "完整知识观点。",
                        "围绕该观点形成的完整论证。",
                        ("e1",),
                    ),
                ),
                (),
                (
                    KnowledgeEvidence(
                        "e1",
                        source_fact_id,
                        0,
                        len(evidence_text),
                        evidence_text,
                    ),
                ),
            )
        )


class StaticKnowledgeQualifier:
    def __init__(self, qualification=None):
        self.qualification = qualification or KnowledgeQualification.passed()

    def qualify(self, source_fact_id, snapshot, uncertainties, candidate):
        return self.qualification


class UnexpectedPipelineCall:
    def _fail(self, operation):
        raise AssertionError(f"Stable result should have skipped {operation}")

    def identify(self, *args):
        self._fail("identity")

    def acquire(self, *args):
        self._fail("media acquisition")

    def normalize(self, *args):
        self._fail("audio normalization")

    def recognize(self, *args):
        self._fail("Primary recognition")

    def review(self, *args):
        self._fail("faithful review")

    def derive(self, *args):
        self._fail("knowledge derivation")

    def qualify(self, *args):
        self._fail("knowledge qualification")


def confirmed_resolution():
    return IdentityResolution.confirmed(
        ConfirmedMaterialIdentity(
            platform="douyin",
            platform_item_id="stable-work-1",
            original_url="placeholder",
            canonical_url="https://www.douyin.com/video/stable-work-1",
        )
    )


def test_home_page_is_available(tmp_path):
    app = create_app(tmp_path / "knowledge.sqlite3")

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert "Local Web" in response.text
    assert "添加内容" in response.text
    assert "粘贴一条抖音口播链接或分享文案" in response.text
    assert "开始蒸馏" in response.text
    assert "正在蒸馏，请稍候" in response.text
    assert "data-processing-feedback hidden" in response.text
    assert '<textarea\n              id="url"' in response.text
    assert 'action="/submissions" method="post"' in response.text
    assert 'href="/knowledge"' in response.text
    assert "知识库" in response.text
    assert 'action="/knowledge/organization-events" method="post"' in response.text
    assert "开始整理" in response.text
    assert 'href="/knowledge/insight-candidates"' in response.text
    assert "还没有正式沉淀的知识" in response.text
    for unsupported in ("运行正常", "处理中数量", "1/3", "预计剩余"):
        assert unsupported not in response.text


def test_bare_douyin_url_is_accepted():
    assert validate_douyin_url("https://v.douyin.com/example/") == (
        "https://v.douyin.com/example/"
    )


def test_douyin_url_is_extracted_from_share_text_and_surrounding_whitespace():
    shared = (
        "\n  【测试作品】市场的KPI是转化和数据分析 "
        "https://www.douyin.com/video/1234567890123456789 复制此链接  \n"
    )

    assert validate_douyin_url(shared) == (
        "https://www.douyin.com/video/1234567890123456789"
    )


def test_input_without_a_url_is_rejected_without_creating_a_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(database_path)

    response = app.test_client().post(
        "/submissions", data={"url": "【作品描述】这里没有链接"}
    )

    assert response.status_code == 400
    assert "请输入有效的抖音作品链接" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_multiple_douyin_urls_are_rejected_without_creating_a_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(database_path)
    shared = (
        "https://v.douyin.com/first/ 和 "
        "https://www.douyin.com/video/1234567890123456789"
    )

    response = app.test_client().post("/submissions", data={"url": shared})

    assert response.status_code == 400
    assert "当前一次只能提交一条抖音作品链接" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_invalid_input_is_rejected_without_creating_a_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(database_path)

    response = app.test_client().post(
        "/submissions", data={"url": "https://example.com/not-douyin"}
    )

    assert response.status_code == 400
    assert "请输入有效的抖音作品链接" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_douyin_home_page_is_not_accepted_as_a_material_submission(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(database_path)

    response = app.test_client().post(
        "/submissions", data={"url": "https://www.douyin.com/"}
    )

    assert response.status_code == 400


def test_valid_submission_establishes_source_fact_without_exposing_internals(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
    )
    client = app.test_client()

    response = client.post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "知识整理完成" in response.text
    assert "准备保存到你的知识库" in response.text
    assert "有一处关键内容需要你确认" not in response.text
    with connect(database_path) as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
        material_count = connection.execute("SELECT count(*) FROM materials").fetchone()[0]
        source_fact_count = connection.execute(
            "SELECT count(*) FROM source_facts"
        ).fetchone()[0]
        knowledge_result_count = connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0]
    assert task["submitted_url"] == "https://v.douyin.com/example/"
    assert task["material_id"] is not None
    assert material_count == 1
    assert source_fact_count == 1
    assert knowledge_result_count == 1
    for internal_term in (
        "Candidate A",
        "SourceFact",
        "KnowledgeResult",
        "evidence locator",
        "platform_item_id",
        "Material ID",
        "Task ID",
    ):
        assert internal_term not in response.text


def test_share_text_enters_existing_orchestration_with_only_extracted_url(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
    )
    shared = (
        "【测试作品】市场的KPI是转化和数据分析 #商业 "
        "https://v.douyin.com/example/ 复制此链接"
    )

    response = app.test_client().post(
        "/submissions", data={"url": shared}, follow_redirects=True
    )

    assert response.status_code == 200
    assert "知识整理完成" in response.text
    with connect(database_path) as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 1
    assert task["submitted_url"] == "https://v.douyin.com/example/"


def test_valid_submission_can_finish_with_human_readable_obsidian_feedback(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
        obsidian_vault_root=vault_root,
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已保存到 Obsidian" in response.text
    assert "安全保存" in response.text
    with connect(database_path) as connection:
        knowledge = connection.execute("SELECT * FROM knowledge_results").fetchone()
    assert knowledge["published_at"] is not None
    assert knowledge["published_path"] is not None
    assert (vault_root / knowledge["published_path"]).is_file()
    assert decide_next_boundary(database_path, 1) is NextBoundary.COMPLETE
    with connect(database_path) as connection:
        task_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
        }
    assert {"status", "stage", "progress"}.isdisjoint(task_columns)
    for internal_term in ("KnowledgeResult", "no-clobber", "transaction"):
        assert internal_term not in response.text


def test_existing_source_fact_resumes_at_knowledge_derivation(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    first_app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(
            KnowledgeDerivation.failed(DerivationFailure.RUNTIME_UNAVAILABLE)
        ),
    )
    first_app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    assert decide_next_boundary(database_path, 1) is NextBoundary.KNOWLEDGE_DERIVATION

    forbidden = UnexpectedPipelineCall()
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    resumed_app = create_app(
        database_path,
        forbidden,
        forbidden,
        tmp_path / "runtime",
        forbidden,
        forbidden,
        forbidden,
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
        obsidian_vault_root=vault_root,
    )

    response = resumed_app.test_client().post(
        "/tasks/1/continue",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已保存到 Obsidian" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 1
    assert decide_next_boundary(database_path, 1) is NextBoundary.COMPLETE


def test_existing_knowledge_result_resumes_only_at_publication(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    first_app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
    )
    first_app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    assert decide_next_boundary(database_path, 1) is NextBoundary.OBSIDIAN_PUBLISHING

    forbidden = UnexpectedPipelineCall()
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    resumed_app = create_app(
        database_path,
        forbidden,
        forbidden,
        tmp_path / "runtime",
        forbidden,
        forbidden,
        forbidden,
        forbidden,
        forbidden,
        obsidian_vault_root=vault_root,
    )

    response = resumed_app.test_client().post(
        "/tasks/1/continue",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已保存到 Obsidian" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 1
    assert decide_next_boundary(database_path, 1) is NextBoundary.COMPLETE


def test_duplicate_submission_of_completed_material_skips_completed_pipeline(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    first_app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
        obsidian_vault_root=vault_root,
    )
    first_app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    with connect(database_path) as connection:
        before = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "materials", "source_facts", "knowledge_results")
        }
        published_path = connection.execute(
            "SELECT published_path FROM knowledge_results"
        ).fetchone()["published_path"]

    def unexpected_publication(*args):
        raise AssertionError("Completed task should not publish again")

    monkeypatch.setattr(web_module, "publish_task_knowledge", unexpected_publication)
    forbidden = UnexpectedPipelineCall()
    repeated_app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        forbidden,
        tmp_path / "runtime",
        forbidden,
        forbidden,
        forbidden,
        forbidden,
        forbidden,
        obsidian_vault_root=vault_root,
    )

    response = repeated_app.test_client().post(
        "/submissions",
        data={"url": "https://www.douyin.com/video/stable-work-1"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "已保存到 Obsidian" in response.text
    with connect(database_path) as connection:
        after = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "materials", "source_facts", "knowledge_results")
        }
        assert connection.execute(
            "SELECT published_path FROM knowledge_results"
        ).fetchone()["published_path"] == published_path
    assert after == before


def test_obsidian_conflict_is_human_readable_and_does_not_overwrite(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    target = vault_root / publication_relative_path(1, "结构化知识标题")
    target.parent.mkdir(parents=True)
    existing = "# 用户已有的不同内容\n"
    target.write_text(existing, encoding="utf-8")
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
        obsidian_vault_root=vault_root,
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "目标位置已有不同内容" in response.text
    assert "没有写入" in response.text
    assert target.read_text(encoding="utf-8") == existing
    with connect(database_path) as connection:
        knowledge = connection.execute("SELECT * FROM knowledge_results").fetchone()
    assert knowledge["published_at"] is None
    assert knowledge["published_path"] is None


def test_login_required_is_recoverable_without_creating_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(IdentityResolution.failed(IdentityFailure.LOGIN_REQUIRED)),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "需要重新登录抖音" in response.text
    assert "不需要再次提交" in response.text
    assert "我已重新登录，继续" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 0


def test_continue_after_login_reuses_the_same_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    resolver = SequenceResolver(
        [
            IdentityResolution.failed(IdentityFailure.LOGIN_REQUIRED),
            confirmed_resolution(),
        ]
    )
    app = create_app(
        database_path,
        resolver,
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(),
    )
    client = app.test_client()
    first_response = client.post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=False,
    )
    task_location = first_response.headers["Location"]

    resumed_response = client.post(
        f"{task_location}/continue", follow_redirects=True
    )

    assert resumed_response.status_code == 200
    assert "知识整理完成" in resumed_response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        task = connection.execute("SELECT * FROM tasks").fetchone()
    assert task["material_id"] is not None
    assert task["waiting_reason"] is None


def test_media_login_required_keeps_identified_task_recoverable(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(MediaAcquisition.failed(MediaFailure.LOGIN_REQUIRED)),
        tmp_path / "runtime",
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "需要重新登录抖音" in response.text
    with connect(database_path) as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
    assert task["material_id"] is not None


def test_media_failure_is_human_readable_without_internal_details(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(MediaAcquisition.failed(MediaFailure.INTEGRITY_FAILED)),
        tmp_path / "runtime",
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "暂时无法取得完整来源内容" in response.text
    for internal_term in (
        "Candidate A",
        "VideoDownloader",
        "ffprobe",
        "platform_item_id",
        "Material ID",
        "Task ID",
        "media_integrity_failed",
    ):
        assert internal_term not in response.text


def test_primary_failure_is_human_readable_without_internal_details(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(
            PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE)
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "暂时无法完整恢复口播" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 0
    assert (
        decide_next_boundary(database_path, 1)
        is NextBoundary.SOURCE_FACT_PRODUCTION
    )
    for internal_term in (
        "Qwen",
        "MLX",
        "WAV",
        "PrimaryFailure",
        "primary_incomplete",
        "SourceFact",
    ):
        assert internal_term not in response.text


def test_review_failure_is_human_readable_without_internal_details(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(
            FaithfulReview.failed(ReviewFailure.INVALID_OUTPUT)
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "暂时无法忠实整理口播" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
    for internal_term in (
        "provider",
        "LLM",
        "FaithfulReview",
        "review_invalid_output",
        "SourceFact",
    ):
        assert internal_term not in response.text


def test_review_runtime_unavailable_is_recoverable_without_new_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(
            FaithfulReview.failed(ReviewFailure.RUNTIME_UNAVAILABLE)
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "当前缺少整理口播所需的运行条件" in response.text
    assert "重新尝试" in response.text
    with connect(database_path) as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
    assert task["waiting_boundary"] == "source_fact_production"
    assert task["waiting_reason"] == "faithful_review_unavailable"


def test_meaning_changing_concern_stays_at_source_boundary(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    text = "完整 Primary 文本"
    start = text.index("P")
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(
            FaithfulReview.succeeded(
                FaithfulReviewCandidate(
                    text,
                    (
                        ReviewConcern(
                            start,
                            start + 1,
                            "P",
                            "这个局部可能改变原意。",
                            True,
                            ("初",),
                        ),
                    ),
                )
            )
        ),
        secondary_audio_clipper=StaticSecondaryClipper(),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "有一处关键内容需要你确认" in response.text
    assert "请只确认这一处" in response.text
    assert 'value="P"' in response.text
    assert 'value="初"' in response.text
    assert "无法确认" in response.text
    assert response.text.count("<textarea") == 1
    assert '<form action="/tasks/1/source-confirmation"' in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    for internal_term in ("SourceFact", "Secondary", "checkpoint"):
        assert internal_term not in response.text


def _human_confirmation_app(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    text = (
        "作者先介绍药品集中采购的基本流程，然后解释医疗机构如何上报采购量。"
        "随后作者说明中选企业的供应责任、医院的采购要求，以及奖励资金的使用范围。"
    )
    start = text.index("采购量")
    recovery = PrimaryRecovery(
        text,
        "Chinese",
        (PrimaryChunk(text, 0.0, 62.02, "Chinese"),),
    )
    clipper = StaticSecondaryClipper()
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(PrimaryRecognition.succeeded(recovery)),
        StaticFaithfulReviewer(
            FaithfulReview.succeeded(
                FaithfulReviewCandidate(
                    text,
                    (
                        ReviewConcern(
                            start,
                            start + len("采购量"),
                            "采购量",
                            "数量对象可能改变原意。",
                            True,
                            ("上报量",),
                        ),
                    ),
                )
            )
        ),
        secondary_resolver=StaticSecondaryResolver("采购量或上报量都有可能"),
        secondary_audio_clipper=clipper,
    )
    return app, database_path, text, start, clipper


def _ticket_from(response):
    match = re.search(r'name="ticket" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def test_allowed_web_confirmation_changes_only_target_and_continues(tmp_path):
    app, database_path, text, start, clipper = _human_confirmation_app(tmp_path)
    client = app.test_client()
    initial = client.post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    ticket = _ticket_from(initial)
    audio_path = clipper.calls[0][3]
    assert audio_path.is_file()

    response = client.post(
        "/tasks/1/source-confirmation",
        data={"ticket": ticket, "choice": "上报量"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "知识整理完成" in response.text
    assert "有一处关键内容需要你确认" not in response.text
    with connect(database_path) as connection:
        source = connection.execute("SELECT content_snapshot FROM source_facts").fetchone()
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 1
    assert source["content_snapshot"] == text[:start] + "上报量" + text[start + 3 :]
    assert not audio_path.exists()


def test_unable_to_confirm_stops_before_source_fact_and_knowledge(tmp_path):
    app, database_path, _, _, clipper = _human_confirmation_app(tmp_path)
    client = app.test_client()
    initial = client.post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    ticket = _ticket_from(initial)
    audio_path = clipper.calls[0][3]

    response = client.post(
        "/tasks/1/source-confirmation",
        data={"ticket": ticket, "choice": "__unable_to_confirm__"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "这条内容目前无法可靠处理" in response.text
    assert "不会继续整理知识或保存到 Obsidian" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    assert decide_next_boundary(database_path, 1) is NextBoundary.SOURCE_FACT_PRODUCTION
    assert not audio_path.exists()


def test_tampered_confirmation_is_rejected_without_changing_source(tmp_path):
    app, database_path, _, _, _ = _human_confirmation_app(tmp_path)
    client = app.test_client()
    initial = client.post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    ticket = _ticket_from(initial)

    response = client.post(
        "/tasks/1/source-confirmation",
        data={"ticket": ticket, "choice": "任意改写整份来源"},
    )

    assert response.status_code == 400
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    retry_page = client.get("/tasks/1")
    assert "有一处关键内容需要你确认" in retry_page.text


def test_local_confirmation_audio_is_ticket_scoped_and_not_cached(tmp_path):
    app, _, _, _, _ = _human_confirmation_app(tmp_path)
    client = app.test_client()
    initial = client.post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    ticket = _ticket_from(initial)

    response = client.get(f"/tasks/1/source-confirmation/{ticket}/audio")

    assert response.status_code == 200
    assert response.mimetype == "audio/wav"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/tasks/1/source-confirmation/wrong-ticket/audio").status_code == 404


def test_pending_question_is_runtime_only_and_restart_uses_stable_boundary(tmp_path):
    first_app, database_path, _, _, _ = _human_confirmation_app(tmp_path)
    first_response = first_app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )
    assert "有一处关键内容需要你确认" in first_response.text

    restarted_app = create_app(database_path)
    response = restarted_app.test_client().get("/tasks/1")

    assert response.status_code == 200
    assert "需要重新准备这处局部内容" in response.text
    assert "重新尝试" in response.text
    assert 'name="choice"' not in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0


def test_web_pipeline_uses_secondary_only_for_target_and_then_continues(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    text = (
        "作者先介绍药品集中采购的基本流程，然后解释医疗机构如何上报采购量。"
        "随后作者说明中选企业的供应责任、医院的采购要求，以及奖励资金的使用范围。"
    )
    start = text.index("采购量")
    recovery = PrimaryRecovery(
        text,
        "Chinese",
        (PrimaryChunk(text, 0.0, 62.02, "Chinese"),),
    )
    resolver = StaticSecondaryResolver("局部声学结果明确是上报量。")
    clipper = StaticSecondaryClipper()
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(PrimaryRecognition.succeeded(recovery)),
        StaticFaithfulReviewer(
            FaithfulReview.succeeded(
                FaithfulReviewCandidate(
                    text,
                    (
                        ReviewConcern(
                            start,
                            start + len("采购量"),
                            "采购量",
                            "数量对象可能改变原意。",
                            True,
                            ("上报量",),
                        ),
                    ),
                )
            )
        ),
        secondary_resolver=resolver,
        secondary_audio_clipper=clipper,
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "知识整理完成" in response.text
    assert len(resolver.calls) == 1
    assert len(clipper.calls) == 1
    with connect(database_path) as connection:
        source_fact = connection.execute("SELECT * FROM source_facts").fetchone()
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 1
    assert source_fact["content_snapshot"] == text[:start] + "上报量" + text[start + 3 :]


def test_derivation_failure_is_human_readable_without_internal_details(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(
            KnowledgeDerivation.failed(DerivationFailure.INVALID_OUTPUT)
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "暂时无法忠实整理知识" in response.text
    assert "来源内容已经可靠保存" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    for internal_term in ("KnowledgeResult", "LLM", "evidence locator"):
        assert internal_term not in response.text


def test_derivation_runtime_unavailable_preserves_source_fact_for_retry(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(
            KnowledgeDerivation.failed(DerivationFailure.RUNTIME_UNAVAILABLE)
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "暂时无法整理知识" in response.text
    assert "来源内容已经可靠保存" in response.text
    assert "重新尝试" in response.text
    with connect(database_path) as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    assert task["waiting_boundary"] == "knowledge_derivation"
    assert task["waiting_reason"] == "knowledge_derivation_unavailable"


def test_knowledge_qualification_rejection_is_human_readable(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(
            KnowledgeQualification.rejected(
                (
                    QualificationIssue(
                        "p1",
                        "证据不能支持观点中的完整条件。",
                    ),
                )
            )
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "这次知识整理未通过检查" in response.text
    assert "不会保存为正式知识" in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 0
    for internal_term in ("KnowledgeResult", "qualification", "evidence locator"):
        assert internal_term not in response.text


def test_knowledge_qualification_runtime_unavailable_is_recoverable(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(confirmed_resolution()),
        StaticMediaAcquirer(),
        tmp_path / "runtime",
        StaticAudioNormalizer(),
        StaticPrimaryRecognizer(),
        StaticFaithfulReviewer(),
        StaticKnowledgeDeriver(),
        StaticKnowledgeQualifier(
            KnowledgeQualification.failed(QualificationFailure.RUNTIME_UNAVAILABLE)
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "暂时无法整理知识" in response.text
    assert "重新尝试" in response.text
    with connect(database_path) as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
        assert connection.execute(
            "SELECT count(*) FROM knowledge_results"
        ).fetchone()[0] == 0
    assert task["waiting_boundary"] == "knowledge_derivation"
    assert task["waiting_reason"] == "knowledge_qualification_unavailable"


def test_identity_failure_uses_human_feedback_without_internal_details(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    app = create_app(
        database_path,
        StaticResolver(
            IdentityResolution.failed(IdentityFailure.IDENTITY_UNCONFIRMED)
        ),
    )

    response = app.test_client().post(
        "/submissions",
        data={"url": "https://v.douyin.com/example/"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "暂时无法确认该作品" in response.text
    assert "你的投递已保留，但当前不能继续处理" in response.text
    assert "Candidate A" not in response.text
    assert "IdentityFailure" not in response.text
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 0


LIBRARY_SNAPSHOT = "本地搜索只读取已经成立并发布的正式知识。"


def add_published_library_result(
    database_path,
    item_id,
    *,
    title="本地历史知识检索",
    summary="通过关键词重新找到已经发布的正式观点。",
    statement="本地搜索可以找回正式观点。",
    role="core",
    published_path=None,
):
    initialize_database(database_path)
    task_id = create_task(database_path, f"https://example.test/{item_id}")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            "douyin",
            item_id,
            f"https://example.test/{item_id}",
            f"https://example.test/{item_id}",
        ),
    )
    source = establish_source_fact(
        database_path,
        task_id,
        {
            "author": {
                "display_name": "知识作者",
                "platform_account_id": "author-account",
            },
            "original_description": "本地知识原描述",
        },
        LIBRARY_SNAPSHOT,
        [],
    )
    point = {
        "id": f"{role}-point",
        "statement": statement,
        "argument": "完整论证保留在正式知识文档中。",
        "evidence_ids": ["e1"],
    }
    knowledge = establish_knowledge_result(
        database_path,
        task_id,
        source.source_fact_id,
        {
            "title": title,
            "summary": summary,
            "core_points": [point] if role == "core" else [
                {
                    "id": "required-core",
                    "statement": "这条核心观点不包含目标词。",
                    "argument": "用于维持正式 payload 的核心观点要求。",
                    "evidence_ids": ["e1"],
                }
            ],
            "other_points": [point] if role == "other" else [],
            "evidence_registry": [
                {
                    "id": "e1",
                    "source_fact_id": source.source_fact_id,
                    "start": 0,
                    "end": len(LIBRARY_SNAPSHOT),
                    "evidence_text": LIBRARY_SNAPSHOT,
                }
            ],
        },
    )
    relative_path = published_path or f"知识蒸馏器/{item_id}.md"
    record_knowledge_result_published(
        database_path,
        task_id,
        knowledge.knowledge_result_id,
        relative_path,
    )
    return task_id, source.source_fact_id, knowledge.knowledge_result_id, relative_path


def library_app(database_path, *, vault_root=None):
    unexpected = UnexpectedPipelineCall()
    return create_app(
        database_path,
        unexpected,
        unexpected,
        database_path.parent / "runtime",
        unexpected,
        unexpected,
        unexpected,
        unexpected,
        unexpected,
        obsidian_vault_root=vault_root,
    )


def test_knowledge_page_has_submission_navigation_and_empty_query_help(tmp_path):
    app = library_app(tmp_path / "knowledge.sqlite3")

    response = app.test_client().get("/knowledge")

    assert response.status_code == 200
    assert 'href="/"' in response.text
    assert "投递" in response.text
    assert "关键词、概念或短句" in response.text
    assert response.text.count('class="knowledge-search library-search"') == 1
    assert '<a href="/knowledge" aria-current="page">主题</a>' in response.text
    assert '<a href="/knowledge?view=knowledge">知识</a>' in response.text


def test_knowledge_page_renders_formal_point_and_normal_empty_state(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_published_library_result(database_path, "visible")
    app = library_app(database_path)

    found = app.test_client().get("/knowledge", query_string={"q": "找回正式观点"})
    missing = app.test_client().get("/knowledge", query_string={"q": "不存在"})

    assert found.status_code == 200
    assert "本地搜索可以找回正式观点。" in found.text
    assert "本地历史知识检索" in found.text
    assert "核心观点" in found.text
    assert "知识作者" in found.text
    assert "抖音" in found.text
    assert missing.status_code == 200
    assert "没有找到相关正式观点" in missing.text


def test_source_knowledge_page_lists_every_result_and_keeps_result_level_content(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    for index in range(1, 9):
        add_published_library_result(
            database_path,
            f"browse-{index}",
            title=f"来源型知识 {index}",
            summary=f"第 {index} 份知识的一句话总括。",
            statement=f"第 {index} 条核心观点。",
        )
    app = library_app(database_path)

    response = app.test_client().get("/knowledge", query_string={"view": "knowledge"})

    assert response.status_code == 200
    assert response.text.count('<details class="knowledge-browse-details">') == 8
    assert response.text.index("来源型知识 8") < response.text.index("来源型知识 7")
    assert "来源型知识 1" in response.text
    assert "第 8 份知识的一句话总括。" in response.text
    assert "第 8 条核心观点。" in response.text
    assert '<a href="/knowledge?view=knowledge" aria-current="page">知识</a>' in response.text
    assert '<a href="/knowledge">主题</a>' in response.text


def test_source_knowledge_page_expands_only_core_points(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_published_library_result(
        database_path,
        "roles",
        title="角色边界知识",
        summary="知识页只展开核心观点。",
        statement="不应出现在知识页的其他观点。",
        role="other",
    )
    app = library_app(database_path)

    response = app.test_client().get("/knowledge", query_string={"view": "knowledge"})

    assert "这条核心观点不包含目标词。" in response.text
    assert "不应出现在知识页的其他观点。" not in response.text
    assert "完整论证保留在正式知识文档中。" not in response.text
    assert LIBRARY_SNAPSHOT not in response.text
    assert "evidence_registry" not in response.text


def test_source_knowledge_page_locally_excludes_bad_record_and_only_links_safe_file(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    target = vault_root / "知识蒸馏器" / "safe.md"
    target.parent.mkdir(parents=True)
    target.write_text("safe", encoding="utf-8")
    add_published_library_result(
        database_path, "safe", title="安全知识", published_path="知识蒸馏器/safe.md"
    )
    add_published_library_result(
        database_path, "missing", title="文件缺失知识", published_path="知识蒸馏器/missing.md"
    )
    _, _, bad_id, _ = add_published_library_result(database_path, "bad")
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER knowledge_result_content_cannot_be_updated")
        connection.execute(
            "UPDATE knowledge_results SET payload_json = '{private broken payload' "
            "WHERE knowledge_result_id = ?",
            (bad_id,),
        )
    app = library_app(database_path, vault_root=vault_root)

    response = app.test_client().get("/knowledge", query_string={"view": "knowledge"})

    assert response.status_code == 200
    assert "安全知识" in response.text
    assert "文件缺失知识" in response.text
    assert "有 1 份来源型知识当前无法读取" in response.text
    assert "{private broken payload" not in response.text
    assert response.text.count("obsidian://open?path=") == 1


def test_source_knowledge_and_search_views_do_not_read_topics_or_call_models(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "knowledge.sqlite3"
    add_published_library_result(database_path, "independent")
    app = library_app(database_path)

    class UnexpectedTopicLibrary:
        def __getattr__(self, _name):
            raise AssertionError("knowledge and search views must not touch topics")

    app.config["TOPIC_LIBRARY"] = UnexpectedTopicLibrary()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("read-only knowledge browsing crossed a model boundary")

    monkeypatch.setattr(
        "knowledge_distiller.legacy.orchestration.decide_next_boundary", unexpected
    )
    monkeypatch.setattr(
        "knowledge_distiller.knowledge_derivation.build_knowledge_deriver", unexpected
    )
    monkeypatch.setattr(
        "knowledge_distiller.legacy.candidate_a.build_candidate_a_adapters", unexpected
    )
    client = app.test_client()

    assert client.get("/knowledge", query_string={"view": "knowledge"}).status_code == 200
    assert client.get("/knowledge", query_string={"q": "正式观点"}).status_code == 200


def test_knowledge_query_is_template_escaped(tmp_path):
    app = library_app(tmp_path / "knowledge.sqlite3")
    query = '<script>alert("x")</script>'

    response = app.test_client().get("/knowledge", query_string={"q": query})

    assert response.status_code == 200
    assert query not in response.text
    assert "&lt;script&gt;" in response.text
    assert "&#34;x&#34;" in response.text


def test_one_bad_formal_record_warns_but_healthy_results_remain_available(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_published_library_result(database_path, "healthy-one")
    _, _, bad_result_id, _ = add_published_library_result(database_path, "bad")
    add_published_library_result(
        database_path,
        "healthy-two",
        statement="第二份正式观点同样保持可读。",
    )
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER knowledge_result_content_cannot_be_updated")
        connection.execute(
            "UPDATE knowledge_results SET payload_json = '{bad json' WHERE knowledge_result_id = ?",
            (bad_result_id,),
        )
        before = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
    app = library_app(database_path)

    response = app.test_client().get("/knowledge", query_string={"q": "正式观点"})

    assert response.status_code == 200
    assert "本地搜索可以找回正式观点。" in response.text
    assert "第二份正式观点同样保持可读。" in response.text
    assert "有 1 份正式知识当前无法读取" in response.text
    assert "本次搜索结果可能不完整" in response.text
    assert "{bad json" not in response.text
    with connect(database_path) as connection:
        after = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
        assert connection.execute(
            "SELECT payload_json FROM knowledge_results WHERE knowledge_result_id = ?",
            (bad_result_id,),
        ).fetchone()[0] == "{bad json"
    assert after == before


def test_whole_library_failure_is_not_presented_as_no_results(tmp_path, monkeypatch):
    app = library_app(tmp_path / "knowledge.sqlite3")

    def fail_search(*_args, **_kwargs):
        raise web_module.KnowledgeLibraryError("whole library failed")

    monkeypatch.setattr(web_module, "search_formal_points", fail_search)
    response = app.test_client().get("/knowledge", query_string={"q": "知识"})

    assert response.status_code == 500
    assert "本次搜索没有完成" in response.text
    assert "没有找到相关正式观点" not in response.text
    assert "whole library failed" not in response.text


def test_vault_unconfigured_keeps_result_visible_without_bad_link(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_published_library_result(database_path, "no-vault")
    app = library_app(database_path)

    response = app.test_client().get("/knowledge", query_string={"q": "正式观点"})

    assert "本地搜索可以找回正式观点。" in response.text
    assert "obsidian://open" not in response.text


def test_existing_vault_file_gets_fully_encoded_obsidian_uri(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "我的 Vault"
    vault_root.mkdir()
    relative_path = "知识蒸馏器/中文 空格#问号?.md"
    add_published_library_result(
        database_path,
        "encoded",
        published_path=relative_path,
    )
    target = vault_root / relative_path
    target.parent.mkdir()
    target.write_text("正式 Markdown", encoding="utf-8")
    app = library_app(database_path, vault_root=vault_root)

    response = app.test_client().get("/knowledge", query_string={"q": "正式观点"})
    expected = "obsidian://open?path=" + quote(str(target.resolve()), safe="")

    assert response.status_code == 200
    assert expected in response.text
    assert "%2F" in response.text
    assert "%23" in response.text
    assert "%3F" in response.text
    assert "%20" in response.text


def test_absolute_and_resolved_outside_paths_have_no_obsidian_link(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    add_published_library_result(database_path, "unsafe")
    app = library_app(database_path, vault_root=vault_root)

    for unsafe_path in (str(outside), "../outside.md"):
        with connect(database_path) as connection:
            connection.execute(
                "UPDATE knowledge_results SET published_path = ? WHERE knowledge_result_id = 1",
                (unsafe_path,),
            )
        response = app.test_client().get(
            "/knowledge", query_string={"q": "正式观点"}
        )
        assert "obsidian://open" not in response.text


def test_relative_path_with_parent_component_is_allowed_when_it_resolves_inside(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    target = vault_root / "inside.md"
    target.parent.mkdir()
    (vault_root / "folder").mkdir()
    target.write_text("inside", encoding="utf-8")
    add_published_library_result(database_path, "inside")
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE knowledge_results SET published_path = 'folder/../inside.md' WHERE knowledge_result_id = 1"
        )
    app = library_app(database_path, vault_root=vault_root)

    response = app.test_client().get("/knowledge", query_string={"q": "正式观点"})

    assert "obsidian://open?path=" in response.text
    assert quote(str(target.resolve()), safe="") in response.text


def test_missing_recorded_file_keeps_point_visible_without_link(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    add_published_library_result(database_path, "missing")
    app = library_app(database_path, vault_root=vault_root)

    response = app.test_client().get("/knowledge", query_string={"q": "正式观点"})

    assert "本地搜索可以找回正式观点。" in response.text
    assert "obsidian://open" not in response.text


def test_web_search_does_not_change_sqlite_or_markdown(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _, _, _, relative_path = add_published_library_result(database_path, "read-only")
    target = vault_root / relative_path
    target.parent.mkdir()
    target.write_text("user-owned markdown", encoding="utf-8")
    before_markdown = (target.read_bytes(), target.stat().st_mtime_ns)
    with connect(database_path) as connection:
        before_database = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
    app = library_app(database_path, vault_root=vault_root)

    response = app.test_client().get("/knowledge", query_string={"q": "正式观点"})

    assert response.status_code == 200
    with connect(database_path) as connection:
        after_database = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
    assert after_database == before_database
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before_markdown


@pytest.mark.parametrize(
    ("published_at", "expected"),
    [
        ("2026-08-20T11:59:31+00:00", "刚刚"),
        ("2026-08-20T11:42:00+00:00", "18 分钟前"),
        ("2026-08-20T08:00:00+00:00", "4 小时前"),
        ("2026-08-18T08:00:00+00:00", "8 月 18 日"),
        ("2025-12-31T08:00:00+00:00", "2025 年 12 月 31 日"),
    ],
)
def test_recent_time_labels_are_deterministic(published_at, expected):
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    assert format_recent_time(published_at, now=now) == expected


def test_recent_time_label_respects_timezone_and_rejects_future_invalid_values():
    shanghai = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 20, 10, 0, tzinfo=shanghai)

    assert format_recent_time("2026-08-19T23:00:00+00:00", now=now) == "3 小时前"
    assert format_recent_time(now + timedelta(seconds=1), now=now) == "时间待确认"
    assert format_recent_time("not-a-time", now=now) == "时间待确认"
    assert format_recent_time("2026-08-20T09:00:00", now=now) == "时间待确认"


def test_recent_view_model_adds_only_safe_obsidian_handoff(tmp_path):
    vault_root = tmp_path / "Probe Vault"
    target = vault_root / "知识蒸馏器" / "知识.md"
    target.parent.mkdir(parents=True)
    target.write_text("probe", encoding="utf-8")
    record = RecentKnowledgeRecord(
        7,
        "标题",
        "总括",
        ("观点一", "观点二"),
        "来源作者",
        "douyin",
        "2026-08-20T10:00:00+00:00",
        "知识蒸馏器/知识.md",
    )

    item = recent_knowledge_item(
        record,
        vault_root=vault_root,
        rendered_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )

    assert item.knowledge_result_id == 7
    assert item.title == "标题"
    assert item.summary == "总括"
    assert item.core_point_statements == ("观点一", "观点二")
    assert item.source_label == "来源作者"
    assert item.platform == "douyin"
    assert item.published_at == "2026-08-20T10:00:00+00:00"
    assert item.display_time == "2 小时前"
    assert item.published_path == "知识蒸馏器/知识.md"
    assert item.obsidian_uri == "obsidian://open?path=" + quote(
        str(target.resolve()), safe=""
    )


def test_home_shows_six_newest_readable_results_as_independent_details(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    result_ids = []
    for index in range(1, 8):
        result_ids.append(
            add_published_library_result(
                database_path,
                f"home-{index}",
                title=f"最近知识 {index}",
                summary=f"第 {index} 条一句话总括。",
                statement=f"第 {index} 条核心观点。",
            )[2]
        )
    app = library_app(database_path)

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert response.text.count('<details class="recent-details">') == 6
    assert response.text.count("<summary aria-label=") == 6
    for index in range(2, 8):
        assert f"最近知识 {index}" in response.text
    assert "最近知识 1" not in response.text
    assert response.text.index("最近知识 7") < response.text.index("最近知识 6")
    assert "完整论证保留在正式知识文档中" not in response.text
    assert LIBRARY_SNAPSHOT not in response.text
    assert "evidence" not in response.text
    assert [record for record in result_ids if record] == list(range(1, 8))


def test_home_bad_recent_record_is_skipped_without_consuming_six_slots(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    ids = [
        add_published_library_result(
            database_path,
            f"bad-home-{index}",
            title=f"可读知识 {index}",
        )[2]
        for index in range(1, 9)
    ]
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER knowledge_result_content_cannot_be_updated")
        connection.execute(
            "UPDATE knowledge_results SET payload_json = '{private broken payload' WHERE knowledge_result_id = ?",
            (ids[-1],),
        )
    app = library_app(database_path)

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert response.text.count('<details class="recent-details">') == 6
    assert "有 1 份正式知识当前不完整" in response.text
    assert "可读知识 8" not in response.text
    for index in range(2, 8):
        assert f"可读知识 {index}" in response.text
    assert "{private broken payload" not in response.text


def test_home_recent_query_failure_is_isolated_from_submission(tmp_path, monkeypatch):
    app = library_app(tmp_path / "knowledge.sqlite3")

    def fail_recent(*_args, **_kwargs):
        raise web_module.KnowledgeLibraryError("simulated recent failure")

    monkeypatch.setattr(web_module, "read_recent_formal_knowledge", fail_recent)

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert "最近沉淀暂时无法安全读取" in response.text
    assert "开始蒸馏" in response.text
    assert "simulated recent failure" not in response.text


@pytest.mark.parametrize("vault_kind", ["unconfigured", "missing", "outside"])
def test_home_keeps_recent_item_without_unsafe_obsidian_link(tmp_path, vault_kind):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = None if vault_kind == "unconfigured" else tmp_path / "vault"
    if vault_root is not None:
        vault_root.mkdir()
    _, _, result_id, _ = add_published_library_result(
        database_path,
        f"home-{vault_kind}",
        title="无坏链接也可阅读",
    )
    if vault_kind == "outside":
        outside = tmp_path / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        with connect(database_path) as connection:
            connection.execute(
                "UPDATE knowledge_results SET published_path = ? WHERE knowledge_result_id = ?",
                (str(outside), result_id),
            )
    app = library_app(database_path, vault_root=vault_root)

    response = app.test_client().get("/")

    assert "无坏链接也可阅读" in response.text
    assert 'class="recent-obsidian-action"' not in response.text
    assert "obsidian://open" not in response.text


def test_home_safe_obsidian_link_is_outside_the_details_toggle(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _, _, _, relative_path = add_published_library_result(
        database_path,
        "home-safe-link",
        title="安全打开知识",
    )
    target = vault_root / relative_path
    target.parent.mkdir()
    target.write_text("probe", encoding="utf-8")
    app = library_app(database_path, vault_root=vault_root)

    response = app.test_client().get("/")
    expected_uri = "obsidian://open?path=" + quote(str(target.resolve()), safe="")

    assert expected_uri in response.text
    details_end = response.text.index("</details>")
    link_start = response.text.index('class="recent-obsidian-action"')
    assert details_end < link_start


def test_home_get_is_read_only_and_does_not_touch_topic_or_pipeline(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _, _, _, relative_path = add_published_library_result(
        database_path,
        "home-read-only",
    )
    target = vault_root / relative_path
    target.parent.mkdir()
    target.write_text("user-owned markdown", encoding="utf-8")
    markdown_before = (
        hashlib.sha256(target.read_bytes()).hexdigest(),
        target.stat().st_mtime_ns,
    )
    with connect(database_path) as connection:
        database_before = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
    app = library_app(database_path, vault_root=vault_root)

    class UnexpectedTopicLibrary:
        def __getattr__(self, name):
            raise AssertionError(f"home GET touched Topic operation {name}")

    app.config["TOPIC_LIBRARY"] = UnexpectedTopicLibrary()

    response = app.test_client().get("/")

    assert response.status_code == 200
    with connect(database_path) as connection:
        database_after = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
    markdown_after = (
        hashlib.sha256(target.read_bytes()).hexdigest(),
        target.stat().st_mtime_ns,
    )
    assert database_after == database_before
    assert markdown_after == markdown_before
