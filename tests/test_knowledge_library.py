import sqlite3

import pytest

from knowledge_distiller import knowledge_library as library_module
from knowledge_distiller.database import (
    attach_task_to_material,
    connect,
    create_task,
    establish_knowledge_result,
    establish_source_fact,
    initialize_database,
    record_knowledge_result_published,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity
from knowledge_distiller.knowledge_library import (
    KnowledgeLibraryError,
    normalize_search_text,
    read_all_formal_knowledge,
    read_recent_formal_knowledge,
    search_formal_points,
)


SNAPSHOT = "这是用于检验正式知识检索的可靠来源原文。"


def add_formal_knowledge(
    database_path,
    item_id,
    *,
    title="本地知识检索",
    summary="正式观点可以从历史知识中重新找到。",
    core_points=(
        ("p1", "知识检索让正式观点重新进入视野。", "本地搜索保留来源边界。"),
    ),
    other_points=(),
    author="来源作者",
    account="account-1",
    description="原平台描述",
    platform="douyin",
):
    initialize_database(database_path)
    task_id = create_task(database_path, f"https://example.test/{item_id}")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            platform,
            item_id,
            f"https://example.test/{item_id}",
            f"https://example.test/{item_id}",
        ),
    )
    source = establish_source_fact(
        database_path,
        task_id,
        {
            "author": {
                "display_name": author,
                "platform_account_id": account,
            },
            "original_description": description,
        },
        SNAPSHOT,
        [],
    )
    all_points = (*core_points, *other_points)
    payload = {
        "title": title,
        "summary": summary,
        "core_points": [
            {
                "id": point_id,
                "statement": statement,
                "argument": argument,
                "evidence_ids": ["e1"],
            }
            for point_id, statement, argument in core_points
        ],
        "other_points": [
            {
                "id": point_id,
                "statement": statement,
                "argument": argument,
                "evidence_ids": ["e1"],
            }
            for point_id, statement, argument in other_points
        ],
        "evidence_registry": [
            {
                "id": "e1",
                "source_fact_id": source.source_fact_id,
                "start": 0,
                "end": len(SNAPSHOT),
                "evidence_text": SNAPSHOT,
            }
        ],
    }
    assert all_points
    knowledge = establish_knowledge_result(
        database_path,
        task_id,
        source.source_fact_id,
        payload,
    )
    record_knowledge_result_published(
        database_path,
        task_id,
        knowledge.knowledge_result_id,
        f"知识蒸馏器/{item_id}.md",
    )
    return task_id, source.source_fact_id, knowledge.knowledge_result_id


@pytest.mark.parametrize(
    "query",
    [
        "知识",
        "正式观点重新进入",
        "KNOWLEDGE",
        "2026",
        "AI2026",
        "ＡＩ２０２６",
    ],
)
def test_chinese_english_numeric_mixed_and_nfkc_queries(tmp_path, query):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "mixed",
        core_points=(("p1", "Knowledge 知识在 AI2026 中重新进入视野。", "正式观点重新进入。"),),
    )

    results = search_formal_points(database_path, query).results

    assert [(card.knowledge_result_id, card.point_id) for card in results] == [(1, "p1")]


def test_normalization_casefolds_and_collapses_whitespace():
    assert normalize_search_text("  ＫＮＯＷＬＥＤＧＥ\n\t检索  ") == "knowledge 检索"


def test_whitespace_terms_use_and_semantics_with_context_assistance(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "and",
        core_points=(("p1", "药品集采需要医院先报量。", "企业随后竞价。"),),
        author="法律作者",
    )

    assert search_formal_points(database_path, "集采 法律作者").results
    assert search_formal_points(database_path, "集采 不存在").results == ()


@pytest.mark.parametrize("role", ["core", "other"])
def test_context_assisted_and_terms_apply_to_core_and_other_points(tmp_path, role):
    database_path = tmp_path / f"{role}.sqlite3"
    points = ((f"{role}-point", "集采需要先报量。", "企业随后竞价。"),)
    add_formal_knowledge(
        database_path,
        role,
        core_points=points if role == "core" else (("core", "无关核心。", "无关论证。"),),
        other_points=points if role == "other" else (),
        author="法律作者",
    )

    result = search_formal_points(database_path, "集采 法律作者").results

    assert len(result) == 1
    assert result[0].role == role


def test_statement_and_argument_can_each_make_point_relevant(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "fields",
        core_points=(
            ("statement", "报量是采购的起点。", "先确认需求。"),
            ("argument", "企业进入履约环节。", "中选之后必须按约定供应。"),
        ),
    )

    assert [card.point_id for card in search_formal_points(database_path, "报量").results] == [
        "statement"
    ]
    assert [card.point_id for card in search_formal_points(database_path, "约定供应").results] == [
        "argument"
    ]


@pytest.mark.parametrize(
    "query",
    ["本地知识检索", "历史知识", "来源作者", "account-1", "原平台描述"],
)
def test_context_only_matches_never_invent_a_representative_point(tmp_path, query):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "context-only",
        core_points=(("p1", "观点正文完全不包含查询词。", "论证也没有。"),),
    )

    assert search_formal_points(database_path, query).results == ()


def test_core_and_other_points_have_equal_search_eligibility(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "roles",
        core_points=(("core", "核心观点包含采购报量。", "核心论证。"),),
        other_points=(("other", "其他观点包含奖励资金。", "其他论证。"),),
    )

    assert search_formal_points(database_path, "采购报量").results[0].role == "core"
    assert search_formal_points(database_path, "奖励资金").results[0].role == "other"


def test_one_point_matching_multiple_fields_is_not_duplicated(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "dedupe",
        title="集采知识",
        summary="集采总括",
        core_points=(("p1", "集采观点。", "集采论证。"),),
        description="集采来源",
    )

    results = search_formal_points(database_path, "集采").results

    assert len(results) == 1


def test_multiple_genuinely_matching_points_in_one_result_are_kept(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "multi",
        core_points=(
            ("p1", "第一条集采观点。", "第一条论证。"),
            ("p2", "第二条集采观点。", "第二条论证。"),
        ),
    )

    assert {card.point_id for card in search_formal_points(database_path, "集采").results} == {
        "p1",
        "p2",
    }


def test_relevance_outranks_role_but_core_breaks_an_exact_tie_across_results(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "older-core",
        core_points=(("core", "普通检索相关观点。", "完整短句只在论证中。"),),
    )
    add_formal_knowledge(
        database_path,
        "newer-other",
        core_points=(("unrelated", "不相关观点。", "也不相关。"),),
        other_points=(("other", "完整短句。", "普通论证。"),),
    )

    stronger = search_formal_points(database_path, "完整短句").results
    assert [card.point_id for card in stronger] == ["other", "core"]

    add_formal_knowledge(
        database_path,
        "older-core-tie",
        core_points=(("core-tie", "相同相关词。", "论证。"),),
    )
    add_formal_knowledge(
        database_path,
        "newer-other-tie",
        core_points=(("unrelated-tie", "不命中。", "也不命中。"),),
        other_points=(("other-tie", "相同相关词。", "论证。"),),
    )
    tied = search_formal_points(database_path, "相同相关词").results
    assert tied[0].role == "core"
    assert tied[0].point_id == "core-tie"


def test_order_is_deterministic_and_limit_is_capped_at_fifty(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    for index in range(55):
        add_formal_knowledge(
            database_path,
            f"limit-{index}",
            core_points=((f"p{index}", "共同检索词。", "共同论证。"),),
        )

    first = search_formal_points(database_path, "共同检索词", limit=500).results
    second = search_formal_points(database_path, "共同检索词", limit=500).results

    assert first == second
    assert len(first) == 50


@pytest.mark.parametrize("query", ["", " \n\t "])
def test_empty_query_does_not_search(tmp_path, monkeypatch, query):
    def unexpected(_database_path):
        raise AssertionError("empty query must not touch the library")

    monkeypatch.setattr(library_module, "list_searchable_formal_knowledge", unexpected)
    assert search_formal_points(tmp_path / "missing.sqlite3", query).results == ()


def test_no_result_and_special_characters_are_literal_and_safe(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(
        database_path,
        "special",
        core_points=(("p1", "C++ 的 100%_coverage 不是 SQL。", "含有 ' \" . + # ?。"),),
    )

    assert search_formal_points(database_path, "没有这个词").results == ()
    for query in ("%_", "'", '"', ".", "+", "#", "?"):
        assert search_formal_points(database_path, query).results


def test_corrupt_formal_payload_is_counted_without_hiding_healthy_results(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(database_path, "good")
    add_formal_knowledge(database_path, "bad")
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER knowledge_result_content_cannot_be_updated")
        connection.execute(
            "UPDATE knowledge_results SET payload_json = '{bad json' WHERE knowledge_result_id = 2"
        )

    result = search_formal_points(database_path, "知识")

    assert len(result.results) == 1
    assert result.unreadable_count == 1


def test_invalid_metadata_is_counted_as_one_unreadable_formal_result(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(database_path, "good")
    _, source_fact_id, _ = add_formal_knowledge(database_path, "bad-metadata")
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER source_facts_cannot_be_updated")
        connection.execute(
            "UPDATE source_facts SET metadata_json = '[]' WHERE source_fact_id = ?",
            (source_fact_id,),
        )

    result = search_formal_points(database_path, "知识")

    assert len(result.results) == 1
    assert result.unreadable_count == 1


def test_whole_library_query_failure_is_explicit(tmp_path, monkeypatch):
    def fail_query(_database_path):
        raise sqlite3.OperationalError("simulated whole-library failure")

    monkeypatch.setattr(library_module, "list_searchable_formal_knowledge", fail_query)

    with pytest.raises(KnowledgeLibraryError):
        search_formal_points(tmp_path / "knowledge.sqlite3", "知识")


def test_search_does_not_call_llm_source_adapters_or_orchestration(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(database_path, "read-only")
    with connect(database_path) as connection:
        before = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }

    def unexpected(*_args, **_kwargs):
        raise AssertionError("search crossed a production boundary")

    monkeypatch.setattr("knowledge_distiller.legacy.orchestration.decide_next_boundary", unexpected)
    monkeypatch.setattr("knowledge_distiller.knowledge_derivation.build_knowledge_deriver", unexpected)
    monkeypatch.setattr("knowledge_distiller.legacy.candidate_a.build_candidate_a_adapters", unexpected)

    assert search_formal_points(database_path, "知识").results
    with connect(database_path) as connection:
        after = {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in ("materials", "source_facts", "knowledge_results", "tasks")
        }
    assert after == before


def test_recent_projection_keeps_result_identity_and_ordered_core_points(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    _, _, knowledge_result_id = add_formal_knowledge(
        database_path,
        "recent",
        title="高浓度知识标题",
        summary="一句话总括。",
        core_points=(
            ("p1", "第一条核心观点。", "第一条完整论证。"),
            ("p2", "第二条核心观点。", "第二条完整论证。"),
        ),
        other_points=(("p3", "其他观点。", "其他完整论证。"),),
        author="来源作者",
        platform="douyin",
    )

    result = read_recent_formal_knowledge(database_path)

    assert result.unreadable_count == 0
    assert len(result.records) == 1
    record = result.records[0]
    assert record.knowledge_result_id == knowledge_result_id
    assert record.title == "高浓度知识标题"
    assert record.summary == "一句话总括。"
    assert record.core_point_statements == (
        "第一条核心观点。",
        "第二条核心观点。",
    )
    assert record.source_label == "来源作者"
    assert record.platform == "douyin"
    assert record.published_at
    assert record.published_path == "知识蒸馏器/recent.md"


def test_all_formal_knowledge_keeps_publication_order_without_recent_limit(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    result_ids = [
        add_formal_knowledge(database_path, f"browse-{index}")[2]
        for index in range(8)
    ]

    result = read_all_formal_knowledge(database_path)

    assert [record.knowledge_result_id for record in result.records] == list(
        reversed(result_ids)
    )
    assert len(result.records) == 8


def test_recent_bad_record_is_excluded_without_consuming_visible_limit(
    tmp_path, caplog
):
    database_path = tmp_path / "knowledge.sqlite3"
    result_ids = [
        add_formal_knowledge(database_path, f"recent-{index}")[2]
        for index in range(8)
    ]
    bad_result_id = result_ids[-1]
    corrupt_payload = "{private bad payload"
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER knowledge_result_content_cannot_be_updated")
        connection.execute(
            "UPDATE knowledge_results SET payload_json = ? WHERE knowledge_result_id = ?",
            (corrupt_payload, bad_result_id),
        )

    result = read_recent_formal_knowledge(database_path, limit=50)

    assert [record.knowledge_result_id for record in result.records] == list(
        reversed(result_ids[1:-1])
    )
    assert len(result.records) == 6
    assert result.unreadable_count == 1
    assert corrupt_payload not in caplog.text


def test_recent_invalid_source_metadata_is_locally_excluded(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    add_formal_knowledge(database_path, "healthy")
    _, source_fact_id, _ = add_formal_knowledge(database_path, "bad-metadata")
    with connect(database_path) as connection:
        connection.execute("DROP TRIGGER source_facts_cannot_be_updated")
        connection.execute(
            "UPDATE source_facts SET metadata_json = '[]' WHERE source_fact_id = ?",
            (source_fact_id,),
        )

    result = read_recent_formal_knowledge(database_path)

    assert [record.title for record in result.records] == ["本地知识检索"]
    assert result.unreadable_count == 1


def test_recent_whole_query_failure_is_explicit(tmp_path, monkeypatch):
    def fail_query(_database_path):
        raise sqlite3.OperationalError("simulated recent-query failure")

    monkeypatch.setattr(library_module, "list_recent_formal_knowledge", fail_query)

    with pytest.raises(KnowledgeLibraryError):
        read_recent_formal_knowledge(tmp_path / "knowledge.sqlite3")
