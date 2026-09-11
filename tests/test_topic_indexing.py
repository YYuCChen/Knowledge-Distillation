import json
import sys
from types import SimpleNamespace

import pytest

from knowledge_distiller.topic_indexing import (
    ExistingTopicInput,
    TopicIndexFailure,
    TopicIndexingAdapter,
    AnthropicCompatibleTopicRuntime,
    TopicPointInput,
    TopicPointReference,
    _TopicRuntimeResult,
    normalize_topic_name,
    parse_topic_plan,
    build_topic_indexer,
)


POINTS = (
    TopicPointInput(1, 11, "p1", "core", "采购需要先报量。", "报量形成需求基础。", "采购机制", "报量与履约。"),
    TopicPointInput(1, 11, "p2", "other", "节约资金可按规则奖励。", "奖励关联实际节约。", "采购机制", "报量与履约。"),
    TopicPointInput(2, 22, "p3", "core", "本地文件应由用户持有。", "本地持有避免平台失效。", "知识所有权", "长期保存知识。"),
)
EXISTING = (
    ExistingTopicInput(7, "采购与履约", "收纳采购与履约知识。", (TopicPointReference(1, "p1"),)),
)


def payload(*, topics=None, unassigned=None):
    return json.dumps(
        {
            "topics": topics
            if topics is not None
            else [
                {
                    "topic_id": 7,
                    "name": "采购机制与履约",
                    "scope": "收纳报量、竞价和履约责任相关知识。",
                    "members": [
                        {"knowledge_result_id": 1, "point_id": "p1"},
                        {"knowledge_result_id": 1, "point_id": "p2"},
                    ],
                }
            ],
            "unassigned_points": unassigned
            if unassigned is not None
            else [{"knowledge_result_id": 2, "point_id": "p3"}],
        },
        ensure_ascii=False,
    )


def test_codec_reuses_existing_topic_and_covers_core_other_and_unassigned():
    plan = parse_topic_plan(payload(), POINTS, EXISTING)

    assert plan is not None
    assert plan.topics[0].topic_id == 7
    assert [member.point_id for member in plan.topics[0].members] == ["p1", "p2"]
    assert plan.unassigned_points == (TopicPointReference(2, "p3"),)


def test_codec_allows_point_in_multiple_two_member_topics_and_zero_topics():
    shared = {"knowledge_result_id": 1, "point_id": "p1"}
    plan = parse_topic_plan(
        payload(
            topics=[
                {
                    "topic_id": 7,
                    "name": "采购与履约",
                    "scope": "收纳采购流程与履约相关知识。",
                    "members": [shared, {"knowledge_result_id": 1, "point_id": "p2"}],
                },
                {
                    "new_topic_key": "new-1",
                    "name": "需求形成机制",
                    "scope": "收纳需求量形成方式相关知识。",
                    "members": [shared, {"knowledge_result_id": 2, "point_id": "p3"}],
                },
            ],
            unassigned=[],
        ),
        POINTS,
        EXISTING,
    )
    empty = parse_topic_plan(
        payload(
            topics=[],
            unassigned=[
                {"knowledge_result_id": point.knowledge_result_id, "point_id": point.point_id}
                for point in POINTS
            ],
        ),
        POINTS,
        EXISTING,
    )

    assert plan is not None and plan.topics[1].new_topic_key == "new-1"
    assert empty is not None and empty.topics == ()


def test_codec_rejects_single_member_new_and_reused_topics():
    new_topic = payload(
        topics=[
            {
                "new_topic_key": "new-1",
                "name": "采购需求形成",
                "scope": "收纳采购需求形成相关知识。",
                "members": [{"knowledge_result_id": 1, "point_id": "p1"}],
            }
        ],
        unassigned=[
            {"knowledge_result_id": 1, "point_id": "p2"},
            {"knowledge_result_id": 2, "point_id": "p3"},
        ],
    )
    reused_topic = payload(
        topics=[
            {
                "topic_id": 7,
                "name": "采购与履约",
                "scope": "收纳采购与履约相关知识。",
                "members": [{"knowledge_result_id": 1, "point_id": "p1"}],
            }
        ],
        unassigned=[
            {"knowledge_result_id": 1, "point_id": "p2"},
            {"knowledge_result_id": 2, "point_id": "p3"},
        ],
    )

    assert parse_topic_plan(new_topic, POINTS, EXISTING) is None
    assert parse_topic_plan(reused_topic, POINTS, EXISTING) is None


def test_codec_allows_two_different_points_from_same_knowledge_result():
    plan = parse_topic_plan(
        payload(
            topics=[
                {
                    "new_topic_key": "new-1",
                    "name": "采购需求与奖励机制",
                    "scope": "收纳采购需求形成与节约奖励相关知识。",
                    "members": [
                        {"knowledge_result_id": 1, "point_id": "p1"},
                        {"knowledge_result_id": 1, "point_id": "p2"},
                    ],
                }
            ],
            unassigned=[{"knowledge_result_id": 2, "point_id": "p3"}],
        ),
        POINTS,
        (),
    )

    assert plan is not None
    assert {member.knowledge_result_id for member in plan.topics[0].members} == {1}


def test_codec_rejects_repeated_member_instead_of_counting_it_twice():
    repeated = payload(
        topics=[
            {
                "new_topic_key": "new-1",
                "name": "采购需求形成",
                "scope": "收纳采购需求形成相关知识。",
                "members": [
                    {"knowledge_result_id": 1, "point_id": "p1"},
                    {"knowledge_result_id": 1, "point_id": "p1"},
                ],
            }
        ],
        unassigned=[
            {"knowledge_result_id": 1, "point_id": "p2"},
            {"knowledge_result_id": 2, "point_id": "p3"},
        ],
    )

    assert parse_topic_plan(repeated, POINTS, ()) is None


def test_codec_allows_omitted_existing_topic_with_remaining_point_unassigned():
    plan = parse_topic_plan(
        payload(
            topics=[],
            unassigned=[
                {"knowledge_result_id": point.knowledge_result_id, "point_id": point.point_id}
                for point in POINTS
            ],
        ),
        POINTS,
        EXISTING,
    )

    assert plan is not None
    assert plan.topics == ()
    assert plan.unassigned_points == tuple(point.reference for point in POINTS)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value["topics"][0].update(
            why_this_topic_exists="这是不允许输出的内部理由。"
        ),
        lambda value: value["topics"][0].update(topic_id=999),
        lambda value: value["topics"][0]["members"].append({"knowledge_result_id": 9, "point_id": "missing"}),
        lambda value: value["topics"][0]["members"].append({"knowledge_result_id": 1, "point_id": "p1"}),
        lambda value: value["unassigned_points"].append({"knowledge_result_id": 1, "point_id": "p1"}),
        lambda value: value.__setitem__("unassigned_points", []),
        lambda value: value["topics"][0].__setitem__("members", []),
        lambda value: value["topics"][0].__setitem__("name", "  "),
        lambda value: value["topics"][0].__setitem__("scope", "两行\n范围"),
        lambda value: value["topics"][0].__setitem__("name", "杂项"),
        lambda value: value["topics"][0].__setitem__("name", "其它知识"),
        lambda value: value["topics"][0].__setitem__("name", "杂项知识"),
        lambda value: value["topics"][0].__setitem__("name", "其他内容"),
        lambda value: value["topics"][0].__setitem__("name", "其他。"),
        lambda value: value["topics"][0].__setitem__("name", "综合"),
        lambda value: value["topics"][0].__setitem__("name", "未分类内容"),
        lambda value: value["topics"][0].__setitem__("name", "杂项 / 其他"),
        lambda value: value["topics"][0].__setitem__("name", "采购\u2028履约"),
        lambda value: value["topics"][0].__setitem__("scope", "采购\v履约"),
    ],
)
def test_codec_rejects_unknown_invalid_duplicate_conflicting_or_incomplete_output(mutate):
    value = json.loads(payload())
    mutate(value)

    assert parse_topic_plan(json.dumps(value, ensure_ascii=False), POINTS, EXISTING) is None


def test_codec_rejects_duplicate_nfkc_names_and_new_name_that_refuses_id_reuse():
    duplicate = json.loads(payload())
    duplicate["topics"].append(
        {
            "new_topic_key": "new-1",
            "name": "Ｃａｓｅ  Topic",
            "scope": "收纳测试知识。",
            "members": [
                {"knowledge_result_id": 1, "point_id": "p1"},
                {"knowledge_result_id": 2, "point_id": "p3"},
            ],
        }
    )
    duplicate["topics"].append(
        {
            "new_topic_key": "new-2",
            "name": "case topic",
            "scope": "收纳另一类测试知识。",
            "members": [
                {"knowledge_result_id": 1, "point_id": "p2"},
                {"knowledge_result_id": 2, "point_id": "p3"},
            ],
        }
    )
    duplicate["unassigned_points"] = []
    refuses_reuse = json.loads(payload())
    refuses_reuse["topics"][0] = {
        "new_topic_key": "new-1",
        "name": " 采购与履约 ",
        "scope": "收纳采购知识。",
        "members": refuses_reuse["topics"][0]["members"],
    }

    assert parse_topic_plan(json.dumps(duplicate, ensure_ascii=False), POINTS, EXISTING) is None
    assert parse_topic_plan(json.dumps(refuses_reuse, ensure_ascii=False), POINTS, EXISTING) is None
    assert normalize_topic_name("Ｃａｓｅ\n  TOPIC") == "case topic"


def test_codec_rejects_duplicate_topic_identity_and_new_topic_key():
    duplicate_id = json.loads(payload())
    duplicate_id["topics"].append(
        {
            "topic_id": 7,
            "name": "采购需求形成",
            "scope": "收纳需求量形成相关知识。",
            "members": [
                {"knowledge_result_id": 1, "point_id": "p2"},
                {"knowledge_result_id": 2, "point_id": "p3"},
            ],
        }
    )
    duplicate_id["unassigned_points"] = []
    duplicate_key = json.loads(payload())
    duplicate_key["topics"] = [
        {
            "new_topic_key": "same-key",
            "name": "需求形成机制",
            "scope": "收纳需求量形成相关知识。",
            "members": [
                {"knowledge_result_id": 1, "point_id": "p1"},
                {"knowledge_result_id": 1, "point_id": "p2"},
            ],
        },
        {
            "new_topic_key": "same-key",
            "name": "知识本地持有",
            "scope": "收纳知识本地持有相关知识。",
            "members": [
                {"knowledge_result_id": 1, "point_id": "p2"},
                {"knowledge_result_id": 2, "point_id": "p3"},
            ],
        },
    ]
    duplicate_key["unassigned_points"] = []

    assert parse_topic_plan(json.dumps(duplicate_id, ensure_ascii=False), POINTS, EXISTING) is None
    assert parse_topic_plan(json.dumps(duplicate_key, ensure_ascii=False), POINTS, EXISTING) is None


def test_codec_preserves_point_identity_whitespace_without_repairing_it():
    spaced_points = (
        TopicPointInput(
            1,
            11,
            " p1 ",
            "core",
            "采购需要先报量。",
            "报量形成需求基础。",
            "采购机制",
            "报量与履约。",
        ),
        TopicPointInput(
            1,
            11,
            "p2",
            "other",
            "节约资金可按规则奖励。",
            "奖励关联实际节约。",
            "采购机制",
            "报量与履约。",
        ),
    )
    exact = payload(
        topics=[
            {
                "new_topic_key": "new-1",
                "name": "采购需求形成",
                "scope": "收纳需求报量相关知识。",
                "members": [
                    {"knowledge_result_id": 1, "point_id": " p1 "},
                    {"knowledge_result_id": 1, "point_id": "p2"},
                ],
            }
        ],
        unassigned=[],
    )
    repaired = json.loads(exact)
    repaired["topics"][0]["members"][0]["point_id"] = "p1"

    plan = parse_topic_plan(exact, spaced_points, ())

    assert plan is not None
    assert plan.topics[0].members[0].point_id == " p1 "
    assert parse_topic_plan(
        json.dumps(repaired, ensure_ascii=False), spaced_points, ()
    ) is None


class StaticBinding:
    def __init__(self, text, stop_reason="end_turn"):
        self.result = _TopicRuntimeResult(text, stop_reason)

    def is_available(self):
        return True

    def complete(self, _points, _existing):
        return self.result


def test_adapter_requires_complete_end_and_never_repairs_invalid_output():
    incomplete = TopicIndexingAdapter(StaticBinding(payload(), "max_tokens")).organize(POINTS, EXISTING)
    invalid = TopicIndexingAdapter(StaticBinding("not-json")).organize(POINTS, EXISTING)

    assert incomplete.failure is TopicIndexFailure.INCOMPLETE
    assert invalid.failure is TopicIndexFailure.INVALID_OUTPUT


def test_anthropic_binding_disables_thinking_and_sends_only_allowed_inputs(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": payload()}], "stop_reason": "end_turn"}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    fake_httpx = SimpleNamespace(post=post, HTTPError=RuntimeError)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    runtime = AnthropicCompatibleTopicRuntime("https://model.test", "topic-model", "secret")

    result = runtime.complete(POINTS, EXISTING)

    body = captured["json"]
    user_text = body["messages"][0]["content"]
    assert result.stop_reason == "end_turn"
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert body["model"] == "topic-model"
    assert "statement" in user_text and "argument" in user_text
    system_prompt = body["system"]
    assert "至少两个不同的当前输入观点" in system_prompt
    assert "两个成员只是必要条件，不是充分条件" in system_prompt
    assert "不得把无关观点" in system_prompt
    assert "优先使用 unassigned_points" in system_prompt
    assert "无Markdown、解释或理由字段" in system_prompt
    assert "why_this_topic_exists" not in system_prompt
    for forbidden in ("evidence", "published_path", "author", "platform", "obsidian", "secret"):
        assert forbidden not in user_text.casefold()


def test_topic_configuration_takes_priority_then_falls_back_to_derivation(monkeypatch):
    for name in (
        "KNOWLEDGE_DISTILLER_TOPIC_BASE_URL",
        "KNOWLEDGE_DISTILLER_TOPIC_MODEL",
        "KNOWLEDGE_DISTILLER_TOPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_DERIVATION_BASE_URL", "https://fallback.test")
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_DERIVATION_MODEL", "fallback-model")
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_DERIVATION_API_KEY", "fallback-key")
    fallback = build_topic_indexer().binding
    assert (fallback.base_url, fallback.model, fallback.api_key) == (
        "https://fallback.test",
        "fallback-model",
        "fallback-key",
    )

    monkeypatch.setenv("KNOWLEDGE_DISTILLER_TOPIC_BASE_URL", "https://topic.test")
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_TOPIC_MODEL", "topic-model")
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_TOPIC_API_KEY", "topic-key")
    topic = build_topic_indexer().binding
    assert (topic.base_url, topic.model, topic.api_key) == (
        "https://topic.test",
        "topic-model",
        "topic-key",
    )
