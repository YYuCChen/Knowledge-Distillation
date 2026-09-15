from knowledge_distiller.v1.database import SCHEMA_VERSION
import json
import sqlite3
from pathlib import Path

import pytest

from knowledge_distiller.v1.domain import (
    CapturedMaterial,
    Evidence,
    Knowledge,
    Point,
    SourceFact,
)
from knowledge_distiller.v1.database import connect, SCHEMA
from knowledge_distiller.v1.store import Store


def captured(tmp_path: Path) -> CapturedMaterial:
    media = tmp_path / "source.mp4"
    media.write_bytes(b"media")
    return CapturedMaterial(
        "douyin",
        "123",
        "https://v.douyin.com/a/",
        "https://www.douyin.com/video/123",
        {"author": {"display_name": "测试作者"}},
        media,
        12.5,
    )


def knowledge() -> Knowledge:
    return Knowledge(
        "注意力需要边界",
        "说明边界如何保护有限注意力。",
        "主动设定边界可以减少注意力损耗。",
        (Point("p1", "边界保护注意力。", "持续切换会带来额外损耗。", ("e1",)),),
        (),
        (Evidence("e1", 0, 7, "持续切换会带来"),),
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "knowledge.sqlite3")
    result.initialize()
    return result


def test_clean_store_records_one_complete_result(store: Store, tmp_path: Path) -> None:
    item_id = store.create_item("https://v.douyin.com/a/")
    store.mark_working(item_id, "collecting")
    material_id = store.attach_material(item_id, captured(tmp_path))
    fact_id = store.establish_source_fact(
        material_id, SourceFact("持续切换会带来额外损耗。")
    )
    result_id = store.establish_knowledge(fact_id, knowledge())
    store.mark_published(result_id, "知识蒸馏器/注意力--kr-1.md", vault=tmp_path)
    store.mark_succeeded(item_id)

    row = store.item_bundle(item_id)

    assert row is not None
    assert row["state"] == "succeeded"
    assert row["source_kind"] == "douyin"
    assert row["source_fact_id"] == fact_id
    assert row["knowledge_result_id"] == result_id
    assert row["published_path"] == "知识蒸馏器/注意力--kr-1.md"


def test_source_fact_is_idempotent_but_immutable(store: Store, tmp_path: Path) -> None:
    item_id = store.create_item("https://v.douyin.com/a/")
    material_id = store.attach_material(item_id, captured(tmp_path))
    first = store.establish_source_fact(material_id, SourceFact("完整来源。"))

    assert store.establish_source_fact(material_id, SourceFact("完整来源。")) == first
    with pytest.raises(ValueError, match="differs"):
        store.establish_source_fact(material_id, SourceFact("被改写的来源。"))

    with connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_facts SET snapshot = '改写' WHERE source_fact_id = ?",
                (first,),
            )


def test_duplicate_material_reuses_identity_and_formal_results(
    store: Store, tmp_path: Path
) -> None:
    first_item = store.create_item("https://v.douyin.com/a/")
    second_item = store.create_item("https://www.douyin.com/video/123")

    first_material = store.attach_material(first_item, captured(tmp_path))
    second_material = store.attach_material(second_item, captured(tmp_path))
    fact_id = store.establish_source_fact(
        first_material, SourceFact("持续切换会带来额外损耗。")
    )
    result_id = store.establish_knowledge(fact_id, knowledge())

    assert second_material == first_material
    assert store.item_bundle(second_item)["source_fact_id"] == fact_id
    assert store.item_bundle(second_item)["knowledge_result_id"] == result_id


def test_knowledge_result_cannot_change_for_same_source_fact(
    store: Store, tmp_path: Path
) -> None:
    item_id = store.create_item("https://v.douyin.com/a/")
    material_id = store.attach_material(item_id, captured(tmp_path))
    fact_id = store.establish_source_fact(
        material_id, SourceFact("持续切换会带来额外损耗。")
    )
    store.establish_knowledge(fact_id, knowledge())
    changed = knowledge()
    changed = Knowledge(
        changed.title,
        changed.subtitle,
        "另一份摘要。",
        changed.core_points,
        changed.other_points,
        changed.evidence,
    )

    with pytest.raises(ValueError, match="differs"):
        store.establish_knowledge(fact_id, changed)


def test_settings_and_connection_have_only_current_values(store: Store) -> None:
    store.set_setting("vault_path", "/tmp/vault")
    store.set_setting("vault_path", "/tmp/new-vault")
    store.save_connection("douyin", "@author")
    store.save_connection("douyin", "@new-author")

    connection = store.connection("douyin")

    assert store.setting("vault_path") == "/tmp/new-vault"
    assert connection["generation"] == 2
    assert connection["account_label"] == "@new-author"
    store.clear_connection("douyin")
    assert store.connection("douyin")["state"] == "unconfigured"
    store.save_connection("douyin", "@third-author")
    assert store.connection("douyin")["generation"] == 3


def test_knowledge_content_is_immutable_but_publication_can_be_recorded(
    store: Store, tmp_path: Path
) -> None:
    item_id = store.create_item("https://v.douyin.com/a/")
    material_id = store.attach_material(item_id, captured(tmp_path))
    fact_id = store.establish_source_fact(
        material_id, SourceFact("持续切换会带来额外损耗。")
    )
    result_id = store.establish_knowledge(fact_id, knowledge())

    with connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="content is immutable"):
            connection.execute(
                "UPDATE knowledge_results SET payload_json = '{}' WHERE knowledge_result_id = ?",
                (result_id,),
            )

    store.mark_published(result_id, "知识蒸馏器/结果.md", vault=tmp_path)
    assert store.item_bundle(item_id)["published_path"] == "知识蒸馏器/结果.md"


def test_unknown_schema_version_is_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(RuntimeError, match="unsupported database version"):
        Store(path).initialize()


def test_v1_schema_migrates_existing_queue_order(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE distill_items (
                   item_id INTEGER PRIMARY KEY,
                   created_at TEXT NOT NULL, confirmation_json TEXT
               )"""
        )
        connection.execute(
            "INSERT INTO distill_items (created_at) VALUES ('2026-09-05T01:02:03+00:00')"
        )
        connection.execute("CREATE TABLE source_facts (source_fact_id INTEGER PRIMARY KEY, material_id INTEGER)")
        connection.execute("CREATE TABLE source_connections (platform TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE materials " + SCHEMA.split("CREATE TABLE materials ", 1)[1].split(";", 1)[0])
        connection.execute("DROP TABLE IF EXISTS group_decisions")
        connection.execute("DROP TABLE IF EXISTS manual_cards")
        connection.execute("PRAGMA user_version=1")
        connection.execute("CREATE TABLE knowledge_results (knowledge_result_id INTEGER PRIMARY KEY, source_fact_id INTEGER)")

    from knowledge_distiller.v1.database import initialize
    initialize(path)

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        queued_at = connection.execute(
            "SELECT queued_at FROM distill_items WHERE item_id = 1"
        ).fetchone()[0]
    assert version == SCHEMA_VERSION
    assert queued_at == "2026-09-05T01:02:03+00:00"


def test_fifo_retry_rejoins_tail_and_restart_recovers_head(store: Store) -> None:
    first = store.create_item("https://v.douyin.com/first/")
    second = store.create_item("https://v.douyin.com/second/")
    assert store.claim_next_item() == first
    store.mark_failed(first, "collecting", "temporary")
    store.retry_item(first)
    assert store.claim_next_item() == second
    store.mark_failed(second, "collecting", "temporary")

    third = store.create_item("https://v.douyin.com/third/")
    assert store.claim_next_item() == first
    fourth = store.create_item("https://v.douyin.com/fourth/")
    assert store.requeue_interrupted() == 1
    assert store.claim_next_item() == first
    assert store.item_bundle(third)["state"] == "queued"
    assert store.item_bundle(fourth)["state"] == "queued"


def test_v2_migration_does_not_infer_old_publication_destination(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE knowledge_results (
                knowledge_result_id INTEGER PRIMARY KEY,
                source_fact_id INTEGER,
                published_path TEXT
            );
            INSERT INTO knowledge_results VALUES (1, NULL, '知识蒸馏器/old.md');
            CREATE TABLE distill_items (item_id INTEGER PRIMARY KEY, confirmation_json TEXT, created_at TEXT);
            CREATE TABLE source_facts (source_fact_id INTEGER PRIMARY KEY, material_id INTEGER);
            CREATE TABLE source_connections (platform TEXT PRIMARY KEY);
            PRAGMA user_version = 2;
        """)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE materials " + SCHEMA.split("CREATE TABLE materials ", 1)[1].split(";", 1)[0])
    from knowledge_distiller.v1.database import initialize
    initialize(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT published_path, published_vault FROM knowledge_results"
        ).fetchone() == ("知识蒸馏器/old.md", None)


def test_waiting_and_failure_are_user_visible_facts(store: Store) -> None:
    item_id = store.create_item("https://v.douyin.com/a/")
    store.mark_waiting(item_id, {"text": "原文疑点", "choices": ["一", "二"]})
    waiting = store.item_bundle(item_id)
    assert waiting["state"] == "waiting_user"
    assert json.loads(waiting["confirmation_json"])["choices"] == ["一", "二"]

    store.mark_failed(item_id, "reviewing", "source_unconfirmed")
    failed = store.item_bundle(item_id)
    assert failed["state"] == "failed"
    assert failed["error_code"] == "source_unconfirmed"


def test_v11_migration_preserves_item_and_does_not_invent_rejection_reason(tmp_path):
    store = Store(tmp_path / 'v11.sqlite3')
    store.initialize()
    item = store.create_item('https://v.douyin.com/a/')
    store.mark_failed(item, 'distilling', 'knowledge_not_qualified')
    with connect(store.path) as db:
        db.execute('ALTER TABLE distill_items DROP COLUMN rejection_reason')
        for table in ('media_lifecycle','feishu_parts','feishu_receipts','feishu_binding','collection_previews'):
            db.execute(f'DROP TABLE {table}')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=11')
        before = dict(db.execute('SELECT * FROM distill_items').fetchone())
    store.initialize()
    with connect(store.path) as db:
        after = dict(db.execute('SELECT * FROM distill_items').fetchone())
        assert after.pop('rejection_reason') is None
        assert after == before
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


def test_v12_dismiss_migration_preserves_existing_failure(tmp_path):
    store = Store(tmp_path / 'v12.sqlite3')
    store.initialize()
    item = store.create_item('https://v.douyin.com/a/')
    store.mark_failed(item, 'distilling', 'knowledge_not_qualified', rejection_reason='仅描述外观。')
    with connect(store.path) as db:
        db.execute('DROP TRIGGER source_media_no_update')
        db.execute("""CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media
            WHEN EXISTS(SELECT 1 FROM source_facts WHERE material_id=OLD.material_id)
            BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END""")
        db.execute('ALTER TABLE distill_items DROP COLUMN dismissed_at')
        for table in ('media_lifecycle','feishu_parts','feishu_receipts','feishu_binding','collection_previews'):
            db.execute(f'DROP TABLE {table}')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=12')
        before = dict(db.execute('SELECT * FROM distill_items').fetchone())
    store.initialize()
    with connect(store.path) as db:
        after = dict(db.execute('SELECT * FROM distill_items').fetchone())
        assert after.pop('dismissed_at') is None
        assert after == before


def test_v13_title_migration_preserves_user_state_and_source_fact(tmp_path):
    from knowledge_distiller.v1.database import connect
    store = Store(tmp_path/'migration.sqlite3')
    store.initialize()
    item = store.create_item('https://www.douyin.com/video/123')
    store.mark_failed(item, 'collecting', 'fixture_failure')
    with connect(store.path) as db:
        db.execute('ALTER TABLE distill_items DROP COLUMN submitted_title')
        for table in ('media_lifecycle','feishu_parts','feishu_receipts','feishu_binding','collection_previews'):
            db.execute(f'DROP TABLE {table}')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=13')
        before = dict(db.execute('SELECT * FROM distill_items').fetchone())
    store.initialize()
    with connect(store.path) as db:
        after = dict(db.execute('SELECT * FROM distill_items').fetchone())
        assert after.pop('submitted_title') == ''
        assert after == before
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_recent_limit_counts_distinct_knowledge_and_preserves_pending_tasks(store, tmp_path):
    from dataclasses import replace
    capture = captured(tmp_path)
    def complete(value):
        item = store.create_item(value.submitted_url)
        material = store.attach_material(item, value)
        fact = store.establish_source_fact(material, SourceFact("持续切换会带来额外损耗。"))
        store.establish_knowledge(fact, knowledge())
        store.mark_succeeded(item)
        return item
    first = complete(capture)
    # Same title/content on another material must remain a distinct knowledge.
    second = complete(replace(capture, source_key='456', canonical_url='https://www.douyin.com/video/456'))
    for _ in range(35):
        item = store.create_item(capture.submitted_url)
        store.attach_material(item, capture)
        store.mark_succeeded(item)
    pending = store.create_item(capture.submitted_url)
    store.attach_material(pending, capture)
    restarted = Store(store.path)
    rows = restarted.recent_items(limit=2)
    assert [r['item_id'] for r in rows if r['state']=='succeeded'] == [second, first]
    assert [r['item_id'] for r in rows if r['state']=='queued'] == [pending]
    assert restarted.item_bundle(item)['state'] == 'succeeded'


def test_recent_card_keeps_the_first_completed_attempt(store, tmp_path):
    capture = captured(tmp_path)
    first = store.create_item(capture.submitted_url)
    second = store.create_item(capture.submitted_url)
    for item in (first, second):
        store.attach_material(item, capture)
    store.mark_succeeded(second)
    store.mark_succeeded(first)
    assert [row['item_id'] for row in store.recent_items()] == [second]


def test_previous_release_schema_16_upgrades_and_keeps_facts(store,tmp_path):
    item=store.create_item('https://v.douyin.com/a/')
    material=store.attach_material(item,captured(tmp_path))
    fact=store.establish_source_fact(material,SourceFact('升级必须保留的真实来源。'))
    with connect(store.path) as db:
        db.execute('DROP TABLE confirmation_decisions')
        db.execute('DROP TABLE feishu_action_queue')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=16')
    store.initialize()
    with connect(store.path) as db:
        # Schema 18 adds complete review results; migration must retain facts.
        assert db.execute('PRAGMA user_version').fetchone()[0]== SCHEMA_VERSION
        assert db.execute('SELECT snapshot FROM source_facts WHERE source_fact_id=?',(fact,)).fetchone()[0]=='升级必须保留的真实来源。'
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
        assert db.execute('SELECT count(*) FROM confirmation_decisions').fetchone()[0]==0


def test_review_commit_rejects_late_attempt_after_requeue(store, tmp_path):
    item = store.create_item('https://v.douyin.com/a/')
    store.attach_material(item, captured(tmp_path))
    store.mark_working(item, 'reviewing')
    revision = store.item_bundle(item)['review_revision']
    store.requeue_interrupted()
    store.mark_working(item, 'reviewing')
    with pytest.raises(ValueError, match='revision_conflict'):
        store.commit_source_review(item, revision, 'source', {'failure': None},
                                   fact=SourceFact('late result'))
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM source_review_results').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM source_facts').fetchone()[0] == 0


def test_review_commit_and_fact_are_atomic_on_database_failure(store, tmp_path):
    item = store.create_item('https://v.douyin.com/a/')
    store.attach_material(item, captured(tmp_path))
    store.mark_working(item, 'reviewing')
    revision = store.item_bundle(item)['review_revision']
    with connect(store.path) as db:
        db.execute("""CREATE TRIGGER injected_failure BEFORE INSERT ON source_facts
                      BEGIN SELECT RAISE(ABORT, 'injected disk write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        store.commit_source_review(item, revision, 'source', {'failure': None},
                                   fact=SourceFact('source'))
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM source_review_results').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM source_facts').fetchone()[0] == 0
        db.execute('DROP TRIGGER injected_failure')
    assert store.item_bundle(item)['review_revision'] == revision
    store.commit_source_review(item, revision, 'source', {'failure': None},
                               fact=SourceFact('source'))
    with connect(store.path) as db:
        assert db.execute('SELECT status FROM source_review_results').fetchone()[0] == 'complete'
    assert store.item_bundle(item)['snapshot'] == 'source'


def test_schema_17_adds_review_state_without_inventing_completion(store, tmp_path):
    item = store.create_item('https://v.douyin.com/a/')
    store.attach_material(item, captured(tmp_path))
    before = dict(store.item_bundle(item))
    before.pop('review_revision')
    with connect(store.path) as db:
        db.execute('DROP TRIGGER distill_review_revision')
        db.execute('DROP TABLE source_review_results')
        db.execute('ALTER TABLE distill_items DROP COLUMN review_revision')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=17')
    store.initialize()
    after = dict(store.item_bundle(item))
    assert after.pop('review_revision') == 0
    assert after == before
    with connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert db.execute('SELECT count(*) FROM source_review_results').fetchone()[0] == 0
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_submitted_review_rejects_stale_state_without_consuming_source(store):
    from knowledge_distiller.v1.file_sources import prepare_direct_text
    from knowledge_distiller.v1.source_parsing import ParsedSource
    source = prepare_direct_text('必须保留的来源')
    item = store.submit_source(source)
    store.mark_working(item, 'reviewing')
    revision = store.item_bundle(item)['review_revision']
    store.requeue_interrupted()
    with pytest.raises(ValueError, match='revision_conflict'):
        store.establish_submitted_fact(item, source, ParsedSource(source.content.decode(), {}, {}),
            expected_revision=revision, review_result={'snapshot': 'late'})
    assert store.item_bundle(item)['state'] == 'queued'
    assert store.submitted_source(item).content == source.content
    with connect(store.path) as db:
        assert not db.execute('SELECT 1 FROM materials').fetchone()
        assert not db.execute('SELECT 1 FROM source_review_results').fetchone()
