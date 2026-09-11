from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    get_task,
    initialize_database,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.media import (
    MediaAcquisition,
    MediaAcquisitionKind,
    MediaFailure,
    VerifiedTemporaryMedia,
)
from knowledge_distiller.legacy.media_service import MediaProcessingKind, prepare_task_media
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary


class SequenceAcquirer:
    def __init__(self, acquisitions):
        self.acquisitions = iter(acquisitions)
        self.calls = []

    def acquire(self, identity, target_url, work_dir):
        self.calls.append((identity, target_url, work_dir))
        return next(self.acquisitions)


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
    return task_id, identity


def ready_media(path):
    return MediaAcquisition.succeeded(
        MediaAcquisitionKind.DOWNLOADED,
        VerifiedTemporaryMedia(
            platform="douyin",
            platform_item_id="stable-work-1",
            path=path,
            duration_seconds=62.02,
        ),
    )


def test_verified_media_remains_inside_source_boundary_without_source_fact(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, identity = identified_task(database_path)
    acquirer = SequenceAcquirer([ready_media(tmp_path / "runtime" / "source.mp4")])

    result = prepare_task_media(database_path, task_id, acquirer, tmp_path / "runtime")

    assert result.kind is MediaProcessingKind.READY
    assert result.media.platform_item_id == identity.platform_item_id
    assert decide_next_boundary(database_path, task_id) is NextBoundary.SOURCE_FACT_PRODUCTION
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0
    assert acquirer.calls[0][0] == identity
    assert acquirer.calls[0][1] == identity.canonical_url


def test_media_login_recovery_keeps_same_task_and_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _ = identified_task(database_path)
    acquirer = SequenceAcquirer(
        [
            MediaAcquisition.failed(MediaFailure.LOGIN_REQUIRED),
            ready_media(tmp_path / "runtime" / "source.mp4"),
        ]
    )

    waiting = prepare_task_media(database_path, task_id, acquirer, tmp_path / "runtime")
    waiting_task = get_task(database_path, task_id)
    resumed = prepare_task_media(database_path, task_id, acquirer, tmp_path / "runtime")

    assert waiting.kind is MediaProcessingKind.LOGIN_REQUIRED
    assert waiting_task["waiting_reason"] == "douyin_login_required"
    assert waiting_task["material_id"] is not None
    assert resumed.kind is MediaProcessingKind.READY
    assert resumed.task_id == task_id
    assert get_task(database_path, task_id)["waiting_reason"] is None
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 1


def test_media_failure_is_thin_source_boundary_diagnostic(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _ = identified_task(database_path)
    acquirer = SequenceAcquirer(
        [MediaAcquisition.failed(MediaFailure.INTEGRITY_FAILED)]
    )

    result = prepare_task_media(database_path, task_id, acquirer, tmp_path / "runtime")

    task = get_task(database_path, task_id)
    assert result.kind is MediaProcessingKind.FAILED
    assert task["last_failure_boundary"] == "source_fact_production"
    assert task["last_failure_reason"] == "media_integrity_failed"
    assert task["waiting_reason"] is None
