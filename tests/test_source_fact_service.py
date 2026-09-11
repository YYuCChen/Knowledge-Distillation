import json
import sqlite3

import pytest

from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_knowledge_result,
    get_task,
    initialize_database,
)
from knowledge_distiller.faithful_review import (
    FaithfulReviewCandidate,
    ReviewConcern,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.knowledge_derivation import (
    KnowledgeCandidate,
    KnowledgeEvidence,
    KnowledgePoint,
    knowledge_candidate_payload,
    validate_knowledge_candidate,
)
from knowledge_distiller.media import VerifiedTemporaryMedia
from knowledge_distiller.obsidian_renderer import render_task_knowledge_markdown
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery
from knowledge_distiller.primary import StandardAudio
from knowledge_distiller.secondary import (
    SecondaryAudio,
    SecondaryFailure,
    SecondaryResolution,
)
from knowledge_distiller.legacy.source_fact_service import (
    SourceFactProcessingKind,
    apply_human_resolution,
    produce_task_source_fact,
)


TEXT = (
    "作者先介绍药品集中采购的基本流程，然后解释医疗机构如何上报采购量。"
    "随后作者说明中选企业的供应责任、医院的采购要求，以及奖励资金的使用范围。"
)


def identified_task(database_path, item_id="stable-work-1"):
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            platform="douyin",
            platform_item_id=item_id,
            original_url="https://v.douyin.com/example/",
            canonical_url=f"https://www.douyin.com/video/{item_id}",
        ),
    )
    return task_id


def media(
    path,
    item_id="stable-work-1",
    *,
    author_name=None,
    author_platform_id=None,
    original_description=None,
    published_at=None,
):
    return VerifiedTemporaryMedia(
        "douyin",
        item_id,
        path,
        62.02,
        author_name,
        author_platform_id,
        original_description,
        published_at,
    )


def recovery():
    return PrimaryRecovery(
        TEXT,
        "Chinese",
        (PrimaryChunk(TEXT, 0.0, 62.02, "Chinese"),),
    )


def candidate(*concerns):
    return FaithfulReviewCandidate(TEXT, tuple(concerns))


def uncertainty(phrase="作者", *, meaning_may_change=False):
    start = TEXT.index(phrase)
    return ReviewConcern(
        start,
        start + len(phrase),
        phrase,
        "局部称呼无法安全唯一确定。",
        meaning_may_change,
        (phrase,),
    )


class RecordingClipper:
    def __init__(self):
        self.calls = []

    def clip(self, audio, start_seconds, end_seconds, output_path):
        self.calls.append((audio, start_seconds, end_seconds, output_path))
        return SecondaryAudio(
            output_path,
            start_seconds,
            end_seconds,
            end_seconds - start_seconds,
        )


class RecordingSecondary:
    def __init__(self, resolution):
        self.resolution = resolution
        self.calls = []

    @property
    def available(self):
        return self.resolution.failure is not SecondaryFailure.RUNTIME_UNAVAILABLE

    def resolve(self, audio):
        self.calls.append(audio)
        return self.resolution


def test_normal_candidate_establishes_immutable_source_fact_atomically(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    resolver = RecordingSecondary(
        SecondaryResolution.succeeded("不应在正常路径被调用")
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=resolver,
        audio_clipper=RecordingClipper(),
    )

    assert result.kind is SourceFactProcessingKind.ESTABLISHED
    assert resolver.calls == []
    assert result.created is True
    assert result.accepted_with_uncertainty is False
    with connect(database_path) as connection:
        material = connection.execute("SELECT * FROM materials").fetchone()
        source_fact = connection.execute("SELECT * FROM source_facts").fetchone()
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    assert material["current_source_fact_id"] == result.source_fact_id
    assert source_fact["source_fact_id"] == result.source_fact_id
    assert source_fact["material_id"] == material["material_id"]
    assert source_fact["content_snapshot"] == TEXT
    assert json.loads(source_fact["uncertainty_json"]) == []
    metadata = json.loads(source_fact["metadata_json"])
    assert metadata["platform"] == "douyin"
    assert metadata["platform_item_id"] == "stable-work-1"
    assert metadata["content_type"] == "video"
    assert metadata["media_duration_seconds"] == 62.02
    assert decide_next_boundary(database_path, task_id) is NextBoundary.KNOWLEDGE_DERIVATION
    task = get_task(database_path, task_id)
    assert task["last_failure_reason"] is None

    with connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_facts SET content_snapshot = 'changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM source_facts")


def test_long_source_fact_is_paragraphed_before_evidence_and_rendering(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    long_text = "".join(
        f"第{index}部分说明品牌、市场与销售之间各自承担的工作边界和衔接关系。"
        for index in range(45)
    )
    long_recovery = PrimaryRecovery(
        long_text,
        "Chinese",
        (PrimaryChunk(long_text, 0.0, 62.02, "Chinese"),),
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        long_recovery,
        FaithfulReviewCandidate(long_text, ()),
    )

    with connect(database_path) as connection:
        source = connection.execute("SELECT * FROM source_facts").fetchone()
    snapshot = source["content_snapshot"]
    assert snapshot.count("\n\n") >= 2
    evidence_text = "第42部分说明品牌、市场与销售之间各自承担的工作边界和衔接关系。"
    start = snapshot.index(evidence_text)
    candidate = KnowledgeCandidate(
        "品牌、市场与销售的工作边界",
        "三类职责需要正确衔接。",
        (
            KnowledgePoint(
                "p1",
                "品牌、市场与销售分别承担不同职责。",
                "三类工作存在各自边界，并需要形成完整衔接。",
                ("e1",),
            ),
        ),
        (),
        (
            KnowledgeEvidence(
                "e1",
                result.source_fact_id,
                start,
                start + len(evidence_text),
                evidence_text,
            ),
        ),
    )
    assert validate_knowledge_candidate(result.source_fact_id, snapshot, candidate)
    establish_knowledge_result(
        database_path,
        task_id,
        result.source_fact_id,
        knowledge_candidate_payload(candidate),
    )

    rendered = render_task_knowledge_markdown(database_path, task_id)

    assert rendered.source_block_count == snapshot.count("\n\n") + 1
    assert rendered.evidence_navigation_count == 1
    assert snapshot[start : start + len(evidence_text)] == evidence_text


def test_noncritical_uncertainty_is_saved_with_source_fact(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    concern = uncertainty()

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(concern),
    )

    assert result.kind is SourceFactProcessingKind.ESTABLISHED
    assert result.accepted_with_uncertainty is True
    with connect(database_path) as connection:
        row = connection.execute("SELECT uncertainty_json FROM source_facts").fetchone()
    assert json.loads(row["uncertainty_json"]) == [
        {
            "start": concern.start_offset,
            "end": concern.end_offset,
            "text": concern.text,
            "reason": concern.reason,
            "meaning_may_change": False,
            "candidate_readings": ["作者"],
        }
    ]


def test_available_source_metadata_is_saved_without_private_upstream_shape(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(
            tmp_path / "source.mp4",
            author_name="原作者",
            author_platform_id="MS4wLjABAAAA-stable-account",
            original_description="原平台描述 #药品集采",
            published_at="2026-01-02T03:04:05+00:00",
        ),
        recovery(),
        candidate(),
    )

    assert result.kind is SourceFactProcessingKind.ESTABLISHED
    with connect(database_path) as connection:
        row = connection.execute("SELECT metadata_json FROM source_facts").fetchone()
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
    metadata = json.loads(row["metadata_json"])
    assert metadata["author"] == {
        "display_name": "原作者",
        "platform_account_id": "MS4wLjABAAAA-stable-account",
    }
    assert metadata["original_description"] == "原平台描述 #药品集采"
    assert metadata["source_title"] is None
    assert metadata["published_at"] == "2026-01-02T03:04:05+00:00"
    assert "aweme_detail" not in metadata


def test_genuinely_missing_source_metadata_is_saved_as_null(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)

    produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(),
    )

    with connect(database_path) as connection:
        row = connection.execute("SELECT metadata_json FROM source_facts").fetchone()
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
    metadata = json.loads(row["metadata_json"])
    assert metadata["author"] is None
    assert metadata["original_description"] is None
    assert metadata["source_title"] is None
    assert metadata["published_at"] is None


@pytest.mark.parametrize(
    "review_candidate,kind,reason",
    [
        (
            FaithfulReviewCandidate("内容摘要。", ()),
            SourceFactProcessingKind.FAILED,
            "snapshot_rejected",
        ),
    ],
)
def test_unaccepted_candidate_does_not_create_source_fact(
    tmp_path,
    review_candidate,
    kind,
    reason,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        review_candidate,
    )

    assert result.kind is kind
    assert get_task(database_path, task_id)["last_failure_reason"] == reason
    assert decide_next_boundary(database_path, task_id) is NextBoundary.SOURCE_FACT_PRODUCTION
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_critical_local_concern_waits_for_secondary_only_when_needed(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(uncertainty("采购量", meaning_may_change=True)),
    )

    assert result.kind is SourceFactProcessingKind.WAITING_FOR_EXTERNAL_CONDITION
    task = get_task(database_path, task_id)
    assert task["waiting_boundary"] == "source_fact_production"
    assert task["waiting_reason"] == "secondary_unavailable"
    assert task["last_failure_reason"] is None
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_secondary_resolves_only_target_span_then_establishes_source_fact(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    start = TEXT.index("采购量")
    concern = ReviewConcern(
        start,
        start + len("采购量"),
        "采购量",
        "数量对象会改变原意。",
        True,
        ("上报量",),
    )
    clipper = RecordingClipper()
    resolver = RecordingSecondary(
        SecondaryResolution.succeeded("与原候选不同的其他上下文，上报量，以及额外文字。")
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=resolver,
        audio_clipper=clipper,
    )

    assert result.kind is SourceFactProcessingKind.ESTABLISHED
    assert len(clipper.calls) == 1
    assert len(resolver.calls) == 1
    assert 0 <= clipper.calls[0][1] < clipper.calls[0][2] <= 62.02
    with connect(database_path) as connection:
        row = connection.execute("SELECT content_snapshot FROM source_facts").fetchone()
    assert row["content_snapshot"] == TEXT[:start] + "上报量" + TEXT[concern.end_offset :]


def test_path_two_accepts_float_noise_at_adjacent_chunk_boundary(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    start = TEXT.index("采购量")
    concern = ReviewConcern(
        start,
        start + len("采购量"),
        "采购量",
        "数量对象会改变原意。",
        True,
        ("上报量",),
    )
    recovery_with_float_noise = PrimaryRecovery(
        TEXT,
        "Chinese",
        (
            PrimaryChunk(TEXT, 0.0, 181.4399375, "Chinese"),
            PrimaryChunk("补充", 181.43993749999998, 194.28275, "Chinese"),
        ),
    )
    clipper = RecordingClipper()

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery_with_float_noise,
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 194.28275),
        secondary_resolver=RecordingSecondary(
            SecondaryResolution.succeeded("局部独立恢复为上报量。")
        ),
        audio_clipper=clipper,
    )

    assert result.kind is SourceFactProcessingKind.ESTABLISHED
    assert len(clipper.calls) == 1


@pytest.mark.parametrize(
    "boundaries",
    [
        ((0.0, 40.0), (39.0, 62.02)),
        ((0.0, 40.0), (20.0, 30.0)),
    ],
)
def test_unreliable_chunk_timeline_blocks_path_two_without_bypassing_concern(
    tmp_path,
    boundaries,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    concern = uncertainty("采购量", meaning_may_change=True)
    unreliable_recovery = PrimaryRecovery(
        TEXT,
        "Chinese",
        tuple(
            PrimaryChunk(TEXT, start, end, "Chinese")
            for start, end in boundaries
        ),
    )
    clipper = RecordingClipper()
    resolver = RecordingSecondary(
        SecondaryResolution.succeeded("不应调用 Secondary")
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        unreliable_recovery,
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=resolver,
        audio_clipper=clipper,
    )

    assert result.kind is SourceFactProcessingKind.NEEDS_LOCAL_RESOLUTION
    assert clipper.calls == []
    assert resolver.calls == []
    assert get_task(database_path, task_id)["last_failure_reason"] == (
        "snapshot_needs_local_resolution"
    )
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "transcript",
    [
        "局部仍然听成采购量。",
        "局部可能是采购量，也可能是上报量。",
    ],
)
def test_secondary_without_unique_gain_does_not_establish_source_fact(
    tmp_path,
    transcript,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    start = TEXT.index("采购量")
    concern = ReviewConcern(
        start,
        start + len("采购量"),
        "采购量",
        "局部无法唯一确定。",
        True,
        ("上报量",),
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=RecordingSecondary(
            SecondaryResolution.succeeded(transcript)
        ),
        audio_clipper=RecordingClipper(),
    )

    assert result.kind is SourceFactProcessingKind.NEEDS_HUMAN_CONFIRMATION
    assert result.human_resolution is not None
    assert result.human_resolution.choices == ("采购量", "上报量")
    task = get_task(database_path, task_id)
    assert task["waiting_reason"] == "human_source_confirmation_required"
    assert task["last_failure_reason"] is None
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_secondary_runtime_unavailable_preserves_same_task_and_results(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    concern = uncertainty("采购量", meaning_may_change=True)
    resolver = RecordingSecondary(
        SecondaryResolution.failed(SecondaryFailure.RUNTIME_UNAVAILABLE)
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        FaithfulReviewCandidate(
            TEXT,
            (
                ReviewConcern(
                    concern.start_offset,
                    concern.end_offset,
                    concern.text,
                    concern.reason,
                    True,
                    ("上报量",),
                ),
            ),
        ),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=resolver,
        audio_clipper=RecordingClipper(),
    )

    assert result.task_id == task_id
    assert result.kind is SourceFactProcessingKind.NEEDS_HUMAN_CONFIRMATION
    assert result.human_resolution is not None
    assert get_task(database_path, task_id)["waiting_reason"] == (
        "human_source_confirmation_required"
    )
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_allowed_human_choice_changes_only_target_then_establishes_source_fact(
    tmp_path,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    start = TEXT.index("采购量")
    concern = ReviewConcern(
        start,
        start + len("采购量"),
        "采购量",
        "数量对象无法唯一确定。",
        True,
        ("上报量",),
    )
    initial = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=RecordingSecondary(
            SecondaryResolution.succeeded("采购量或上报量都有可能")
        ),
        audio_clipper=RecordingClipper(),
    )
    assert initial.human_resolution is not None

    corrected = apply_human_resolution(initial.human_resolution, "上报量")
    assert corrected.text == TEXT[:start] + "上报量" + TEXT[concern.end_offset :]
    assert corrected.concerns == ()
    final = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        corrected,
    )

    assert final.kind is SourceFactProcessingKind.ESTABLISHED
    with connect(database_path) as connection:
        row = connection.execute("SELECT content_snapshot FROM source_facts").fetchone()
    assert row["content_snapshot"] == corrected.text


def test_human_choice_cannot_inject_an_unregistered_replacement(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    start = TEXT.index("采购量")
    concern = ReviewConcern(
        start,
        start + len("采购量"),
        "采购量",
        "局部无法唯一确定。",
        True,
        ("上报量",),
    )
    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=RecordingSecondary(
            SecondaryResolution.succeeded("无唯一结果")
        ),
        audio_clipper=RecordingClipper(),
    )
    assert result.human_resolution is not None

    with pytest.raises(ValueError, match="not allowed"):
        apply_human_resolution(result.human_resolution, "任意改写全文")

    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_human_choice_still_cannot_bypass_unified_snapshot_acceptance(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    start = TEXT.index("采购量")
    unsupported = "无关内容" * 15
    concern = ReviewConcern(
        start,
        start + len("采购量"),
        "采购量",
        "局部无法唯一确定。",
        True,
        (unsupported,),
    )
    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=RecordingSecondary(
            SecondaryResolution.succeeded("没有唯一听法")
        ),
        audio_clipper=RecordingClipper(),
    )
    assert result.human_resolution is not None
    corrected = apply_human_resolution(result.human_resolution, unsupported)

    final = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        corrected,
    )

    assert final.kind is SourceFactProcessingKind.FAILED
    assert get_task(database_path, task_id)["last_failure_reason"] == (
        "snapshot_rejected"
    )
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_few_local_concerns_can_be_confirmed_one_at_a_time(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    first_start = TEXT.index("作")
    second_start = TEXT.index("医")
    first = ReviewConcern(first_start, first_start + 1, "作", "主体疑点。", True, ("撰",))
    second = ReviewConcern(second_start, second_start + 1, "医", "对象疑点。", True, ("院",))
    current = FaithfulReviewCandidate(TEXT, (first, second))
    resolver = RecordingSecondary(SecondaryResolution.succeeded("作医"))

    first_result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        current,
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=resolver,
        audio_clipper=RecordingClipper(),
    )
    assert first_result.human_resolution is not None
    current = apply_human_resolution(first_result.human_resolution, "撰")

    second_result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        current,
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=resolver,
        audio_clipper=RecordingClipper(),
    )
    assert second_result.human_resolution is not None
    current = apply_human_resolution(second_result.human_resolution, "院")

    final = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        current,
    )
    assert final.kind is SourceFactProcessingKind.ESTABLISHED
    assert current.concerns == ()


def test_overall_recovery_failure_never_calls_secondary(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    phrases = ("作者", "药品", "流程", "医院")
    concerns = tuple(
        sorted(
            (uncertainty(phrase, meaning_may_change=True) for phrase in phrases),
            key=lambda item: item.start_offset,
        )
    )
    resolver = RecordingSecondary(
        SecondaryResolution.succeeded("不应该被使用")
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(*concerns),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=resolver,
        audio_clipper=RecordingClipper(),
    )

    assert result.kind is SourceFactProcessingKind.FAILED
    assert resolver.calls == []


def test_secondary_correction_still_requires_unified_acceptance(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    start = TEXT.index("采购量")
    unsupported = "完全不相关的大量文字" * 12
    concern = ReviewConcern(
        start,
        start + len("采购量"),
        "采购量",
        "局部无法唯一确定。",
        True,
        (unsupported,),
    )

    result = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        recovery(),
        candidate(concern),
        standard_audio=StandardAudio(tmp_path / "standard.wav", 62.02),
        secondary_resolver=RecordingSecondary(
            SecondaryResolution.succeeded(unsupported)
        ),
        audio_clipper=RecordingClipper(),
    )

    assert result.kind is SourceFactProcessingKind.FAILED
    assert get_task(database_path, task_id)["last_failure_reason"] == (
        "snapshot_rejected_after_secondary"
    )
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_verified_media_must_belong_to_task_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)

    with pytest.raises(ValueError, match="does not belong"):
        produce_task_source_fact(
            database_path,
            task_id,
            media(tmp_path / "source.mp4", "another-work"),
            recovery(),
            candidate(),
        )

    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_repeated_execution_reuses_current_source_fact(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    verified_media = media(tmp_path / "source.mp4")

    first = produce_task_source_fact(
        database_path,
        task_id,
        verified_media,
        recovery(),
        candidate(),
    )
    second = produce_task_source_fact(
        database_path,
        task_id,
        media(tmp_path / "unrelated.mp4", "another-work"),
        recovery(),
        candidate(uncertainty("采购量", meaning_may_change=True)),
    )

    assert second.source_fact_id == first.source_fact_id
    assert second.created is False
    assert second.accepted_with_uncertainty is False
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 1


def test_source_fact_and_current_pointer_roll_back_together(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    with connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_source_pointer
            BEFORE UPDATE OF current_source_fact_id ON materials
            BEGIN
                SELECT RAISE(ABORT, 'forced pointer failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced pointer failure"):
        produce_task_source_fact(
            database_path,
            task_id,
            media(tmp_path / "source.mp4"),
            recovery(),
            candidate(),
        )

    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        material = connection.execute("SELECT * FROM materials").fetchone()
    assert material["current_source_fact_id"] is None


def test_source_fact_schema_has_no_temporary_checkpoint_columns(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)

    with connect(database_path) as connection:
        columns = {
            row[1]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    assert not {
        "media_path",
        "wav_path",
        "asr_path",
        "review_checkpoint",
        "review_raw_response",
        "candidate_snapshot",
        "secondary_completed",
        "secondary_audio_path",
        "secondary_transcript",
        "human_confirmation",
        "human_resolution_state",
        "human_audio_path",
        "human_candidate_snapshot",
    } & columns
