import hashlib

import pytest

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
from knowledge_distiller.knowledge_library import FormalPointCard
from knowledge_distiller.topic_indexing import (
    TopicDraft,
    TopicIndexing,
    TopicPlan,
)
from knowledge_distiller.topic_library import (
    TopicCard,
    TopicLibrary,
    TopicLibraryError,
    TopicLibrarySnapshot,
    TopicPointProjection,
    TopicRefreshKind,
    TopicRefreshResult,
)
from knowledge_distiller.legacy.web import create_app


FORMAL_TABLES = (
    "tasks",
    "materials",
    "source_facts",
    "knowledge_results",
    "topics",
    "topic_memberships",
    "topic_index_state",
)


class UnavailableIndexer:
    def is_available(self):
        return False

    def organize(self, points, existing_topics):
        raise AssertionError("GET must not organize topics")


class FixedTopicIndexer:
    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def organize(self, points, existing_topics):
        self.calls += 1
        return TopicIndexing.succeeded(
            TopicPlan(
                (
                    TopicDraft(
                        None,
                        "topic-v2",
                        "同一知识的多个观点",
                        "验证观点级 membership、摘要投影与只读浏览。",
                        tuple(point.reference for point in points),
                    ),
                ),
                (),
            )
        )


class FakeTopicLibrary:
    def __init__(self, snapshot, *, available=True, refresh_kind=TopicRefreshKind.REFRESHED):
        self.current_snapshot = snapshot
        self.available = available
        self.refresh_kind = refresh_kind
        self.refresh_calls = []

    def indexer_available(self):
        return self.available

    def snapshot(self):
        return self.current_snapshot

    def topic(self, topic_id):
        topic = next((item for item in self.current_snapshot.topics if item.topic_id == topic_id), None)
        return topic, self.current_snapshot

    def refresh(self, *, force=False):
        self.refresh_calls.append(force)
        return TopicRefreshResult(self.refresh_kind)


def point(
    statement,
    *,
    knowledge_result_id=91,
    point_id="p1",
    title="正式知识",
    role="core",
    source_label="本地作者",
    platform="douyin",
    published_path="知识蒸馏器/missing.md",
):
    return FormalPointCard(
        knowledge_result_id,
        point_id,
        role,
        statement,
        title,
        source_label,
        platform,
        published_path,
    )


def topic_card(topic_id, name, scope, cards, *, summaries=None):
    summaries = summaries or tuple(
        f"{card.knowledge_title} 的一句话总括。" for card in cards
    )
    return TopicCard(
        topic_id,
        name,
        scope,
        tuple(
            TopicPointProjection(card, summary)
            for card, summary in zip(cards, summaries, strict=True)
        ),
    )


def snapshot(*, topics=(), has_index=False, current=False, empty=False, unreadable=0, uncovered=0):
    return TopicLibrarySnapshot(tuple(topics), has_index, current, empty, unreadable, uncovered)


def app_with_library(tmp_path, library, *, vault_root=None):
    app = create_app(
        tmp_path / "knowledge.sqlite3",
        topic_indexer=UnavailableIndexer(),
        obsidian_vault_root=vault_root,
    )
    app.config.update(TESTING=True, TOPIC_LIBRARY=library)
    return app


def add_two_point_formal_knowledge(database_path, published_path):
    initialize_database(database_path)
    task_id = create_task(database_path, "https://example.test/topic-v2")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            "douyin",
            "topic-v2",
            "https://example.test/topic-v2",
            "https://example.test/topic-v2",
        ),
    )
    snapshot_text = "同一份正式来源同时支持核心观点与其他观点。"
    source = establish_source_fact(
        database_path,
        task_id,
        {"author": {"display_name": "来源作者"}},
        snapshot_text,
        [],
    )
    evidence = {
        "id": "e1",
        "source_fact_id": source.source_fact_id,
        "start": 0,
        "end": len(snapshot_text),
        "evidence_text": snapshot_text,
    }
    knowledge = establish_knowledge_result(
        database_path,
        task_id,
        source.source_fact_id,
        {
            "title": "同一知识的观点级导航",
            "summary": "一份知识可以向同一主题贡献多条不同的正式观点。",
            "core_points": [
                {
                    "id": "core-point",
                    "statement": "核心观点必须按 membership 顺序独立展示。",
                    "argument": "核心观点的完整论证。",
                    "evidence_ids": ["e1"],
                }
            ],
            "other_points": [
                {
                    "id": "other-point",
                    "statement": "其他观点具有相同的主题浏览资格。",
                    "argument": "其他观点的完整论证。",
                    "evidence_ids": ["e1"],
                }
            ],
            "evidence_registry": [evidence],
        },
    )
    record_knowledge_result_published(
        database_path,
        task_id,
        knowledge.knowledge_result_id,
        published_path,
    )


def database_snapshot(database_path):
    with connect(database_path) as connection:
        return {
            table: tuple(
                tuple(row) for row in connection.execute(f"SELECT * FROM {table}")
            )
            for table in FORMAL_TABLES
        }


def test_absent_index_auto_refreshes_once_on_page_load_but_get_does_not_refresh(tmp_path):
    library = FakeTopicLibrary(snapshot(), available=True)
    app = app_with_library(tmp_path, library)

    response = app.test_client().get("/knowledge")

    assert response.status_code == 200
    assert "主题还未整理" in response.text
    assert "正在整理主题……" in response.text
    assert response.text.count("fetch(") == 1
    assert response.text.count('<form action="/knowledge/topics/refresh" method="post" data-auto-topic-refresh') == 1
    assert library.refresh_calls == []


@pytest.mark.parametrize(
    ("current_snapshot", "available", "expected"),
    [
        (snapshot(empty=True), True, "还没有可浏览的正式知识"),
        (snapshot(), False, "主题整理当前不可用"),
        (
            snapshot(has_index=True, current=True, uncovered=2),
            True,
            "当前知识尚未形成具有足够导航价值的主题",
        ),
        (snapshot(has_index=True, current=False, uncovered=3), False, "3 个有效观点"),
    ],
)
def test_topic_list_explains_legal_empty_unavailable_unassigned_and_stale_states(
    tmp_path, current_snapshot, available, expected
):
    response = app_with_library(
        tmp_path, FakeTopicLibrary(current_snapshot, available=available)
    ).test_client().get("/knowledge")

    assert response.status_code == 200
    assert expected in response.text
    assert "fetch(" not in response.text
    assert response.text.count('class="knowledge-search library-search"') == 1


def test_current_topics_distinguish_unassigned_points_from_legal_empty_index(tmp_path):
    topic = topic_card(
        8,
        "可浏览主题",
        "收纳已形成共同导航价值的观点。",
        (point("已归类观点一"), point("已归类观点二", point_id="p2")),
    )
    current = snapshot(
        topics=(topic,), has_index=True, current=True, uncovered=2
    )

    response = app_with_library(
        tmp_path, FakeTopicLibrary(current)
    ).test_client().get("/knowledge")

    assert "2 个有效观点暂未归入主题" in response.text
    assert "尚未形成具有足够导航价值的主题" not in response.text


def test_topic_list_hides_single_member_stale_topic(tmp_path):
    topic = topic_card(
        11,
        "不应显示的过期主题",
        "旧索引交集后只剩一个观点。",
        (point("唯一仍有效观点"),),
    )
    stale = snapshot(topics=(topic,), has_index=True, current=False, uncovered=1)

    response = app_with_library(
        tmp_path, FakeTopicLibrary(stale, available=False)
    ).test_client().get("/knowledge")

    assert response.status_code == 200
    assert "主题列表尚未覆盖最新知识" in response.text
    assert "不应显示的过期主题" not in response.text
    assert "唯一仍有效观点" not in response.text


def test_search_results_offer_a_return_to_topic_browsing(tmp_path):
    response = app_with_library(
        tmp_path, FakeTopicLibrary(snapshot())
    ).test_client().get("/knowledge", query_string={"q": "不存在"})

    assert response.status_code == 200
    assert '<a href="/knowledge">主题</a>' in response.text
    assert response.text.count('class="knowledge-search library-search"') == 1


def test_unknown_topic_notice_does_not_suppress_needed_auto_refresh(tmp_path):
    response = app_with_library(
        tmp_path, FakeTopicLibrary(snapshot(), available=True)
    ).test_client().get("/knowledge", query_string={"topic_notice": "unknown"})

    assert response.text.count("fetch(") == 1


def test_failed_notice_prevents_a_second_automatic_refresh_and_search_remains(tmp_path):
    library = FakeTopicLibrary(snapshot(), available=True)

    response = app_with_library(tmp_path, library).test_client().get(
        "/knowledge", query_string={"topic_notice": "failed"}
    )

    assert "这次主题整理没有完成" in response.text
    assert "fetch(" not in response.text
    assert 'name="q"' in response.text
    assert library.refresh_calls == []


def test_refresh_route_supports_normal_and_force_refresh(tmp_path):
    library = FakeTopicLibrary(snapshot(), available=True)
    client = app_with_library(tmp_path, library).test_client()

    normal = client.post("/knowledge/topics/refresh")
    forced = client.post("/knowledge/topics/refresh", data={"force": "true"})

    assert normal.status_code == forced.status_code == 303
    assert normal.headers["Location"].endswith("topic_notice=refreshed")
    assert library.refresh_calls == [False, True]


def test_topic_detail_uses_ordered_shared_cards_escapes_content_and_hides_internals(tmp_path):
    cards = (
        point('<script>first</script>', point_id="raw-provider-model-signature"),
        point(
            "第二个观点",
            point_id="p2",
            title="第二份知识",
            role="other",
        ),
    )
    topic = topic_card(
        7,
        '<img src=x onerror="bad">',
        "范围 <script>bad</script>",
        cards,
        summaries=("第一份知识的严格总括。", "第二份知识的严格总括。"),
    )
    current = snapshot(topics=(topic,), has_index=True, current=False, unreadable=1, uncovered=2)

    response = app_with_library(tmp_path, FakeTopicLibrary(current)).test_client().get(
        "/knowledge/topics/7"
    )

    assert response.status_code == 200
    assert response.text.index("&lt;script&gt;first") < response.text.index("第二个观点")
    assert "&lt;img src=x onerror=&#34;bad&#34;&gt;" in response.text
    assert "尚未覆盖最新知识" in response.text
    assert "损坏或无法读取" in response.text
    assert response.text.count("<details class=\"topic-point\">") == 2
    assert response.text.count("<summary class=\"topic-point-summary\">") == 2
    assert "见《正式知识》" in response.text
    assert "见《第二份知识》" in response.text
    assert "第一份知识的严格总括。" in response.text
    assert "第二份知识的严格总括。" in response.text
    assert "记录的 Obsidian 文件当前不可用" not in response.text
    assert "obsidian://" not in response.text
    assert "核心观点" not in response.text
    assert "其他观点" not in response.text
    assert "raw-provider-model-signature" not in response.text
    assert "返回主题列表" in response.text
    assert 'id="topic-search"' not in response.text
    assert response.text.count('class="knowledge-search library-search"') == 1


def test_topic_detail_links_existing_point_card_to_obsidian(tmp_path):
    vault_root = tmp_path / "vault"
    target = vault_root / "知识蒸馏器" / "topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntitle: topic\n---\n", encoding="utf-8")
    cards = (
        point("可下钻观点一", published_path="知识蒸馏器/topic.md"),
        point(
            "可下钻观点二",
            point_id="p2",
            published_path="知识蒸馏器/topic.md",
        ),
    )
    topic = topic_card(9, "可下钻主题", "收纳可进入本地文件的观点。", cards)
    current = snapshot(topics=(topic,), has_index=True, current=True)

    response = app_with_library(
        tmp_path, FakeTopicLibrary(current), vault_root=vault_root
    ).test_client().get("/knowledge/topics/9")

    assert response.status_code == 200
    assert response.text.count("obsidian://open?path=") == 2
    assert response.text.count("在 Obsidian 中打开") == 2
    assert "记录的 Obsidian 文件当前不可用" not in response.text


def test_topic_detail_omits_missing_outside_and_absolute_obsidian_targets(tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    cards = (
        point("缺失文件观点", published_path="知识蒸馏器/missing.md"),
        point("越界文件观点", point_id="p2", published_path="../outside.md"),
        point("绝对路径观点", point_id="p3", published_path=str(outside)),
    )
    topic = topic_card(12, "安全入口主题", "坏路径不产生入口。", cards)
    current = snapshot(topics=(topic,), has_index=True, current=True)

    response = app_with_library(
        tmp_path, FakeTopicLibrary(current), vault_root=vault_root
    ).test_client().get("/knowledge/topics/12")

    assert response.status_code == 200
    assert all(card.statement in response.text for card in cards)
    assert response.text.count("的一句话总括。") == 3
    assert "obsidian://" not in response.text
    assert "在 Obsidian 中打开" not in response.text
    assert "记录的 Obsidian 文件当前不可用" not in response.text


def test_topic_detail_naturally_omits_missing_source_fields(tmp_path):
    cards = (
        point(
            "无来源字段观点一",
            source_label="来源信息未标注",
            platform="",
        ),
        point(
            "无来源字段观点二",
            point_id="p2",
            source_label="来源信息未标注",
            platform="",
        ),
    )
    topic = topic_card(13, "来源自然省略", "不补未知字段。", cards)
    current = snapshot(topics=(topic,), has_index=True, current=True)

    response = app_with_library(
        tmp_path, FakeTopicLibrary(current)
    ).test_client().get("/knowledge/topics/13")

    assert response.status_code == 200
    assert "来源：" not in response.text
    assert "来源信息未标注" not in response.text


def test_topic_get_is_read_only_and_never_organizes_or_republishes(tmp_path):
    database_path = tmp_path / "knowledge.sqlite3"
    vault_root = tmp_path / "vault"
    markdown_path = vault_root / "知识蒸馏器" / "topic-v2.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("probe markdown\n", encoding="utf-8")
    relative_path = "知识蒸馏器/topic-v2.md"
    add_two_point_formal_knowledge(database_path, relative_path)
    indexer = FixedTopicIndexer()
    library = TopicLibrary(database_path, indexer)
    assert library.refresh().kind is TopicRefreshKind.REFRESHED
    topic_id = library.snapshot().topics[0].topic_id
    before_database = database_snapshot(database_path)
    before_markdown = (
        hashlib.sha256(markdown_path.read_bytes()).hexdigest(),
        markdown_path.stat().st_mtime_ns,
        markdown_path.stat().st_ctime_ns,
    )
    app = create_app(
        database_path,
        topic_indexer=UnavailableIndexer(),
        obsidian_vault_root=vault_root,
    )
    app.config.update(TESTING=True, TOPIC_LIBRARY=library)

    response = app.test_client().get(f"/knowledge/topics/{topic_id}")

    assert response.status_code == 200
    assert response.text.index("核心观点必须") < response.text.index("其他观点具有")
    assert response.text.count("见《同一知识的观点级导航》") == 2
    assert response.text.count("一份知识可以向同一主题贡献") == 2
    assert response.text.count("在 Obsidian 中打开") == 2
    assert indexer.calls == 1
    assert database_snapshot(database_path) == before_database
    assert (
        hashlib.sha256(markdown_path.read_bytes()).hexdigest(),
        markdown_path.stat().st_mtime_ns,
        markdown_path.stat().st_ctime_ns,
    ) == before_markdown


def test_topic_detail_rejects_single_member_topic_even_when_library_returns_it(tmp_path):
    topic = topic_card(
        10,
        "过期单成员主题",
        "旧索引交集后只剩一个观点。",
        (point("唯一仍有效观点"),),
    )
    stale = snapshot(topics=(topic,), has_index=True, current=False)

    response = app_with_library(
        tmp_path, FakeTopicLibrary(stale)
    ).test_client().get("/knowledge/topics/10")

    assert response.status_code == 404
    assert "唯一仍有效观点" not in response.text


def test_topic_list_is_a_light_entry_list_without_counts_or_representative_points(tmp_path):
    cards = tuple(point(f"代表观点 {number}", point_id=f"p{number}") for number in range(1, 5))
    topic = topic_card(8, "可浏览主题", "只展示三条代表观点。", cards)
    current = snapshot(topics=(topic,), has_index=True, current=True)

    response = app_with_library(tmp_path, FakeTopicLibrary(current)).test_client().get("/knowledge")

    assert "可浏览主题" in response.text
    assert "只展示三条代表观点。" in response.text
    assert "4 个观点" not in response.text
    assert all(f"代表观点 {number}" not in response.text for number in range(1, 5))
    assert "knowledge_result_id" not in response.text


def test_formal_point_partial_stays_search_specific():
    from pathlib import Path

    template_root = Path(__file__).parents[1] / "src/knowledge_distiller/legacy/templates"

    assert '_formal_point_card.html' in (template_root / "knowledge.html").read_text()
    assert '_formal_point_card.html' not in (template_root / "topic.html").read_text()
    assert 'class="topic-point"' in (template_root / "topic.html").read_text()


def test_topic_read_failure_is_distinct_from_not_found(tmp_path):
    class BrokenLibrary(FakeTopicLibrary):
        def topic(self, topic_id):
            raise TopicLibraryError("internal sqlite detail")

    library = BrokenLibrary(snapshot())
    client = app_with_library(tmp_path, library).test_client()

    failed = client.get("/knowledge/topics/1")
    missing = app_with_library(tmp_path, FakeTopicLibrary(snapshot())).test_client().get(
        "/knowledge/topics/1"
    )

    assert failed.status_code == 500
    assert "这个主题暂时无法读取" in failed.text
    assert "internal sqlite detail" not in failed.text
    assert missing.status_code == 404
