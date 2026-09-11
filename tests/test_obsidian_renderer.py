import json

import pytest

from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_knowledge_result,
    establish_source_fact,
    initialize_database,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.obsidian_renderer import render_task_knowledge_markdown
from knowledge_distiller.legacy.orchestration import NextBoundary, decide_next_boundary


FIRST_PARAGRAPH = "医院先上报约定采购量，企业中选后按约定供应。"
SECOND_PARAGRAPH = "未达到约定采购量时，部分地区可能扣减相应医保资金。"
THIRD_PARAGRAPH = "达到采购量后，部分节约金额可以用于医护人员绩效奖励。"
SNAPSHOT = "\n\n".join((FIRST_PARAGRAPH, SECOND_PARAGRAPH, THIRD_PARAGRAPH))


def evidence(evidence_id, source_fact_id, evidence_text):
    start = SNAPSHOT.index(evidence_text)
    return {
        "id": evidence_id,
        "source_fact_id": source_fact_id,
        "start": start,
        "end": start + len(evidence_text),
        "evidence_text": evidence_text,
    }


def payload(source_fact_id):
    return {
        "title": "药品集采的采购与激励机制",
        "summary": "医院报量、企业履约，未达量可能扣款，达量后可以形成绩效激励。",
        "core_points": [
            {
                "id": "p1",
                "statement": "医院报量与企业履约构成集采采购链路。",
                "argument": "医院先上报约定采购量，企业中选以后按约定供应。",
                "evidence_ids": ["e1", "e2"],
            },
            {
                "id": "p2",
                "statement": "企业中选后需要按约定供应。",
                "argument": "企业中选是进入按约定供应环节的前提。",
                "evidence_ids": ["e2"],
            },
        ],
        "other_points": [
            {
                "id": "p3",
                "statement": "未达量可能扣款，达量后则可能形成绩效激励。",
                "argument": "来源分别说明了未达到采购量和达到采购量后的处理。",
                "evidence_ids": ["e3"],
            }
        ],
        "evidence_registry": [
            evidence("e1", source_fact_id, "医院先上报约定采购量"),
            evidence("e2", source_fact_id, "企业中选后按约定供应"),
            evidence(
                "e3",
                source_fact_id,
                "未达到约定采购量时，部分地区可能扣减相应医保资金",
            ),
        ],
    }


def task_with_knowledge_result(database_path, *, payload_factory=payload):
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
            "author": {
                "display_name": "来源作者",
                "platform_account_id": "account-1",
            },
            "source_title": "原平台标题",
            "original_description": "原平台描述",
            "published_at": "2026-08-11T03:32:07+00:00",
        },
        SNAPSHOT,
        [],
    )
    knowledge = establish_knowledge_result(
        database_path,
        task_id,
        source.source_fact_id,
        payload_factory(source.source_fact_id),
    )
    return task_id, source.source_fact_id, knowledge.knowledge_result_id


def test_same_knowledge_result_renders_identically_with_frozen_reading_order(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, knowledge_result_id = task_with_knowledge_result(database_path)

    first = render_task_knowledge_markdown(database_path, task_id)
    second = render_task_knowledge_markdown(database_path, task_id)

    assert first == second
    assert first.source_block_count == 3
    assert first.evidence_navigation_count == 4
    markdown = first.markdown
    assert markdown.startswith(
        "---\n"
        'kd_material_platform: "douyin"\n'
        'kd_material_item_id: "stable-work-1"\n'
        f"kd_knowledge_result_id: {knowledge_result_id}\n"
        "---\n"
    )
    assert markdown.index("# 药品集采的采购与激励机制") < markdown.index(
        "> 医院报量、企业履约，未达量可能扣款，达量后可以形成绩效激励。"
    )
    assert markdown.index("## 核心观点") < markdown.index(
        "> [!note]- 医院报量与企业履约构成集采采购链路。"
    )
    assert markdown.index("医院报量与企业履约构成集采采购链路。") < markdown.index(
        "企业中选后需要按约定供应。"
    )
    assert markdown.index("企业中选后需要按约定供应。") < markdown.index(
        "## 更多正式观点"
    )
    assert markdown.index("## 更多正式观点") < markdown.index(
        "未达量可能扣款，达量后则可能形成绩效激励。"
    )
    assert markdown.index("未达量可能扣款，达量后则可能形成绩效激励。") < (
        markdown.index("## 完整原文")
    )


def test_points_arguments_and_all_evidence_relationships_are_preserved(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, _ = task_with_knowledge_result(database_path)

    markdown = render_task_knowledge_markdown(database_path, task_id).markdown

    expected = payload(1)
    for point in expected["core_points"] + expected["other_points"]:
        assert point["statement"] in markdown
        assert point["argument"] in markdown
    assert markdown.count("[[#^source-1|") == 3
    assert markdown.count("[[#^source-2|") == 1
    assert "来源：医院先上报约定采购量" in markdown
    assert markdown.count("来源：企业中选后按约定供应") == 2
    assert "来源：未达到约定采购量时，部分地区可能扣减相应医保资金" in markdown


def test_evidence_navigation_targets_natural_paragraph_containing_exact_span(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, _ = task_with_knowledge_result(database_path)

    markdown = render_task_knowledge_markdown(database_path, task_id).markdown

    assert f"{FIRST_PARAGRAPH} ^source-1" in markdown
    assert f"{SECOND_PARAGRAPH} ^source-2" in markdown
    assert f"{THIRD_PARAGRAPH} ^source-3" in markdown
    assert markdown.index(f"{FIRST_PARAGRAPH} ^source-1") < markdown.index(
        f"{SECOND_PARAGRAPH} ^source-2"
    )
    assert markdown.index(f"{SECOND_PARAGRAPH} ^source-2") < markdown.index(
        f"{THIRD_PARAGRAPH} ^source-3"
    )
    assert "P01" not in markdown
    assert "P02" not in markdown
    source_section = markdown.split("## 完整原文\n\n", 1)[1].split(
        "\n\n## 来源身份信息",
        1,
    )[0]
    restored = "\n\n".join(
        paragraph.rsplit(" ^source-", 1)[0]
        for paragraph in source_section.split("\n\n")
    )
    assert restored == SNAPSHOT


def test_full_source_and_source_identity_are_rendered_after_all_points(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, _ = task_with_knowledge_result(database_path)

    markdown = render_task_knowledge_markdown(database_path, task_id).markdown

    assert markdown.index("## 完整原文") > markdown.index("## 更多正式观点")
    assert markdown.index("## 来源身份信息") > markdown.index(THIRD_PARAGRAPH)
    assert "- 平台：douyin" in markdown
    assert "- 作者：来源作者" in markdown
    assert "- 平台账号：account-1" in markdown
    assert "- 原平台标题：原平台标题" in markdown
    assert "- 原平台描述：原平台描述" in markdown
    assert "- 发布时间：2026-08-11T03:32:07+00:00" in markdown
    assert "- 原链接：<https://www.douyin.com/video/stable-work-1>" in markdown


def test_render_is_read_only_and_publication_facts_remain_empty(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, knowledge_result_id = task_with_knowledge_result(database_path)
    with connect(database_path) as connection:
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }

    render_task_knowledge_markdown(database_path, task_id)

    with connect(database_path) as connection:
        after = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
        result = connection.execute(
            "SELECT * FROM knowledge_results WHERE knowledge_result_id = ?",
            (knowledge_result_id,),
        ).fetchone()
    assert after == before
    assert result["published_at"] is None
    assert result["published_path"] is None
    assert decide_next_boundary(database_path, task_id) is NextBoundary.OBSIDIAN_PUBLISHING


def test_more_points_section_disappears_when_upstream_list_is_empty(tmp_path):
    def core_only_payload(source_fact_id):
        value = payload(source_fact_id)
        value["other_points"] = []
        value["evidence_registry"] = value["evidence_registry"][:2]
        return value

    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, _ = task_with_knowledge_result(
        database_path,
        payload_factory=core_only_payload,
    )

    markdown = render_task_knowledge_markdown(database_path, task_id).markdown

    assert "## 更多正式观点" not in markdown


def test_renderer_rejects_invalid_locator_in_formal_payload(tmp_path):
    def invalid_payload(source_fact_id):
        value = payload(source_fact_id)
        value["evidence_registry"][0]["end"] -= 1
        return value

    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, _ = task_with_knowledge_result(
        database_path,
        payload_factory=invalid_payload,
    )

    with pytest.raises(ValueError, match="faithfully rendered"):
        render_task_knowledge_markdown(database_path, task_id)


def test_cross_paragraph_evidence_navigates_to_its_start_paragraph(tmp_path):
    def crossing_payload(source_fact_id):
        value = payload(source_fact_id)
        evidence_text = FIRST_PARAGRAPH[-4:] + "\n\n" + SECOND_PARAGRAPH[:4]
        start = SNAPSHOT.index(evidence_text)
        value["evidence_registry"][0] = {
            "id": "e1",
            "source_fact_id": source_fact_id,
            "start": start,
            "end": start + len(evidence_text),
            "evidence_text": evidence_text,
        }
        return value

    database_path = tmp_path / "knowledge.sqlite3"
    task_id, _, knowledge_result_id = task_with_knowledge_result(
        database_path,
        payload_factory=crossing_payload,
    )
    with connect(database_path) as connection:
        before_payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM knowledge_results "
                "WHERE knowledge_result_id = ?",
                (knowledge_result_id,),
            ).fetchone()["payload_json"]
        )

    markdown = render_task_knowledge_markdown(database_path, task_id).markdown

    assert "[[#^source-1|" in markdown
    assert f"{FIRST_PARAGRAPH} ^source-1" in markdown
    assert f"{SECOND_PARAGRAPH} ^source-2" in markdown
    assert f"{THIRD_PARAGRAPH} ^source-3" in markdown
    source_section = markdown.split("## 完整原文\n\n", 1)[1].split(
        "\n\n## 来源身份信息",
        1,
    )[0]
    restored = "\n\n".join(
        paragraph.rsplit(" ^source-", 1)[0]
        for paragraph in source_section.split("\n\n")
    )
    assert restored == SNAPSHOT

    with connect(database_path) as connection:
        after_payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM knowledge_results "
                "WHERE knowledge_result_id = ?",
                (knowledge_result_id,),
            ).fetchone()["payload_json"]
        )
    assert after_payload == before_payload
    assert after_payload["evidence_registry"][0] == {
        "id": "e1",
        "source_fact_id": before_payload["evidence_registry"][0]["source_fact_id"],
        "start": SNAPSHOT.index(FIRST_PARAGRAPH[-4:] + "\n\n" + SECOND_PARAGRAPH[:4]),
        "end": SNAPSHOT.index(FIRST_PARAGRAPH[-4:] + "\n\n" + SECOND_PARAGRAPH[:4])
        + len(FIRST_PARAGRAPH[-4:] + "\n\n" + SECOND_PARAGRAPH[:4]),
        "evidence_text": FIRST_PARAGRAPH[-4:] + "\n\n" + SECOND_PARAGRAPH[:4],
    }


def test_task_without_knowledge_result_cannot_render(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    initialize_database(database_path)
    task_id = create_task(database_path, "https://v.douyin.com/example/")

    with pytest.raises(ValueError, match="no current publishable"):
        render_task_knowledge_markdown(database_path, task_id)
