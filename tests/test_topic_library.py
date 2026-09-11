import json
import hashlib
import threading
import time
from dataclasses import fields, replace

from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_knowledge_result,
    establish_source_fact,
    initialize_database,
    query_searchable_formal_knowledge,
    record_knowledge_result_published,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.topic_indexing import (
    TopicDraft,
    TopicIndexing,
    TopicPlan,
    TopicPointReference,
    TopicIndexFailure,
)
from knowledge_distiller import topic_library as topic_library_module
from knowledge_distiller.topic_library import (
    TopicLibrary,
    TopicRefreshKind,
    _load_points,
    compute_source_signature,
)


SNAPSHOT = "正式来源支持当前观点。"


def add_knowledge(
    database_path,
    item_id,
    points,
    *,
    published=True,
    platform="douyin",
    metadata=None,
    published_path=None,
):
    initialize_database(database_path)
    task_id = create_task(database_path, f"https://example.test/{item_id}")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(platform, item_id, f"https://example.test/{item_id}", f"https://example.test/{item_id}"),
    )
    source = establish_source_fact(
        database_path,
        task_id,
        metadata
        or {"author": {"display_name": "作者", "platform_account_id": item_id}},
        SNAPSHOT,
        [],
    )
    core = []
    other = []
    for point_id, role, statement in points:
        target = core if role == "core" else other
        target.append({"id": point_id, "statement": statement, "argument": f"{statement}的完整论证。", "evidence_ids": ["e1"]})
    if not core:
        core.append({"id": "required-core", "statement": "必要核心观点。", "argument": "维持正式结构。", "evidence_ids": ["e1"]})
    knowledge = establish_knowledge_result(
        database_path,
        task_id,
        source.source_fact_id,
        {
            "title": f"知识 {item_id}",
            "summary": f"{item_id} 的一句话总括。",
            "core_points": core,
            "other_points": other,
            "evidence_registry": [{"id": "e1", "source_fact_id": source.source_fact_id, "start": 0, "end": len(SNAPSHOT), "evidence_text": SNAPSHOT}],
        },
    )
    if published:
        record_knowledge_result_published(
            database_path,
            task_id,
            knowledge.knowledge_result_id,
            published_path or f"知识蒸馏器/{item_id}.md",
        )
    return task_id, knowledge.knowledge_result_id


def eligible_signature(database_path):
    with connect(database_path) as connection:
        loaded = _load_points(
            query_searchable_formal_knowledge(connection), strict=True
        )
    return compute_source_signature(loaded.points)


class PlanningIndexer:
    def __init__(self, planner, *, available=True, delay=0):
        self.planner = planner
        self.available = available
        self.delay = delay
        self.calls = 0

    def is_available(self):
        return self.available

    def organize(self, points, existing_topics):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return TopicIndexing.succeeded(self.planner(points, existing_topics))


def first_two_topic(points, existing_topics=()):
    references = tuple(point.reference for point in points[:2])
    topic_id = existing_topics[0].topic_id if existing_topics else None
    return TopicPlan(
        (TopicDraft(topic_id, None if topic_id else "new-1", "采购与履约", "收纳采购形成与履约责任相关知识。", references),),
        tuple(point.reference for point in points[2:]),
    )


def all_points_topic(points, existing_topics=()):
    topic_id = existing_topics[0].topic_id if existing_topics else None
    return TopicPlan(
        (
            TopicDraft(
                topic_id,
                None if topic_id else "all-points",
                "全部当前观点",
                "收纳用于验证当前性交集的观点。",
                tuple(point.reference for point in points),
            ),
        ),
        (),
    )


def test_initial_refresh_current_skip_force_and_stable_existing_id(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (
            ("p1", "core", "采购需要先报量。"),
            ("p2", "other", "奖励与节约资金相关。"),
            ("p3", "other", "合同需要明确履约责任。"),
        ),
    )
    indexer = PlanningIndexer(first_two_topic)
    library = TopicLibrary(database_path, indexer)

    assert library.refresh().kind is TopicRefreshKind.REFRESHED
    first = library.snapshot()
    topic_id = first.topics[0].topic_id
    assert first.current and first.unassigned_count == 1
    assert library.refresh().kind is TopicRefreshKind.CURRENT
    assert indexer.calls == 1
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    assert indexer.calls == 2
    assert library.snapshot().topics[0].topic_id == topic_id


def test_force_refresh_updates_reused_topic_and_order_without_duplicates(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (
            ("p1", "core", "观点一。"),
            ("p2", "other", "观点二。"),
            ("p3", "other", "观点三。"),
        ),
    )

    def evolving_plan(points, existing_topics):
        by_id = {point.point_id: point.reference for point in points}
        if not existing_topics:
            return TopicPlan(
                (
                    TopicDraft(
                        None,
                        "new-1",
                        "旧主题名",
                        "旧范围。",
                        (by_id["p1"], by_id["p2"], by_id["p3"]),
                    ),
                ),
                (),
            )
        return TopicPlan(
            (
                TopicDraft(
                    existing_topics[0].topic_id,
                    None,
                    "新主题名",
                    "精炼后的新范围。",
                    (by_id["p3"], by_id["p1"]),
                ),
            ),
            (by_id["p2"],),
        )

    indexer = PlanningIndexer(evolving_plan)
    library = TopicLibrary(database_path, indexer)
    assert library.refresh().kind is TopicRefreshKind.REFRESHED
    original_id = library.snapshot().topics[0].topic_id

    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED

    topic = library.snapshot().topics[0]
    assert topic.topic_id == original_id
    assert (topic.name, topic.scope) == ("新主题名", "精炼后的新范围。")
    assert [card.point_id for card in topic.cards] == ["p3", "p1"]
    assert [card.role for card in topic.cards] == ["other", "core"]
    assert [point.knowledge_summary for point in topic.points] == [
        "one 的一句话总括。",
        "one 的一句话总括。",
    ]
    assert [card.point_id for card in topic.representative_cards] == ["p3", "p1"]
    assert library.snapshot().unassigned_count == 1
    with connect(database_path) as connection:
        memberships = connection.execute(
            "SELECT point_id, position FROM topic_memberships ORDER BY position"
        ).fetchall()
        assert [tuple(row) for row in memberships] == [("p3", 0), ("p1", 1)]
        assert connection.execute("SELECT count(*) FROM topics").fetchone()[0] == 1


def test_new_topic_id_is_not_reused_after_legal_empty_index(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (("p1", "core", "观点一。"), ("p2", "other", "观点二。")),
    )
    plans = [first_two_topic, lambda points, existing: TopicPlan((), tuple(point.reference for point in points)), first_two_topic]
    indexer = PlanningIndexer(lambda points, existing: plans[indexer.calls - 1](points, existing))
    library = TopicLibrary(database_path, indexer)

    library.refresh(force=True)
    first_id = library.snapshot().topics[0].topic_id
    library.refresh(force=True)
    assert library.snapshot().topics == ()
    assert library.snapshot().current
    library.refresh(force=True)
    assert library.snapshot().topics[0].topic_id > first_id


def test_stale_read_intersects_current_points_and_does_not_guess_new_members(tmp_path):
    """E2E-09: stale read intersects safely and never calls the model again."""
    database_path = tmp_path / "knowledge.sqlite3"
    _, first_result = add_knowledge(
        database_path,
        "one",
        (("p1", "core", "旧观点一。"), ("p2", "other", "旧观点二。")),
    )
    indexer = PlanningIndexer(first_two_topic)
    library = TopicLibrary(database_path, indexer)
    library.refresh()
    add_knowledge(database_path, "two", (("p3", "core", "新观点。"),))

    stale = library.snapshot()

    assert indexer.calls == 1
    assert not stale.current
    assert stale.uncovered_count == 1
    assert [card.statement for card in stale.topics[0].cards] == [
        "旧观点一。",
        "旧观点二。",
    ]
    with connect(database_path) as connection:
        connection.execute("UPDATE materials SET current_knowledge_result_id = NULL WHERE current_knowledge_result_id = ?", (first_result,))
    emptied = library.snapshot()
    assert emptied.topics == ()


def test_stale_read_removes_invalidated_and_unpublished_members_from_representatives(
    tmp_path,
):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path, "one", (("p1", "core", "仍当前的观点。"),)
    )
    _, invalidated_result = add_knowledge(
        database_path, "two", (("p2", "core", "已失效的观点。"),)
    )
    _, unpublished_result = add_knowledge(
        database_path, "three", (("p3", "core", "已取消发布的观点。"),)
    )
    library = TopicLibrary(database_path, PlanningIndexer(all_points_topic))
    assert library.refresh().kind is TopicRefreshKind.REFRESHED
    topic_id = library.snapshot().topics[0].topic_id
    assert len(library.snapshot().topics[0].representative_cards) == 3

    with connect(database_path) as connection:
        connection.execute(
            "UPDATE knowledge_results SET invalidated_at = 'now' "
            "WHERE knowledge_result_id = ?",
            (invalidated_result,),
        )
        connection.execute(
            "UPDATE knowledge_results SET published_at = NULL, published_path = NULL "
            "WHERE knowledge_result_id = ?",
            (unpublished_result,),
        )

    stale = library.snapshot()

    assert not stale.current
    assert stale.topics == ()
    assert stale.uncovered_count == 1
    assert library.topic(topic_id)[0] is None


def test_stale_topic_with_two_current_members_remains_visible(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    _, first_result = add_knowledge(
        database_path, "one", (("p1", "core", "仍当前的观点一。"),)
    )
    _, second_result = add_knowledge(
        database_path, "two", (("p2", "core", "仍当前的观点二。"),)
    )
    _, invalidated_result = add_knowledge(
        database_path, "three", (("p3", "core", "已失效的观点。"),)
    )
    _, unpublished_result = add_knowledge(
        database_path, "four", (("p4", "core", "已取消发布的观点。"),)
    )
    library = TopicLibrary(database_path, PlanningIndexer(all_points_topic))
    assert library.refresh().kind is TopicRefreshKind.REFRESHED

    with connect(database_path) as connection:
        connection.execute(
            "UPDATE knowledge_results SET invalidated_at = 'now' "
            "WHERE knowledge_result_id = ?",
            (invalidated_result,),
        )
        connection.execute(
            "UPDATE knowledge_results SET published_at = NULL, published_path = NULL "
            "WHERE knowledge_result_id = ?",
            (unpublished_result,),
        )

    stale = library.snapshot()

    assert not stale.current
    assert {card.knowledge_result_id for card in stale.topics[0].cards} == {
        first_result,
        second_result,
    }
    assert stale.uncovered_count == 0


def test_corrupt_payload_blocks_refresh_but_healthy_old_members_continue(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (("p1", "core", "健康观点一。"), ("p2", "other", "健康观点二。")),
    )
    library = TopicLibrary(database_path, PlanningIndexer(first_two_topic))
    library.refresh()
    _, bad_result = add_knowledge(database_path, "bad", (("p3", "core", "损坏观点。"),))
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER knowledge_result_content_cannot_be_updated")
        connection.execute("UPDATE knowledge_results SET payload_json = '{bad' WHERE knowledge_result_id = ?", (bad_result,))
        old_topics = tuple(tuple(row) for row in connection.execute("SELECT * FROM topics"))

    result = library.refresh()
    snapshot = library.snapshot()

    assert result.kind is TopicRefreshKind.FAILED
    assert snapshot.unreadable_count == 1
    assert [card.statement for card in snapshot.topics[0].cards] == [
        "健康观点一。",
        "健康观点二。",
    ]
    with connect(database_path) as connection:
        assert tuple(tuple(row) for row in connection.execute("SELECT * FROM topics")) == old_topics


def test_empty_knowledge_and_unavailable_indexer_do_not_call_model(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    indexer = PlanningIndexer(first_two_topic, available=False)
    library = TopicLibrary(database_path, indexer)

    assert library.refresh().kind is TopicRefreshKind.EMPTY
    assert library.snapshot().empty_knowledge
    assert indexer.calls == 0
    add_knowledge(database_path, "one", (("p1", "core", "观点。"),))
    assert library.refresh().kind is TopicRefreshKind.UNAVAILABLE
    assert indexer.calls == 0


def test_concurrent_normal_refresh_calls_model_once_and_never_exposes_half_write(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (("p1", "core", "观点一。"), ("p2", "other", "观点二。")),
    )
    indexer = PlanningIndexer(first_two_topic, delay=0.08)
    library = TopicLibrary(database_path, indexer)
    results = []

    threads = [threading.Thread(target=lambda: results.append(library.refresh().kind)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert indexer.calls == 1
    assert set(results) == {TopicRefreshKind.REFRESHED, TopicRefreshKind.CURRENT}
    assert library.snapshot().current


def test_reader_sees_old_complete_index_while_replacement_transaction_is_paused(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (
            ("p1", "core", "旧索引观点一。"),
            ("p2", "other", "旧索引观点二。"),
            ("p3", "other", "新索引观点。"),
        ),
    )
    original_library = TopicLibrary(database_path, PlanningIndexer(first_two_topic))
    assert original_library.refresh().kind is TopicRefreshKind.REFRESHED

    def second_point_topic(points, existing_topics):
        by_id = {point.point_id: point.reference for point in points}
        return TopicPlan(
            (
                TopicDraft(
                    existing_topics[0].topic_id,
                    None,
                    "重组后主题",
                    "收纳重组后的观点。",
                    (by_id["p2"], by_id["p3"]),
                ),
            ),
            (by_id["p1"],),
        )

    replacement_started = threading.Event()
    allow_commit = threading.Event()
    original_replace = topic_library_module._replace_topic_index

    def paused_replace(connection, plan, signature):
        connection.execute("DELETE FROM topic_memberships")
        replacement_started.set()
        assert allow_commit.wait(timeout=2)
        original_replace(connection, plan, signature)

    monkeypatch.setattr(topic_library_module, "_replace_topic_index", paused_replace)
    updated_library = TopicLibrary(
        database_path, PlanningIndexer(second_point_topic)
    )
    results = []
    writer = threading.Thread(
        target=lambda: results.append(updated_library.refresh(force=True).kind)
    )
    writer.start()
    assert replacement_started.wait(timeout=2)

    during = original_library.snapshot()
    assert [card.point_id for card in during.topics[0].cards] == ["p1", "p2"]

    allow_commit.set()
    writer.join(timeout=2)
    assert not writer.is_alive()
    assert results == [TopicRefreshKind.REFRESHED]
    assert [
        card.point_id for card in updated_library.snapshot().topics[0].cards
    ] == ["p2", "p3"]


def test_signature_is_stable_and_excludes_publication_and_source_metadata():
    from knowledge_distiller.topic_indexing import TopicPointInput

    first = TopicPointInput(1, 2, "p1", "core", "S", "A", "T", "M")
    second = TopicPointInput(3, 4, "p2", "other", "S2", "A2", "T2", "M2")

    assert compute_source_signature((first, second)) == compute_source_signature((second, first))
    assert compute_source_signature((first,)) != compute_source_signature((first,), version="topic-input-v2")
    changed = TopicPointInput(1, 2, "p1", "core", "changed", "A", "T", "M")
    assert compute_source_signature((first,)) != compute_source_signature((changed,))
    assert {
        field.name for field in fields(TopicPointInput)
    }.isdisjoint({"published_path", "platform", "author"})


def test_signature_tracks_every_contract_input_field():
    from knowledge_distiller.topic_indexing import TopicPointInput

    original = TopicPointInput(1, 2, "p1", "core", "S", "A", "T", "M")
    changes = (
        {"knowledge_result_id": 9},
        {"source_fact_id": 8},
        {"point_id": "p2"},
        {"role": "other"},
        {"statement": "S2"},
        {"argument": "A2"},
        {"title": "T2"},
        {"summary": "M2"},
    )

    for change in changes:
        assert compute_source_signature((original,)) != compute_source_signature(
            (replace(original, **change),)
        )


def test_eligible_signature_changes_with_currentness_but_not_source_metadata(tmp_path):
    first_database = tmp_path / "first.sqlite3"
    second_database = tmp_path / "second.sqlite3"
    points = (("p1", "core", "稳定观点。"),)
    add_knowledge(
        first_database,
        "same",
        points,
        platform="douyin",
        metadata={"author": {"display_name": "作者甲"}},
        published_path="知识蒸馏器/first.md",
    )
    add_knowledge(
        second_database,
        "same",
        points,
        platform="xiaohongshu",
        metadata={"author": {"display_name": "作者乙"}},
        published_path="知识蒸馏器/second.md",
    )
    assert eligible_signature(first_database) == eligible_signature(second_database)

    original = eligible_signature(first_database)
    new_task, new_result = add_knowledge(
        first_database, "new", (("p2", "core", "新观点。"),)
    )
    assert eligible_signature(first_database) != original
    with connect(first_database) as connection:
        connection.execute(
            "UPDATE knowledge_results SET invalidated_at = 'now' WHERE knowledge_result_id = ?",
            (new_result,),
        )
    assert eligible_signature(first_database) == original
    with connect(first_database) as connection:
        connection.execute(
            "UPDATE knowledge_results SET invalidated_at = NULL, "
            "published_at = NULL, published_path = NULL "
            "WHERE knowledge_result_id = ?",
            (new_result,),
        )
    assert eligible_signature(first_database) == original
    with connect(first_database) as connection:
        connection.execute(
            "UPDATE knowledge_results SET published_at = 'now', published_path = 'new.md' "
            "WHERE knowledge_result_id = ?",
            (new_result,),
        )
        material_id = connection.execute(
            "SELECT material_id FROM tasks WHERE task_id = ?", (new_task,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE materials SET current_knowledge_result_id = NULL "
            "WHERE material_id = ?",
            (material_id,),
        )
    assert eligible_signature(first_database) == original


def test_topic_tables_do_not_change_formal_tables(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    markdown_path = tmp_path / "probe-vault" / "知识蒸馏器" / "one.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("正式 Markdown 内容。\n", encoding="utf-8")
    markdown_before = (
        hashlib.sha256(markdown_path.read_bytes()).hexdigest(),
        markdown_path.stat().st_mtime_ns,
    )
    add_knowledge(
        database_path,
        "one",
        (("p1", "core", "观点一。"), ("p2", "other", "观点二。")),
    )
    with connect(database_path) as connection:
        before = {table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}")) for table in ("materials", "source_facts", "knowledge_results", "tasks")}
    TopicLibrary(database_path, PlanningIndexer(first_two_topic)).refresh()
    with connect(database_path) as connection:
        after = {table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}")) for table in ("materials", "source_facts", "knowledge_results", "tasks")}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert after == before
    assert (
        hashlib.sha256(markdown_path.read_bytes()).hexdigest(),
        markdown_path.stat().st_mtime_ns,
    ) == markdown_before


def test_source_change_during_model_call_rolls_back_without_overwriting_old_index(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (("p1", "core", "旧观点一。"), ("p2", "other", "旧观点二。")),
    )
    stable = PlanningIndexer(first_two_topic)
    library = TopicLibrary(database_path, stable)
    assert library.refresh().kind is TopicRefreshKind.REFRESHED
    with connect(database_path) as connection:
        old_topics = tuple(tuple(row) for row in connection.execute("SELECT * FROM topics"))
        old_memberships = tuple(tuple(row) for row in connection.execute("SELECT * FROM topic_memberships"))
        old_state = tuple(tuple(row) for row in connection.execute("SELECT * FROM topic_index_state"))

    class ChangingIndexer(PlanningIndexer):
        def organize(self, points, existing_topics):
            add_knowledge(database_path, "two", (("p3", "core", "并发新观点。"),))
            return super().organize(points, existing_topics)

    result = TopicLibrary(database_path, ChangingIndexer(first_two_topic)).refresh(force=True)

    assert result.kind is TopicRefreshKind.KNOWLEDGE_CHANGED
    with connect(database_path) as connection:
        assert tuple(tuple(row) for row in connection.execute("SELECT * FROM topics")) == old_topics
        assert tuple(tuple(row) for row in connection.execute("SELECT * FROM topic_memberships")) == old_memberships
        assert tuple(tuple(row) for row in connection.execute("SELECT * FROM topic_index_state")) == old_state


def test_provider_failure_and_mid_transaction_error_preserve_complete_old_index(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.sqlite3"
    add_knowledge(
        database_path,
        "one",
        (("p1", "core", "观点一。"), ("p2", "other", "观点二。")),
    )
    library = TopicLibrary(database_path, PlanningIndexer(first_two_topic))
    library.refresh()
    with connect(database_path) as connection:
        before = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("topics", "topic_memberships", "topic_index_state")
        }

    class FailedIndexer:
        def is_available(self):
            return True

        def organize(self, _points, _existing):
            return TopicIndexing.failed(TopicIndexFailure.RUNTIME_FAILED)

    assert TopicLibrary(database_path, FailedIndexer()).refresh(force=True).kind is TopicRefreshKind.FAILED

    def fail_after_partial_write(connection, _plan, _signature):
        connection.execute("DELETE FROM topic_memberships")
        raise sqlite3.OperationalError("simulated transaction failure")

    import sqlite3
    monkeypatch.setattr(topic_library_module, "_replace_topic_index", fail_after_partial_write)
    assert library.refresh(force=True).kind is TopicRefreshKind.FAILED
    with connect(database_path) as connection:
        after = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("topics", "topic_memberships", "topic_index_state")
        }
    assert after == before
