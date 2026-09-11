import pytest

from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    get_task,
    initialize_database,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.media import VerifiedTemporaryMedia
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary
from knowledge_distiller.primary import (
    AudioNormalization,
    AudioNormalizationFailure,
    PrimaryChunk,
    PrimaryFailure,
    PrimaryRecognition,
    PrimaryRecovery,
    StandardAudio,
)
from knowledge_distiller.legacy.primary_service import (
    PrimaryProcessingKind,
    recover_task_primary,
)


class StaticNormalizer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def normalize(self, media, work_dir):
        self.calls.append((media, work_dir))
        return self.result


class StaticRecognizer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def recognize(self, audio):
        self.calls.append(audio)
        return self.result


def identified_task(database_path):
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")
    identity = ConfirmedMaterialIdentity(
        platform="douyin",
        platform_item_id="stable-work-1",
        original_url="https://v.douyin.com/example/",
        canonical_url="https://www.douyin.com/video/stable-work-1",
    )
    attach_task_to_material(database_path, task_id, identity)
    return task_id


def media(path, platform_item_id="stable-work-1"):
    return VerifiedTemporaryMedia(
        platform="douyin",
        platform_item_id=platform_item_id,
        path=path,
        duration_seconds=62.02,
    )


def successful_normalization(path):
    return AudioNormalization.succeeded(StandardAudio(path, 62.02))


def successful_recognition():
    return PrimaryRecognition.succeeded(
        PrimaryRecovery(
            text="完整 Primary 文本",
            language="Chinese",
            chunks=(PrimaryChunk("完整 Primary 文本", 0.0, 62.02, "Chinese"),),
        )
    )


def test_verified_media_can_enter_primary_without_creating_stable_result(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)
    normalizer = StaticNormalizer(
        successful_normalization(tmp_path / "runtime" / "standard.wav")
    )
    recognizer = StaticRecognizer(successful_recognition())

    result = recover_task_primary(
        database_path,
        task_id,
        media(tmp_path / "runtime" / "source.mp4"),
        normalizer,
        recognizer,
        tmp_path / "runtime",
    )

    assert result.kind is PrimaryProcessingKind.READY
    assert result.recovery.text == "完整 Primary 文本"
    assert len(normalizer.calls) == 1
    assert recognizer.calls == [result.audio]
    assert decide_next_boundary(database_path, task_id) is NextBoundary.SOURCE_FACT_PRODUCTION
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge_results").fetchone()[0] == 0
        columns = {
            row[1]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
    assert not {"media_path", "wav_path", "asr_path", "primary_completed"} & columns


@pytest.mark.parametrize(
    "normalization,recognition,reason",
    [
        (
            AudioNormalization.failed(AudioNormalizationFailure.CONVERSION_FAILED),
            successful_recognition(),
            "primary_audio_conversion_failed",
        ),
        (
            successful_normalization("standard.wav"),
            PrimaryRecognition.failed(PrimaryFailure.INCOMPLETE),
            "primary_incomplete",
        ),
    ],
)
def test_primary_failures_do_not_create_source_fact(
    tmp_path,
    normalization,
    recognition,
    reason,
):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)

    result = recover_task_primary(
        database_path,
        task_id,
        media(tmp_path / "source.mp4"),
        StaticNormalizer(normalization),
        StaticRecognizer(recognition),
        tmp_path / "runtime",
    )

    assert result.kind is PrimaryProcessingKind.FAILED
    task = get_task(database_path, task_id)
    assert task["last_failure_boundary"] == "source_fact_production"
    assert task["last_failure_reason"] == reason
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_primary_rejects_verified_media_for_another_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id = identified_task(database_path)

    with pytest.raises(ValueError, match="does not belong"):
        recover_task_primary(
            database_path,
            task_id,
            media(tmp_path / "source.mp4", "another-work"),
            StaticNormalizer(successful_normalization(tmp_path / "standard.wav")),
            StaticRecognizer(successful_recognition()),
            tmp_path / "runtime",
        )
