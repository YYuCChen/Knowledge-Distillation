import json
import sqlite3

import pytest

from knowledge_distiller.database import (
    TaskMaterialConflict,
    attach_task_to_material,
    connect,
    create_task,
    get_task,
    initialize_database,
    list_recent_formal_knowledge,
    list_searchable_formal_knowledge,
    utc_now,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.schema_migrations import CURRENT_BASELINE_CORE_STATEMENTS


def test_database_initializes_primary_records_and_three_topic_tables(tmp_path):
    database_path = tmp_path / "data" / "knowledge.sqlite3"

    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        task_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tasks)")
        }
        all_columns = {
            row[1]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    assert {
        "materials",
        "source_facts",
        "knowledge_results",
        "tasks",
        "topics",
        "topic_memberships",
        "topic_index_state",
    } <= tables
    assert "status" not in task_columns
    assert "media_path" not in all_columns


def test_exact_old_four_table_database_is_preserved_during_migration(tmp_path):
    database_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in CURRENT_BASELINE_CORE_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO materials (
                material_id, platform, platform_item_id, original_url, created_at
            ) VALUES (1, 'douyin', 'old', 'https://example.test/old', 'now')
            """
        )
        connection.execute(
            "INSERT INTO source_facts VALUES (1, 1, '{}', '原文', '[]', NULL, NULL, 'now')"
        )
        connection.execute(
            """
            INSERT INTO knowledge_results
            VALUES (1, 1, '{"title":"old"}', 'now', NULL, NULL, 'now', '知识蒸馏器/old.md')
            """
        )
        connection.execute(
            """
            INSERT INTO tasks
            VALUES (1, 1, 'https://example.test/old', 'now', 'now', NULL, NULL, NULL, NULL)
            """
        )
        connection.execute(
            """
            UPDATE materials
            SET current_source_fact_id = 1, current_knowledge_result_id = 1
            WHERE material_id = 1
            """
        )
        before = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }

    initialize_database(database_path)
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        after = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
        assert connection.execute("SELECT count(*) FROM topics").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM topic_memberships").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM topic_index_state").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert after == before


def test_topic_schema_enforces_names_memberships_positions_and_nonreused_ids(tmp_path):
    database_path = tmp_path / "topics.sqlite3"
    _insert_formal_rows(database_path)
    with connect(database_path) as connection:
        first_id = connection.execute(
            "INSERT INTO topics (normalized_name, name, scope) VALUES ('topic', 'Topic', '范围')"
        ).lastrowid
        for values in (("", "Topic", "范围"), ("name", " ", "范围"), ("scope", "Topic", " ")):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO topics (normalized_name, name, scope) VALUES (?, ?, ?)",
                    values,
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO topics (normalized_name, name, scope) VALUES ('topic', '同名', '范围')"
            )
        connection.execute(
            "INSERT INTO topic_memberships VALUES (?, 1, 'p1', 0)", (first_id,)
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO topic_memberships VALUES (?, 1, ' ', 1)", (first_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO topic_memberships VALUES (?, 1, 'p2', 0)", (first_id,)
            )
        second_id = connection.execute(
            "INSERT INTO topics (normalized_name, name, scope) VALUES ('second', 'Second', '范围')"
        ).lastrowid
        connection.execute(
            "INSERT INTO topic_memberships VALUES (?, 1, 'p1', 0)", (second_id,)
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO topic_memberships VALUES (999, 1, 'p1', 0)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO topic_memberships VALUES (?, 999, 'p1', 1)", (second_id,)
            )
        connection.execute("DELETE FROM topics WHERE topic_id = ?", (first_id,))
        third_id = connection.execute(
            "INSERT INTO topics (normalized_name, name, scope) VALUES ('third', 'Third', '范围')"
        ).lastrowid
    assert third_id > second_id > first_id


def test_task_survives_a_new_database_connection(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")

    task = get_task(database_path, task_id)

    assert task is not None
    assert task["submitted_url"] == "https://v.douyin.com/example/"
    assert task["material_id"] is None
    assert task["last_failure_boundary"] is None
    assert task["waiting_boundary"] is None


def test_current_pointers_cannot_cross_material_or_source_fact_boundaries(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    now = utc_now()

    with connect(database_path) as connection:
        first_material = connection.execute(
            """
            INSERT INTO materials (platform, platform_item_id, original_url, created_at)
            VALUES ('douyin', 'item-1', 'https://v.douyin.com/one/', ?)
            """,
            (now,),
        ).lastrowid
        second_material = connection.execute(
            """
            INSERT INTO materials (platform, platform_item_id, original_url, created_at)
            VALUES ('douyin', 'item-2', 'https://v.douyin.com/two/', ?)
            """,
            (now,),
        ).lastrowid
        first_source_fact = connection.execute(
            """
            INSERT INTO source_facts (
                material_id, metadata_json, content_snapshot,
                uncertainty_json, created_at
            ) VALUES (?, ?, '第一份来源', ?, ?)
            """,
            (first_material, json.dumps({}), json.dumps([]), now),
        ).lastrowid
        second_source_fact = connection.execute(
            """
            INSERT INTO source_facts (
                material_id, metadata_json, content_snapshot,
                uncertainty_json, created_at
            ) VALUES (?, ?, '第二份来源', ?, ?)
            """,
            (second_material, json.dumps({}), json.dumps([]), now),
        ).lastrowid
        knowledge_result = connection.execute(
            """
            INSERT INTO knowledge_results (source_fact_id, payload_json, created_at)
            VALUES (?, ?, ?)
            """,
            (first_source_fact, json.dumps({"title": "结果"}), now),
        ).lastrowid

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE materials SET current_source_fact_id = ? WHERE material_id = ?",
                (second_source_fact, first_material),
            )

        connection.execute(
            "UPDATE materials SET current_source_fact_id = ? WHERE material_id = ?",
            (first_source_fact, first_material),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE materials
                SET current_source_fact_id = ?, current_knowledge_result_id = ?
                WHERE material_id = ?
                """,
                (second_source_fact, knowledge_result, first_material),
            )


def test_identified_task_cannot_be_rebound_to_another_material(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/one/")
    first_identity = ConfirmedMaterialIdentity(
        platform="douyin",
        platform_item_id="work-1",
        original_url="https://v.douyin.com/one/",
        canonical_url="https://www.douyin.com/video/work-1",
    )
    second_identity = ConfirmedMaterialIdentity(
        platform="douyin",
        platform_item_id="work-2",
        original_url="https://v.douyin.com/two/",
        canonical_url="https://www.douyin.com/video/work-2",
    )
    attach_task_to_material(database_path, task_id, first_identity)

    with pytest.raises(TaskMaterialConflict):
        attach_task_to_material(database_path, task_id, second_identity)

    task = get_task(database_path, task_id)
    with connect(database_path) as connection:
        material = connection.execute(
            "SELECT * FROM materials WHERE material_id = ?", (task["material_id"],)
        ).fetchone()
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
    assert material["platform_item_id"] == "work-1"


def _insert_formal_rows(
    database_path,
    *,
    item_id="formal-1",
    published_at=None,
):
    initialize_database(database_path)
    now = utc_now()
    publication_time = published_at or now
    with connect(database_path) as connection:
        material_id = connection.execute(
            """
            INSERT INTO materials (platform, platform_item_id, original_url, created_at)
            VALUES ('douyin', ?, ?, ?)
            """,
            (item_id, f"https://example.test/{item_id}", now),
        ).lastrowid
        source_fact_id = connection.execute(
            """
            INSERT INTO source_facts (
                material_id, metadata_json, content_snapshot,
                uncertainty_json, created_at
            ) VALUES (?, '{}', '正式来源', '[]', ?)
            """,
            (material_id, now),
        ).lastrowid
        knowledge_result_id = connection.execute(
            """
            INSERT INTO knowledge_results (
                source_fact_id, payload_json, created_at,
                published_at, published_path
            ) VALUES (?, '{}', ?, ?, ?)
            """,
            (
                source_fact_id,
                now,
                publication_time,
                f"知识蒸馏器/{item_id}.md",
            ),
        ).lastrowid
        connection.execute(
            """
            UPDATE materials
            SET current_source_fact_id = ?, current_knowledge_result_id = ?
            WHERE material_id = ?
            """,
            (source_fact_id, knowledge_result_id, material_id),
        )
    return material_id, source_fact_id, knowledge_result_id


def test_formal_library_query_reads_current_valid_published_result(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    material_id, source_fact_id, knowledge_result_id = _insert_formal_rows(
        database_path
    )

    rows = list_searchable_formal_knowledge(database_path)

    assert len(rows) == 1
    assert rows[0]["material_id"] == material_id
    assert rows[0]["source_fact_id"] == source_fact_id
    assert rows[0]["knowledge_result_id"] == knowledge_result_id
    assert list_recent_formal_knowledge(database_path)[0][
        "knowledge_result_id"
    ] == knowledge_result_id


@pytest.mark.parametrize(
    "mutation",
    [
        "noncurrent",
        "invalidated",
        "unpublished",
        "missing_path",
        "blank_path",
        "wrong_current_source",
    ],
)
def test_formal_library_query_excludes_ineligible_results(tmp_path, mutation):
    database_path = tmp_path / f"{mutation}.sqlite3"
    material_id, source_fact_id, knowledge_result_id = _insert_formal_rows(
        database_path
    )
    now = utc_now()
    with connect(database_path) as connection:
        if mutation == "noncurrent":
            connection.execute(
                "UPDATE materials SET current_knowledge_result_id = NULL WHERE material_id = ?",
                (material_id,),
            )
        elif mutation == "invalidated":
            connection.execute(
                "UPDATE knowledge_results SET invalidated_at = ? WHERE knowledge_result_id = ?",
                (now, knowledge_result_id),
            )
        elif mutation == "unpublished":
            connection.execute(
                """
                UPDATE knowledge_results
                SET published_at = NULL, published_path = NULL
                WHERE knowledge_result_id = ?
                """,
                (knowledge_result_id,),
            )
        elif mutation == "missing_path":
            connection.execute("DROP TRIGGER knowledge_result_content_cannot_be_updated")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE knowledge_results SET published_path = NULL WHERE knowledge_result_id = ?",
                (knowledge_result_id,),
            )
        elif mutation == "blank_path":
            connection.execute(
                "UPDATE knowledge_results SET published_path = '   ' WHERE knowledge_result_id = ?",
                (knowledge_result_id,),
            )
        elif mutation == "wrong_current_source":
            replacement = connection.execute(
                """
                INSERT INTO source_facts (
                    material_id, metadata_json, content_snapshot,
                    uncertainty_json, created_at
                ) VALUES (?, '{}', '新来源', '[]', ?)
                """,
                (material_id, now),
            ).lastrowid
            connection.execute(
                "UPDATE materials SET current_knowledge_result_id = NULL WHERE material_id = ?",
                (material_id,),
            )
            connection.execute(
                "UPDATE materials SET current_source_fact_id = ? WHERE material_id = ?",
                (replacement, material_id),
            )

    assert list_searchable_formal_knowledge(database_path) == []
    assert list_recent_formal_knowledge(database_path) == []


def test_formal_library_query_rejects_cross_material_source_ownership(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    material_id, source_fact_id, knowledge_result_id = _insert_formal_rows(
        database_path
    )
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER source_facts_cannot_be_updated")
        other_material = connection.execute(
            """
            INSERT INTO materials (platform, platform_item_id, original_url, created_at)
            VALUES ('douyin', 'other', 'https://example.test/other', ?)
            """,
            (utc_now(),),
        ).lastrowid
        connection.execute(
            "UPDATE source_facts SET material_id = ? WHERE source_fact_id = ?",
            (other_material, source_fact_id),
        )

    assert list_searchable_formal_knowledge(database_path) == []
    assert list_recent_formal_knowledge(database_path) == []


def test_recent_formal_library_query_orders_by_publication_then_identity(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    _, _, oldest_id = _insert_formal_rows(
        database_path,
        item_id="oldest",
        published_at="2026-08-18T10:00:00+00:00",
    )
    _, _, tied_first_id = _insert_formal_rows(
        database_path,
        item_id="tied-first",
        published_at="2026-08-19T10:00:00+00:00",
    )
    _, _, tied_second_id = _insert_formal_rows(
        database_path,
        item_id="tied-second",
        published_at="2026-08-19T10:00:00+00:00",
    )

    rows = list_recent_formal_knowledge(database_path)

    assert [row["knowledge_result_id"] for row in rows] == [
        tied_second_id,
        tied_first_id,
        oldest_id,
    ]


def test_formal_library_query_is_read_only(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    _insert_formal_rows(database_path)
    with connect(database_path) as connection:
        before = tuple(
            tuple(row)
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
        )

    list_searchable_formal_knowledge(database_path)
    list_recent_formal_knowledge(database_path)

    with connect(database_path) as connection:
        after = tuple(
            tuple(row)
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
        )
    assert after == before
