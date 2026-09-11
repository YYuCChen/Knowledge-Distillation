from knowledge_distiller.database import connect, create_task, get_task, initialize_database
from knowledge_distiller.identity import (
    ConfirmedMaterialIdentity,
    IdentityFailure,
    IdentityResolution,
)
from knowledge_distiller.legacy.identity_service import (
    IdentityProcessingKind,
    confirm_task_identity,
)
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary


class StableIdentityResolver:
    def __init__(self, platform_item_id="stable-work-1"):
        self.platform_item_id = platform_item_id

    def identify(self, original_url, target_url):
        return IdentityResolution.confirmed(
            ConfirmedMaterialIdentity(
                platform="douyin",
                platform_item_id=self.platform_item_id,
                original_url=original_url,
                canonical_url=(
                    f"https://www.douyin.com/video/{self.platform_item_id}"
                ),
            )
        )


class SequenceResolver:
    def __init__(self, resolutions):
        self.resolutions = iter(resolutions)

    def identify(self, original_url, target_url):
        resolution = next(self.resolutions)
        if resolution.identity is None:
            return resolution
        return IdentityResolution.confirmed(
            ConfirmedMaterialIdentity(
                platform=resolution.identity.platform,
                platform_item_id=resolution.identity.platform_item_id,
                original_url=original_url,
                canonical_url=resolution.identity.canonical_url,
            )
        )


def test_different_urls_for_same_stable_identity_reuse_material_and_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    resolver = StableIdentityResolver()
    first_task = create_task(database_path, "https://v.douyin.com/first/")
    second_task = create_task(
        database_path, "https://www.douyin.com/video/stable-work-1"
    )

    first = confirm_task_identity(database_path, first_task, resolver)
    second = confirm_task_identity(database_path, second_task, resolver)

    assert first.kind is IdentityProcessingKind.CONFIRMED
    assert second.kind is IdentityProcessingKind.CONFIRMED
    assert second.task_id == first.task_id
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        material = connection.execute("SELECT * FROM materials").fetchone()
        task = connection.execute("SELECT * FROM tasks").fetchone()
    assert material["platform_item_id"] == "stable-work-1"
    assert material["original_url"] == "https://v.douyin.com/first/"
    assert task["material_id"] == material["material_id"]


def test_same_url_resubmission_reuses_the_current_processing_chain(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    resolver = StableIdentityResolver()
    submitted_url = "https://v.douyin.com/same/"
    first_task = create_task(database_path, submitted_url)
    first = confirm_task_identity(database_path, first_task, resolver)
    duplicate_task = create_task(database_path, submitted_url)

    duplicate = confirm_task_identity(database_path, duplicate_task, resolver)

    assert duplicate.task_id == first.task_id
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_material_identity_still_leaves_source_fact_as_next_boundary(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")

    result = confirm_task_identity(database_path, task_id, StableIdentityResolver())

    assert (
        decide_next_boundary(database_path, result.task_id)
        is NextBoundary.SOURCE_FACT_PRODUCTION
    )
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_facts").fetchone()[0] == 0


def test_login_recovery_continues_the_same_task(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")
    identity = ConfirmedMaterialIdentity(
        platform="douyin",
        platform_item_id="stable-work-1",
        original_url="placeholder",
        canonical_url="https://www.douyin.com/video/stable-work-1",
    )
    resolver = SequenceResolver(
        [
            IdentityResolution.failed(IdentityFailure.LOGIN_REQUIRED),
            IdentityResolution.confirmed(identity),
        ]
    )

    waiting = confirm_task_identity(database_path, task_id, resolver)
    waiting_task = get_task(database_path, task_id)
    resumed = confirm_task_identity(database_path, task_id, resolver)

    assert waiting.kind is IdentityProcessingKind.LOGIN_REQUIRED
    assert waiting_task["waiting_boundary"] == "source_fact_production"
    assert waiting_task["waiting_reason"] == "douyin_login_required"
    assert resumed.kind is IdentityProcessingKind.CONFIRMED
    assert resumed.task_id == task_id
    assert get_task(database_path, task_id)["material_id"] is not None


def test_identity_failure_does_not_create_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")
    resolver = SequenceResolver(
        [IdentityResolution.failed(IdentityFailure.IDENTITY_UNCONFIRMED)]
    )

    result = confirm_task_identity(database_path, task_id, resolver)

    assert result.kind is IdentityProcessingKind.FAILED
    with connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 0
    task = get_task(database_path, task_id)
    assert task["material_id"] is None
    assert task["last_failure_reason"] == "identity_unconfirmed"
