import json

import pytest

from knowledge_distiller.database import connect, create_task, initialize_database, utc_now
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    initialize_database(path)
    return path


def create_identified_task(database_path):
    now = utc_now()
    with connect(database_path) as connection:
        material_id = connection.execute(
            """
            INSERT INTO materials (
                platform, platform_item_id, original_url, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("douyin", "stable-item-1", "https://v.douyin.com/example/", now),
        ).lastrowid
        task_id = connection.execute(
            """
            INSERT INTO tasks (
                material_id, submitted_url, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (material_id, "https://v.douyin.com/example/", now, now),
        ).lastrowid
    return int(task_id), int(material_id)


def test_unconfirmed_submission_stops_at_source_fact_production(database_path):
    task_id = create_task(database_path, "https://v.douyin.com/example/")

    assert (
        decide_next_boundary(database_path, task_id)
        is NextBoundary.SOURCE_FACT_PRODUCTION
    )


def test_orchestration_uses_stable_results_to_choose_the_next_boundary(database_path):
    task_id, material_id = create_identified_task(database_path)
    now = utc_now()

    assert (
        decide_next_boundary(database_path, task_id)
        is NextBoundary.SOURCE_FACT_PRODUCTION
    )

    with connect(database_path) as connection:
        source_fact_id = connection.execute(
            """
            INSERT INTO source_facts (
                material_id, metadata_json, content_snapshot,
                uncertainty_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (material_id, json.dumps({}), "可信内容", json.dumps([]), now),
        ).lastrowid
        connection.execute(
            "UPDATE materials SET current_source_fact_id = ? WHERE material_id = ?",
            (source_fact_id, material_id),
        )

    assert (
        decide_next_boundary(database_path, task_id)
        is NextBoundary.KNOWLEDGE_DERIVATION
    )

    with connect(database_path) as connection:
        knowledge_result_id = connection.execute(
            """
            INSERT INTO knowledge_results (
                source_fact_id, payload_json, created_at
            ) VALUES (?, ?, ?)
            """,
            (source_fact_id, json.dumps({"title": "结果"}), now),
        ).lastrowid
        connection.execute(
            """
            UPDATE materials
            SET current_knowledge_result_id = ?
            WHERE material_id = ?
            """,
            (knowledge_result_id, material_id),
        )

    assert (
        decide_next_boundary(database_path, task_id)
        is NextBoundary.OBSIDIAN_PUBLISHING
    )

    with connect(database_path) as connection:
        connection.execute(
            """
            UPDATE knowledge_results
            SET published_at = ?, published_path = ?
            WHERE knowledge_result_id = ?
            """,
            (now, "知识/结果.md", knowledge_result_id),
        )

    assert decide_next_boundary(database_path, task_id) is NextBoundary.COMPLETE
