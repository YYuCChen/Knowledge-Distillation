import json
import os
import sqlite3

import pytest

import knowledge_distiller.legacy.obsidian_publisher as publisher_module
from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_knowledge_result,
    establish_source_fact,
    initialize_database,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.legacy.obsidian_publisher import (
    PublicationKind,
    publication_relative_path,
    publish_task_knowledge,
)
from knowledge_distiller.obsidian_renderer import render_task_knowledge_markdown
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary


SNAPSHOT = "医院先上报约定采购量。\n\n企业中选后按约定供应。"
TITLE = "药品集采：采购/供应机制"


def task_with_knowledge_result(database_path):
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            "douyin",
            "stable-work-1",
            "https://v.douyin.com/example/",
            "https://www.douyin.com/video/stable-work-1",
        ),
    )
    source = establish_source_fact(
        database_path,
        task_id,
        {
            "platform": "douyin",
            "platform_item_id": "stable-work-1",
            "author": {"display_name": "来源作者"},
        },
        SNAPSHOT,
        [],
    )
    evidence_text = "医院先上报约定采购量"
    knowledge = establish_knowledge_result(
        database_path,
        task_id,
        source.source_fact_id,
        {
            "title": TITLE,
            "summary": "医院报量，企业履约。",
            "core_points": [
                {
                    "id": "p1",
                    "statement": "医院报量后由中选企业履约。",
                    "argument": "采购链路从医院报量开始，再由中选企业按约定供应。",
                    "evidence_ids": ["e1"],
                }
            ],
            "other_points": [],
            "evidence_registry": [
                {
                    "id": "e1",
                    "source_fact_id": source.source_fact_id,
                    "start": SNAPSHOT.index(evidence_text),
                    "end": SNAPSHOT.index(evidence_text) + len(evidence_text),
                    "evidence_text": evidence_text,
                }
            ],
        },
    )
    return task_id, knowledge.knowledge_result_id


def publication_row(database_path, knowledge_result_id):
    with connect(database_path) as connection:
        return connection.execute(
            """
            SELECT published_at, published_path
            FROM knowledge_results
            WHERE knowledge_result_id = ?
            """,
            (knowledge_result_id,),
        ).fetchone()


def test_first_publication_uses_stable_relative_path_and_records_success(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    task_id, knowledge_result_id = task_with_knowledge_result(database_path)
    expected = render_task_knowledge_markdown(database_path, task_id).markdown

    result = publish_task_knowledge(database_path, task_id, vault_root)

    expected_path = publication_relative_path(knowledge_result_id, TITLE)
    assert result.kind is PublicationKind.PUBLISHED
    assert result.relative_path == expected_path.as_posix()
    assert not expected_path.is_absolute()
    assert f"kr-{knowledge_result_id}" in expected_path.name
    assert "/" not in expected_path.name
    assert (vault_root / expected_path).read_text(encoding="utf-8") == expected
    row = publication_row(database_path, knowledge_result_id)
    assert row["published_at"] is not None
    assert row["published_path"] == expected_path.as_posix()
    assert decide_next_boundary(database_path, task_id) is NextBoundary.COMPLETE
    assert not list((vault_root / expected_path.parent).glob("*.tmp"))


def test_complete_closed_temporary_file_precedes_no_clobber_placement(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    task_id, _ = task_with_knowledge_result(database_path)
    expected = render_task_knowledge_markdown(database_path, task_id).markdown
    observed = []
    real_link = os.link

    def inspect_then_link(source, target):
        source_path = publisher_module.Path(source)
        target_path = publisher_module.Path(target)
        assert source_path.read_text(encoding="utf-8") == expected
        assert not target_path.exists()
        observed.append(source_path)
        real_link(source, target)

    monkeypatch.setattr(publisher_module.os, "link", inspect_then_link)

    result = publish_task_knowledge(database_path, task_id, vault_root)

    assert result.kind is PublicationKind.PUBLISHED
    assert len(observed) == 1
    assert not observed[0].exists()


def test_sqlite_failure_does_not_claim_success_and_next_call_recovers(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    task_id, knowledge_result_id = task_with_knowledge_result(database_path)
    expected = render_task_knowledge_markdown(database_path, task_id).markdown
    real_recorder = publisher_module.record_knowledge_result_published

    def fail_recording(*args, **kwargs):
        raise sqlite3.OperationalError("simulated SQLite failure")

    monkeypatch.setattr(
        publisher_module,
        "record_knowledge_result_published",
        fail_recording,
    )
    failed = publish_task_knowledge(database_path, task_id, vault_root)
    target = vault_root / publication_relative_path(knowledge_result_id, TITLE)

    assert failed.kind is PublicationKind.FAILED
    assert target.read_text(encoding="utf-8") == expected
    row = publication_row(database_path, knowledge_result_id)
    assert row["published_at"] is None
    assert row["published_path"] is None

    before = target.stat().st_mtime_ns
    monkeypatch.setattr(
        publisher_module,
        "record_knowledge_result_published",
        real_recorder,
    )
    recovered = publish_task_knowledge(database_path, task_id, vault_root)

    assert recovered.kind is PublicationKind.RECOVERED
    assert target.stat().st_mtime_ns == before
    row = publication_row(database_path, knowledge_result_id)
    assert row["published_at"] is not None
    assert row["published_path"] == recovered.relative_path


@pytest.mark.parametrize(
    "existing_content",
    [
        "# 没有机器身份的已有笔记\n",
        (
            "---\n"
            'kd_material_platform: "douyin"\n'
            'kd_material_item_id: "another-work"\n'
            "kd_knowledge_result_id: 1\n"
            "---\n\n# 其他笔记\n"
        ),
    ],
)
def test_missing_or_mismatched_identity_is_a_conflict_without_overwrite(
    tmp_path,
    existing_content,
):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    task_id, knowledge_result_id = task_with_knowledge_result(database_path)
    target = vault_root / publication_relative_path(knowledge_result_id, TITLE)
    target.parent.mkdir(parents=True)
    target.write_text(existing_content, encoding="utf-8")

    result = publish_task_knowledge(database_path, task_id, vault_root)

    assert result.kind is PublicationKind.CONFLICT
    assert target.read_text(encoding="utf-8") == existing_content
    row = publication_row(database_path, knowledge_result_id)
    assert row["published_at"] is None
    assert row["published_path"] is None


def test_matching_identity_with_different_content_is_not_overwritten(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    task_id, knowledge_result_id = task_with_knowledge_result(database_path)
    target = vault_root / publication_relative_path(knowledge_result_id, TITLE)
    target.parent.mkdir(parents=True)
    changed = render_task_knowledge_markdown(database_path, task_id).markdown + "\n修改"
    target.write_text(changed, encoding="utf-8")

    result = publish_task_knowledge(database_path, task_id, vault_root)

    assert result.kind is PublicationKind.CONFLICT
    assert target.read_text(encoding="utf-8") == changed
    row = publication_row(database_path, knowledge_result_id)
    assert row["published_at"] is None
    assert row["published_path"] is None


def test_already_published_result_is_not_checked_or_republished(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    task_id, knowledge_result_id = task_with_knowledge_result(database_path)
    first = publish_task_knowledge(database_path, task_id, vault_root)
    target = vault_root / first.relative_path
    target.unlink()

    second = publish_task_knowledge(database_path, task_id, vault_root)

    assert second.kind is PublicationKind.ALREADY_PUBLISHED
    assert second.relative_path == first.relative_path
    assert not target.exists()
    row = publication_row(database_path, knowledge_result_id)
    assert row["published_at"] is not None
    assert row["published_path"] == first.relative_path
